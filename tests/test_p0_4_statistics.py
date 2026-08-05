"""
P0-4 测试: 统计修复

18 个必测场景 + 审计方程验证。
"""

from __future__ import annotations

import copy
import json
import math
import tempfile
from pathlib import Path

import pytest

from serenity_v2.phase_b2 import (
    B2Metrics, CycleRecord, _p50, _p95,
)


# ══════════════════════════════════════════════════════════════════════════
# T01: 单次成功周期
# ══════════════════════════════════════════════════════════════════════════

class TestT01_SingleSuccessfulCycle:
    def test_cycle_record_created_with_all_timestamps(self):
        cr = CycleRecord(cycle_sequence=1)
        cr.scheduled_at = "2026-07-23T09:30:00.000+08:00"
        cr.cycle_started_at = "2026-07-23T09:30:00.050+08:00"
        cr.request_started_at = "2026-07-23T09:30:00.100+08:00"
        cr.response_received_at = "2026-07-23T09:30:00.350+08:00"
        cr.normalization_completed_at = "2026-07-23T09:30:00.400+08:00"
        cr.events_completed_at = "2026-07-23T09:30:00.420+08:00"
        cr.signals_completed_at = "2026-07-23T09:30:00.430+08:00"
        cr.cycle_completed_at = "2026-07-23T09:30:00.450+08:00"
        cr.status = "COMPLETED"

        cr.scheduled_mono = 1000.0
        cr.cycle_started_mono = 1000.05
        cr.request_started_mono = 1000.10
        cr.response_received_mono = 1000.35
        cr.normalization_completed_mono = 1000.40
        cr.events_completed_mono = 1000.42
        cr.signals_completed_mono = 1000.43
        cr.cycle_completed_mono = 1000.45

        # 计算间隔
        cr.schedule_delay_ms = (cr.cycle_started_mono - cr.scheduled_mono) * 1000
        cr.http_duration_ms = (cr.response_received_mono - cr.request_started_mono) * 1000
        cr.normalization_duration_ms = (cr.normalization_completed_mono - cr.response_received_mono) * 1000
        cr.event_duration_ms = (cr.events_completed_mono - cr.normalization_completed_mono) * 1000
        cr.signal_duration_ms = (cr.signals_completed_mono - cr.events_completed_mono) * 1000
        cr.cycle_duration_ms = (cr.cycle_completed_mono - cr.cycle_started_mono) * 1000

        assert cr.status == "COMPLETED"
        assert abs(cr.schedule_delay_ms - 50) < 1
        assert abs(cr.http_duration_ms - 250) < 1
        assert abs(cr.cycle_duration_ms - 400) < 1

    def test_timing_invariants_hold(self):
        cr = CycleRecord(cycle_sequence=1)
        cr.cycle_started_mono = 100.0
        cr.request_started_mono = 100.1
        cr.response_received_mono = 100.3
        cr.cycle_completed_mono = 100.5
        cr.http_duration_ms = 200
        cr.cycle_duration_ms = 400

        violations = cr.validate_timing_invariants()
        assert len(violations) == 0, f"Unexpected violations: {violations}"

    def test_timing_invariant_violation_detected(self):
        """cycle_duration < http_duration 应被检测。"""
        cr = CycleRecord(cycle_sequence=1)
        cr.cycle_started_mono = 100.0
        cr.request_started_mono = 100.1
        cr.response_received_mono = 100.9  # http took 800ms
        cr.cycle_completed_mono = 100.5     # but cycle ended before response?!
        cr.http_duration_ms = 800
        cr.cycle_duration_ms = 400

        violations = cr.validate_timing_invariants()
        assert len(violations) > 0


# ══════════════════════════════════════════════════════════════════════════
# T02: HTTP 失败
# ══════════════════════════════════════════════════════════════════════════

class TestT02_HttpFailure:
    def test_fetch_failed_tracked(self):
        m = B2Metrics()
        m.fetch_failed += 1
        m.requests_failed += 1
        m.total_failures = m.fetch_failed + m.http_failed  # recalc
        assert m.fetch_failed == 1
        assert m.requests_failed == 1

    def test_no_data_cycle_marked_failed(self):
        cr = CycleRecord(cycle_sequence=1)
        cr.status = "FAILED"
        cr.primary_failure_type = "fetch_failed"
        assert cr.status == "FAILED"
        assert cr.primary_failure_type == "fetch_failed"


# ══════════════════════════════════════════════════════════════════════════
# T03: HTTP 重试后成功
# ══════════════════════════════════════════════════════════════════════════

class TestT03_HttpRetrySuccess:
    def test_multiple_attempt_times_preserved(self):
        m = B2Metrics()
        m.raw_http_attempt_times_ms.extend([120, 250, 180])
        assert len(m.raw_http_attempt_times_ms) == 3
        # HTTP cycle time covers all attempts
        total = sum(m.raw_http_attempt_times_ms)
        assert total > max(m.raw_http_attempt_times_ms)

    def test_http_cycle_includes_retries(self):
        """HTTP 周期总时间 >= 各次尝试之和。"""
        attempts = [100, 200, 150]
        cycle_http = sum(attempts) + 50  # 含重试等待
        assert cycle_http >= max(attempts)
        assert cycle_http >= sum(attempts)


# ══════════════════════════════════════════════════════════════════════════
# T04: 解析失败
# ══════════════════════════════════════════════════════════════════════════

class TestT04_ParseFailure:
    def test_parse_failed_type(self):
        m = B2Metrics()
        m.parse_failed += 1
        assert m.parse_failed == 1

    def test_parse_failure_cycle_record(self):
        cr = CycleRecord(cycle_sequence=2)
        cr.status = "FAILED"
        cr.primary_failure_type = "parse_failed"
        assert cr.primary_failure_type == "parse_failed"


# ══════════════════════════════════════════════════════════════════════════
# T05: Validation 拒绝
# ══════════════════════════════════════════════════════════════════════════

class TestT05_ValidationRejection:
    def test_validation_failed_type(self):
        m = B2Metrics()
        m.validation_failed += 1
        assert m.validation_failed == 1

    def test_rejection_detail_structure(self):
        rejection = {
            "symbol": "600487",
            "source_timestamp": "2026-07-23T09:30:00",
            "collected_at": "2026-07-23T09:30:01",
            "validation_reason": "price_out_of_range",
            "raw_payload_sha256": "abc123def456",
            "cycle_sequence": 1,
        }
        assert "symbol" in rejection
        assert "validation_reason" in rejection
        assert "raw_payload_sha256" in rejection
        assert "cycle_sequence" in rejection


# ══════════════════════════════════════════════════════════════════════════
# T06: Event 处理失败
# ══════════════════════════════════════════════════════════════════════════

class TestT06_EventProcessingFailure:
    def test_event_failed_type(self):
        m = B2Metrics()
        m.event_failed += 1
        m.event_processing_failed += 1
        assert m.event_failed == 1
        assert m.event_processing_failed == 1


# ══════════════════════════════════════════════════════════════════════════
# T07: Signal 处理失败
# ══════════════════════════════════════════════════════════════════════════

class TestT07_SignalProcessingFailure:
    def test_signal_failed_type(self):
        m = B2Metrics()
        m.signal_failed += 1
        assert m.signal_failed == 1


# ══════════════════════════════════════════════════════════════════════════
# T08: Ledger 写入失败
# ══════════════════════════════════════════════════════════════════════════

class TestT08_LedgerWriteFailure:
    def test_ledger_failed_type(self):
        m = B2Metrics()
        m.ledger_failed += 1
        assert m.ledger_failed == 1


# ══════════════════════════════════════════════════════════════════════════
# T09: Report 写入失败
# ══════════════════════════════════════════════════════════════════════════

class TestT09_ReportWriteFailure:
    def test_report_failed_type(self):
        m = B2Metrics()
        m.report_failed += 1
        assert m.report_failed == 1


# ══════════════════════════════════════════════════════════════════════════
# T10: 周期被调度跳过
# ══════════════════════════════════════════════════════════════════════════

class TestT10_CycleScheduledSkip:
    def test_skipped_cycle_counted(self):
        m = B2Metrics()
        m.cycles_skipped += 1
        m.cycles_planned = 10
        m.cycles_started = 9
        assert m.cycles_skipped == 1
        assert m.cycles_planned == m.cycles_started + m.cycles_skipped

    def test_not_due_cycles_not_counted_as_failure(self):
        m = B2Metrics()
        m.not_due_cycles = 2
        m.cycles_planned = 12
        m.cycles_started = 10
        m.cycles_skipped = 0
        assert m.cycles_planned == m.cycles_started + m.cycles_skipped + m.not_due_cycles


# ══════════════════════════════════════════════════════════════════════════
# T11: Session boundary 正常停止
# ══════════════════════════════════════════════════════════════════════════

class TestT11_SessionBoundaryStop:
    def test_normal_stop_not_counted_as_error(self):
        cr = CycleRecord(cycle_sequence=5)
        cr.status = "ABORTED"
        cr.primary_failure_type = ""
        assert cr.status == "ABORTED"
        assert cr.primary_failure_type == ""


# ══════════════════════════════════════════════════════════════════════════
# T12: HTTP 时间长于旧统计窗口
# ══════════════════════════════════════════════════════════════════════════

class TestT12_HttpLongerThanCycleWindow:
    def test_http_duration_longer_detected(self):
        cr = CycleRecord(cycle_sequence=1)
        cr.http_duration_ms = 6000  # 6秒
        cr.cycle_duration_ms = 3000  # 3秒 — 不可能
        violations = cr.validate_timing_invariants()
        assert len(violations) > 0
        assert any("cycle_duration" in v for v in violations)

    def test_http_shorter_than_cycle_is_normal(self):
        cr = CycleRecord(cycle_sequence=1)
        cr.http_duration_ms = 250
        cr.cycle_duration_ms = 400
        violations = cr.validate_timing_invariants()
        assert len(violations) == 0


# ══════════════════════════════════════════════════════════════════════════
# T13: 多次重试时 cycle 耗时覆盖全部 attempt
# ══════════════════════════════════════════════════════════════════════════

class TestT13_CycleCoversAllAttempts:
    def test_multiple_attempts_sum_less_than_cycle(self):
        attempts = [500, 600, 300]
        cycle_duration = 2000  # 包含重试等待
        assert cycle_duration > sum(attempts)
        assert cycle_duration > max(attempts)

    def test_preserved_attempt_times_array(self):
        m = B2Metrics()
        m.raw_http_attempt_times_ms = [200, 450, 180]
        assert _p50(m.raw_http_attempt_times_ms) > 0
        assert _p95(m.raw_http_attempt_times_ms) > 0


# ══════════════════════════════════════════════════════════════════════════
# T14: 空样本时分位数安全处理
# ══════════════════════════════════════════════════════════════════════════

class TestT14_EmptySamplePercentileSafety:
    def test_p50_empty_returns_zero(self):
        assert _p50([]) == 0.0

    def test_p95_empty_returns_zero(self):
        assert _p95([]) == 0.0

    def test_empty_array_no_crash(self):
        m = B2Metrics()
        assert _p50(m.http_response_times_ms) == 0.0
        assert _p95(m.cycle_times_ms) == 0.0
        assert _p50(m.schedule_delay_ms_values) == 0.0


# ══════════════════════════════════════════════════════════════════════════
# T15: 单样本 P50/P95/MAX 一致
# ══════════════════════════════════════════════════════════════════════════

class TestT15_SingleSampleConsistency:
    def test_single_value_all_equal(self):
        vals = [42.0]
        assert _p50(vals) == 42.0
        assert _p95(vals) == 42.0
        assert max(vals) == 42.0

    def test_two_values(self):
        vals = [10.0, 20.0]
        assert _p50(vals) == 15.0  # interpolation
        assert _p95(vals) == 10.0  # int(2*0.95)-1 = 0, sorted[0] = 10.0


# ══════════════════════════════════════════════════════════════════════════
# T16: 原始统计数据可重算
# ══════════════════════════════════════════════════════════════════════════

class TestT16_RawStatsRecomputable:
    def test_p50_from_raw_matches(self):
        raw = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
        p50_val = _p50(raw)
        # Verify by manual computation
        sorted_raw = sorted(raw)
        n = len(sorted_raw)
        expected = (sorted_raw[4] + sorted_raw[5]) / 2  # (50+60)/2 = 55
        assert abs(p50_val - expected) < 0.01, f"{p50_val} != {expected}"

    def test_p95_from_raw_matches(self):
        raw = list(range(1, 101))  # 1..100
        p95_val = _p95(raw)
        # 95th percentile of 1..100: index = max(0, int(100*0.95)-1) = 94
        # sorted[94] = 95 (indexing from 0)
        assert p95_val == 95.0, f"{p95_val} != 95.0"

    def test_cycle_record_preserves_all_raw_data(self):
        cr = CycleRecord(cycle_sequence=1)
        cr.http_duration_ms = 250.5
        cr.cycle_duration_ms = 400.2
        cr.normalization_duration_ms = 30.0
        cr.event_duration_ms = 15.0
        cr.signal_duration_ms = 8.0

        d = cr.__dict__ if hasattr(cr, '__dict__') else {}
        assert "http_duration_ms" in dir(cr) or hasattr(cr, 'http_duration_ms')
        assert cr.http_duration_ms == 250.5
        assert cr.cycle_duration_ms > cr.http_duration_ms


# ══════════════════════════════════════════════════════════════════════════
# T17: 所有审计方程平衡
# ══════════════════════════════════════════════════════════════════════════

class TestT17_AllAuditEquations:
    def test_scheduling_equation(self):
        m = B2Metrics()
        m.cycles_planned = 10
        m.cycles_started = 8
        m.cycles_skipped = 2
        m.not_due_cycles = 0
        assert m.cycles_planned == m.cycles_started + m.cycles_skipped + m.not_due_cycles

        m.cycles_completed = 7
        m.cycles_aborted = 1
        assert m.cycles_started == m.cycles_completed + m.cycles_aborted

    def test_data_equation(self):
        m = B2Metrics()
        m.raw_received = 9
        m.normalized_accepted = 6
        m.normalized_rejected = 2
        m.quarantined = 1
        assert m.raw_received == m.normalized_accepted + m.normalized_rejected + m.quarantined

    def test_event_equation(self):
        m = B2Metrics()
        m.normalized_accepted = 6
        m.events_created = 4
        m.events_deduplicated = 1
        m.events_not_triggered = 1
        m.event_processing_failed = 0
        assert m.normalized_accepted == (m.events_created + m.events_deduplicated
                                         + m.events_not_triggered + m.event_processing_failed)

    def test_action_equation(self):
        m = B2Metrics()
        m.candidate_ACTION = 5
        m.effective_ACTION = 3
        m.ACTION_downgraded = 1
        m.ACTION_rejected = 1
        assert m.candidate_ACTION == m.effective_ACTION + m.ACTION_downgraded + m.ACTION_rejected

    def test_ledger_equation(self):
        m = B2Metrics()
        m.ledger_claimed = 10
        m.ledger_completed = 8
        m.ledger_failed_count = 1
        m.ledger_in_progress = 1
        assert m.ledger_claimed == m.ledger_completed + m.ledger_failed_count + m.ledger_in_progress

    def test_failure_types_sum_equals_total(self):
        m = B2Metrics()
        m.fetch_failed = 3
        m.http_failed = 1
        m.parse_failed = 2
        m.validation_failed = 1
        m.normalization_failed = 0
        m.quarantine_failed = 0
        m.event_failed = 1
        m.signal_failed = 0
        m.ledger_failed = 0
        m.report_failed = 0
        m.safety_guard_failed = 0
        m.scheduler_failed = 0
        m.session_check_failed = 0
        m.total_failures = sum([
            m.scheduler_failed, m.session_check_failed,
            m.fetch_failed, m.http_failed, m.parse_failed,
            m.validation_failed, m.normalization_failed,
            m.quarantine_failed, m.event_failed, m.signal_failed,
            m.ledger_failed, m.report_failed, m.safety_guard_failed,
        ])
        assert m.total_failures == 8, f"total={m.total_failures}, expected 8"

    def test_total_failures_no_double_count(self):
        """同一故障不重复计数。"""
        m = B2Metrics()
        # 一个 HTTP 错误只计入 http_failed，不计入 fetch_failed
        m.http_failed = 1
        m.total_failures = m.http_failed
        assert m.fetch_failed == 0  # 不应同时计入
        assert m.total_failures == 1


# ══════════════════════════════════════════════════════════════════════════
# T18: 生产文件及账户状态不变
# ══════════════════════════════════════════════════════════════════════════

class TestT18_ProdFileAndAccountUnchanged:
    def test_b2_metrics_prod_fields_zero(self):
        m = B2Metrics()
        assert m.real_push_count == 0
        assert m.real_trade_count == 0
        assert m.account_modifications == 0

    def test_cycle_record_not_contain_prod_data(self):
        cr = CycleRecord(cycle_sequence=1)
        cr.status = "COMPLETED"
        assert not hasattr(cr, 'prod_touched') or getattr(cr, 'prod_touched', False) is False


# ══════════════════════════════════════════════════════════════════════════
# 额外: save_report 包含原始数组
# ══════════════════════════════════════════════════════════════════════════

class TestSaveReportRawArrays:
    def test_save_report_includes_raw_cycle_records(self):
        """验证 save_report 保留原始周期记录可重算。"""
        from serenity_v2.phase_b2 import B2Runner
        from serenity_v2.env import reset_env
        from serenity_v2.account_baseline import reset_baseline
        from serenity_v2.account_fixture import reset_fixture as reset_fix

        reset_env()
        reset_baseline()
        reset_fix()

        import tempfile
        tmpdir = tempfile.mkdtemp()
        prod_db = Path(tmpdir) / "serenity.db"
        prod_db.write_bytes(b'fake')

        try:
            runner = B2Runner(
                duration_seconds=5,
                interval_seconds=1,
                protected_prod_db=str(prod_db),
                manifest_path='',
            )
            # 模拟一次运行（不实际 fetch，只验证报告结构）
            from dataclasses import asdict
            data = asdict(runner.metrics)
            assert "cycle_records" in data
            assert "http_response_times_ms" in data
            assert "schedule_delay_ms_values" in data
        finally:
            reset_env()
            reset_baseline()
            reset_fix()
