"""
测试成交模拟器 — execution_simulator.py

覆盖:
  [x] 基础成交模拟（含滑点）
  [x] 交易成本计算（佣金/印花税/过户费）
  [x] 滑点估算（基础/中小盘/大单）
  [x] 冲击成本（参与率阈值）
  [x] 成本预估工具函数
  [x] 买卖双向成本差异
"""

import pytest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date
from execution_simulator import (
    ExecutionSimulator, Order, Bar, LiquidityState,
    FillResult, build_liquidity_state,
    COMMISSION_RATE, STAMP_TAX_RATE, TRANSFER_FEE_RATE,
)


def make_order(code="002281", action="buy", price=200.0, quantity=300):
    return Order(code=code, action=action, price=price,
                 quantity=quantity, order_date=date.today())


def make_bar(code="002281", close=200.0, open=198.0, high=202.0,
             low=197.0, volume=1000000, amount=200000000):
    return Bar(code=code, date="2026-07-05", open=open, close=close,
               high=high, low=low, volume=volume, amount=amount)


def make_liquidity(avg_vol=2000000, avg_amount=400000000,
                   total_mv=5e10, is_small=False):
    return LiquidityState(
        avg_daily_volume=avg_vol,
        avg_daily_amount=avg_amount,
        total_mv=total_mv,
        is_small_cap=is_small,
        spread_estimate=0.002 if is_small else 0.001,
    )


class TestBasicFill:
    """基础成交模拟"""

    def test_buy_fills_at_close_plus_slippage(self):
        sim = ExecutionSimulator()
        order = make_order(action="buy", price=200.0, quantity=300)
        bar = make_bar(close=200.0)
        liq = make_liquidity()

        result = sim.simulate_fill(order, bar, liq)
        assert result.filled
        assert result.fill_quantity == 300
        # 买入: 滑点使成交价略高于收盘价
        assert result.fill_price >= bar.close

    def test_sell_fills_at_close_minus_slippage(self):
        sim = ExecutionSimulator()
        order = make_order(action="sell", price=200.0, quantity=300)
        bar = make_bar(close=200.0)
        liq = make_liquidity()

        result = sim.simulate_fill(order, bar, liq)
        assert result.filled
        # 卖出: 滑点使成交价略低于收盘价
        assert result.fill_price <= bar.close * 1.001  # 允许小误差

    def test_full_quantity_filled(self):
        sim = ExecutionSimulator()
        order = make_order(quantity=500)
        bar = make_bar()
        liq = make_liquidity()
        result = sim.simulate_fill(order, bar, liq)
        assert result.fill_quantity == 500
        assert result.filled


class TestTradingCosts:
    """交易成本计算"""

    def test_buy_costs_no_stamp_tax(self):
        """买入不收印花税"""
        sim = ExecutionSimulator()
        order = make_order(action="buy", price=200.0, quantity=300)
        bar = make_bar(close=200.0)
        liq = make_liquidity()

        result = sim.simulate_fill(order, bar, liq)
        assert result.stamp_tax == 0.0
        assert result.commission > 0

    def test_sell_costs_include_stamp_tax(self):
        """卖出收印花税"""
        sim = ExecutionSimulator()
        order = make_order(action="sell", price=200.0, quantity=300)
        bar = make_bar(close=200.0)
        liq = make_liquidity()

        result = sim.simulate_fill(order, bar, liq)
        assert result.stamp_tax > 0

    def test_commission_minimum_5_yuan(self):
        """佣金最低 5 元"""
        sim = ExecutionSimulator()
        # 极小单: 100 股 × 10 元 = 1000 元
        order = make_order(action="buy", price=10.0, quantity=100)
        bar = make_bar(close=10.0)
        liq = make_liquidity(avg_vol=5000000, avg_amount=50000000)
        result = sim.simulate_fill(order, bar, liq)
        assert result.commission >= 5.0

    def test_transfer_fee_present(self):
        """过户费存在"""
        sim = ExecutionSimulator()
        order = make_order(action="buy", price=200.0, quantity=300)
        bar = make_bar(close=200.0)
        liq = make_liquidity()
        result = sim.simulate_fill(order, bar, liq)
        assert result.transfer_fee > 0

    def test_total_cost_reasonable(self):
        """总交易成本在合理范围（<2%）"""
        sim = ExecutionSimulator()
        order = make_order(action="buy", price=200.0, quantity=300)
        bar = make_bar(close=200.0)
        liq = make_liquidity()
        result = sim.simulate_fill(order, bar, liq)
        assert result.total_cost_pct < 0.02  # <2%

    def test_cost_estimate_function(self):
        """成本预估函数"""
        sim = ExecutionSimulator()
        liq = make_liquidity()
        cost = sim.compute_trading_cost(200.0, 300, "buy", liq)
        assert "total_cost" in cost
        assert "total_cost_pct" in cost
        assert cost["stamp_tax"] == 0.0  # 买入无印花税

    def test_sell_cost_estimate_includes_stamp_tax(self):
        sim = ExecutionSimulator()
        liq = make_liquidity()
        cost = sim.compute_trading_cost(200.0, 300, "sell", liq)
        assert cost["stamp_tax"] > 0


class TestSlippage:
    """滑点估算"""

    def test_small_cap_higher_slippage(self):
        sim = ExecutionSimulator()
        liq_small = make_liquidity(is_small=True)
        liq_large = make_liquidity(is_small=False)
        slip_small = sim.estimate_slippage("002281", 300, "buy", liq_small)
        slip_large = sim.estimate_slippage("002281", 300, "buy", liq_large)
        assert slip_small > slip_large

    def test_large_order_higher_slippage(self):
        sim = ExecutionSimulator()
        liq = make_liquidity(avg_vol=100000)
        slip_small = sim.estimate_slippage("002281", 100, "buy", liq)
        slip_large = sim.estimate_slippage("002281", 10000, "buy", liq)
        assert slip_large > slip_small

    def test_slippage_capped(self):
        sim = ExecutionSimulator()
        liq = make_liquidity(avg_vol=100, is_small=True)
        slip = sim.estimate_slippage("002281", 100000, "buy", liq)
        assert slip <= 0.01  # max 1%


class TestImpactCost:
    """冲击成本"""

    def test_no_impact_for_small_trade(self):
        sim = ExecutionSimulator()
        liq = make_liquidity(avg_amount=1e9)  # 日均 10 亿成交额
        impact = sim.estimate_impact(100, 200.0, liq)
        assert impact == 0.0  # 参与率 < 2%

    def test_impact_for_large_trade(self):
        sim = ExecutionSimulator()
        liq = make_liquidity(avg_amount=1e7)  # 日均 1000 万成交额
        impact = sim.estimate_impact(50000, 200.0, liq)
        # 参与率 = 10000000 / 10000000 = 100% → impact > 0
        assert impact > 0

    def test_impact_capped(self):
        sim = ExecutionSimulator()
        liq = make_liquidity(avg_amount=1e6)
        impact = sim.estimate_impact(1000000, 200.0, liq)
        assert impact <= 0.02  # max 2%


class TestPriceDeviation:
    """信号价格 vs 实际成交"""

    def test_signal_price_recorded(self):
        sim = ExecutionSimulator()
        order = make_order(price=200.0, quantity=300)
        bar = make_bar(close=205.0)
        liq = make_liquidity()
        result = sim.simulate_fill(order, bar, liq)
        assert result.signal_price == 200.0
        # 成交价 vs 信号价有偏差
        assert result.price_deviation_pct != 0.0
