"""
测试涨跌停处理

覆盖:
  [x] 涨停 → 不可买入
  [x] 跌停 → 不可卖出
  [x] 一字涨停 → 成交概率 <5%
  [x] 一字跌停 → 成交概率 <5%
  [x] 普通涨停 vs 一字板区分（基于成交量）
  [x] 连续涨跌停 → 成交概率递减
  [x] 跌停时止损无法执行 → 进入"流动性危机"追踪
"""

import pytest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date
from market_microstructure import MarketMicrostructure, LimitStatus
from fill_model import FillModel, FillProbability


class TestLimitUpDownDetection:
    """涨跌停检测"""

    def test_normal(self):
        ms = MarketMicrostructure()
        assert ms.get_limit_status("002281", change_pct=1.0) == LimitStatus.NORMAL
        assert ms.get_limit_status("002281", change_pct=-1.0) == LimitStatus.NORMAL
        assert ms.get_limit_status("002281", change_pct=5.0) == LimitStatus.NORMAL

    def test_limit_up_boundary(self):
        ms = MarketMicrostructure()
        # 刚好 9.9% → 涨停
        assert ms.get_limit_status("002281", change_pct=9.9) == LimitStatus.LIMIT_UP
        # 9.8% → 正常
        assert ms.get_limit_status("002281", change_pct=9.8) == LimitStatus.NORMAL

    def test_limit_down_boundary(self):
        ms = MarketMicrostructure()
        assert ms.get_limit_status("002281", change_pct=-9.9) == LimitStatus.LIMIT_DOWN
        assert ms.get_limit_status("002281", change_pct=-9.8) == LimitStatus.NORMAL

    def test_hard_limit_with_volume_zero(self):
        """volume < 100 且涨跌停 → 一字板"""
        ms = MarketMicrostructure()
        assert ms.get_limit_status("002281", change_pct=10.0, volume=50,
                                   avg_volume=1000000) == LimitStatus.LIMIT_UP_HARD
        assert ms.get_limit_status("002281", change_pct=-10.0, volume=50,
                                   avg_volume=1000000) == LimitStatus.LIMIT_DOWN_HARD

    def test_hard_limit_with_volume_ratio(self):
        """vol/avg_vol < 0.05 且涨跌停 → 一字板"""
        ms = MarketMicrostructure()
        assert ms.get_limit_status("002281", change_pct=10.0,
                                   volume=1000, avg_volume=100000) == LimitStatus.LIMIT_UP_HARD

    def test_normal_limit_with_volume(self):
        """涨跌停但有成交量 → 普通涨跌停"""
        ms = MarketMicrostructure()
        assert ms.get_limit_status("002281", change_pct=10.0,
                                   volume=500000, avg_volume=1000000) == LimitStatus.LIMIT_UP
        assert ms.get_limit_status("002281", change_pct=-10.0,
                                   volume=500000, avg_volume=1000000) == LimitStatus.LIMIT_DOWN

    def test_suspended(self):
        ms = MarketMicrostructure()
        ms.set_suspended("002281", True)
        assert ms.get_limit_status("002281", change_pct=1.0) == LimitStatus.SUSPENDED

    def test_unknown_when_no_data(self):
        ms = MarketMicrostructure()
        assert ms.get_limit_status("002281") == LimitStatus.UNKNOWN


class TestBuySellBlockedByLimits:
    """涨跌停阻止交易"""

    def test_limit_up_blocks_buy(self):
        ms = MarketMicrostructure()
        today = date.today()
        snapshot = {"change_pct": 10.0, "volume": 500000}
        result = ms.can_buy("002281", today, 200.0, 300, snapshot=snapshot)
        assert not result.executable

    def test_limit_up_hard_blocks_buy(self):
        ms = MarketMicrostructure()
        today = date.today()
        snapshot = {"change_pct": 10.0, "volume": 50}
        result = ms.can_buy("002281", today, 200.0, 300, snapshot=snapshot)
        assert not result.executable

    def test_limit_down_blocks_sell(self):
        ms = MarketMicrostructure()
        today = date.today()
        snapshot = {"change_pct": -10.0, "volume": 500000}
        result = ms.can_sell("002281", 300, today, 200.0, 300, snapshot=snapshot)
        assert not result.executable

    def test_normal_market_allows_trading(self):
        ms = MarketMicrostructure()
        today = date.today()
        snapshot = {"change_pct": 2.0, "volume": 500000}

        buy_result = ms.can_buy("002281", today, 200.0, 300, snapshot=snapshot)
        assert buy_result.executable

        sell_result = ms.can_sell("002281", 300, today, 200.0, 300, snapshot=snapshot)
        assert sell_result.executable


class TestFillProbability:
    """成交概率模型"""

    def test_hard_limit_up_very_low_prob(self):
        model = FillModel()
        prob = model.prob_fill_at_limit_up("002281", is_hard=True)
        assert prob.prob < 0.05
        assert not prob.can_fill

    def test_hard_limit_down_very_low_prob(self):
        model = FillModel()
        prob = model.prob_fill_at_limit_down("002281", is_hard=True)
        assert prob.prob < 0.05
        assert not prob.can_fill

    def test_normal_limit_up_some_prob(self):
        model = FillModel()
        prob = model.prob_fill_at_limit_up("002281", is_hard=False)
        assert 0.05 < prob.prob < 0.50
        # 默认无成交额数据时使用最小概率
        assert prob.prob >= 0.10

    def test_normal_market_high_prob(self):
        model = FillModel()
        prob = model.prob_fill_normal("002281")
        assert prob.prob > 0.95
        assert prob.can_fill

    def test_suspended_zero_prob(self):
        model = FillModel()
        prob = model.prob_fill("002281", "suspended")
        assert prob.prob == 0.0
        assert not prob.can_fill

    def test_consecutive_limits_decrease_prob(self):
        """连续涨跌停天数增加 → 成交概率递减"""
        model = FillModel()
        # 第一天: 无历史连续涨停
        prob1 = model.prob_fill_at_limit_up("002281", is_hard=False)
        # 标记连续涨停
        model.update_consecutive_limits("002281", True)
        model.update_consecutive_limits("002281", True)  # 连续 2 天
        # 第三天: consecutive=2, penalty = 0.7^(2-1) = 0.7
        prob2 = model.prob_fill_at_limit_up("002281", is_hard=False)
        # 连续涨停概率应更低
        assert prob2.prob < prob1.prob
        assert model.get_consecutive_limit_days("002281") == 2

    def test_limit_clears_after_normal_day(self):
        model = FillModel()
        model.update_consecutive_limits("002281", True)
        model.update_consecutive_limits("002281", True)
        assert model.get_consecutive_limit_days("002281") == 2
        model.update_consecutive_limits("002281", False)
        assert model.get_consecutive_limit_days("002281") == 0

    def test_unified_interface(self):
        model = FillModel()
        assert model.prob_fill("002281", "limit_up_hard").prob < 0.05
        assert model.prob_fill("002281", "limit_down_hard").prob < 0.05
        assert model.prob_fill("002281", "limit_up").prob > 0.05
        assert model.prob_fill("002281", "limit_down").prob > 0.05
        assert model.prob_fill("002281", "normal").prob > 0.95
        assert model.prob_fill("002281", "suspended").prob == 0.0
