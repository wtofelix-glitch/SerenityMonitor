import unittest

from serenity_evolution.candidate import FactorEvidence, WeightCandidateGenerator


class CandidateTests(unittest.TestCase):
    def test_candidate_is_normalized_and_bounded(self):
        generator = WeightCandidateGenerator(min_samples=5, max_weekly_delta=0.02)
        baseline = {"factor": 0.5, "momentum": 0.5}
        evidence = [
            FactorEvidence("factor", (0.10, 0.11, 0.12, 0.09, 0.10)),
            FactorEvidence("momentum", (-0.02, -0.01, 0.00, -0.03, -0.02)),
        ]
        candidate = generator.generate("frozen-v1", baseline, evidence)
        self.assertAlmostEqual(sum(candidate.weights.values()), 1.0)
        self.assertLessEqual(abs(candidate.weights["factor"] - 0.5), 0.021)
        self.assertLessEqual(abs(candidate.weights["momentum"] - 0.5), 0.021)
        self.assertEqual(candidate.evidence["policy"]["frequency"], "weekly")

    def test_candidate_refuses_small_sample(self):
        generator = WeightCandidateGenerator(min_samples=5)
        with self.assertRaisesRegex(ValueError, "insufficient IC samples"):
            generator.generate(
                "frozen-v1", {"factor": 1.0}, [FactorEvidence("factor", (0.1, 0.2))]
            )

    def test_candidate_refuses_mismatched_factor_set(self):
        generator = WeightCandidateGenerator(min_samples=2)
        with self.assertRaisesRegex(ValueError, "exactly match"):
            generator.generate(
                "frozen-v1",
                {"factor": 1.0},
                [FactorEvidence("momentum", (0.1, 0.2))],
            )
