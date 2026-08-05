from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

from .candidate import FactorEvidence
from .engine import StrategySeries


class SerenityEvolutionAdapter(Protocol):
    """Boundary to implement inside the existing SerenityMonitor repository."""

    def frozen_version(self) -> str: ...

    def frozen_weights(self) -> dict[str, float]: ...

    def factor_ic_history(self, min_samples: int) -> Sequence[FactorEvidence]: ...

    def backtest_candidate(self, weights: dict[str, float]) -> StrategySeries: ...

    def backtest_frozen(self) -> StrategySeries: ...

    def data_quality_ok(self) -> bool: ...

    def write_paper_weights(self, candidate_id: str, weights: dict[str, float]) -> None: ...


@dataclass(frozen=True)
class IntegrationHooks:
    """The only approved mutation points in SerenityMonitor."""

    after_outcome_backfill: str = "collect factor IC evidence; never mutate weights"
    weekly_research_job: str = "propose and evaluate one candidate"
    paper_trader: str = "read active paper candidate"
    auto_gate: str = "keep live kernel frozen unless explicit approved promotion exists"
    dashboard: str = "read-only status from evolution_*_v2 tables"
