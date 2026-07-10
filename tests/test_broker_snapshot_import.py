"""Validated, idempotent broker-evidence ingestion."""

from __future__ import annotations

import os
import tempfile
from datetime import datetime

import pytest


@pytest.fixture
def snapshot_db(monkeypatch):
    import db

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    path = tmp.name
    tmp.close()
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    yield db
    os.unlink(path)


def _snapshot(**overrides):
    payload = {
        "snapshot_at": "2026-07-04 15:30:00",
        "source": "broker_manual",
        "total_assets": 60000.0,
        "holdings_value": 50000.0,
        "cash": 10000.0,
        "daily_profit": 600.0,
        "daily_profit_pct": 1.0,
        "positions": [
            {"code": "000938", "name": "紫光股份", "shares": 1000, "market_value": 30000.0},
            {"code": "600585", "name": "海螺水泥", "shares": 1000, "market_value": 20000.0},
        ],
    }
    payload.update(overrides)
    return payload


def _internal(payload):
    return {
        "total_value": payload["total_assets"],
        "holdings_value": payload["holdings_value"],
        "cash": payload["cash"],
        "positions": payload["positions"],
    }


def test_valid_snapshot_is_saved_and_idempotent(snapshot_db):
    import operations_center

    payload = _snapshot()
    first = operations_center.import_broker_snapshot(
        payload,
        internal=_internal(payload),
        now=datetime(2026, 7, 4, 15, 35),
    )
    second = operations_center.import_broker_snapshot(
        payload,
        internal=_internal(payload),
        now=datetime(2026, 7, 4, 15, 36),
    )

    assert first["saved"] is True
    assert first["idempotent"] is False
    assert second["saved"] is False
    assert second["idempotent"] is True
    assert second["risk_assessment"]["point_count"] == 1


def test_rounded_legacy_position_ratio_does_not_create_false_conflict(snapshot_db):
    import operations_center

    payload = _snapshot(position_ratio_pct=83.3)
    snapshot_db.save_portfolio_reconciliation(payload)

    result = operations_center.import_broker_snapshot(
        payload,
        internal=_internal(payload),
        now=datetime(2026, 7, 4, 15, 35),
    )

    assert result["idempotent"] is True
    assert result["saved"] is False


def test_same_timestamp_with_different_facts_is_rejected(snapshot_db):
    import operations_center

    payload = _snapshot()
    operations_center.import_broker_snapshot(
        payload, internal=_internal(payload), now=datetime(2026, 7, 4, 15, 35),
    )

    with pytest.raises(ValueError, match="conflicts with existing evidence"):
        operations_center.import_broker_snapshot(
            _snapshot(
                cash=9000,
                holdings_value=51000,
                positions=[
                    {"code": "000938", "shares": 1000, "market_value": 31000},
                    {"code": "600585", "shares": 1000, "market_value": 20000},
                ],
            ),
            now=datetime(2026, 7, 4, 15, 36),
        )


@pytest.mark.parametrize("overrides, message", [
    ({"total_assets": 60000, "cash": 9000, "holdings_value": 50000}, "asset equation"),
    ({"holdings_value": 49000, "cash": 11000}, "position market values"),
    ({"snapshot_at": "2026-07-04 16:00:00"}, "future"),
    ({"positions": [{"code": "300001", "shares": 100, "market_value": 50000}]}, "main-board"),
])
def test_invalid_snapshot_is_rejected(snapshot_db, overrides, message):
    import operations_center

    with pytest.raises(ValueError, match=message):
        operations_center.import_broker_snapshot(
            _snapshot(**overrides), now=datetime(2026, 7, 4, 15, 35),
        )


def test_older_snapshot_cannot_be_inserted_after_newer_evidence(snapshot_db):
    import operations_center

    payload = _snapshot()
    operations_center.import_broker_snapshot(
        payload, internal=_internal(payload), now=datetime(2026, 7, 4, 15, 35),
    )

    with pytest.raises(ValueError, match="older than latest"):
        operations_center.import_broker_snapshot(
            _snapshot(snapshot_at="2026-07-03 15:30:00"),
            now=datetime(2026, 7, 4, 15, 35),
        )


def test_second_snapshot_produces_verified_drawdown(snapshot_db):
    import operations_center

    first = _snapshot(snapshot_at="2026-07-03 15:30:00", total_assets=65000,
                      holdings_value=55000, cash=10000,
                      positions=[{"code": "000938", "shares": 1000, "market_value": 55000}])
    second = _snapshot()
    operations_center.import_broker_snapshot(
        first, internal=_internal(first), now=datetime(2026, 7, 3, 15, 35),
    )
    result = operations_center.import_broker_snapshot(
        second, internal=_internal(second), now=datetime(2026, 7, 4, 15, 35),
    )

    assert result["risk_assessment"]["verified"] is True
    assert result["risk_assessment"]["drawdown_pct"] == pytest.approx(-7.69, abs=0.01)
    assert result["risk_assessment"]["lock_required"] is True


def test_broker_snapshot_template_surfaces_missing_evidence(snapshot_db, monkeypatch):
    import operations_center

    payload = _snapshot()
    snapshot_db.save_portfolio_reconciliation(payload)
    monkeypatch.setattr(operations_center, "_portfolio_facts", lambda: {
        "total_value": 61000,
        "holdings_value": 50000,
        "cash": 11000,
        "positions": [
            {"code": "000938", "name": "紫光股份", "shares": 1000, "current_value": 30000},
        ],
    })

    result = operations_center.build_broker_snapshot_template(
        now=datetime(2026, 7, 5, 15, 30),
    )

    assert result["status"] == "needs_broker_snapshot"
    assert result["current_points"] == 1
    assert result["missing_points"] == 1
    assert result["latest_snapshot_at"] == payload["snapshot_at"]
    assert result["template"]["snapshot_at"] == "2026-07-05 15:30:00"
    assert result["template"]["positions"][0]["code"] == "000938"
    assert result["template"]["positions"][0]["market_value"] is None


def test_broker_snapshot_lint_reports_template_placeholders(snapshot_db):
    import operations_center

    result = operations_center.lint_broker_snapshot_payload({
        "snapshot_at": "2026-07-05 15:30:00",
        "source": "broker_manual",
        "total_assets": None,
        "holdings_value": None,
        "cash": None,
        "positions": [
            {"code": "000938", "shares": 1000, "market_value": None},
        ],
    }, now=datetime(2026, 7, 5, 15, 35))

    assert result["valid"] is False
    assert "missing required field: total_assets" in result["errors"]
    assert "missing required field: holdings_value" in result["errors"]
    assert "missing required field: cash" in result["errors"]
    assert "positions[0].market_value is required" in result["errors"]


def test_broker_snapshot_lint_validates_without_persisting(snapshot_db):
    import operations_center

    payload = _snapshot()
    result = operations_center.lint_broker_snapshot_payload(
        payload,
        now=datetime(2026, 7, 4, 15, 35),
    )

    assert result["valid"] is True
    assert result["errors"] == []
    assert result["normalized"]["total_assets"] == 60000.0
    assert result["next_command"] == "python3 cli.py broker-snapshot <json-file> --dry-run"
    assert snapshot_db.get_latest_portfolio_reconciliation() is None


def test_cli_registers_broker_snapshot_command():
    from pathlib import Path

    source = (Path(__file__).parents[1] / "cli.py").read_text()
    assert '"broker-snapshot": cmd_broker_snapshot' in source
    assert '"broker-snapshot-template": cmd_broker_snapshot_template' in source
    assert '"broker-snapshot-lint": cmd_broker_snapshot_lint' in source
    assert "def cmd_broker_snapshot" in source
    assert "def cmd_broker_snapshot_template" in source
    assert "def cmd_broker_snapshot_lint" in source
    assert "broker-snapshot-template [--write]" in source
    assert "broker-snapshot-lint <json-file>" in source
    assert "written_path" in source
