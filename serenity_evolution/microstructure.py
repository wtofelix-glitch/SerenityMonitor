from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from .models import FillResult, MarketBar, Order, PositionLot, Side


def _money(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _price(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class AShareRules:
    """Versioned assumptions; broker-specific charges must be overridden."""

    effective_from: date = date(2023, 8, 28)
    board_lot: int = 100
    commission_rate: float = 0.0003
    minimum_commission: float = 5.0
    stamp_tax_sell_rate: float = 0.0005
    slippage_bps: float = 8.0
    main_board_limit: float = 0.10
    st_limit: float = 0.05
    limit_tolerance: float = 0.001


class AShareExecutionSimulator:
    def __init__(self, rules: AShareRules | None = None) -> None:
        self.rules = rules or AShareRules()

    def limit_prices(self, bar: MarketBar) -> tuple[float | None, float | None]:
        if bar.no_price_limit:
            return None, None
        ratio = self.rules.st_limit if bar.is_st else self.rules.main_board_limit
        return _price(bar.prev_close * (1 + ratio)), _price(bar.prev_close * (1 - ratio))

    def simulate(
        self,
        order: Order,
        bar: MarketBar,
        lots: tuple[PositionLot, ...] = (),
    ) -> FillResult:
        if order.code != bar.code:
            return FillResult(False, "CODE_MISMATCH")
        if bar.suspended or bar.volume <= 0:
            return FillResult(False, "SUSPENDED_OR_NO_LIQUIDITY")
        if order.quantity <= 0:
            return FillResult(False, "INVALID_QUANTITY")
        if order.side is Side.BUY and order.quantity % self.rules.board_lot:
            return FillResult(False, "BUY_NOT_BOARD_LOT")

        available = sum(
            lot.quantity
            for lot in lots
            if lot.code == order.code and lot.acquired_on < bar.trade_date
        )
        if order.side is Side.SELL and order.quantity > available:
            return FillResult(False, "T1_LOCKED_OR_INSUFFICIENT_POSITION")

        limit_up, limit_down = self.limit_prices(bar)
        if limit_up is not None and order.side is Side.BUY:
            one_price_limit = abs(bar.high - bar.low) <= self.rules.limit_tolerance and bar.low >= limit_up
            if one_price_limit:
                return FillResult(False, "ONE_PRICE_LIMIT_UP")
        if limit_down is not None and order.side is Side.SELL:
            one_price_limit = abs(bar.high - bar.low) <= self.rules.limit_tolerance and bar.high <= limit_down
            if one_price_limit:
                return FillResult(False, "ONE_PRICE_LIMIT_DOWN")

        direction = 1 if order.side is Side.BUY else -1
        raw_price = bar.open * (1 + direction * self.rules.slippage_bps / 10_000)
        fill_price = _price(raw_price)
        if order.limit_price is not None:
            if order.side is Side.BUY and fill_price > order.limit_price:
                return FillResult(False, "BUY_LIMIT_NOT_REACHED")
            if order.side is Side.SELL and fill_price < order.limit_price:
                return FillResult(False, "SELL_LIMIT_NOT_REACHED")

        gross = _money(fill_price * order.quantity)
        reference = bar.open * order.quantity
        commission = _money(max(self.rules.minimum_commission, gross * self.rules.commission_rate))
        stamp = _money(gross * self.rules.stamp_tax_sell_rate) if order.side is Side.SELL else 0.0
        slippage = _money(abs(gross - reference))
        return FillResult(
            True,
            "FILLED",
            quantity=order.quantity,
            price=fill_price,
            gross_value=gross,
            commission=commission,
            stamp_tax=stamp,
            slippage_cost=slippage,
        )
