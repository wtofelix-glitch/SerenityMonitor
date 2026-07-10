import sqlite3

import alpha_validation


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE strategy_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            version TEXT,
            config_hash TEXT,
            is_active INTEGER,
            created_at TEXT
        );
        CREATE TABLE signal_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT,
            date TEXT,
            action TEXT,
            return_5d REAL,
            outcome_5d REAL,
            excess_5d REAL,
            exit_date TEXT,
            strategy_version TEXT,
            settlement_status TEXT,
            executable_status TEXT,
            data_quality TEXT,
            adjustment_mode TEXT
        );
        CREATE TABLE nav_history (
            date TEXT,
            total_value REAL,
            cash REAL,
            holdings_value REAL,
            positions_json TEXT
        );
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT,
            action TEXT,
            price REAL,
            quantity INTEGER,
            date TEXT,
            trade_amount REAL
        );
        CREATE TABLE portfolio_reconciliations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_at TEXT,
            source TEXT,
            total_assets REAL,
            holdings_value REAL,
            cash REAL,
            positions_json TEXT
        );
        CREATE TABLE cashflow_reconciliations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            window_start TEXT,
            window_end TEXT,
            source TEXT,
            unexplained_cash_effect REAL,
            tolerance REAL,
            evidence_cash_effect_total REAL,
            remaining_gap REAL,
            evidence_hash TEXT,
            evidence_items_json TEXT,
            payload_json TEXT,
            notes TEXT,
            created_at TEXT
        );
        CREATE TABLE price_history (
            code TEXT,
            date TEXT,
            close REAL
        );
    """)
    return conn


def _insert_price_pair(conn: sqlite3.Connection, code: str, start: float, end: float) -> None:
    conn.executemany(
        "INSERT INTO price_history (code, date, close) VALUES (?, ?, ?)",
        [
            (code, "2025-12-01", 90.0),
            (code, "2025-12-31", 95.0),
            (code, "2026-01-01", start),
            (code, "2026-03-31", end),
        ],
    )


def test_alpha_validation_reports_insufficient_when_no_real_samples(monkeypatch):
    conn = _make_conn()
    try:
        monkeypatch.setattr(alpha_validation, "_current_config_hash", lambda: "hash")
        conn.execute(
            """
            INSERT INTO strategy_versions (version, config_hash, is_active, created_at)
            VALUES ('v1.0', 'hash', 1, '2026-01-01')
            """
        )
        conn.commit()

        report = alpha_validation.build_alpha_validation_report(conn)
        rendered = alpha_validation.format_alpha_validation_report(report)

        assert report["verdict"] == "P0_NOT_PROVEN"
        assert report["samples"]["status"] == "INSUFFICIENT"
        assert report["samples"]["sample_count"] == 0
        assert "P0_NOT_PROVEN" in rendered
        assert "samples: 0/50" in rendered
    finally:
        conn.close()


def test_alpha_validation_pending_state_is_version_aware(monkeypatch):
    conn = _make_conn()
    try:
        monkeypatch.setattr(alpha_validation, "_current_config_hash", lambda: "hash")
        conn.execute(
            """
            INSERT INTO strategy_versions (version, config_hash, is_active, created_at)
            VALUES ('v15.0', 'hash', 1, '2026-01-01')
            """
        )
        conn.executemany(
            """
            INSERT INTO signal_log
                (code, date, action, strategy_version, settlement_status)
            VALUES (?, ?, ?, ?, 'pending')
            """,
            [
                ("002281", "2026-07-09", "BUY", "v15.0"),
                ("000988", "2026-07-08", "BUY", "v13.0"),
            ],
        )
        conn.commit()

        report = alpha_validation.build_alpha_validation_report(conn)
        rendered = alpha_validation.format_alpha_validation_report(report)

        assert report["pending"]["pending"] == 2
        assert report["pending"]["current_version_pending"] == 1
        assert report["pending"]["by_strategy_version"] == {"v15.0": 1, "v13.0": 1}
        assert "current_version_pending: 1 (v15.0)" in rendered
    finally:
        conn.close()


def test_alpha_validation_blocks_invalid_nav_and_negative_trades(monkeypatch):
    conn = _make_conn()
    try:
        monkeypatch.setattr(alpha_validation, "_current_config_hash", lambda: "hash")
        conn.execute(
            """
            INSERT INTO strategy_versions (version, config_hash, is_active, created_at)
            VALUES ('v1.0', 'hash', 1, '2026-01-01')
            """
        )
        conn.executemany(
            """
            INSERT INTO nav_history
                (date, total_value, cash, holdings_value, positions_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                ("2026-01-01", 100.0, 10.0, 90.0, "2"),
                ("2026-01-02", 101.0, 11.0, 90.0, "[]"),
            ],
        )
        conn.execute(
            """
            INSERT INTO trades (code, action, price, quantity, date, trade_amount)
            VALUES ('600141', 'buy', -2.4, 100, '2026-01-01', -240)
            """
        )
        conn.commit()

        report = alpha_validation.build_alpha_validation_report(conn)
        rendered = alpha_validation.format_alpha_validation_report(report)

        assert report["verdict"] == "P0_DATA_INVALID"
        assert report["data_quality"]["status"] == "BLOCK"
        assert report["data_quality"]["nav"]["invalid_position_rows"][0]["date"] == "2026-01-01"
        assert report["data_quality"]["trades"]["negative_trade_rows"][0]["code"] == "600141"
        assert "[BLOCK] NAV and trade records" in rendered
    finally:
        conn.close()


def test_alpha_validation_prefers_valid_broker_nav_over_dirty_diagnostic_ledger(monkeypatch):
    conn = _make_conn()
    try:
        monkeypatch.setattr(alpha_validation, "_current_config_hash", lambda: "hash")
        conn.execute(
            """
            INSERT INTO strategy_versions (version, config_hash, is_active, created_at)
            VALUES ('v1.0', 'hash', 1, '2026-01-01')
            """
        )
        conn.executemany(
            """
            INSERT INTO nav_history
                (date, total_value, cash, holdings_value, positions_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                ("2026-01-01", 100.0, 10.0, 90.0, "2"),
                ("2026-01-02", 90.0, 10.0, 80.0, "1"),
            ],
        )
        conn.execute(
            """
            INSERT INTO trades (code, action, price, quantity, date, trade_amount)
            VALUES ('600141', 'buy', -2.4, 100, '2026-01-01', -240)
            """
        )
        conn.executemany(
            """
            INSERT INTO portfolio_reconciliations
                (snapshot_at, source, total_assets, holdings_value, cash, positions_json)
            VALUES (?, 'broker_screenshot', ?, ?, ?, ?)
            """,
            [
                ("2026-01-01 15:10:00", 100.0, 80.0, 20.0, "[]"),
                ("2026-01-02 15:10:00", 102.0, 82.0, 20.0, "[]"),
            ],
        )
        conn.commit()

        report = alpha_validation.build_alpha_validation_report(conn)

        assert report["portfolio"]["source"] == "broker_reconciled"
        assert report["data_quality"]["status"] == "PASS"
        assert report["data_quality"]["trades"]["status"] == "SKIP"
        assert report["verdict"] != "P0_DATA_INVALID"
    finally:
        conn.close()


def test_alpha_validation_blocks_diagnostic_nav_cashflow_mismatch(monkeypatch):
    conn = _make_conn()
    try:
        monkeypatch.setattr(alpha_validation, "_current_config_hash", lambda: "hash")
        conn.execute(
            """
            INSERT INTO strategy_versions (version, config_hash, is_active, created_at)
            VALUES ('v1.0', 'hash', 1, '2026-01-01')
            """
        )
        conn.executemany(
            """
            INSERT INTO nav_history
                (date, total_value, cash, holdings_value, positions_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                ("2026-01-01", 120.0, 50.0, 70.0, "[]"),
                ("2026-01-02", 100.0, 40.0, 60.0, "[]"),
            ],
        )
        conn.execute(
            """
            INSERT INTO trades (code, action, price, quantity, date, trade_amount)
            VALUES ('600001', 'sell', 10, 10, '2026-01-02', 100)
            """
        )
        conn.commit()

        report = alpha_validation.build_alpha_validation_report(conn)

        assert report["verdict"] == "P0_DATA_INVALID"
        assert report["data_quality"]["drawdown_cashflow"]["status"] == "BLOCK"
        assert report["data_quality"]["drawdown_cashflow"]["gap"] == -110.0
        assert report["drawdown_attribution"]["start_cash"] == 50.0
        assert report["drawdown_attribution"]["end_cash"] == 40.0
    finally:
        conn.close()


def test_alpha_validation_accepts_imported_cashflow_reconciliation(monkeypatch):
    conn = _make_conn()
    try:
        monkeypatch.setattr(alpha_validation, "_current_config_hash", lambda: "hash")
        conn.execute(
            """
            INSERT INTO strategy_versions (version, config_hash, is_active, created_at)
            VALUES ('v1.0', 'hash', 1, '2026-01-01')
            """
        )
        conn.executemany(
            """
            INSERT INTO nav_history
                (date, total_value, cash, holdings_value, positions_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                ("2026-01-01", 120.0, 50.0, 70.0, "[]"),
                ("2026-01-02", 100.0, 40.0, 60.0, "[]"),
            ],
        )
        conn.execute(
            """
            INSERT INTO trades (code, action, price, quantity, date, trade_amount)
            VALUES ('600001', 'sell', 10, 10, '2026-01-02', 100)
            """
        )
        conn.execute(
            """
            INSERT INTO cashflow_reconciliations
                (window_start, window_end, source, unexplained_cash_effect,
                 tolerance, evidence_cash_effect_total, remaining_gap,
                 evidence_hash, evidence_items_json, payload_json, notes, created_at)
            VALUES ('2026-01-01', '2026-01-02', 'manual', -110, 50,
                    -110, 0, 'hash1', '[]', '{}', '', '2026-01-03 09:00:00')
            """
        )
        conn.commit()

        report = alpha_validation.build_alpha_validation_report(conn)

        cashflow = report["data_quality"]["drawdown_cashflow"]
        assert report["verdict"] != "P0_DATA_INVALID"
        assert report["data_quality"]["status"] == "PASS"
        assert cashflow["status"] == "PASS"
        assert cashflow["reason"] == "external_cashflow_evidence_verified"
        assert cashflow["evidence"]["status"] == "verified"
        assert cashflow["evidence"]["evidence_hash"] == "hash1"
    finally:
        conn.close()


def test_alpha_validation_can_pass_when_real_oos_and_benchmarks_clear(monkeypatch):
    conn = _make_conn()
    try:
        monkeypatch.setattr(alpha_validation, "_current_config_hash", lambda: "hash")
        monkeypatch.setitem(alpha_validation.CAPITAL_CONFIG, "initial_capital", 100.0)
        monkeypatch.setattr(alpha_validation, "ALL_CODES", ["600001", "600002", "600003"])
        monkeypatch.setattr(alpha_validation, "TIER_1_CODES", ["600001"])
        monkeypatch.setattr(alpha_validation, "TIER_2_CODES", ["600002"])
        monkeypatch.setattr(alpha_validation, "TIER_4_CODES", ["600002"])

        conn.execute(
            """
            INSERT INTO strategy_versions (version, config_hash, is_active, created_at)
            VALUES ('v1.0', 'hash', 1, '2026-01-01')
            """
        )
        conn.executemany(
            "INSERT INTO nav_history (date, total_value) VALUES (?, ?)",
            [("2026-01-01", 100.0), ("2026-03-31", 112.0)],
        )
        for code in [
            "600001", "600002", "600003", "000300", "000905", "sh512100",
            "sh515050", "sh512480",
        ]:
            _insert_price_pair(conn, code, 100.0, 105.0)

        rows = []
        for idx in range(50):
            code = "600001" if idx % 2 == 0 else "600002"
            month = "01" if idx < 18 else "02" if idx < 34 else "03"
            rows.append((
                code,
                f"2026-{month}-01",
                "BUY",
                1.2,
                1.2,
                0.8,
                f"2026-{month}-08",
                "v1.0",
                "settled",
                "executable",
                "high",
                "raw",
            ))
        conn.executemany(
            """
            INSERT INTO signal_log
                (code, date, action, return_5d, outcome_5d, excess_5d,
                 exit_date, strategy_version, settlement_status,
                 executable_status, data_quality, adjustment_mode)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()

        report = alpha_validation.build_alpha_validation_report(conn)

        assert report["verdict"] == "P0_PASS"
        assert report["samples"]["sample_count"] == 50
        assert report["oos"]["window_count"] == 3
        assert report["benchmarks"]["same_pool_excess_pct"] == 7.0
        assert all(item["status"] == "PASS" for item in report["criteria"])
    finally:
        conn.close()
