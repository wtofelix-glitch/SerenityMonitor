"""Dashboard auto-gate presentation tests."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_auto_gate_card_exposes_compliance_flow(monkeypatch):
    import auto_gate
    import monitoring_dashboard

    monkeypatch.setattr(auto_gate, "get_latest_gate_result", lambda: {
        "state": "MANUAL",
        "gate_passed": True,
        "strategy_version": "v1.0",
        "sample_count": 50,
        "win_rate": 0.66,
        "wilson_lower": 0.52,
        "excess_win_rate": 0.58,
        "avg_excess_5d": 0.8,
        "consecutive_loss_ok": True,
        "compliance_status": "reported_pending_review",
        "max_state": "MANUAL",
        "reasons": [],
        "explain": {},
    })

    card = monitoring_dashboard._get_auto_gate_card()

    assert card["compliance_status"] == "reported_pending_review"
    assert [step["id"] for step in card["compliance_flow"]] == [
        "not_reported",
        "reported_pending_review",
        "approved",
    ]
    assert card["compliance_flow"][0]["done"] is True
    assert card["compliance_flow"][1]["active"] is True
    assert card["compliance_flow"][2]["done"] is False


def test_auto_gate_card_exposes_recent_data_quality_warnings(monkeypatch):
    import auto_gate
    import db
    import monitoring_dashboard

    monkeypatch.setattr(auto_gate, "get_latest_gate_result", lambda: {
        "state": "LOCKED",
        "gate_passed": False,
        "sample_count": 0,
        "reasons": [],
        "explain": {},
    })
    monkeypatch.setattr(db, "get_latest_data_quality_logs", lambda limit=10: [
        {"code": "002281", "date": "2026-06-25", "quality_status": "low", "warning": "source conflict", "conflict_pct": 1.7},
        {"code": "600487", "date": "2026-06-25", "quality_status": "high", "warning": "", "conflict_pct": 0.0},
    ])

    card = monitoring_dashboard._get_auto_gate_card()

    assert card["data_quality_warnings"] == [{
        "code": "002281",
        "date": "2026-06-25",
        "quality_status": "low",
        "warning": "source conflict",
        "conflict_pct": 1.7,
    }]
