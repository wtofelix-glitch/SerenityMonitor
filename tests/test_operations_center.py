"""Tests for the auditable daily operations loop."""
from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, timedelta

import pytest


@pytest.fixture
def operations_db(monkeypatch):
    import db

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    path = tmp.name
    tmp.close()
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    yield db
    os.unlink(path)


def _broker_snapshot(**overrides):
    snapshot = {
        "snapshot_at": "2026-07-02 23:10:00",
        "source": "broker_screenshot",
        "total_assets": 58419.98,
        "holdings_value": 57675.0,
        "cash": 744.98,
        "daily_profit": -440.0,
        "daily_profit_pct": -0.75,
        "positions": [
            {"code": "000938", "name": "紫光股份", "shares": 1000, "market_value": 29030, "profit_pct": 10.487},
            {"code": "600585", "name": "海螺水泥", "shares": 1700, "market_value": 28645, "profit_pct": -0.875},
        ],
    }
    snapshot.update(overrides)
    return snapshot


def _internal_snapshot(**overrides):
    snapshot = {
        "total_value": 58419.98,
        "holdings_value": 57675.0,
        "cash": 744.98,
        "positions": [
            {"code": "000938", "shares": 1000},
            {"code": "600585", "shares": 1700},
        ],
    }
    snapshot.update(overrides)
    return snapshot


def test_reconciliation_matches_exact_broker_facts(operations_db):
    import operations_center

    result = operations_center.run_reconciliation(
        internal=_internal_snapshot(),
        broker=_broker_snapshot(),
        now=datetime(2026, 7, 3, 0, 10),
    )

    assert result["status"] == "matched"
    assert result["asset_drift"] == 0
    assert result["position_mismatches"] == []
    assert operations_db.get_operational_tasks() == []


def test_reconciliation_task_is_deduplicated(operations_db):
    import operations_center

    for _ in range(2):
        result = operations_center.run_reconciliation(
            internal=_internal_snapshot(cash=1000),
            broker=_broker_snapshot(),
            now=datetime(2026, 7, 3, 0, 10),
        )

    tasks = operations_db.get_operational_tasks()
    assert result["status"] == "blocked"
    assert len(tasks) == 1
    assert tasks[0]["dedupe_key"] == "reconciliation:portfolio"
    assert tasks[0]["severity"] == "critical"


def test_data_quality_summary_keeps_low_confidence_visible(operations_db):
    import operations_center

    conn = operations_db.get_conn()
    # 用相对日期（昨天），确保落在 get_data_quality_summary(days=30) 窗口内
    insert_date = (date.today() - timedelta(days=1)).isoformat()
    conn.executemany(
        """
        INSERT INTO data_quality_log
            (code, date, quality_status, conflict_pct, warning)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            ("000938", insert_date, "high", 0, ""),
            ("600585", insert_date, "low", 0.017, "source conflict"),
            ("600900", insert_date, "missing", 0, "no source"),
        ],
    )
    conn.commit()
    conn.close()

    result = operations_center.get_data_quality_summary(days=30)

    assert result["total"] == 3
    assert result["high_confidence"] == 1
    assert result["low_confidence"] == 1
    assert result["missing"] == 1
    assert result["conflicts"] == 1
    assert len(result["warnings"]) == 2


def test_risk_tasks_use_reconciled_daily_loss_and_concentration(operations_db):
    import operations_center

    broker = _broker_snapshot(daily_profit_pct=-2.2)
    quality = {
        "low_confidence": 0,
        "missing": 0,
        "conflicts": 0,
        "warnings": [],
        "settlements": {},
    }
    tasks = operations_center.generate_risk_tasks(
        broker=broker,
        quality=quality,
        reconciliation={"status": "matched"},
        dry_run=True,
    )

    keys = {task["dedupe_key"] for task in tasks}
    assert "risk:daily_loss" in keys
    assert "risk:concentration:000938" in keys
    assert "risk:concentration:600585" in keys


def test_risk_tasks_surface_due_settlement_backlog(operations_db):
    import operations_center

    tasks = operations_center.generate_risk_tasks(
        broker=_broker_snapshot(),
        quality={"low_confidence": 0, "missing": 0, "conflicts": 0, "warnings": [], "settlements": {}},
        reconciliation={"status": "matched"},
        settlement={
            "pending": 7,
            "due_blocked": 5,
            "reason_counts": {"missing_benchmark_entry": 5, "awaiting_exit_date": 2},
        },
        dry_run=True,
    )

    task = next(item for item in tasks if item["dedupe_key"] == "data_quality:settlement_backlog")
    assert task["severity"] == "critical"
    assert "5 due signals" in task["summary"]


def test_risk_tasks_surface_p0_validation_blockers(operations_db, monkeypatch):
    import operations_center

    fake_report = {
        "verdict": "P0_DATA_INVALID",
        "criteria": [
            {"key": "nav_data_quality", "status": "BLOCK", "label": "NAV valid"},
            {"key": "real_samples", "status": "INSUFFICIENT", "label": "samples"},
        ],
        "portfolio": {"broker_candidate": {"points": 1, "required_points": 2}},
        "data_quality": {
            "drawdown_cashflow": {
                "status": "BLOCK",
                "gap": -35089.6,
                "tolerance": 339.22,
                "window": "2026-06-16~2026-06-17",
            },
        },
        "drawdown_attribution": {"status": "ready", "start": "2026-06-16", "end": "2026-06-17"},
        "samples": {"sample_count": 0, "required_sample_count": 50},
    }

    monkeypatch.setattr(
        "alpha_validation.build_alpha_validation_report",
        lambda: fake_report,
    )
    monkeypatch.setattr(
        operations_center,
        "build_broker_snapshot_template",
        lambda: {"missing_points": 1, "import_command": "python3 cli.py broker-snapshot x --dry-run"},
    )
    monkeypatch.setattr(
        operations_center,
        "build_sample_readiness_report",
        lambda: {"status": "awaiting_exit_date", "pending_current_version": 10},
    )

    tasks = operations_center.generate_risk_tasks(
        broker=_broker_snapshot(),
        quality={"low_confidence": 0, "missing": 0, "conflicts": 0, "warnings": [], "settlements": {}},
        reconciliation={"status": "matched"},
        dry_run=True,
    )

    by_key = {task["dedupe_key"]: task for task in tasks}
    assert by_key["p0:alpha_validation"]["severity"] == "critical"
    assert by_key["p0:broker_snapshot"]["details"]["broker_points"] == 1
    assert by_key["p0:drawdown_cashflow"]["details"]["drawdown_cashflow"]["gap"] == -35089.6
    assert (
        by_key["p0:drawdown_cashflow"]["details"]["lint_command"]
        == "python3 cli.py cashflow-reconciliation-lint <json-file>"
    )
    assert by_key["p0:real_samples"]["severity"] == "warning"
    assert by_key["p0:real_samples"]["details"]["readiness_command"] == "python3 cli.py p0-sample-readiness"
    assert by_key["p0:real_samples"]["details"]["sample_readiness"]["pending_current_version"] == 10


def test_persisted_p0_tasks_resolve_after_validation_passes(operations_db, monkeypatch):
    import operations_center

    blocking_report = {
        "verdict": "P0_DATA_INVALID",
        "criteria": [
            {"key": "nav_data_quality", "status": "BLOCK", "label": "NAV valid"},
        ],
        "portfolio": {"broker_candidate": {"points": 1, "required_points": 2}},
        "data_quality": {
            "drawdown_cashflow": {
                "status": "BLOCK",
                "gap": -35089.6,
                "tolerance": 339.22,
            },
        },
        "drawdown_attribution": {"status": "ready"},
        "samples": {"sample_count": 0, "required_sample_count": 50},
    }
    passing_report = {
        "verdict": "P0_PASS",
        "criteria": [],
        "portfolio": {"broker_candidate": {"points": 2, "required_points": 2}},
        "data_quality": {},
        "samples": {"sample_count": 50, "required_sample_count": 50},
    }
    report = {"value": blocking_report}

    monkeypatch.setattr(
        "alpha_validation.build_alpha_validation_report",
        lambda: report["value"],
    )
    monkeypatch.setattr(
        operations_center,
        "build_broker_snapshot_template",
        lambda: {"missing_points": 1, "import_command": "python3 cli.py broker-snapshot x --dry-run"},
    )
    monkeypatch.setattr(
        operations_center,
        "build_sample_readiness_report",
        lambda: {"status": "awaiting_exit_date", "pending_current_version": 10},
    )

    operations_center.generate_risk_tasks(
        broker=_broker_snapshot(),
        quality={"low_confidence": 0, "missing": 0, "conflicts": 0, "warnings": [], "settlements": {}},
        reconciliation={"status": "matched"},
        dry_run=False,
    )

    open_keys = {task["dedupe_key"] for task in operations_db.get_operational_tasks()}
    assert "p0:alpha_validation" in open_keys
    assert "p0:broker_snapshot" in open_keys
    assert "p0:drawdown_cashflow" in open_keys
    assert "p0:real_samples" in open_keys

    report["value"] = passing_report
    operations_center.generate_risk_tasks(
        broker=_broker_snapshot(),
        quality={"low_confidence": 0, "missing": 0, "conflicts": 0, "warnings": [], "settlements": {}},
        reconciliation={"status": "matched"},
        dry_run=False,
    )

    open_p0_keys = {
        task["dedupe_key"]
        for task in operations_db.get_operational_tasks()
        if task["dedupe_key"].startswith("p0:")
    }
    resolved_p0_keys = {
        task["dedupe_key"]
        for task in operations_db.get_operational_tasks(status="", limit=100)
        if task["dedupe_key"].startswith("p0:") and task["status"] == "resolved"
    }
    assert open_p0_keys == set()
    assert {
        "p0:alpha_validation",
        "p0:broker_snapshot",
        "p0:drawdown_cashflow",
        "p0:real_samples",
    }.issubset(resolved_p0_keys)


def test_cashflow_reconciliation_template_explains_negative_gap(operations_db, monkeypatch):
    import operations_center

    fake_report = {
        "verdict": "P0_DATA_INVALID",
        "data_quality": {
            "drawdown_cashflow": {
                "status": "BLOCK",
                "gap": -35089.6,
                "tolerance": 339.22,
                "window": "2026-06-16~2026-06-17",
            },
        },
        "drawdown_attribution": {
            "status": "ready",
            "source": "diagnostic_nav_history",
            "start": "2026-06-16",
            "end": "2026-06-17",
            "start_total": 67843.62,
            "end_total": 49829.53,
            "start_cash": 35825.7,
            "end_cash": 736.1,
            "start_holdings": 32017.92,
            "end_holdings": 49093.43,
            "cash_change": -35089.6,
            "buy_amount": 0,
            "sell_amount": 0,
            "net_trade_cashflow": 0,
            "cashflow_gap": -35089.6,
            "cashflow_tolerance": 339.22,
            "cashflow_reconciled": False,
            "trades": [],
        },
    }
    monkeypatch.setattr(
        "alpha_validation.build_alpha_validation_report",
        lambda: fake_report,
    )

    result = operations_center.build_cashflow_reconciliation_template(
        now=datetime(2026, 7, 9, 8, 30),
    )

    assert result["status"] == "needs_cashflow_reconciliation"
    assert result["direction"] == "unexplained_cash_outflow"
    assert "missing buy trade" in result["likely_causes"]
    assert result["template"]["cash_reconciliation"]["start_cash"] == 35825.7
    assert result["template"]["cash_reconciliation"]["end_cash"] == 736.1
    assert result["template"]["evidence_items"][0]["cash_effect"] == -35089.6
    assert result["suggested_filename"] == "cashflow_reconciliation_20260709_0830.json"


def test_cashflow_reconciliation_template_skips_when_not_blocked(operations_db, monkeypatch):
    import operations_center

    monkeypatch.setattr(
        "alpha_validation.build_alpha_validation_report",
        lambda: {
            "verdict": "P0_NOT_PROVEN",
            "data_quality": {"drawdown_cashflow": {"status": "PASS"}},
            "drawdown_attribution": {},
        },
    )

    result = operations_center.build_cashflow_reconciliation_template(
        now=datetime(2026, 7, 9, 8, 30),
    )

    assert result["status"] == "not_needed"
    assert result["cashflow_status"] == "PASS"


def test_cashflow_reconciliation_lint_rejects_template_placeholders(operations_db):
    import operations_center

    payload = {
        "window": {"start": "2026-06-16", "end": "2026-06-17"},
        "cash_reconciliation": {
            "unexplained_cash_effect": -35089.6,
            "tolerance": 339.22,
        },
        "evidence_items": [
            {
                "date": "2026-06-17",
                "type": None,
                "cash_effect": -35089.6,
                "amount": 35089.6,
                "evidence_path": "",
            },
        ],
    }

    result = operations_center.lint_cashflow_reconciliation_payload(payload)

    assert result["valid"] is False
    assert "evidence_items[0].type is required" in result["errors"]
    assert "evidence_items[0].evidence_path is required" in result["errors"]


def test_cashflow_reconciliation_lint_accepts_supported_evidence(operations_db):
    import operations_center

    payload = {
        "window": {"start": "2026-06-16", "end": "2026-06-17"},
        "cash_reconciliation": {
            "unexplained_cash_effect": -35089.6,
            "tolerance": 339.22,
        },
        "evidence_items": [
            {
                "date": "2026-06-17",
                "type": "cash_transfer_out",
                "cash_effect": -35089.6,
                "amount": 35089.6,
                "evidence_path": "/evidence/broker_statement.pdf",
            },
        ],
    }

    result = operations_center.lint_cashflow_reconciliation_payload(payload)

    assert result["valid"] is True
    assert result["remaining_gap"] == 0
    assert result["evidence_cash_effect_total"] == -35089.6


def test_cashflow_reconciliation_import_persists_supported_evidence(operations_db):
    import operations_center

    payload = {
        "window": {"start": "2026-06-16", "end": "2026-06-17", "source": "manual"},
        "cash_reconciliation": {
            "unexplained_cash_effect": -35089.6,
            "tolerance": 339.22,
        },
        "evidence_items": [
            {
                "date": "2026-06-17",
                "type": "cash_transfer_out",
                "cash_effect": -35089.6,
                "amount": 35089.6,
                "evidence_path": "/evidence/broker_statement.pdf",
                "notes": "broker statement transfer out",
            },
        ],
    }

    dry_run = operations_center.import_cashflow_reconciliation(payload, dry_run=True)
    assert dry_run["valid"] is True
    assert dry_run["imported"] is False
    assert operations_db.get_cashflow_reconciliation("2026-06-16", "2026-06-17") is None

    result = operations_center.import_cashflow_reconciliation(payload, dry_run=False)

    assert result["valid"] is True
    assert result["imported"] is True
    saved = operations_db.get_cashflow_reconciliation("2026-06-16", "2026-06-17")
    assert saved["remaining_gap"] == 0
    assert saved["evidence_hash"] == result["evidence_hash"]
    assert saved["evidence_items"][0]["evidence_path"] == "/evidence/broker_statement.pdf"


def test_cashflow_reconciliation_import_rejects_invalid_payload(operations_db):
    import operations_center

    payload = {
        "window": {"start": "2026-06-16", "end": "2026-06-17"},
        "cash_reconciliation": {
            "unexplained_cash_effect": -35089.6,
            "tolerance": 339.22,
        },
        "evidence_items": [
            {
                "date": "2026-06-17",
                "type": None,
                "cash_effect": -35089.6,
                "amount": 35089.6,
                "evidence_path": "",
            },
        ],
    }

    result = operations_center.import_cashflow_reconciliation(payload, dry_run=False)

    assert result["valid"] is False
    assert result["imported"] is False
    assert operations_db.get_cashflow_reconciliation("2026-06-16", "2026-06-17") is None


def test_cashflow_reconciliation_lint_blocks_partial_evidence(operations_db):
    import operations_center

    payload = {
        "window": {"start": "2026-06-16", "end": "2026-06-17"},
        "cash_reconciliation": {
            "unexplained_cash_effect": -35089.6,
            "tolerance": 339.22,
        },
        "evidence_items": [
            {
                "date": "2026-06-17",
                "type": "cash_transfer_out",
                "cash_effect": -1000,
                "amount": 1000,
                "evidence_path": "/evidence/broker_statement.pdf",
            },
        ],
    }

    result = operations_center.lint_cashflow_reconciliation_payload(payload)

    assert result["valid"] is False
    assert result["remaining_gap"] == -34089.6
    assert any("remaining_gap" in error for error in result["errors"])


def test_sample_readiness_report_groups_current_pending_and_blockers(operations_db, monkeypatch):
    import operations_center

    monkeypatch.setattr(
        "alpha_validation.build_alpha_validation_report",
        lambda: {
            "samples": {"sample_count": 3, "required_sample_count": 50},
        },
    )
    monkeypatch.setattr(
        "auto_gate.diagnose_signal_settlements",
        lambda as_of=None: {
            "as_of": as_of or "2026-07-09",
            "current_strategy_version": "v15.0",
            "pending": 4,
            "due": 2,
            "details": [
                {
                    "id": 1,
                    "code": "002281",
                    "strategy_version": "v15.0",
                    "due": False,
                    "exit_date": "2026-07-15",
                    "ready_to_settle": False,
                    "reasons": ["awaiting_exit_date"],
                },
                {
                    "id": 2,
                    "code": "000938",
                    "strategy_version": "v15.0",
                    "due": True,
                    "exit_date": "2026-07-08",
                    "ready_to_settle": True,
                    "reasons": [],
                },
                {
                    "id": 3,
                    "code": "600585",
                    "strategy_version": "v15.0",
                    "due": True,
                    "exit_date": "2026-07-08",
                    "ready_to_settle": False,
                    "reasons": ["missing_stock_exit"],
                },
                {
                    "id": 4,
                    "code": "600900",
                    "strategy_version": "legacy_unversioned",
                    "due": True,
                    "exit_date": "2026-07-08",
                    "ready_to_settle": False,
                    "reasons": ["legacy_unversioned"],
                },
            ],
        },
    )

    result = operations_center.build_sample_readiness_report(as_of="2026-07-09")

    assert result["status"] == "settlement_ready"
    assert result["sample_count"] == 3
    assert result["remaining_required"] == 47
    assert result["pending_current_version"] == 3
    assert result["pending_legacy_or_other_version"] == 1
    assert result["ready_to_settle_current_version"] == 1
    assert result["blocked_due_current_version"] == 1
    assert result["forecast_by_exit_date"] == {"2026-07-15": 1}
    assert result["blocked_reason_counts"]["missing_stock_exit"] == 1
    assert result["blocked_reason_counts"]["legacy_unversioned"] == 1


def test_sample_readiness_report_flags_version_mismatch_without_current_pending(operations_db, monkeypatch):
    import operations_center

    monkeypatch.setattr(
        "alpha_validation.build_alpha_validation_report",
        lambda: {
            "samples": {"sample_count": 0, "required_sample_count": 50},
        },
    )
    monkeypatch.setattr(
        "auto_gate.diagnose_signal_settlements",
        lambda as_of=None: {
            "as_of": "2026-07-09",
            "current_strategy_version": "v15.0",
            "pending": 2,
            "due": 0,
            "details": [
                {
                    "id": 1,
                    "code": "002281",
                    "strategy_version": "v13.0",
                    "due": False,
                    "exit_date": "2026-07-15",
                    "ready_to_settle": False,
                    "reasons": ["awaiting_exit_date"],
                },
                {
                    "id": 2,
                    "code": "000938",
                    "strategy_version": "v13.0",
                    "due": False,
                    "exit_date": "2026-07-15",
                    "ready_to_settle": False,
                    "reasons": ["awaiting_exit_date"],
                },
            ],
        },
    )

    result = operations_center.build_sample_readiness_report()

    assert result["status"] == "version_mismatch_no_current_pending"
    assert result["pending_current_version"] == 0
    assert result["pending_by_strategy_version"] == {"v13.0": 2}
    assert "python3 cli.py signal" in result["next_commands"]


def test_sample_readiness_report_tracks_required_price_rows(operations_db, monkeypatch):
    import operations_center

    conn = operations_db.get_conn()
    conn.executemany(
        """
        INSERT INTO price_history
            (code, date, open, close, adjustment_mode, quality_status)
        VALUES (?, ?, ?, ?, 'raw', 'high')
        """,
        [
            ("002281", "2026-07-08", 10.0, 10.5),
            ("000905", "2026-07-08", 100.0, 101.0),
        ],
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(
        "alpha_validation.build_alpha_validation_report",
        lambda: {
            "samples": {"sample_count": 3, "required_sample_count": 50},
        },
    )
    monkeypatch.setattr(
        "auto_gate.diagnose_signal_settlements",
        lambda as_of=None: {
            "as_of": "2026-07-09",
            "current_strategy_version": "v15.0",
            "pending": 1,
            "due": 0,
            "details": [
                {
                    "id": 10,
                    "code": "002281",
                    "benchmark_code": "000905",
                    "strategy_version": "v15.0",
                    "due": False,
                    "entry_date": "2026-07-08",
                    "exit_date": "2026-07-10",
                    "ready_to_settle": False,
                    "reasons": ["awaiting_exit_date"],
                },
            ],
        },
    )

    result = operations_center.build_sample_readiness_report(as_of="2026-07-09")

    assert result["required_collection_codes"] == ["000905", "002281"]
    assert result["missing_from_default_collection"] == []
    assert result["price_row_status_counts"] == {"future": 2, "ready": 2}
    assert result["due_price_rows_today"] == []
    assert result["missing_due_price_rows"] == []
    assert result["next_required_price_dates"] == ["2026-07-10"]
    assert result["next_record_date"] == "2026-07-10"
    assert result["next_record_command"] == "python3 cli.py record-real-data --as-of 2026-07-10"
    assert (
        result["dry_run_record_command"]
        == "python3 cli.py record-real-data --dry-run --as-of 2026-07-10"
    )
    rows = result["details"]["required_price_rows"]
    assert {row["role"]: row["status"] for row in rows} == {
        "stock_entry": "ready",
        "benchmark_entry": "ready",
        "stock_exit": "future",
        "benchmark_exit": "future",
    }


def test_sample_readiness_report_flags_missing_due_price_rows(operations_db, monkeypatch):
    import operations_center

    monkeypatch.setattr(
        "alpha_validation.build_alpha_validation_report",
        lambda: {
            "samples": {"sample_count": 3, "required_sample_count": 50},
        },
    )
    monkeypatch.setattr(
        "auto_gate.diagnose_signal_settlements",
        lambda as_of=None: {
            "as_of": "2026-07-10",
            "current_strategy_version": "v15.0",
            "pending": 1,
            "due": 0,
            "details": [
                {
                    "id": 10,
                    "code": "002281",
                    "benchmark_code": "000905",
                    "strategy_version": "v15.0",
                    "due": False,
                    "entry_date": "2026-07-10",
                    "exit_date": "2026-07-17",
                    "ready_to_settle": False,
                    "reasons": ["awaiting_exit_date"],
                },
            ],
        },
    )

    result = operations_center.build_sample_readiness_report(as_of="2026-07-10")

    assert len(result["due_price_rows_today"]) == 2
    assert len(result["missing_due_price_rows"]) == 2
    assert {row["role"] for row in result["missing_due_price_rows"]} == {
        "stock_entry",
        "benchmark_entry",
    }
    assert result["price_row_status_counts"]["missing"] == 2
    assert result["next_record_date"] == "2026-07-10"
    assert result["next_record_command"] == "python3 cli.py record-real-data --as-of 2026-07-10"


def test_risk_tasks_escalate_real_samples_when_due_price_rows_missing(operations_db, monkeypatch):
    import operations_center

    monkeypatch.setattr(
        "alpha_validation.build_alpha_validation_report",
        lambda: {
            "verdict": "P0_NOT_PROVEN",
            "criteria": [
                {"key": "real_samples", "status": "INSUFFICIENT", "label": "samples"},
            ],
            "portfolio": {"broker_candidate": {"points": 2, "required_points": 2}},
            "data_quality": {"drawdown_cashflow": {"status": "PASS"}},
            "samples": {"sample_count": 3, "required_sample_count": 50},
        },
    )
    monkeypatch.setattr(
        operations_center,
        "build_sample_readiness_report",
        lambda: {
            "status": "awaiting_exit_date",
            "missing_due_price_rows": [
                {"code": "002281", "date": "2026-07-10", "role": "stock_entry", "status": "missing"},
            ],
            "next_record_date": "2026-07-10",
            "next_record_command": "python3 cli.py record-real-data --as-of 2026-07-10",
            "dry_run_record_command": "python3 cli.py record-real-data --dry-run --as-of 2026-07-10",
        },
    )

    tasks = operations_center.generate_risk_tasks(
        broker=_broker_snapshot(),
        quality={"low_confidence": 0, "missing": 0, "conflicts": 0, "warnings": [], "settlements": {}},
        reconciliation={"status": "matched"},
        dry_run=True,
    )

    task = next(item for item in tasks if item["dedupe_key"] == "p0:real_samples")
    assert task["severity"] == "critical"
    assert "1 due price rows missing or invalid" in task["summary"]


def test_p0_evidence_status_orders_next_actions(operations_db, monkeypatch):
    import operations_center

    monkeypatch.setattr(
        "alpha_validation.build_alpha_validation_report",
        lambda: {
            "verdict": "P0_DATA_INVALID",
            "strategy": {"version": "v15.0"},
            "criteria": [
                {"key": "nav_data_quality", "label": "NAV valid", "status": "BLOCK"},
                {"key": "real_samples", "label": "samples", "status": "INSUFFICIENT"},
            ],
        },
    )
    monkeypatch.setattr(
        operations_center,
        "build_broker_snapshot_template",
        lambda: {
            "status": "needs_broker_snapshot",
            "current_points": 1,
            "required_points": 2,
            "missing_points": 1,
            "latest_snapshot_at": "2026-07-02 23:10:00",
            "risk_reasons": ["broker_snapshot_stale"],
        },
    )
    monkeypatch.setattr(
        operations_center,
        "build_cashflow_reconciliation_template",
        lambda: {
            "status": "needs_cashflow_reconciliation",
            "window": "2026-06-16~2026-06-17",
            "direction": "unexplained_cash_outflow",
            "gap": -35089.6,
            "tolerance": 339.22,
        },
    )
    monkeypatch.setattr(
        operations_center,
        "build_sample_readiness_report",
        lambda as_of=None: {
            "as_of": as_of or "2026-07-10",
            "status": "awaiting_exit_date",
            "sample_count": 0,
            "required_sample_count": 50,
            "pending_current_version": 3,
            "pending_by_strategy_version": {"v15.0": 3},
            "price_row_status_counts": {"missing": 2},
            "next_required_price_dates": ["2026-07-17"],
            "required_collection_codes": ["002281", "000905"],
            "missing_due_price_rows": [
                {"code": "002281", "date": "2026-07-10", "role": "stock_entry", "status": "missing"},
            ],
        },
    )

    result = operations_center.build_p0_evidence_status(as_of="2026-07-10")

    assert result["status"] == "P0_DATA_INVALID"
    assert [item["key"] for item in result["next_actions"][:4]] == [
        "broker_snapshot",
        "drawdown_cashflow",
        "sample_price_rows",
        "sample_count",
    ]
    assert result["next_actions"][0]["command"] == "python3 cli.py broker-snapshot-template --write"
    assert (
        result["next_actions"][1]["dry_run_import_command"]
        == "python3 cli.py cashflow-reconciliation <json-file> --dry-run"
    )
    assert (
        result["next_actions"][1]["import_command"]
        == "python3 cli.py cashflow-reconciliation <json-file>"
    )
    assert result["next_actions"][2]["command"] == "python3 cli.py record-real-data --as-of 2026-07-10"
    assert (
        result["next_actions"][2]["dry_run_command"]
        == "python3 cli.py record-real-data --dry-run --as-of 2026-07-10"
    )
    assert result["samples"]["missing_due_price_rows"][0]["code"] == "002281"


def test_p0_evidence_status_recommends_signal_when_no_current_pending(operations_db, monkeypatch):
    import operations_center

    monkeypatch.setattr(
        "alpha_validation.build_alpha_validation_report",
        lambda: {
            "verdict": "P0_NOT_PROVEN",
            "strategy": {"version": "v15.0"},
            "criteria": [
                {"key": "real_samples", "label": "samples", "status": "INSUFFICIENT"},
            ],
        },
    )
    monkeypatch.setattr(
        operations_center,
        "build_broker_snapshot_template",
        lambda: {"status": "ready", "current_points": 2, "required_points": 2, "missing_points": 0},
    )
    monkeypatch.setattr(
        operations_center,
        "build_cashflow_reconciliation_template",
        lambda: {"status": "not_needed"},
    )
    monkeypatch.setattr(
        operations_center,
        "build_sample_readiness_report",
        lambda as_of=None: {
            "as_of": "2026-07-09",
            "status": "version_mismatch_no_current_pending",
            "sample_count": 0,
            "required_sample_count": 50,
            "pending_current_version": 0,
            "pending_by_strategy_version": {"v13.0": 10},
            "price_row_status_counts": {},
            "next_required_price_dates": [],
            "missing_due_price_rows": [],
        },
    )

    result = operations_center.build_p0_evidence_status()

    assert result["next_actions"][0]["key"] == "current_version_signals"
    assert result["next_actions"][0]["command"] == "python3 cli.py signal"


def test_p0_evidence_status_uses_dated_sample_collection_command(operations_db, monkeypatch):
    import operations_center

    monkeypatch.setattr(
        "alpha_validation.build_alpha_validation_report",
        lambda: {
            "verdict": "P0_NOT_PROVEN",
            "strategy": {"version": "v15.0"},
            "criteria": [
                {"key": "real_samples", "label": "samples", "status": "INSUFFICIENT"},
            ],
        },
    )
    monkeypatch.setattr(
        operations_center,
        "build_broker_snapshot_template",
        lambda: {"status": "ready", "current_points": 2, "required_points": 2, "missing_points": 0},
    )
    monkeypatch.setattr(
        operations_center,
        "build_cashflow_reconciliation_template",
        lambda: {"status": "not_needed"},
    )
    monkeypatch.setattr(
        operations_center,
        "build_sample_readiness_report",
        lambda as_of=None: {
            "as_of": "2026-07-09",
            "status": "awaiting_exit_date",
            "sample_count": 0,
            "required_sample_count": 50,
            "pending_current_version": 3,
            "pending_by_strategy_version": {"v15.0": 3},
            "price_row_status_counts": {"future": 12},
            "next_required_price_dates": ["2026-07-10", "2026-07-17"],
            "next_record_date": "2026-07-10",
            "next_record_command": "python3 cli.py record-real-data --as-of 2026-07-10",
            "dry_run_record_command": "python3 cli.py record-real-data --dry-run --as-of 2026-07-10",
            "required_collection_codes": ["000300", "600585"],
            "missing_due_price_rows": [],
        },
    )

    result = operations_center.build_p0_evidence_status()

    action = result["next_actions"][0]
    assert action["key"] == "sample_collection_calendar"
    assert action["command"] == "python3 cli.py record-real-data --as-of 2026-07-10"
    assert action["dry_run_command"] == "python3 cli.py record-real-data --dry-run --as-of 2026-07-10"
    assert result["commands"]["record_real_data"] == "python3 cli.py record-real-data --as-of 2026-07-10"


def test_concentration_recovery_plan_is_reference_only():
    import operations_center

    plan = operations_center.build_concentration_recovery_plan(_broker_snapshot())

    assert plan["manual_confirmation_required"] is True
    assert plan["creates_orders"] is False
    assert plan["current_invested_pct"] == pytest.approx(98.72, abs=0.01)
    assert plan["reference_auto_pool_pct"] == 30.0
    assert len(plan["positions"]) == 2
    assert plan["positions"][0]["excess_value_above_single_limit"] > 0


def test_dashboard_merges_fresh_risk_tasks_with_persisted_tasks(operations_db, monkeypatch):
    import operations_center

    broker = _broker_snapshot()
    operations_db.save_portfolio_reconciliation(broker)
    operations_db.upsert_operational_task({
        "dedupe_key": "paper:manual_review",
        "task_type": "paper",
        "severity": "warning",
        "title": "Review PAPER note",
        "summary": "persisted task",
    })
    monkeypatch.setattr(operations_center, "_portfolio_facts", lambda: _internal_snapshot())

    result = operations_center.get_dashboard_data()

    keys = {task["dedupe_key"] for task in result["tasks"]}
    assert "paper:manual_review" in keys
    assert "risk:evidence" in keys
    assert "risk:concentration:000938" in keys


class _FakePaperTrader:
    def __init__(self):
        self.executions = []

    def execute_signal(self, code, action, price, shares=0, amount=0, reason=""):
        self.executions.append((code, action, price, shares, amount))
        return {"status": action, "code": code, "shares": shares}

    def get_paper_portfolio(self):
        return {
            "total_value": 51000,
            "total_profit_amount": 1000,
            "total_profit_pct": 2,
        }


def test_paper_drill_allows_one_buy_and_is_idempotent(operations_db):
    import operations_center

    trader = _FakePaperTrader()
    plan = {
        "date": "2026-07-03",
        "sells": [],
        "buys": [
            {"code": "600900", "price": 26.95, "shares": 100, "amount": 2695},
            {"code": "000988", "price": 156.2, "shares": 100, "amount": 15620},
        ],
    }

    first = operations_center.run_paper_drill(
        plan=plan, trader=trader, run_date="2026-07-03", dry_run=False,
    )
    second = operations_center.run_paper_drill(
        plan=plan, trader=trader, run_date="2026-07-03", dry_run=False,
    )

    assert first["orders_generated"] == 1
    assert first["orders_filled"] == 1
    assert len(trader.executions) == 1
    assert second["idempotent"] is True
    conn = operations_db.get_conn()
    states = [row[0] for row in conn.execute(
        "SELECT state FROM order_state_log ORDER BY id"
    ).fetchall()]
    conn.close()
    assert states == ["generated", "pending_confirm", "confirmed", "submitted", "filled"]


def test_paper_drill_blocks_invalid_baseline(operations_db):
    import operations_center

    trader = _FakePaperTrader()
    trader.get_paper_portfolio = lambda: {
        "cash": 1000,
        "total_value": 2000,
        "positions": [{"code": "600141", "avg_cost": -1.84, "shares": 100}],
    }
    result = operations_center.run_paper_drill(
        plan={"date": "2026-07-03", "sells": [], "buys": []},
        trader=trader,
        run_date="2026-07-03",
        dry_run=False,
    )

    assert result["status"] == "blocked_invalid_baseline"
    assert result["orders_filled"] == 0
    assert operations_db.get_operational_tasks()[0]["dedupe_key"] == "paper:invalid_baseline"


def test_paper_baseline_archive_preserves_legacy_ledger(operations_db):
    conn = operations_db.get_conn()
    conn.execute(
        """
        INSERT INTO paper_trades
            (code, action, price, quantity, date, reason, trade_amount)
        VALUES ('600141', 'buy', -1.84, 100, '2026-06-25', 'legacy seed', -184)
        """
    )
    conn.commit()
    conn.close()

    result = operations_db.archive_and_clear_paper_account("invalid legacy seed")

    assert result["trades_archived"] == 1
    conn = operations_db.get_conn()
    assert conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0] == 0
    archive = conn.execute("SELECT * FROM paper_baseline_archives").fetchone()
    conn.close()
    assert "600141" in archive["trades_json"]
    assert archive["reason"] == "invalid legacy seed"


def test_history_analytics_calculates_peak_drawdown(operations_db):
    import operations_center

    conn = operations_db.get_conn()
    conn.executemany(
        "INSERT INTO nav_history (date, total_value, profit_pct) VALUES (?, ?, ?)",
        [
            ("2026-07-01", 100, 0),
            ("2026-07-02", 120, 20),
            ("2026-07-03", 108, 8),
        ],
    )
    conn.commit()
    conn.close()

    result = operations_center.get_history_analytics()

    assert result["nav"]["peak_value"] == 120
    assert result["nav"]["current_drawdown_pct"] == -10
    assert result["nav"]["max_drawdown_pct"] == -10
