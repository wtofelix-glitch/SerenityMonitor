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
