"""测试 GitHub A股量化项目融合体检层。"""

import sqlite3
from datetime import date as RealDate, timedelta

import quant_fusion
from config import ALL_CODES


class FakeDate(RealDate):
    @classmethod
    def today(cls):
        return cls(2026, 6, 22)


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE price_history (
            code TEXT,
            date TEXT,
            close REAL
        );
        CREATE TABLE signal_log (
            code TEXT,
            date TEXT,
            outcome_1d REAL,
            outcome_5d REAL
        );
        CREATE TABLE score_reflections (
            code TEXT,
            date TEXT,
            actual_return_1d REAL,
            dimension_ic TEXT
        );
        CREATE TABLE scoring_history (
            code TEXT,
            date TEXT,
            total_score REAL,
            technical_score REAL,
            factor_score REAL,
            sentiment_score REAL,
            serenity_score REAL,
            moat_score REAL,
            uzi_score REAL
        );
        CREATE TABLE execution_log (
            status TEXT
        );
    """)
    return conn


def test_top_projects_are_ranked_and_traceable():
    projects = quant_fusion.get_top_projects()

    assert len(projects) == 10
    assert projects[0]["repo"] == "bbfamily/abu"
    assert projects[1]["repo"] == "shidenggui/easytrader"
    assert [p["stars"] for p in projects] == sorted(
        [p["stars"] for p in projects],
        reverse=True,
    )
    assert len({p["repo"] for p in projects}) == 10
    assert all(p["url"].startswith("https://github.com/") for p in projects)
    assert all(p["essence"] and p["fusion"] for p in projects)


def test_quantdinger_essence_is_traceable():
    essence = quant_fusion.get_quantdinger_essence()

    assert len(essence) >= 5
    assert any("objective_score" in item["pattern"] for item in essence)
    assert any("多周期" in item["pattern"] for item in essence)
    assert all(item["pattern"] and item["fusion"] for item in essence)


def test_data_resilience_is_good_when_latest_history_covers_universe(monkeypatch):
    monkeypatch.setattr(quant_fusion, "date", FakeDate)
    conn = _make_conn()
    try:
        for code in ALL_CODES:
            conn.execute(
                "INSERT INTO price_history (code, date, close) VALUES (?, ?, ?)",
                (code, "2026-06-21", 100.0),
            )

        assessment = quant_fusion.assess_data_resilience(conn)

        assert assessment["status"] == "good"
        assert assessment["score"] >= 90
        assert assessment["metrics"]["latest_price_date"] == "2026-06-21"
        assert assessment["metrics"]["latest_coverage"].startswith(
            f"{len(ALL_CODES)}/{len(ALL_CODES)}"
        )
        assert "tencent" in assessment["metrics"]["providers"]
    finally:
        conn.close()


def test_quantdinger_consensus_builds_multitimeframe_decision():
    conn = _make_conn()
    try:
        start = RealDate(2026, 4, 1)
        code = ALL_CODES[0]
        for i in range(20):
            day = (start + timedelta(days=i)).isoformat()
            conn.execute(
                """
                INSERT INTO scoring_history
                    (code, date, total_score, technical_score, factor_score,
                     sentiment_score, serenity_score, moat_score, uzi_score)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (code, day, 64 + i, 70 + i * 0.5, 68, 62, 75, 72, 80),
            )
            conn.execute(
                "INSERT INTO price_history (code, date, close) VALUES (?, ?, ?)",
                (code, day, 100.0 + i),
            )
        conn.commit()
        changes_before = conn.total_changes

        consensus = quant_fusion.build_quantdinger_consensus(conn, limit=3)
        lead = consensus["signals"][0]

        assert conn.total_changes == changes_before
        assert consensus["source"]["repo"] == "brokermr810/QuantDinger"
        assert consensus["latest_date"] == "2026-04-20"
        assert lead["code"] == code
        assert lead["consensus_decision"] == "BUY"
        assert lead["consensus_score"] >= 20
        assert lead["agreement_ratio"] > 0
        assert lead["confidence"] > 0
        assert set(lead["timeframes"]) == {"latest", "week", "month"}
        assert consensus["top_opportunities"][0]["code"] == code
    finally:
        conn.close()


def test_build_report_is_read_only_and_has_actionable_sections(monkeypatch):
    monkeypatch.setattr(quant_fusion, "date", FakeDate)
    conn = _make_conn()
    try:
        start = RealDate(2026, 4, 1)
        for i in range(20):
            day = (start + timedelta(days=i)).isoformat()
            conn.execute(
                "INSERT INTO price_history (code, date, close) VALUES (?, ?, ?)",
                (ALL_CODES[0], day, 100.0 + i),
            )
            conn.execute(
                "INSERT INTO scoring_history (code, date, total_score) VALUES (?, ?, ?)",
                (ALL_CODES[0], day, 80.0),
            )
        conn.executemany(
            "INSERT INTO signal_log (code, date, outcome_1d, outcome_5d) VALUES (?, ?, ?, ?)",
            [
                (ALL_CODES[0], "2026-04-01", 1.2, 2.5),
                (ALL_CODES[0], "2026-04-02", None, None),
            ],
        )
        conn.execute(
            "INSERT INTO score_reflections "
            "(code, date, actual_return_1d, dimension_ic) VALUES (?, ?, ?, ?)",
            (ALL_CODES[0], "2026-04-01", 1.2, '{"momentum_score": 0.12}'),
        )
        conn.commit()
        changes_before = conn.total_changes

        report = quant_fusion.build_fusion_report(conn)
        rendered = quant_fusion.format_fusion_report(report)

        assert conn.total_changes == changes_before
        assert report["title"] == "Serenity x QuantDinger 量化融合体检"
        assert set(report["assessments"]) == {
            "data_resilience",
            "feedback_loop",
            "execution_boundary",
            "backtest_readiness",
            "quantdinger_consensus",
        }
        assert report["quantdinger_consensus"]["source"]["repo"] == "brokermr810/QuantDinger"
        assert report["recommendations"]
        assert "QuantDinger 精华已融合" in rendered
        assert "QuantDinger 客观共识" in rendered
        assert "GitHub Top10 精华源" in rendered
        assert "下一步修缮动作" in rendered
    finally:
        conn.close()
