"""Portfolio reconciliation tests for the monitoring dashboard."""

import json
import sqlite3

import pytest

import monitoring_dashboard as dashboard


def _portfolio_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE stocks (
            code TEXT PRIMARY KEY, name TEXT, buy_price REAL, is_active INTEGER
        );
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, action TEXT,
            price REAL, quantity INTEGER
        );
        CREATE TABLE daily_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, date TEXT, close REAL
        );
    """)
    conn.executemany(
        "INSERT INTO stocks VALUES (?, ?, ?, 1)",
        [
            ("000938", "紫光股份", 26.275),
            ("600585", "海螺水泥", 16.999),
            ("CASH", "可用资金", 1.0),
        ],
    )
    conn.executemany(
        "INSERT INTO trades(code, action, price, quantity) VALUES (?, ?, ?, ?)",
        [
            ("000938", "buy", 26.275, 1000),
            ("600585", "buy", 16.999, 1700),
            ("CASH", "sell", 744.98, 1),
        ],
    )
    conn.executemany(
        "INSERT INTO daily_snapshots(code, date, close) VALUES (?, '2026-07-02', ?)",
        [("000938", 29.03), ("600585", 16.85)],
    )
    return conn


def test_quick_pnl_excludes_cash_and_weights_positions():
    conn = _portfolio_conn()

    result = dashboard._quick_position_pnl(conn)

    assert [position["code"] for position in result["positions"]] == ["000938", "600585"]
    assert result["cash"] == 744.98
    assert result["holdings_value"] == 57675.00
    assert result["total_assets"] == 58419.98
    assert result["total_pct"] == pytest.approx(4.53, abs=0.01)
    assert result["total_profit_amount"] == result["calculated_profit_amount"]


def test_portfolio_summary_preserves_cents(monkeypatch):
    class FakePortfolioManager:
        def get_portfolio_value(self):
            return {
                "position_count": 2,
                "total_value": 58419.98,
                "cash": 744.98,
                "holdings_value": 57675.0,
                "total_profit_pct": 14.4,
                "total_profit_amount": 7353.57,
                "positions": [
                    {"code": "000938", "current_value": 29030.0},
                    {"code": "600585", "current_value": 28645.0},
                ],
            }

    monkeypatch.setattr(dashboard, "PortfolioManager", FakePortfolioManager)
    dashboard._cache["pf"] = None
    dashboard._cache_time["pf"] = None

    result = dashboard._get_portfolio_summary()

    assert result["total_value"] == 58419.98
    assert result["cash"] == 744.98
    assert result["total_profit_amount"] == 7353.57
    assert result["position_details"][0]["weight"] == 49.7
    assert result["position_details"][1]["weight"] == 49.0


def test_nav_snapshot_persists_position_details(monkeypatch):
    class FakeConn:
        def __init__(self):
            self.params = None
            self.committed = False
            self.closed = False

        def execute(self, sql, params):
            self.params = params

        def commit(self):
            self.committed = True

        def close(self):
            self.closed = True

    conn = FakeConn()
    monkeypatch.setattr(dashboard, "get_conn", lambda: conn)
    portfolio = {
        "total_value": 58419.98,
        "cash": 744.98,
        "holdings_value": 57675.0,
        "total_profit_pct": 14.4,
        "position_details": [{"code": "000938"}, {"code": "600585"}],
    }

    dashboard._persist_nav_snapshot("2026-07-02", portfolio)

    assert json.loads(conn.params[-1]) == portfolio["position_details"]
    assert conn.committed is True
    assert conn.closed is True
