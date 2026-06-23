"""测试 Serenity Alpha Gate 选股候选研究闸门。"""

import sqlite3

import alpha_gate


POSITIVE_IC = {
    "latest": {
        "total_score": 0.11,
        "momentum_score": 0.08,
        "factor_score": 0.06,
        "technical_score": 0.05,
    },
    "mean_ic": {
        "total_score": 0.09,
        "momentum_score": 0.07,
        "factor_score": 0.05,
        "technical_score": 0.04,
    },
    "ic_ir": {
        "total_score": 0.8,
        "momentum_score": 0.6,
        "factor_score": 0.4,
        "technical_score": 0.3,
    },
    "win_rate": {
        "total_score": 70.0,
        "momentum_score": 65.0,
        "factor_score": 60.0,
        "technical_score": 55.0,
    },
    "n_days": {
        "total_score": 20,
        "momentum_score": 20,
        "factor_score": 20,
        "technical_score": 20,
    },
}


NEGATIVE_IC = {
    "latest": {"total_score": -0.08, "momentum_score": -0.06},
    "mean_ic": {"total_score": -0.07, "momentum_score": -0.05},
    "ic_ir": {"total_score": -0.7, "momentum_score": -0.4},
    "win_rate": {"total_score": 30.0, "momentum_score": 35.0},
    "n_days": {"total_score": 20, "momentum_score": 20},
}


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE signal_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT,
            date TEXT,
            time TEXT,
            action TEXT,
            total_score REAL,
            price REAL,
            outcome_1d REAL,
            outcome_3d REAL,
            outcome_5d REAL
        );
        CREATE TABLE signal_performance (
            code TEXT,
            action TEXT,
            total_signals INTEGER,
            wins_1d INTEGER,
            wins_3d INTEGER,
            wins_5d INTEGER,
            avg_return_1d REAL,
            avg_return_3d REAL,
            avg_return_5d REAL
        );
        CREATE TABLE scoring_history (
            code TEXT,
            date TEXT,
            total_score REAL,
            momentum_score REAL,
            factor_score REAL,
            technical_score REAL
        );
    """)
    return conn


def _seed_score_trend(conn: sqlite3.Connection, code: str, scores: list[float]) -> None:
    for idx, score in enumerate(scores, 1):
        conn.execute(
            """
            INSERT INTO scoring_history
                (code, date, total_score, momentum_score, factor_score, technical_score)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (code, f"2026-06-{17 + idx:02d}", score, 70.0, 70.0, 70.0),
        )


def test_alpha_gate_promotes_stable_high_quality_candidate_read_only():
    conn = _make_conn()
    try:
        conn.executemany(
            """
            INSERT INTO signal_log (code, date, time, action, total_score, price)
            VALUES (?, '2026-06-22', '15:00', ?, ?, 10.0)
            """,
            [
                ("002281", "BUY", 88.0),
                ("600585", "HOLD", 70.0),
                ("300750", "BUY", 95.0),
            ],
        )
        conn.executemany(
            """
            INSERT INTO signal_performance
                (code, action, total_signals, wins_1d, wins_3d, wins_5d,
                 avg_return_1d, avg_return_3d, avg_return_5d)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("002281", "BUY", 6, 4, 4, 5, 0.8, 1.5, 2.2),
                ("600585", "HOLD", 4, 2, 1, 1, 0.1, -0.2, -0.4),
                ("300750", "BUY", 6, 5, 5, 5, 1.0, 2.0, 3.0),
            ],
        )
        _seed_score_trend(conn, "002281", [78.0, 82.0, 85.0, 88.0])
        _seed_score_trend(conn, "600585", [74.0, 73.0, 71.0, 70.0])
        conn.commit()
        changes_before = conn.total_changes

        report = alpha_gate.build_alpha_gate_report(conn, ic_data=POSITIVE_IC)

        assert conn.total_changes == changes_before
        codes = [item["code"] for item in report["candidates"]]
        assert codes[0] == "002281"
        assert "300750" not in codes
        assert report["candidates"][0]["status"] == "PASS"
        assert report["candidates"][0]["velocity"] == "加速观察"
        assert report["summary"]["pass_count"] >= 1
        assert any(item["priority"] == "KEEP" for item in report["factor_adjustments"])
    finally:
        conn.close()


def test_alpha_gate_blocks_high_score_when_history_and_ic_are_bad():
    conn = _make_conn()
    try:
        conn.execute(
            """
            INSERT INTO signal_log (code, date, time, action, total_score, price)
            VALUES ('000988', '2026-06-22', '15:00', 'BUY', 92.0, 10.0)
            """
        )
        conn.execute(
            """
            INSERT INTO signal_performance
                (code, action, total_signals, wins_1d, wins_3d, wins_5d,
                 avg_return_1d, avg_return_3d, avg_return_5d)
            VALUES ('000988', 'BUY', 5, 2, 1, 1, -0.2, -0.8, -2.1)
            """
        )
        _seed_score_trend(conn, "000988", [96.0, 94.0, 93.0, 92.0])
        conn.commit()

        report = alpha_gate.build_alpha_gate_report(conn, ic_data=NEGATIVE_IC)
        candidate = report["candidates"][0]

        assert candidate["code"] == "000988"
        assert candidate["status"] == "BLOCK"
        assert any("5日平均收益" in item for item in candidate["risks"])
        assert report["factor_health"]["status"] == "risk"
        assert any(item["priority"] == "P1" for item in report["factor_adjustments"])
    finally:
        conn.close()


def test_signal_log_mature_outcomes_override_stale_summary_table():
    conn = _make_conn()
    try:
        conn.executemany(
            """
            INSERT INTO signal_log
                (code, date, time, action, total_score, price,
                 outcome_1d, outcome_3d, outcome_5d)
            VALUES ('002281', ?, '15:00', 'BUY', 86.0, 10.0, ?, ?, ?)
            """,
            [
                ("2026-06-20", 0.5, 1.2, 2.0),
                ("2026-06-22", None, None, None),
            ],
        )
        conn.execute(
            """
            INSERT INTO signal_performance
                (code, action, total_signals, wins_1d, wins_3d, wins_5d,
                 avg_return_1d, avg_return_3d, avg_return_5d)
            VALUES ('002281', 'BUY', 2, 0, 0, 0, -1.0, -2.0, -3.0)
            """
        )
        conn.commit()

        perf = alpha_gate.load_signal_performance(conn)
        exact = perf["exact"][("002281", "BUY")]

        assert exact["total_signals"] == 2
        assert exact["samples_5d"] == 1
        assert exact["hit_rate_5d"] == 100.0
        assert exact["avg_return_5d"] == 2.0
    finally:
        conn.close()


def test_format_alpha_gate_report_contains_sources_and_next_actions():
    conn = _make_conn()
    try:
        conn.execute(
            """
            INSERT INTO scoring_history
                (code, date, total_score, momentum_score, factor_score, technical_score)
            VALUES ('600900', '2026-06-22', 76.0, 60.0, 58.0, 62.0)
            """
        )
        conn.commit()
        report = alpha_gate.build_alpha_gate_report(conn, ic_data=POSITIVE_IC)
        rendered = alpha_gate.format_alpha_gate_report(report)

        assert "Serenity Alpha Gate" in rendered
        assert "microsoft/qlib" in rendered
        assert "因子调参建议" in rendered
        assert "下一步动作" in rendered
        assert "600900" in rendered
    finally:
        conn.close()
