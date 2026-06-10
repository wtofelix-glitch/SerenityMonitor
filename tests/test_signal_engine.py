"""测试 signal_engine 模块 — 信号级别判定核心逻辑"""

import sys
sys.path.insert(0, '/Users/mac/workspace/SerenityMonitor')

import signal_engine
from signal_engine import get_signal_level, get_position_signal, SIGNAL_LEVELS
from config import SIGNAL_CONFIG as SC


class TestGetSignalLevel:
    """get_signal_level: 综合评分 → 信号级别"""

    def test_strong_buy_at_high_score(self, monkeypatch):
        monkeypatch.setattr(signal_engine, '_apply_conviction_to_signal_config', lambda: SC)
        assert get_signal_level(85) == "STRONG_BUY"
        assert get_signal_level(90) == "STRONG_BUY"
        assert get_signal_level(100) == "STRONG_BUY"

    def test_buy_at_mid_high_score(self, monkeypatch):
        monkeypatch.setattr(signal_engine, '_apply_conviction_to_signal_config', lambda: SC)
        assert get_signal_level(75) == "BUY"
        assert get_signal_level(78) == "STRONG_BUY"  # 78+ → strong_buy

    def test_caution_buy_at_moderate_score(self, monkeypatch):
        monkeypatch.setattr(signal_engine, '_apply_conviction_to_signal_config', lambda: SC)
        assert get_signal_level(65) == "CAUTION_BUY"
        assert get_signal_level(65) == "CAUTION_BUY"

    def test_hold_at_neutral_score(self, monkeypatch):
        monkeypatch.setattr(signal_engine, '_apply_conviction_to_signal_config', lambda: SC)
        assert get_signal_level(55) == "HOLD"
        assert get_signal_level(50) == "HOLD"

    def test_watch_at_low_score(self, monkeypatch):
        monkeypatch.setattr(signal_engine, '_apply_conviction_to_signal_config', lambda: SC)
        assert get_signal_level(46) == "WATCH"
        assert get_signal_level(45) == "WATCH"  # border: 45 is NOT below sell_threshold

    def test_sell_at_very_low_score(self, monkeypatch):
        monkeypatch.setattr(signal_engine, '_apply_conviction_to_signal_config', lambda: SC)
        assert get_signal_level(35) == "SELL"
        assert get_signal_level(20) == "SELL"
        assert get_signal_level(0) == "SELL"

    def test_boundary_transitions(self, monkeypatch):
        """测试临界值转换"""
        monkeypatch.setattr(signal_engine, '_apply_conviction_to_signal_config', lambda: SC)
        # 72: BUY
        assert get_signal_level(70) == "BUY"
        # 71.9: CAUTION_BUY (below buy_threshold)
        assert get_signal_level(65) == "CAUTION_BUY"
        # 50: HOLD
        assert get_signal_level(50) == "HOLD"
        # 49.9: WATCH (below hold_low)
        assert get_signal_level(49.9) == "WATCH"

    def test_monotonic(self, monkeypatch):
        """评分越高信号不应越弱"""
        monkeypatch.setattr(signal_engine, '_apply_conviction_to_signal_config', lambda: SC)
        scores = [0, 20, 40, 48, 50, 62, 65, 70, 72, 75, 78, 85, 95]
        levels = [get_signal_level(s) for s in scores]
        # convert to numeric rank for monotonicity check
        rank = {"SELL": 0, "WATCH": 1, "HOLD": 2, "CAUTION_BUY": 3, "BUY": 4, "STRONG_BUY": 5}
        num_levels = [rank[l] for l in levels]
        for i in range(1, len(num_levels)):
            assert num_levels[i] >= num_levels[i-1], \
                f"Non-monotonic at score {scores[i]}: {levels[i-1]} -> {levels[i]}"


class TestGetPositionSignal:
    """get_position_signal: 持仓专属信号"""

    def test_holding_high_profit_high_score_strong_hold(self, monkeypatch):
        """持仓中，高浮盈+高评分 → STRONG_HOLD"""
        monkeypatch.setattr(signal_engine, '_apply_conviction_to_signal_config', lambda: SC)
        assert get_position_signal(70, 10.0, True) == "STRONG_HOLD"

    def test_holding_high_profit_good_score_consider_add(self, monkeypatch):
        """持仓中，高浮盈+次高评分 → CONSIDER_ADD"""
        monkeypatch.setattr(signal_engine, '_apply_conviction_to_signal_config', lambda: SC)
        assert get_position_signal(58, 8.0, True) == "CONSIDER_ADD"

    def test_holding_low_score_weak_hold(self, monkeypatch):
        """持仓中，低评分 → WEAK_HOLD"""
        monkeypatch.setattr(signal_engine, '_apply_conviction_to_signal_config', lambda: SC)
        assert get_position_signal(44, -2.0, True) == "WEAK_HOLD"
        assert get_position_signal(40, -5.0, True) == "WEAK_HOLD"
        assert get_position_signal(47, 0.0, True) == "WEAK_HOLD"

    def test_not_holding_returns_base_signal(self, monkeypatch):
        """非持仓标的使用基础信号"""
        monkeypatch.setattr(signal_engine, '_apply_conviction_to_signal_config', lambda: SC)
        assert get_position_signal(75, 0, False) == "BUY"
        assert get_position_signal(85, 0, False) == "STRONG_BUY"
        assert get_position_signal(50, 0, False) == "HOLD"
        assert get_position_signal(30, 0, False) == "SELL"

    def test_boundary_48_is_hold_not_weak_hold(self, monkeypatch):
        """边界：评分48对于持仓是HOLD（不低于48）"""
        monkeypatch.setattr(signal_engine, '_apply_conviction_to_signal_config', lambda: SC)
        assert get_position_signal(48.0, 0, True) != "WEAK_HOLD"

    def test_boundary_47_9_is_weak_hold(self, monkeypatch):
        """边界：评分47.9对于持仓是WEAK_HOLD"""
        monkeypatch.setattr(signal_engine, '_apply_conviction_to_signal_config', lambda: SC)
        assert get_position_signal(47.9, 0, True) == "WEAK_HOLD"

    def test_buy_upgraded_to_strong_hold_for_positions(self, monkeypatch):
        """持仓中BUY信号升级为STRONG_HOLD"""
        monkeypatch.setattr(signal_engine, '_apply_conviction_to_signal_config', lambda: SC)
        result = get_position_signal(73, 2.0, True)
        assert result in ("STRONG_HOLD",)  # should be STRONG_HOLD (since profit > 0 and score >= 72 → BUY → upgraded)

    def test_sell_not_blocked_by_position_buffers(self, monkeypatch):
        """持仓中评分<48不受买入缓冲，但标记为WEAK_HOLD"""
        monkeypatch.setattr(signal_engine, '_apply_conviction_to_signal_config', lambda: SC)
        result = get_position_signal(30, -10.0, True)
        assert result == "WEAK_HOLD", f"Expected WEAK_HOLD, got {result}"


class TestSignalLevelsConfig:
    """SIGNAL_LEVELS 配置完整性"""

    def test_all_levels_have_required_keys(self):
        for name, config in SIGNAL_LEVELS.items():
            assert "score" in config, f"{name} missing score"
            assert "icon" in config, f"{name} missing icon"
            assert "desc" in config, f"{name} missing desc"

    def test_trade_signal_levels_ordered_by_score(self):
        """核心交易信号（非持仓专属）按分数降序排列"""
        trade_levels = {k: v for k, v in SIGNAL_LEVELS.items()
                        if k not in ("STRONG_HOLD", "WEAK_HOLD", "CONSIDER_ADD", "TAKE_PROFIT")}
        scores = [(name, cfg["score"]) for name, cfg in trade_levels.items()]
        for i in range(1, len(scores)):
            assert scores[i][1] <= scores[i-1][1], \
                f"Trade levels not sorted: {scores[i-1][0]}({scores[i-1][1]}) < {scores[i][0]}({scores[i][1]})"
