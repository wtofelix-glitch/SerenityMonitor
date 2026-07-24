"""
Phase B2 — 盘中实时影子链路运行器.

安全边界:
  · 仅 CONTINUOUS_AM (09:30-11:30) / CONTINUOUS_PM (13:00-14:57) 时段运行
  · 14:57 后自动停止 ACTION 生成（进入 CLOSING_AUCTION）
  · 15 分钟窗口，固定频率 5s，不能跨越时段边界
  · 影子模式：不推送、不成交、不写生产库
  · 自动停止条件：见 B2_AUTO_STOP_CHECKS

用法:
    python -m serenity_v2.phase_b2 --env shadow [--duration 900] [--interval 5]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time as _time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

CST = timezone(timedelta(hours=8))
logger = logging.getLogger("serenity_v2.phase_b2")

# ---------------------------------------------------------------------------
# B2 配置
# ---------------------------------------------------------------------------

B2_SYMBOLS = ["600487", "600176", "000988"]
B2_DEFAULT_DURATION = 900       # 15 分钟
B2_DEFAULT_INTERVAL = 5         # 5 秒
B2_MAX_CONSECUTIVE_FAILURES = 3
B2_MAX_DATA_AGE_MS = 300_000    # 连续竞价 5 分钟

# 允许运行的时段
B2_SAFE_SESSIONS = {"CONTINUOUS_AM", "CONTINUOUS_PM"}

# 禁止运行的时段
B2_FORBIDDEN_SESSIONS = {
    "OPENING_AUCTION_CANCELABLE", "OPENING_AUCTION_NO_CANCEL",
    "OPENING_MATCH_EVENT", "PRE_OPEN_PAUSE",
    "LUNCH_BREAK", "CLOSING_AUCTION",
    "POSTMARKET", "CLOSED",
}


# ---------------------------------------------------------------------------
# B2 指标
# ---------------------------------------------------------------------------

@dataclass
class CycleRecord:
    """P0-4: 每周期完整计时和结果数据。"""

    cycle_sequence: int = 0

    # ── 壁钟时间戳 (CST ISO) ──
    scheduled_at: str = ""
    cycle_started_at: str = ""
    request_started_at: str = ""
    response_received_at: str = ""
    normalization_completed_at: str = ""
    events_completed_at: str = ""
    signals_completed_at: str = ""
    cycle_completed_at: str = ""

    # ── 单调时钟 (time.monotonic, 用于精确间隔) ──
    scheduled_mono: float = 0.0
    cycle_started_mono: float = 0.0
    request_started_mono: float = 0.0
    response_received_mono: float = 0.0
    normalization_completed_mono: float = 0.0
    events_completed_mono: float = 0.0
    signals_completed_mono: float = 0.0
    cycle_completed_mono: float = 0.0

    # ── 计算间隔 (ms) ──
    schedule_delay_ms: float = 0.0
    http_duration_ms: float = 0.0
    normalization_duration_ms: float = 0.0
    event_duration_ms: float = 0.0
    signal_duration_ms: float = 0.0
    cycle_duration_ms: float = 0.0

    # ── 结果 ──
    status: str = "PENDING"  # COMPLETED / FAILED / SKIPPED / ABORTED
    primary_failure_type: str = ""
    secondary_failure_types: list = field(default_factory=list)

    # ── 计数 ──
    http_responses: int = 0
    raw_quote_records: int = 0
    normalized_accepted: int = 0
    normalized_rejected: int = 0
    quarantined: int = 0
    events_created: int = 0
    events_deduplicated: int = 0
    signals_created: int = 0
    signals_skipped: int = 0

    # ── 拒绝明细 ──
    rejections: list = field(default_factory=list)

    # ── 验证 ──
    def validate_timing_invariants(self) -> list[str]:
        violations = []
        if self.request_started_mono > 0 and self.cycle_started_mono > 0:
            if self.request_started_mono < self.cycle_started_mono:
                violations.append("cycle_started > request_started")
        if self.response_received_mono > 0 and self.request_started_mono > 0:
            if self.response_received_mono < self.request_started_mono:
                violations.append("request_started > response_received")
        if self.cycle_completed_mono > 0 and self.response_received_mono > 0:
            if self.cycle_completed_mono < self.response_received_mono:
                violations.append("response_received > cycle_completed")
        if self.cycle_duration_ms > 0 and self.http_duration_ms > 0:
            if self.cycle_duration_ms < self.http_duration_ms - 0.5:
                violations.append(
                    f"cycle_duration({self.cycle_duration_ms:.1f}ms) "
                    f"< http_duration({self.http_duration_ms:.1f}ms)"
                )
        return violations


@dataclass
class B2Metrics:
    """Phase B2 综合指标 (P0-4 扩展)。"""

    run_id: str = ""
    started_at: str = ""
    ended_at: str = ""
    duration_seconds: float = 0
    status: str = "PENDING"

    # ── 调度层 (P0-4 扩展) ──
    cycles_planned: int = 0
    cycles_started: int = 0
    cycles_completed: int = 0
    cycles_skipped: int = 0
    cycles_aborted: int = 0
    cycles_overlapped: int = 0
    cycles_failed: int = 0
    not_due_cycles: int = 0

    # ── HTTP + 周期计时 ──
    requests_total: int = 0
    requests_success: int = 0
    requests_failed: int = 0
    http_response_times_ms: list = field(default_factory=list)
    cycle_times_ms: list = field(default_factory=list)
    schedule_delay_ms_values: list = field(default_factory=list)
    normalization_duration_ms_values: list = field(default_factory=list)
    event_duration_ms_values: list = field(default_factory=list)
    signal_duration_ms_values: list = field(default_factory=list)
    raw_http_attempt_times_ms: list = field(default_factory=list)

    # ── 行情层 ──
    http_responses: int = 0
    raw_received: int = 0          # 保留兼容名 = raw_quote_records
    raw_quote_records: int = 0
    raw_stored: int = 0
    raw_duplicates: int = 0
    normalized_accepted: int = 0
    normalized_rejected: int = 0
    stale_count: int = 0
    future_timestamp_count: int = 0
    quarantined: int = 0
    quarantine_details: list = field(default_factory=list)
    data_age_ms_values: list = field(default_factory=list)

    # ── 事件层 (P0-4 扩展) ──
    events_created: int = 0
    events_deduplicated: int = 0
    events_quarantined: int = 0
    events_not_triggered: int = 0
    event_processing_failed: int = 0

    # ── 信号层 (P0-4 扩展) ──
    signals_total: int = 0
    signals_created_unique: int = 0
    signals_skipped_idempotent: int = 0
    signals_no_decision: int = 0
    candidate_ACTION: int = 0
    effective_ACTION: int = 0
    ACTION_downgraded: int = 0
    ACTION_rejected: int = 0
    DECISION_count: int = 0
    WATCH_count: int = 0
    INFO_count: int = 0

    # ── P0-4 失败类型拆分 ──
    scheduler_failed: int = 0
    session_check_failed: int = 0
    fetch_failed: int = 0
    http_failed: int = 0
    parse_failed: int = 0
    validation_failed: int = 0
    normalization_failed: int = 0
    quarantine_failed: int = 0
    event_failed: int = 0
    signal_failed: int = 0
    ledger_failed: int = 0
    report_failed: int = 0
    safety_guard_failed: int = 0
    total_failures: int = 0

    # ── P0-2 幂等统计 ──
    ledger_claimed: int = 0
    ledger_completed: int = 0
    ledger_failed_count: int = 0
    ledger_already_processed: int = 0
    ledger_in_progress: int = 0

    # ── 安全层 ──
    prod_file_hash_before: dict = field(default_factory=dict)
    prod_file_hash_after: dict = field(default_factory=dict)
    real_push_count: int = 0
    real_trade_count: int = 0
    account_modifications: int = 0
    non_whitelist_network: int = 0

    # ── 自动停止 ──
    auto_stop_triggered: bool = False
    auto_stop_reason: str = ""
    signal_generation_suspended: bool = False
    session_boundary_reached: bool = False
    session_at_boundary: str = ""

    # ── 每周期市场时段记录 ──
    cycle_sessions: list = field(default_factory=list)

    # ── 每周期明细 (P0-4) ──
    cycle_records: list = field(default_factory=list)

    # ── 明细 ──
    signal_details: list = field(default_factory=list)
    violations: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# 统计工具
# ---------------------------------------------------------------------------

def _p50(values: list) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    if n % 2 == 1:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2.0


def _p95(values: list) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = max(0, int(len(s) * 0.95) - 1)
    return s[idx]


# ---------------------------------------------------------------------------
# B2 运行器
# ---------------------------------------------------------------------------

class B2Runner:
    """Phase B2 实时影子链路运行器。"""

    def __init__(self, duration_seconds: int = B2_DEFAULT_DURATION,
                 interval_seconds: int = B2_DEFAULT_INTERVAL,
                 init_env: bool = True,
                 protected_prod_db: str = "",
                 manifest_path: str = ""):
        self.duration = duration_seconds
        self.interval = interval_seconds
        self.metrics = B2Metrics()
        self._protected_prod_db = protected_prod_db
        self._manifest_path = manifest_path

        if init_env:
            self._init_env()
        self._consecutive_failures = 0
        self._cycle_count = 0  # track across run()

    def _init_env(self):
        from .env import get_env, set_env, SerenityEnv
        from .prod_guard import ProductionGuard, ProdGuardConfig, load_manifest

        ROOT = Path(__file__).resolve().parent.parent
        self.shadow_dir = ROOT / "shadow_data" / "b2"
        self.shadow_dir.mkdir(parents=True, exist_ok=True)
        self.shadow_db = self.shadow_dir / "b2_shadow.db"

        # 可信配置清单（若指定或默认路径存在）
        self._manifest: dict | None = None
        if self._manifest_path:
            mp = Path(self._manifest_path)
        else:
            mp = ROOT / "docs" / "runtime-manifest.json"
        if mp.exists():
            self._manifest = load_manifest(mp)

        # 生产保护器（P0-1: 显式配置，不从 worktree 推导）
        if self._protected_prod_db:
            self.guard = ProductionGuard(ProdGuardConfig(
                protected_prod_db=self._protected_prod_db,
                shadow_db_dir=str(self.shadow_dir),
            ))
        else:
            self.guard = None

        try:
            get_env()
        except RuntimeError:
            set_env(SerenityEnv.shadow(
                db_path=self.shadow_db,
                log_dir=self.shadow_dir / "logs",
                protected_prod_db=self._protected_prod_db or None,
            ))

        from .migrations import apply_migrations
        apply_migrations(self.shadow_db)

        from .sina_market import _init_tables
        _init_tables(self.shadow_db)

        from .event_record import EventStore
        self.store = EventStore(db_path=self.shadow_db)
        self.store.init_schema()

        # P0-3: 从 Fixture JSON 加载账户上下文
        from .account_fixture import (
            load_and_set_fixture, to_account_state,
            FIXTURE_SIGNAL_TAGS,
        )
        from .account_baseline import get_baseline, reset_baseline
        reset_baseline()
        self.baseline = get_baseline()

        fixture_path = self.shadow_dir.parent / "b2" / "account_fixture.json"
        if not fixture_path.exists():
            fixture_path = ROOT / "shadow_data" / "b2" / "account_fixture.json"
        if not fixture_path.exists():
            fixture_path = ROOT / "tests" / "fixtures" / "b2" / "account_fixture_20260722.json"
        self._fixture = load_and_set_fixture(fixture_path)

        state = to_account_state(self._fixture)
        state.snapshot_at = ""
        self.baseline.save_snapshot(state)

        # P0-3: account_snapshot_id 使用完整 SHA-256 (64 hex, 用于幂等键)
        # 短 ID 用于显示 (16 hex)
        self._account_snapshot_id = self._fixture.snapshot_id_full
        self._account_snapshot_id_short = self._fixture.snapshot_id

        from .intelligence_network import get_intel, reset_intel
        reset_intel()
        self.intel = get_intel(shadow_mode=True)

        from .signal_desk import get_desk, reset_desk
        reset_desk()
        self.desk = get_desk()

        # P0-2: 信号幂等 — 事件处理账本 + 幂等处理器
        from .signal_idempotency import (
            EventProcessingLedger, IdempotentSignalProcessor,
        )
        self.ledger = EventProcessingLedger(self.shadow_db)
        self.ledger.init_schema()
        self.idempotent = IdempotentSignalProcessor(
            desk=self.desk, ledger=self.ledger,
            worker_id=self.metrics.run_id,
        )

        # P0-2: 稳定上下文键 — 用于信号幂等
        # strategy_version: 从 desk/verifier 配置派生
        self._strategy_version = "b2-1.0"
        # strategy_config_hash: B2 固定配置的 SHA256
        self._strategy_config_hash = hashlib.sha256(
            json.dumps({
                "symbols": B2_SYMBOLS,
                "duration": self.duration,
                "interval": self.interval,
                "hard_gate_version": getattr(
                    getattr(self.desk, 'verifier', None),
                    'version', 'unknown'),
            }, sort_keys=True).encode()
        ).hexdigest()[:16]
        from .sina_market import SinaQuoteFetcher
        self.fetcher = SinaQuoteFetcher()

        self.metrics.run_id = datetime.now(tz=CST).strftime("B2_%Y%m%d_%H%M%S")

    def verify_environment(self) -> tuple:
        """显式验证运行环境并输出确认信息。

        返回 (ok, details_dict, violations)。任一检查失败 → ok=False。
        """
        from .env import get_env
        from .clock import get_clock, RealClock, SimClock
        violations: list[str] = []

        try:
            env = get_env()
        except RuntimeError:
            return False, {}, ["环境未初始化，请先调用 serenity_v2.env.set_env()"]

        clock = get_clock()
        clock_mode = "REAL" if isinstance(clock, RealClock) else (
            "SIM" if isinstance(clock, SimClock) else "UNKNOWN"
        )

        details = {
            "clock_mode": clock_mode,
            "timezone": "Asia/Shanghai",
            "environment": env.mode,
            "db": str(env.db_path.resolve()),
            "shadow_db_realpath": str(self.shadow_db.resolve()),
            "push_adapter": "disabled" if env.push_adapter is None else "PRESENT ⚠",
            "trade_adapter": "disabled" if env.broker_adapter is None else "PRESENT ⚠",
            "account_state_mode": "FIXTURE",
            "account_state_stale": "true",
            "account_snapshot_id": self._account_snapshot_id,
            "account_snapshot_as_of": (
                self._fixture.snapshot_as_of if hasattr(self, '_fixture') else "N/A"
            ),
            "fixture_file_hash": (
                self._fixture.file_hash[:16] + "..." if hasattr(self, '_fixture') else "N/A"
            ),
        }

        # 0. 时钟必须为真实时钟
        if clock_mode != "REAL":
            violations.append(f"时钟模式为 {clock_mode}，盘中运行必须使用 RealClock")

        # P0-1: 生产路径保护（显式配置，不从 worktree 推导）
        if self.guard is not None:
            ok_guard, guard_details, guard_violations = self.guard.preflight(
                manifest=self._manifest,
            )
            details.update(guard_details)
            if not ok_guard:
                violations.extend(guard_violations)
                return False, details, violations
        else:
            violations.append(
                "P0-1: 未配置 --protected-prod-db，"
                "拒绝从 worktree 推导生产路径"
            )
            return False, details, violations

    def _print_env_confirmation(self):
        """输出环境确认信息块。任一检查失败则抛出 RuntimeError。"""
        ok, details, violations = self.verify_environment()

        print(f"\n{'='*60}")
        print(f"环境确认")
        print(f"{'='*60}")
        for k, v in details.items():
            # 跳过嵌套快照字典（单独展示）
            if k in ("before_snapshot",):
                continue
            icon = "✅" if "⚠" not in str(v) else "⚠️"
            print(f"  {icon} {k}: {v}")
        if ok:
            prod = self.guard.before.main if self.guard and self.guard.before else None
            print(f"  ✅ protected_prod_db: {details.get('protected_prod_db', 'N/A')}")
            if prod and prod.state == "PRESENT":
                print(f"  ✅ prod_db_sha256: {prod.sha256}")
                print(f"  ✅ prod_db_inode: {prod.inode} dev={prod.device}")
        print(f"{'='*60}")

        if violations:
            print(f"\n❌ 环境验证失败:")
            for v in violations:
                print(f"  ❌ {v}")
            raise RuntimeError(
                "B2 环境验证失败: " + "; ".join(violations)
            )

        print(f"✅ 环境验证通过\n")

    # _compute_prod_hashes 已由 ProductionGuard.preflight()/postflight() 替代 (P0-1)

    def check_session(self) -> tuple:
        """检查当前时段是否允许运行（使用当前时钟，不重置）。

        返回 (ok, session_name, reason)。
        """
        from .clock import get_clock
        clock = get_clock()
        session = clock.market_session()
        if session in B2_FORBIDDEN_SESSIONS:
            return False, session, f"当前时段 {session} 禁止运行 B2"
        if session not in B2_SAFE_SESSIONS:
            return False, session, f"当前时段 {session} 不在允许列表"
        return True, session, ""

    def _check_session_remaining(self) -> tuple:
        """检查当前连续竞价时段剩余时间是否足够运行。

        返回 (ok, remaining_seconds, reason)。
        """
        from .clock import get_clock
        clock = get_clock()
        now = clock.now()

        # 时段结束时间（CST）
        session_end_times = {
            "CONTINUOUS_AM": now.replace(hour=11, minute=30, second=0, microsecond=0),
            "CONTINUOUS_PM": now.replace(hour=14, minute=57, second=0, microsecond=0),
        }

        session = clock.market_session()
        if session not in session_end_times:
            return False, 0, f"当前时段 {session} 不支持剩余时间计算"

        end = session_end_times[session]
        remaining = (end - now).total_seconds()

        if remaining < self.duration:
            return False, remaining, (
                f"连续竞价剩余 {remaining:.0f}s 不足 {self.duration}s 窗口，"
                f"时段 {session} 结束于 {end.strftime('%H:%M')}"
            )

        return True, remaining, ""

    def _auto_stop(self, reason: str):
        if not self.metrics.auto_stop_triggered:
            self.metrics.auto_stop_triggered = True
            self.metrics.auto_stop_reason = reason
            self.metrics.signal_generation_suspended = True
            logger.warning(f"B2 自动停止: {reason}")
            print(f"\n🚨 自动停止: {reason}")

    def _record_signal(self, sig) -> None:
        self.metrics.signals_total += 1
        self.metrics.signals_created_unique += 1
        cand = sig.candidate_signal_level
        eff = sig.effective_signal_level

        # 注入 FIXTURE 账户上下文标签（B2 影子链路永远不应用于实盘）
        tags = sig.execution_tags or []
        for tag in ("ACCOUNT_CONTEXT_FIXTURE", "ACCOUNT_CONTEXT_STALE", "NOT_FOR_EXECUTION"):
            if tag not in tags:
                tags.append(tag)
        sig.execution_tags = tags

        if cand == "ACTION":
            self.metrics.candidate_ACTION += 1
        if eff == "ACTION":
            self.metrics.effective_ACTION += 1
        if cand == "ACTION" and eff != "ACTION":
            self.metrics.ACTION_downgraded += 1
        prim = sig.primary_normalization_reason or ""
        if prim.startswith("GATE_DOWNGRADE"):
            self.metrics.ACTION_rejected += 1

        if eff == "DECISION":
            self.metrics.DECISION_count += 1
        elif eff == "WATCH":
            self.metrics.WATCH_count += 1
        elif eff == "INFO":
            self.metrics.INFO_count += 1
        elif eff in ("HOLD", "NONE", ""):
            self.metrics.signals_no_decision += 1

        self.metrics.signal_details.append({
            "signal_id": sig.signal_id,
            "symbol": sig.symbol,
            "candidate_level": cand,
            "candidate_action": sig.candidate_trade_action,
            "effective_level": eff,
            "effective_action": sig.effective_trade_action,
            "primary_norm": sig.primary_normalization_reason,
            "secondary_norms": sig.secondary_normalization_reasons,
            "confidence": sig.confidence,
            "market_session": sig.market_session,
            "event_id": sig.event_id,
            "action_suppressed": sig.action_suppressed,
            "execution_tags": sig.execution_tags,
        })

        required_tags = {"SHADOW_ONLY", "NOT_FOR_EXECUTION",
                         "ACCOUNT_CONTEXT_FIXTURE", "ACCOUNT_CONTEXT_STALE"}
        missing = required_tags - set(tags)
        if missing:
            self.metrics.violations.append(
                f"缺少安全标签: {sig.signal_id} missing={missing} tags={tags}"
            )

    def run(self):
        from .clock import get_clock, reset_clock
        from .sina_market import (
            store_raw, store_normalized, store_quarantine,
            normalized_quote_to_event,
        )

        # ── 环境验证 ──
        self._print_env_confirmation()

        ok, session, why = self.check_session()
        if not ok:
            self.metrics.status = "ERROR"
            self.metrics.violations.append(f"时段不允许: {why}")
            logger.error(f"B2 不允许运行: {why}")
            print(f"❌ 时段检查失败: {why}")
            return self.metrics

        # ── 剩余时间检查 ──
        rem_ok, remaining, rem_why = self._check_session_remaining()
        if not rem_ok:
            self.metrics.status = "ERROR"
            self.metrics.violations.append(f"窗口不足: {rem_why}")
            logger.error(f"B2 窗口不足: {rem_why}")
            print(f"❌ 窗口不足: {rem_why}")
            return self.metrics

        self.metrics.status = "RUNNING"
        self.metrics.started_at = datetime.now(tz=CST).isoformat(timespec="seconds")

        reset_clock()
        clock = get_clock()
        symbols = B2_SYMBOLS
        last_session = session

        start_mono = _time.monotonic()
        next_cycle_mono = start_mono
        cycle = 0
        self.metrics.cycles_planned = max(1, self.duration // self.interval)

        print(f"\n{'='*70}")
        print(f"Phase B2 实时影子链路")
        print(f"{'='*70}")
        print(f"  run_id:         {self.metrics.run_id}")
        print(f"  session:        {session}")
        print(f"  session_remains:{remaining:.0f}s")
        print(f"  duration:       {self.duration}s ({self.duration//60}min)")
        print(f"  interval:       {self.interval}s")
        print(f"  symbols:        {symbols}")
        print(f"  shadow DB:      {self.shadow_db}")
        print(f"  safe windows:   CONTINUOUS_AM 09:30-11:30 / "
              f"CONTINUOUS_PM 13:00-14:57")
        print(f"  account:        FIXTURE (快照 as_of 7月22日, stale=true)")
        print(f"  snapshot_id:    {self._account_snapshot_id}")
        print(f"  fixture:        {getattr(self._fixture, 'fixture_id', 'N/A')}")
        print(f"{'='*70}\n")

        try:
            while True:
                now_mono = _time.monotonic()
                elapsed_total = now_mono - start_mono
                if elapsed_total >= self.duration:
                    break

                # 每周期重新检查时段（可能跨越边界）
                reset_clock()
                current_session = get_clock().market_session()
                self.metrics.cycle_sessions.append(current_session)

                # 时段边界检测
                if current_session != last_session:
                    self.metrics.session_boundary_reached = True
                    self.metrics.session_at_boundary = (
                        f"{last_session} → {current_session}"
                    )
                    logger.warning(
                        f"时段边界: {last_session} → {current_session}"
                    )

                if current_session not in B2_SAFE_SESSIONS:
                    reason = (
                        f"SESSION_BOUNDARY_REACHED: "
                        f"{last_session} → {current_session}"
                        if current_session != last_session
                        else f"session_exit:{current_session}"
                    )
                    self._auto_stop(reason)
                    print(f"  ⛔ 时段边界 {last_session} → {current_session}，"
                          f"停止信号生成")
                    break

                last_session = current_session

                # 固定频率等待
                wait = next_cycle_mono - now_mono
                if wait > 0:
                    _time.sleep(wait)
                elif wait < -self.interval:
                    self.metrics.cycles_skipped += 1
                    next_cycle_mono = now_mono

                cycle += 1
                self._cycle_count = cycle
                scheduled_mono = next_cycle_mono
                self.metrics.cycles_started += 1

                # ── P0-4: 每周期计时 ──
                cycle_started_mono = _time.monotonic()
                cycle_started_dt = datetime.now(tz=CST)
                schedule_delay_ms = (cycle_started_mono - scheduled_mono) * 1000.0
                self.metrics.schedule_delay_ms_values.append(max(0, schedule_delay_ms))

                cr = CycleRecord(cycle_sequence=cycle)
                cr.scheduled_mono = scheduled_mono
                # scheduled_at 由 start_mono + (cycle-1)*interval 推导
                from datetime import timedelta
                run_start_dt = datetime.fromisoformat(self.metrics.started_at)
                cr.scheduled_at = (run_start_dt + timedelta(seconds=(cycle - 1) * self.interval)).isoformat(timespec="milliseconds")
                cr.cycle_started_mono = cycle_started_mono
                cr.cycle_started_at = cycle_started_dt.isoformat(timespec="milliseconds")
                cr.schedule_delay_ms = max(0, schedule_delay_ms)

                request_start_mono = _time.monotonic()
                request_start_dt = datetime.now(tz=CST)
                cr.request_started_mono = request_start_mono
                cr.request_started_at = request_start_dt.isoformat(timespec="milliseconds")

                bt = clock.now().isoformat(timespec="seconds")
                print(f"\n[{cycle:03d}] {request_start_dt.strftime('%H:%M:%S')} "
                      f"抓取...", end=" ", flush=True)

                try:
                    # ── Step 1: 抓取 ──
                    raw_records = self.fetcher.fetch(symbols)
                    request_end_mono = _time.monotonic()
                    http_time_ms = (request_end_mono - request_start_mono) * 1000.0

                    cr.response_received_mono = request_end_mono
                    cr.response_received_at = datetime.now(tz=CST).isoformat(timespec="milliseconds")
                    cr.http_duration_ms = http_time_ms

                    self.metrics.requests_total += 1
                    self.metrics.http_responses += 1
                    self.metrics.http_response_times_ms.append(http_time_ms)
                    self.metrics.raw_http_attempt_times_ms.append(http_time_ms)

                    if not raw_records:
                        self._consecutive_failures += 1
                        self.metrics.requests_failed += 1
                        self.metrics.fetch_failed += 1
                        cr.status = "FAILED"
                        cr.primary_failure_type = "fetch_failed"
                        cr.cycle_completed_mono = _time.monotonic()
                        cr.cycle_completed_at = datetime.now(tz=CST).isoformat(timespec="milliseconds")
                        cr.cycle_duration_ms = (cr.cycle_completed_mono - cycle_started_mono) * 1000.0
                        self.metrics.cycle_records.append(cr)
                        print(f"❌ 无数据")
                        if self._consecutive_failures >= B2_MAX_CONSECUTIVE_FAILURES:
                            self._auto_stop(f"连续 {B2_MAX_CONSECUTIVE_FAILURES} 次无数据")
                            break
                        next_cycle_mono = scheduled_mono + self.interval
                        continue

                    self._consecutive_failures = 0
                    self.metrics.requests_success += 1
                    self.metrics.raw_received += len(raw_records)

                    # ── Step 2: 存储原始 ──
                    raw_stored = store_raw(self.shadow_db, raw_records)
                    raw_dup = len(raw_records) - raw_stored
                    self.metrics.raw_stored += raw_stored
                    self.metrics.raw_duplicates += raw_dup

                    # ── Step 3: 标准化 ──
                    norms = []
                    for r in raw_records:
                        nq = self.fetcher.normalize(r, business_time=bt)
                        if nq is not None:
                            norms.append(nq)
                            if nq.data_age_ms > 0:
                                self.metrics.data_age_ms_values.append(nq.data_age_ms)

                    ns = store_normalized(self.shadow_db, norms)
                    cr.raw_quote_records = len(raw_records)
                    cr.normalized_accepted = sum(1 for nq in norms if nq.validation_status == "valid")
                    cr.normalized_rejected = sum(1 for nq in norms if nq.validation_status != "valid")

                    for nq in norms:
                        if nq.validation_status == "valid":
                            self.metrics.normalized_accepted += 1
                        else:
                            self.metrics.normalized_rejected += 1
                            cr.rejections.append({
                                "symbol": nq.symbol,
                                "source_timestamp": getattr(nq, 'source_timestamp', ''),
                                "collected_at": cr.cycle_started_at,
                                "validation_reason": "; ".join(nq.validation_errors),
                                "raw_payload_sha256": hashlib.sha256(
                                    str(getattr(nq, 'raw_payload', '')).encode()
                                ).hexdigest()[:16] if hasattr(nq, 'raw_payload') else "",
                                "cycle_sequence": cycle,
                            })
                        if nq.stale_for_trading:
                            self.metrics.stale_count += 1
                        if "future_timestamp" in nq.validation_errors:
                            self.metrics.future_timestamp_count += 1

                    # P0-4: 标准化完成时间
                    cr.normalization_completed_mono = _time.monotonic()
                    cr.normalization_completed_at = datetime.now(tz=CST).isoformat(timespec="milliseconds")
                    cr.normalization_duration_ms = (
                        cr.normalization_completed_mono - cr.response_received_mono) * 1000.0
                    self.metrics.normalization_duration_ms_values.append(cr.normalization_duration_ms)

                    rejected = len(norms) - ns

                    # ── Step 4: 桥接 + 隔离 + 情报网 ──
                    events_this_cycle = 0
                    quarantined_this_cycle = 0

                    for nq in norms:
                        event, quarantined, qreason = normalized_quote_to_event(nq)

                        if quarantined:
                            self.store.quarantine_event(
                                event, qreason,
                                validation_errors=nq.validation_errors,
                                normalized_at=nq.normalized_at,
                            )
                            store_quarantine(self.shadow_db, nq, qreason)
                            self.metrics.quarantined += 1
                            self.metrics.events_quarantined += 1
                            self.metrics.quarantine_details.append({
                                "symbol": nq.symbol,
                                "reason": qreason,
                                "errors": nq.validation_errors,
                            })
                            quarantined_this_cycle += 1
                        else:
                            ingested = self.intel.ingest(event)
                            if ingested.event_id:
                                events_this_cycle += 1

                    dedup_this = max(0, len(norms) - quarantined_this_cycle - events_this_cycle)
                    self.metrics.events_created += events_this_cycle
                    self.metrics.events_deduplicated += dedup_this
                    self.metrics.events_not_triggered += max(0, len(norms) - quarantined_this_cycle - events_this_cycle - dedup_this)

                    cr.events_created = events_this_cycle
                    cr.events_deduplicated = dedup_this
                    cr.quarantined = quarantined_this_cycle

                    # P0-4: 事件处理完成时间
                    cr.events_completed_mono = _time.monotonic()
                    cr.events_completed_at = datetime.now(tz=CST).isoformat(timespec="milliseconds")
                    cr.event_duration_ms = (
                        cr.events_completed_mono - cr.normalization_completed_mono) * 1000.0
                    self.metrics.event_duration_ms_values.append(cr.event_duration_ms)

                    # ── Step 5: 信号台 (P0-2 幂等) ──
                    signals = self.idempotent.process_events(
                        strategy_version=self._strategy_version,
                        strategy_config_hash=self._strategy_config_hash,
                        account_snapshot_id=self._account_snapshot_id,
                        market_data=None,
                    )

                    for sig in signals:
                        self._record_signal(sig)

                    # P0-4: 信号处理完成时间
                    cr.signals_completed_mono = _time.monotonic()
                    cr.signals_completed_at = datetime.now(tz=CST).isoformat(timespec="milliseconds")
                    cr.signal_duration_ms = (
                        cr.signals_completed_mono - cr.events_completed_mono) * 1000.0
                    self.metrics.signal_duration_ms_values.append(cr.signal_duration_ms)
                    cr.signals_created = len(signals)
                    cr.signals_skipped = self.idempotent.stats.get("already_processed", 0)
                    cr.http_responses = 1

                    # ── Step 6: 周期计时 ──
                    cycle_end_mono = _time.monotonic()
                    cycle_time_ms = (cycle_end_mono - request_start_mono) * 1000.0
                    self.metrics.cycle_times_ms.append(cycle_time_ms)

                    # P0-4: 周期完成
                    cr.cycle_completed_mono = cycle_end_mono
                    cr.cycle_completed_at = datetime.now(tz=CST).isoformat(timespec="milliseconds")
                    cr.cycle_duration_ms = (cycle_end_mono - cycle_started_mono) * 1000.0
                    cr.status = "COMPLETED"
                    self.metrics.cycle_records.append(cr)
                    self.metrics.cycles_completed += 1

                    # 自停: 行情年龄超标
                    for nq in norms:
                        if nq.data_age_ms > B2_MAX_DATA_AGE_MS:
                            if nq.validation_status == "valid" and not nq.stale_for_trading:
                                self._auto_stop(
                                    f"行情年龄超标: {nq.symbol} {nq.data_age_ms:.0f}ms"
                                )
                                break

                    # 自停: effective=false → ACTION
                    for sig in signals:
                        for nq in norms:
                            if (nq.symbol == sig.symbol
                                    and not nq.effective_action_eligible
                                    and sig.signal_level == "ACTION"):
                                self._auto_stop(
                                    f"effective_action_eligible=false → ACTION: {sig.symbol}"
                                )
                                break

                    # ── 输出 ──
                    flag_parts = []
                    if events_this_cycle:
                        flag_parts.append(f"events={events_this_cycle}")
                    if quarantined_this_cycle:
                        flag_parts.append(f"quarantine={quarantined_this_cycle}")
                    if signals:
                        flag_parts.append(f"sigs={len(signals)}")
                    flag_str = f" [{', '.join(flag_parts)}]" if flag_parts else ""

                    print(f"✅ raw={len(raw_records)}({raw_dup}dup) "
                          f"norm={ns}({rejected}rej) "
                          f"http={http_time_ms:.0f}ms cyc={cycle_time_ms:.0f}ms"
                          f"{flag_str}")

                    for nq in norms:
                        if nq.validation_status != "valid":
                            print(f"  ⚠ {nq.symbol}: {nq.validation_status} — "
                                  f"{'; '.join(nq.validation_errors)}")
                        elif nq.acceptable_as_postmarket_snapshot:
                            print(f"  ℹ {nq.symbol}: valid(pm_snapshot)")

                    for sig in signals:
                        print(f"  📡 {sig.symbol}: {sig.signal_level} · {sig.trade_action} "
                              f"[{sig.confidence}] "
                              f"cand={sig.candidate_signal_level}·{sig.candidate_trade_action}"
                              + (f" norm={sig.primary_normalization_reason}"
                                 if sig.primary_normalization_reason else ""))

                except Exception as e:
                    self.metrics.cycles_failed += 1
                    self.metrics.cycles_aborted += 1
                    self._consecutive_failures += 1
                    # P0-4: 失败类型分类
                    err_msg = str(e).lower()
                    if "parse" in err_msg or "json" in err_msg or "decode" in err_msg:
                        self.metrics.parse_failed += 1
                        cr.primary_failure_type = "parse_failed"
                    elif "fetch" in err_msg or "http" in err_msg or "timeout" in err_msg:
                        self.metrics.http_failed += 1
                        cr.primary_failure_type = "http_failed"
                    elif "validate" in err_msg or "valid" in err_msg:
                        self.metrics.validation_failed += 1
                        cr.primary_failure_type = "validation_failed"
                    elif "quarantine" in err_msg:
                        self.metrics.quarantine_failed += 1
                        cr.primary_failure_type = "quarantine_failed"
                    elif "event" in err_msg:
                        self.metrics.event_failed += 1
                        cr.primary_failure_type = "event_failed"
                    elif "signal" in err_msg or "ledger" in err_msg:
                        self.metrics.signal_failed += 1
                        cr.primary_failure_type = "signal_failed"
                    else:
                        self.metrics.signal_failed += 1
                        cr.primary_failure_type = "signal_failed"
                    cr.status = "FAILED"
                    cr.cycle_completed_mono = _time.monotonic()
                    cr.cycle_completed_at = datetime.now(tz=CST).isoformat(timespec="milliseconds")
                    cr.cycle_duration_ms = (cr.cycle_completed_mono - cycle_started_mono) * 1000.0
                    self.metrics.cycle_records.append(cr)
                    logger.error(f"周期 {cycle} 异常: {e}", exc_info=True)
                    print(f"❌ 异常: {e}")
                    if self._consecutive_failures >= B2_MAX_CONSECUTIVE_FAILURES:
                        self._auto_stop(f"连续 {B2_MAX_CONSECUTIVE_FAILURES} 次周期失败")
                        break

                # 周期重叠检测
                if cycle_time_ms / 1000.0 > self.interval:
                    self.metrics.cycles_overlapped += 1
                    self._auto_stop(
                        f"周期重叠: {cycle_time_ms:.0f}ms > 间隔 {self.interval}s"
                    )
                    break

                if self.metrics.auto_stop_triggered:
                    break

                next_cycle_mono = scheduled_mono + self.interval

        except KeyboardInterrupt:
            print("\n\n⚠ B2 被用户中断")
            self.metrics.status = "AUTO_STOPPED"
            self.metrics.auto_stop_reason = "user_interrupt"

        # 收尾
        # 不覆盖 cycles_completed — 已在循环中逐周期计入
        self.metrics.cycles_planned = max(1, self.duration // self.interval)
        self.metrics.ended_at = datetime.now(tz=CST).isoformat(timespec="seconds")
        self.metrics.duration_seconds = _time.monotonic() - start_mono
        if self.metrics.status == "RUNNING":
            self.metrics.status = "COMPLETED"

        # P0-4: 计算总失败数 = 所有互斥失败类型之和
        self.metrics.total_failures = (
            self.metrics.scheduler_failed + self.metrics.session_check_failed
            + self.metrics.fetch_failed + self.metrics.http_failed
            + self.metrics.parse_failed + self.metrics.validation_failed
            + self.metrics.normalization_failed + self.metrics.quarantine_failed
            + self.metrics.event_failed + self.metrics.signal_failed
            + self.metrics.ledger_failed + self.metrics.report_failed
            + self.metrics.safety_guard_failed
        )

        # P0-2: 收集幂等账本统计
        try:
            ledger_stats = self.ledger.get_stats()
            self.metrics.ledger_claimed = ledger_stats.get("total", 0)
            self.metrics.ledger_completed = ledger_stats.get("COMPLETED", 0)
            self.metrics.ledger_failed_count = ledger_stats.get("FAILED", 0)
            self.metrics.ledger_in_progress = ledger_stats.get("PROCESSING", 0)
            self.metrics.ledger_already_processed = (
                self.idempotent.stats.get("already_processed", 0))
        except Exception:
            self.metrics.report_failed += 1

        # P0-1: 运行后生产文件检查
        if self.guard is not None:
            try:
                ok_post, changes, post_violations = self.guard.postflight(self.shadow_db)
                if changes:
                    logger.warning(f"生产文件变化: {changes}")
                    for ch in changes:
                        self.metrics.violations.append(f"生产文件: {ch}")
                if post_violations:
                    for v in post_violations:
                        self.metrics.violations.append(v)
                        self._auto_stop(v)
                if not ok_post:
                    if self.metrics.status == "COMPLETED":
                        self.metrics.status = "AUTO_STOPPED"
            except Exception:
                self.metrics.safety_guard_failed += 1

        self._print_report()
        return self.metrics

    def _print_report(self):
        m = self.metrics
        http_t = m.http_response_times_ms
        cyc_t = m.cycle_times_ms
        ages = m.data_age_ms_values
        sched_d = m.schedule_delay_ms_values
        norm_d = m.normalization_duration_ms_values
        evt_d = m.event_duration_ms_values
        sig_d = m.signal_duration_ms_values

        print(f"\n{'='*70}")
        print(f"Phase B2 运行报告 (P0-4)")
        print(f"{'='*70}")
        print(f"  run_id:     {m.run_id}")
        print(f"  status:     {m.status}")
        print(f"  started:    {m.started_at}")
        print(f"  ended:      {m.ended_at}")
        print(f"  duration:   {m.duration_seconds:.1f}s")
        if m.auto_stop_triggered:
            print(f"  🚨 自动停止: {m.auto_stop_reason}")

        # ── P0-4 调度层 ──
        print(f"\n── 调度层 (P0-4) ──")
        print(f"  planned={m.cycles_planned}  started={m.cycles_started}  "
              f"completed={m.cycles_completed}  skipped={m.cycles_skipped}  "
              f"aborted={m.cycles_aborted}  failed={m.cycles_failed}  "
              f"overlapped={m.cycles_overlapped}")
        sched_audit_1 = m.cycles_planned == m.cycles_started + m.cycles_skipped + m.not_due_cycles
        sched_audit_2 = m.cycles_started == m.cycles_completed + m.cycles_aborted
        print(f"  审计 planned=started+skipped+not_due: "
              f"{m.cycles_planned}={m.cycles_started}+{m.cycles_skipped}+{m.not_due_cycles} "
              f"→ {'✅' if sched_audit_1 else '❌'}")
        print(f"  审计 started=completed+aborted: "
              f"{m.cycles_started}={m.cycles_completed}+{m.cycles_aborted} "
              f"→ {'✅' if sched_audit_2 else '❌'}")

        # ── P0-4 周期计时 ──
        print(f"\n── 周期计时 (P0-4) ──")
        print(f"  HTTP responses: {m.http_responses}  "
              f"请求成功: {m.requests_success} / 失败: {m.requests_failed}")
        if http_t:
            print(f"  HTTP attempt  P50/P95/MAX: {_p50(http_t):.0f}/{_p95(http_t):.0f}/{max(http_t):.0f}ms  (n={len(http_t)})")
        if sched_d:
            print(f"  schedule_delay P50/P95/MAX: {_p50(sched_d):.1f}/{_p95(sched_d):.1f}/{max(sched_d):.1f}ms")
        if norm_d:
            print(f"  normalization P50/P95/MAX: {_p50(norm_d):.1f}/{_p95(norm_d):.1f}/{max(norm_d):.1f}ms")
        if evt_d:
            print(f"  event_proc    P50/P95/MAX: {_p50(evt_d):.1f}/{_p95(evt_d):.1f}/{max(evt_d):.1f}ms")
        if sig_d:
            print(f"  signal_proc   P50/P95/MAX: {_p50(sig_d):.1f}/{_p95(sig_d):.1f}/{max(sig_d):.1f}ms")
        if cyc_t:
            print(f"  full_cycle    P50/P95/MAX: {_p50(cyc_t):.0f}/{_p95(cyc_t):.0f}/{max(cyc_t):.0f}ms  (n={len(cyc_t)})")
        if cyc_t and http_t:
            for i, (c, h) in enumerate(zip(cyc_t, http_t)):
                if c < h - 0.5:
                    print(f"  ❌ cycle[{i}]: cycle({c:.0f}ms) < http({h:.0f}ms) — 违反 cycle_duration >= http_duration")
                    break
            else:
                print(f"  ✅ 所有周期 cycle_duration >= http_duration")

        # ── 行情层 ──
        print(f"\n── 行情层 ──")
        print(f"  raw_received: {m.raw_received}  stored: {m.raw_stored}  "
              f"dup: {m.raw_duplicates}")
        print(f"  norm_accepted: {m.normalized_accepted}  "
              f"rejected: {m.normalized_rejected}  quarantined: {m.quarantined}")
        data_audit = (m.raw_received == m.normalized_accepted + m.normalized_rejected + m.quarantined)
        print(f"  审计 raw = accepted + rejected + quarantined: "
              f"{m.raw_received}={m.normalized_accepted}+{m.normalized_rejected}+{m.quarantined} "
              f"→ {'✅' if data_audit else '❌'}")
        print(f"  stale: {m.stale_count}  future_ts: {m.future_timestamp_count}")
        if ages:
            print(f"  DataAge P50/P95/MAX: {_p50(ages):.0f}/{_p95(ages):.0f}/{max(ages):.0f}ms  (n={len(ages)})")

        # ── 事件层 (P0-4) ──
        print(f"\n── 事件层 (P0-4) ──")
        print(f"  created: {m.events_created}  deduplicated: {m.events_deduplicated}  "
              f"quarantined: {m.events_quarantined}  not_triggered: {m.events_not_triggered}  "
              f"proc_failed: {m.event_processing_failed}")
        event_audit = (m.normalized_accepted == m.events_created + m.events_deduplicated
                       + m.events_not_triggered + m.event_processing_failed)
        print(f"  审计 accepted = created+dedup+not_triggered+proc_failed: "
              f"{m.normalized_accepted}={m.events_created}+{m.events_deduplicated}"
              f"+{m.events_not_triggered}+{m.event_processing_failed} "
              f"→ {'✅' if event_audit else '❌'}")

        # ── 信号层 (P0-4) ──
        print(f"\n── 信号层 (P0-4) ──")
        print(f"  total: {m.signals_total}  unique: {m.signals_created_unique}  "
              f"skipped(idempotent): {m.signals_skipped_idempotent}  "
              f"no_decision: {m.signals_no_decision}")
        print(f"  cand_ACTION: {m.candidate_ACTION}  eff_ACTION: {m.effective_ACTION}  "
              f"downgraded: {m.ACTION_downgraded}  rejected: {m.ACTION_rejected}")
        print(f"  DECISION: {m.DECISION_count}  WATCH: {m.WATCH_count}  INFO: {m.INFO_count}")
        action_audit = (m.candidate_ACTION == m.effective_ACTION + m.ACTION_downgraded + m.ACTION_rejected)
        print(f"  审计 candidate = effective + downgraded + rejected: "
              f"{m.candidate_ACTION}={m.effective_ACTION}+{m.ACTION_downgraded}+{m.ACTION_rejected} "
              f"→ {'✅' if action_audit else '❌'}")

        # ── P0-2 幂等 ──
        print(f"\n── P0-2 幂等 ──")
        print(f"  claimed: {m.ledger_claimed}  completed: {m.ledger_completed}  "
              f"failed: {m.ledger_failed_count}  in_progress: {m.ledger_in_progress}")
        print(f"  already_processed (skipped): {m.ledger_already_processed}")
        ledger_audit = (m.ledger_claimed == m.ledger_completed + m.ledger_failed_count + m.ledger_in_progress)
        print(f"  审计 claimed = completed + failed + in_progress: "
              f"{'✅' if ledger_audit else '❌'}")

        # ── P0-4 失败分类 ──
        print(f"\n── P0-4 失败分类 ──")
        failures = [
            ("scheduler_failed", m.scheduler_failed),
            ("session_check_failed", m.session_check_failed),
            ("fetch_failed", m.fetch_failed),
            ("http_failed", m.http_failed),
            ("parse_failed", m.parse_failed),
            ("validation_failed", m.validation_failed),
            ("normalization_failed", m.normalization_failed),
            ("quarantine_failed", m.quarantine_failed),
            ("event_failed", m.event_failed),
            ("signal_failed", m.signal_failed),
            ("ledger_failed", m.ledger_failed),
            ("report_failed", m.report_failed),
            ("safety_guard_failed", m.safety_guard_failed),
        ]
        nonzero = [(n, c) for n, c in failures if c > 0]
        if nonzero:
            for name, count in nonzero:
                print(f"  {name}: {count}")
        else:
            print(f"  (无失败)")
        fail_sum = sum(c for _, c in failures)
        fail_audit = m.total_failures == fail_sum
        print(f"  total_failures: {m.total_failures}  "
              f"sum(types): {fail_sum} → {'✅' if fail_audit else '❌'}")

        # ── 安全层 ──
        print(f"\n── 安全层 ──")
        if self.guard and self.guard.before and self.guard.after:
            before_main = self.guard.before.main
            after_main = self.guard.after.main
            if before_main.state == "PRESENT" and after_main.state == "PRESENT":
                prod_ok = before_main.sha256 == after_main.sha256
                print(f"  生产 DB SHA256 前: {before_main.sha256}")
                print(f"  生产 DB SHA256 后: {after_main.sha256}")
                print(f"  生产文件: {'✅ 不变' if prod_ok else '❌ 变化!'}")
            else:
                print(f"  生产 DB 前: {before_main.state}  后: {after_main.state}")
        else:
            print(f"  生产文件: ⚠️ 未配置生产保护")
        print(f"  真实推送: {m.real_push_count}  真实成交: {m.real_trade_count}")
        print(f"  账户修改: {m.account_modifications}")

        if m.violations:
            print(f"\n── 违规 ({len(m.violations)}) ──")
            for v in m.violations[:15]:
                print(f"  ❌ {v}")

        if m.signal_details:
            print(f"\n── 信号明细 ({len(m.signal_details)}) ──")
            for sd in m.signal_details:
                print(f"  [{sd['signal_id']}] {sd['symbol']}: "
                      f"{sd['candidate_level']}·{sd['candidate_action']} → "
                      f"{sd['effective_level']}·{sd['effective_action']} "
                      f"[{sd['confidence']}] {sd['market_session']}"
                      + (f" {sd['primary_norm']}" if sd['primary_norm'] else ""))

        print(f"\n{'='*70}")
        print(f"B2 报告结束 | status={m.status}")
        print(f"{'='*70}\n")

    def save_report(self) -> Path:
        report_path = self.shadow_dir / f"{self.metrics.run_id}_report.json"
        data = asdict(self.metrics)

        # P0-4: 保留原始数组以便重算分位数
        raw_arrays = {}
        for key in list(data.keys()):
            if key.endswith("_times_ms") or key.endswith("_ms_values"):
                vals = data[key]
                raw_arrays[key] = vals
                if vals:
                    data[f"{key}_p50"] = _p50(vals)
                    data[f"{key}_p95"] = _p95(vals)
                    data[f"{key}_max"] = max(vals)
                    data[f"{key}_n"] = len(vals)
                else:
                    data[f"{key}_p50"] = 0
                    data[f"{key}_p95"] = 0
                    data[f"{key}_max"] = 0
                    data[f"{key}_n"] = 0
                del data[key]

        # P0-4: 保留原始周期记录（含重算所需的全部字段）
        data["_p0_4_raw_cycle_records"] = [
            asdict(cr) for cr in self.metrics.cycle_records
        ]
        data["_p0_4_quantile_method"] = "numpy-style linear interpolation"
        data["_p0_4_unit"] = "milliseconds"

        report_path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str))
        logger.info(f"B2 报告已保存: {report_path}")
        return report_path


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Phase B2 — 盘中实时影子链路运行器",
        epilog="示例: python -m serenity_v2.phase_b2 --env shadow "
               "--protected-prod-db /path/to/serenity.db --duration 900 --interval 5",
    )
    parser.add_argument(
        "--env", required=True, choices=["shadow"],
        help="必须显式指定 --env shadow（其他模式拒绝运行）",
    )
    parser.add_argument(
        "--protected-prod-db", required=True, type=str,
        help="受保护的生产 DB 绝对路径（P0-1: 不从 worktree 推导）",
    )
    parser.add_argument(
        "--manifest", type=str, default="",
        help="可信配置清单路径（默认 docs/runtime-manifest.json）",
    )
    parser.add_argument(
        "--duration", type=int, default=B2_DEFAULT_DURATION,
        help=f"运行时长（秒），默认 {B2_DEFAULT_DURATION}（15min）",
    )
    parser.add_argument(
        "--interval", type=int, default=B2_DEFAULT_INTERVAL,
        help=f"抓取间隔（秒），默认 {B2_DEFAULT_INTERVAL}",
    )
    args = parser.parse_args()

    # 双重保障：CLI 已限制 choices=["shadow"]，此处再显式校验
    if args.env != "shadow":
        print("❌ B2 仅支持 --env shadow，拒绝运行")
        sys.exit(2)

    runner = B2Runner(
        duration_seconds=args.duration,
        interval_seconds=args.interval,
        protected_prod_db=args.protected_prod_db,
        manifest_path=args.manifest,
    )
    metrics = runner.run()
    runner.save_report()

    if metrics.auto_stop_triggered or metrics.violations:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
