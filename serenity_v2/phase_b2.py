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
import atexit
import hashlib
import json
import logging
import os
import signal
import sys
import time as _time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

CST = timezone(timedelta(hours=8))
logger = logging.getLogger("serenity_v2.phase_b2")

# v10: Report schema version — bump when report structure changes
B2_REPORT_SCHEMA_VERSION = "b2-1.1"

# v27: 报告脱敏配置
B2_DESENSITIZE_FIELDS = {
    "redact": [
        "account_snapshot_id_full",  # 完整 fixture 哈希（可用于指纹识别）
    ],
    "mask": [
        # 符号级别屏蔽（仅在 desensitize=True 时替换为匿名标识）
    ],
    "drop": [
        # 完全移除的字段
    ],
}

# v27: 报告复审敏感词检测
B2_SENSITIVITY_PATTERNS = [
    # 不应出现在报告中的敏感关键词
    (r"\bpassword\b", "疑似密码字段"),
    (r"\bapi[_-]?key\b", "疑似 API key"),
    (r"\btoken\b", "疑似 token"),
    (r"sk-[a-zA-Z0-9]{20,}", "疑似 OpenAI/API key 格式"),
    (r"\b\d{6,}\s*(元|¥|CNY|RMB)", "疑似金额（¥）"),
    (r"position.*\d{3,}\s*(股|share)", "疑似持仓数量"),
]

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

# ── P1: 跨事件 cooldown ──
B2_COOLDOWN_WINDOW_SEC = 300            # 5 分钟窗口
B2_COOLDOWN_POLICY_VERSION = "b2-cooldown-2.0"


# ---------------------------------------------------------------------------
# P1: CooldownTracker — 跨事件信号去重
# ---------------------------------------------------------------------------

def compute_market_fingerprint(event_id: str, price: float = 0.0,
                                volume: int = 0,
                                reference_price: float = 0.0) -> str:
    """计算稳定行情指纹，用于 cooldown re-arm 判定（v24: 参考价百分比分桶 + 整数运算）。

    行情实质变化时指纹改变 → cooldown 窗口内允许新信号。
    行情不变时指纹稳定 → cooldown 有效抑制重复信号。

    Args:
        event_id: 事件 ID（唯一标识一个行情事件）
        price: 最新成交价（元）
        volume: 成交量（股）
        reference_price: 参考价格（昨收价，元）。为 0 时回退为 price 自身（仅用于测试）。

    Returns:
        稳定的行情指纹字符串。

    v26 策略（基于昨收参考价的绝对值向零百分比分桶，纯整数运算）:
      - event_id 前 8 字符作为 session 标识
      - price bucket: 以昨收价为基准，计算绝对值百分比变化，按 2% 档位分桶，
        向零方向取整。正负方向对称。
        公式: abs_bucket = floor(|(price - ref) / ref| * 50)
              p_bucket = sign(delta) * abs_bucket
        等价于: abs_bucket = (abs(delta_cents) * 50) // ref_cents
        边界舍入方向: 向零取整（truncation toward zero）
        区间: [-2%, +2%) → p0, [+2%, +4%) → p1, [-4%, -2%) → p-1, ...
      - volume bucket: 整数 bit_length 对数量化（等价于 floor(log2(vol)/2)）
        边界舍入方向: 向下取整

    关键性质:
      - 价格未跨真实 2% 区间时 fingerprint 稳定（包括零边界）
      - ±2% 内（含昨收价轻微波动）→ 同一 p0 bucket
      - 同一价格的等价浮点表示得到相同 fingerprint
      - 微小 ULP 差异不改变 bucket
      - 正负方向对称：+2% → p1, -2% → p-1
      - 不使用当前价格同时作为分子和 bucket 宽度基准
    """
    parts = [event_id[:8] if event_id else "noevent"]
    if price > 0:
        # v26: 绝对值向零百分比分桶 — 以昨收价为基准，±2% 内均为 p0
        ref = reference_price if (reference_price is not None and reference_price > 0) else price
        price_cents = int(round(price * 100))
        ref_cents = int(round(ref * 100))
        delta_cents = price_cents - ref_cents

        if ref_cents > 0:
            # abs(|pct_change| / 2) = abs(delta_cents) * 50 // ref_cents
            # 向零方向取整（truncation toward zero）→ 正负对称
            abs_bucket = (abs(delta_cents) * 50) // ref_cents
            p_bucket = abs_bucket if delta_cents >= 0 else -abs_bucket
        else:
            p_bucket = 0
        parts.append(f"p{p_bucket}")
    if volume > 0:
        # v24: bit_length 对数量化 — 等价于 floor(log2(vol)/2)，但纯整数
        v = max(int(volume), 1)
        v_bucket = (v.bit_length() - 1) // 2
        parts.append(f"v{v_bucket}")
    return ":".join(parts)


class CooldownTracker:
    """跨事件 cooldown：同语义信号在窗口内只生成一次。

    与 EventProcessingLedger 的区别：
      - Ledger 保证同一 event_id 只处理一次（PER-EVENT 幂等）
      - CooldownTracker 保证同语义信号在时间窗口内不重复生成
        （CROSS-EVENT 去重）

    键语义（b2-cooldown-2.0）— 完整上下文感知：
      键 = (symbol, effective_trade_action, strategy_id, strategy_version,
            strategy_config_hash, signal_rule_version,
            account_snapshot_id_full, environment, market_fingerprint)

      不同策略/配置/账户/行情 → 不会错误互抑。
      同一语义 + 相同上下文 → 窗口内只允许一个信号。

    参数：
      - window_seconds: cooldown 窗口长度（默认 300s）
    """

    # 键字段列表（用于报告和审计）
    KEY_FIELDS = (
        "symbol", "effective_action", "strategy_id", "strategy_version",
        "strategy_config_hash", "signal_rule_version",
        "account_snapshot_id_full", "environment", "market_fingerprint",
    )

    def __init__(self, window_seconds: int = B2_COOLDOWN_WINDOW_SEC):
        self._window = window_seconds
        self._records: dict[tuple, float] = {}  # composite_key → mono timestamp
        self._skipped: int = 0
        self._total_checked: int = 0
        self._reset_reason: str = ""

    @property
    def window_seconds(self) -> int:
        return self._window

    @property
    def skipped_count(self) -> int:
        return self._skipped

    @property
    def total_checked(self) -> int:
        return self._total_checked

    @property
    def reset_reason(self) -> str:
        """最近一次 reset() 的原因。空字符串 = 从未 reset。"""
        return self._reset_reason

    def should_suppress(self,
                        symbol: str,
                        effective_action: str,
                        current_mono: float,
                        strategy_id: str = "",
                        strategy_version: str = "",
                        strategy_config_hash: str = "",
                        signal_rule_version: str = "",
                        account_snapshot_id_full: str = "",
                        environment: str = "",
                        market_fingerprint: str = "",
                        ) -> bool:
        """检查完整语义键是否在 cooldown 窗口内。

        键包含所有可能影响信号合法性的上下文维度。
        不同 strategy/config/account/env/行情 → 不会错误互抑。

        Returns:
            True  → 应跳过（窗口内已有同语义记录）
            False → 可以生成信号
        """
        self._total_checked += 1
        key = (
            symbol, effective_action,
            strategy_id, strategy_version,
            strategy_config_hash, signal_rule_version,
            account_snapshot_id_full, environment,
            market_fingerprint,
        )
        last = self._records.get(key)
        if last is not None:
            elapsed = current_mono - last
            if elapsed < self._window:
                self._skipped += 1
                return True
        # 不在窗口内（或首次出现）→ 记录并放行
        self._records[key] = current_mono
        return False

    def reset(self, reason: str = ""):
        """重置 tracker 状态（策略/config/账户上下文变更时调用）。

        Args:
            reason: 重置原因（如 "strategy_config_changed", "test_teardown"）。
        """
        self._records.clear()
        self._skipped = 0
        self._total_checked = 0
        self._reset_reason = reason


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
    cancelled_cycles_auto_stop: int = 0  # auto-stop 后未执行的计划周期

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
    signals_total: int = 0                    # emitted 信号数 (未被 cooldown/idempotent 抑制)
    signals_created_unique: int = 0           # = signals_total (每次 emit +1)
    # v29: 候选信号守恒 — 本次 run 内信号台处理的所有候选信号总数
    # scope=RUN  unit=CANDIDATE  source=COOLDOWN(total_checked)
    signals_candidate_total_run: int = 0
    signals_skipped_idempotent: int = 0
    # v28: 幂等统计 run/lifetime 分离
    signals_skipped_idempotent_run: int = 0    # scope=RUN  本次运行的 delta
    signals_skipped_idempotent_lifetime: int = 0  # scope=LIFETIME 账本生命周期累计
    signals_no_decision: int = 0              # scope=RUN  unit=EMITTED_SIGNAL
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

    # ── v14/v18: 跨事件去重（cooldown）──
    duplicate_signals_created: int = 0   # 精确重复（同一 event_id 被多次处理）
    signals_skipped_cooldown: int = 0    # 因 cooldown 跳过
    cooldown_enabled: bool = False
    cooldown_policy_version: str = ""
    cooldown_key_fields: str = ""        # 键字段（逗号分隔）
    cooldown_scope: str = ""             # 作用域描述
    cooldown_reset_reason: str = ""      # 最近一次 reset 原因

    # ── 安全层 ──
    prod_file_hash_before: dict = field(default_factory=dict)
    prod_file_hash_after: dict = field(default_factory=dict)
    real_push_count: int = 0
    real_trade_count: int = 0
    account_modifications: int = 0
    non_whitelist_network: int = 0

    # ── 运行终止元数据 (v10) ──
    run_completed: bool = False
    run_terminated_early: bool = False
    termination_type: str = ""          # SAFETY_AUTO_STOP / SESSION_BOUNDARY / USER_INTERRUPT / NORMAL
    termination_reason: str = ""
    target_duration_sec: int = 0
    actual_duration_ms: float = 0

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

    # ── v29: 结构化 postflight / review 结果（供 Live gate 读取，非 console grep）──
    postflight_audit: dict = field(default_factory=dict)
    review_result: dict = field(default_factory=dict)

    # ── v10: 结构化报告序列化 ──

    def to_report_dict(self) -> dict:
        """显式构造报告 dict，包含 schema 版本标记。

        与 asdict() 不同，此方法保证:
          - 顶层始终是 dict（不会变成 repr 字符串）
          - 包含 schema_version 字段
          - 嵌套 dataclass（如 CycleRecord）递归转换
        """
        report = {
            "schema_version": B2_REPORT_SCHEMA_VERSION,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_seconds": self.duration_seconds,
            "status": self.status,
        }

        # ── 调度层 ──
        report.update({
            "cycles_planned": self.cycles_planned,
            "cycles_started": self.cycles_started,
            "cycles_completed": self.cycles_completed,
            "cycles_skipped": self.cycles_skipped,
            "cycles_aborted": self.cycles_aborted,
            "cycles_overlapped": self.cycles_overlapped,
            "cycles_failed": self.cycles_failed,
            "not_due_cycles": self.not_due_cycles,
            "cancelled_cycles_auto_stop": self.cancelled_cycles_auto_stop,
        })

        # ── HTTP + 周期计时 ──
        report.update({
            "requests_total": self.requests_total,
            "requests_success": self.requests_success,
            "requests_failed": self.requests_failed,
            "http_response_times_ms": self.http_response_times_ms,
            "cycle_times_ms": self.cycle_times_ms,
            "schedule_delay_ms_values": self.schedule_delay_ms_values,
            "normalization_duration_ms_values": self.normalization_duration_ms_values,
            "event_duration_ms_values": self.event_duration_ms_values,
            "signal_duration_ms_values": self.signal_duration_ms_values,
            "raw_http_attempt_times_ms": self.raw_http_attempt_times_ms,
        })

        # ── 行情层 ──
        report.update({
            "http_responses": self.http_responses,
            "raw_received": self.raw_received,
            "raw_quote_records": self.raw_quote_records,
            "raw_stored": self.raw_stored,
            "raw_duplicates": self.raw_duplicates,
            "normalized_accepted": self.normalized_accepted,
            "normalized_rejected": self.normalized_rejected,
            "stale_count": self.stale_count,
            "future_timestamp_count": self.future_timestamp_count,
            "quarantined": self.quarantined,
            "quarantine_details": self.quarantine_details,
            "data_age_ms_values": self.data_age_ms_values,
        })

        # ── 事件层 ──
        report.update({
            "events_created": self.events_created,
            "events_deduplicated": self.events_deduplicated,
            "events_quarantined": self.events_quarantined,
            "events_not_triggered": self.events_not_triggered,
            "event_processing_failed": self.event_processing_failed,
        })

        # ── 信号层 ──
        report.update({
            "signals_total": self.signals_total,
            "signals_created_unique": self.signals_created_unique,
            # v29: 候选信号守恒
            "signals_candidate_total_run": self.signals_candidate_total_run,
            "signals_skipped_idempotent": self.signals_skipped_idempotent,
            # v28: run/lifetime 分离字段
            "signals_skipped_idempotent_run": self.signals_skipped_idempotent_run,
            "signals_skipped_idempotent_lifetime": self.signals_skipped_idempotent_lifetime,
            "signals_no_decision": self.signals_no_decision,
            "candidate_ACTION": self.candidate_ACTION,
            "effective_ACTION": self.effective_ACTION,
            "ACTION_downgraded": self.ACTION_downgraded,
            "ACTION_rejected": self.ACTION_rejected,
            "DECISION_count": self.DECISION_count,
            "WATCH_count": self.WATCH_count,
            "INFO_count": self.INFO_count,
        })

        # ── 失败分类 ──
        report.update({
            "scheduler_failed": self.scheduler_failed,
            "session_check_failed": self.session_check_failed,
            "fetch_failed": self.fetch_failed,
            "http_failed": self.http_failed,
            "parse_failed": self.parse_failed,
            "validation_failed": self.validation_failed,
            "normalization_failed": self.normalization_failed,
            "quarantine_failed": self.quarantine_failed,
            "event_failed": self.event_failed,
            "signal_failed": self.signal_failed,
            "ledger_failed": self.ledger_failed,
            "report_failed": self.report_failed,
            "safety_guard_failed": self.safety_guard_failed,
            "total_failures": self.total_failures,
        })

        # ── 幂等 ──
        report.update({
            "ledger_claimed": self.ledger_claimed,
            "ledger_completed": self.ledger_completed,
            "ledger_failed_count": self.ledger_failed_count,
            "ledger_already_processed": self.ledger_already_processed,
            "ledger_in_progress": self.ledger_in_progress,
        })

        # ── 安全层 ──
        report.update({
            "prod_file_hash_before": self.prod_file_hash_before,
            "prod_file_hash_after": self.prod_file_hash_after,
            # v14 canonical names
            "real_pushes": self.real_push_count,
            "real_trades": self.real_trade_count,
            "duplicate_signals_created": self.duplicate_signals_created,
            "signals_skipped_cooldown": self.signals_skipped_cooldown,
            "cooldown_enabled": self.cooldown_enabled,
            "cooldown_policy_version": self.cooldown_policy_version,
            "cooldown_key_fields": self.cooldown_key_fields,
            "cooldown_scope": self.cooldown_scope,
            "cooldown_reset_reason": self.cooldown_reset_reason,
            # deprecated (保留兼容，值必须与新字段相等)
            "real_push_count": self.real_push_count,
            "real_trade_count": self.real_trade_count,
            "account_modifications": self.account_modifications,
            "non_whitelist_network": self.non_whitelist_network,
        })

        # ── 终止元数据 (v10) ──
        report.update({
            "run_completed": self.run_completed,
            # v14 canonical name
            "terminated_early": self.run_terminated_early,
            "termination_type": self.termination_type,
            "termination_reason": self.termination_reason,
            "target_duration_sec": self.target_duration_sec,
            "actual_duration_ms": self.actual_duration_ms,
            # deprecated (保留兼容)
            "run_terminated_early": self.run_terminated_early,
        })

        # ── 自动停止 ──
        report.update({
            "auto_stop_triggered": self.auto_stop_triggered,
            "auto_stop_reason": self.auto_stop_reason,
            "signal_generation_suspended": self.signal_generation_suspended,
            "session_boundary_reached": self.session_boundary_reached,
            "session_at_boundary": self.session_at_boundary,
        })

        # ── 明细 ──
        report["cycle_sessions"] = self.cycle_sessions
        report["cycle_records"] = [asdict(cr) for cr in self.cycle_records]
        report["signal_details"] = self.signal_details
        report["violations"] = self.violations

        # ── v29: 结构化 postflight / review（Live gate canonical 依据，非 console grep）──
        if self.postflight_audit:
            pa = self.postflight_audit
            report["postflight"] = {
                "run_id": self.run_id,           # v29: 校验只读本次运行文件
                "all_pass": pa.get("all_pass", False),
                "total_checks": pa.get("total_checks", 0),
                "passed": pa.get("passed", 0),
                "failed": pa.get("failed", 0),
                "failed_invariant_ids": [
                    c["id"] for c in pa.get("checks", []) if not c.get("pass", True)
                ],
                "checks": pa.get("checks", []),
            }
        else:
            report["postflight"] = {
                "run_id": self.run_id,
                "all_pass": False, "total_checks": 0, "passed": 0,
                "failed": 0, "failed_invariant_ids": [], "checks": [],
            }
        if self.review_result:
            rv = self.review_result
            report["review"] = {
                "run_id": self.run_id,           # v29: 校验只读本次运行文件
                "ok": rv.get("ok", False),
                "findings_count": rv.get("findings_count", 0),
                "findings": rv.get("findings", []),
                "warnings_count": rv.get("warnings_count", 0),
                "warnings": rv.get("warnings", []),
            }
        else:
            report["review"] = {
                "run_id": self.run_id,
                "ok": False, "findings_count": 0, "findings": [],
                "warnings_count": 0, "warnings": [],
            }

        return report

    # ── v27: 综合后飞行不变量审计 ──

    def audit_postflight_invariants(self, prod_guard_before: dict = None,
                                    prod_guard_after: dict = None,
                                    shadow_db_path: str = "") -> dict:
        """综合后飞行不变量与安全审计。

        返回包含所有不变量检查结果的审计报告。
        所有检查项必须全部 PASS 才算审计通过。
        """
        checks = []

        # ────── A. 调度方程 (v10) ──────
        eq1 = (self.cycles_planned == self.cycles_started
               + self.cycles_skipped + self.not_due_cycles
               + self.cancelled_cycles_auto_stop)
        checks.append({
            "category": "scheduling",
            "id": "SCHED_EQ1",
            "name": "planned = started + skipped + not_due + cancelled",
            "pass": eq1,
            "detail": (f"planned={self.cycles_planned} == "
                       f"started({self.cycles_started}) + skipped({self.cycles_skipped}) + "
                       f"not_due({self.not_due_cycles}) + cancelled({self.cancelled_cycles_auto_stop})"),
        })

        eq2 = (self.cycles_started == self.cycles_completed
               + self.cycles_aborted + self.cycles_failed)
        checks.append({
            "category": "scheduling",
            "id": "SCHED_EQ2",
            "name": "started = completed + aborted + failed",
            "pass": eq2,
            "detail": (f"started={self.cycles_started} == "
                       f"completed({self.cycles_completed}) + aborted({self.cycles_aborted}) + "
                       f"failed({self.cycles_failed})"),
        })

        # ────── B. 数据流方程 ──────
        eq3 = (self.raw_received == self.raw_stored + self.raw_duplicates)
        checks.append({
            "category": "data_integrity",
            "id": "DATA_EQ1",
            "name": "raw_received = raw_stored + raw_duplicates",
            "pass": eq3,
            "detail": (f"raw_received={self.raw_received} == "
                       f"raw_stored({self.raw_stored}) + raw_duplicates({self.raw_duplicates})"),
        })

        # ────── C. 事件方程 ──────
        eq4 = (self.events_created >= self.events_deduplicated)
        checks.append({
            "category": "data_integrity",
            "id": "DATA_EQ2",
            "name": "events_created >= events_deduplicated",
            "pass": eq4,
            "detail": f"events_created={self.events_created} >= events_deduplicated={self.events_deduplicated}",
        })

        # ────── D. 信号方程 (v29: 候选信号守恒，统一 scope/unit) ──────
        # 守恒模型（无重复计数）：
        #   candidate_total_run (unit=CANDIDATE, 信号台处理的全部候选)
        #     = skipped_idempotent_run (幂等跳过, 不进入cooldown)
        #     + cooldown_total_checked_run (进入cooldown检查的候选)
        #   cooldown_total_checked_run = emitted (unit=EMITTED_SIGNAL)
        #     + skipped_cooldown (unit=CANDIDATE)
        #   no_decision ⊆ emitted（_record_signal 里 emitted 同时计 no_decision），
        #   故不单列，避免重复计数。
        # 展开 → candidate_total_run = skipped_idempotent_run + emitted + skipped_cooldown
        eq5 = (self.signals_candidate_total_run == self.signals_created_unique
               + self.signals_skipped_idempotent_run + self.signals_skipped_cooldown)
        checks.append({
            "category": "data_integrity",
            "id": "SIGNAL_EQ1",
            "name": "candidate_total_run = skipped_idempotent(run) + emitted + skipped_cooldown",
            "pass": eq5,
            "detail": (f"candidate_total_run={self.signals_candidate_total_run} == "
                       f"skipped_idempotent(run)={self.signals_skipped_idempotent_run} + "
                       f"emitted({self.signals_created_unique}) + "
                       f"skipped_cooldown({self.signals_skipped_cooldown})"
                       f" [no_decision({self.signals_no_decision}) ⊆ emitted]"
                       f" (lifetime={self.signals_skipped_idempotent_lifetime})"),
        })

        # ────── E. 失败会计方程 ──────
        fail_sum = (self.scheduler_failed + self.session_check_failed
                    + self.fetch_failed + self.http_failed + self.parse_failed
                    + self.validation_failed + self.normalization_failed
                    + self.quarantine_failed + self.event_failed
                    + self.signal_failed + self.ledger_failed
                    + self.report_failed + self.safety_guard_failed)
        eq6 = (self.total_failures == fail_sum)
        checks.append({
            "category": "failure_accounting",
            "id": "FAIL_EQ1",
            "name": "total_failures = sum(all failure types)",
            "pass": eq6,
            "detail": f"total_failures={self.total_failures} == sum(types)={fail_sum}",
        })

        # ────── F. 账本幂等方程 ──────
        eq7 = (self.ledger_claimed == self.ledger_completed
               + self.ledger_failed_count + self.ledger_in_progress)
        checks.append({
            "category": "idempotency",
            "id": "LEDGER_EQ1",
            "name": "ledger claimed = completed + failed + in_progress",
            "pass": eq7,
            "detail": (f"claimed={self.ledger_claimed} == "
                       f"completed({self.ledger_completed}) + "
                       f"failed({self.ledger_failed_count}) + "
                       f"in_progress({self.ledger_in_progress})"),
        })

        # ────── G. 生产 DB 不变量 (v28: 客观 + 归因分离) ──────
        # G1: 客观事实 — 生产 DB 全局 SHA256 是否变化
        prod_unchanged = True  # default: no guard data → assume unchanged
        if prod_guard_before and prod_guard_after:
            b_main_hash = prod_guard_before.get("sha256")
            a_main_hash = prod_guard_after.get("sha256")
            if b_main_hash and a_main_hash:
                prod_unchanged = (b_main_hash == a_main_hash)
            checks.append({
                "category": "security",
                "id": "SEC_PROD_DB_GLOBAL_UNCHANGED",
                "name": "生产 DB SHA256 客观不变 (pre vs post)",
                "pass": prod_unchanged,
                "detail": (f"before={b_main_hash[:12]}... "
                           f"after={a_main_hash[:12]}... "
                           f"{'✅ 未变' if prod_unchanged else '❌ 客观变化!'}"),
            })

        # G2: 归因判断 — Runner 是否写入生产 DB
        runner_no_write = (self.real_push_count == 0
                           and self.real_trade_count == 0
                           and self.account_modifications == 0)
        checks.append({
            "category": "security",
            "id": "SEC_RUNNER_DID_NOT_MODIFY_PROD_DB",
            "name": "Runner 归因: 零生产写入 (推送/成交/账户修改=0)",
            "pass": runner_no_write,
            "detail": (f"real_pushes={self.real_push_count} "
                       f"real_trades={self.real_trade_count} "
                       f"account_mods={self.account_modifications}"),
        })

        # ────── H. 零副作用硬门禁 ──────
        zero_side_effects = (self.real_push_count == 0 and self.real_trade_count == 0
                             and self.account_modifications == 0)
        checks.append({
            "category": "security",
            "id": "SEC_ZERO_SIDE_EFFECTS",
            "name": "零真实副作用（无推送/成交/账户修改）",
            "pass": zero_side_effects,
            "detail": (f"real_pushes={self.real_push_count} "
                       f"real_trades={self.real_trade_count} "
                       f"account_mods={self.account_modifications}"),
        })

        # ────── I. 影子 DB 身份 (P0-1) ──────
        if shadow_db_path and "/shadow" in str(shadow_db_path):
            checks.append({
                "category": "security",
                "id": "SEC_SHADOW_IDENTITY",
                "name": "DB 路径包含 /shadow（非生产）",
                "pass": True,
                "detail": f"shadow_db={shadow_db_path}",
            })
        else:
            checks.append({
                "category": "security",
                "id": "SEC_SHADOW_IDENTITY",
                "name": "DB 路径包含 /shadow（非生产）",
                "pass": False,
                "detail": f"shadow_db={shadow_db_path} 路径不含 /shadow!",
            })

        # ────── J. 报告 schema 版本 ──────
        checks.append({
            "category": "report_integrity",
            "id": "RPT_SCHEMA_VERSION",
            "name": f"报告 schema 版本 = {B2_REPORT_SCHEMA_VERSION}",
            "pass": True,
            "detail": f"schema_version={B2_REPORT_SCHEMA_VERSION}",
        })

        # ────── K. 运行终止合理性 ──────
        if self.run_completed and not self.run_terminated_early:
            termination_ok = True
            term_detail = "正常运行完成"
        elif self.run_terminated_early:
            termination_ok = (self.termination_type
                              in ("SAFETY_AUTO_STOP", "SESSION_BOUNDARY",
                                  "USER_INTERRUPT", "NORMAL"))
            term_detail = (f"提前终止 type={self.termination_type} "
                           f"reason={self.termination_reason}")
        else:
            termination_ok = False
            term_detail = "运行未完成（异常）"
        checks.append({
            "category": "process_integrity",
            "id": "PROC_TERMINATION",
            "name": "运行终止状态合理",
            "pass": termination_ok,
            "detail": term_detail,
        })

        # ────── 汇总 ──────
        passed = sum(1 for c in checks if c["pass"])
        failed = len(checks) - passed
        all_pass = failed == 0

        return {
            "audit_version": "v28",
            "timestamp": datetime.now(tz=CST).isoformat(timespec="seconds"),
            "run_id": self.run_id,
            "all_pass": all_pass,
            "total_checks": len(checks),
            "passed": passed,
            "failed": failed,
            "checks": checks,
            "summary": (f"✅ 审计全部通过 ({passed}/{len(checks)})"
                        if all_pass else
                        f"❌ 审计失败: {failed}/{len(checks)} 项未通过"),
        }

    def print_audit_report(self, audit: dict):
        """打印人类可读的审计报告。"""
        print(f"\n{'='*70}")
        print(f"  Postflight 不变量与安全审计 (v28)")
        print(f"{'='*70}")
        print(f"  run_id: {audit['run_id']}")
        print(f"  timestamp: {audit['timestamp']}")
        print()

        from collections import defaultdict
        by_cat = defaultdict(list)
        for c in audit["checks"]:
            by_cat[c["category"]].append(c)

        cat_names = {
            "scheduling": "调度方程",
            "data_integrity": "数据完整性",
            "failure_accounting": "失败会计",
            "idempotency": "幂等性",
            "security": "安全边界",
            "report_integrity": "报告完整性",
            "process_integrity": "进程完整性",
        }

        for cat, items in by_cat.items():
            cat_label = cat_names.get(cat, cat)
            print(f"  ── {cat_label} ──")
            for c in items:
                icon = "✅" if c["pass"] else "❌"
                print(f"    {icon} [{c['id']}] {c['name']}")
                if not c["pass"]:
                    print(f"       详情: {c['detail']}")

        print()
        print(f"  {audit['summary']}")
        print(f"{'='*70}\n")

    # ── v27: 报告复审与脱敏 ──

    def review_report(self, audit_result: dict = None) -> dict:
        """复审 B2 报告质量与敏感信息 (v28: 集成 postflight 审计)。

        返回审查结果: {ok, warnings, findings}。
        audit_result: 若提供，失败的不变量将计入 findings。
        """
        import re
        report = self.to_report_dict()
        findings = []
        warnings = []

        # 1. 必填字段完整性检查
        required_fields = [
            "run_id", "schema_version", "status", "started_at", "ended_at",
            "duration_seconds", "cycles_completed", "total_failures",
        ]
        for field in required_fields:
            if field not in report or report[field] is None:
                findings.append(f"缺失必填字段: {field}")

        # 2. 审计方程完整性 (v27: 直接计算，不依赖 save_report 的 _audit_scheduling_v10)
        eq1 = (report.get("cycles_planned", 0) == report.get("cycles_started", 0)
               + report.get("cycles_skipped", 0) + report.get("not_due_cycles", 0)
               + report.get("cancelled_cycles_auto_stop", 0))
        eq2 = (report.get("cycles_started", 0) == report.get("cycles_completed", 0)
               + report.get("cycles_aborted", 0) + report.get("cycles_failed", 0))
        if not eq1:
            findings.append("调度方程1不通过: planned != started + skipped + not_due + cancelled")
        if not eq2:
            findings.append("调度方程2不通过: started != completed + aborted + failed")

        # 3. 安全层门禁
        if report.get("real_pushes", -1) != 0:
            findings.append(f"真实推送计数异常: {report['real_pushes']}")
        if report.get("real_trades", -1) != 0:
            findings.append(f"真实成交计数异常: {report['real_trades']}")
        if report.get("account_modifications", -1) != 0:
            findings.append(f"账户修改异常: {report['account_modifications']}")

        # 4. 敏感信息检测
        report_text = json.dumps(report, ensure_ascii=False, default=str)
        for pattern, description in B2_SENSITIVITY_PATTERNS:
            matches = re.findall(pattern, report_text, re.IGNORECASE)
            if matches:
                warnings.append(f"{description}: 匹配到 {len(matches)} 次 ({pattern})")

        # 5. 数据合理性检查
        if report.get("cycles_completed", 0) < 0:
            findings.append("cycles_completed 为负值")
        if report.get("duration_seconds", 0) < 0:
            findings.append("duration_seconds 为负值")

        # v28: 检查 postflight 审计结果 — 任一无变数失败均为 finding
        if audit_result is not None and not audit_result.get("all_pass", True):
            for c in audit_result.get("checks", []):
                if not c.get("pass", True):
                    findings.append(
                        f"postflight invariant failed: [{c['id']}] "
                        f"{c.get('name', c['id'])} — {c.get('detail', '')}"
                    )

        return {
            "ok": len(findings) == 0,
            "findings_count": len(findings),
            "warnings_count": len(warnings),
            "findings": findings,
            "warnings": warnings,
            "summary": (f"✅ 报告复审通过"
                        if not findings else
                        f"❌ 报告复审发现 {len(findings)} 个问题"),
        }

    @staticmethod
    def desensitize_report(report_data: dict) -> dict:
        """对 B2 报告进行脱敏处理。

        返回脱敏后的 dict 副本，原数据不变。
        """
        import copy
        sanitized = copy.deepcopy(report_data)

        # 红action 指定字段
        for field in B2_DESENSITIZE_FIELDS.get("redact", []):
            if field in sanitized:
                sanitized[field] = "[REDACTED]"

        # 脱敏信号详情中的 fixture 哈希
        if "signal_details" in sanitized:
            for sd in sanitized["signal_details"]:
                if "account_snapshot_id_full" in sd:
                    sd["account_snapshot_id_full"] = "[REDACTED]"
                if "account_snapshot_id" in sd:
                    sd["account_snapshot_id"] = (
                        sd["account_snapshot_id"][:8] + "...[REDACTED]"
                        if len(sd["account_snapshot_id"]) > 8
                        else "[REDACTED]"
                    )

        # 脱敏 prod_file_hash 中的完整 SHA256
        for key in ("prod_file_hash_before", "prod_file_hash_after"):
            if key in sanitized and isinstance(sanitized[key], dict):
                if "sha256" in sanitized[key] and sanitized[key]["sha256"]:
                    sanitized[key]["sha256"] = (
                        sanitized[key]["sha256"][:12] + "...[REDACTED]"
                    )

        # 标记脱敏版本
        sanitized["_desensitized"] = True
        sanitized["_desensitized_version"] = "v28"

        return sanitized


# ---------------------------------------------------------------------------
# v27: 独立报告复审与脱敏工具
# ---------------------------------------------------------------------------

class B2ReportReviewer:
    """独立的 B2 报告复审与脱敏工具。

    用于检查已保存的报告文件，无需启动 B2Runner。
    """

    @staticmethod
    def review_file(report_path: str) -> dict:
        """复审一个已保存的报告文件。"""
        import re
        from pathlib import Path

        path = Path(report_path)
        if not path.exists():
            return {"ok": False, "findings": [f"文件不存在: {report_path}"],
                    "warnings": [], "findings_count": 1, "warnings_count": 0,
                    "summary": "❌ 文件不存在"}

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            return {"ok": False, "findings": [f"JSON 解析失败: {e}"],
                    "warnings": [], "findings_count": 1, "warnings_count": 0,
                    "summary": "❌ JSON 解析失败"}

        # 通过 B2Metrics 实例进行复审
        m = B2Metrics()
        m.run_id = data.get("run_id", "")
        m.status = data.get("status", "")
        # 注入关键字段供审查
        for field in ["run_id", "schema_version", "status", "started_at", "ended_at",
                      "duration_seconds", "cycles_completed", "total_failures",
                      "real_pushes", "real_trades", "account_modifications",
                      "cycles_planned", "cycles_started", "cycles_skipped",
                      "not_due_cycles", "cancelled_cycles_auto_stop",
                      "cycles_aborted", "cycles_failed"]:
            if field in data:
                setattr(m, field, data[field])

        return m.review_report()

    @staticmethod
    def desensitize_file(report_path: str, output_path: str = "") -> str:
        """对报告文件进行脱敏并保存。"""
        from pathlib import Path

        path = Path(report_path)
        data = json.loads(path.read_text(encoding="utf-8"))
        sanitized = B2Metrics.desensitize_report(data)

        if not output_path:
            output_path = str(path.parent / f"{path.stem}_sanitized{path.suffix}")

        Path(output_path).write_text(
            json.dumps(sanitized, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        return output_path


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
        self._lock_acquired = False

        # P1: 跨事件 cooldown tracker — 始终初始化（与 init_env 无关）
        # 作用域：单策略上下文 — (symbol, effective_action) 键
        # strategy/config/rule/account 在单个 run() 调用期间不可变
        self.cooldown = CooldownTracker(window_seconds=B2_COOLDOWN_WINDOW_SEC)
        self.metrics.cooldown_enabled = True
        self.metrics.cooldown_policy_version = B2_COOLDOWN_POLICY_VERSION
        self.metrics.cooldown_key_fields = ",".join(CooldownTracker.KEY_FIELDS)
        self.metrics.cooldown_scope = "single_strategy_fixture_shadow"
        self._latest_market: dict[str, dict] = {}  # v24: symbol→market data for fingerprint (price, volume, reference_price)

        if init_env:
            self._init_env()
        self._consecutive_failures = 0
        self._cycle_count = 0  # track across run()
        self._cooldown_context_snapshot: Optional[dict] = None  # v19: run() 入口快照

    # ── v11: 进程隔离锁 (原子 O_CREAT|O_EXCL + fcntl.flock) ──

    _lock_fd: int | None = None  # 持有锁期间保持打开的 fd

    @classmethod
    def _lock_path(cls) -> Path:
        """影子目录下的进程锁文件路径。"""
        ROOT = Path(__file__).resolve().parent.parent
        return ROOT / "shadow_data" / "b2" / ".b2_runner.lock"

    @classmethod
    def _acquire_lock(cls, caller_token: str = "") -> None:
        """原子获取进程互斥锁。

        使用 os.O_CREAT | O_EXCL 原子创建锁文件。
        若文件已存在（其他进程持有锁），检查 PID 是否存活:
          - 存活 → RuntimeError（拒绝并发）
          - 已死 → 清理残留锁并重试
          - 同 PID + 同 token → 允许顺序重用

        fcntl.flock(LOCK_EX | LOCK_NB) 提供第二层防护。
        fd 保持打开直到进程终止 → SIGKILL/崩溃自动释放。
        """
        lock = cls._lock_path()
        lock.parent.mkdir(parents=True, exist_ok=True)

        max_retries = 3
        for attempt in range(max_retries):
            try:
                fd = os.open(
                    str(lock),
                    os.O_CREAT | os.O_EXCL | os.O_RDWR,
                    0o644,
                )
                # 原子创建成功 — 我们是唯一持有者
                os.write(fd, str(os.getpid()).encode())
                os.fsync(fd)

                # fcntl 咨询锁（第二层防护，跨 NFS 也有效）
                try:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except (ImportError, OSError):
                    pass  # fcntl 不可用时降级为 O_EXCL 防护

                cls._lock_fd = fd
                # fd 保持打开直到进程终止或 release_lock()
                # SIGKILL/崩溃时 OS 自动关闭 fd → 锁自动释放
                atexit.register(cls._release_lock)
                logger.debug(
                    f"锁已获取: {lock} PID={os.getpid()} "
                    f"token={caller_token or '(none)'}"
                )
                return

            except FileExistsError:
                # 锁文件已存在 → 检查是否可重用
                try:
                    stale_pid = int(lock.read_text().strip())
                except (ValueError, OSError):
                    # 损坏的锁文件
                    logger.warning(f"锁文件内容无效，清理: {lock}")
                    lock.unlink(missing_ok=True)
                    continue

                # 同进程 + 同 token → 允许顺序重用
                if stale_pid == os.getpid() and caller_token:
                    existing_fd = cls._lock_fd
                    if existing_fd is not None:
                        logger.debug(
                            f"锁文件属于当前进程 (PID={stale_pid}, "
                            f"token={caller_token})，允许重用"
                        )
                        return

                # 检查进程是否存活
                try:
                    os.kill(stale_pid, 0)
                    # 进程存活 → 拒绝
                    raise RuntimeError(
                        f"另一个 B2 实例正在运行 (PID={stale_pid})。"
                        f"锁文件: {lock}"
                    )
                except OSError:
                    # PID 不存在 → 残留锁，清理后重试
                    logger.warning(
                        f"残留锁文件 (PID={stale_pid} 已死)，"
                        f"清理后重试 (attempt {attempt + 1}/{max_retries})"
                    )
                    lock.unlink(missing_ok=True)
                    continue

        raise RuntimeError(
            f"无法获取进程锁 after {max_retries} attempts: {lock}"
        )

    @classmethod
    def _release_lock(cls) -> None:
        """释放进程互斥锁（关闭 fd → OS 自动释放 flock + 删除文件）。"""
        if cls._lock_fd is not None:
            try:
                os.close(cls._lock_fd)
            except OSError:
                pass
            cls._lock_fd = None

        lock = cls._lock_path()
        try:
            if lock.exists():
                pid_text = lock.read_text().strip()
                if int(pid_text) == os.getpid():
                    lock.unlink(missing_ok=True)
        except (ValueError, OSError):
            lock.unlink(missing_ok=True)

    @classmethod
    def _is_lock_held(cls) -> bool:
        """检查当前进程是否持有锁。"""
        if cls._lock_fd is None:
            return False
        try:
            os.fstat(cls._lock_fd)
            return True
        except OSError:
            return False

    def _init_env(self):
        # v11: 原子获取进程互斥锁 — 阻止双实例并发
        self._acquire_lock(caller_token=f"b2-runner-{id(self)}")
        self._lock_acquired = True

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

        # v28: T0 快照 — 用于计算 run-scoped delta
        self._idempotent_skipped_at_T0 = (
            self.idempotent.stats.get("already_processed", 0))

        # P0-2: 稳定上下文键 — 用于信号幂等
        # strategy_id: B2 影子运行器唯一标识（非 Python object repr）
        self._strategy_id = "b2-shadow-runner"
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

        return True, details, violations

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
                # v27: 保存生产 DB 哈希到 metrics（供审计使用）
                self.metrics.prod_file_hash_before = {
                    "sha256": prod.sha256, "size": prod.size,
                    "inode": prod.inode, "device": prod.device,
                }
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

    def _verify_cooldown_context(self) -> tuple:
        """v22: 验证 cooldown 作用域运行时约束 — fail-closed。

        检查:
        - 环境为 SHADOW（非生产）— production 由 env.mode 判定，不由路径推断
        - 单策略配置（strategy_version, config_hash 已设置且非空）
        - account_snapshot_id 已设置（FIXTURE 模式）
        - protected_prod_db 必须已配置（显式声明受保护的生产 DB）
        - shadow_db 与 protected_prod_db 路径隔离（realpath 不同、非同一 inode）
        - push_adapter 必须为 disabled
        - trade_adapter 必须为 disabled

        Returns:
            (ok, violations_list)
        """
        violations = []

        # 1. 环境检查: 必须是 shadow 模式
        from .env import get_env
        try:
            env = get_env()
            env_mode = env.mode if env else "UNSET"
        except Exception:
            env_mode = "UNSET"
        if env_mode != "shadow":
            violations.append(
                f"cooldown SCOPE 违规: environment={env_mode}, "
                f"期望 shadow（cooldown_scope={self.metrics.cooldown_scope}）"
            )

        # 2. 单策略配置检查
        sv = getattr(self, '_strategy_version', None)
        sh = getattr(self, '_strategy_config_hash', None)
        if not sv or not sh:
            violations.append(
                f"cooldown SCOPE 违规: strategy_version={sv!r} "
                f"strategy_config_hash={sh!r}（两者都必须设置）"
            )

        # 3. 账户快照检查
        asid = getattr(self, '_account_snapshot_id', None)
        if not asid:
            violations.append(
                "cooldown SCOPE 违规: account_snapshot_id 未设置（需要 FIXTURE 模式）"
            )

        # 4. 生产 DB 保护必须已配置（防御性：必须显式声明受保护的生产 DB 路径）
        protected_prod_db = getattr(self, '_protected_prod_db', None)
        if not protected_prod_db:
            violations.append(
                "cooldown SCOPE 违规: protected_prod_db 未设置，"
                "无法保证生产 DB 不被修改"
            )

        # 5. Shadow/生产 DB 路径隔离检查
        shadow_db = getattr(self, 'shadow_db', None)
        if shadow_db and protected_prod_db:
            try:
                shadow_real = Path(str(shadow_db)).resolve()
                prod_real = Path(str(protected_prod_db)).resolve()
                # 5a. realpath 相同
                if shadow_real == prod_real:
                    violations.append(
                        "cooldown SCOPE 违规: shadow_db 与 protected_prod_db "
                        f"realpath 相同 ({shadow_real})"
                    )
                # 5b. 文件存在时 inode 相同（硬链接或同一文件）
                elif shadow_real.exists() and prod_real.exists():
                    shadow_stat = shadow_real.stat()
                    prod_stat = prod_real.stat()
                    if (shadow_stat.st_dev, shadow_stat.st_ino) == \
                       (prod_stat.st_dev, prod_stat.st_ino):
                        violations.append(
                            "cooldown SCOPE 违规: shadow_db 与 protected_prod_db "
                            f"inode 相同 dev={shadow_stat.st_dev} ino={shadow_stat.st_ino} "
                            "（通过硬链接或同一文件）"
                        )
            except Exception as e:
                violations.append(
                    f"cooldown SCOPE 违规: DB 隔离检查失败: {e}"
                )

        # 6. push_adapter 必须为 disabled
        try:
            if env.push_adapter is not None:
                violations.append(
                    "cooldown SCOPE 违规: push_adapter 已启用，"
                    "单策略 FIXTURE SHADOW 作用域不允许推送"
                )
        except Exception:
            pass  # env 未初始化时由 check #1 捕获

        # 7. trade_adapter 必须为 disabled
        try:
            if env.broker_adapter is not None:
                violations.append(
                    "cooldown SCOPE 违规: trade_adapter 已启用，"
                    "单策略 FIXTURE SHADOW 作用域不允许交易"
                )
        except Exception:
            pass  # env 未初始化时由 check #1 捕获

        return len(violations) == 0, violations

    def _snapshot_cooldown_context(self) -> dict:
        """v22: 拍摄 cooldown 上下文快照用于运行时逐周期验证。

        Returns:
            包含 strategy_version, strategy_config_hash, account_snapshot_id,
            protected_prod_db, shadow_db_realpath, push_adapter, trade_adapter 的字典。
            如果 run() 执行期间其中任何值发生变化，cooldown 必须重置。
        """
        shadow_db = getattr(self, 'shadow_db', None)
        try:
            from .env import get_env
            env = get_env()
            push_adapter = "disabled" if env.push_adapter is None else "present"
            trade_adapter = "disabled" if env.broker_adapter is None else "present"
        except Exception:
            push_adapter = "unknown"
            trade_adapter = "unknown"

        return {
            "strategy_version": getattr(self, '_strategy_version', None),
            "strategy_config_hash": getattr(self, '_strategy_config_hash', None),
            "account_snapshot_id": getattr(self, '_account_snapshot_id', None),
            "protected_prod_db": getattr(self, '_protected_prod_db', None),
            "shadow_db_realpath": str(Path(str(shadow_db)).resolve()) if shadow_db else None,
            "push_adapter": push_adapter,
            "trade_adapter": trade_adapter,
        }

    def _auto_stop(self, reason: str):
        if not self.metrics.auto_stop_triggered:
            import time as _time
            self.metrics.auto_stop_triggered = True
            self.metrics.auto_stop_reason = reason
            # 统计未执行的剩余计划周期
            executed = (self.metrics.cycles_started + self.metrics.cycles_skipped
                        + self.metrics.not_due_cycles)
            self.metrics.cancelled_cycles_auto_stop = max(
                0, self.metrics.cycles_planned - executed
            )
            # 运行级终止元数据
            self.metrics.run_completed = False
            self.metrics.run_terminated_early = True
            self.metrics.termination_type = "SAFETY_AUTO_STOP"
            self.metrics.termination_reason = reason
            if hasattr(self, '_run_start_mono'):
                self.metrics.actual_duration_ms = (
                    _time.monotonic() - self._run_start_mono
                ) * 1000
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
            "event_id": sig.event_id,
            "candidate_level": cand,
            "candidate_action": sig.candidate_trade_action,
            "effective_level": eff,
            "effective_action": sig.effective_trade_action,
            "primary_norm": sig.primary_normalization_reason,
            "secondary_norms": sig.secondary_normalization_reasons,
            "confidence": sig.confidence,
            "market_session": sig.market_session,
            "action_suppressed": sig.action_suppressed,
            "execution_tags": sig.execution_tags,
            # ── v14: 完整 lineage（从 ledger/持久化记录读取）──
            "strategy_id": self._strategy_id,
            "strategy_version": self._strategy_version,
            "strategy_config_hash": self._strategy_config_hash,
            "account_snapshot_id": self._account_snapshot_id,
            "account_snapshot_id_full": self._account_snapshot_id,
            "signal_rule_version": self._strategy_version,
            "session": sig.market_session,
            "environment": "shadow",
        })

        required_tags = {"SHADOW_ONLY", "NOT_FOR_EXECUTION",
                         "ACCOUNT_CONTEXT_FIXTURE", "ACCOUNT_CONTEXT_STALE"}
        missing = required_tags - set(tags)
        if missing:
            self.metrics.violations.append(
                f"缺少安全标签: {sig.signal_id} missing={missing} tags={tags}"
            )

    def run(self):
        from .clock import get_clock
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

        # ── v19: Cooldown 作用域运行时验证（fail-closed）──
        ctx_ok, ctx_violations = self._verify_cooldown_context()
        if not ctx_ok:
            for v in ctx_violations:
                self.metrics.violations.append(v)
                logger.error(f"cooldown scope: {v}")
                print(f"❌ {v}")
            self.metrics.status = "ERROR"
            return self.metrics
        # 拍摄上下文快照用于逐周期验证
        self._cooldown_context_snapshot = self._snapshot_cooldown_context()

        self.metrics.status = "RUNNING"
        self.metrics.started_at = datetime.now(tz=CST).isoformat(timespec="seconds")

        clock = get_clock()
        symbols = B2_SYMBOLS
        last_session = session

        start_mono = _time.monotonic()
        self._run_start_mono = start_mono
        next_cycle_mono = start_mono
        cycle = 0
        self.metrics.cycles_planned = max(1, self.duration // self.interval)
        self.metrics.target_duration_sec = self.duration

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

        # ── v28.2: 主循环开始前重新快照 T0 幂等账本 ──
        # 构造器 _init_env 在 __init__ 时快照(0)，但到主循环真正启动时，
        # 持久化账本已累计上次 run 的跳过数(如162)，导致 run-scoped delta 错误
        # (SIGNAL_EQ1 不成立)。必须在首周期抓取前重新快照。
        try:
            self._idempotent_skipped_at_T0 = (
                self.idempotent.stats.get("already_processed", 0))
        except Exception:
            self._idempotent_skipped_at_T0 = 0

        try:
            while True:
                now_mono = _time.monotonic()
                elapsed_total = now_mono - start_mono
                if elapsed_total >= self.duration:
                    break

                # 每周期重新检查时段（可能跨越边界）
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

                # v19: 逐周期 cooldown 上下文验证
                if self._cooldown_context_snapshot:
                    current_ctx = self._snapshot_cooldown_context()
                    if current_ctx != self._cooldown_context_snapshot:
                        changed = [k for k in current_ctx
                                   if current_ctx[k] != self._cooldown_context_snapshot[k]]
                        reason = f"cooldown_context_changed:{','.join(changed)}"
                        logger.warning(f"cooldown 上下文变更: {reason}")
                        self.cooldown.reset(reason)
                        self._cooldown_context_snapshot = current_ctx

                # v11: 周期边界守卫 — 不允许启动超过 planned 的周期
                if cycle >= self.metrics.cycles_planned:
                    break

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

                    # v24: 存储最新行情用于 cooldown market fingerprint（含昨收参考价）
                    for nq in norms:
                        self._latest_market[nq.symbol] = {
                            "price": nq.price,
                            "volume": getattr(nq, 'volume', 0),
                            "reference_price": getattr(nq, 'previous_close', 0),
                        }

                    for nq in norms:
                        event, quarantined, qreason = normalized_quote_to_event(nq)

                        # v14: 注入安全上下文（EventRecord 首次持久化时写入，不可变）
                        event.payload.data["safety"] = {
                            "environment": "shadow",
                            "execution_allowed": False,
                            "tags": [
                                "SHADOW_ONLY",
                                "NOT_FOR_EXECUTION",
                                "ACCOUNT_CONTEXT_FIXTURE",
                                "ACCOUNT_CONTEXT_STALE",
                            ],
                        }
                        event.payload.data["account_context"] = {
                            "mode": "FIXTURE",
                            "stale": True,
                            "account_snapshot_id": self._account_snapshot_id,
                            "account_snapshot_id_full": self._account_snapshot_id,
                        }

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

                    # ── P1: 跨事件 cooldown 检查 ──
                    cycle_mono = _time.monotonic()
                    for sig in signals:
                        mkt = self._latest_market.get(sig.symbol, {})
                        fingerprint = compute_market_fingerprint(
                            sig.event_id or "",
                            mkt.get("price", 0),
                            mkt.get("volume", 0),
                            reference_price=mkt.get("reference_price", 0),
                        )
                        if self.cooldown.should_suppress(
                            symbol=sig.symbol,
                            effective_action=sig.effective_trade_action,
                            current_mono=cycle_mono,
                            strategy_id=self._strategy_id,
                            strategy_version=self._strategy_version,
                            strategy_config_hash=self._strategy_config_hash,
                            signal_rule_version=self._strategy_version,
                            account_snapshot_id_full=self._account_snapshot_id,
                            environment="shadow",
                            market_fingerprint=fingerprint,
                        ):
                            continue  # cooldown 窗口内已有相同语义信号
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
            if not self.metrics.run_terminated_early:
                self.metrics.run_completed = False
                self.metrics.run_terminated_early = True
                self.metrics.termination_type = "USER_INTERRUPT"
                self.metrics.termination_reason = "user_interrupt"
                if hasattr(self, '_run_start_mono'):
                    self.metrics.actual_duration_ms = (
                        _time.monotonic() - self._run_start_mono
                    ) * 1000

        # 收尾
        # 不覆盖 cycles_completed — 已在循环中逐周期计入
        self.metrics.cycles_planned = max(1, self.duration // self.interval)

        # v11: 调度方程 reconciliation — 严格约束
        # 约束 1: 0 ≤ started ≤ planned（违反 = 审计失败，不静默修正）
        if self.metrics.cycles_started > self.metrics.cycles_planned:
            self.metrics.violations.append(
                f"SCHEDULING_OVERSHOOT: started={self.metrics.cycles_started} "
                f"> planned={self.metrics.cycles_planned} — "
                f"周期边界守卫失效"
            )
            if self.metrics.status == "COMPLETED":
                self.metrics.status = "AUDIT_FAILED"

        # 约束 2: planned = started + skipped + not_due + cancelled_auto_stop
        # not_due_cycles ≥ 0，仅计算不足 planned 的部分
        accounted = (self.metrics.cycles_started + self.metrics.cycles_skipped
                     + self.metrics.not_due_cycles
                     + self.metrics.cancelled_cycles_auto_stop)
        if accounted < self.metrics.cycles_planned:
            self.metrics.not_due_cycles = max(
                0, self.metrics.cycles_planned - accounted
            )
        self.metrics.ended_at = datetime.now(tz=CST).isoformat(timespec="seconds")
        self.metrics.duration_seconds = _time.monotonic() - start_mono
        if self.metrics.status == "RUNNING":
            self.metrics.status = "COMPLETED"

        # v10: 运行级终止元数据
        if not self.metrics.run_terminated_early:
            self.metrics.run_completed = True
            self.metrics.termination_type = "NORMAL"
            if hasattr(self, '_run_start_mono'):
                self.metrics.actual_duration_ms = (
                    _time.monotonic() - self._run_start_mono
                ) * 1000

        # P0-2: 收集幂等账本统计
        try:
            ledger_stats = self.ledger.get_stats()
            self.metrics.ledger_claimed = ledger_stats.get("total", 0)
            self.metrics.ledger_completed = ledger_stats.get("COMPLETED", 0)
            self.metrics.ledger_failed_count = ledger_stats.get("FAILED", 0)
            self.metrics.ledger_in_progress = ledger_stats.get("PROCESSING", 0)
            self.metrics.ledger_already_processed = (
                self.idempotent.stats.get("already_processed", 0))
            # v28: 幂等统计 — run-scoped delta + lifetime
            # 修复 SIGNAL_EQ1 混合 run/lifetime 作用域缺陷。
            lifetime_skipped = self.idempotent.stats.get("already_processed", 0)
            t0_skipped = getattr(self, '_idempotent_skipped_at_T0', 0)
            # v29: run delta 不 clamp。T1<T0 时得负值 → SIGNAL_EQ1 失衡 → fail-closed。
            # 不得用 max(0,...) 静默掩盖（v28 曾 clamp 导致 delta=0 掩盖了账本回退）。
            self.metrics.signals_skipped_idempotent_run = (
                lifetime_skipped - t0_skipped)
            self.metrics.signals_skipped_idempotent_lifetime = lifetime_skipped
            # 保留旧字段兼容（= lifetime，标记 deprecated）
            self.metrics.signals_skipped_idempotent = lifetime_skipped
        except Exception:
            self.metrics.report_failed += 1

        # P1: 同步 cooldown tracker 统计到 metrics
        self.metrics.signals_skipped_cooldown = self.cooldown.skipped_count
        # v29: 候选信号总数 = 本次 run 全部候选 = 幂等跳过 + 进入 cooldown 检查的候选。
        # 幂等跳过的候选在 process_events 内部被丢弃、不进入 cooldown，
        # 所以必须加上 skipped_idempotent_run 才是完整 candidate_total_run。
        # (v29.0 曾错误定义为 cooldown.total_checked，在 skipped_idempotent_run>0 时失守)
        self.metrics.signals_candidate_total_run = (
            self.metrics.signals_skipped_idempotent_run
            + self.cooldown.total_checked)
        self.metrics.cooldown_reset_reason = self.cooldown.reset_reason

        # v19: 飞行后 cooldown 上下文一致性验证
        if self._cooldown_context_snapshot:
            final_ctx = self._snapshot_cooldown_context()
            if final_ctx != self._cooldown_context_snapshot:
                changed = [k for k in final_ctx
                           if final_ctx[k] != self._cooldown_context_snapshot[k]]
                violation = (
                    f"cooldown postflight: 上下文在运行期间变更 "
                    f"{','.join(changed)}"
                )
                self.metrics.violations.append(violation)
                logger.error(violation)

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

        # v15: 计算总失败数 — 必须在所有 failure counter 递增之后
        # （ledger stats 异常→report_failed，postflight 异常→safety_guard_failed）
        self.metrics.total_failures = (
            self.metrics.scheduler_failed + self.metrics.session_check_failed
            + self.metrics.fetch_failed + self.metrics.http_failed
            + self.metrics.parse_failed + self.metrics.validation_failed
            + self.metrics.normalization_failed + self.metrics.quarantine_failed
            + self.metrics.event_failed + self.metrics.signal_failed
            + self.metrics.ledger_failed + self.metrics.report_failed
            + self.metrics.safety_guard_failed
        )

        # v27: 综合后飞行不变量与安全审计
        self._run_postflight_audit()

        self._print_report()
        return self.metrics

    def _run_postflight_audit(self):
        """v27: 执行综合 postflight 不变量与安全审计。"""
        prod_before = {}
        prod_after = {}
        if self.guard and self.guard.before:
            prod_before = {
                "sha256": self.guard.before.main.sha256,
                "size": self.guard.before.main.size,
                "inode": self.guard.before.main.inode,
            }
        if self.guard and self.guard.after:
            prod_after = {
                "sha256": self.guard.after.main.sha256,
                "size": self.guard.after.main.size,
                "inode": self.guard.after.main.inode,
            }
            self.metrics.prod_file_hash_after = prod_after

        audit = self.metrics.audit_postflight_invariants(
            prod_guard_before=prod_before,
            prod_guard_after=prod_after,
            shadow_db_path=str(self.shadow_db),
        )
        self.metrics.postflight_audit = audit  # v29: 结构化审计结果供 gate 读取
        self.metrics.print_audit_report(audit)

        # v28: review 在 postflight audit 之后执行，集成审计结果
        review = self.metrics.review_report(audit_result=audit)
        self.metrics.review_result = review  # v29: 结构化 review 结果供 gate 读取
        if not review["ok"]:
            for f in review["findings"]:
                self.metrics.violations.append(f"REVIEW: {f}")
            if self.metrics.status == "COMPLETED":
                self.metrics.status = "AUTO_STOPPED"
            self.metrics.auto_stop_triggered = True
            extra = f"review({review['findings_count']} findings)"
            self.metrics.auto_stop_reason = (
                self.metrics.auto_stop_reason + "; " + extra
            ) if self.metrics.auto_stop_reason else extra

        # 审计失败触发自动停止
        if not audit["all_pass"]:
            self.metrics.violations.append(
                f"POSTFLIGHT_AUDIT_FAILED: {audit['failed']}/{audit['total_checks']} 检查未通过"
            )
            if self.metrics.status == "COMPLETED":
                self.metrics.status = "AUTO_STOPPED"
            self.metrics.auto_stop_triggered = True
            self.metrics.auto_stop_reason = (
                self.metrics.auto_stop_reason + "; "
                + f"postflight_audit({audit['failed']} failed)"
            ) if self.metrics.auto_stop_reason else (
                f"postflight_audit({audit['failed']} failed)"
            )

        return audit

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
        if m.cancelled_cycles_auto_stop:
            print(f"  cancelled_auto_stop={m.cancelled_cycles_auto_stop}")
        sched_audit_1 = m.cycles_planned == (
            m.cycles_started + m.cycles_skipped + m.not_due_cycles
            + m.cancelled_cycles_auto_stop
        )
        sched_audit_2 = m.cycles_started == m.cycles_completed + m.cycles_aborted
        print(f"  审计 planned=started+skipped+not_due+cancelled_auto_stop: "
              f"{m.cycles_planned}={m.cycles_started}+{m.cycles_skipped}+"
              f"{m.not_due_cycles}+{m.cancelled_cycles_auto_stop} "
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

        # ── P1 cooldown ──
        if m.cooldown_enabled:
            print(f"\n── P1 跨事件 cooldown ──")
            print(f"  策略: {m.cooldown_policy_version}  "
                  f"窗口: {B2_COOLDOWN_WINDOW_SEC}s  "
                  f"启用: {'✅' if m.cooldown_enabled else '❌'}")
            print(f"  作用域: {m.cooldown_scope}  "
                  f"键: ({m.cooldown_key_fields})")
            print(f"  检查总数: {self.cooldown.total_checked}  "
                  f"跳过(cooldown): {m.signals_skipped_cooldown}")

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
        """保存结构化 JSON 报告（v10: schema 版本 + 验证）。

        报告始终为 JSON 对象（不以 repr 字符串形式输出）。
        """
        report_path = self.shadow_dir / f"{self.metrics.run_id}_report.json"

        # v10: 显式构造报告 dict，不依赖 asdict 的隐式行为
        data = self.metrics.to_report_dict()

        # P0-4: 保留原始数组以便重算分位数
        for key in list(data.keys()):
            if key.endswith("_times_ms") or key.endswith("_ms_values"):
                vals = data[key]
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

        # v10: 审计方程验证标记
        planned = data.get("cycles_planned", 0)
        started = data.get("cycles_started", 0)
        skipped = data.get("cycles_skipped", 0)
        not_due = data.get("not_due_cycles", 0)
        cancelled = data.get("cancelled_cycles_auto_stop", 0)
        completed = data.get("cycles_completed", 0)
        aborted = data.get("cycles_aborted", 0)
        data["_audit_scheduling_v10"] = {
            "equation_1": f"planned={planned} == started({started}) + skipped({skipped}) + not_due({not_due}) + cancelled({cancelled})",
            "equation_1_pass": planned == started + skipped + not_due + cancelled,
            "equation_2": f"started={started} == completed({completed}) + aborted({aborted})",
            "equation_2_pass": started == completed + aborted,
        }

        # v11: 防御性验证 — 确保输出是 JSON 对象，不是 repr 字符串
        json_text = json.dumps(data, ensure_ascii=False, indent=2, default=str)

        # 验证输出的第一非空字符是 '{'（对象），不是 '"'（字符串 repr）
        stripped = json_text.lstrip()
        if stripped.startswith('"'):
            logger.error(
                f"CRITICAL: 报告序列化异常 — 输出为字符串 repr 而非 JSON 对象"
            )
            self.metrics.report_failed += 1
            self.metrics.violations.append(
                "REPORT_SERIALIZATION_FAILED: output is repr string, not JSON object"
            )
            # 回退: 用强制 dict 包装再序列化
            fallback = {
                "schema_version": B2_REPORT_SCHEMA_VERSION,
                "error": "report_serialization_fallback",
                "run_id": self.metrics.run_id,
                "status": "REPORT_WRITE_FAILED",
                "raw_repr_preview": json_text[:500],
            }
            json_text = json.dumps(fallback, ensure_ascii=False, indent=2)

        # v11: 原子写入 — 先写临时文件，再 rename
        # dashboard 不会读到半写 JSON
        import tempfile
        tmp_fd, tmp_path = tempfile.mkstemp(
            suffix=".json",
            prefix=f"{self.metrics.run_id}_",
            dir=str(self.shadow_dir),
        )
        try:
            os.write(tmp_fd, json_text.encode("utf-8"))
            os.fsync(tmp_fd)
            os.close(tmp_fd)
            os.replace(tmp_path, str(report_path))
        except Exception:
            os.close(tmp_fd)
            Path(tmp_path).unlink(missing_ok=True)
            raise

        logger.info(f"B2 报告已保存: {report_path} "
                     f"(schema={B2_REPORT_SCHEMA_VERSION}, "
                     f"size={len(json_text)} bytes)")
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
