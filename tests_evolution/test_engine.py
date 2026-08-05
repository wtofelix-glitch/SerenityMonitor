import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from serenity_evolution.candidate import FactorEvidence, WeightCandidateGenerator
from serenity_evolution.engine import EvolutionEngine, StrategySeries
from serenity_evolution.store import EvolutionStore


class EngineTests(unittest.TestCase):
    def test_end_to_end_promotes_only_to_paper_canary(self):
        with TemporaryDirectory() as directory:
            store = EvolutionStore(Path(directory) / "serenity.db")
            store.migrate()
            candidate = WeightCandidateGenerator(min_samples=50).generate(
                "frozen-v1",
                {"factor": 0.5, "momentum": 0.5},
                [
                    FactorEvidence("factor", tuple(0.08 + (i % 3) * 0.001 for i in range(50))),
                    FactorEvidence("momentum", tuple(0.02 + (i % 3) * 0.001 for i in range(50))),
                ],
            )
            engine = EvolutionEngine(store)
            engine.register(candidate)
            candidate_series = StrategySeries(
                daily_returns=[0.0015 + (i % 5) * 0.0001 for i in range(120)],
                trade_returns=[0.02 if i % 3 else -0.01 for i in range(40)],
                turnover=1.0,
                executable_rate=0.99,
            )
            baseline_series = StrategySeries(
                daily_returns=[0.0001 + (i % 5) * 0.0001 for i in range(120)],
                trade_returns=[0.01 if i % 2 else -0.01 for i in range(40)],
                turnover=0.8,
                executable_rate=0.99,
            )
            _, result = engine.evaluate(
                candidate.candidate_id,
                candidate_series,
                baseline_series,
                data_quality_ok=True,
                costs_included=True,
                market_rules_included=True,
            )
            self.assertTrue(result.passed)
            self.assertEqual(result.stage.value, "PAPER_CANARY")
            status = store.status()
            paper = next(x for x in status["active"] if x["environment"] == "paper")
            live = next(x for x in status["active"] if x["environment"] == "live")
            self.assertEqual(paper["candidate_id"], candidate.candidate_id)
            self.assertIsNone(live["candidate_id"])
