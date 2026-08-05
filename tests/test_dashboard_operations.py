"""Dashboard contract tests for the daily operations workbench."""
from __future__ import annotations


def test_operations_center_api_exposes_auditable_sections(monkeypatch):
    import operations_center
    import monitoring_dashboard

    expected = {
        "reconciliation": {"status": "matched"},
        "quality": {"high_confidence_pct": 100, "warnings": []},
        "tasks": [],
        "paper": {"status": "completed", "orders_filled": 1},
        "operations_run": {"status": "completed"},
        "analytics": {"nav": {"points": []}, "strategy_versions": []},
    }
    monkeypatch.setattr(operations_center, "get_dashboard_data", lambda: expected)

    client = monitoring_dashboard.app.test_client()
    response = client.get("/api/operations-center")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["data"] == expected
    assert payload["updated"]


def test_operations_frontend_distinguishes_waiting_from_due_blocked():
    from pathlib import Path

    source = (Path(__file__).parents[1] / "static/js/monitor.js").read_text()

    assert "quality.settlement_diagnostics" in source
    assert "settlement.pending" in source
    assert "settlement.due_blocked" in source
    assert "data.risk_recovery" in source
    assert "manual_confirmation_required" in source
