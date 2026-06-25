"""Auto gate tests for real-data validation and controlled execution."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def gate_db(monkeypatch):
    import db

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp_path = tmp.name
    tmp.close()
    monkeypatch.setattr(db, "DB_PATH", tmp_path)
    db.init_db()
    yield db
    os.unlink(tmp_path)


def _insert_gate_samples(db_module, version: str, wins: int, total: int = 50, latest_bad_run: int = 0):
    start = date(2026, 1, 1)
    conn = db_module.get_conn()
    for i in range(total):
        sample_date = (start + timedelta(days=i)).isoformat()
        is_latest_bad = i >= total - latest_bad_run
        is_win = i < wins and not is_latest_bad
        ret = 1.0 if is_win else -0.4
        excess = 0.8 if is_win else -0.3
        conn.execute(
            """
            INSERT INTO signal_log
                (code, date, time, action, total_score, price,
                 outcome_5d, return_5d, benchmark_return_5d, excess_5d,
                 strategy_version, settlement_status, executable_status,
                 data_quality, adjustment_mode)
            VALUES (?, ?, '14:55', 'BUY', 75, 10,
                    ?, ?, 0.2, ?, ?, 'settled', 'executable',
                    'high', 'raw')
            """,
            (f"600{i % 6:03d}", sample_date, ret, ret, excess, version),
        )
    conn.commit()
    conn.close()


def test_wilson_lower_bound_blocks_nominal_60pct_gate(gate_db):
    import auto_gate

    version = auto_gate.ensure_current_strategy_version()["version"]
    _insert_gate_samples(gate_db, version, wins=30)
    gate_db.set_compliance_status("approved", notes="broker checked")

    result = auto_gate.evaluate_auto_gate()

    assert result["sample_count"] == 50
    assert result["win_rate"] == pytest.approx(0.60)
    assert result["wilson_lower"] < 0.50
    assert result["gate_passed"] is False
    assert result["state"] != "SEMI_AUTO"


def test_compliance_three_state_caps_semi_auto(gate_db):
    import auto_gate

    version = auto_gate.ensure_current_strategy_version()["version"]
    _insert_gate_samples(gate_db, version, wins=33)

    for status in ("not_reported", "reported_pending_review", "rejected"):
        gate_db.set_compliance_status(status)
        result = auto_gate.evaluate_auto_gate()
        assert result["gate_passed"] is True
        assert result["max_state"] == "MANUAL"
        assert result["state"] == "MANUAL"

    gate_db.set_compliance_status("approved")
    result = auto_gate.evaluate_auto_gate()
    assert result["gate_passed"] is True
    assert result["max_state"] == "SEMI_AUTO"
    assert result["state"] == "SEMI_AUTO"


def test_consecutive_loss_rule_is_hashed_and_explained(gate_db):
    import auto_gate

    default_hash = auto_gate.compute_strategy_hash()
    changed = auto_gate.default_strategy_config()
    changed["consecutive_loss_rule"] = {
        "mode": "AND",
        "lookback": 10,
        "max_consecutive": 3,
    }
    assert auto_gate.compute_strategy_hash(changed) != default_hash

    version = auto_gate.ensure_current_strategy_version()["version"]
    _insert_gate_samples(gate_db, version, wins=36, latest_bad_run=3)
    gate_db.set_compliance_status("approved")

    result = auto_gate.evaluate_auto_gate(explain=True)

    assert result["gate_passed"] is False
    assert result["consecutive_loss_ok"] is False
    assert len(result["consecutive_loss_trigger"]) == 3


def test_backtest_adjusted_data_is_diagnostic_only():
    import auto_gate

    assert auto_gate.classify_backtest_price_source("raw") == "gate_eligible"
    assert auto_gate.classify_backtest_price_source("unadjusted") == "gate_eligible"
    assert auto_gate.classify_backtest_price_source("qfq") == "diagnostic_only"
    assert auto_gate.classify_backtest_price_source("hfq") == "diagnostic_only"


def test_constants_and_order_state_spellings_are_explicit():
    import auto_gate

    assert auto_gate.SIGNAL_OUTCOME_EXPIRY_TRADING_DAYS == 15
    assert auto_gate.MAX_HOLDING_TRADING_DAYS == 20
    assert auto_gate.SIGNAL_OUTCOME_EXPIRY_TRADING_DAYS != auto_gate.MAX_HOLDING_TRADING_DAYS
    assert "filled" in auto_gate.ORDER_STATES
    assert "full_filled" not in auto_gate.ORDER_STATES
    assert "cancelled" in auto_gate.ORDER_STATES
    assert "canceled" not in auto_gate.ORDER_STATES
