"""测试 factor_engine 模块 — 因子计算核心逻辑"""

import os, sys, pytest, numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import factor_engine


# ── Mock OHLCV data ──────────────────────────────────────────
def make_ohlcv(n=60, base=10.0, trend=0.005, noise=0.01):
    np.random.seed(42)
    rows = []
    for i in range(n):
        c = base * (1 + trend * i + noise * np.random.randn())
        rows.append({'date': f'2026-06-{i+1:02d}',
                     'open': c * (1 - noise * 0.3), 'high': c * (1 + noise * 0.5),
                     'low': c * (1 - noise * 0.5), 'close': c, 'volume': 1000000 + i * 50000})
    return rows[::-1]  # newest first


def make_large_ohlcv(n=120):
    """More data for TS factors (needs > max_window + 1 rows)"""
    np.random.seed(43)
    rows = []
    for i in range(n):
        c = 10.0 * (1 + 0.005 * i + 0.01 * np.random.randn())
        rows.append({'date': f'2026-06-{i+1:02d}',
                     'open': c * 0.997, 'high': c * 1.005,
                     'low': c * 0.995, 'close': c, 'volume': 1000000 + i * 50000})
    return rows[::-1]


@pytest.fixture
def mock_db(monkeypatch):
    """Mock db.get_price_history to return synthetic OHLCV"""
    data = make_ohlcv()
    monkeypatch.setattr(factor_engine, 'get_price_history', lambda code, days=120: data)
    return data


class TestMACD:
    def test_returns_dict(self, mock_db):
        result = factor_engine.compute_macd('600001', use_db=False)
        assert isinstance(result, dict)
        # MACD returns keys like 'macd_line', 'macd_signal', 'macd_histogram', 'macd_cross'
        assert any(k.startswith('macd') for k in result.keys())

    def test_with_db(self, mock_db):
        result = factor_engine.compute_macd('600001', use_db=True)
        assert isinstance(result, dict)


class TestOBV:
    def test_returns_dict(self, mock_db):
        result = factor_engine.compute_obv('600001', use_db=False)
        assert isinstance(result, dict)

    def test_with_db(self, mock_db):
        result = factor_engine.compute_obv('600001', use_db=True)
        assert isinstance(result, dict)


class TestMFI:
    def test_returns_dict(self, mock_db):
        result = factor_engine.compute_mfi('600001', use_db=False)
        assert isinstance(result, dict)

    def test_value_in_range(self, mock_db):
        result = factor_engine.compute_mfi('600001', use_db=False)
        for k, v in result.items():
            if isinstance(v, (int, float)) and 'signal' in k.lower():
                assert 0 <= v <= 100, f"MFI value out of range: {k}={v}"


class TestCCI:
    def test_returns_dict(self, mock_db):
        result = factor_engine.compute_cci('600001', use_db=False)
        assert isinstance(result, dict)


class TestWQAlphas:
    def test_alpha1(self, mock_db):
        assert isinstance(factor_engine.compute_wq_alpha1('600001', use_db=False), dict)
    def test_alpha3(self, mock_db):
        assert isinstance(factor_engine.compute_wq_alpha3('600001', use_db=False), dict)
    def test_alpha5(self, mock_db):
        assert isinstance(factor_engine.compute_wq_alpha5('600001', use_db=False), dict)
    def test_alpha15(self, mock_db):
        assert isinstance(factor_engine.compute_wq_alpha15('600001', use_db=False), dict)
    def test_alpha19(self, mock_db):
        assert isinstance(factor_engine.compute_wq_alpha19('600001', use_db=False), dict)


class TestCandleFactors:
    def test_no_crash(self):
        result = factor_engine.compute_candle_factors(10.0, 10.5, 11.0, 9.5)
        assert isinstance(result, dict)
        assert len(result) > 0

    def test_doji(self):
        """十字星: open ≈ close"""
        result = factor_engine.compute_candle_factors(10.0, 10.01, 11.0, 9.0)
        assert isinstance(result, dict)


class TestTSFactors:
    def test_returns_dict(self, monkeypatch):
        # Need > 60+1 data points for max_window=60
        monkeypatch.setattr(factor_engine, 'get_price_history',
            lambda code, days=120: make_large_ohlcv(120))
        result = factor_engine.compute_ts_factors('600001', use_db=True)
        assert isinstance(result, dict)
        # Should have some keys for each window
        assert len(result) > 0, "Expected non-empty result from TS factors"


class TestNormalizeSignal:
    def test_positive(self):
        assert isinstance(factor_engine._normalize_signal(2.0), float)

    def test_negative(self):
        assert isinstance(factor_engine._normalize_signal(-1.5), float)

    def test_zero(self):
        val = factor_engine._normalize_signal(0.0)
        assert isinstance(val, float)

    def test_tanh_range(self):
        """tanh output is in [-1, 1]"""
        for v in [-10, -2, 0, 2, 10]:
            n = factor_engine._normalize_signal(float(v))
            assert -1 <= n <= 1, f"tanh({v}) = {n} out of range"


class TestIntegrateFactors:
    def test_returns_dict(self, monkeypatch, mock_db):
        monkeypatch.setattr(factor_engine, 'compute_all_factors',
            lambda code, use_db=True: {'macd_signal': 0.6, 'obv_trend': 0.3, 'mfi_signal': 50})
        result = factor_engine.integrate_factors('600001', {})
        assert isinstance(result, dict)
