"""v28: Postflight audit, report review, and invariant tests."""
from __future__ import annotations
import json
from pathlib import Path
import pytest


class TestV28PostflightAudit:
    """v28: B2Metrics.audit_postflight_invariants with SEC_PROD_DB split and SIGNAL_EQ1 run-scoped."""

    @staticmethod
    def _make_clean():
        from serenity_v2.phase_b2 import B2Metrics
        m = B2Metrics()
        m.run_id = "B2_TEST_V28"
        m.status = "COMPLETED"
        m.run_completed = True
        m.cycles_planned = 10; m.cycles_started = 8; m.cycles_completed = 7
        m.cycles_aborted = 1; m.cycles_skipped = 1; m.not_due_cycles = 1
        m.cancelled_cycles_auto_stop = 0; m.cycles_failed = 0
        m.raw_received = 100; m.raw_stored = 90; m.raw_duplicates = 10
        m.events_created = 50; m.events_deduplicated = 5
        m.signals_total = 20; m.signals_created_unique = 12
        m.signals_skipped_idempotent_run = 3; m.signals_skipped_idempotent_lifetime = 3
        m.signals_skipped_idempotent = 3; m.signals_skipped_cooldown = 2
        m.signals_no_decision = 3
        m.total_failures = 0
        m.ledger_claimed = 20; m.ledger_completed = 18
        m.ledger_failed_count = 1; m.ledger_in_progress = 1
        m.real_push_count = 0; m.real_trade_count = 0; m.account_modifications = 0
        return m

    def test_all_13_invariants_pass(self):
        m = self._make_clean()
        audit = m.audit_postflight_invariants(
            prod_guard_before={"sha256": "aaa111222333aaa111222333aaa111222333aaa111222333aaa111222333aaa111"},
            prod_guard_after={"sha256": "aaa111222333aaa111222333aaa111222333aaa111222333aaa111222333aaa111"},
            shadow_db_path="/tmp/shadow/shadow.db")
        assert audit["all_pass"] is True
        assert audit["failed"] == 0
        assert audit["total_checks"] == 13
        assert audit["audit_version"] == "v28"

    def test_v28_sec_prod_db_split(self):
        """v28: External DB change -> GLOBAL_UNCHANGED fails, RUNNER_DID_NOT_MODIFY passes."""
        m = self._make_clean()
        audit = m.audit_postflight_invariants(
            prod_guard_before={"sha256": "aaa111"},
            prod_guard_after={"sha256": "bbb222"},
            shadow_db_path="/tmp/shadow/shadow.db")
        global_check = [c for c in audit["checks"] if c["id"] == "SEC_PROD_DB_GLOBAL_UNCHANGED"][0]
        assert global_check["pass"] is False
        runner_check = [c for c in audit["checks"] if c["id"] == "SEC_RUNNER_DID_NOT_MODIFY_PROD_DB"][0]
        assert runner_check["pass"] is True

    def test_v28_runner_write_detected(self):
        """v28: Runner writes -> both RUNNER_DID_NOT_MODIFY and ZERO_SIDE_EFFECTS fail."""
        m = self._make_clean()
        m.real_push_count = 1
        audit = m.audit_postflight_invariants()
        failed = {c["id"] for c in audit["checks"] if not c["pass"]}
        assert "SEC_RUNNER_DID_NOT_MODIFY_PROD_DB" in failed
        assert "SEC_ZERO_SIDE_EFFECTS" in failed

    def test_v28_no_old_sec_prod_db_id(self):
        """v28: Old SEC_PROD_DB_UNCHANGED must not appear."""
        m = self._make_clean()
        audit = m.audit_postflight_invariants(
            prod_guard_before={"sha256": "aaa111"},
            prod_guard_after={"sha256": "aaa111"},
            shadow_db_path="/tmp/shadow/shadow.db")
        ids = {c["id"] for c in audit["checks"]}
        assert "SEC_PROD_DB_UNCHANGED" not in ids
        assert "SEC_PROD_DB_GLOBAL_UNCHANGED" in ids
        assert "SEC_RUNNER_DID_NOT_MODIFY_PROD_DB" in ids

    def test_v28_signal_eq1_run_scoped(self):
        """v28: SIGNAL_EQ1 uses run-scoped delta, not lifetime."""
        m = self._make_clean()
        m.signals_skipped_idempotent_run = 3
        m.signals_skipped_idempotent_lifetime = 999  # large lifetime, small run delta
        audit = m.audit_postflight_invariants()
        signal = [c for c in audit["checks"] if c["id"] == "SIGNAL_EQ1"][0]
        assert signal["pass"] is True
        assert "skipped_idempotent(run)" in signal["name"]
        assert "lifetime=999" in signal["detail"]

    def test_v28_signal_eq1_detects_mismatch(self):
        """v28: SIGNAL_EQ1 fails when run-scoped equation doesn't balance."""
        m = self._make_clean()
        m.signals_skipped_idempotent_run = 0  # 12+0+2+3=17 != 20
        audit = m.audit_postflight_invariants()
        failed = {c["id"] for c in audit["checks"] if not c["pass"]}
        assert "SIGNAL_EQ1" in failed

    def test_v28_scheduling_violation_detected(self):
        m = self._make_clean()
        m.cycles_planned = 10; m.cycles_started = 5
        audit = m.audit_postflight_invariants()
        failed = {c["id"] for c in audit["checks"] if not c["pass"]}
        assert "SCHED_EQ1" in failed

    def test_v28_data_integrity_violation_detected(self):
        m = self._make_clean()
        m.raw_received = 100; m.raw_stored = 80; m.raw_duplicates = 5
        audit = m.audit_postflight_invariants()
        failed = {c["id"] for c in audit["checks"] if not c["pass"]}
        assert "DATA_EQ1" in failed

    def test_v28_failure_accounting_violation_detected(self):
        m = self._make_clean()
        m.total_failures = 5; m.scheduler_failed = 3
        audit = m.audit_postflight_invariants()
        failed = {c["id"] for c in audit["checks"] if not c["pass"]}
        assert "FAIL_EQ1" in failed

    def test_v28_ledger_violation_detected(self):
        m = self._make_clean()
        m.ledger_claimed = 10; m.ledger_completed = 5
        m.ledger_failed_count = 1; m.ledger_in_progress = 1
        audit = m.audit_postflight_invariants()
        failed = {c["id"] for c in audit["checks"] if not c["pass"]}
        assert "LEDGER_EQ1" in failed

    def test_v28_all_categories_present(self):
        m = self._make_clean()
        audit = m.audit_postflight_invariants(
            prod_guard_before={"sha256": "aaa111"}, prod_guard_after={"sha256": "aaa111"},
            shadow_db_path="/tmp/shadow/shadow.db")
        cats = {c["category"] for c in audit["checks"]}
        for expected in ["scheduling", "data_integrity", "failure_accounting",
                         "idempotency", "security", "report_integrity", "process_integrity"]:
            assert expected in cats


class TestV28ReportReview:
    """v28: review_report with postflight audit integration."""

    @staticmethod
    def _make_clean():
        from serenity_v2.phase_b2 import B2Metrics
        m = B2Metrics()
        m.run_id = "B2_TEST"; m.status = "COMPLETED"
        m.started_at = "2026-08-01T12:00:00+08:00"; m.ended_at = "2026-08-01T12:05:00+08:00"
        m.duration_seconds = 300; m.cycles_planned = 60; m.cycles_started = 60
        m.cycles_completed = 60
        m.real_push_count = 0; m.real_trade_count = 0; m.account_modifications = 0
        return m

    def test_clean_metrics_pass_review(self):
        m = self._make_clean()
        result = m.review_report()
        assert result["ok"] is True

    def test_v28_review_includes_audit_failures(self):
        m = self._make_clean()
        failed_audit = {"all_pass": False, "checks": [
            {"id": "SCHED_EQ1", "name": "sched eq", "pass": False, "detail": "planned=10 != accounted=7"},
            {"id": "DATA_EQ1", "name": "data eq", "pass": False, "detail": "raw=100 != stored=80+dup=5"},
        ]}
        result = m.review_report(audit_result=failed_audit)
        assert result["ok"] is False
        assert any("SCHED_EQ1" in f for f in result["findings"])
        assert any("DATA_EQ1" in f for f in result["findings"])

    def test_v28_review_clean_with_passing_audit(self):
        m = self._make_clean()
        result = m.review_report(audit_result={"all_pass": True, "checks": []})
        assert result["ok"] is True

    def test_review_detects_real_pushes(self):
        m = self._make_clean(); m.real_push_count = 1
        result = m.review_report()
        assert result["ok"] is False

    def test_review_detects_real_trades(self):
        m = self._make_clean(); m.real_trade_count = 5
        result = m.review_report()
        assert result["ok"] is False


class TestV28Desensitization:
    """v28: desensitize_report with v28 version tag."""

    def test_desensitize_redacts_snapshot_id(self):
        from serenity_v2.phase_b2 import B2Metrics
        data = {"run_id": "test", "signal_details": [{
            "signal_id": "SIG", "symbol": "600487",
            "account_snapshot_id": "18dc7d197f33a1bd",
            "account_snapshot_id_full": "18dc7d197f33a1bde4312e187743309d3a01b7108e51d76d901e8c4e2b46ff67",
        }]}
        s = B2Metrics.desensitize_report(data)
        assert s["signal_details"][0]["account_snapshot_id_full"] == "[REDACTED]"
        assert s["signal_details"][0]["account_snapshot_id"] == "18dc7d19...[REDACTED]"

    def test_desensitize_truncates_sha256(self):
        from serenity_v2.phase_b2 import B2Metrics
        data = {"run_id": "test", "prod_file_hash_before": {
            "sha256": "ab9cc9266796abcdef1234567890abcdef1234567890abcdef1234567890abcd"}}
        s = B2Metrics.desensitize_report(data)
        sha = s["prod_file_hash_before"]["sha256"]
        assert len(sha) < 30
        assert "[REDACTED]" in sha

    def test_desensitize_v28_version(self):
        from serenity_v2.phase_b2 import B2Metrics
        s = B2Metrics.desensitize_report({"run_id": "test"})
        assert s["_desensitized_version"] == "v28"

    def test_desensitize_idempotent(self):
        from serenity_v2.phase_b2 import B2Metrics
        data = {"run_id": "test", "signal_details": [{
            "account_snapshot_id_full": "18dc7d197f33a1bde4312e187743309d3a01b7108e51d76d901e8c4e2b46ff67"}]}
        s1 = B2Metrics.desensitize_report(data)
        s2 = B2Metrics.desensitize_report(s1)
        assert s1 == s2


class TestV28B2ReportReviewer:
    """v28: B2ReportReviewer standalone tool."""

    def test_b2_report_reviewer_importable(self):
        from serenity_v2.phase_b2 import B2ReportReviewer
        assert B2ReportReviewer is not None
        assert hasattr(B2ReportReviewer, 'review_file')
        assert hasattr(B2ReportReviewer, 'desensitize_file')
