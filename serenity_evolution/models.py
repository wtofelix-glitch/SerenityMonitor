from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class Stage(str, Enum):
    REJECTED = "REJECTED"
    PAPER_CANDIDATE = "PAPER_CANDIDATE"
    PAPER_CANARY = "PAPER_CANARY"
    LIVE_PENDING_APPROVAL = "LIVE_PENDING_APPROVAL"
    LIVE = "LIVE"
    ROLLED_BACK = "ROLLED_BACK"


@dataclass(frozen=True)
class MarketBar:
    trade_date: date
    code: str
    open: float
    high: float
    low: float
    close: float
    prev_close: float
    volume: int
    suspended: bool = False
    is_st: bool = False
    no_price_limit: bool = False


@dataclass(frozen=True)
class Order:
    code: str
    side: Side
    quantity: int
    submitted_at: datetime
    limit_price: float | None = None


@dataclass(frozen=True)
class PositionLot:
    code: str
    quantity: int
    acquired_on: date


@dataclass(frozen=True)
class FillResult:
    filled: bool
    reason: str
    quantity: int = 0
    price: float | None = None
    gross_value: float = 0.0
    commission: float = 0.0
    stamp_tax: float = 0.0
    slippage_cost: float = 0.0

    @property
    def total_cost(self) -> float:
        return self.commission + self.stamp_tax + self.slippage_cost


@dataclass(frozen=True)
class StrategyMetrics:
    observations: int
    trades: int
    total_return: float
    annual_return: float
    annual_volatility: float
    sharpe: float
    max_drawdown: float
    win_rate: float
    profit_factor: float
    turnover: float
    executable_rate: float


@dataclass(frozen=True)
class ComparisonMetrics:
    candidate: StrategyMetrics
    baseline: StrategyMetrics
    excess_return: float
    sharpe_delta: float
    drawdown_delta: float
    bootstrap_probability: float
    bootstrap_excess_lower: float
    data_quality_ok: bool = True
    costs_included: bool = True
    market_rules_included: bool = True


@dataclass(frozen=True)
class GateResult:
    passed: bool
    stage: Stage
    failures: tuple[str, ...]
    checks: dict[str, bool]


@dataclass(frozen=True)
class EvolutionCandidate:
    candidate_id: str
    created_at: datetime
    baseline_version: str
    weights: dict[str, float]
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["created_at"] = self.created_at.astimezone(timezone.utc).isoformat()
        return value
