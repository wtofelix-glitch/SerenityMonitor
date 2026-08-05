"""
停牌处理测试 — market_microstructure.py 停牌相关

覆盖:
  [x] 停牌不可买入
  [x] 停牌不可卖出
  [x] is_suspended() 状态查询
  [x] set_suspended() 标记/解除
  [x] 停牌复牌后恢复正常交易
  [x] 批量停牌/复牌
  [x] get_limit_status 返回 SUSPENDED
  [x] 停牌优先于涨跌停检查
"""

import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date, timedelta
from market_microstructure import (
    MarketMicrostructure, TradeabilityResult, LimitStatus,
)


class TestSuspendedCannotTrade:
    """停牌不可交易"""

    def test_cannot_buy_when_suspended(self):
        """停牌标的 → can_buy 返回不可买入"""
        ms = MarketMicrostructure()
        ms.set_suspended("002281", True)

        result = ms.can_buy("002281", date.today(), 200.0, 300)
        assert not result.executable
        assert "停牌" in result.block_reason
        assert result.limit_status == LimitStatus.SUSPENDED
        assert "not_suspended" in result.checks_failed

    def test_cannot_sell_when_suspended(self):
        """停牌标的 → can_sell 返回不可卖出"""
        ms = MarketMicrostructure()
        ms.set_suspended("600487", True)

        today = date.today()
        result = ms.can_sell(
            "600487", 300, today.ctime(),  # shares, buy_date
            30.0, 300,  # price, current_shares
        )

        # Reparse buy_date from today
        result = ms.can_sell("600487", 500, today, 30.0, 500)
        assert not result.executable
        assert "停牌" in result.block_reason
        assert result.limit_status == LimitStatus.SUSPENDED
        assert "not_suspended" in result.checks_failed

    def test_suspended_blocks_both_directions(self):
        """停牌同时阻塞买卖两个方向"""
        ms = MarketMicrostructure()
        today = date.today()

        ms.set_suspended("000988", True)

        buy = ms.can_buy("000988", today, 150.0, 200)
        sell = ms.can_sell("000988", 500, today, 150.0, 500)

        assert not buy.executable
        assert not sell.executable
        assert "停牌" in buy.block_reason
        assert "停牌" in sell.block_reason


class TestResumeTrading:
    """复牌后恢复正常交易"""

    def test_resume_allows_buying(self):
        """停牌解除 → can_buy 恢复正常"""
        ms = MarketMicrostructure()
        today = date.today()

        ms.set_suspended("002281", True)
        assert ms.is_suspended("002281")

        ms.set_suspended("002281", False)
        assert not ms.is_suspended("002281")

        result = ms.can_buy("002281", today, 200.0, 300)
        # 复牌后不应该因为停牌而被阻止（可能因为其他原因阻止，但不应该是停牌）
        assert result.limit_status != LimitStatus.SUSPENDED
        assert "not_suspended" not in result.checks_failed

    def test_resume_allows_selling(self):
        """停牌解除 → can_sell 恢复正常"""
        ms = MarketMicrostructure()
        today = date.today()

        ms.set_suspended("600487", True)
        ms.set_suspended("600487", False)

        result = ms.can_sell("600487", 300, today, 35.0, 300)
        assert result.limit_status != LimitStatus.SUSPENDED
        assert "not_suspended" not in result.checks_failed

    def test_resume_non_suspended_is_noop(self):
        """对未停牌标的解除停牌 → 无副作用"""
        ms = MarketMicrostructure()
        assert not ms.is_suspended("002281")
        ms.set_suspended("002281", False)
        assert not ms.is_suspended("002281")


class TestSuspendedStatusQueries:
    """停牌状态查询"""

    def test_is_suspended_positive(self):
        ms = MarketMicrostructure()
        ms.set_suspended("002281", True)
        assert ms.is_suspended("002281")

    def test_is_suspended_negative(self):
        ms = MarketMicrostructure()
        assert not ms.is_suspended("002281")

    def test_get_limit_status_returns_suspended(self):
        """get_limit_status 对停牌标的返回 SUSPENDED"""
        ms = MarketMicrostructure()
        ms.set_suspended("600487", True)

        status = ms.get_limit_status("600487")
        assert status == LimitStatus.SUSPENDED

    def test_get_limit_status_normal_after_resume(self):
        """复牌后 get_limit_status 不再返回 SUSPENDED"""
        ms = MarketMicrostructure()
        ms.set_suspended("600487", True)
        ms.set_suspended("600487", False)

        status = ms.get_limit_status("600487", change_pct=1.5)
        assert status == LimitStatus.NORMAL

    def test_new_instance_has_no_suspension(self):
        """新实例不含停牌状态（停牌是运行时状态）"""
        ms = MarketMicrostructure()
        assert not ms.is_suspended("002281")
        assert not ms.is_suspended("000988")


class TestBatchSuspension:
    """批量停牌/复牌"""

    def test_batch_suspend(self):
        ms = MarketMicrostructure()
        for code in ["002281", "000988", "600487"]:
            ms.set_suspended(code, True)

        assert all(ms.is_suspended(c) for c in ["002281", "000988", "600487"])
        assert not ms.is_suspended("600036")

    def test_batch_resume(self):
        ms = MarketMicrostructure()
        for code in ["002281", "000988"]:
            ms.set_suspended(code, True)

        for code in ["002281", "000988"]:
            ms.set_suspended(code, False)

        assert not any(ms.is_suspended(c) for c in ["002281", "000988"])


class TestSuspensionPriority:
    """停牌优先级高于涨跌停"""

    def test_suspended_overrides_limit_up(self):
        """停牌优先级高于涨停"""
        ms = MarketMicrostructure()
        ms.set_suspended("002281", True)

        # 即使价格变化显示涨停，get_limit_status 仍返回 SUSPENDED
        status = ms.get_limit_status("002281", change_pct=10.0)
        assert status == LimitStatus.SUSPENDED

    def test_suspended_overrides_limit_down(self):
        """停牌优先级高于跌停"""
        ms = MarketMicrostructure()
        ms.set_suspended("600487", True)

        status = ms.get_limit_status("600487", change_pct=-10.0)
        assert status == LimitStatus.SUSPENDED

    def test_suspended_blocks_even_otherwise_valid(self):
        """即使所有其他条件满足，停牌也阻止交易"""
        ms = MarketMicrostructure()
        today = date.today()

        # 正常条件下可买入
        ms.set_suspended("002281", False)
        buy_normal = ms.can_buy("002281", today, 200.0, 300)

        # 停牌后同一个标的不可买入
        ms.set_suspended("002281", True)
        buy_suspended = ms.can_buy("002281", today, 200.0, 300)

        assert not buy_suspended.executable
        assert "停牌" in buy_suspended.block_reason


class TestEdgeCases:
    """边界情况"""

    def test_suspend_code_not_in_universe(self):
        """停牌非候选池标的仍可标记（不验证代码）"""
        ms = MarketMicrostructure()
        ms.set_suspended("999999", True)
        assert ms.is_suspended("999999")

    def test_multiple_toggles(self):
        """多次切换停牌/复牌"""
        ms = MarketMicrostructure()
        for _ in range(5):
            ms.set_suspended("002281", True)
            assert ms.is_suspended("002281")
            ms.set_suspended("002281", False)
            assert not ms.is_suspended("002281")
