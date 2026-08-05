from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from typing import Iterable

from .models import EvolutionCandidate


@dataclass(frozen=True)
class FactorEvidence:
    factor: str
    ic_values: tuple[float, ...]


class WeightCandidateGenerator:
    """Generate a bounded, shrunk weekly candidate from factor IC evidence."""

    def __init__(
        self,
        *,
        min_samples: int = 50,
        max_weekly_delta: float = 0.02,
        shrinkage: float = 0.50,
        z_penalty: float = 1.28,
        min_weight: float = 0.02,
        max_weight: float = 0.35,
    ) -> None:
        self.min_samples = min_samples
        self.max_weekly_delta = max_weekly_delta
        self.shrinkage = shrinkage
        self.z_penalty = z_penalty
        self.min_weight = min_weight
        self.max_weight = max_weight

    @staticmethod
    def _reliability(values: tuple[float, ...], z_penalty: float) -> float:
        if len(values) < 2:
            return 0.0
        mean = sum(values) / len(values)
        variance = sum((x - mean) ** 2 for x in values) / (len(values) - 1)
        lower = mean - z_penalty * math.sqrt(variance / len(values))
        return max(0.0, lower)

    @staticmethod
    def _normalize(weights: dict[str, float]) -> dict[str, float]:
        total = sum(weights.values())
        if total <= 0:
            raise ValueError("weights must have a positive sum")
        return {name: value / total for name, value in weights.items()}

    def generate(
        self,
        baseline_version: str,
        baseline_weights: dict[str, float],
        evidence: Iterable[FactorEvidence],
        *,
        created_at: datetime | None = None,
    ) -> EvolutionCandidate:
        baseline = self._normalize(baseline_weights)
        evidence_map = {item.factor: item.ic_values for item in evidence}
        if set(evidence_map) != set(baseline):
            raise ValueError("IC evidence must exactly match baseline factor names")
        insufficient = [name for name, values in evidence_map.items() if len(values) < self.min_samples]
        if insufficient:
            raise ValueError(f"insufficient IC samples: {', '.join(sorted(insufficient))}")

        reliabilities = {
            name: self._reliability(values, self.z_penalty)
            for name, values in evidence_map.items()
        }
        reliability_total = sum(reliabilities.values())
        target = baseline if reliability_total == 0 else {
            name: reliabilities[name] / reliability_total for name in baseline
        }
        bounded: dict[str, float] = {}
        for name, old in baseline.items():
            desired = old + self.shrinkage * (target[name] - old)
            delta = max(-self.max_weekly_delta, min(self.max_weekly_delta, desired - old))
            bounded[name] = max(self.min_weight, min(self.max_weight, old + delta))
        weights = self._normalize(bounded)

        timestamp = created_at or datetime.now(timezone.utc)
        fingerprint = sha256(
            (baseline_version + repr(sorted(weights.items())) + timestamp.date().isoformat()).encode()
        ).hexdigest()[:16]
        return EvolutionCandidate(
            candidate_id=f"evo-{timestamp:%Y%m%d}-{fingerprint}",
            created_at=timestamp,
            baseline_version=baseline_version,
            weights=weights,
            evidence={
                "samples": {name: len(values) for name, values in evidence_map.items()},
                "reliability": reliabilities,
                "policy": {
                    "frequency": "weekly",
                    "max_weekly_delta": self.max_weekly_delta,
                    "shrinkage": self.shrinkage,
                },
            },
        )
