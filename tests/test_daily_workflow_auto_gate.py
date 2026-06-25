"""Daily workflow integration tests for the real-data auto gate."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_real_data_gate_step_skips_non_trading_day(monkeypatch, capsys):
    import check_trading_day
    import daily_workflow

    monkeypatch.setattr(check_trading_day, "is_trading_day", lambda: False)

    result = daily_workflow.run_real_data_gate_step(dry_run=True)

    assert result["skipped"] is True
    assert "非交易日" in capsys.readouterr().out


def test_real_data_gate_step_records_settles_and_evaluates(monkeypatch):
    import auto_gate
    import check_trading_day
    import daily_workflow

    calls = []
    monkeypatch.setattr(check_trading_day, "is_trading_day", lambda: True)
    monkeypatch.setattr(auto_gate, "record_real_data", lambda dry_run=False: calls.append(("record", dry_run)) or {"dry_run": dry_run, "saved": 2, "count": 2, "low_quality": [], "missing": [], "source_errors": {}})
    monkeypatch.setattr(auto_gate, "settle_pending_signal_outcomes", lambda dry_run=False: calls.append(("settle", dry_run)) or {"dry_run": dry_run, "settled": 1, "pending": 0, "expired_unsettled": 0, "non_executable": 0})
    monkeypatch.setattr(auto_gate, "evaluate_auto_gate", lambda explain=False: calls.append(("gate", explain)) or {"state": "PAPER", "gate_passed": False, "sample_count": 1})
    monkeypatch.setattr(auto_gate, "format_record_report", lambda result: f"recorded {result['saved']}")
    monkeypatch.setattr(auto_gate, "format_gate_report", lambda result: f"gate {result['state']}")

    result = daily_workflow.run_real_data_gate_step(dry_run=True)

    assert calls == [("record", True), ("settle", True), ("gate", True)]
    assert result["record"]["saved"] == 2
    assert result["settle"]["settled"] == 1
    assert result["gate"]["state"] == "PAPER"


def test_controlled_execution_blocks_below_semi_auto(monkeypatch):
    import auto_gate
    import daily_workflow

    monkeypatch.setattr(auto_gate, "evaluate_auto_gate", lambda explain=False: {"state": "MANUAL", "gate_passed": True, "compliance_status": "not_reported"})
    monkeypatch.setattr(auto_gate, "format_gate_report", lambda gate: "gate MANUAL")

    result = daily_workflow.run_controlled_execution_step({"date": "2026-06-25", "sells": [], "buys": [{"code": "002281", "price": 10, "shares": 100, "amount": 1000}]})

    assert result["blocked"] is True
    assert result["staged"] == 0


def test_controlled_execution_stages_pending_confirm(monkeypatch):
    import auto_gate
    import daily_workflow

    staged = []
    monkeypatch.setattr(auto_gate, "evaluate_auto_gate", lambda explain=False: {"state": "SEMI_AUTO", "gate_passed": True, "compliance_status": "approved"})
    monkeypatch.setattr(auto_gate, "format_gate_report", lambda gate: "gate SEMI_AUTO")
    monkeypatch.setattr(auto_gate, "create_order_state", lambda *args, **kwargs: staged.append((args, kwargs)) or {"state": args[2]})

    plan = {
        "date": "2026-06-25",
        "sells": [{"code": "600487", "shares": 100, "estimated_proceeds": 9500, "reasons": ["risk"]}],
        "buys": [{"code": "002281", "price": 10, "shares": 100, "amount": 1000, "reason": "edge"}],
    }
    result = daily_workflow.run_controlled_execution_step(plan)

    assert result["blocked"] is False
    assert result["staged"] == 2
    assert [item[0][2] for item in staged] == ["generated", "pending_confirm", "generated", "pending_confirm"]
