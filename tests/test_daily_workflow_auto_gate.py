"""Daily workflow integration tests for the real-data auto gate."""

from __future__ import annotations

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_real_data_gate_step_skips_non_trading_day(monkeypatch, capsys):
    import auto_gate
    import check_trading_day
    import daily_workflow

    calls = []
    monkeypatch.setattr(check_trading_day, "is_trading_day", lambda: False)
    monkeypatch.setattr(auto_gate, "record_real_data", lambda dry_run=False: (_ for _ in ()).throw(AssertionError("must not record on a weekend")))
    monkeypatch.setattr(auto_gate, "settle_pending_signal_outcomes", lambda dry_run=False: calls.append("settle") or {"dry_run": dry_run, "settled": 0, "pending": 3, "expired_unsettled": 0, "non_executable": 0, "reason_counts": {"awaiting_exit_date": 3}})
    monkeypatch.setattr(auto_gate, "evaluate_auto_gate", lambda explain=False: calls.append("gate") or {"state": "LOCKED", "sample_count": 0})
    monkeypatch.setattr(auto_gate, "format_gate_report", lambda result: "gate LOCKED")

    result = daily_workflow.run_real_data_gate_step(dry_run=True)

    assert result["skipped"] is False
    assert result["record"]["skipped"] is True
    assert calls == ["settle", "gate"]
    assert "非交易日" in capsys.readouterr().out


def test_daily_workflow_stops_before_scoring_on_non_trading_day(monkeypatch, capsys):
    import check_trading_day
    import daily_workflow

    audit_calls = []
    monkeypatch.setattr(check_trading_day, "is_trading_day", lambda: False)
    monkeypatch.setattr(daily_workflow, "run_real_data_gate_step", lambda dry_run=False: {
        "skipped": False,
        "record": {"skipped": True},
        "settle": {"settled": 0, "pending": 3},
        "gate": {"state": "LOCKED"},
    })
    monkeypatch.setattr(
        daily_workflow,
        "run_operations_audit_step",
        lambda dry_run=False: audit_calls.append(dry_run) or {
            "dry_run": dry_run,
            "tasks": [],
            "critical": 0,
            "warning": 0,
            "p0_task_count": 0,
        },
    )
    monkeypatch.setattr(daily_workflow.sys, "argv", ["daily_workflow.py", "--dry-run"])

    daily_workflow.main()

    output = capsys.readouterr().out
    assert audit_calls == [True]
    assert "非交易日维护完成" in output
    assert "多因子评分" not in output


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


def test_real_data_audit_push_summarizes_record_settle_and_gate(monkeypatch):
    import daily_workflow
    import notifier

    sent = []
    monkeypatch.setattr(notifier, "send_message", lambda title, content, content_type="markdown", summary="": sent.append((title, content, content_type, summary)))
    result = {
        "record": {"saved": 15, "count": 15, "low_quality": [], "missing": [], "source_errors": {}},
        "settle": {"settled": 2, "pending": 8, "expired_unsettled": 1, "non_executable": 0},
        "gate": {"state": "LOCKED", "sample_count": 12, "required_sample_count": 50, "win_rate": 0.58, "wilson_lower": 0.42, "compliance_status": "reported_pending_review"},
    }

    ok = daily_workflow.send_real_data_audit_push(result, today="2026-06-25")

    assert ok is True
    assert sent
    title, content, content_type, summary = sent[0]
    assert "真实数据审计" in title
    assert "记录: 15/15" in content
    assert "结算: 2" in content
    assert "expired_unsettled: 1" in content
    assert "LOCKED" in content
    assert "12/50" in content
    assert content_type == "markdown"
    assert "LOCKED" in summary


def test_real_data_audit_push_skips_when_step_skipped(monkeypatch):
    import daily_workflow
    import notifier

    monkeypatch.setattr(notifier, "send_message", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not send")))

    assert daily_workflow.send_real_data_audit_push({"skipped": True}, today="2026-06-25") is False


def test_operations_audit_step_refreshes_p0_tasks(monkeypatch, capsys):
    import daily_workflow
    import operations_center

    calls = []
    monkeypatch.setattr(
        operations_center,
        "run_reconciliation",
        lambda dry_run=False: calls.append(("reconciliation", dry_run)) or {"status": "matched"},
    )
    monkeypatch.setattr(
        operations_center,
        "get_data_quality_summary",
        lambda: calls.append(("quality", None)) or {"low_confidence": 0, "missing": 0, "conflicts": 0},
    )
    monkeypatch.setattr(
        operations_center,
        "generate_risk_tasks",
        lambda quality=None, reconciliation=None, dry_run=False: calls.append(("tasks", dry_run)) or [
            {"dedupe_key": "p0:alpha_validation", "severity": "critical"},
            {"dedupe_key": "p0:real_samples", "severity": "warning"},
        ],
    )

    result = daily_workflow.run_operations_audit_step(dry_run=False)

    assert calls == [
        ("reconciliation", False),
        ("quality", None),
        ("tasks", False),
    ]
    assert result["critical"] == 1
    assert result["warning"] == 1
    assert result["p0_task_count"] == 2
    assert "P0阻断" in capsys.readouterr().out


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
        "buys": [{
            "code": "002281", "price": 10, "shares": 100,
            "amount": 1000, "reason": "edge",
            "net_edge": {"ready": True, "samples": 10, "net_pct": 0.7},
        }],
    }
    result = daily_workflow.run_controlled_execution_step(plan)

    assert result["blocked"] is False
    assert result["staged"] == 2
    assert [item[0][2] for item in staged] == ["generated", "pending_confirm", "generated", "pending_confirm"]


def test_controlled_execution_skips_buys_without_positive_net_edge(monkeypatch):
    import auto_gate
    import daily_workflow

    staged = []
    monkeypatch.setattr(auto_gate, "evaluate_auto_gate", lambda explain=False: {"state": "SEMI_AUTO", "gate_passed": True, "compliance_status": "approved"})
    monkeypatch.setattr(auto_gate, "format_gate_report", lambda gate: "gate SEMI_AUTO")
    monkeypatch.setattr(auto_gate, "create_order_state", lambda *args, **kwargs: staged.append((args, kwargs)) or {"state": args[2]})
    monkeypatch.setattr(auto_gate, "estimate_net_expected_return", lambda code, action: {
        "ready": False,
        "samples": 0,
        "gross_pct": None,
        "net_pct": None,
    })

    plan = {
        "date": "2026-06-25",
        "sells": [{"code": "600487", "shares": 100, "estimated_proceeds": 9500, "reasons": ["risk"]}],
        "buys": [{"code": "002281", "price": 10, "shares": 100, "amount": 1000, "reason": "edge"}],
    }
    result = daily_workflow.run_controlled_execution_step(plan)

    assert result["blocked"] is False
    assert result["staged"] == 1
    assert result["skipped_buys"] == [{
        "code": "002281",
        "reason": "net_edge_not_ready",
        "samples": 0,
        "net_pct": None,
    }]
    assert [item[0][1] for item in staged] == ["SELL", "SELL"]


def test_ths_live_step_blocks_when_p0_gate_is_locked(monkeypatch):
    import auto_gate
    import daily_workflow

    bridge_calls = []
    monkeypatch.setattr(auto_gate, "evaluate_auto_gate", lambda explain=False: {
        "state": "LOCKED",
        "p0_alpha": {"verdict": "P0_DATA_INVALID"},
    })
    monkeypatch.setattr(auto_gate, "format_gate_report", lambda gate: "gate LOCKED")
    monkeypatch.setitem(
        sys.modules,
        "ths_bridge",
        types.SimpleNamespace(
            auto_execute_to_ths=lambda *args, **kwargs: bridge_calls.append((args, kwargs)) or {}
        ),
    )

    result = daily_workflow.run_ths_live_step({
        "sells": [],
        "buys": [{"code": "002281", "price": 10, "shares": 100, "amount": 1000}],
    })

    assert result["blocked"] is True
    assert result["gate"]["state"] == "LOCKED"
    assert bridge_calls == []


def test_ths_live_step_dry_run_uses_bridge_without_order_files(monkeypatch):
    import auto_gate
    import daily_workflow

    bridge_calls = []
    monkeypatch.setattr(auto_gate, "evaluate_auto_gate", lambda explain=False: {"state": "LOCKED"})
    monkeypatch.setattr(auto_gate, "format_gate_report", lambda gate: "gate LOCKED")
    monkeypatch.setitem(
        sys.modules,
        "ths_bridge",
        types.SimpleNamespace(
            auto_execute_to_ths=lambda plan, dry_run=False: bridge_calls.append((plan, dry_run)) or {
                "buys": [{"code": "002281", "action": "buy", "status": "dry_run"}],
                "sells": [],
            }
        ),
    )

    result = daily_workflow.run_ths_live_step({
        "sells": [],
        "buys": [{"code": "002281", "price": 10, "shares": 900, "amount": 9000}],
    }, dry_run=True)

    assert result["blocked"] is False
    assert bridge_calls[0][1] is True
    assert bridge_calls[0][0]["buys"][0]["amount"] == 5000
