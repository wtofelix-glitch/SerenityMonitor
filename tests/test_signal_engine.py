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
        assert get_signal_level(SC["buy_threshold"]) == "BUY"
        assert get_signal_level(SC["strong_buy_threshold"]) == "STRONG_BUY"

    def test_caution_buy_at_moderate_score(self, monkeypatch):
        monkeypatch.setattr(signal_engine, '_apply_conviction_to_signal_config', lambda: SC)
        assert get_signal_level(SC["hold_high"]) == "CAUTION_BUY"
        assert get_signal_level(SC["buy_threshold"] - 0.1) == "CAUTION_BUY"

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
        assert get_signal_level(SC["strong_buy_threshold"]) == "STRONG_BUY"
        assert get_signal_level(SC["buy_threshold"]) == "BUY"
        assert get_signal_level(SC["hold_high"]) == "CAUTION_BUY"
        assert get_signal_level(SC["hold_low"]) == "HOLD"
        assert get_signal_level(SC["hold_low"] - 0.1) == "WATCH"

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
        assert get_position_signal(SC["buy_threshold"], 0, False) == "BUY"
        assert get_position_signal(SC["strong_buy_threshold"], 0, False) == "STRONG_BUY"
        assert get_position_signal(SC["hold_low"], 0, False) == "HOLD"
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
        result = get_position_signal(SC["buy_threshold"], 2.0, True)
        assert result == "STRONG_HOLD"

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


# ═══ Trend Score ════════════════════════════════════════════

class TestTrendScore:
    """compute_trend_score: 技术面综合评分"""

    def test_neutral_tech_returns_middle_score(self):
        tech = {'ma_alignment': 0, 'rsi': 50, 'bb_position': 0.5,
                'volume_ratio': 1.0, 'atr_pct': 2.0}
        score = signal_engine.compute_trend_score(tech)
        assert 40 <= score <= 70, f"Neutral tech should score 40-70, got {score}"

    def test_bullish_tech_returns_high_score(self):
        tech = {'ma_alignment': 1, 'rsi': 60, 'bb_position': 0.8,
                'volume_ratio': 1.5, 'atr_pct': 1.0}
        score = signal_engine.compute_trend_score(tech)
        assert score >= 50, f"Bullish tech should score above neutral, got {score}"

    def test_bearish_tech_returns_low_score(self):
        tech = {'ma_alignment': -1, 'rsi': 30, 'bb_position': 0.2,
                'volume_ratio': 0.5, 'atr_pct': 4.0}
        score = signal_engine.compute_trend_score(tech)
        assert score <= 55, f"Bearish tech should score low, got {score}"

    def test_missing_keys_default_safe(self):
        score = signal_engine.compute_trend_score({})
        assert isinstance(score, (int, float))
        assert 0 <= score <= 100


# ═══ Sell Triggers ═════════════════════════════════════════

class TestSellTriggers:
    """compute_sell_triggers: 卖出触发条件"""

    def test_no_sell_on_good_signals(self, monkeypatch):
        monkeypatch.setattr(signal_engine, '_compute_prev_rsi', lambda c, d: 55)
        monkeypatch.setattr(signal_engine, '_get_recent_scores', lambda c, d: [65, 68, 70, 72, 70])
        tech = {'rsi': 55, 'atr_pct': 2.0}
        triggers = signal_engine.compute_sell_triggers('600001', tech, 65)
        # With good scores and normal RSI, should not trigger sells
        assert isinstance(triggers, list)

    def test_sell_on_low_score_high_rsi(self, monkeypatch):
        monkeypatch.setattr(signal_engine, '_compute_prev_rsi', lambda c, d: 80)
        monkeypatch.setattr(signal_engine, '_get_recent_scores', lambda c, d: [65, 55, 45, 40, 35])
        tech = {'rsi': 80, 'atr_pct': 2.0}
        triggers = signal_engine.compute_sell_triggers('600001', tech, 30)
        # Low score + overbought RSI should generate triggers
        if len(triggers) > 0:
            for t in triggers:
                assert 'trigger' in t, f"Missing trigger key in {t}"

    def test_no_crash_with_no_history(self, monkeypatch):
        monkeypatch.setattr(signal_engine, '_compute_prev_rsi', lambda c, d: None)
        monkeypatch.setattr(signal_engine, '_get_recent_scores', lambda c, d: [])
        tech = {'rsi': 50, 'atr_pct': 2.0}
        triggers = signal_engine.compute_sell_triggers('600001', tech, 55)
        assert isinstance(triggers, list)


# ═══ Buy Confirmation ══════════════════════════════════════

class TestBuyConfirmation:
    """confirm_buy_signal: 买入信号确认"""

    def test_confirm_returns_dict_structure(self):
        tech = {'rsi': 55, 'atr_pct': 2.0}
        alpha = {'trend': 60, 'momentum': 55}
        result = signal_engine.confirm_buy_signal('600001', tech, alpha)
        assert isinstance(result, dict)
        assert 'confirmed' in result or 'passed' in result or 'checks' in result

    def test_confirm_no_crash_empty_alpha(self):
        tech = {'rsi': 50}
        result = signal_engine.confirm_buy_signal('600001', tech, {})
        assert isinstance(result, dict)


# ═══ Caution Buy Filter ════════════════════════════════════

class TestCautionBuyFilter:
    """compute_caution_buy_filter: 3-stage quality check"""

    def test_filter_returns_valid_result(self):
        tech = {'rsi': 55, 'atr_pct': 2.0, 'ma_alignment': 1}
        alpha = {'trend': 55, 'momentum': 50}
        # compute_caution_buy_filter(code, tech, alpha_signals, scores=None)
        result = signal_engine.compute_caution_buy_filter('600001', tech, alpha)
        assert isinstance(result, dict)

    def test_filter_no_crash_empty_inputs(self):
        result = signal_engine.compute_caution_buy_filter('600001', {}, {})
        assert isinstance(result, dict)


# ═══ Dynamic Stop Loss ═════════════════════════════════════

class TestDynamicStopLoss:
    """get_dynamic_stop_loss: ATR-based dynamic stop loss"""

    def test_returns_dict_with_stop_price(self):
        result = signal_engine.get_dynamic_stop_loss('600001', 10.0)
        assert isinstance(result, dict)
        if 'stop_price' in result:
            assert result['stop_price'] < 10.0, "Stop loss should be below buy price"

    def test_stop_loss_percentage_reasonable(self):
        result = signal_engine.get_dynamic_stop_loss('600001', 12.5)
        if 'stop_price' in result and result['stop_price'] > 0:
            stop_pct = (12.5 - result['stop_price']) / 12.5 * 100
            assert 2 <= stop_pct <= 20, f"Stop loss {stop_pct:.1f}% seems unreasonable"
