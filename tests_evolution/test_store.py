from datetime import datetime, timezone

import unittest
from tempfile import TemporaryDirectory
from pathlib import Path

from serenity_evolution.models import (
    ComparisonMetrics,
    EvolutionCandidate,
    GateResult,
    Stage,
    StrategyMetrics,
)
from serenity_evolution.store import EvolutionStore


def metrics():
    return StrategyMetrics(120, 40, 0.2, 0.2, 0.1, 1.5, 0.08, 0.55, 1.4, 1.0, 0.99)


class StoreTests(unittest.TestCase):
    def test_live_requires_all_three_locks(self):
        with TemporaryDirectory() as directory:
            store = EvolutionStore(Path(directory) / "serenity.db")
            store.migrate()
            candidate = EvolutionCandidate(
                "evo-test", datetime.now(timezone.utc), "frozen-v1", {"factor": 1.0}
            )
            store.save_candidate(candidate)
            comparison = ComparisonMetrics(metrics(), metrics(), 0.1, 0.5, 0.0, 0.98, 0.01)
            store.record_evaluation(
                candidate.candidate_id,
                GateResult(True, Stage.PAPER_CANARY, (), {"all": True}),
                comparison,
            )

            with self.assertRaisesRegex(PermissionError, "disabled"):
                store.promote_live(
                    candidate.candidate_id,
                    approval_ref="approved-by-user",
                    canary_days=20,
                    live_apply_enabled=False,
                )
            with self.assertRaisesRegex(PermissionError, "20 paper-canary"):
                store.promote_live(
                    candidate.candidate_id,
                    approval_ref="approved-by-user",
                    canary_days=19,
                    live_apply_enabled=True,
                )
            store.promote_live(
                candidate.candidate_id,
                approval_ref="approved-by-user",
                canary_days=20,
                live_apply_enabled=True,
            )
            status = store.status()
            live = next(item for item in status["active"] if item["environment"] == "live")
            self.assertEqual(live["candidate_id"], candidate.candidate_id)

    def test_rollback_needs_audit_reason(self):
        with TemporaryDirectory() as directory:
            store = EvolutionStore(Path(directory) / "serenity.db")
            store.migrate()
            with self.assertRaises(ValueError):
                store.rollback_live(reason="", approval_ref="ref")

    def test_ic_evidence_idempotent_insert(self):
        """同一天同因子重复写入应是幂等操作 (INSERT OR IGNORE)。"""
        import json
        from evolution_bridge import _ensure_evidence_table

        with TemporaryDirectory() as directory:
            store = EvolutionStore(Path(directory) / "serenity.db")
            store.migrate()
            _ensure_evidence_table(store)

            now = "2026-07-11T12:00:00+00:00"
            collected_on = "2026-07-11"

            with store.connect() as db:
                # 第一次写入
                db.execute(
                    "INSERT OR IGNORE INTO evolution_ic_evidence "
                    "(collected_at, collected_on, factor, sample_count, ic_values_json, "
                    "as_of_date, outcome_horizon, strategy_version) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (now, collected_on, "momentum", 50, json.dumps([0.1]*50),
                     "2026-07-10", "1d", "frozen-v1"),
                )
                # 第二次写入同 key → 应被忽略
                db.execute(
                    "INSERT OR IGNORE INTO evolution_ic_evidence "
                    "(collected_at, collected_on, factor, sample_count, ic_values_json, "
                    "as_of_date, outcome_horizon, strategy_version) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (now, collected_on, "momentum", 50, json.dumps([0.1]*50),
                     "2026-07-10", "1d", "frozen-v1"),
                )
                count = db.execute(
                    "SELECT COUNT(*) as cnt FROM evolution_ic_evidence "
                    "WHERE factor='momentum' AND collected_on='2026-07-11'"
                ).fetchone()["cnt"]
                self.assertEqual(count, 1, f"幂等失败: 期望 1 行，实际 {count}")

    def test_ic_evidence_unique_constraint(self):
        """不同 horizon 的同一因子记录互不冲突。"""
        import json
        from evolution_bridge import _ensure_evidence_table

        with TemporaryDirectory() as directory:
            store = EvolutionStore(Path(directory) / "serenity.db")
            store.migrate()
            _ensure_evidence_table(store)

            now = "2026-07-11T12:00:00+00:00"
            with store.connect() as db:
                for horizon in ("1d", "5d", "10d"):
                    db.execute(
                        "INSERT OR IGNORE INTO evolution_ic_evidence "
                        "(collected_at, collected_on, factor, sample_count, ic_values_json, "
                        "as_of_date, outcome_horizon, strategy_version) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (now, "2026-07-11", "momentum", 50,
                         json.dumps([0.1]*50), "2026-07-10", horizon, "frozen-v1"),
                    )
                count = db.execute(
                    "SELECT COUNT(DISTINCT outcome_horizon) as cnt FROM evolution_ic_evidence"
                ).fetchone()["cnt"]
                self.assertEqual(count, 3, f"不同 horizon 应独立存储，实际 {count} 种")
