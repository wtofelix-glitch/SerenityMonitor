"""Regression tests for guarded CLI auto execution helpers."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_stage_order_states_skips_buy_without_net_edge(monkeypatch):
    import auto_gate
    import cli

    staged = []
    monkeypatch.setattr(
        auto_gate,
        "create_order_state",
        lambda *args, **kwargs: staged.append((args, kwargs)) or {"state": args[2]},
    )
    monkeypatch.setattr(
        auto_gate,
        "estimate_net_expected_return",
        lambda code, signal: {
            "ready": False,
            "samples": 0,
            "net_pct": None,
            "reason": "insufficient_same_version_samples",
        },
    )

    staged_count, skipped_buys = cli._stage_order_states({
        "date": "2026-07-09",
        "sells": [{"code": "600487", "shares": 100, "estimated_proceeds": 9500}],
        "buys": [{"code": "002281", "price": 10, "shares": 100, "amount": 1000}],
    })

    assert staged_count == 1
    assert [item[0][1] for item in staged] == ["SELL", "SELL"]
    assert skipped_buys == [{
        "code": "002281",
        "samples": 0,
        "net_pct": None,
        "reason": "insufficient_same_version_samples",
    }]


def test_record_real_data_cli_passes_as_of(monkeypatch, capsys):
    import auto_gate
    import cli

    calls = {}

    def fake_record_real_data(dry_run=False, as_of=None):
        calls["record"] = {"dry_run": dry_run, "as_of": as_of}
        return {
            "dry_run": dry_run,
            "date": as_of,
            "count": 0,
            "saved": 0,
            "low_quality": [],
            "missing": [],
            "source_date_mismatches": [],
            "source_errors": {},
            "records": [],
        }

    def fake_settle_pending_signal_outcomes(dry_run=False, as_of=None):
        calls["settle"] = {"dry_run": dry_run, "as_of": as_of}
        return {
            "dry_run": dry_run,
            "settled": 0,
            "pending": 0,
            "expired_unsettled": 0,
            "non_executable": 0,
            "reason_counts": {},
        }

    monkeypatch.setattr(auto_gate, "record_real_data", fake_record_real_data)
    monkeypatch.setattr(auto_gate, "settle_pending_signal_outcomes", fake_settle_pending_signal_outcomes)
    monkeypatch.setattr(sys, "argv", ["cli.py", "record-real-data", "--dry-run", "--as-of", "2026-07-10"])

    cli.cmd_record_real_data()

    assert calls == {
        "record": {"dry_run": True, "as_of": "2026-07-10"},
        "settle": {"dry_run": True, "as_of": "2026-07-10"},
    }
    assert "date=2026-07-10" in capsys.readouterr().out
