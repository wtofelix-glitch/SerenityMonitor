"""趋势质量评分测试 — trend_quality.py"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pytest

from trend_quality import (
    compute_weighted_regression, compute_short_momentum, compute_trend_health,
    TrendQuality, HALF_LIFE,
)


class TestWeightedRegression:
    """加权线性回归"""

    def test_perfect_positive_trend(self):
        """完美上升趋势 → R² ≈ 1.0, 正斜率"""
        prices = np.array([1.0 + i * 0.1 for i in range(50)], dtype=np.float64)
        result = compute_weighted_regression(prices, half_life=10)

        assert result["r_squared"] > 0.9
        assert result["slope"] > 0.05
        assert result["annualized_slope"] > 0

    def test_perfect_negative_trend(self):
        """完美下降趋势 → R² ≈ 1.0, 负斜率"""
        prices = np.array([10.0 - i * 0.1 for i in range(50)], dtype=np.float64)
        result = compute_weighted_regression(prices, half_life=10)

        assert result["r_squared"] > 0.9
        assert result["slope"] < -0.05
        assert result["annualized_slope"] < 0

    def test_noisy_trend(self):
        """噪音价格 → R² < 1.0"""
        rng = np.random.RandomState(42)
        base = np.arange(50, dtype=np.float64) * 0.05
        noise = rng.normal(0, 0.3, 50)
        prices = 10.0 + base + noise
        result = compute_weighted_regression(prices, half_life=10)

        assert 0.0 < result["r_squared"] < 0.9
        assert result["n"] == 50

    def test_insufficient_data(self):
        """数据不足 → 返回错误标记"""
        prices = np.array([1.0, 2.0, 3.0])
        result = compute_weighted_regression(prices)

        assert "error" in result
        assert result["error"] == "insufficient_data"

    def test_exponential_weighting(self):
        """近期数据权重更大 → 近期趋势影响更强"""
        # 前 40 天下降, 最后 10 天急剧上升
        prices = np.array(
            [10.0 - i * 0.02 for i in range(40)] +
            [9.2 + i * 0.5 for i in range(10)],
            dtype=np.float64
        )
        result = compute_weighted_regression(prices, half_life=10)

        # 加权后应该反映上升趋势（近期权重更大）
        assert result["slope"] > 0
        assert result["r_squared"] > 0.3

    def test_r_squared_bounds(self):
        """R² 在 [0, 1] 范围内"""
        prices = np.random.RandomState(42).normal(10, 1, 30)
        result = compute_weighted_regression(prices)

        assert 0.0 <= result["r_squared"] <= 1.0

    def test_half_life_effect(self):
        """半衰期越短 → 近期数据权重越大"""
        trend = np.arange(40, dtype=np.float64) * 0.1 + 10.0
        # 最后 10 天转跌
        trend[30:] = trend[30] - np.arange(10) * 0.3

        r_short = compute_weighted_regression(trend, half_life=5)
        r_long = compute_weighted_regression(trend, half_life=30)

        # 短半衰期应该更早反映转跌
        assert r_short["slope"] < r_long["slope"]


class TestShortMomentum:
    """短期动量"""

    def test_above_ma(self):
        prices = np.array([10.0, 10.2, 10.1, 10.5, 10.8])
        result = compute_short_momentum(prices, window=4)
        assert result > 1.0  # 当前价 > 均线

    def test_below_ma(self):
        prices = np.array([10.8, 10.5, 10.2, 10.1, 10.0])
        result = compute_short_momentum(prices, window=4)
        assert result < 1.0

    def test_at_ma(self):
        prices = np.array([10.0, 10.0, 10.0, 10.0, 10.0])
        result = compute_short_momentum(prices, window=4)
        assert abs(result - 1.0) < 0.01


class TestTrendHealth:
    """趋势健康度"""

    def test_at_peak(self):
        prices = np.linspace(10.0, 20.0, 30)
        result = compute_trend_health(prices, window=20)
        assert abs(result - 1.0) < 0.01

    def test_drawdown(self):
        base = np.linspace(10.0, 20.0, 25)
        drop = np.linspace(20.0, 15.0, 10)
        prices = np.concatenate([base, drop])
        result = compute_trend_health(prices, window=20)
        assert result < 0.85


class TestTrendQualityIntegration:
    """TrendQuality 集成 — 用真实数据"""

    def test_real_stock_data(self):
        """从 price_history 加载真实数据"""
        tq = TrendQuality(lookback=60)
        result = tq.compute("002281")

        assert "code" in result
        assert "r_squared" in result
        assert "quality_score" in result
        assert "trend_label" in result
        assert 0.0 <= result["quality_score"] <= 100.0
        # 光迅科技可能有下降趋势，quality_score 可能为 0
        assert result["trend_label"] in (
            "极强趋势", "优质上升", "趋势形成中",
            "低质量波动", "下降趋势", "数据不足"
        )

    def test_insufficient_data_handles_gracefully(self):
        """数据不足时不崩溃"""
        tq = TrendQuality(lookback=5)
        result = tq.compute("002281")
        assert result["trend_label"] == "数据不足"
        assert result["quality_score"] == 0.0

    def test_batch_compute(self):
        """批量计算 20 只标的"""
        tq = TrendQuality()
        results = tq.compute_batch()
        assert len(results) <= 20
        assert len(results) > 0
        # 按 quality_score 排序
        for i in range(len(results) - 1):
            assert results[i]["quality_score"] >= results[i + 1]["quality_score"]

    def test_wufu_candidate_interface(self):
        """候选策略接口返回正确结构"""
        from trend_quality import wufu_momentum_return
        from scorer import _SCORE_WEIGHT_DEFAULTS

        result = wufu_momentum_return(
            ["002281", "600036", "600900"],
            "2026-07-10",
            dict(_SCORE_WEIGHT_DEFAULTS),
        )

        assert "daily_return" in result
        assert "quality_map" in result
        assert len(result["quality_map"]) == 3
        for qm in result["quality_map"].values():
            assert "r_squared" in qm
            assert "quality_score" in qm
            assert "trend_label" in qm
