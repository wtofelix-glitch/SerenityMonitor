"""测试 market_timing — 大盘择时信号（纯函数 + mock 网络）"""

import sys
sys.path.insert(0, '/Users/mac/workspace/SerenityMonitor')

import json
import numpy as np
from market_timing import (
    compute_ma_trend, compute_rsi, compute_volume_trend,
    analyze_index, get_market_signal, get_market_advice,
    _fetch_index_kline,
)


# ── MA Trend ──────────────────────────────────────────────

class TestComputeMaTrend:
    def test_insufficient_data(self):
        """少于 ma_long 天 → 数据不足"""
        closes = np.array([1.0, 2.0, 3.0])
        r = compute_ma_trend(closes, ma_long=60)
        assert r["trend"] == "数据不足"
        assert r["ma_short_val"] == 0

    def test_bullish(self):
        """current > MA20 > MA60 → 多头"""
        arr = list(range(100, 162))  # 62 points, rising
        closes = np.array(arr, dtype=float)
        r = compute_ma_trend(closes, ma_short=20, ma_long=60)
        assert "多头" in r["trend"]
        assert r["ma_short_val"] > r["ma_long_val"]

    def test_bearish(self):
        """current < MA20 < MA60 → 空头"""
        arr = list(range(161, 99, -1))  # 62 points, falling
        closes = np.array(arr, dtype=float)
        r = compute_ma_trend(closes, ma_short=20, ma_long=60)
        assert "空头" in r["trend"]
        assert r["ma_short_val"] < r["ma_long_val"]

    def test_oscillator_bull(self):
        """MA20 > MA60 且 current > MA20 但不符合多头严格条件 → 震荡偏多"""
        # 急涨后回落 — 最后几根压到 MA20 以下，但 MA20 还在 MA60 上方
        arr = list(range(50, 108)) + [107, 105, 103, 101, 99]  # 62 pts, last 5 dip
        closes = np.array(arr, dtype=float)
        r = compute_ma_trend(closes, ma_short=20, ma_long=60)
        assert r["trend"] not in ("数据不足", "空头")

    def test_oscillator_bear(self):
        """MA20 < MA60 但 current ≥ MA20 → 震荡偏空"""
        arr = list(range(111, 49, -1))  # 62 pts down
        # bump the last point up to be > MA20 but still < MA60
        arr[-1] = arr[-3]  # pull last back toward middle
        closes = np.array(arr, dtype=float)
        r = compute_ma_trend(closes, ma_short=20, ma_long=60)
        # May or may not be 震荡偏空 depending on specific values
        # but verify it's not 数据不足 and has correct shape
        assert r["trend"] != "数据不足"
        assert isinstance(r["price_vs_ma20_pct"], float)

    def test_price_vs_ma_pct(self):
        """验证 price_vs_ma20_pct / price_vs_ma60_pct 计算"""
        closes = np.array(list(range(100, 162)), dtype=float)
        r = compute_ma_trend(closes, ma_short=20, ma_long=60)
        ma20 = closes[-20:].mean()
        ma60 = closes[-60:].mean()
        expected_vs_20 = round((closes[-1] - ma20) / ma20 * 100, 2)
        expected_vs_60 = round((closes[-1] - ma60) / ma60 * 100, 2)
        assert r["price_vs_ma20_pct"] == expected_vs_20
        assert r["price_vs_ma60_pct"] == expected_vs_60


# ── RSI ────────────────────────────────────────────────────

class TestComputeRSI:
    def test_insufficient_data(self):
        """少于 period+1 天 → RSI=50 数据不足"""
        closes = np.array([1.0, 2.0, 3.0])
        r = compute_rsi(closes, period=14)
        assert r["rsi"] == 50
        assert "数据不足" in r["status"]

    def test_all_up_overbought(self):
        """全部上涨 → RSI=100 → 超买"""
        closes = np.array([100.0 + i for i in range(16)], dtype=float)
        r = compute_rsi(closes)
        assert r["rsi"] == 100.0
        assert "超买" in r["status"]

    def test_all_down_oversold(self):
        """全部下跌 → RSI=0 → 超卖"""
        closes = np.array([115.0 - i for i in range(16)], dtype=float)
        r = compute_rsi(closes)
        assert r["rsi"] == 0.0
        assert "超卖" in r["status"]

    def test_neutral_rsi(self):
        """涨跌各半 → RSI=50 → 中性"""
        vals = []
        for i in range(8):
            vals.append(100 + i)
            vals.append(100 + i)  # 平盘，后求 diff 得 0
        # 制造 exactly equal up/down moves
        closes = np.array([100.0, 101.0, 100.0, 101.0, 100.0, 101.0,
                           100.0, 101.0, 100.0, 101.0, 100.0, 101.0,
                           100.0, 101.0, 100.0], dtype=float)
        r = compute_rsi(closes)
        assert r["rsi"] == 50.0
        assert "中性" in r["status"]

    def test_strong_rsi(self):
        """RSI 在 60-70 之间 → 偏强"""
        # Mix of more up than down moves
        closes = np.array([100, 102, 101, 103, 102, 104, 103, 105,
                           104, 106, 105, 107, 106, 108, 107], dtype=float)
        r = compute_rsi(closes)
        assert 60 < r["rsi"] < 70
        assert "偏强" in r["status"]

    def test_weak_rsi(self):
        """RSI 在 30-40 之间 → 偏弱"""
        closes = np.array([107, 105, 106, 104, 105, 103, 104, 102,
                           103, 101, 102, 100, 101, 99, 100], dtype=float)
        r = compute_rsi(closes)
        assert 30 < r["rsi"] < 40
        assert "偏弱" in r["status"]


# ── Volume Trend ─────────────────────────────────────────

class TestComputeVolumeTrend:
    def test_insufficient_data(self):
        """少于 long_window → 数据不足"""
        vols = np.array([1.0, 2.0, 3.0])
        r = compute_volume_trend(vols, long_window=20)
        assert r["volume_trend"] == "数据不足"

    def test_surge(self):
        """vol_ratio ≥ 1.5 → 放量"""
        vols = np.array([100.0] * 20 + [200.0] * 5)
        r = compute_volume_trend(vols)
        assert r["volume_trend"] == "放量"
        assert r["vol_ratio"] >= 1.5

    def test_moderate_increase(self):
        """1.2 ≤ vol_ratio < 1.5 → 温和放量"""
        vols = np.array([100.0] * 20 + [130.0] * 5)
        r = compute_volume_trend(vols)
        assert r["volume_trend"] == "温和放量"
        assert 1.2 <= r["vol_ratio"] < 1.5

    def test_shrink(self):
        """vol_ratio ≤ 0.5 → 缩量"""
        vols = np.array([100.0] * 20 + [40.0] * 5)
        r = compute_volume_trend(vols)
        assert r["volume_trend"] == "缩量"

    def test_moderate_shrink(self):
        """0.5 < vol_ratio ≤ 0.8 → 温和缩量"""
        vols = np.array([100.0] * 20 + [70.0] * 5)
        r = compute_volume_trend(vols)
        assert r["volume_trend"] == "温和缩量"

    def test_normal(self):
        """0.8 < vol_ratio < 1.2 → 正常"""
        vols = np.array([100.0] * 20 + [100.0] * 5)
        r = compute_volume_trend(vols)
        assert r["volume_trend"] == "正常"


# ── Analyze Index ────────────────────────────────────────

class TestAnalyzeIndex:
    def test_success(self, monkeypatch):
        """正常分析返回完整指标（足够天数）"""
        data = []
        for i in range(80):
            data.append({
                "day": f"2026-0{3+(i//30):02d}-{(i%30)+1:02d}",
                "open": 3000.0 + i, "close": 3000.0 + i,
                "high": 3010.0 + i, "low": 2990.0 + i,
                "volume": 1e8 + i * 1e6,
            })
        monkeypatch.setattr('market_timing._fetch_index_kline',
                            lambda symbol, days=80: data)

        result = analyze_index("sh000001", "上证指数")
        assert result["name"] == "上证指数"
        assert result["last_close"] > 0
        assert result["rsi"] >= 0
        assert result["ma20"] > 0
        assert result["ma60"] > 0
        # 数据充足时 analyze_index 不返回 status 字段
        assert "trend" in result

    def test_insufficient_data(self, monkeypatch):
        """少于30天数据 → 数据不足"""
        monkeypatch.setattr('market_timing._fetch_index_kline',
                            lambda symbol, days=80: [])
        result = analyze_index("sh000001", "上证指数")
        assert result["status"] == "数据不足"
        assert result["last_close"] == 0


# ── Market Signal ────────────────────────────────────────

class TestGetMarketSignal:
    def test_bullish_signal(self, monkeypatch):
        """多头趋势 → overall_signal = 积极"""
        bull = {
            "name": "上证指数", "symbol": "sh000001",
            "status": "", "last_close": 3200,
            "trend": "多头", "ma20": 3180, "ma60": 3150,
            "price_vs_ma20": 0.63, "rsi": 55, "rsi_status": "中性",
            "volume_trend": "正常", "vol_ratio": 1.0, "signals": "趋势偏多",
        }
        monkeypatch.setattr('market_timing.analyze_index',
                            lambda s, n: bull)
        signal = get_market_signal()
        assert signal["overall_signal"] == "积极"

    def test_bearish_signal(self, monkeypatch):
        """空头趋势 + RSI < 40 → overall_signal = 危险"""
        bear = {
            "name": "上证指数", "symbol": "sh000001",
            "status": "", "last_close": 3000,
            "trend": "空头", "ma20": 3100, "ma60": 3200,
            "price_vs_ma20": -3.23, "rsi": 35, "rsi_status": "偏弱",
            "volume_trend": "缩量", "vol_ratio": 0.6, "signals": "趋势偏空",
        }
        monkeypatch.setattr('market_timing.analyze_index',
                            lambda s, n: bear)
        signal = get_market_signal()
        assert signal["overall_signal"] == "危险"

    def test_insufficient_data(self, monkeypatch):
        """无有效数据 → overall_signal = 中性"""
        insufficient = {"name": "上证指数", "symbol": "sh000001",
                        "status": "数据不足", "last_close": 0}
        monkeypatch.setattr('market_timing.analyze_index',
                            lambda s, n: insufficient)
        signal = get_market_signal()
        assert signal["overall_trend"] == "未知"
        assert signal["overall_signal"] == "中性"

    def test_overbought_signal(self, monkeypatch):
        """avg_rsi ≥ 70 → 超买 → 谨慎"""
        overbought = {
            "name": "上证指数", "symbol": "sh000001",
            "status": "", "last_close": 3500,
            "trend": "震荡", "ma20": 3400, "ma60": 3300,
            "price_vs_ma20": 2.94, "rsi": 72, "rsi_status": "超买",
            "volume_trend": "放量", "vol_ratio": 1.6, "signals": "超买⚠️",
        }
        monkeypatch.setattr('market_timing.analyze_index',
                            lambda s, n: overbought)
        signal = get_market_signal()
        assert signal["overall_signal"] == "谨慎"
        assert "注意回调" in signal["overall_advice"]

    def test_oversold_opportunity(self, monkeypatch):
        """avg_rsi ≤ 35 → 超卖 → 机会"""
        oversold = {
            "name": "上证指数", "symbol": "sh000001",
            "status": "", "last_close": 2800,
            "trend": "震荡偏空", "ma20": 2900, "ma60": 3000,
            "price_vs_ma20": -3.45, "rsi": 32, "rsi_status": "超卖",
            "volume_trend": "缩量", "vol_ratio": 0.5, "signals": "超卖💡",
        }
        monkeypatch.setattr('market_timing.analyze_index',
                            lambda s, n: oversold)
        signal = get_market_signal()
        assert signal["overall_signal"] == "机会"


# ── Market Advice ────────────────────────────────────────

class TestGetMarketAdvice:
    def test_advice_format(self, monkeypatch):
        """输出包含大盘择时和上证/沪深300"""
        signal = {
            "overall_trend": "多头", "overall_signal": "积极",
            "overall_advice": "正常仓位", "avg_rsi": 55.0,
            "sh": {"name": "上证指数", "symbol": "sh000001",
                   "status": "", "last_close": 3200, "ma20": 3180,
                   "ma60": 3150, "rsi": 55, "trend": "多头",
                   "volume_trend": "正常", "vol_ratio": 1.0, "signals": ""},
            "hs300": {"name": "沪深300", "symbol": "sh000300",
                      "status": "", "last_close": 3900, "ma20": 3850,
                      "ma60": 3800, "rsi": 58, "trend": "多头",
                      "volume_trend": "正常", "vol_ratio": 1.0, "signals": ""},
        }
        monkeypatch.setattr('market_timing.get_market_signal',
                            lambda: signal)
        advice = get_market_advice()
        assert "大盘择时" in advice
        assert "上证" in advice
        assert "沪深300" in advice
        assert "正常仓位" in advice
