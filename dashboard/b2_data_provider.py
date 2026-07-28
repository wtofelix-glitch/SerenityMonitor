"""
UI-P0: B2 离线历史报告数据提供器。

职责:
  · 只读取配置的报告根目录
  · realpath 防路径穿越和符号链接逃逸
  · 限制文件类型和大小
  · JSON 解析, schema 识别
  · 返回统一 ViewModel, 不返回原始业务对象
  · 独立复算八组审计方程

不依赖:
  · Dash / Flask / Plotly
  · B2Runner / SignalDesk / EventStore
  · SQLite
  · 网络 / push / trade adapter
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path


DEFAULT_MAX_FILE_SIZE = 50 * 1024 * 1024
DEFAULT_STALE_SECONDS = 300
SUPPORTED_SCHEMAS = frozenset({"b2-1.0", "b2-1.1"})
# Legacy aliases — normalized to supported schemas
_SCHEMA_ALIASES = {
    "b2-report/1.0": "b2-1.0",
    "b2-report/1.0-implicit": "b2-1.0",
}


class ReportStatus(Enum):
    OK = auto()
    NO_DATA = auto()
    INVALID_JSON = auto()
    UNSUPPORTED_SCHEMA = auto()
    STALE = auto()
    ACCESS_DENIED = auto()
    FILE_TOO_LARGE = auto()


@dataclass
class AuditEquation:
    """审计方程 — reported_passed 可为 None 表示报告未包含此检查。"""
    name: str = ""
    expression: str = ""
    reported_passed: bool | None = None
    recalculated_passed: bool = True
    operands: dict = field(default_factory=dict)

    @property
    def mismatch(self) -> bool:
        """reported=None 时不产生 mismatch（无可比较对象）。"""
        if self.reported_passed is None:
            return False
        return self.reported_passed != self.recalculated_passed

    @property
    def comparison(self) -> str:
        """UNKNOWN | MATCH | MISMATCH — 报告值与复算值的比较状态。"""
        if self.reported_passed is None:
            return "UNKNOWN"
        if self.reported_passed == self.recalculated_passed:
            return "MATCH"
        return "MISMATCH"

    @property
    def primary_passed(self) -> bool:
        """主状态 = 复算结果。UNKNOWN 时仍以复算为准。"""
        return self.recalculated_passed


@dataclass
class TimingInvariant:
    """全周期计时不变量: 每个 cycle_duration_ms >= http_duration_ms。"""
    checked_cycles: int = 0
    violations: int = 0
    status: str = "PASS"  # PASS | FAIL
    violating_sequences: list = field(default_factory=list)


@dataclass
class B2DashboardViewModel:
    report_status: ReportStatus = ReportStatus.NO_DATA
    report_path: str = ""
    report_size: int = 0
    report_mtime: float = 0.0
    source_commit: str = ""
    source_tag: str = ""

    # ── v14: schema 兼容性 ──
    source_schema: str = ""
    compatibility_mode: bool = False
    missing_lineage_fields: list = field(default_factory=list)

    run_id: str = ""
    run_status: str = "UNKNOWN"
    started_at: str = ""
    ended_at: str = ""
    duration_seconds: float = 0
    environment: str = "shadow"
    market_session: str = ""

    cycles_planned: int = 0
    cycles_started: int = 0
    cycles_completed: int = 0
    cycles_aborted: int = 0
    cycles_skipped: int = 0
    not_due_cycles: int = 0

    raw_received: int = 0
    normalized_accepted: int = 0
    normalized_rejected: int = 0
    quarantined: int = 0

    events_created: int = 0
    events_deduplicated: int = 0
    events_not_triggered: int = 0
    event_processing_failed: int = 0

    candidate_ACTION: int = 0
    effective_ACTION: int = 0
    ACTION_downgraded: int = 0
    ACTION_rejected: int = 0
    signals_total: int = 0
    signals_created_unique: int = 0
    signals_skipped_idempotent: int = 0
    duplicate_signals_created: int = 0
    signals_skipped_cooldown: int = 0
    cooldown_enabled: bool = False
    cooldown_policy_version: str = ""

    # ── v14: signal storm ──
    signal_storm_detected: bool = False
    cross_event_repeated_recommendations: int = 0
    signal_storm_per_symbol: dict = field(default_factory=dict)

    # ── v14: 处理效率 ──
    historical_reprocessing_attempts: int = 0

    ledger_claimed: int = 0
    ledger_completed: int = 0
    ledger_completed_with_signal: int = 0
    ledger_completed_no_signal: int = 0
    ledger_failed: int = 0
    ledger_in_progress: int = 0
    ledger_already_processed: int = 0

    http_p50_ms: float = 0.0
    http_p95_ms: float = 0.0
    http_max_ms: float = 0.0
    cycle_p50_ms: float = 0.0
    cycle_p95_ms: float = 0.0
    cycle_max_ms: float = 0.0
    schedule_delay_p50_ms: float = 0.0

    real_push_count: int = 0       # deprecated — use real_pushes
    real_trade_count: int = 0      # deprecated — use real_trades
    real_pushes: int = 0            # v14 canonical
    real_trades: int = 0            # v14 canonical
    account_modifications: int = 0
    production_file_changes: bool = False
    missing_safety_tags: int = 0
    terminated_early: bool = False   # v14 canonical
    run_terminated_early: bool = False  # deprecated

    # ── v14: lineage ──
    lineage_status: str = "UNKNOWN"  # PARTIAL | FULL
    lineage_fields_present: dict = field(default_factory=dict)

    total_failures: int = 0
    fetch_failed: int = 0
    http_failed: int = 0
    parse_failed: int = 0
    validation_failed: int = 0
    normalization_failed: int = 0
    quarantine_failed: int = 0
    event_failed: int = 0
    signal_failed: int = 0
    ledger_failed_count: int = 0
    report_failed: int = 0
    scheduler_failed: int = 0
    session_check_failed: int = 0
    safety_guard_failed: int = 0

    audit_equations: list = field(default_factory=list)
    timing_invariant: TimingInvariant = field(default_factory=TimingInvariant)
    cycle_count: int = 0
    cycle_records_summary: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    raw_json_hash: str = ""

    @property
    def is_live(self) -> bool:
        return self.report_status == ReportStatus.OK

    @property
    def all_audit_passed(self) -> bool:
        return all(eq.recalculated_passed for eq in self.audit_equations) if self.audit_equations else False

    @property
    def has_any_mismatch(self) -> bool:
        return any(eq.mismatch for eq in self.audit_equations)


class B2ReportProvider:
    """B2 离线报告读取、校验、转换。只读, 不连接 DB。"""

    def __init__(self, report_root: str = "",
                 max_file_size: int = DEFAULT_MAX_FILE_SIZE,
                 stale_seconds: int = DEFAULT_STALE_SECONDS):
        self.report_root = Path(report_root).resolve() if report_root else None
        self.max_file_size = max_file_size
        # Defensive: ensure stale_seconds is always a usable int.
        # None / negative / non-int coerced to DEFAULT to prevent
        # accidental disabling of production staleness checks.
        try:
            self.stale_seconds = int(stale_seconds)
        except (TypeError, ValueError):
            self.stale_seconds = DEFAULT_STALE_SECONDS

    def _resolve_safe(self, report_path: str) -> tuple:
        if self.report_root is None:
            return None, ReportStatus.ACCESS_DENIED
        # Resolve relative to report_root, then realpath to catch traversal
        resolved = (self.report_root / report_path).resolve()
        try:
            resolved.relative_to(self.report_root)
        except ValueError:
            return None, ReportStatus.ACCESS_DENIED
        if resolved.suffix.lower() != ".json":
            return None, ReportStatus.ACCESS_DENIED
        return resolved, None

    def load(self, report_path: str) -> B2DashboardViewModel:
        if not report_path:
            return self._error_vm(ReportStatus.NO_DATA, report_path)
        resolved, err = self._resolve_safe(report_path)
        if err:
            return self._error_vm(err, report_path)
        if not resolved.exists():
            return self._error_vm(ReportStatus.NO_DATA, report_path)
        try:
            size = resolved.stat().st_size
        except OSError:
            return self._error_vm(ReportStatus.ACCESS_DENIED, report_path)
        if size > self.max_file_size:
            return self._error_vm(ReportStatus.FILE_TOO_LARGE, report_path,
                                  warnings=[f"文件 {size}B > 上限 {self.max_file_size}B"])
        try:
            raw = resolved.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return self._error_vm(ReportStatus.ACCESS_DENIED, report_path)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            return self._error_vm(ReportStatus.INVALID_JSON, report_path, warnings=[str(exc)])
        if not isinstance(data, dict):
            return self._error_vm(ReportStatus.INVALID_JSON, report_path,
                                  warnings=["顶层不是 JSON 对象"])

        schema = data.get("schema_version", data.get("_schema", ""))
        # Normalize legacy aliases
        schema = _SCHEMA_ALIASES.get(schema, schema)
        if not schema and "run_id" in data and "cycles_planned" in data:
            schema = "b2-1.0"
        if schema not in SUPPORTED_SCHEMAS:
            return self._error_vm(ReportStatus.UNSUPPORTED_SCHEMA, report_path,
                                  warnings=[f"schema '{schema}' 不支持"])

        try:
            mtime = resolved.stat().st_mtime
            age = time.time() - mtime
            if age > self.stale_seconds:
                vm = self._build_vm(data, resolved)
                vm.report_status = ReportStatus.STALE
                vm.warnings.append(f"报告距今 {int(age)}s > {self.stale_seconds}s")
                return vm
        except OSError:
            pass

        return self._build_vm(data, resolved)

    def _build_vm(self, data: dict, path: Path) -> B2DashboardViewModel:
        vm = B2DashboardViewModel(report_status=ReportStatus.OK)
        vm.report_path = str(path)
        try:
            st = path.stat()
            vm.report_size = st.st_size
            vm.report_mtime = st.st_mtime
        except OSError:
            pass

        schema = data.get("schema_version", data.get("_schema", ""))
        vm.source_schema = schema
        vm.compatibility_mode = (schema == "b2-1.0")

        def _ci(key, default=0):
            """Canonical int — 先取 v14 canonical 名，再退到 deprecated。"""
            return int(data.get(key, default))
        def _cf(key, default=0.0):
            return float(data.get(key, default))
        def _cs(key, default=""):
            return str(data.get(key, default))

        vm.run_id = _cs("run_id")
        vm.run_status = _cs("status")
        vm.started_at = _cs("started_at")
        vm.ended_at = _cs("ended_at")
        vm.duration_seconds = _cf("duration_seconds")
        vm.source_commit = _cs("source_commit", data.get("_commit", ""))
        vm.source_tag = _cs("source_tag", data.get("_tag", ""))
        vm.market_session = _cs("market_session", data.get("_session", ""))
        vm.environment = _cs("environment", "shadow")

        vm.cycles_planned = _ci("cycles_planned")
        vm.cycles_completed = _ci("cycles_completed")
        vm.cycles_skipped = _ci("cycles_skipped")
        cycles_failed = _ci("cycles_failed")
        vm.cycles_started = _ci("cycles_started", vm.cycles_completed + cycles_failed)
        vm.cycles_aborted = cycles_failed if cycles_failed else (vm.cycles_started - vm.cycles_completed)
        vm.not_due_cycles = _ci("not_due_cycles")

        vm.raw_received = _ci("raw_received")
        vm.normalized_accepted = _ci("normalized_accepted")
        vm.normalized_rejected = _ci("normalized_rejected")
        vm.quarantined = _ci("quarantined")

        vm.events_created = _ci("events_created")
        vm.events_deduplicated = _ci("events_deduplicated")
        vm.events_not_triggered = _ci("events_not_triggered",
                                     vm.normalized_accepted - vm.events_created - vm.events_deduplicated)
        vm.event_processing_failed = _ci("event_processing_failed")

        vm.candidate_ACTION = _ci("candidate_ACTION")
        vm.effective_ACTION = _ci("effective_ACTION")
        vm.ACTION_downgraded = _ci("ACTION_downgraded")
        vm.ACTION_rejected = _ci("ACTION_rejected")
        vm.signals_total = _ci("signals_total")
        vm.signals_created_unique = _ci("signals_created_unique")
        vm.signals_skipped_idempotent = _ci("signals_skipped_idempotent")

        # ── v14: canonical safety fields with deprecated fallback ──
        vm.real_pushes = _ci("real_pushes", data.get("real_push_count", 0))
        vm.real_trades = _ci("real_trades", data.get("real_trade_count", 0))
        vm.real_push_count = _ci("real_push_count", vm.real_pushes)
        vm.real_trade_count = _ci("real_trade_count", vm.real_trades)
        vm.account_modifications = _ci("account_modifications")
        vm.terminated_early = data.get("terminated_early",
                                       data.get("run_terminated_early", False))
        vm.run_terminated_early = data.get("run_terminated_early",
                                           vm.terminated_early)

        # ── v14: cooldown / duplicate ──
        vm.duplicate_signals_created = _ci("duplicate_signals_created")
        vm.signals_skipped_cooldown = _ci("signals_skipped_cooldown")
        vm.cooldown_enabled = bool(data.get("cooldown_enabled", False))
        vm.cooldown_policy_version = _cs("cooldown_policy_version")
        vm.historical_reprocessing_attempts = _ci("ledger_already_processed",
                                                   data.get("historical_reprocessing_attempts", 0))
        vm.production_file_changes = bool(data.get("production_file_changes",
                                                   data.get("prod_file_changed", False)))
        vm.missing_safety_tags = _ci("missing_safety_tags")

        # ── v14: signal storm computation from signal_details ──
        signal_details = data.get("signal_details", [])
        if signal_details:
            from collections import Counter
            sym_act = Counter((s.get("symbol", "?"), s.get("candidate_action", "?"))
                            for s in signal_details)
            vm.signal_storm_per_symbol = {
                f"{sym}:{act}": count for (sym, act), count in sym_act.most_common()
            }
            repeats = sum(c - 1 for c in sym_act.values())
            vm.cross_event_repeated_recommendations = repeats
            vm.signal_storm_detected = repeats > 0

        # ── v14: lineage completeness check ──
        if signal_details:
            lineage_keys = ["strategy_id", "strategy_version", "strategy_config_hash",
                          "account_snapshot_id", "signal_rule_version"]
            present = {}
            for key in lineage_keys:
                count = sum(1 for s in signal_details if s.get(key))
                present[key] = f"{count}/{len(signal_details)}"
            vm.lineage_fields_present = present
            all_full = all(v.startswith(f"{len(signal_details)}/") for v in present.values())
            any_present = any(int(v.split("/")[0]) > 0 for v in present.values())
            if all_full:
                vm.lineage_status = "FULL"
            elif any_present:
                vm.lineage_status = "PARTIAL"
            else:
                vm.lineage_status = "MISSING"
            if vm.lineage_status != "FULL":
                vm.missing_lineage_fields = [
                    k for k in lineage_keys
                    if present.get(k, "0/0").startswith("0/")
                ]

        # ── 账本 ──
        vm.ledger_claimed = _ci("ledger_claimed")
        vm.ledger_completed = _ci("ledger_completed")
        vm.ledger_completed_no_signal = _ci("ledger_completed_no_signal")
        vm.ledger_completed_with_signal = _ci("ledger_completed_with_signal",
                                              vm.ledger_completed - vm.ledger_completed_no_signal)
        vm.ledger_failed = _ci("ledger_failed_count", data.get("ledger_failed", 0))
        vm.ledger_in_progress = _ci("ledger_in_progress")
        vm.ledger_already_processed = _ci("ledger_already_processed")

        # ── 延迟 ──
        vm.http_p50_ms = _cf("http_response_times_ms_p50", data.get("http_p50_ms", 0))
        vm.http_p95_ms = _cf("http_response_times_ms_p95", data.get("http_p95_ms", 0))
        vm.http_max_ms = _cf("http_response_times_ms_max", data.get("http_max_ms", 0))
        vm.cycle_p50_ms = _cf("cycle_times_ms_p50", data.get("cycle_p50_ms", 0))
        vm.cycle_p95_ms = _cf("cycle_times_ms_p95", data.get("cycle_p95_ms", 0))
        vm.cycle_max_ms = _cf("cycle_times_ms_max", data.get("cycle_max_ms", 0))
        vm.schedule_delay_p50_ms = _cf("schedule_delay_ms_values_p50", data.get("schedule_delay_p50_ms", 0))

        vm.fetch_failed = _ci("fetch_failed", data.get("http_failed", 0))
        vm.http_failed = _ci("http_failed")
        vm.parse_failed = _ci("parse_failed")
        vm.validation_failed = _ci("validation_failed")
        vm.normalization_failed = _ci("normalization_failed")
        vm.quarantine_failed = _ci("quarantine_failed")
        vm.event_failed = _ci("event_failed")
        vm.signal_failed = _ci("signal_failed")
        vm.ledger_failed_count = _ci("ledger_failed_count", data.get("ledger_failed", 0))
        vm.report_failed = _ci("report_failed")
        vm.scheduler_failed = _ci("scheduler_failed")
        vm.session_check_failed = _ci("session_check_failed")
        vm.safety_guard_failed = _ci("safety_guard_failed")
        vm.total_failures = _ci("total_failures", sum([
            vm.fetch_failed, vm.http_failed, vm.parse_failed, vm.validation_failed,
            vm.normalization_failed, vm.quarantine_failed, vm.event_failed,
            vm.signal_failed, vm.ledger_failed_count, vm.report_failed,
            vm.scheduler_failed, vm.session_check_failed, vm.safety_guard_failed]))

        cycle_records = data.get("cycle_records", data.get("cycle_records_summary", []))
        vm.cycle_count = len(cycle_records)
        for cr in cycle_records[:20]:
            vm.cycle_records_summary.append({
                "seq": cr.get("cycle_sequence", cr.get("seq", 0)),
                "status": cr.get("status", ""),
                "http_ms": cr.get("http_duration_ms", cr.get("http_ms", 0)),
                "cycle_ms": cr.get("cycle_duration_ms", cr.get("cycle_ms", 0)),
                "signals": cr.get("signals_created", cr.get("signals", 0)),
                "failure": cr.get("primary_failure_type", cr.get("failure", "")),
            })

        vm.audit_equations = self._compute_equations(vm, data)
        vm.raw_json_hash = hashlib.sha256(
            json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]

        return vm

    def _compute_equations(self, vm: B2DashboardViewModel, data: dict) -> list:
        eqs = []

        def _add(name, expr, reported_ok, recalc, ops):
            """reported_ok=None → reported_passed=None (UNKNOWN, 不产生 mismatch)。"""
            rp = None if reported_ok is None else bool(reported_ok)
            eqs.append(AuditEquation(
                name=name, expression=expr,
                reported_passed=rp,
                recalculated_passed=recalc,
                operands=ops))

        # 8 arithmetic audit equations — reported_passed=None if key missing
        _add("scheduling", "planned = started + skipped + not_due",
             data.get("audit_scheduling_ok"),
             vm.cycles_planned == vm.cycles_started + vm.cycles_skipped + vm.not_due_cycles,
             {"planned": vm.cycles_planned, "started": vm.cycles_started,
              "skipped": vm.cycles_skipped, "not_due": vm.not_due_cycles})

        _add("started", "started = completed + aborted",
             data.get("audit_started_ok"),
             vm.cycles_started == vm.cycles_completed + vm.cycles_aborted,
             {"started": vm.cycles_started, "completed": vm.cycles_completed,
              "aborted": vm.cycles_aborted})

        _add("data", "raw = accepted + rejected + quarantined",
             data.get("audit_data_ok"),
             vm.raw_received == vm.normalized_accepted + vm.normalized_rejected + vm.quarantined,
             {"raw": vm.raw_received, "accepted": vm.normalized_accepted,
              "rejected": vm.normalized_rejected, "quarantined": vm.quarantined})

        _add("event", "accepted = created + dedup + not_triggered + proc_failed",
             data.get("audit_event_ok"),
             vm.normalized_accepted == (vm.events_created + vm.events_deduplicated +
                                        vm.events_not_triggered + vm.event_processing_failed),
             {"accepted": vm.normalized_accepted, "created": vm.events_created,
              "dedup": vm.events_deduplicated, "not_triggered": vm.events_not_triggered,
              "proc_failed": vm.event_processing_failed})

        _add("ACTION", "candidate = effective + downgraded + rejected",
             data.get("audit_action_ok"),
             vm.candidate_ACTION == vm.effective_ACTION + vm.ACTION_downgraded + vm.ACTION_rejected,
             {"candidate": vm.candidate_ACTION, "effective": vm.effective_ACTION,
              "downgraded": vm.ACTION_downgraded, "rejected": vm.ACTION_rejected})

        _add("ledger", "claimed = completed + failed + in_progress",
             data.get("audit_ledger_ok"),
             vm.ledger_claimed == vm.ledger_completed + vm.ledger_failed + vm.ledger_in_progress,
             {"claimed": vm.ledger_claimed, "completed": vm.ledger_completed,
              "failed": vm.ledger_failed, "in_progress": vm.ledger_in_progress})

        _add("COMPLETED", "COMPLETED = WITH_SIGNAL + NO_SIGNAL",
             data.get("audit_completed_breakdown_ok"),
             vm.ledger_completed == vm.ledger_completed_with_signal + vm.ledger_completed_no_signal,
             {"COMPLETED": vm.ledger_completed,
              "WITH_SIGNAL": vm.ledger_completed_with_signal,
              "NO_SIGNAL": vm.ledger_completed_no_signal})

        fsum = (vm.fetch_failed + vm.http_failed + vm.parse_failed + vm.validation_failed +
                vm.normalization_failed + vm.quarantine_failed + vm.event_failed +
                vm.signal_failed + vm.ledger_failed_count + vm.report_failed +
                vm.scheduler_failed + vm.session_check_failed + vm.safety_guard_failed)
        _add("failure_sum", "total_failures = sum(failure_types)",
             data.get("audit_failure_sum_ok"),
             vm.total_failures == fsum,
             {"total": vm.total_failures, "sum_of_types": fsum})

        # 9th check: cross-cycle timing invariant
        cycle_records = data.get("cycle_records", data.get("cycle_records_summary", []))
        checked = 0
        violating_seqs: list = []
        for cr in cycle_records:
            http_ms = cr.get("http_duration_ms", cr.get("http_ms", 0))
            cycle_ms = cr.get("cycle_duration_ms", cr.get("cycle_ms", 0))
            checked += 1
            if cycle_ms < http_ms:
                violating_seqs.append(cr.get("cycle_sequence", cr.get("seq", 0)))
        vm.timing_invariant = TimingInvariant(
            checked_cycles=checked,
            violations=len(violating_seqs),
            status="PASS" if len(violating_seqs) == 0 else "FAIL",
            violating_sequences=violating_seqs,
        )

        return eqs

    def _error_vm(self, status: ReportStatus, path: str,
                  warnings: list | None = None) -> B2DashboardViewModel:
        vm = B2DashboardViewModel()
        vm.report_status = status
        vm.report_path = str(path)
        vm.warnings = warnings or []
        return vm
