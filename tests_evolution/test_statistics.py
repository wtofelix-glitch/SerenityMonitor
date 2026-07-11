import unittest

from serenity_evolution.statistics import calculate_metrics, paired_block_bootstrap


class StatisticsTests(unittest.TestCase):
    def test_metrics_compound_and_drawdown(self):
        result = calculate_metrics(
            [0.10, -0.05, 0.02],
            [0.10, -0.05],
            turnover=0.5,
            executable_rate=1.0,
        )
        self.assertAlmostEqual(result.total_return, 1.10 * 0.95 * 1.02 - 1)
        self.assertAlmostEqual(result.max_drawdown, 0.05)
        self.assertEqual(result.win_rate, 0.5)

    def test_bootstrap_detects_consistent_excess(self):
        candidate = [0.0015 + (i % 3) * 0.0001 for i in range(120)]
        baseline = [0.0001 + (i % 3) * 0.0001 for i in range(120)]
        probability, lower = paired_block_bootstrap(candidate, baseline, samples=300)
        self.assertEqual(probability, 1.0)
        self.assertGreater(lower, 0)
