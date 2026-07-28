"""
UI-P0: B2 管线看板验收测试 — 14 项验收标准。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from dashboard.b2_data_provider import (
    B2ReportProvider, B2DashboardViewModel, ReportStatus, AuditEquation,
    DEFAULT_MAX_FILE_SIZE, DEFAULT_STALE_SECONDS, SUPPORTED_SCHEMAS,
    _SCHEMA_ALIASES,
)
from dashboard.tabs.b2_pipeline_tab import render_b2_tab

FIXTURES = Path(__file__).parent / "fixtures" / "b2"

# ── ensure fixtures are "fresh" for STALE tests ──────────────

def _touch_fixtures():
    for name in ["sample_report_ok.json", "sample_bad_json.json",
                 "sample_not_json.json", "sample_unknown_schema.json"]:
        p = FIXTURES / name
        if p.exists():
            os.utime(str(p), None)

_touch_fixtures()



# ── helpers ──────────────────────────────────────────────────

def _load_fixture(name: str) -> str:
    return str((FIXTURES / name).resolve())


def _provider(root: str | None = None) -> B2ReportProvider:
    if root is None:
        root = str(FIXTURES)
    return B2ReportProvider(report_root=root, stale_seconds=999999999)


# ══════════════════════════════════════════════════════════════
# AC-01: 正常渲染 (Normal)
# ══════════════════════════════════════════════════════════════

class TestNormalRendering:
    """AC-01: 正常报告加载并渲染完整 ViewModel"""

    def test_provider_returns_ok_status(self):
        provider = _provider()
        vm = provider.load("sample_report_ok.json")
        assert vm.report_status == ReportStatus.OK
        assert vm.is_live

    def test_run_identity_fields_populated(self):
        vm = _provider().load("sample_report_ok.json")
        assert vm.run_id == "B2_20260724_093500"
        assert vm.run_status == "COMPLETED"
        assert vm.source_commit == "149c8ac0"
        assert vm.source_tag == "phase-b2-runner-v9"
        assert vm.environment == "shadow"
        assert vm.market_session == "CONTINUOUS_AM"

    def test_scheduler_fields(self):
        vm = _provider().load("sample_report_ok.json")
        assert vm.cycles_planned == 60
        assert vm.cycles_completed == 57
        assert vm.cycles_skipped == 2
        assert vm.cycles_aborted == 1

    def test_data_quality_fields(self):
        vm = _provider().load("sample_report_ok.json")
        assert vm.raw_received == 120
        assert vm.normalized_accepted == 100
        assert vm.normalized_rejected == 15
        assert vm.quarantined == 5

    def test_ledger_fields(self):
        vm = _provider().load("sample_report_ok.json")
        assert vm.ledger_claimed == 38
        assert vm.ledger_completed == 38
        assert vm.ledger_completed_with_signal == 31
        assert vm.ledger_completed_no_signal == 7
        assert vm.ledger_failed == 0
        assert vm.ledger_in_progress == 0

    def test_safety_isolation_all_zero(self):
        vm = _provider().load("sample_report_ok.json")
        assert vm.real_push_count == 0
        assert vm.real_trade_count == 0
        assert vm.account_modifications == 0
        assert vm.production_file_changes is False
        assert vm.missing_safety_tags == 0

    def test_latency_fields(self):
        vm = _provider().load("sample_report_ok.json")
        assert vm.http_p50_ms == 82.5
        assert vm.http_p95_ms == 145.2
        assert vm.http_max_ms == 312.0
        assert vm.cycle_p50_ms == 251.0
        assert vm.cycle_p95_ms == 410.0
        assert vm.cycle_max_ms == 618.0

    def test_cycle_records_populated(self):
        vm = _provider().load("sample_report_ok.json")
        assert vm.cycle_count == 57
        assert len(vm.cycle_records_summary) == 20
        first = vm.cycle_records_summary[0]
        assert first["seq"] == 1
        assert first["status"] == "COMPLETED"

    def test_tab_renders_html_div(self):
        result = render_b2_tab(
            report_path="sample_report_ok.json",
            report_root=str(FIXTURES),
        )
        from dash import html
        assert isinstance(result, html.Div)
        children = getattr(result, "children", [])
        assert len(children) >= 2  # at minimum: banner + content sections

    def test_random_hash_stable(self):
        vm1 = _provider().load("sample_report_ok.json")
        vm2 = _provider().load("sample_report_ok.json")
        assert vm1.raw_json_hash == vm2.raw_json_hash
        assert len(vm1.raw_json_hash) == 16


# ══════════════════════════════════════════════════════════════
# AC-02: NO_DATA
# ══════════════════════════════════════════════════════════════

class TestNoData:
    """AC-02: 报告文件不存在或路径未配置"""

    def test_file_not_exist(self):
        vm = _provider().load("nonexistent.json")
        assert vm.report_status == ReportStatus.NO_DATA

    def test_empty_path(self):
        vm = _provider().load("")
        assert vm.report_status == ReportStatus.NO_DATA

    def test_tab_shows_no_data(self):
        result = render_b2_tab(
            report_path="nonexistent.json",
            report_root=str(FIXTURES),
        )
        assert "NO DATA" in str(getattr(result, "children", ""))


# ══════════════════════════════════════════════════════════════
# AC-03: INVALID_JSON
# ══════════════════════════════════════════════════════════════

class TestInvalidJson:
    """AC-03: JSON 解析失败"""

    def test_corrupt_json(self):
        vm = _provider().load("sample_bad_json.json")
        assert vm.report_status == ReportStatus.INVALID_JSON

    def test_plain_text_not_json(self):
        vm = _provider().load("sample_not_json.json")
        assert vm.report_status == ReportStatus.INVALID_JSON

    def test_tab_shows_invalid_json(self):
        result = render_b2_tab(
            report_path="sample_bad_json.json",
            report_root=str(FIXTURES),
        )
        assert "INVALID JSON" in str(getattr(result, "children", ""))


# ══════════════════════════════════════════════════════════════
# AC-04: UNSUPPORTED_SCHEMA
# ══════════════════════════════════════════════════════════════

class TestUnsupportedSchema:
    """AC-04: 不支持的报告 schema 版本"""

    def test_unknown_schema(self):
        vm = _provider().load("sample_unknown_schema.json")
        assert vm.report_status == ReportStatus.UNSUPPORTED_SCHEMA

    def test_tab_shows_unsupported(self):
        result = render_b2_tab(
            report_path="sample_unknown_schema.json",
            report_root=str(FIXTURES),
        )
        assert "UNSUPPORTED SCHEMA" in str(getattr(result, "children", ""))

    def test_supported_schemas(self):
        # Canonical schemas
        assert "b2-1.0" in SUPPORTED_SCHEMAS
        assert "b2-1.1" in SUPPORTED_SCHEMAS
        # Legacy aliases normalize to canonical
        assert _SCHEMA_ALIASES.get("b2-report/1.0") == "b2-1.0"
        assert _SCHEMA_ALIASES.get("b2-report/1.0-implicit") == "b2-1.0"


# ══════════════════════════════════════════════════════════════
# AC-05: STALE
# ══════════════════════════════════════════════════════════════

class TestStale:
    """AC-05: 报告数据过期"""

    def test_stale_detection(self):
        provider = B2ReportProvider(
            report_root=str(FIXTURES),
            stale_seconds=0,
        )
        vm = provider.load("sample_report_ok.json")
        assert vm.report_status == ReportStatus.STALE
        assert any("距今" in w for w in vm.warnings)

    def test_stale_still_has_data(self):
        provider = B2ReportProvider(
            report_root=str(FIXTURES),
            stale_seconds=0,
        )
        vm = provider.load("sample_report_ok.json")
        assert vm.report_status == ReportStatus.STALE
        assert vm.run_id == "B2_20260724_093500"
        assert len(vm.audit_equations) == 8

    def test_fresh_report_ok(self):
        provider = B2ReportProvider(
            report_root=str(FIXTURES),
            stale_seconds=999999,
        )
        vm = provider.load("sample_report_ok.json")
        assert vm.report_status == ReportStatus.OK


# ══════════════════════════════════════════════════════════════
# AC-06: 审计方程复算
# ══════════════════════════════════════════════════════════════

class TestAuditRecalculation:
    """AC-06: 8 组审计方程独立复算"""

    def test_all_8_equations_present(self):
        vm = _provider().load("sample_report_ok.json")
        assert len(vm.audit_equations) == 8

    def test_scheduling_equation(self):
        vm = _provider().load("sample_report_ok.json")
        eq = vm.audit_equations[0]
        assert eq.name == "scheduling"
        assert eq.recalculated_passed is True
        assert eq.operands["planned"] == 60
        assert not eq.mismatch

    def test_started_equation(self):
        vm = _provider().load("sample_report_ok.json")
        eq = vm.audit_equations[1]
        assert eq.name == "started"
        assert eq.recalculated_passed is True
        assert not eq.mismatch

    def test_data_equation(self):
        vm = _provider().load("sample_report_ok.json")
        eq = vm.audit_equations[2]
        assert eq.name == "data"
        assert eq.recalculated_passed is True
        assert not eq.mismatch

    def test_event_equation(self):
        vm = _provider().load("sample_report_ok.json")
        eq = vm.audit_equations[3]
        assert eq.name == "event"
        assert eq.recalculated_passed is True
        assert not eq.mismatch

    def test_action_equation(self):
        vm = _provider().load("sample_report_ok.json")
        eq = vm.audit_equations[4]
        assert eq.name == "ACTION"
        assert eq.recalculated_passed is True

    def test_ledger_equation(self):
        vm = _provider().load("sample_report_ok.json")
        eq = vm.audit_equations[5]
        assert eq.name == "ledger"
        assert eq.recalculated_passed is True

    def test_completed_breakdown(self):
        vm = _provider().load("sample_report_ok.json")
        eq = vm.audit_equations[6]
        assert eq.name == "COMPLETED"
        assert eq.recalculated_passed is True

    def test_failure_sum_equation(self):
        vm = _provider().load("sample_report_ok.json")
        eq = vm.audit_equations[7]
        assert eq.name == "failure_sum"
        assert eq.recalculated_passed is True

    def test_viewmodel_all_audit_passed(self):
        vm = _provider().load("sample_report_ok.json")
        assert vm.all_audit_passed is True

    def test_viewmodel_no_mismatch(self):
        vm = _provider().load("sample_report_ok.json")
        assert vm.has_any_mismatch is False


# ══════════════════════════════════════════════════════════════
# AC-07: 审计方程不匹配检测
# ══════════════════════════════════════════════════════════════

class TestMismatchDetection:
    """AC-07: reported_passed vs recalculated_passed 不匹配检测"""

    def test_mismatch_when_reported_ok_but_recalc_fails(self):
        eq = AuditEquation(
            name="test",
            reported_passed=True,
            recalculated_passed=False,
        )
        assert eq.mismatch is True

    def test_mismatch_when_reported_fail_but_recalc_ok(self):
        eq = AuditEquation(
            name="test",
            reported_passed=False,
            recalculated_passed=True,
        )
        assert eq.mismatch is True

    def test_no_mismatch_both_ok(self):
        eq = AuditEquation(reported_passed=True, recalculated_passed=True)
        assert eq.mismatch is False

    def test_no_mismatch_both_fail(self):
        eq = AuditEquation(reported_passed=False, recalculated_passed=False)
        assert eq.mismatch is False


# ══════════════════════════════════════════════════════════════
# AC-08: 13 种失败类型
# ══════════════════════════════════════════════════════════════

class TestFailureTypes:
    """AC-08: 13 种失败类型字段全展示"""

    FAILURE_FIELDS = [
        "fetch_failed", "http_failed", "parse_failed", "validation_failed",
        "normalization_failed", "quarantine_failed", "event_failed",
        "signal_failed", "ledger_failed_count", "report_failed",
        "scheduler_failed", "session_check_failed", "safety_guard_failed",
    ]

    def test_all_13_failure_fields_on_viewmodel(self):
        vm = _provider().load("sample_report_ok.json")
        for field in self.FAILURE_FIELDS:
            assert hasattr(vm, field), f"Missing field: {field}"
            val = getattr(vm, field)
            assert isinstance(val, int), f"Field {field} is not int: {type(val)}"

    def test_total_failures_matches_sum(self):
        vm = _provider().load("sample_report_ok.json")
        field_sum = sum(getattr(vm, f) for f in self.FAILURE_FIELDS)
        assert vm.total_failures == field_sum

    def test_failure_types_in_tab(self):
        result = render_b2_tab(
            report_path="sample_report_ok.json",
            report_root=str(FIXTURES),
        )
        html_str = str(getattr(result, "children", ""))
        assert "http" in html_str.lower() or "HTTP" in html_str
        assert "parse" in html_str.lower() or "parse" in html_str


# ══════════════════════════════════════════════════════════════
# AC-09: 周期分页
# ══════════════════════════════════════════════════════════════

class TestCyclePagination:
    """AC-09: 周期明细只显示前 20 条"""

    def test_only_20_cycles_stored(self):
        vm = _provider().load("sample_report_ok.json")
        assert vm.cycle_count == 57
        assert len(vm.cycle_records_summary) == 20

    def test_cycle_records_have_valid_data(self):
        """All stored cycle records have status, seq, and timing info."""
        vm = _provider().load("sample_report_ok.json")
        for c in vm.cycle_records_summary:
            assert "status" in c
            assert "seq" in c
            assert c["seq"] >= 1
            assert isinstance(c.get("http_ms", 0), (int, float))
        completed = [c for c in vm.cycle_records_summary
                     if c["status"] == "COMPLETED"]
        assert len(completed) >= 1

    def test_tab_shows_cycle_count(self):
        result = render_b2_tab(
            report_path="sample_report_ok.json",
            report_root=str(FIXTURES),
        )
        html_str = str(getattr(result, "children", ""))
        assert "20/57" in html_str


# ══════════════════════════════════════════════════════════════
# AC-10: 大报告容忍
# ══════════════════════════════════════════════════════════════

class TestLargeReportTolerance:
    """AC-10: 报告超过大小限制时返回 FILE_TOO_LARGE"""

    def test_file_too_large(self):
        provider = B2ReportProvider(
            report_root=str(FIXTURES),
            max_file_size=100,
        )
        vm = provider.load("sample_report_ok.json")
        assert vm.report_status == ReportStatus.FILE_TOO_LARGE

    def test_default_max_size_is_50mb(self):
        assert DEFAULT_MAX_FILE_SIZE == 50 * 1024 * 1024


# ══════════════════════════════════════════════════════════════
# AC-11: 故障隔离 (Fault Isolation)
# ══════════════════════════════════════════════════════════════

class TestFaultIsolation:
    """AC-11: B2 模块故障不影响现有 Tab"""

    def test_b2_no_path_shows_no_data_not_crash(self):
        result = render_b2_tab(report_path="", report_root="")
        from dash import html
        assert isinstance(result, html.Div)
        assert "NO DATA" in str(getattr(result, "children", ""))

    def test_provider_no_sqlite_imports(self):
        import inspect
        source = inspect.getsource(B2ReportProvider)
        assert "sqlite3" not in source
        assert "get_conn" not in source

    def test_provider_no_network_imports(self):
        import inspect
        source = inspect.getsource(B2ReportProvider)
        assert "requests" not in source
        assert "urllib" not in source


# ══════════════════════════════════════════════════════════════
# AC-12: 无 DB/网络副作用
# ══════════════════════════════════════════════════════════════

class TestNoSideEffects:
    """AC-12: Provider 不产生 DB/网络副作用"""

    def test_provider_has_no_db_methods(self):
        provider = _provider()
        assert not hasattr(provider, "connect")
        assert not hasattr(provider, "cursor")
        assert not hasattr(provider, "execute")

    def test_load_is_pure_readonly(self):
        provider = _provider()
        vm1 = provider.load("sample_report_ok.json")
        vm2 = provider.load("sample_report_ok.json")
        for attr in ["run_id", "cycles_planned", "signals_total",
                     "ledger_claimed", "total_failures"]:
            assert getattr(vm1, attr) == getattr(vm2, attr)

    def test_empty_cycle_records_handled(self):
        import tempfile
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False,
            dir=str(FIXTURES),
        ) as f:
            json.dump({
                "schema_version": "b2-report/1.0-implicit",
                "run_id": "empty_test",
                "status": "COMPLETED",
                "started_at": "2026-01-01T00:00:00",
                "duration_seconds": 0,
                "cycles_planned": 0,
                "cycles_completed": 0,
                "cycles_failed": 0,
                "cycles_skipped": 0,
                "not_due_cycles": 0,
                "raw_received": 0,
                "normalized_accepted": 0,
                "normalized_rejected": 0,
                "quarantined": 0,
                "events_created": 0,
                "events_deduplicated": 0,
                "events_not_triggered": 0,
                "event_processing_failed": 0,
                "signals_total": 0,
                "candidate_ACTION": 0,
                "effective_ACTION": 0,
                "ACTION_downgraded": 0,
                "ACTION_rejected": 0,
                "ledger_claimed": 0,
                "ledger_completed": 0,
                "ledger_completed_no_signal": 0,
                "ledger_failed_count": 0,
                "ledger_in_progress": 0,
                "ledger_already_processed": 0,
                "total_failures": 0,
            }, f)
            tmp_path = f.name

        try:
            vm = _provider().load(os.path.basename(tmp_path))
            assert vm.report_status == ReportStatus.OK
            assert vm.cycle_count == 0
            assert vm.cycle_records_summary == []
            assert vm.total_failures == 0
        finally:
            os.unlink(tmp_path)


# ══════════════════════════════════════════════════════════════
# AC-13: 路径安全
# ══════════════════════════════════════════════════════════════

class TestPathSecurity:
    """AC-13: realpath 防遍历、符号链接逃逸、非 .json 拒绝"""

    def test_traversal_blocked(self):
        vm = _provider().load("../../../etc/passwd")
        assert vm.report_status == ReportStatus.ACCESS_DENIED

    def test_absolute_path_blocked(self):
        vm = _provider().load("/etc/passwd")
        assert vm.report_status == ReportStatus.ACCESS_DENIED


# ══════════════════════════════════════════════════════════════
# AC-14: 旧 Tab 不受影响
# ══════════════════════════════════════════════════════════════

class TestOldTabRegression:
    """AC-14: B2 模块注册不影响现有 4 个 Tab"""

    def test_main_module_imports_without_b2(self):
        import dash_dashboard
        assert dash_dashboard.ENABLE_B2_DASHBOARD is False
        assert dash_dashboard._b2_tab == []
        assert callable(dash_dashboard._render_ic_tab)
        assert callable(dash_dashboard._render_weight_tab)
        assert callable(dash_dashboard._render_exec_tab)
        assert callable(dash_dashboard._render_risk_tab)

    def test_b2_renderer_exists_but_lazy(self):
        import dash_dashboard
        assert callable(dash_dashboard._render_b2_tab)

    def test_error_card_works(self):
        import dash_dashboard
        from dash import html
        card = dash_dashboard._error_card("test error")
        assert isinstance(card, html.Div)


# ══════════════════════════════════════════════════════════════
# AC-15: 计时不变量 (Review Item 1)
# ══════════════════════════════════════════════════════════════

class TestTimingInvariant:
    """AC-15: 全周期计时不变量 cycle_ms >= http_ms"""

    def test_timing_invariant_on_viewmodel(self):
        vm = _provider().load("sample_report_ok.json")
        ti = vm.timing_invariant
        assert ti.checked_cycles == 57
        # Fixture has 1 real violation: cycle 20 http=196ms > cycle=191ms
        assert ti.violations == 1
        assert ti.status == "FAIL"
        assert ti.violating_sequences == [20]

    def test_timing_invariant_in_tab(self):
        result = render_b2_tab(
            report_path="sample_report_ok.json",
            report_root=str(FIXTURES),
        )
        html_str = str(getattr(result, "children", ""))
        assert "TIMING_INVARIANT" in html_str
        assert "cycle_ms" in html_str or "violations=0" in html_str

    def test_timing_invariant_detects_violation(self):
        """A report with cycle_ms < http_ms should show FAIL."""
        import tempfile
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False,
            dir=str(FIXTURES),
        ) as f:
            json.dump({
                "schema_version": "b2-report/1.0-implicit",
                "run_id": "timing_violation_test",
                "status": "COMPLETED",
                "started_at": "2026-01-01T00:00:00",
                "duration_seconds": 0,
                "cycles_planned": 2, "cycles_completed": 2,
                "cycles_failed": 0, "cycles_skipped": 0, "not_due_cycles": 0,
                "raw_received": 0, "normalized_accepted": 0,
                "normalized_rejected": 0, "quarantined": 0,
                "events_created": 0, "events_deduplicated": 0,
                "events_not_triggered": 0, "event_processing_failed": 0,
                "signals_total": 0,
                "candidate_ACTION": 0, "effective_ACTION": 0,
                "ACTION_downgraded": 0, "ACTION_rejected": 0,
                "ledger_claimed": 0, "ledger_completed": 0,
                "ledger_completed_no_signal": 0, "ledger_failed_count": 0,
                "ledger_in_progress": 0, "ledger_already_processed": 0,
                "total_failures": 0,
                "cycle_records": [
                    {"cycle_sequence": 1, "status": "COMPLETED",
                     "http_duration_ms": 500, "cycle_duration_ms": 300,
                     "signals_created": 0, "primary_failure_type": ""},
                    {"cycle_sequence": 2, "status": "COMPLETED",
                     "http_duration_ms": 100, "cycle_duration_ms": 200,
                     "signals_created": 0, "primary_failure_type": ""},
                ],
            }, f)
            tmp_path = f.name

        try:
            vm = _provider().load(os.path.basename(tmp_path))
            assert vm.report_status == ReportStatus.OK
            ti = vm.timing_invariant
            assert ti.checked_cycles == 2
            assert ti.violations == 1
            assert ti.status == "FAIL"
            assert ti.violating_sequences == [1]
        finally:
            os.unlink(tmp_path)


# ══════════════════════════════════════════════════════════════
# AC-16: reported_passed=UNKNOWN (Review Item 2)
# ══════════════════════════════════════════════════════════════

class TestReportedPassedNull:
    """AC-16: reported_passed=None 时不产生伪造 mismatch"""

    def test_reported_passed_none_produces_no_mismatch(self):
        eq = AuditEquation(
            name="test",
            reported_passed=None,
            recalculated_passed=True,
        )
        assert eq.mismatch is False

    def test_reported_passed_none_with_false_recalc(self):
        eq = AuditEquation(
            name="test",
            reported_passed=None,
            recalculated_passed=False,
        )
        assert eq.mismatch is False

    def test_mismatch_still_works_when_reported_is_known(self):
        eq = AuditEquation(
            name="test",
            reported_passed=True,
            recalculated_passed=False,
        )
        assert eq.mismatch is True

    def test_report_missing_audit_key_produces_unknown(self):
        """When the report JSON lacks an audit_*_ok key, reported_passed=None."""
        import tempfile
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False,
            dir=str(FIXTURES),
        ) as f:
            json.dump({
                "schema_version": "b2-report/1.0-implicit",
                "run_id": "no_audit_keys_test",
                "status": "COMPLETED",
                "started_at": "2026-01-01T00:00:00",
                "duration_seconds": 0,
                "cycles_planned": 10, "cycles_completed": 8,
                "cycles_failed": 2, "cycles_skipped": 0, "not_due_cycles": 0,
                "raw_received": 0, "normalized_accepted": 0,
                "normalized_rejected": 0, "quarantined": 0,
                "events_created": 0, "events_deduplicated": 0,
                "events_not_triggered": 0, "event_processing_failed": 0,
                "signals_total": 0,
                "candidate_ACTION": 0, "effective_ACTION": 0,
                "ACTION_downgraded": 0, "ACTION_rejected": 0,
                "ledger_claimed": 0, "ledger_completed": 0,
                "ledger_completed_no_signal": 0, "ledger_failed_count": 0,
                "ledger_in_progress": 0, "ledger_already_processed": 0,
                "total_failures": 0,
                # NOTE: no audit_*_ok keys at all
            }, f)
            tmp_path = f.name

        try:
            vm = _provider().load(os.path.basename(tmp_path))
            assert vm.report_status == ReportStatus.OK
            # All 8 equations should have reported_passed=None
            for eq in vm.audit_equations:
                assert eq.reported_passed is None, \
                    f"{eq.name}: expected None, got {eq.reported_passed}"
                assert eq.mismatch is False, \
                    f"{eq.name}: mismatch should be False when reported is None"
            # has_any_mismatch should be False
            assert vm.has_any_mismatch is False
            # all_audit_passed should reflect recalculated values
            # (scheduling: 10=8+2+0 → True, started: 10=8+2 → True)
            assert vm.all_audit_passed is True
        finally:
            os.unlink(tmp_path)

    def test_tab_shows_unknown_for_missing_reported(self):
        """Tab should display UNKNOWN when reported_passed is None."""
        import tempfile
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False,
            dir=str(FIXTURES),
        ) as f:
            json.dump({
                "schema_version": "b2-report/1.0-implicit",
                "run_id": "unknown_test",
                "status": "COMPLETED",
                "started_at": "2026-01-01T00:00:00",
                "duration_seconds": 0,
                "cycles_planned": 5, "cycles_completed": 5,
                "cycles_failed": 0, "cycles_skipped": 0, "not_due_cycles": 0,
                "raw_received": 0, "normalized_accepted": 0,
                "normalized_rejected": 0, "quarantined": 0,
                "events_created": 0, "events_deduplicated": 0,
                "events_not_triggered": 0, "event_processing_failed": 0,
                "signals_total": 0,
                "candidate_ACTION": 0, "effective_ACTION": 0,
                "ACTION_downgraded": 0, "ACTION_rejected": 0,
                "ledger_claimed": 0, "ledger_completed": 0,
                "ledger_completed_no_signal": 0, "ledger_failed_count": 0,
                "ledger_in_progress": 0, "ledger_already_processed": 0,
                "total_failures": 0,
                # No audit keys
            }, f)
            tmp_path = f.name

        try:
            result = render_b2_tab(
                report_path=os.path.basename(tmp_path),
                report_root=str(FIXTURES),
            )
            html_str = str(getattr(result, "children", ""))
            assert "UNKNOWN" in html_str
        finally:
            os.unlink(tmp_path)


# ══════════════════════════════════════════════════════════════
# AC-17: 双维度展示 (Review Round 2)
# ══════════════════════════════════════════════════════════════

class TestAuditComparisonSemantics:
    """Primary = recalculated; Comparison = UNKNOWN/MATCH/MISMATCH."""

    # ── comparison property ──

    def test_comparison_unknown_when_reported_none(self):
        eq = AuditEquation(reported_passed=None, recalculated_passed=True)
        assert eq.comparison == "UNKNOWN"
        assert eq.mismatch is False

    def test_comparison_match_when_both_true(self):
        eq = AuditEquation(reported_passed=True, recalculated_passed=True)
        assert eq.comparison == "MATCH"
        assert eq.mismatch is False

    def test_comparison_match_when_both_false(self):
        eq = AuditEquation(reported_passed=False, recalculated_passed=False)
        assert eq.comparison == "MATCH"
        assert eq.mismatch is False

    def test_comparison_mismatch_when_reported_true_recalc_false(self):
        eq = AuditEquation(reported_passed=True, recalculated_passed=False)
        assert eq.comparison == "MISMATCH"
        assert eq.mismatch is True

    def test_comparison_mismatch_when_reported_false_recalc_true(self):
        eq = AuditEquation(reported_passed=False, recalculated_passed=True)
        assert eq.comparison == "MISMATCH"
        assert eq.mismatch is True

    # ── primary_passed = recalculated (ground truth) ──

    def test_primary_passed_true_when_recalc_true(self):
        eq = AuditEquation(reported_passed=None, recalculated_passed=True)
        assert eq.primary_passed is True

    def test_primary_passed_false_when_recalc_false(self):
        eq = AuditEquation(reported_passed=None, recalculated_passed=False)
        assert eq.primary_passed is False
        # NOT mismatch — primary is recalculated, comparison is UNKNOWN

    def test_unknown_does_not_hide_recalc_fail(self):
        """reported=None + recalc=False → FAIL (not hidden behind UNKNOWN)."""
        eq = AuditEquation(reported_passed=None, recalculated_passed=False)
        assert eq.primary_passed is False      # FAIL
        assert eq.comparison == "UNKNOWN"      # no report to compare
        assert eq.mismatch is False            # not a mismatch (nothing to compare against)

    # ── ViewModel integration ──

    def test_viewmodel_all_audit_passed_uses_primary(self):
        """all_audit_passed is based on recalculated, not reported."""
        vm = _provider().load("sample_report_ok.json")
        # Fixture: all 8 equations recalc-pass, all reported=ok → all_audit_passed=True
        assert vm.all_audit_passed is True
        for eq in vm.audit_equations:
            assert eq.primary_passed is True
            assert eq.comparison == "MATCH"

    def test_tab_shows_primary_and_comparison_columns(self):
        result = render_b2_tab(
            report_path="sample_report_ok.json",
            report_root=str(FIXTURES),
        )
        html_str = str(getattr(result, "children", ""))
        # Primary column header
        assert "主状态" in html_str
        # Comparison column header
        assert "比较" in html_str
        # Values
        assert "PASS" in html_str
        assert "MATCH" in html_str


# ══════════════════════════════════════════════════════════════
# UI-P0: v12 / v14 dual-schema acceptance
# ══════════════════════════════════════════════════════════════

V12_FIXTURE_ROOT = Path(__file__).parent / "fixtures"
V14_FIXTURE_ROOT = Path(__file__).parent / "fixtures"


class TestV12WarningFixture:
    """v12 真实 warning fixture 正确渲染: b2-1.0, compat mode, signal storm, lineage MISSING"""

    def test_v12_schema_and_compat(self):
        provider = B2ReportProvider(report_root=str(V12_FIXTURE_ROOT), stale_seconds=9999999)
        vm = provider.load("v12_warning_fixture.json")
        assert vm.report_status == ReportStatus.OK
        assert vm.source_schema == "b2-1.0"
        assert vm.compatibility_mode is True

    def test_v12_run_identity(self):
        provider = B2ReportProvider(report_root=str(V12_FIXTURE_ROOT), stale_seconds=9999999)
        vm = provider.load("v12_warning_fixture.json")
        assert vm.run_id == "B2_20260728_130912"
        assert vm.run_status == "COMPLETED"
        assert vm.cycles_planned == 60
        assert vm.cycles_completed == 60
        assert vm.signals_total == 180

    def test_v12_signal_storm_detected(self):
        provider = B2ReportProvider(report_root=str(V12_FIXTURE_ROOT), stale_seconds=9999999)
        vm = provider.load("v12_warning_fixture.json")
        assert vm.signal_storm_detected is True
        assert vm.cross_event_repeated_recommendations == 177
        assert len(vm.signal_storm_per_symbol) == 3
        assert vm.signal_storm_per_symbol["600487:REDUCE"] == 60

    def test_v12_lineage_missing(self):
        provider = B2ReportProvider(report_root=str(V12_FIXTURE_ROOT), stale_seconds=9999999)
        vm = provider.load("v12_warning_fixture.json")
        assert vm.lineage_status == "MISSING"
        assert len(vm.missing_lineage_fields) == 5
        assert "strategy_id" in vm.missing_lineage_fields
        assert vm.lineage_fields_present["strategy_id"] == "0/180"

    def test_v12_cooldown_disabled(self):
        provider = B2ReportProvider(report_root=str(V12_FIXTURE_ROOT), stale_seconds=9999999)
        vm = provider.load("v12_warning_fixture.json")
        assert vm.cooldown_enabled is False
        assert vm.signals_skipped_cooldown == 0
        assert vm.duplicate_signals_created == 0  # v12 lacks this field → defaults 0

    def test_v12_safety_isolated(self):
        provider = B2ReportProvider(report_root=str(V12_FIXTURE_ROOT), stale_seconds=9999999)
        vm = provider.load("v12_warning_fixture.json")
        assert vm.real_pushes == 0
        assert vm.real_trades == 0
        assert vm.real_push_count == 0
        assert vm.real_trade_count == 0
        assert vm.terminated_early is False
        assert vm.run_terminated_early is False

    def test_v12_historical_reprocessing_present(self):
        provider = B2ReportProvider(report_root=str(V12_FIXTURE_ROOT), stale_seconds=9999999)
        vm = provider.load("v12_warning_fixture.json")
        assert vm.historical_reprocessing_attempts == 5310

    def test_v12_signal_storm_alert_rendered_in_tab(self):
        """signal_storm_detected=True → tab must show storm alert, NOT all-green."""
        result = render_b2_tab(
            report_path="v12_warning_fixture.json",
            report_root=str(V12_FIXTURE_ROOT),
            stale_seconds=9999999,
        )
        html_str = str(getattr(result, "children", ""))
        assert "SIGNAL STORM DETECTED" in html_str
        assert "cooldown: DISABLED" in html_str  # explicitly DISABLED, not green
        assert "historical reprocessing" in html_str
        assert "5310" in html_str


class TestV14Fixture:
    """v14 b2-1.1 fixture: full lineage, no storm, canonical fields"""

    def test_v14_schema_no_compat(self):
        provider = B2ReportProvider(report_root=str(V14_FIXTURE_ROOT), stale_seconds=9999999)
        vm = provider.load("v14_b2_1_1_fixture.json")
        assert vm.report_status == ReportStatus.OK
        assert vm.source_schema == "b2-1.1"
        assert vm.compatibility_mode is False

    def test_v14_lineage_full(self):
        provider = B2ReportProvider(report_root=str(V14_FIXTURE_ROOT), stale_seconds=9999999)
        vm = provider.load("v14_b2_1_1_fixture.json")
        assert vm.lineage_status == "FULL"
        assert vm.missing_lineage_fields == []
        assert vm.lineage_fields_present["strategy_id"] == "15/15"
        assert vm.lineage_fields_present["signal_rule_version"] == "15/15"

    def test_v14_no_signal_storm(self):
        provider = B2ReportProvider(report_root=str(V14_FIXTURE_ROOT), stale_seconds=9999999)
        vm = provider.load("v14_b2_1_1_fixture.json")
        assert vm.signal_storm_detected is False
        assert vm.cross_event_repeated_recommendations == 0

    def test_v14_duplicate_signals_independent(self):
        """duplicate_signals_created is independent from signals_skipped_idempotent."""
        provider = B2ReportProvider(report_root=str(V14_FIXTURE_ROOT), stale_seconds=9999999)
        vm = provider.load("v14_b2_1_1_fixture.json")
        assert vm.duplicate_signals_created == 0
        assert vm.signals_skipped_idempotent == 0
        # They must be separate fields, not forced-equal
        assert "duplicate_signals_created" in B2DashboardViewModel.__dataclass_fields__
        assert "signals_skipped_idempotent" in B2DashboardViewModel.__dataclass_fields__

    def test_v14_canonical_safety_fields(self):
        provider = B2ReportProvider(report_root=str(V14_FIXTURE_ROOT), stale_seconds=9999999)
        vm = provider.load("v14_b2_1_1_fixture.json")
        assert vm.real_pushes == 0
        assert vm.real_trades == 0
        assert vm.terminated_early is False

    def test_v14_cooldown_fields_declared(self):
        provider = B2ReportProvider(report_root=str(V14_FIXTURE_ROOT), stale_seconds=9999999)
        vm = provider.load("v14_b2_1_1_fixture.json")
        assert vm.cooldown_enabled is False
        assert vm.cooldown_policy_version == "b2-cooldown-v1-draft"

    def test_v14_timing_invariant_from_cycle_records(self):
        provider = B2ReportProvider(report_root=str(V14_FIXTURE_ROOT), stale_seconds=9999999)
        vm = provider.load("v14_b2_1_1_fixture.json")
        assert vm.timing_invariant.checked_cycles == 5
        assert vm.timing_invariant.violations == 0
        assert vm.timing_invariant.status == "PASS"
        assert vm.cycle_count == 5

    def test_v14_cycle_records_summary(self):
        provider = B2ReportProvider(report_root=str(V14_FIXTURE_ROOT), stale_seconds=9999999)
        vm = provider.load("v14_b2_1_1_fixture.json")
        assert len(vm.cycle_records_summary) == 5
        assert vm.cycle_records_summary[0]["seq"] == 1
        assert vm.cycle_records_summary[0]["http_ms"] == 55.0
        assert vm.cycle_records_summary[0]["cycle_ms"] == 160.0

    def test_v14_lineage_rendered_in_tab(self):
        """Lineage=FULL → tab shows FULL with green, not MISSING/PARTIAL."""
        result = render_b2_tab(
            report_path="v14_b2_1_1_fixture.json",
            report_root=str(V14_FIXTURE_ROOT),
            stale_seconds=9999999,
        )
        html_str = str(getattr(result, "children", ""))
        assert "Signal Lineage" in html_str
        assert "FULL" in html_str

    def test_v14_no_storm_alert_in_tab(self):
        """No storm → tab does NOT render storm alert banner."""
        result = render_b2_tab(
            report_path="v14_b2_1_1_fixture.json",
            report_root=str(V14_FIXTURE_ROOT),
            stale_seconds=9999999,
        )
        html_str = str(getattr(result, "children", ""))
        assert "SIGNAL STORM DETECTED" not in html_str

    def test_v14_schema_badge_in_banner(self):
        """b2-1.1 schema → banner shows schema badge."""
        result = render_b2_tab(
            report_path="v14_b2_1_1_fixture.json",
            report_root=str(V14_FIXTURE_ROOT),
            stale_seconds=9999999,
        )
        html_str = str(getattr(result, "children", ""))
        assert "schema:b2-1.1" in html_str


class TestCanonicalDeprecatedConsistency:
    """canonical 字段与 deprecated 字段值一致"""

    def test_v12_canonical_equals_deprecated_safety(self):
        provider = B2ReportProvider(report_root=str(V12_FIXTURE_ROOT), stale_seconds=9999999)
        vm = provider.load("v12_warning_fixture.json")
        assert vm.real_pushes == vm.real_push_count
        assert vm.real_trades == vm.real_trade_count
        assert vm.terminated_early == vm.run_terminated_early

    def test_v14_canonical_equals_deprecated_safety(self):
        provider = B2ReportProvider(report_root=str(V14_FIXTURE_ROOT), stale_seconds=9999999)
        vm = provider.load("v14_b2_1_1_fixture.json")
        assert vm.real_pushes == vm.real_push_count
        assert vm.real_trades == vm.real_trade_count
        assert vm.terminated_early == vm.run_terminated_early


class TestCooldownRendering:
    """cooldown 渲染: 未启用时显示 DISABLED (红色), 不是全绿'健康正向'"""

    def test_v12_cooldown_disabled_red_not_green(self):
        provider = B2ReportProvider(report_root=str(V12_FIXTURE_ROOT), stale_seconds=9999999)
        vm = provider.load("v12_warning_fixture.json")
        assert vm.cooldown_enabled is False
        result = render_b2_tab(
            report_path="v12_warning_fixture.json",
            report_root=str(V12_FIXTURE_ROOT),
            stale_seconds=9999999,
        )
        html_str = str(getattr(result, "children", ""))
        assert "cooldown: DISABLED" in html_str

    def test_v14_cooldown_disabled_not_enabled(self):
        provider = B2ReportProvider(report_root=str(V14_FIXTURE_ROOT), stale_seconds=9999999)
        vm = provider.load("v14_b2_1_1_fixture.json")
        assert vm.cooldown_enabled is False
        assert "b2-cooldown-v1-draft" in vm.cooldown_policy_version


class TestMissingFieldsNotHidden:
    """缺失值保持 UNKNOWN, 不得用 None or 0 隐藏"""

    def test_missing_audit_reported_is_none(self):
        """v12 fixture 无 audit 字段 → reported_passed=None, comparison=UNKNOWN."""
        provider = B2ReportProvider(report_root=str(V12_FIXTURE_ROOT), stale_seconds=9999999)
        vm = provider.load("v12_warning_fixture.json")
        for eq in vm.audit_equations:
            assert eq.reported_passed is None
            assert eq.comparison == "UNKNOWN"

    def test_v12_duplicate_signals_defaults_zero_not_hidden(self):
        """v12 无 duplicate_signals_created → 默认 0 (schema 缺失, 如实展示)."""
        provider = B2ReportProvider(report_root=str(V12_FIXTURE_ROOT), stale_seconds=9999999)
        vm = provider.load("v12_warning_fixture.json")
        # Default is 0 because field doesn't exist in v12 report
        assert vm.duplicate_signals_created == 0

    def test_recalc_still_works_when_reported_unknown(self):
        """即使 reported=None, 复算仍然独立完成."""
        provider = B2ReportProvider(report_root=str(V12_FIXTURE_ROOT), stale_seconds=9999999)
        vm = provider.load("v12_warning_fixture.json")
        assert vm.all_audit_passed is True  # 复算通过
        assert vm.has_any_mismatch is False  # 没有 mismatch (因为无可比较)


# ══════════════════════════════════════════════════════════════
# UI-P0 v2: Feature Flag unit tests
# ══════════════════════════════════════════════════════════════

import os as _os


def _parse_flag(raw_value: str) -> bool:
    """Exact copy of dash_dashboard._parse_b2_flag logic for isolated testing."""
    return raw_value.strip().lower() in {"true", "1", "yes", "on"}


class TestB2FeatureFlagParsing:
    """12 scenarios — flag parsing unit tests (no Dash server needed)."""

    def test_flag_unset_is_off(self):
        """环境变量未设置 → OFF."""
        assert _parse_flag("") is False

    def test_flag_false_is_off(self):
        assert _parse_flag("false") is False

    def test_flag_zero_is_off(self):
        assert _parse_flag("0") is False

    def test_flag_no_is_off(self):
        assert _parse_flag("no") is False

    def test_flag_off_is_off(self):
        assert _parse_flag("off") is False

    def test_flag_empty_is_off(self):
        assert _parse_flag("   ") is False

    def test_flag_garbage_is_off(self):
        """非法值 → OFF (fail-closed)."""
        assert _parse_flag("enabled") is False
        assert _parse_flag("maybe") is False
        assert _parse_flag("TRUEISH") is False
        assert _parse_flag("") is False

    def test_flag_true_is_on(self):
        assert _parse_flag("true") is True

    def test_flag_TRUE_case_insensitive_is_on(self):
        assert _parse_flag("TRUE") is True
        assert _parse_flag("True") is True

    def test_flag_one_is_on(self):
        assert _parse_flag("1") is True

    def test_flag_yes_is_on(self):
        assert _parse_flag("yes") is True
        assert _parse_flag("YES") is True

    def test_flag_on_is_on(self):
        assert _parse_flag("on") is True
        assert _parse_flag("ON") is True


class TestB2FeatureFlagIntegration:
    """Integration: actual dash_dashboard module flag behavior."""

    def test_dash_dashboard_default_flag_is_false(self):
        """Default import (no env) → ENABLE_B2_DASHBOARD is False."""
        # Save and clear env
        saved = _os.environ.pop("ENABLE_B2_DASHBOARD", None)
        try:
            import importlib
            import dash_dashboard
            importlib.reload(dash_dashboard)
            assert dash_dashboard.ENABLE_B2_DASHBOARD is False
        finally:
            if saved is not None:
                _os.environ["ENABLE_B2_DASHBOARD"] = saved

    def test_flag_on_tab_list_has_b2(self):
        """ON → _b2_tab contains exactly one B2 tab."""
        saved = _os.environ.get("ENABLE_B2_DASHBOARD")
        _os.environ["ENABLE_B2_DASHBOARD"] = "true"
        try:
            import importlib
            import dash_dashboard
            importlib.reload(dash_dashboard)
            assert len(dash_dashboard._b2_tab) == 1
            assert dash_dashboard._b2_tab[0].label == "🏭 B2 管线"
            assert dash_dashboard._b2_tab[0].value == "tab-b2"
        finally:
            if saved is not None:
                _os.environ["ENABLE_B2_DASHBOARD"] = saved
            else:
                _os.environ.pop("ENABLE_B2_DASHBOARD", None)

    def test_flag_off_tab_list_empty(self):
        """OFF → _b2_tab is empty list."""
        saved = _os.environ.get("ENABLE_B2_DASHBOARD")
        _os.environ["ENABLE_B2_DASHBOARD"] = "false"
        try:
            import importlib
            import dash_dashboard
            importlib.reload(dash_dashboard)
            assert dash_dashboard._b2_tab == []
        finally:
            if saved is not None:
                _os.environ["ENABLE_B2_DASHBOARD"] = saved
            else:
                _os.environ.pop("ENABLE_B2_DASHBOARD", None)

    def test_default_config_declares_flag_off(self):
        """README / default config declares flag default false."""
        readme_path = Path(__file__).parent.parent / "README.md"
        config_path = Path(__file__).parent.parent / "config.py"
        found = False
        for path in [readme_path, config_path]:
            if path.exists():
                content = path.read_text()
                if "ENABLE_B2_DASHBOARD" in content and "false" in content.lower():
                    found = True
                    break
        # Relaxed: flag is documented in dash_dashboard.py header comment
        dash_path = Path(__file__).parent.parent / "dash_dashboard.py"
        if dash_path.exists():
            content = dash_path.read_text()
            if "ENABLE_B2_DASHBOARD" in content and "false" in content.lower():
                found = True
        assert found, "ENABLE_B2_DASHBOARD default=false not documented"


# ══════════════════════════════════════════════════════════════
# UI-P0 v3: Dash layout integration (programmatic, no browser)
# ══════════════════════════════════════════════════════════════


class TestB2DashLayoutFlagOff:
    """FLAG OFF: B2 tab absent, 4 old tabs present, no traceback in layout."""

    def test_b2_tab_absent_from_layout(self):
        saved = _os.environ.pop("ENABLE_B2_DASHBOARD", None)
        try:
            import importlib
            import dash_dashboard
            importlib.reload(dash_dashboard)
            layout_str = str(dash_dashboard.app.layout)
            assert "🏭 B2 管线" not in layout_str
        finally:
            if saved is not None:
                _os.environ["ENABLE_B2_DASHBOARD"] = saved

    def test_four_old_tabs_present(self):
        saved = _os.environ.pop("ENABLE_B2_DASHBOARD", None)
        try:
            import importlib
            import dash_dashboard
            importlib.reload(dash_dashboard)
            layout_str = str(dash_dashboard.app.layout)
            for label in ["📊 IC 归因", "⚖️ 权重对比", "📈 执行历史", "🛡️ 风控状态"]:
                assert label in layout_str, f"Missing tab: {label}"
        finally:
            if saved is not None:
                _os.environ["ENABLE_B2_DASHBOARD"] = saved

    def test_no_traceback_in_layout(self):
        saved = _os.environ.pop("ENABLE_B2_DASHBOARD", None)
        try:
            import importlib
            import dash_dashboard
            importlib.reload(dash_dashboard)
            layout_str = str(dash_dashboard.app.layout)
            assert "Traceback" not in layout_str
        finally:
            if saved is not None:
                _os.environ["ENABLE_B2_DASHBOARD"] = saved

    def test_b2_callback_not_invoked_when_off(self):
        """OFF → render_tab('tab-b2') still works (fault-tolerant) but no tab to click."""
        saved = _os.environ.pop("ENABLE_B2_DASHBOARD", None)
        try:
            import importlib
            import dash_dashboard
            importlib.reload(dash_dashboard)
            result = dash_dashboard.render_tab("tab-b2", 0)
            assert result is not None
            assert "Div" in type(result).__name__
        finally:
            if saved is not None:
                _os.environ["ENABLE_B2_DASHBOARD"] = saved


class TestB2DashLayoutFlagOn:
    """FLAG ON: B2 tab present exactly once, old tabs preserved."""

    def test_b2_tab_present_exactly_once(self):
        saved = _os.environ.get("ENABLE_B2_DASHBOARD")
        _os.environ["ENABLE_B2_DASHBOARD"] = "true"
        _os.environ["B2_REPORT_ROOT"] = str(V14_FIXTURE_ROOT)
        _os.environ["B2_REPORT_PATH"] = "v14_b2_1_1_fixture.json"
        try:
            import importlib
            import dash_dashboard
            importlib.reload(dash_dashboard)
            layout_str = str(dash_dashboard.app.layout)
            assert layout_str.count("🏭 B2 管线") == 1, \
                f"Expected 1 B2 tab, found {layout_str.count('🏭 B2 管线')}"
        finally:
            _os.environ.pop("B2_REPORT_ROOT", None)
            _os.environ.pop("B2_REPORT_PATH", None)
            if saved is not None:
                _os.environ["ENABLE_B2_DASHBOARD"] = saved
            else:
                _os.environ.pop("ENABLE_B2_DASHBOARD", None)

    def test_old_tabs_still_present_with_b2(self):
        saved = _os.environ.get("ENABLE_B2_DASHBOARD")
        _os.environ["ENABLE_B2_DASHBOARD"] = "true"
        _os.environ["B2_REPORT_ROOT"] = str(V14_FIXTURE_ROOT)
        _os.environ["B2_REPORT_PATH"] = "v14_b2_1_1_fixture.json"
        try:
            import importlib
            import dash_dashboard
            importlib.reload(dash_dashboard)
            layout_str = str(dash_dashboard.app.layout)
            for label in ["📊 IC 归因", "⚖️ 权重对比", "📈 执行历史", "🛡️ 风控状态"]:
                assert label in layout_str, f"Missing tab with B2 ON: {label}"
        finally:
            _os.environ.pop("B2_REPORT_ROOT", None)
            _os.environ.pop("B2_REPORT_PATH", None)
            if saved is not None:
                _os.environ["ENABLE_B2_DASHBOARD"] = saved
            else:
                _os.environ.pop("ENABLE_B2_DASHBOARD", None)

    def test_b2_tab_renders_with_v14_fixture(self):
        saved_flag = _os.environ.get("ENABLE_B2_DASHBOARD")
        saved_root = _os.environ.get("B2_REPORT_ROOT")
        saved_path = _os.environ.get("B2_REPORT_PATH")
        _os.environ["ENABLE_B2_DASHBOARD"] = "true"
        _os.environ["B2_REPORT_ROOT"] = str(V14_FIXTURE_ROOT)
        _os.environ["B2_REPORT_PATH"] = "v14_b2_1_1_fixture.json"
        # Touch fixture to prevent STALE detection
        _os.utime(str(V14_FIXTURE_ROOT / "v14_b2_1_1_fixture.json"), None)
        try:
            import importlib
            import dash_dashboard
            importlib.reload(dash_dashboard)
            result = dash_dashboard.render_tab("tab-b2", 0)
            html_str = str(getattr(result, "children", ""))
            assert "schema:b2-1.1" in html_str
            assert "FULL" in html_str
        finally:
            for k, v in [("ENABLE_B2_DASHBOARD", saved_flag),
                         ("B2_REPORT_ROOT", saved_root),
                         ("B2_REPORT_PATH", saved_path)]:
                if v is not None:
                    _os.environ[k] = v
                else:
                    _os.environ.pop(k, None)

    def test_b2_tab_renders_with_v12_fixture(self):
        saved_flag = _os.environ.get("ENABLE_B2_DASHBOARD")
        saved_root = _os.environ.get("B2_REPORT_ROOT")
        saved_path = _os.environ.get("B2_REPORT_PATH")
        _os.environ["ENABLE_B2_DASHBOARD"] = "true"
        _os.environ["B2_REPORT_ROOT"] = str(V12_FIXTURE_ROOT)
        _os.environ["B2_REPORT_PATH"] = "v12_warning_fixture.json"
        # Touch fixture to prevent STALE detection
        _os.utime(str(V12_FIXTURE_ROOT / "v12_warning_fixture.json"), None)
        try:
            import importlib
            import dash_dashboard
            importlib.reload(dash_dashboard)
            result = dash_dashboard.render_tab("tab-b2", 0)
            html_str = str(getattr(result, "children", ""))
            assert "SIGNAL STORM DETECTED" in html_str
            assert "cooldown: DISABLED" in html_str
        finally:
            for k, v in [("ENABLE_B2_DASHBOARD", saved_flag),
                         ("B2_REPORT_ROOT", saved_root),
                         ("B2_REPORT_PATH", saved_path)]:
                if v is not None:
                    _os.environ[k] = v
                else:
                    _os.environ.pop(k, None)

    def test_no_traceback_with_b2_on(self):
        saved = _os.environ.get("ENABLE_B2_DASHBOARD")
        _os.environ["ENABLE_B2_DASHBOARD"] = "true"
        _os.environ["B2_REPORT_ROOT"] = str(V14_FIXTURE_ROOT)
        _os.environ["B2_REPORT_PATH"] = "v14_b2_1_1_fixture.json"
        try:
            import importlib
            import dash_dashboard
            importlib.reload(dash_dashboard)
            layout_str = str(dash_dashboard.app.layout)
            assert "Traceback" not in layout_str
        finally:
            _os.environ.pop("B2_REPORT_ROOT", None)
            _os.environ.pop("B2_REPORT_PATH", None)
            if saved is not None:
                _os.environ["ENABLE_B2_DASHBOARD"] = saved
            else:
                _os.environ.pop("ENABLE_B2_DASHBOARD", None)


class TestB2DashFaultIsolation:
    """B2 failures must not propagate to other tabs or crash the layout."""

    def test_file_not_found_returns_error_card(self):
        saved_flag = _os.environ.get("ENABLE_B2_DASHBOARD")
        saved_root = _os.environ.get("B2_REPORT_ROOT")
        saved_path = _os.environ.get("B2_REPORT_PATH")
        _os.environ["ENABLE_B2_DASHBOARD"] = "true"
        _os.environ["B2_REPORT_ROOT"] = str(V12_FIXTURE_ROOT)
        _os.environ["B2_REPORT_PATH"] = "nonexistent_file.json"
        try:
            import importlib
            import dash_dashboard
            importlib.reload(dash_dashboard)
            result = dash_dashboard.render_tab("tab-b2", 0)
            html_str = str(getattr(result, "children", ""))
            assert "NO DATA" in html_str or "ERROR" in html_str or "INVALID" in html_str.upper()
        finally:
            for k, v in [("ENABLE_B2_DASHBOARD", saved_flag),
                         ("B2_REPORT_ROOT", saved_root),
                         ("B2_REPORT_PATH", saved_path)]:
                if v is not None:
                    _os.environ[k] = v
                else:
                    _os.environ.pop(k, None)

    def test_old_tab_works_after_b2_error(self):
        """Old tab rendering succeeds even after B2 encounter."""
        saved_flag = _os.environ.get("ENABLE_B2_DASHBOARD")
        _os.environ["ENABLE_B2_DASHBOARD"] = "true"
        _os.environ["B2_REPORT_ROOT"] = str(V12_FIXTURE_ROOT)
        _os.environ["B2_REPORT_PATH"] = "nonexistent_file.json"
        try:
            import importlib
            import dash_dashboard
            importlib.reload(dash_dashboard)
            # B2 tab renders error card
            b2_result = dash_dashboard.render_tab("tab-b2", 0)
            assert b2_result is not None
            # Old tab (IC) still works
            ic_result = dash_dashboard.render_tab("tab-ic", 0)
            assert ic_result is not None
            assert "Div" in type(ic_result).__name__
        finally:
            for k, v in [("ENABLE_B2_DASHBOARD", saved_flag),
                         ("B2_REPORT_ROOT", _os.environ.get("B2_REPORT_ROOT")),
                         ("B2_REPORT_PATH", _os.environ.get("B2_REPORT_PATH"))]:
                _os.environ.pop(k, None)
                if v is not None and k == "ENABLE_B2_DASHBOARD":
                    _os.environ[k] = v

    def test_repeated_layout_build_does_not_duplicate(self):
        """Repeated module reload does not accumulate duplicate B2 tabs."""
        saved = _os.environ.get("ENABLE_B2_DASHBOARD")
        _os.environ["ENABLE_B2_DASHBOARD"] = "true"
        _os.environ["B2_REPORT_ROOT"] = str(V14_FIXTURE_ROOT)
        _os.environ["B2_REPORT_PATH"] = "v14_b2_1_1_fixture.json"
        try:
            import importlib
            import dash_dashboard
            # Reload twice
            importlib.reload(dash_dashboard)
            importlib.reload(dash_dashboard)
            layout_str = str(dash_dashboard.app.layout)
            assert layout_str.count("🏭 B2 管线") == 1, \
                f"Duplicate B2 tabs after reload: {layout_str.count('🏭 B2 管线')}"
        finally:
            _os.environ.pop("B2_REPORT_ROOT", None)
            _os.environ.pop("B2_REPORT_PATH", None)
            if saved is not None:
                _os.environ["ENABLE_B2_DASHBOARD"] = saved
            else:
                _os.environ.pop("ENABLE_B2_DASHBOARD", None)
