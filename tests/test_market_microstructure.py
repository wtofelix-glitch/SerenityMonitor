"""
测试 A 股微观结构约束检查 — market_microstructure.py

覆盖:
  [x] T+1 锁定: 当日买入不可卖出
  [x] 涨停不可买入
  [x] 跌停不可卖出
  [x] 停牌不可交易
  [x] 100 股整数手
  [x] T+1 锁定的组合层聚合校验
"""

import pytest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date, timedelta
from market_microstructure import (
    MarketMicrostructure, TradeabilityResult, LimitStatus,
    PositionLock,
)
from check_trading_day import is_trading_day, next_trading_day


class TestLimitStatus:
    """涨跌停状态检测"""

    def test_normal_trading(self):
        ms = MarketMicrostructure(load_existing_locks=False)
        status = ms.get_limit_status("002281", change_pct=1.5)
        assert status == LimitStatus.NORMAL

    def test_limit_up(self):
        ms = MarketMicrostructure(load_existing_locks=False)
        status = ms.get_limit_status("002281", change_pct=9.95)
        assert status == LimitStatus.LIMIT_UP

    def test_limit_down(self):
        ms = MarketMicrostructure(load_existing_locks=False)
        status = ms.get_limit_status("002281", change_pct=-9.95)
        assert status == LimitStatus.LIMIT_DOWN

    def test_limit_up_hard_with_no_volume(self):
        """一字涨停：涨幅 ≥9.9% 且成交量极低"""
        ms = MarketMicrostructure(load_existing_locks=False)
        status = ms.get_limit_status("002281", change_pct=10.0,
                                     volume=50, avg_volume=1000000)
        assert status == LimitStatus.LIMIT_UP_HARD

    def test_limit_down_hard_with_no_volume(self):
        """一字跌停：跌幅 ≥9.9% 且成交量极低"""
        ms = MarketMicrostructure(load_existing_locks=False)
        status = ms.get_limit_status("002281", change_pct=-10.0,
                                     volume=50, avg_volume=1000000)
        assert status == LimitStatus.LIMIT_DOWN_HARD

    def test_not_limit_up_with_volume(self):
        """涨停但有成交量 → 普通涨停, 非一字板"""
        ms = MarketMicrostructure(load_existing_locks=False)
        status = ms.get_limit_status("002281", change_pct=10.0,
                                     volume=500000, avg_volume=1000000)
        assert status == LimitStatus.LIMIT_UP  # vol ratio 0.5 > 0.05

    def test_suspended_blocks_all(self):
        ms = MarketMicrostructure(load_existing_locks=False)
        ms.set_suspended("002281", True)
        assert ms.is_suspended("002281")


class TestCanBuy:
    """买入可成交性检查"""

    def test_normal_buy_allowed(self):
        ms = MarketMicrostructure(load_existing_locks=False)
        today = date.today()
        result = ms.can_buy("002281", today, 200.0, 300)
        assert result.executable

    def test_stock_must_be_mainboard(self):
        ms = MarketMicrostructure(load_existing_locks=False)
        today = date.today()
        # 创业板 300xxx 不在主版范围内
        result = ms.can_buy("300782", today, 100.0, 300)
        assert not result.executable
        assert "主板" in result.block_reason or "mainboard" in result.block_reason.lower()

    def test_suspended_stock_cannot_buy(self):
        ms = MarketMicrostructure(load_existing_locks=False)
        ms.set_suspended("002281", True)
        today = date.today()
        result = ms.can_buy("002281", today, 200.0, 300)
        assert not result.executable
        assert "停牌" in result.block_reason

    def test_limit_up_stock_cannot_buy(self):
        ms = MarketMicrostructure(load_existing_locks=False)
        today = date.today()
        # 使用 get_limit_status 直接测试 — 普通涨停 (有成交量, vol_ratio=0.5)
        status = ms.get_limit_status("002281", change_pct=10.0,
                                     volume=500000, avg_volume=1000000)
        assert status == LimitStatus.LIMIT_UP
        # 通过 can_buy 验证涨停阻止买入
        result = ms.can_buy("002281", today, 200.0, 300,
                           snapshot={"change_pct": 10.0, "volume": 500000})
        assert not result.executable

    def test_quantity_must_be_lot_aligned(self):
        ms = MarketMicrostructure(load_existing_locks=False)
        today = date.today()
        result = ms.can_buy("002281", today, 200.0, 150)  # not 100-aligned
        assert not result.executable
        assert "100" in result.block_reason

    def test_quantity_100_aligned_passes(self):
        ms = MarketMicrostructure(load_existing_locks=False)
        today = date.today()
        result = ms.can_buy("002281", today, 200.0, 300)
        assert result.executable

    def test_zero_quantity_rejected(self):
        ms = MarketMicrostructure(load_existing_locks=False)
        today = date.today()
        result = ms.can_buy("002281", today, 200.0, 0)
        assert not result.executable


class TestCanSell:
    """卖出可成交性检查"""

    def test_normal_sell_allowed(self):
        ms = MarketMicrostructure(load_existing_locks=False)
        today = date.today()
        result = ms.can_sell("002281", 300, today, 200.0, 300)
        assert result.executable

    def test_no_position_cannot_sell(self):
        ms = MarketMicrostructure(load_existing_locks=False)
        today = date.today()
        result = ms.can_sell("002281", 0, today, 200.0, 300)
        assert not result.executable
        assert "持仓" in result.block_reason

    def test_sell_more_than_position_blocked(self):
        ms = MarketMicrostructure(load_existing_locks=False)
        today = date.today()
        result = ms.can_sell("002281", 200, today, 200.0, 500)
        assert not result.executable

    def test_t1_locked_cannot_sell(self):
        """T+1 锁定: 当日买入 → 当日不可卖出"""
        ms = MarketMicrostructure(load_existing_locks=False)
        today = date.today()
        # 手动注入 T+1 锁定
        ms.add_t1_lock("002281", today, 200.0, 300)
        result = ms.can_sell("002281", 300, today, 200.0, 300)
        assert not result.executable
        assert result.t1_locked
        assert result.t1_locked_shares == 300

    def test_limit_down_cannot_sell(self):
        ms = MarketMicrostructure(load_existing_locks=False)
        today = date.today()
        # 使用 get_limit_status 直接测试 — 普通跌停 (有成交量, vol_ratio=0.5)
        status = ms.get_limit_status("002281", change_pct=-10.0,
                                     volume=500000, avg_volume=1000000)
        assert status == LimitStatus.LIMIT_DOWN
        # 通过 can_sell 验证跌停阻止卖出
        result = ms.can_sell("002281", 300, today, 200.0, 300,
                            snapshot={"change_pct": -10.0, "volume": 500000})
        assert not result.executable

    def test_suspended_cannot_sell(self):
        ms = MarketMicrostructure(load_existing_locks=False)
        ms.set_suspended("002281", True)
        today = date.today()
        result = ms.can_sell("002281", 300, today, 200.0, 300)
        assert not result.executable


class TestT1Lock:
    """T+1 锁定状态机"""

    def test_add_lock_creates_correct_unlock_date(self):
        ms = MarketMicrostructure(load_existing_locks=False)
        today = date.today()
        lock = ms.add_t1_lock("002281", today, 200.0, 300)
        assert lock.code == "002281"
        assert lock.shares == 300
        assert lock.buy_price == 200.0
        # unlock_date 应该是下一个交易日
        next_td = next_trading_day(today)
        assert lock.unlock_date == next_td.isoformat()

    def test_is_t1_locked_on_buy_day(self):
        ms = MarketMicrostructure(load_existing_locks=False)
        today = date.today()
        ms.add_t1_lock("002281", today, 200.0, 300)
        assert ms.is_t1_locked("002281", today)

    def test_not_t1_locked_for_non_locked_stock(self):
        ms = MarketMicrostructure(load_existing_locks=False)
        today = date.today()
        assert not ms.is_t1_locked("000988", today)

    def test_t1_lock_aggregate_cap_two_buys(self):
        """v4 §10.2 关键测试: 两笔各 25% 的当日买入
        → T+1 锁定总额 50% → 超过 40% 上限 → 第二笔被拒"""
        ms = MarketMicrostructure(load_existing_locks=False)
        today = date.today()

        # 第一笔买入: 10000 元 (25%)
        ms.add_t1_lock("002281", today, 200.0, 50)  # 50 * 200 = 10000
        price_map = {"002281": 200.0}

        # 第二笔买入: 另外 10000 元 (另外 25%)
        result = ms.check_t1_lock_aggregate(
            "000988", 10000.0, 40000.0, price_map, max_t1_locked_pct=0.40)
        # total: 10000 + 10000 = 20000 / 40000 = 50% > 40%
        assert not result.executable
        assert "50" in result.block_reason and "40%" in result.block_reason

    def test_t1_lock_aggregate_under_limit_passes(self):
        """T+1 锁定总额在限额内 → 通过"""
        ms = MarketMicrostructure(load_existing_locks=False)
        today = date.today()

        ms.add_t1_lock("002281", today, 200.0, 25)  # 5000
        price_map = {"002281": 200.0}

        result = ms.check_t1_lock_aggregate(
            "000988", 5000.0, 40000.0, price_map, max_t1_locked_pct=0.40)
        # total: 5000 + 5000 = 10000 / 40000 = 25% < 40%
        assert result.executable


class TestTradeabilityResult:
    """结果结构体"""

    def test_default_is_executable(self):
        result = TradeabilityResult()
        assert result.executable

    def test_blocked_result_has_reason(self):
        result = TradeabilityResult(
            executable=False,
            block_reason="涨停无法买入",
            checks_failed=["not_limit_up"],
        )
        assert not result.executable
        assert len(result.checks_failed) == 1


class TestMainboardCheck:
    """主板检查"""

    def test_sh_mainboard_passes(self):
        ms = MarketMicrostructure(load_existing_locks=False)
        assert ms._is_mainboard("600036")

    def test_sz_mainboard_passes(self):
        ms = MarketMicrostructure(load_existing_locks=False)
        assert ms._is_mainboard("000988")

    def test_gem_fails(self):
        """创业板 300xxx 不在主版"""
        ms = MarketMicrostructure(load_existing_locks=False)
        assert not ms._is_mainboard("300308")

    def test_star_fails(self):
        """科创板 688xxx 不在主版"""
        ms = MarketMicrostructure(load_existing_locks=False)
        assert not ms._is_mainboard("688256")
