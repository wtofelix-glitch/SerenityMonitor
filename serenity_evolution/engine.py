from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .gate import PromotionGate
from .models import ComparisonMetrics, EvolutionCandidate, GateResult
from .statistics import calculate_metrics, paired_block_bootstrap
from .store import EvolutionStore


@dataclass(frozen=True)
class StrategySeries:
    daily_returns: Sequence[float]
    trade_returns: Sequence[float]
    turnover: float
    executable_rate: float


class EvolutionEngine:
    def __init__(self, store: EvolutionStore, gate: PromotionGate | None = None) -> None:
        self.store = store
        self.gate = gate or PromotionGate()

    def register(self, candidate: EvolutionCandidate) -> None:
        self.store.save_candidate(candidate)

    def evaluate(
        self,
        candidate_id: str,
        candidate: StrategySeries,
        baseline: StrategySeries,
        *,
        data_quality_ok: bool,
        costs_included: bool,
        market_rules_included: bool,
    ) -> tuple[ComparisonMetrics, GateResult]:
        candidate_metrics = calculate_metrics(
            candidate.daily_returns,
            candidate.trade_returns,
            turnover=candidate.turnover,
            executable_rate=candidate.executable_rate,
        )
        baseline_metrics = calculate_metrics(
            baseline.daily_returns,
            baseline.trade_returns,
            turnover=baseline.turnover,
            executable_rate=baseline.executable_rate,
        )
        probability, lower = paired_block_bootstrap(
            candidate.daily_returns, baseline.daily_returns
        )
        comparison = ComparisonMetrics(
            candidate=candidate_metrics,
            baseline=baseline_metrics,
            excess_return=candidate_metrics.total_return - baseline_metrics.total_return,
            sharpe_delta=candidate_metrics.sharpe - baseline_metrics.sharpe,
            drawdown_delta=candidate_metrics.max_drawdown - baseline_metrics.max_drawdown,
            bootstrap_probability=probability,
            bootstrap_excess_lower=lower,
            data_quality_ok=data_quality_ok,
            costs_included=costs_included,
            market_rules_included=market_rules_included,
        )
        result = self.gate.evaluate(comparison)
        self.store.record_evaluation(candidate_id, result, comparison)
        return comparison, result
