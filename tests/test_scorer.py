"""测试 scorer 模块 — 8维评分核心逻辑"""

import sys
sys.path.insert(0, '/Users/mac/workspace/SerenityMonitor')

from scorer import compute_zone_score, compute_momentum_score, compute_volume_score


class TestZoneScore:
    """compute_zone_score: 价格位置评分"""

    def _detail(self, zone_low: float = 100, zone_high: float = 150, target_sell: float = 200):
        return {"buy_zone_low": zone_low, "buy_zone_high": zone_high, "target_sell": target_sell}

    def test_price_at_target_returns_done(self):
        """价格已达目标价 → zone=done, 低分"""
        d = self._detail()
        score, label, cls = compute_zone_score(200, d)
        assert cls == "done", f"Expected done, got {cls}"
        assert score <= 30, f"Score at target should be low, got {score}"
        assert "已达" in label

    def test_price_above_target_returns_done(self):
        """价格超过目标价 → zone=done"""
        d = self._detail()
        score, label, cls = compute_zone_score(210, d)
        assert cls == "done"

    def test_price_in_buy_zone_high_score(self):
        """价格在买入区 → 高分"""
        d = self._detail(zone_low=100, zone_high=150, target_sell=200)
        score, label, cls = compute_zone_score(120, d)
        assert cls in ("", "buy_zone"), f"Expected empty/buy_zone, got {cls}"
        assert score >= 60, f"Score in zone should be high, got {score}"
        assert "买入" in label

    def test_price_below_zone_discount_score(self):
        """价格低于买入区 → 折扣分"""
        d = self._detail(zone_low=100, zone_high=150, target_sell=200)
        score, label, cls = compute_zone_score(85, d)
        assert cls == "below"
        assert score >= 85, f"Below-zone discount should be high, got {score}"
        assert "折扣" in label or "低于" in label

    def test_price_above_zone_score(self):
        """价格高于买入区但低于目标 → 中等分"""
        d = self._detail(zone_low=100, zone_high=150, target_sell=200)
        score, label, cls = compute_zone_score(170, d)
        assert cls == "above"
        assert "高于买入区" in label

    def test_zero_price(self):
        """价格为0 → 不崩溃"""
        d = self._detail()
        try:
            score, label, cls = compute_zone_score(0, d)
            assert isinstance(score, (int, float))
        except Exception as e:
            assert False, f"Zero price raised {e}"

    def test_no_detail(self):
        """无 detail 配置 → 不崩溃"""
        score, label, cls = compute_zone_score(100, {})
        assert isinstance(score, (int, float))


class TestMomentumScore:
    """compute_momentum_score: 动量评分"""

    def _detail(self, target_sell: float = 200):
        return {"target_sell": target_sell}

    def test_heavy_drop_gives_mid_score(self):
        """大跌(-4%) → 中等分"""
        d = self._detail(target_sell=300)
        score = compute_momentum_score(-4.0, 100, d)
        assert 20 <= score <= 80

    def test_slight_drop_gives_high_score(self):
        """小跌(-1%)且有上升空间 → 高分"""
        d = self._detail(target_sell=300)
        score = compute_momentum_score(-1.0, 100, d)
        assert score >= 70

    def test_high_rise_gives_low_score(self):
        """大涨(+6%)空间有限 → 低分"""
        d = self._detail(target_sell=110)  # only 10% room
        score = compute_momentum_score(6.0, 100, d)
        assert score <= 30

    def test_score_range(self):
        """所有结果在有效范围内"""
        changes = [-6.0, -4.0, -2.0, 0.0, 1.0, 3.0, 5.0, 8.0]
        for c in changes:
            score = compute_momentum_score(c, 100, {"target_sell": 200})
            assert 0 <= score <= 100, f"change_pct={c} gave score={score}, out of range"

    def test_no_target_room_means_lower_score(self):
        """目标空间有限时评分更低"""
        d_roomy = {"target_sell": 300}   # 200% room
        d_tight = {"target_sell": 105}   # 5% room
        score_roomy = compute_momentum_score(-1.0, 100, d_roomy)
        score_tight = compute_momentum_score(-1.0, 100, d_tight)
        assert score_roomy >= score_tight, \
            f"Roomy({score_roomy}) should be >= Tight({score_tight})"


class TestVolumeScore:
    """compute_volume_score: 成交量评分"""

    def _mock_avg_volume(self, code: str, days: int) -> float:
        # 下面测试通过 monkeypatch get_avg_volume 来测试
        return _MOCK_AVG_VOLUMES.get(code, 1000000)

    def test_normal_volume_scores_high(self, monkeypatch):
        """成交量在0.8x~1.5x均值 → 高分"""
        monkeypatch.setattr('scorer.get_avg_volume', lambda c, d: 1000000)
        score = compute_volume_score("002281", 1200000)  # 1.2x
        assert score >= 70

    def test_low_volume_scores_medium(self, monkeypatch):
        """成交量偏小(0.6x) → 中等分"""
        monkeypatch.setattr('scorer.get_avg_volume', lambda c, d: 1000000)
        score = compute_volume_score("002281", 600000)  # 0.6x
        assert 30 <= score <= 70

    def test_extreme_volume_scores_low(self, monkeypatch):
        """成交量异常大(4x+) → 低分"""
        monkeypatch.setattr('scorer.get_avg_volume', lambda c, d: 1000000)
        score = compute_volume_score("002281", 5000000)  # 5x
        assert score <= 30

    def test_no_avg_volume_returns_default(self, monkeypatch):
        """没有均量数据 → 默认60分"""
        monkeypatch.setattr('scorer.get_avg_volume', lambda c, d: 0)
        score = compute_volume_score("002281", 1000000)
        assert score == 60

    def test_zero_volume(self):
        """成交量为0 → 不崩溃"""
        try:
            compute_volume_score("002281", 0)
        except Exception as e:
            assert False, f"Zero volume raised {e}"


# 全局 mock 数据
_MOCK_AVG_VOLUMES = {
    "002281": 1500000,
    "000988": 2000000,
    "600460": 800000,
}
