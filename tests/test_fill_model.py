"""
成交概率模型测试 — fill_model.py

覆盖:
  [x] 一字板涨停：买入概率极低 (~3%)
  [x] 普通涨停：买入概率 10-30%
  [x] 一字板跌停：卖出概率极低 (~3%)
  [x] 普通跌停：卖出概率 10-30%
  [x] 正常行情：成交概率 ~100%
  [x] 停牌：成交概率 0%
  [x] 连续涨跌停天数增加 → 概率递减
  [x] prob_fill 统一接口分发正确
  [x] update_consecutive_limits 状态追踪
"""

import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fill_model import (
    FillModel, FillProbability, HARD_LIMIT_FILL_PROB,
    NORMAL_LIMIT_FILL_PROB_MIN, NORMAL_LIMIT_FILL_PROB_MAX,
    NORMAL_FILL_PROB,
)


class TestHardLimitUp:
    """一字板涨停成交概率"""

    def test_hard_limit_up_very_low_prob(self):
        """一字板涨停买入概率约 3%"""
        model = FillModel()
        result = model.prob_fill_at_limit_up("002281", is_hard=True)

        assert isinstance(result, FillProbability)
        assert result.prob == pytest.approx(HARD_LIMIT_FILL_PROB, rel=0.1)
        assert result.can_fill is False
        assert result.expected_fill_ratio == 0.0
        assert result.limit_status == "limit_up_hard"
        assert "一字涨停" in result.reason

    def test_hard_limit_up_with_volume_does_not_increase(self):
        """一字板即使有量，概率也不增加（散户几乎排不到）"""
        model = FillModel()
        result = model.prob_fill_at_limit_up(
            "002281", is_hard=True, volume=1e8, turnover_amount=5e9)

        assert result.prob == pytest.approx(HARD_LIMIT_FILL_PROB, rel=0.1)
        assert result.can_fill is False


class TestNormalLimitUp:
    """普通涨停成交概率"""

    def test_normal_limit_up_min_prob(self):
        """普通涨停最低概率 10%"""
        model = FillModel()
        result = model.prob_fill_at_limit_up("000988", is_hard=False)

        assert result.prob >= 0.09  # ~10%
        assert result.limit_status == "limit_up"

    def test_normal_limit_up_with_turnover_increases_prob(self):
        """封板成交额越高，散户排到概率越大"""
        model = FillModel()
        result_low = model.prob_fill_at_limit_up(
            "002281", is_hard=False, turnover_amount=1e8)
        result_high = model.prob_fill_at_limit_up(
            "000988", is_hard=False, turnover_amount=5e8)

        # 更高成交额 → 更高概率（但在 30% 上限内）
        assert result_high.prob >= result_low.prob

    def test_normal_limit_up_capped_at_max(self):
        """普通涨停概率不超过 30%"""
        model = FillModel()
        result = model.prob_fill_at_limit_up(
            "002281", is_hard=False, turnover_amount=1e15)

        assert result.prob <= NORMAL_LIMIT_FILL_PROB_MAX + 0.01

    def test_normal_limit_up_prob_lower_bound(self):
        """普通涨停概率不低于 1%"""
        model = FillModel()
        result = model.prob_fill_at_limit_up("000988", is_hard=False)

        assert result.prob >= 0.01


class TestHardLimitDown:
    """一字板跌停成交概率"""

    def test_hard_limit_down_very_low_prob(self):
        """一字板跌停卖出概率约 3%"""
        model = FillModel()
        result = model.prob_fill_at_limit_down("002281", is_hard=True)

        assert isinstance(result, FillProbability)
        assert result.prob == pytest.approx(HARD_LIMIT_FILL_PROB, rel=0.1)
        assert result.can_fill is False
        assert result.expected_fill_ratio == 0.0
        assert result.limit_status == "limit_down_hard"
        assert "一字跌停" in result.reason


class TestNormalLimitDown:
    """普通跌停成交概率"""

    def test_normal_limit_down_min_prob(self):
        """普通跌停最低概率 10%"""
        model = FillModel()
        result = model.prob_fill_at_limit_down("600487", is_hard=False)

        assert result.prob >= 0.09

    def test_normal_limit_down_with_volume_increases_prob(self):
        """成交量越大，跌停卖出概率越高"""
        model = FillModel()
        result_low = model.prob_fill_at_limit_down(
            "600487", is_hard=False, volume=1e7)
        result_high = model.prob_fill_at_limit_down(
            "603083", is_hard=False, volume=1e9)

        assert result_high.prob >= result_low.prob


class TestNormalFill:
    """正常行情成交概率"""

    def test_normal_fill_near_certain(self):
        """正常行情成交概率约 99%"""
        model = FillModel()
        result = model.prob_fill_normal("002281")

        assert result.prob == pytest.approx(NORMAL_FILL_PROB, rel=0.05)
        assert result.can_fill is True
        assert result.expected_fill_ratio == 1.0
        assert result.limit_status == "normal"


class TestConsecutiveLimits:
    """连续涨跌停天数对成交概率的影响"""

    def test_consecutive_days_reduce_prob(self):
        """连续涨停天数越多，买入概率越低"""
        model = FillModel()

        # 第一次涨停 (consecutive=1, no reduction yet)
        r1 = model.prob_fill_at_limit_up("002281", is_hard=False, turnover_amount=5e8)

        # 标记两次 → 连续涨停 2 天 → reduction kicks in
        model.update_consecutive_limits("002281", is_limit=True)
        model.update_consecutive_limits("002281", is_limit=True)
        assert model.get_consecutive_limit_days("002281") == 2

        r2 = model.prob_fill_at_limit_up("002281", is_hard=False, turnover_amount=5e8)
        # 连续天数增加 → 概率更低 (0.7^(2-1) = 0.7x)
        assert r2.prob < r1.prob

    def test_not_limit_resets_counter(self):
        """非涨跌停日重置连续计数"""
        model = FillModel()
        model.update_consecutive_limits("002281", is_limit=True)
        model.update_consecutive_limits("002281", is_limit=True)
        assert model.get_consecutive_limit_days("002281") == 2

        model.update_consecutive_limits("002281", is_limit=False)
        assert model.get_consecutive_limit_days("002281") == 0

    def test_reset_clears_all(self):
        """reset() 清除所有连续计数"""
        model = FillModel()
        model.update_consecutive_limits("002281", is_limit=True)
        model.update_consecutive_limits("000988", is_limit=True)

        model.reset()
        assert model.get_consecutive_limit_days("002281") == 0
        assert model.get_consecutive_limit_days("000988") == 0


class TestProbFillUnified:
    """prob_fill 统一接口"""

    def test_unified_limit_up_hard(self):
        model = FillModel()
        r = model.prob_fill("002281", "limit_up_hard")
        assert r.limit_status == "limit_up_hard"
        assert not r.can_fill

    def test_unified_limit_up(self):
        model = FillModel()
        r = model.prob_fill("002281", "limit_up")
        assert r.limit_status == "limit_up"

    def test_unified_limit_down_hard(self):
        model = FillModel()
        r = model.prob_fill("002281", "limit_down_hard")
        assert r.limit_status == "limit_down_hard"
        assert not r.can_fill

    def test_unified_limit_down(self):
        model = FillModel()
        r = model.prob_fill("002281", "limit_down")
        assert r.limit_status == "limit_down"

    def test_unified_suspended(self):
        model = FillModel()
        r = model.prob_fill("002281", "suspended")
        assert r.prob == 0.0
        assert not r.can_fill
        assert "停牌" in r.reason

    def test_unified_normal(self):
        model = FillModel()
        r = model.prob_fill("002281", "normal")
        assert r.can_fill
        assert r.prob > 0.9

    def test_unified_unknown_falls_to_normal(self):
        """未知状态退化为正常交易"""
        model = FillModel()
        r = model.prob_fill("002281", "unknown_something")
        assert r.can_fill
