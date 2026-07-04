"""Auto gate tests for real-data validation and controlled execution."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from datetime import date, datetime, timedelta

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
    win_flags = [(i % 5) in (0, 1, 3) for i in range(total)]  # 60%, never 3 losses in a row
    for idx in range(total):
        if sum(win_flags) >= wins:
            break
        if not win_flags[idx] and idx < total - latest_bad_run:
            win_flags[idx] = True
    for idx in range(total - latest_bad_run - 1, -1, -1):
        if sum(win_flags) <= wins:
            break
        if win_flags[idx]:
            win_flags[idx] = False
    for idx in range(total - latest_bad_run, total):
        if idx >= 0:
            win_flags[idx] = False
    conn = db_module.get_conn()
    for i in range(total):
        sample_date = (start + timedelta(days=i)).isoformat()
        is_win = win_flags[i]
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


def _insert_valid_broker_risk_evidence(db_module):
    now = datetime.now().replace(microsecond=0)
    conn = db_module.get_conn()
    _insert_broker_nav(conn, (now - timedelta(days=1)).isoformat(sep=" "), 100)
    _insert_broker_nav(conn, now.isoformat(sep=" "), 101)
    conn.commit()
    conn.close()


def test_wilson_lower_bound_blocks_nominal_60pct_gate(gate_db):
    import auto_gate

    version = auto_gate.ensure_current_strategy_version()["version"]
    _insert_gate_samples(gate_db, version, wins=30)
    gate_db.set_compliance_status("approved", notes="broker checked")

    result = auto_gate.evaluate_auto_gate()

    assert result["sample_count"] >= result["required_sample_count"]
    assert result["win_rate"] == pytest.approx(0.60)
    assert result["wilson_lower"] < 0.35
    assert result["gate_passed"] is False
    assert result["state"] != "SEMI_AUTO"


def test_compliance_three_state_caps_semi_auto(gate_db):
    import auto_gate

    version = auto_gate.ensure_current_strategy_version()["version"]
    _insert_gate_samples(gate_db, version, wins=33)
    _insert_valid_broker_risk_evidence(gate_db)

    for status in ("not_reported", "reported_pending_review", "rejected"):
        gate_db.set_compliance_status(status)
        result = auto_gate.evaluate_auto_gate()
        assert result["max_state"] == "MANUAL"
        assert result["state"] in ("MANUAL", "PAPER")

    gate_db.set_compliance_status("approved")
    result = auto_gate.evaluate_auto_gate()
    assert result["max_state"] == "SEMI_AUTO"


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
    _insert_valid_broker_risk_evidence(gate_db)
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


def _insert_broker_nav(conn, snapshot_at, total_assets, daily_profit_pct=0):
    conn.execute(
        """
        INSERT INTO portfolio_reconciliations
            (snapshot_at, total_assets, holdings_value, cash, daily_profit_pct)
        VALUES (?, ?, ?, 0, ?)
        """,
        (snapshot_at, total_assets, total_assets, daily_profit_pct),
    )


def test_broker_drawdown_requires_two_fresh_points(gate_db):
    import auto_gate

    conn = gate_db.get_conn()
    _insert_broker_nav(conn, "2026-07-04 12:00:00", 100)
    conn.commit()

    result = auto_gate.assess_broker_risk(
        conn=conn, now=datetime(2026, 7, 4, 13, 0),
    )
    conn.close()

    assert result["verified"] is False
    assert result["lock_required"] is True
    assert result["drawdown_pct"] is None
    assert "broker_drawdown_history 1 < 2" in result["reasons"]


def test_broker_drawdown_uses_broker_points_not_internal_nav(gate_db):
    import auto_gate

    conn = gate_db.get_conn()
    conn.executemany(
        "INSERT INTO nav_history(date,total_value) VALUES (?,?)",
        [("2026-07-02", 100), ("2026-07-03", 200), ("2026-07-04", 150)],
    )
    _insert_broker_nav(conn, "2026-07-03 15:30:00", 100)
    _insert_broker_nav(conn, "2026-07-04 15:30:00", 102)
    conn.commit()

    result = auto_gate.assess_broker_risk(
        conn=conn, now=datetime(2026, 7, 4, 16, 0),
    )
    conn.close()

    assert result["verified"] is True
    assert result["drawdown_pct"] == 0
    assert result["lock_required"] is False


def test_verified_broker_drawdown_triggers_lock(gate_db):
    import auto_gate

    conn = gate_db.get_conn()
    _insert_broker_nav(conn, "2026-07-02 15:30:00", 100)
    _insert_broker_nav(conn, "2026-07-03 15:30:00", 120)
    _insert_broker_nav(conn, "2026-07-04 15:30:00", 108)
    conn.commit()

    result = auto_gate.assess_broker_risk(
        conn=conn, now=datetime(2026, 7, 4, 16, 0),
    )
    conn.close()

    assert result["verified"] is True
    assert result["drawdown_pct"] == pytest.approx(-10)
    assert result["lock_required"] is True
    assert "broker_drawdown -10.00% <= -6.00%" in result["reasons"]


def test_constants_and_order_state_spellings_are_explicit():
    import auto_gate

    assert auto_gate.SIGNAL_OUTCOME_EXPIRY_TRADING_DAYS == 15
    assert auto_gate.MAX_HOLDING_TRADING_DAYS == 20
    assert auto_gate.SIGNAL_OUTCOME_EXPIRY_TRADING_DAYS != auto_gate.MAX_HOLDING_TRADING_DAYS
    assert "filled" in auto_gate.ORDER_STATES
    assert "full_filled" not in auto_gate.ORDER_STATES
    assert "cancelled" in auto_gate.ORDER_STATES
    assert "canceled" not in auto_gate.ORDER_STATES
    config = auto_gate.default_strategy_config()
    assert config["signal_date_rule"] == "exchange_trading_days_only"
    assert config["benchmark_rule"]["historical_backfill"] == "diagnostic_only"
    assert "stock_and_benchmark_data_quality=high" in config["executable_sample_filters"]


def test_record_real_data_conflict_marks_low_quality(gate_db, monkeypatch):
    import auto_gate
    import data_engine

    def fake_fetch(codes, source="sina"):
        prices = {"tencent": 100.0, "sina": 102.0, "akshare": 0.0}
        price = prices[source]
        if price <= 0:
            return []
        return [{
            "code": "002281",
            "name": "光迅科技",
            "date": "2026-01-05",
            "open": price,
            "price": price,
            "high": price,
            "low": price,
            "volume": 1000,
            "amount": 100000,
            "close_yesterday": 99.0,
        }]

    monkeypatch.setattr(data_engine, "fetch_realtime", fake_fetch)

    result = auto_gate.record_real_data(dry_run=False, codes=["002281"], as_of="2026-01-05")

    assert result["saved"] == 1
    assert result["low_quality"][0]["code"] == "002281"
    conn = gate_db.get_conn()
    row = conn.execute("SELECT quality_status, source FROM price_history WHERE code='002281'").fetchone()
    warning = conn.execute("SELECT warning FROM data_quality_log WHERE code='002281'").fetchone()
    conn.close()
    assert row["quality_status"] == "low"
    assert row["source"] == "tencent"
    assert "source conflict" in warning["warning"]


def test_record_real_data_skips_akshare_when_primary_sources_cover_codes(gate_db, monkeypatch):
    import auto_gate
    import data_engine

    calls = []

    def fake_fetch(codes, source="sina"):
        calls.append(source)
        if source == "akshare":
            raise AssertionError("akshare should stay quiet when Tencent/Sina cover the codes")
        price = 100.0 if source == "tencent" else 100.2
        return [{
            "code": "002281",
            "name": "光迅科技",
            "date": "2026-01-05",
            "open": price,
            "price": price,
            "high": price,
            "low": price,
            "volume": 1000,
            "amount": 100000,
            "close_yesterday": 99.0,
        }]

    monkeypatch.setattr(data_engine, "fetch_realtime", fake_fetch)

    result = auto_gate.record_real_data(dry_run=True, codes=["002281"], as_of="2026-01-05")

    assert calls == ["tencent", "sina"]
    assert result["source_errors"] == {}
    assert result["missing"] == []


def test_record_real_data_uses_akshare_when_primary_sources_fail(gate_db, monkeypatch):
    import auto_gate
    import data_engine

    calls = []

    def fake_fetch(codes, source="sina"):
        calls.append(source)
        if source in ("tencent", "sina"):
            raise RuntimeError(f"{source} down")
        return [{
            "code": "002281",
            "name": "光迅科技",
            "date": "2026-01-05",
            "open": 99.0,
            "price": 99.0,
            "high": 99.0,
            "low": 99.0,
            "volume": 1000,
            "amount": 100000,
            "close_yesterday": 98.0,
        }]

    monkeypatch.setattr(data_engine, "fetch_realtime", fake_fetch)

    result = auto_gate.record_real_data(dry_run=True, codes=["002281"], as_of="2026-01-05")

    assert calls == ["tencent", "sina", "akshare"]
    assert result["source_errors"]["tencent"] == "tencent down"
    assert result["source_errors"]["sina"] == "sina down"
    assert result["missing"] == []


def test_default_real_data_collection_includes_gate_benchmarks(gate_db, monkeypatch):
    import auto_gate
    import data_engine

    requested = []

    def fake_fetch(codes, source="sina"):
        requested.append((source, set(codes)))
        return [{
            "code": code,
            "name": code,
            "date": "2026-07-03",
            "open": 100.0,
            "price": 100.0,
            "high": 101.0,
            "low": 99.0,
            "volume": 1000,
            "amount": 100000,
            "close_yesterday": 99.0,
        } for code in codes]

    monkeypatch.setattr(data_engine, "fetch_realtime", fake_fetch)

    result = auto_gate.record_real_data(dry_run=True, as_of="2026-07-03")

    assert {"000300", "000905"}.issubset(requested[0][1])
    assert {"000300", "000905"}.issubset({row["code"] for row in result["records"]})


def test_benchmark_indices_use_shanghai_market_prefix():
    import data_engine

    assert data_engine._market_prefix_for_code("000300") == "sh"
    assert data_engine._market_prefix_for_code("000905") == "sh"


def test_real_data_collection_skips_non_trading_day(gate_db, monkeypatch):
    import auto_gate
    import data_engine

    monkeypatch.setattr(
        data_engine,
        "fetch_realtime",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network must stay idle")),
    )

    result = auto_gate.record_real_data(dry_run=False, as_of="2026-07-04")

    assert result["skipped"] is True
    assert result["skip_reason"] == "non_trading_day"


def test_non_trading_day_signal_is_terminally_non_executable(gate_db):
    import auto_gate

    version = auto_gate.ensure_current_strategy_version()["version"]
    conn = gate_db.get_conn()
    conn.execute(
        """
        INSERT INTO signal_log
            (code, date, time, action, total_score, price, strategy_version,
             settlement_status, executable_status, data_quality, adjustment_mode)
        VALUES ('002281', '2026-07-04', '16:00', 'BUY', 80, 10, ?,
                'pending', 'unknown', 'unknown', 'raw')
        """,
        (version,),
    )
    conn.commit()
    conn.close()

    result = auto_gate.settle_pending_signal_outcomes(dry_run=False)

    assert result["non_executable"] == 1
    assert result["pending"] == 0
    conn = gate_db.get_conn()
    row = conn.execute("SELECT * FROM signal_log WHERE code='002281'").fetchone()
    conn.close()
    assert row["non_executable_reason"] == "non_trading_signal_date"


def test_settle_outcome_uses_t1_to_t6_for_stock_and_benchmark(gate_db):
    import auto_gate

    signal_date = auto_gate.add_trading_days(date.today().isoformat(), -7)
    entry_date = auto_gate.add_trading_days(signal_date, 1)
    exit_date = auto_gate.add_trading_days(signal_date, 6)
    version = auto_gate.ensure_current_strategy_version()["version"]
    conn = gate_db.get_conn()
    conn.execute(
        """
        INSERT INTO signal_log
            (code, date, time, action, total_score, price,
             strategy_version, settlement_status, executable_status, data_quality, adjustment_mode)
        VALUES ('002281', ?, '14:55', 'BUY', 80, 10, ?,
                'pending', 'unknown', 'unknown', 'raw')
        """,
        (signal_date, version),
    )
    for code, entry_open, exit_open in [
        ("002281", 10.0, 11.0),
        ("000905", 100.0, 102.0),
    ]:
        conn.execute(
            """
            INSERT INTO price_history
                (code, date, open, close, high, low, volume, change_pct,
                 adjustment_mode, quality_status)
            VALUES (?, ?, ?, ?, ?, ?, 1000, 0, 'raw', 'high')
            """,
            (code, entry_date, entry_open, entry_open, entry_open, entry_open),
        )
        conn.execute(
            """
            INSERT INTO price_history
                (code, date, open, close, high, low, volume, change_pct,
                 adjustment_mode, quality_status)
            VALUES (?, ?, ?, ?, ?, ?, 1000, 0, 'raw', 'high')
            """,
            (code, exit_date, exit_open, exit_open, exit_open, exit_open),
        )
    conn.commit()
    conn.close()

    result = auto_gate.settle_pending_signal_outcomes(dry_run=False)

    assert result["settled"] == 1
    conn = gate_db.get_conn()
    row = conn.execute("SELECT * FROM signal_log WHERE code='002281'").fetchone()
    conn.close()
    assert row["entry_date"] == entry_date
    assert row["exit_date"] == exit_date
    assert row["return_5d"] == pytest.approx(10.0)
    assert row["benchmark_return_5d"] == pytest.approx(2.0)
    assert row["excess_5d"] == pytest.approx(8.0)
    assert row["settlement_status"] == "settled"


def test_settlement_diagnostic_explains_missing_benchmark(gate_db):
    import auto_gate

    signal_date = auto_gate.add_trading_days(date.today().isoformat(), -7)
    entry_date = auto_gate.add_trading_days(signal_date, 1)
    exit_date = auto_gate.add_trading_days(signal_date, 6)
    version = auto_gate.ensure_current_strategy_version()["version"]
    conn = gate_db.get_conn()
    conn.execute(
        """
        INSERT INTO signal_log
            (code, date, time, action, total_score, price, strategy_version,
             settlement_status, executable_status, data_quality, adjustment_mode)
        VALUES ('002281', ?, '14:55', 'BUY', 80, 10, ?,
                'pending', 'unknown', 'unknown', 'raw')
        """,
        (signal_date, version),
    )
    for price_date, value in ((entry_date, 10.0), (exit_date, 11.0)):
        conn.execute(
            """
            INSERT INTO price_history
                (code, date, open, close, adjustment_mode, quality_status)
            VALUES ('002281', ?, ?, ?, 'raw', 'high')
            """,
            (price_date, value, value),
        )
    conn.commit()
    conn.close()

    result = auto_gate.diagnose_signal_settlements(as_of=exit_date)

    assert result["pending"] == 1
    assert result["due_blocked"] == 1
    assert result["reason_counts"]["missing_benchmark_entry"] == 1
    assert result["details"][0]["benchmark_code"] == "000905"


def test_unversioned_signal_never_inherits_current_strategy(gate_db):
    import auto_gate

    signal_date = auto_gate.add_trading_days(date.today().isoformat(), -7)
    entry_date = auto_gate.add_trading_days(signal_date, 1)
    exit_date = auto_gate.add_trading_days(signal_date, 6)
    conn = gate_db.get_conn()
    conn.execute(
        """
        INSERT INTO signal_log
            (code, date, time, action, total_score, price,
             settlement_status, executable_status, data_quality, adjustment_mode)
        VALUES ('002281', ?, '14:55', 'BUY', 80, 10,
                'pending', 'unknown', 'unknown', 'raw')
        """,
        (signal_date,),
    )
    for code, entry_open, exit_open in (("002281", 10.0, 11.0), ("000905", 100.0, 102.0)):
        for price_date, value in ((entry_date, entry_open), (exit_date, exit_open)):
            conn.execute(
                """
                INSERT INTO price_history
                    (code, date, open, close, adjustment_mode, quality_status)
                VALUES (?, ?, ?, ?, 'raw', 'high')
                """,
                (code, price_date, value, value),
            )
    conn.commit()
    conn.close()

    result = auto_gate.settle_pending_signal_outcomes(dry_run=False)

    assert result["settled"] == 0
    assert result["non_executable"] == 1
    conn = gate_db.get_conn()
    row = conn.execute("SELECT * FROM signal_log WHERE code='002281'").fetchone()
    conn.close()
    assert row["strategy_version"] == "legacy_unversioned"
    assert row["settlement_status"] == "non_executable"
    assert row["executable_status"] == "non_executable"
