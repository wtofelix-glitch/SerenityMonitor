from __future__ import annotations

import math
import random
from collections.abc import Sequence

from .models import StrategyMetrics


TRADING_DAYS = 252


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _sample_std(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    avg = _mean(values)
    return math.sqrt(sum((x - avg) ** 2 for x in values) / (len(values) - 1))


def compounded_return(returns: Sequence[float]) -> float:
    wealth = 1.0
    for value in returns:
        wealth *= 1.0 + value
    return wealth - 1.0


def max_drawdown(returns: Sequence[float]) -> float:
    wealth = 1.0
    peak = 1.0
    worst = 0.0
    for value in returns:
        wealth *= 1.0 + value
        peak = max(peak, wealth)
        worst = min(worst, wealth / peak - 1.0)
    return abs(worst)


def calculate_metrics(
    daily_returns: Sequence[float],
    trade_returns: Sequence[float],
    *,
    turnover: float,
    executable_rate: float,
    risk_free_annual: float = 0.0,
) -> StrategyMetrics:
    observations = len(daily_returns)
    total = compounded_return(daily_returns)
    annual = (1 + total) ** (TRADING_DAYS / observations) - 1 if observations and total > -1 else -1.0
    daily_std = _sample_std(daily_returns)
    volatility = daily_std * math.sqrt(TRADING_DAYS)
    annual_mean = _mean(daily_returns) * TRADING_DAYS
    sharpe = (annual_mean - risk_free_annual) / volatility if volatility else 0.0
    wins = [x for x in trade_returns if x > 0]
    losses = [x for x in trade_returns if x < 0]
    win_rate = len(wins) / len(trade_returns) if trade_returns else 0.0
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = gross_profit / gross_loss if gross_loss else (float("inf") if gross_profit else 0.0)
    return StrategyMetrics(
        observations=observations,
        trades=len(trade_returns),
        total_return=total,
        annual_return=annual,
        annual_volatility=volatility,
        sharpe=sharpe,
        max_drawdown=max_drawdown(daily_returns),
        win_rate=win_rate,
        profit_factor=profit_factor,
        turnover=turnover,
        executable_rate=executable_rate,
    )


def paired_block_bootstrap(
    candidate: Sequence[float],
    baseline: Sequence[float],
    *,
    samples: int = 2_000,
    block_size: int = 5,
    seed: int = 20260711,
) -> tuple[float, float]:
    """Return P(candidate excess > 0) and the 5th percentile excess return."""
    if len(candidate) != len(baseline) or not candidate:
        raise ValueError("candidate and baseline must have the same non-zero length")
    rng = random.Random(seed)
    differences: list[float] = []
    length = len(candidate)
    for _ in range(samples):
        selected: list[int] = []
        while len(selected) < length:
            start = rng.randrange(length)
            selected.extend((start + offset) % length for offset in range(block_size))
        selected = selected[:length]
        candidate_return = compounded_return([candidate[i] for i in selected])
        baseline_return = compounded_return([baseline[i] for i in selected])
        differences.append(candidate_return - baseline_return)
    differences.sort()
    probability = sum(value > 0 for value in differences) / samples
    lower = differences[max(0, int(samples * 0.05) - 1)]
    return probability, lower
