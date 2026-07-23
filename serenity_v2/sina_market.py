"""
Serenity 2.0 — Sina 实时行情接入（影子冒烟测试用）

安全边界:
  · 仅访问 hq.sinajs.cn
  · 超时 + 重试上限 + 频率限制
  · 原始响应+标准化分层保存
  · 不覆盖历史原始数据
  · 异常数据进入隔离区
  · 网络异常时停止生成行动级信号

用法:
    python -m serenity_v2.sina_market [秒数]
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import sys
import time as _time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from urllib import request, error

CST = timezone(timedelta(hours=8))
logger = logging.getLogger("serenity_v2.sina_market")

# ---------------------------------------------------------------------------
# 网络边界
# ---------------------------------------------------------------------------
SINA_QUOTE_URL = "http://hq.sinajs.cn/list="
ALLOWED_HOSTS = {"hq.sinajs.cn"}
REQUEST_TIMEOUT = 10
MAX_RETRIES = 2
RETRY_DELAY = 2.0
MIN_REQUEST_INTERVAL = 1.0
MAX_ANOMALIES_BEFORE_STOP = 10

SMOKE_TEST_SYMBOLS = ["600487", "600176", "000988"]


def _to_sina_code(code: str) -> str:
    if code.startswith(("6", "5")):
        return f"sh{code}"
    elif code.startswith(("0", "3", "2")):
        return f"sz{code}"
    raise ValueError(f"无法映射到Sina代码: {code}")


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass
class RawQuoteRecord:
    symbol: str = ""
    source: str = "sina_realtime"
    collected_at: str = ""
    raw_payload: str = ""
    raw_payload_hash: str = ""
    http_status: int = 0
    response_time_ms: float = 0.0


@dataclass
class NormalizedQuote:
    symbol: str = ""
    source: str = "sina_realtime"
    source_timestamp: str = ""
    exchange_timestamp: str = ""
    collected_at: str = ""
    normalized_at: str = ""
    business_time: str = ""
    name: str = ""
    price: float = 0.0
    previous_close: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    volume: int = 0
    amount: float = 0.0
    raw_payload_hash: str = ""
    data_age_ms: float = 0.0
    validation_status: str = "pending"
    validation_errors: list = field(default_factory=list)
    stale_for_trading: bool = False
    acceptable_as_postmarket_snapshot: bool = False
    action_eligible: bool = False


@dataclass
class SmokeMetrics:
    requests_total: int = 0
    requests_success: int = 0
    requests_timeout: int = 0
    requests_error: int = 0
    total_response_time_ms: float = 0.0
    max_response_time_ms: float = 0.0
    records_raw: int = 0
    records_normalized: int = 0
    records_duplicate: int = 0
    records_rejected: int = 0
    field_missing_count: int = 0
    stale_count: int = 0
    future_timestamp_count: int = 0
    negative_price_count: int = 0
    silent_zero_fill_count: int = 0
    symbols_mapped: int = 0
    symbols_unmapped: int = 0
    anomalies_consecutive: int = 0
    auto_stop_triggered: bool = False
    auto_stop_reason: str = ""
    network_targets: set = field(default_factory=set)
    # 循环级计时明细（用于 P50/P95/MAX 口径）
    http_response_times_ms: list = field(default_factory=list)
    cycle_times_ms: list = field(default_factory=list)
    cycle_timings: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# 抓取器
# ---------------------------------------------------------------------------

class SinaQuoteFetcher:

    def __init__(self):
        self._last_request_time: float = 0.0
        self.metrics = SmokeMetrics()

    def fetch(self, symbols: list) -> list:
        results = []
        sina_codes = []
        for sym in symbols:
            try:
                sina_codes.append(_to_sina_code(sym))
                self.metrics.symbols_mapped += 1
            except ValueError as e:
                logger.warning(f"代码映射失败: {sym} — {e}")
                self.metrics.symbols_unmapped += 1
                self.metrics.anomalies_consecutive += 1
                continue

        if not sina_codes:
            return results

        url = SINA_QUOTE_URL + ",".join(sina_codes)
        elapsed = _time.monotonic() - self._last_request_time
        if elapsed < MIN_REQUEST_INTERVAL:
            _time.sleep(MIN_REQUEST_INTERVAL - elapsed)
        self._last_request_time = _time.monotonic()
        self.metrics.requests_total += 1

        collected_at = datetime.now(tz=CST).isoformat(timespec="milliseconds")
        payload = ""
        http_status = 0
        t_start = _time.monotonic()

        for attempt in range(1 + MAX_RETRIES):
            try:
                req = request.Request(url, headers={
                    "User-Agent": "SerenityMonitor/2.0 (shadow smoke test)",
                    "Referer": "http://finance.sina.com.cn",
                })
                with request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                    http_status = resp.status
                    raw_bytes = resp.read()
                    try:
                        payload = raw_bytes.decode("gbk")
                    except UnicodeDecodeError:
                        payload = raw_bytes.decode("gbk", errors="replace")
                self.metrics.network_targets.add("hq.sinajs.cn")
                break
            except error.URLError as e:
                http_status = getattr(e, "code", 0)
                if attempt <= MAX_RETRIES:
                    logger.warning(f"Sina重试 {attempt}/{MAX_RETRIES}: {e}")
                    _time.sleep(RETRY_DELAY)
                else:
                    logger.error(f"Sina请求失败: {e}")
                    self.metrics.requests_error += 1
                    self.metrics.anomalies_consecutive += 1
            except Exception as e:
                logger.error(f"Sina异常: {e}")
                self.metrics.requests_error += 1
                self.metrics.anomalies_consecutive += 1
                break

        t_end = _time.monotonic()
        response_time_ms = (t_end - t_start) * 1000.0

        if http_status == 200:
            self.metrics.requests_success += 1
            self.metrics.total_response_time_ms += response_time_ms
            self.metrics.max_response_time_ms = max(
                self.metrics.max_response_time_ms, response_time_ms)
            lines = payload.strip().split("\n") if payload else []
            for i, sym in enumerate(symbols):
                line = lines[i] if i < len(lines) else ""
                payload_hash = hashlib.sha256(
                    f"{sym}_{collected_at}_{line}".encode()).hexdigest()[:32]
                results.append(RawQuoteRecord(
                    symbol=sym, source="sina_realtime",
                    collected_at=collected_at, raw_payload=line,
                    raw_payload_hash=payload_hash,
                    http_status=http_status, response_time_ms=response_time_ms))
        else:
            self.metrics.requests_timeout += 1
            self.metrics.anomalies_consecutive += 1
            for sym in symbols:
                results.append(RawQuoteRecord(
                    symbol=sym, source="sina_realtime",
                    collected_at=collected_at, raw_payload=payload,
                    raw_payload_hash="", http_status=http_status,
                    response_time_ms=response_time_ms))

        if self.metrics.anomalies_consecutive >= MAX_ANOMALIES_BEFORE_STOP:
            self.metrics.auto_stop_triggered = True
            self.metrics.auto_stop_reason = (
                f"连续异常{self.metrics.anomalies_consecutive}次")

        return results

    def normalize(self, raw: RawQuoteRecord,
                  business_time: str | None = None) -> NormalizedQuote | None:
        nq = NormalizedQuote(
            symbol=raw.symbol, source="sina_realtime",
            collected_at=raw.collected_at,
            normalized_at=datetime.now(tz=CST).isoformat(timespec="milliseconds"),
            business_time=business_time or "",
            raw_payload_hash=raw.raw_payload_hash)

        if not raw.raw_payload or raw.http_status != 200:
            nq.validation_status = "invalid"
            nq.validation_errors.append("empty_payload_or_http_error")
            self.metrics.records_rejected += 1
            return nq

        try:
            content = raw.raw_payload
            if "=" in content:
                content = content.split("=", 1)[1].strip().strip('"').strip(";").strip('"')
            fields = content.split(",")
            if len(fields) < 32:
                nq.validation_status = "invalid"
                nq.validation_errors.append(f"insufficient_fields:{len(fields)}")
                self.metrics.records_rejected += 1
                self.metrics.field_missing_count += 1
                return nq

            nq.name = fields[0].strip()
            nq.open = self._sf(fields[1])
            nq.previous_close = self._sf(fields[2])
            nq.price = self._sf(fields[3])
            nq.high = self._sf(fields[4])
            nq.low = self._sf(fields[5])
            nq.volume = self._si(fields[8])
            nq.amount = self._sf(fields[9]) * 10000  # 万元→元
            if len(fields) > 30:
                nq.source_timestamp = f"{fields[30].strip()}T{fields[31].strip()}+08:00" if len(fields) > 31 else ""
        except Exception as e:
            nq.validation_status = "invalid"
            nq.validation_errors.append(f"parse_error:{e}")
            self.metrics.records_rejected += 1
            return nq

        if nq.source_timestamp:
            try:
                src_dt = datetime.fromisoformat(nq.source_timestamp)
                col_dt = datetime.fromisoformat(nq.collected_at)
                nq.data_age_ms = (col_dt - src_dt).total_seconds() * 1000
            except (ValueError, TypeError):
                pass

        nq = self._validate(nq)
        if nq.validation_status == "valid":
            self.metrics.records_normalized += 1
        else:
            self.metrics.records_rejected += 1
        return nq

    def _validate(self, nq: NormalizedQuote) -> NormalizedQuote:
        errors = []

        # —— 价格字段检查 ——
        for f in ["price", "previous_close", "open"]:
            v = getattr(nq, f, 0)
            if v < 0:
                errors.append(f"negative_{f}")
                self.metrics.negative_price_count += 1
            elif v == 0:
                errors.append(f"zero_{f}")
                self.metrics.silent_zero_fill_count += 1

        # —— 时间戳检查 ——
        if nq.source_timestamp:
            try:
                src_dt = datetime.fromisoformat(nq.source_timestamp)
                if src_dt > datetime.now(tz=CST) + timedelta(minutes=5):
                    errors.append("future_timestamp")
                    self.metrics.future_timestamp_count += 1
            except (ValueError, TypeError):
                errors.append("unparseable_timestamp")
                self.metrics.field_missing_count += 1

        # —— 新鲜度分级（盘后/非交易时段分类） ——
        from .clock import get_clock
        session = get_clock().market_session()

        # 默认：数据新鲜，可用于交易决策
        nq.stale_for_trading = False
        nq.acceptable_as_postmarket_snapshot = False
        nq.action_eligible = True

        if session in ("CONTINUOUS_AM", "CONTINUOUS_PM"):
            # 交易时段：严格 5 分钟过期
            if nq.data_age_ms > 300_000:
                nq.stale_for_trading = True
                nq.action_eligible = False
                errors.append("stale_data")
                self.metrics.stale_count += 1
        elif nq.data_age_ms > 86_400_000:
            # 超过 24h：任何场景都不可用
            nq.stale_for_trading = True
            nq.action_eligible = False
            errors.append("stale_data_overnight")
            self.metrics.stale_count += 1
        else:
            # 非交易时段，数据 < 24h
            # 判断是否同日快照
            if nq.source_timestamp:
                try:
                    src_dt = datetime.fromisoformat(nq.source_timestamp)
                    today = get_clock().today()
                    if src_dt.date() == today:
                        # 同日收盘数据：不可用于交易，但可作为盘后快照
                        nq.stale_for_trading = True
                        nq.acceptable_as_postmarket_snapshot = True
                        nq.action_eligible = False
                    else:
                        # 前一交易日数据：过期
                        nq.stale_for_trading = True
                        nq.action_eligible = False
                        errors.append("stale_data_overnight")
                        self.metrics.stale_count += 1
                except (ValueError, TypeError):
                    pass

        # —— 判定整体有效性 ——
        # acceptable_as_postmarket_snapshot 场景：数据有效但不可交易
        if nq.acceptable_as_postmarket_snapshot:
            nq.validation_status = "valid"
        elif errors:
            nq.validation_status = "invalid"
        else:
            nq.validation_status = "valid"

        nq.validation_errors = errors
        return nq

    @staticmethod
    def _sf(val: str) -> float:
        try:
            return float(val)
        except (ValueError, TypeError):
            return 0.0

    @staticmethod
    def _si(val: str) -> int:
        try:
            return int(float(val))
        except (ValueError, TypeError):
            return 0


# ---------------------------------------------------------------------------
# 持久化
# ---------------------------------------------------------------------------

def _init_tables(db_path: Path):
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sina_raw_quotes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                source TEXT DEFAULT 'sina_realtime',
                collected_at TEXT NOT NULL,
                raw_payload TEXT,
                raw_payload_hash TEXT,
                http_status INTEGER DEFAULT 0,
                response_time_ms REAL DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            );
            CREATE TABLE IF NOT EXISTS sina_normalized_quotes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                source TEXT DEFAULT 'sina_realtime',
                source_timestamp TEXT,
                exchange_timestamp TEXT,
                collected_at TEXT NOT NULL,
                normalized_at TEXT,
                business_time TEXT,
                name TEXT,
                price REAL,
                previous_close REAL,
                open REAL,
                high REAL,
                low REAL,
                volume INTEGER,
                amount REAL,
                raw_payload_hash TEXT,
                data_age_ms REAL,
                validation_status TEXT DEFAULT 'pending',
                validation_errors TEXT,
                stale_for_trading INTEGER DEFAULT 0,
                acceptable_as_postmarket_snapshot INTEGER DEFAULT 0,
                action_eligible INTEGER DEFAULT 1,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_dedup
                ON sina_raw_quotes(symbol, collected_at, raw_payload_hash);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_norm_dedup
                ON sina_normalized_quotes(symbol, source_timestamp, price, volume);
            CREATE TABLE IF NOT EXISTS sina_smoke_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_at TEXT NOT NULL,
                metrics_json TEXT NOT NULL
            );
        """)
        # 向前兼容：为旧表添加 v2 新增字段
        for col, default in [
            ("stale_for_trading", "0"),
            ("acceptable_as_postmarket_snapshot", "0"),
            ("action_eligible", "1"),
        ]:
            try:
                conn.execute(
                    f"ALTER TABLE sina_normalized_quotes "
                    f"ADD COLUMN {col} INTEGER DEFAULT {default}"
                )
            except sqlite3.OperationalError:
                pass  # 字段已存在
        conn.commit()
    finally:
        conn.close()


def store_raw(db_path: Path, records: list) -> int:
    conn = sqlite3.connect(str(db_path))
    stored = 0
    try:
        for r in records:
            try:
                conn.execute(
                    """INSERT INTO sina_raw_quotes
                       (symbol, source, collected_at, raw_payload,
                        raw_payload_hash, http_status, response_time_ms)
                       VALUES (?,?,?,?,?,?,?)""",
                    (r.symbol, r.source, r.collected_at, r.raw_payload,
                     r.raw_payload_hash, r.http_status, r.response_time_ms))
                stored += 1
            except sqlite3.IntegrityError:
                pass
        conn.commit()
    finally:
        conn.close()
    return stored


def store_normalized(db_path: Path, records: list) -> int:
    conn = sqlite3.connect(str(db_path))
    stored = 0
    try:
        for nq in records:
            try:
                conn.execute(
                    """INSERT INTO sina_normalized_quotes
                       (symbol, source, source_timestamp, exchange_timestamp,
                        collected_at, normalized_at, business_time,
                        name, price, previous_close, open, high, low,
                        volume, amount, raw_payload_hash, data_age_ms,
                        validation_status, validation_errors,
                        stale_for_trading, acceptable_as_postmarket_snapshot,
                        action_eligible)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (nq.symbol, nq.source, nq.source_timestamp, nq.exchange_timestamp,
                     nq.collected_at, nq.normalized_at, nq.business_time,
                     nq.name, nq.price, nq.previous_close, nq.open,
                     nq.high, nq.low, nq.volume, nq.amount,
                     nq.raw_payload_hash, nq.data_age_ms,
                     nq.validation_status,
                     json.dumps(nq.validation_errors, ensure_ascii=False),
                     int(nq.stale_for_trading),
                     int(nq.acceptable_as_postmarket_snapshot),
                     int(nq.action_eligible)))
                stored += 1
            except sqlite3.IntegrityError:
                pass
        conn.commit()
    finally:
        conn.close()
    return stored


def store_metrics_snapshot(db_path: Path, metrics: SmokeMetrics) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        now = datetime.now(tz=CST).isoformat(timespec="seconds")
        d = asdict(metrics)
        d["network_targets"] = list(metrics.network_targets)
        conn.execute(
            "INSERT INTO sina_smoke_metrics (snapshot_at, metrics_json) VALUES (?,?)",
            (now, json.dumps(d, ensure_ascii=False)))
        conn.commit()
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def fetch_and_store(symbols=None, db_path=None, business_time=None):
    from .env import get_env
    if symbols is None:
        symbols = SMOKE_TEST_SYMBOLS
    if db_path is None:
        db_path = get_env().db_path
    _init_tables(db_path)
    fetcher = SinaQuoteFetcher()
    raw_records = fetcher.fetch(symbols)
    raw_stored = store_raw(db_path, raw_records)
    dup = len(raw_records) - raw_stored
    normalized = []
    for raw in raw_records:
        nq = fetcher.normalize(raw, business_time=business_time)
        if nq is not None:
            normalized.append(nq)
    norm_stored = store_normalized(db_path, normalized)
    rejected = len(normalized) - norm_stored
    store_metrics_snapshot(db_path, fetcher.metrics)
    m = fetcher.metrics
    return {
        "symbols": symbols,
        "raw_count": len(raw_records), "raw_stored": raw_stored,
        "dup_skipped": dup,
        "norm_count": len(normalized), "norm_stored": norm_stored,
        "rejected": rejected,
        "metrics_summary": {
            "requests_total": m.requests_total,
            "requests_success": m.requests_success,
            "response_time_max_ms": m.max_response_time_ms,
            "response_time_avg_ms": (
                m.total_response_time_ms / m.requests_success
                if m.requests_success > 0 else 0),
            "records_normalized": m.records_normalized,
            "records_rejected": m.records_rejected,
            "field_missing": m.field_missing_count,
            "stale": m.stale_count,
            "future_timestamp": m.future_timestamp_count,
            "negative_price": m.negative_price_count,
            "silent_zero": m.silent_zero_fill_count,
            "anomalies_consecutive": m.anomalies_consecutive,
            "auto_stop": m.auto_stop_triggered,
        },
        "auto_stop": m.auto_stop_triggered,
        "network_targets": list(m.network_targets),
    }


def _p50(values: list) -> float:
    """中位数。"""
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    if n % 2 == 1:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2.0


def _p95(values: list) -> float:
    """P95。"""
    if not values:
        return 0.0
    s = sorted(values)
    idx = max(0, int(len(s) * 0.95) - 1)
    return s[idx]


def run_smoke_test(duration_seconds=900, interval_seconds=5):
    """固定频率沙冒烟测试。

    调度策略:
      · 固定频率: 从 start 起每 interval_seconds 安排一个周期。
      · 不追赶: 若某周期超时则跳过已错过的时点，从当前时间重新对齐。
      · 不重叠: 同步执行，同一时刻只有一个请求。
    """
    from .env import get_env, set_env, SerenityEnv
    from .clock import get_clock
    try:
        get_env()
    except RuntimeError:
        ROOT = Path(__file__).resolve().parent.parent
        set_env(SerenityEnv.shadow(
            db_path=ROOT / "shadow_data" / "shadow.db",
            log_dir=ROOT / "logs" / "shadow"))
    env = get_env()
    db_path = env.db_path
    _init_tables(db_path)
    clock = get_clock()
    fetcher = SinaQuoteFetcher()
    symbols = SMOKE_TEST_SYMBOLS

    start_mono = _time.monotonic()
    start_dt = datetime.now(tz=CST)
    cycle = 0
    next_cycle_mono = start_mono  # 首个周期立即启动

    # 每周期计时明细
    cycle_timings: list[dict] = []

    print(f"🔬 Sina行情冒烟 | 环境:{env.mode} | DB:{db_path.resolve()}")
    print(f"   标的:{symbols} | 时长:{duration_seconds}s | 间隔:{interval_seconds}s")
    print(f"   启动:{start_dt.isoformat(timespec='seconds')}\n")
    try:
        while True:
            now_mono = _time.monotonic()
            elapsed_total = now_mono - start_mono
            if elapsed_total >= duration_seconds:
                break

            # —— 固定频率等待 ——
            wait = next_cycle_mono - now_mono
            if wait > 0:
                _time.sleep(wait)
            elif wait < -interval_seconds:
                # 落后超过一个完整周期 → 跳过已错过的时点，从当前时间对齐
                next_cycle_mono = now_mono
            # else: 轻微落后（< 1 周期）→ 立即启动

            cycle += 1
            scheduled_mono = next_cycle_mono
            scheduled_dt = datetime.now(tz=CST)

            # —— 请求计时 ——
            request_start_mono = _time.monotonic()
            request_start_dt = datetime.now(tz=CST)

            bt = clock.now().isoformat(timespec="seconds")
            print(f"[{cycle}] {request_start_dt.strftime('%H:%M:%S')} "
                  f"抓取...", end=" ", flush=True)

            raw = fetcher.fetch(symbols)

            request_end_mono = _time.monotonic()
            http_time_ms = (request_end_mono - request_start_mono) * 1000.0

            rs = store_raw(db_path, raw)
            dup = len(raw) - rs

            norms = []
            for r in raw:
                nq = fetcher.normalize(r, business_time=bt)
                if nq is not None:
                    norms.append(nq)
            ns = store_normalized(db_path, norms)
            rej = len(norms) - ns

            cycle_end_mono = _time.monotonic()
            cycle_time_ms = (cycle_end_mono - request_start_mono) * 1000.0

            # 记录本周期计时
            timing = {
                "cycle": cycle,
                "scheduled_at": scheduled_dt.isoformat(timespec="milliseconds"),
                "request_started_at": request_start_dt.isoformat(timespec="milliseconds"),
                "http_time_ms": round(http_time_ms, 1),
                "cycle_time_ms": round(cycle_time_ms, 1),
            }
            cycle_timings.append(timing)
            fetcher.metrics.http_response_times_ms.append(http_time_ms)
            fetcher.metrics.cycle_times_ms.append(cycle_time_ms)
            fetcher.metrics.cycle_timings.append(timing)

            # 输出
            valid_norms = [n for n in norms if n.validation_status == "valid"]
            prices = [f"{n.symbol}={n.price:.2f}" for n in valid_norms]

            flags = []
            actionable = [n for n in norms if n.action_eligible]
            stale_trading = [n for n in norms if n.stale_for_trading]
            pm_snap = [n for n in norms if n.acceptable_as_postmarket_snapshot]
            if actionable:
                flags.append(f"actionable={len(actionable)}")
            if stale_trading:
                flags.append(f"stale_for_trading={len(stale_trading)}")
            if pm_snap:
                flags.append(f"pm_snapshot={len(pm_snap)}")
            flag_str = f" [{', '.join(flags)}]" if flags else ""

            print(f"✅ raw={rs}({dup}dup) norm={ns}({rej}rej) "
                  f"http={http_time_ms:.0f}ms cyc={cycle_time_ms:.0f}ms{flag_str}"
                  + (f" [{', '.join(prices)}]" if prices else " [无有效行情]"))

            for nq in norms:
                if nq.validation_status != "valid":
                    print(f"  ⚠ {nq.symbol}: {nq.validation_status} — "
                          f"{'; '.join(nq.validation_errors)}")
                elif nq.acceptable_as_postmarket_snapshot:
                    print(f"  ℹ {nq.symbol}: valid(pm_snapshot) "
                          f"stale_for_trading=true action_eligible=false")

            if fetcher.metrics.auto_stop_triggered:
                print(f"\n🚨 自动停止: {fetcher.metrics.auto_stop_reason}")
                break

            # 调度下一周期
            next_cycle_mono = scheduled_mono + interval_seconds

    except KeyboardInterrupt:
        print("\n⏸️ 手动停止")
    finally:
        store_metrics_snapshot(db_path, fetcher.metrics)
        m = fetcher.metrics
        elapsed = _time.monotonic() - start_mono

        # 计算延迟分位
        http_p50 = _p50(m.http_response_times_ms)
        http_p95 = _p95(m.http_response_times_ms)
        http_max = max(m.http_response_times_ms) if m.http_response_times_ms else 0.0
        cyc_p50 = _p50(m.cycle_times_ms)
        cyc_p95 = _p95(m.cycle_times_ms)
        cyc_max = max(m.cycle_times_ms) if m.cycle_times_ms else 0.0

        print(f"\n{'='*60}")
        print(f"  冒烟完成")
        print(f"  {'─'*56}")
        print(f"  历时: {elapsed:.1f}s | 周期: {cycle} | "
              f"期望: {duration_seconds // interval_seconds}")
        print(f"  请求: {m.requests_total} | 成功: {m.requests_success} | "
              f"超时: {m.requests_timeout} | 错误: {m.requests_error}")
        print(f"  成功率: {m.requests_success / max(1, m.requests_total) * 100:.0f}%")
        print(f"  {'─'*56}")
        print(f"  📡 HTTP 延迟 (ms): "
              f"P50={http_p50:.0f} P95={http_p95:.0f} MAX={http_max:.0f}")
        print(f"  🔄 周期耗时 (ms): "
              f"P50={cyc_p50:.0f} P95={cyc_p95:.0f} MAX={cyc_max:.0f}")
        print(f"  {'─'*56}")
        print(f"  原始记录: {m.records_raw} | 标准化: {m.records_normalized} | "
              f"拒绝: {m.records_rejected}")
        print(f"  缺字段: {m.field_missing_count} | 过期: {m.stale_count} | "
              f"未来时间: {m.future_timestamp_count}")
        print(f"  负价: {m.negative_price_count} | 补零: {m.silent_zero_fill_count}")
        print(f"  网络目标: {sorted(m.network_targets)} | "
              f"自动停止: {m.auto_stop_triggered}")


if __name__ == "__main__":
    duration = 900
    if len(sys.argv) > 1:
        try:
            duration = int(sys.argv[1])
        except ValueError:
            print(f"用法: python -m serenity_v2.sina_market [秒数]")
            sys.exit(1)
    run_smoke_test(duration_seconds=duration)
