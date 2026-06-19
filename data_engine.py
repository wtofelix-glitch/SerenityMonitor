"""
数据引擎 — 通过新浪 API 获取 A 股实时行情与历史数据
"""
import urllib.request
import json
import re
import time
import random
from functools import wraps
from datetime import datetime, date
from typing import Optional

from config import STOCK_MAP, ALL_CODES, SINA_PREFIX
from serenity_logger import get_logger

log = get_logger(__name__)

try:
    from metrics import API_CALLS, API_ERRORS, CACHE_HITS, CACHE_MISSES
    METRICS_AVAILABLE = True
except ImportError:
    METRICS_AVAILABLE = False


# ── 重试装饰器（指数退避 + 抖动） ──────────────────────

def retry(max_attempts: int = 3, base_delay: float = 1.0, backoff: float = 2.0):
    """
    通用重试装饰器，捕获所有 Exception。

    退避策略: base_delay * backoff^(attempt-1) + random(0, 0.5)
    第1次重试等待 ~1s, 第2次 ~2s, 第3次 ~4s
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exc = e
                    if attempt < max_attempts - 1:
                        delay = base_delay * (backoff ** attempt) + random.uniform(0, 0.5)
                        log.warning(
                            "%s 失败(第%d/%d次): %s, %.1fs后重试",
                            func.__name__, attempt + 1, max_attempts, e, delay,
                        )
                        time.sleep(delay)
            log.error("%s %d次重试后仍失败: %s", func.__name__, max_attempts, last_exc)
            raise last_exc
        return wrapper
    return decorator


@retry(max_attempts=3, base_delay=1.0)
def sina_fetch_raw(code_list: list[str]) -> str:
    """
    通过新浪 API 获取股票实时行情
    返回 CSV 格式原始数据
    网络抖动时自动重试 3 次（1s → 2s 退避）
    """
    codes = []
    for c in code_list:
        mkt = STOCK_MAP[c]["market"]
        codes.append(f"{mkt}{c}")

    url = SINA_PREFIX + ",".join(codes)
    # A 股数据不走代理
    proxy_handler = urllib.request.ProxyHandler({})
    opener = urllib.request.build_opener(proxy_handler)
    req = urllib.request.Request(url)
    req.add_header("Referer", "https://finance.sina.com.cn")
    with opener.open(req, timeout=10) as resp:
        raw = resp.read().decode("gbk")
    return raw


def parse_sina_line(line: str) -> Optional[dict]:
    """
    解析新浪单行行情数据
    格式: var hq_str_sh600000="浦东发展,15.20,15.30,..."
    """
    if not line.startswith("var hq_str_"):
        return None
    try:
        # 提取股票代码
        code_match = re.search(r'var hq_str_(\w+)="', line)
        if not code_match:
            return None
        full_code = code_match.group(1)  # sh300782 或 sz300308
        code = full_code[2:]  # 去掉 sh/sz 前缀

        # 提取数据部分
        data_str = line.split('="')[1].rstrip('";\n')
        fields = data_str.split(",")

        return {
            "code": code,
            "name": fields[0],
            "open": float(fields[1]) if fields[1] else 0,
            "close_yesterday": float(fields[2]) if fields[2] else 0,
            "price": float(fields[3]) if fields[3] else 0,  # 当前价
            "high": float(fields[4]) if fields[4] else 0,
            "low": float(fields[5]) if fields[5] else 0,
            "volume": int(fields[8]) if fields[8] else 0,  # 手
            "amount": float(fields[9]) if fields[9] else 0,  # 万元
            "buy1": float(fields[10]) if fields[10] else 0,
            "sell1": float(fields[12]) if fields[12] else 0,
            "date": fields[30] if len(fields) > 30 else "",
            "time": fields[31] if len(fields) > 31 else "",
        }
    except (ValueError, IndexError, AttributeError):
        return None


def fetch_realtime(code_list: Optional[list[str]] = None,
                   source: str = "sina") -> list[dict]:
    """
    获取多只股票的实时行情
    返回解析后的字典列表

    Parameters
    ----------
    source : str
        "sina" (默认) 或 "akshare"。Sina 快但有时限流，AKShare 稳但首调用慢
    """
    if code_list is None:
        code_list = ALL_CODES

    # Filter out pseudo-codes like 'CASH' that don't exist on Sina
    code_list = [c for c in code_list if c != "CASH"]

    if source == "akshare":
        return _tencent_fetch_realtime(code_list)
    if source == "tencent":
        return _tencent_fetch_realtime(code_list)

    # ── 默认：Sina ──
    if METRICS_AVAILABLE:
        API_CALLS.labels(source="sina").inc()
    try:
        raw = sina_fetch_raw(code_list)
    except Exception:
        if METRICS_AVAILABLE:
            API_ERRORS.labels(source="sina").inc()
        raise

    results = []
    for line in raw.strip().split("\n"):
        parsed = parse_sina_line(line)
        if parsed:
            results.append(parsed)
    return results


def _akshare_fetch_realtime(code_list: list[str]) -> list[dict]:
    """
    通过 AKShare 获取 A 股实时行情（备用数据源）
    映射到与 Sina 相同字段格式，确保下游模块无感切换
    """
    import akshare as ak

    if METRICS_AVAILABLE:
        API_CALLS.labels(source="akshare").inc()

    try:
        df = ak.stock_zh_a_spot_em()
    except Exception as e:
        if METRICS_AVAILABLE:
            API_ERRORS.labels(source="akshare").inc()
        log.warning("AKShare 获取行情失败: %s", e)
        raise

    # 构建代码→行映射（AKShare 使用 6 位纯数字代码）
    target_codes = {c for c in code_list}
    results = []

    for _, row in df.iterrows():
        code = str(row.get("代码", "")).strip()
        if code not in target_codes:
            continue
        try:
            price = float(row.get("最新价", 0) or 0)
            close_y = float(row.get("昨收", 0) or 0)
            results.append({
                "code": code,
                "name": str(row.get("名称", code)),
                "open": float(row.get("今开", 0) or 0),
                "close_yesterday": close_y,
                "price": price,
                "high": float(row.get("最高", 0) or 0),
                "low": float(row.get("最低", 0) or 0),
                "volume": int(float(row.get("成交量", 0) or 0)),
                "amount": float(row.get("成交额", 0) or 0),
                "buy1": 0,
                "sell1": 0,
                "date": datetime.now().strftime("%Y-%m-%d"),
                "time": datetime.now().strftime("%H:%M:%S"),
            })
        except (ValueError, TypeError):
            continue

    return results


def _tencent_fetch_realtime(code_list: list[str]) -> list[dict]:
    """
    通过腾讯行情接口获取 A 股实时行情（备用数据源）
    URL: http://qt.gtimg.cn/q=sh600141,sz000001
    速度 ~1s，直连无需代理，无墙
    """
    import httpx
    from urllib.parse import urlencode

    if METRICS_AVAILABLE:
        API_CALLS.labels(source="tencent").inc()

    # 6开头→sh, 0/3开头→sz
    prefixed = []
    for c in code_list:
        if c.startswith(("6", "60")):
            prefixed.append(f"sh{c}")
        else:
            prefixed.append(f"sz{c}")

    url = f"http://qt.gtimg.cn/q={','.join(prefixed)}"

    try:
        r = httpx.get(url, timeout=8)
        r.raise_for_status()
    except Exception as e:
        if METRICS_AVAILABLE:
            API_ERRORS.labels(source="tencent").inc()
        log.warning("腾讯行情获取失败: %s", e)
        raise

    results = []
    for line in r.text.strip().split("\n"):
        try:
            # v_sh600141="1~兴发集团~600141~37.60~..."
            if '"' not in line:
                continue
            body = line.split('"')[1]
            fields = body.split("~")
            if len(fields) < 38:
                continue
            code = fields[2]
            if code not in code_list:
                continue
            price = float(fields[3])
            close_y = float(fields[4])
            # 成交额：腾讯单位是万元，转元
            amount_raw = float(fields[37]) if fields[37] else 0
            results.append({
                "code": code,
                "name": fields[1],
                "open": float(fields[5]) if fields[5] else 0,
                "close_yesterday": close_y,
                "price": price,
                "high": float(fields[33]) if fields[33] else 0,
                "low": float(fields[34]) if fields[34] else 0,
                "volume": int(float(fields[6])) if fields[6] else 0,
                "amount": amount_raw * 10000,  # 万元→元
                "buy1": float(fields[9]) if fields[9] else 0,
                "sell1": float(fields[19]) if fields[19] else 0,
                "date": datetime.now().strftime("%Y-%m-%d"),
                "time": datetime.now().strftime("%H:%M:%S"),
            })
        except (ValueError, TypeError, IndexError):
            continue

    return results


def fetch_single(code: str) -> Optional[dict]:
    """获取单只股票实时行情"""
    results = fetch_realtime([code])
    return results[0] if results else None


def get_today_snapshot(code: str) -> Optional[dict]:
    """
    获取今日收盘快照（适合收盘后调用）
    如果还没收盘，返回当前行情并附备注
    """
    data = fetch_single(code)
    if not data:
        return None

    info = STOCK_MAP.get(code, {})
    close_y = data.get("close_yesterday", 0)
    price = data.get("price", 0)
    change_pct = round((price - close_y) / close_y * 100, 2) if close_y else 0

    return {
        "code": code,
        "name": info.get("name", code),
        "date": data.get("date") or date.today().isoformat(),
        "open": data.get("open"),
        "close": price,
        "high": data.get("high"),
        "low": data.get("low"),
        "volume": data.get("volume"),
        "amount": data.get("amount"),
        "change_pct": change_pct,
        "price": price,
    }


# ── 简单缓存（同分钟内不重复抓取） ────────────────────
_SNAPSHOT_CACHE: dict[str, tuple[list[dict], str]] = {}  # key → (data, timestamp_minute)


def _cache_key() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def get_all_today_snapshots(use_cache: bool = True) -> list[dict]:
    """获取所有候选标的今日快照

    Parameters
    ----------
    use_cache : bool
        是否使用分钟级缓存（默认 True，同分钟不重复抓取）
    """
    if use_cache:
        ck = _cache_key()
        cached = _SNAPSHOT_CACHE.get("snapshots")
        if cached and cached[1] == ck:
            if METRICS_AVAILABLE:
                CACHE_HITS.labels(cache="snapshot").inc()
            return cached[0]
        if METRICS_AVAILABLE:
            CACHE_MISSES.labels(cache="snapshot").inc()

    results = fetch_realtime()
    snapshots = []
    for data in results:
        code = data["code"]
        info = STOCK_MAP.get(code, {})
        close_y = data.get("close_yesterday", 0)
        price = data.get("price", 0)
        change_pct = round((price - close_y) / close_y * 100, 2) if close_y else 0
        snapshots.append({
            "code": code,
            "name": info.get("name", data.get("name", code)),
            "tier": info.get("tier", 3),
            "date": data.get("date") or date.today().isoformat(),
            "open": data.get("open"),
            "close": price,
            "high": data.get("high"),
            "low": data.get("low"),
            "volume": data.get("volume"),
            "amount": data.get("amount"),
            "change_pct": change_pct,
            "price": price,
            "buy1": data.get("buy1"),
            "sell1": data.get("sell1"),
        })
    if use_cache:
        _SNAPSHOT_CACHE["snapshots"] = (snapshots, ck)
    return snapshots


def invalidate_snapshot_cache():
    """清除快照缓存（强制下次抓取最新数据）"""
    _SNAPSHOT_CACHE.pop("snapshots", None)
