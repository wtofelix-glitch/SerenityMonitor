import unittest

from serenity_evolution.gate import PromotionGate
from serenity_evolution.models import ComparisonMetrics, StrategyMetrics


def metrics(**overrides):
    values = dict(
        observations=120,
        trades=40,
        total_return=0.20,
        annual_return=0.25,
        annual_volatility=0.12,
        sharpe=1.5,
        max_drawdown=0.08,
        win_rate=0.55,
        profit_factor=1.4,
        turnover=1.0,
        executable_rate=0.99,
    )
    values.update(overrides)
    return StrategyMetrics(**values)


def comparison(**overrides):
    candidate = overrides.pop("candidate", metrics())
    baseline = overrides.pop("baseline", metrics(total_return=0.10, sharpe=1.0, turnover=0.8))
    values = dict(
        candidate=candidate,
        baseline=baseline,
        excess_return=0.10,
        sharpe_delta=0.5,
        drawdown_delta=0.0,
        bootstrap_probability=0.98,
        bootstrap_excess_lower=0.02,
        data_quality_ok=True,
        costs_included=True,
        market_rules_included=True,
    )
    values.update(overrides)
    return ComparisonMetrics(**values)


class GateTests(unittest.TestCase):
    def test_all_hard_gates_are_required(self):
        result = PromotionGate().evaluate(comparison())
        self.assertTrue(result.passed)
        self.assertEqual(result.stage.value, "PAPER_CANARY")

    def test_high_win_rate_cannot_override_bad_drawdown(self):
        candidate = metrics(win_rate=0.90, max_drawdown=0.20)
        result = PromotionGate().evaluate(comparison(candidate=candidate, drawdown_delta=0.12))
        self.assertFalse(result.passed)
        self.assertIn("drawdown_guard", result.failures)

    def test_missing_market_rules_rejects_candidate(self):
        result = PromotionGate().evaluate(comparison(market_rules_included=False))
        self.assertFalse(result.passed)
        self.assertIn("market_rules_included", result.failures)
