from datetime import date, datetime, timezone
import unittest

from serenity_evolution.microstructure import AShareExecutionSimulator
from serenity_evolution.models import MarketBar, Order, PositionLot, Side


def bar(**overrides):
    values = dict(
        trade_date=date(2026, 7, 10),
        code="600900",
        open=10.0,
        high=10.2,
        low=9.8,
        close=10.1,
        prev_close=10.0,
        volume=1_000_000,
    )
    values.update(overrides)
    return MarketBar(**values)


def order(side=Side.BUY, quantity=100):
    return Order("600900", side, quantity, datetime(2026, 7, 10, tzinfo=timezone.utc))


class MicrostructureTests(unittest.TestCase):
    def test_buy_requires_board_lot(self):
        result = AShareExecutionSimulator().simulate(order(quantity=150), bar())
        self.assertFalse(result.filled)
        self.assertEqual(result.reason, "BUY_NOT_BOARD_LOT")

    def test_t1_blocks_same_day_sale(self):
        lot = PositionLot("600900", 100, date(2026, 7, 10))
        result = AShareExecutionSimulator().simulate(order(Side.SELL), bar(), (lot,))
        self.assertFalse(result.filled)
        self.assertEqual(result.reason, "T1_LOCKED_OR_INSUFFICIENT_POSITION")

    def test_previous_day_lot_can_sell_and_pays_stamp_tax(self):
        lot = PositionLot("600900", 100, date(2026, 7, 9))
        result = AShareExecutionSimulator().simulate(order(Side.SELL), bar(), (lot,))
        self.assertTrue(result.filled)
        self.assertGreater(result.stamp_tax, 0)
        self.assertGreaterEqual(result.commission, 5)

    def test_one_price_limit_up_blocks_buy(self):
        result = AShareExecutionSimulator().simulate(
            order(), bar(open=11.0, high=11.0, low=11.0, close=11.0)
        )
        self.assertFalse(result.filled)
        self.assertEqual(result.reason, "ONE_PRICE_LIMIT_UP")

    def test_suspension_blocks_all_orders(self):
        result = AShareExecutionSimulator().simulate(order(), bar(suspended=True, volume=0))
        self.assertFalse(result.filled)
        self.assertEqual(result.reason, "SUSPENDED_OR_NO_LIQUIDITY")
