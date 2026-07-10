"""
交易成本测试 — execution_simulator.py

覆盖:
  [x] 佣金 0.025% 双向（最低 5 元）
  [x] 印花税 0.05% 卖出单向
  [x] 过户费 0.001% 双向
  [x] 滑点：基础 0.1%、中小盘附加 0.05%
  [x] 冲击成本：参与率 >2% 时计算
  [x] compute_trading_cost 成本预估完整性
  [x] 交易成本占成交金额比例
  [x] 边界：零股/极小成交额
  [x] 滑点方向：买入有不利滑点，卖出有不利滑点
"""

import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date
from execution_simulator import (
    ExecutionSimulator, Order, Bar, LiquidityState, FillResult,
    COMMISSION_RATE, STAMP_TAX_RATE, TRANSFER_FEE_RATE,
    SLIPPAGE_BASIS_POINTS, SLIPPAGE_SMALL_CAP_BONUS,
    IMPACT_MIN_THRESHOLD, IMPACT_FACTOR,
    get_simulator, build_liquidity_state,
)


# ── Fixture helpers ───────────────────────────────────────────

def _make_order(code="002281", action="buy", price=200.0, quantity=300):
    return Order(code=code, action=action, price=price,
                 quantity=quantity, order_date=date.today())


def _make_bar(code="002281", close=200.0):
    return Bar(code=code, date=date.today().isoformat(),
               open=198.0, close=close, high=202.0, low=197.0,
               volume=5e7, amount=1e10, change_pct=1.5)


def _make_liquidity(avg_vol=1e7, avg_amt=2e9, mv=8e10, small_cap=False):
    return LiquidityState(
        avg_daily_volume=avg_vol,
        avg_daily_amount=avg_amt,
        total_mv=mv,
        is_small_cap=small_cap,
    )


# ═══════════════════════════════════════════════════════════════
# 佣金测试
# ═══════════════════════════════════════════════════════════════


class TestCommission:
    """佣金 0.025% 双向，最低 5 元"""

    def test_buy_commission(self):
        sim = ExecutionSimulator()
        order = _make_order(action="buy", price=200.0, quantity=1000)
        bar = _make_bar(close=200.0)
        liquidity = _make_liquidity()

        result = sim.simulate_fill(order, bar, liquidity)

        trade_amount = result.fill_price * result.fill_quantity
        expected = max(5.0, trade_amount * COMMISSION_RATE)
        assert result.commission == pytest.approx(expected, rel=0.01)

    def test_sell_commission(self):
        sim = ExecutionSimulator()
        order = _make_order(action="sell", price=200.0, quantity=500)
        bar = _make_bar(close=200.0)
        liquidity = _make_liquidity()

        result = sim.simulate_fill(order, bar, liquidity)

        trade_amount = result.fill_price * result.fill_quantity
        expected = max(5.0, trade_amount * COMMISSION_RATE)
        assert result.commission == pytest.approx(expected, rel=0.01)

    def test_commission_min_5_yuan(self):
        """小额成交佣金最低 5 元"""
        sim = ExecutionSimulator()
        order = _make_order(action="buy", price=10.0, quantity=100)
        bar = _make_bar(close=10.0)
        liquidity = _make_liquidity()

        result = sim.simulate_fill(order, bar, liquidity)

        # 成交额 1000 元 → 佣金 0.25 元 < 5 → 最低 5 元
        assert result.commission == 5.0

    def test_commission_both_directions_equal(self):
        """佣金费率买卖对称"""
        sim = ExecutionSimulator()
        order_buy = _make_order(action="buy", price=200.0, quantity=1000)
        order_sell = _make_order(action="sell", price=200.0, quantity=1000)
        bar = _make_bar()
        liquidity = _make_liquidity()

        buy_result = sim.simulate_fill(order_buy, bar, liquidity)
        sell_result = sim.simulate_fill(order_sell, bar, liquidity)

        assert buy_result.commission == pytest.approx(sell_result.commission, rel=0.01)


# ═══════════════════════════════════════════════════════════════
# 印花税测试
# ═══════════════════════════════════════════════════════════════


class TestStampTax:
    """印花税 0.05% 卖出单向"""

    def test_sell_has_stamp_tax(self):
        sim = ExecutionSimulator()
        order = _make_order(action="sell", price=200.0, quantity=1000)
        bar = _make_bar()
        liquidity = _make_liquidity()

        result = sim.simulate_fill(order, bar, liquidity)
        trade_amount = result.fill_price * result.fill_quantity
        expected = trade_amount * STAMP_TAX_RATE

        assert result.stamp_tax == pytest.approx(expected, rel=0.01)
        assert result.stamp_tax > 0

    def test_buy_no_stamp_tax(self):
        sim = ExecutionSimulator()
        order = _make_order(action="buy", price=200.0, quantity=1000)
        bar = _make_bar()
        liquidity = _make_liquidity()

        result = sim.simulate_fill(order, bar, liquidity)
        assert result.stamp_tax == 0.0

    def test_stamp_tax_proportional(self):
        """印花税与成交额成正比"""
        sim = ExecutionSimulator()

        order_1x = _make_order(action="sell", price=200.0, quantity=500)
        order_2x = _make_order(action="sell", price=200.0, quantity=1000)
        bar = _make_bar()
        liquidity = _make_liquidity()

        r1 = sim.simulate_fill(order_1x, bar, liquidity)
        r2 = sim.simulate_fill(order_2x, bar, liquidity)

        # 2x 成交额 → 约 2x 印花税
        assert r2.stamp_tax == pytest.approx(r1.stamp_tax * 2, rel=0.05)


# ═══════════════════════════════════════════════════════════════
# 过户费测试
# ═══════════════════════════════════════════════════════════════


class TestTransferFee:
    """过户费 0.001% 双向"""

    def test_buy_has_transfer_fee(self):
        sim = ExecutionSimulator()
        order = _make_order(action="buy", price=200.0, quantity=1000)
        bar = _make_bar()
        liquidity = _make_liquidity()

        result = sim.simulate_fill(order, bar, liquidity)
        trade_amount = result.fill_price * result.fill_quantity
        expected = trade_amount * TRANSFER_FEE_RATE

        assert result.transfer_fee == pytest.approx(expected, rel=0.01)
        assert result.transfer_fee > 0

    def test_sell_has_transfer_fee(self):
        sim = ExecutionSimulator()
        order = _make_order(action="sell", price=200.0, quantity=1000)
        bar = _make_bar()
        liquidity = _make_liquidity()

        result = sim.simulate_fill(order, bar, liquidity)
        trade_amount = result.fill_price * result.fill_quantity
        expected = trade_amount * TRANSFER_FEE_RATE

        assert result.transfer_fee == pytest.approx(expected, rel=0.01)

    def test_transfer_fee_both_directions_equal(self):
        """过户费买卖对称"""
        sim = ExecutionSimulator()
        buy = sim.simulate_fill(_make_order("buy"), _make_bar(), _make_liquidity())
        sell = sim.simulate_fill(_make_order("sell"), _make_bar(), _make_liquidity())
        assert buy.transfer_fee == pytest.approx(sell.transfer_fee, rel=0.01)


# ═══════════════════════════════════════════════════════════════
# 滑点测试
# ═══════════════════════════════════════════════════════════════


class TestSlippage:
    """滑点估算"""

    def test_basic_slippage_nonzero(self):
        sim = ExecutionSimulator()
        slippage = sim.estimate_slippage("002281", 1000, "buy", _make_liquidity())
        # 基础滑点 0.1% + 价差 0.05%
        assert slippage >= SLIPPAGE_BASIS_POINTS
        assert slippage <= 0.01  # 上限 1%

    def test_small_cap_extra_slippage(self):
        sim = ExecutionSimulator()
        liq_large = _make_liquidity(mv=8e10, small_cap=False)
        liq_small = _make_liquidity(mv=3e10, small_cap=True)

        s_large = sim.estimate_slippage("603083", 1000, "buy", liq_large)
        s_small = sim.estimate_slippage("603083", 1000, "buy", liq_small)

        assert s_small >= s_large

    def test_large_order_extra_slippage(self):
        sim = ExecutionSimulator()
        liq = _make_liquidity(avg_vol=1e6)  # 日均 100 万股

        s_small = sim.estimate_slippage("002281", 1000, "buy", liq)
        s_large = sim.estimate_slippage("002281", 50000, "buy", liq)

        assert s_large >= s_small

    def test_slippage_capped(self):
        """滑点硬上限 1%"""
        sim = ExecutionSimulator()
        liq = _make_liquidity(avg_vol=100, small_cap=True)
        liq.spread_estimate = 0.05

        slippage = sim.estimate_slippage("002281", 1000000, "buy", liq)
        assert slippage <= 0.01 + 0.001  # 容忍浮点误差

    def test_buy_slippage_direction(self):
        """买入滑点 → 成交价高于 bar.close（不利方向）"""
        sim = ExecutionSimulator()
        order = _make_order(action="buy", price=200.0, quantity=1000)
        bar = _make_bar(close=200.0)
        liq = _make_liquidity()

        result = sim.simulate_fill(order, bar, liq)
        # 买入：成交价 ≥ bar.close
        assert result.fill_price >= bar.close

    def test_sell_slippage_direction(self):
        """卖出滑点 → 成交价低于 bar.close（不利方向）"""
        sim = ExecutionSimulator()
        order = _make_order(action="sell", price=200.0, quantity=1000)
        bar = _make_bar(close=200.0)
        liq = _make_liquidity()

        result = sim.simulate_fill(order, bar, liq)
        # 卖出：成交价 ≤ bar.close
        assert result.fill_price <= bar.close


# ═══════════════════════════════════════════════════════════════
# 冲击成本测试
# ═══════════════════════════════════════════════════════════════


class TestImpactCost:
    """冲击成本估算"""

    def test_small_trade_no_impact(self):
        """小单（参与率 <2%）无冲击成本"""
        sim = ExecutionSimulator()
        liq = _make_liquidity(avg_amt=1e9)  # 日均成交额 10 亿
        impact = sim.estimate_impact(1000, 200.0, liq)
        assert impact == 0.0

    def test_large_trade_has_impact(self):
        """大单（参与率 >2%）计算冲击成本"""
        sim = ExecutionSimulator()
        liq = _make_liquidity(avg_amt=1e7)  # 日均成交额 1000 万
        impact = sim.estimate_impact(20000, 200.0, liq)

        # 交易 400 万 / 1000 万 = 40% 参与率 → 应有冲击
        assert impact > 0

    def test_impact_capped(self):
        """冲击成本上限 2%"""
        sim = ExecutionSimulator()
        liq = _make_liquidity(avg_amt=1e7)
        impact = sim.estimate_impact(1000000, 200.0, liq)
        assert impact <= 0.02 + 0.001

    def test_impact_zero_avg_amount(self):
        """日均成交额未知 → 冲击成本为 0"""
        sim = ExecutionSimulator()
        liq = _make_liquidity(avg_amt=0)
        impact = sim.estimate_impact(100000, 200.0, liq)
        assert impact == 0.0

    def test_impact_in_buy_direction(self):
        """买入冲击 → 成交价被推高"""
        sim = ExecutionSimulator()
        order = _make_order(action="buy", price=200.0, quantity=50000)
        bar = _make_bar(close=200.0)
        liq = _make_liquidity(avg_amt=5e7)  # 日均 5000 万

        result = sim.simulate_fill(order, bar, liq)
        assert result.impact_cost >= 0


# ═══════════════════════════════════════════════════════════════
# compute_trading_cost 完整性
# ═══════════════════════════════════════════════════════════════


class TestComputeTradingCost:
    """compute_trading_cost 静态成本预估"""

    def test_buy_cost_components(self):
        sim = ExecutionSimulator()
        cost = sim.compute_trading_cost(price=200.0, quantity=1000, action="buy")

        trade_amount = 200.0 * 1000
        assert "commission" in cost
        assert cost["commission"] == max(5.0, trade_amount * COMMISSION_RATE)
        assert cost["stamp_tax"] == 0.0  # 买入无印花税
        assert "transfer_fee" in cost
        assert cost["trade_amount"] == trade_amount
        assert cost["total_cost"] == cost["commission"] + cost["stamp_tax"] + cost["transfer_fee"]

    def test_sell_cost_components(self):
        sim = ExecutionSimulator()
        cost = sim.compute_trading_cost(price=200.0, quantity=1000, action="sell")

        trade_amount = 200.0 * 1000
        assert cost["stamp_tax"] == pytest.approx(trade_amount * STAMP_TAX_RATE)
        assert cost["commission"] > 0

    def test_with_liquidity_adds_slippage(self):
        sim = ExecutionSimulator()
        liq = _make_liquidity()

        cost_no_liq = sim.compute_trading_cost(price=200.0, quantity=1000, action="buy")
        cost_with_liq = sim.compute_trading_cost(
            price=200.0, quantity=1000, action="buy", liquidity=liq)

        # 有流动性参数时多出滑点和冲击估算
        assert cost_with_liq["slippage_est"] > 0
        assert cost_with_liq["total_cost"] > cost_no_liq["total_cost"]

    def test_total_cost_pct_bound(self):
        """总成本占比在合理范围"""
        sim = ExecutionSimulator()
        cost = sim.compute_trading_cost(price=200.0, quantity=1000, action="sell")
        assert 0.0001 < cost["total_cost_pct"] < 0.01  # 0.01% ~ 1%


# ═══════════════════════════════════════════════════════════════
# 边界情况
# ═══════════════════════════════════════════════════════════════


class TestEdgeCases:
    """边界情况"""

    def test_zero_quantity(self):
        """零股订单 — 无成交额但最低佣金 5 元"""
        sim = ExecutionSimulator()
        order = _make_order(quantity=0)
        bar = _make_bar()
        liquidity = _make_liquidity()

        result = sim.simulate_fill(order, bar, liquidity)
        assert result.fill_quantity == 0
        # 0 股 × 200 元 = 0 成交额 → 最低佣金 5 元
        assert result.commission == 5.0

    def test_very_small_trade(self):
        """极小成交额（1 股 @ 10 元）"""
        sim = ExecutionSimulator()
        order = _make_order(price=10.0, quantity=1)
        bar = _make_bar(close=10.0)
        liquidity = _make_liquidity()

        result = sim.simulate_fill(order, bar, liquidity)
        # 最低佣金 5 元
        assert result.commission == 5.0
        # 应该有结果
        assert isinstance(result, FillResult)

    def test_large_trade_no_crash(self):
        """大单不崩溃"""
        sim = ExecutionSimulator()
        order = _make_order(price=200.0, quantity=100000)
        bar = _make_bar(close=200.0)
        liq = _make_liquidity(avg_vol=1e8, avg_amt=2e10)

        result = sim.simulate_fill(order, bar, liq)
        assert isinstance(result, FillResult)
        assert result.filled

    def test_fill_result_all_fields_populated(self):
        """FillResult 所有成本字段都有值"""
        sim = ExecutionSimulator()
        order = _make_order(action="sell", price=150.0, quantity=800)
        bar = _make_bar(close=150.0)
        liq = _make_liquidity()

        result = sim.simulate_fill(order, bar, liq)

        assert result.commission > 0
        assert result.stamp_tax > 0
        assert result.transfer_fee > 0
        assert result.total_cost > 0
        assert result.total_cost_pct > 0
        assert result.signal_price == 150.0


class TestSingleton:
    """get_simulator 单例"""

    def test_get_simulator_returns_instance(self):
        sim = get_simulator()
        assert isinstance(sim, ExecutionSimulator)

    def test_same_instance_returns_same(self):
        s1 = get_simulator()
        s2 = get_simulator()
        assert s1 is s2

    def test_custom_params_returns_new_instance(self):
        s1 = get_simulator()
        s2 = get_simulator(commission_rate=0.0003)
        assert s1 is not s2
        assert s2.commission_rate == 0.0003
