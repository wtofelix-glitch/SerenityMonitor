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
        assert "b2-report/1.0" in SUPPORTED_SCHEMAS
        assert "b2-report/1.0-implicit" in SUPPORTED_SCHEMAS


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
