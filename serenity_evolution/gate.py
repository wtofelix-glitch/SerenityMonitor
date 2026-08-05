from __future__ import annotations

from dataclasses import dataclass

from .models import ComparisonMetrics, GateResult, Stage


@dataclass(frozen=True)
class GatePolicy:
    min_oos_days: int = 120
    min_trades: int = 30
    min_bootstrap_probability: float = 0.95
    min_bootstrap_excess_lower: float = 0.0
    min_sharpe_delta: float = 0.15
    max_drawdown_degradation: float = 0.02
    min_executable_rate: float = 0.98
    max_turnover_multiple: float = 2.0


class PromotionGate:
    def __init__(self, policy: GatePolicy | None = None) -> None:
        self.policy = policy or GatePolicy()

    def evaluate(self, comparison: ComparisonMetrics) -> GateResult:
        c = comparison.candidate
        b = comparison.baseline
        turnover_cap = max(b.turnover * self.policy.max_turnover_multiple, b.turnover + 0.01)
        checks = {
            "oos_days": c.observations >= self.policy.min_oos_days,
            "trade_count": c.trades >= self.policy.min_trades,
            "positive_excess": comparison.excess_return > 0,
            "bootstrap_probability": comparison.bootstrap_probability >= self.policy.min_bootstrap_probability,
            "bootstrap_lower_bound": comparison.bootstrap_excess_lower > self.policy.min_bootstrap_excess_lower,
            "sharpe_improvement": comparison.sharpe_delta >= self.policy.min_sharpe_delta,
            "drawdown_guard": comparison.drawdown_delta <= self.policy.max_drawdown_degradation,
            "turnover_guard": c.turnover <= turnover_cap,
            "execution_feasibility": c.executable_rate >= self.policy.min_executable_rate,
            "data_quality": comparison.data_quality_ok,
            "costs_included": comparison.costs_included,
            "market_rules_included": comparison.market_rules_included,
        }
        failures = tuple(name for name, passed in checks.items() if not passed)
        return GateResult(
            passed=not failures,
            stage=Stage.PAPER_CANARY if not failures else Stage.REJECTED,
            failures=failures,
            checks=checks,
        )
