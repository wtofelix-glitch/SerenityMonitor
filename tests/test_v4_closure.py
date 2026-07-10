"""Regression coverage for the v4 trading-kernel closure."""

from __future__ import annotations

from datetime import date

import backtest_engine
import monitoring_dashboard
import pytest
from backtest_engine import BaseStrategy, EventDrivenBacktest
from execution_simulator import ExecutionSimulator, get_simulator
from phase4_checklist import Phase4Checklist


class _RecordingStrategy(BaseStrategy):
    def __init__(self):
        self.indices: list[int] = []

    def generate_signals(self, idx: int) -> tuple[float, str]:
        self.indices.append(idx)
        return 0.0, "recorded"


def test_event_backtest_keeps_microstructure_enabled():
    engine = EventDrivenBacktest("600585", _RecordingStrategy())

    engine._ensure_microstructure()

    assert engine.enable_microstructure is True
    assert engine._micro is not None
    assert isinstance(engine._simulator, ExecutionSimulator)


def test_get_simulator_accepts_backtest_cost_configuration():
    simulator = get_simulator(commission_rate=0.0003, stamp_tax_rate=0.0005)

    assert simulator.commission_rate == 0.0003
    assert simulator.stamp_tax_rate == 0.0005


def test_pre_market_uses_previous_trading_day_signal(monkeypatch):
    strategy = _RecordingStrategy()
    engine = EventDrivenBacktest("600585", strategy, enable_microstructure=False)
    strategy.prepare(
        "600585",
        closes=[10.0, 11.0, 12.0],
        highs=[10.5, 11.5, 12.5],
        lows=[9.5, 10.5, 11.5],
        volumes=[1000.0, 1000.0, 1000.0],
        dates=["2026-07-01", "2026-07-02", "2026-07-03"],
    )

    engine._pre_market(
        2,
        "2026-07-03",
        12.0,
        12.5,
        11.5,
        1000.0,
        11.0,
        strategy.closes,
        strategy.dates,
    )

    assert strategy.indices == [1]


def test_phase4_pending_items_block_readiness():
    checker = Phase4Checklist()
    checker.results = [
        {
            "id": "ready",
            "category": "gate",
            "item": "done",
            "priority": "P0",
            "status": "PASS",
            "detail": "ok",
        },
        {
            "id": "pending",
            "category": "gate",
            "item": "waiting",
            "priority": "P0",
            "status": "PENDING",
            "detail": "wait",
        },
    ]
    checker._passed = 1
    checker._pending = 1
    checker._failed = 0

    assert checker.is_ready() is False
    assert "尚未满足 Phase 4" in checker.summary()


def test_governance_api_uses_structured_kernel_state():
    client = monitoring_dashboard.app.test_client()

    payload = client.get("/api/v4/governance").get_json()

    assert payload["ok"] is True
    assert payload["freeze"]["total_managed"] >= 9
    assert payload["freeze"]["total_frozen"] >= 9
    assert payload["freeze"]["modules"]
    assert payload["observation"]["mode"] in {"NORMAL", "OBSERVATION", "EMERGENCY"}


def test_weekly_report_is_renderable():
    from weekly_comparison_report import generate_weekly_report

    report = generate_weekly_report()

    assert "三系统并跑周报" in report
    assert date.today().isoformat() in report


def test_audit_record_has_replay_identity(tmp_path, monkeypatch):
    import audit_logger
    import db

    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "audit.db"))
    db.init_db()
    decision_id = audit_logger.DecisionAuditLogger().log_signal(
        code="600585",
        signal_type="HOLD",
        total_score=60.0,
        score_components={"zone": 60.0},
        baseline_signal="HOLD",
        adaptive_signal="HOLD",
        risk_checks={"valid_price": True},
        feature_snapshot={"open": 10.0, "date": "2026-07-03"},
    )

    conn = db.get_conn()
    row = conn.execute(
        "SELECT config_hash, feature_snapshot_hash, risk_checks_json "
        "FROM decision_audit_log WHERE decision_id=?",
        (decision_id,),
    ).fetchone()
    conn.close()

    assert len(row["config_hash"]) == 64
    assert len(row["feature_snapshot_hash"]) == 64
    assert row["risk_checks_json"] != "{}"


def test_audit_settlement_uses_t1_to_t6_open(monkeypatch):
    import audit_logger
    import db

    stock_rows = [
        {"date": f"2026-07-{day:02d}", "open": 100.0 + day}
        for day in range(1, 10)
    ]
    benchmark_rows = [
        {"date": f"2026-07-{day:02d}", "open": 200.0 + day}
        for day in range(1, 10)
    ]
    monkeypatch.setattr(
        db,
        "get_price_history",
        lambda code, days=90: benchmark_rows if code == "000300" else stock_rows,
    )
    captured = {}
    logger = audit_logger.DecisionAuditLogger()
    monkeypatch.setattr(
        logger,
        "settle_outcome",
        lambda decision_id, t1, t5, t20, benchmark: captured.update(
            {"t1": t1, "t5": t5, "benchmark": benchmark}
        ),
    )

    assert logger._settle_one("d1", "600585", "2026-07-01T15:00:00") is True
    assert captured["t5"] == pytest.approx(107.0 / 102.0 - 1.0 - audit_logger.ROUND_TRIP_COST_RATE)
    assert captured["benchmark"] == pytest.approx(207.0 / 202.0 - 1.0)
