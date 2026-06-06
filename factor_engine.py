"""
因子引擎 — 基于 vnpy Alpha 158 的关键因子移植
提供K线形态因子、时序统计因子的计算，以及与现有评分体系的融合接口

因子分类：
  - 信号因子 (signal): ksft, rank_20, rsv_20, beta_20, resi_20, macd_signal, obv_trend, mfi_signal, cci_signal → [-1, 1]
  - 描述因子 (descriptive): klen, std_20 → 风险提示

Usage:
    from factor_engine import AlphaFactorEngine

    engine = AlphaFactorEngine()
    factors = engine.compute_all_factors("002281")
    enhanced = engine.integrate_factors("002281", existing_scores)
"""

import numpy as np
from typing import Optional
from db import get_price_history, get_snapshots

__all__ = [
    "AlphaFactorEngine",
    "compute_candle_factors",
    "compute_ts_factors",
    "compute_macd",
    "compute_obv",
    "compute_mfi",
    "compute_cci",
    "compute_wq_alpha1",
    "compute_wq_alpha3",
    "compute_wq_alpha5",
    "compute_wq_alpha15",
    "compute_wq_alpha19",
    "compute_all_factors",
    "integrate_factors",
]

# 信号因子名称列表 — 用于积分转换
SIGNAL_FACTORS = ["ksft", "rank_20", "rsv_20", "beta_20", "resi_20",
                  "macd_signal", "obv_trend", "mfi_signal", "cci_signal",
                  "wq_alpha1", "wq_alpha3", "wq_alpha5", "wq_alpha15", "wq_alpha19"]
# 描述因子名称列表 — 仅用于风险提示
DESCRIPTIVE_FACTORS = ["klen", "std_20"]


class AlphaFactorEngine:
    """Alpha 因子引擎"""

    def __init__(self, use_db: bool = True):
        self.use_db = use_db

    # ----------------------------------------------------------------
    # 公共接口
    # ----------------------------------------------------------------

    def compute_candle_factors(self, open_p: float, close_p: float,
                               high_p: float, low_p: float) -> dict:
        """计算K线形态因子（日线级别）"""
        return compute_candle_factors(open_p, close_p, high_p, low_p)

    def compute_ts_factors(self, code: str,
                           windows: Optional[list[int]] = None) -> dict:
        """从 price_history 读取数据并计算时序因子"""
        if windows is None:
            windows = [5, 10, 20, 30, 60]
        return compute_ts_factors(code, windows, use_db=self.use_db)

    def compute_all_factors(self, code: str) -> dict:
        """融合K线形态因子 + 时序因子，返回完整因子 dict"""
        return compute_all_factors(code, use_db=self.use_db)

    # ----------------------------------------------------------------
    # 新增技术指标因子
    # ----------------------------------------------------------------

    def compute_macd(self, code: str) -> dict:
        """计算 MACD 信号"""
        return compute_macd(code, use_db=self.use_db)

    def compute_obv(self, code: str, window: int = 20) -> dict:
        """计算 OBV 趋势"""
        return compute_obv(code, window, use_db=self.use_db)

    def compute_mfi(self, code: str, period: int = 14) -> dict:
        """计算 MFI"""
        return compute_mfi(code, period, use_db=self.use_db)

    def compute_cci(self, code: str, period: int = 20) -> dict:
        """计算 CCI"""
        return compute_cci(code, period, use_db=self.use_db)

    # ----------------------------------------------------------------
    # WorldQuant 101 Formulaic Alphas (精选因子)
    # ----------------------------------------------------------------

    def compute_wq_alpha1(self, code: str) -> dict:
        """Alpha#1: (close - open) / (high - low + 1e-8) — 日内强度"""
        return compute_wq_alpha1(code, use_db=self.use_db)

    def compute_wq_alpha3(self, code: str) -> dict:
        """Alpha#3: close / vwap — 价格 vs 均价"""
        return compute_wq_alpha3(code, use_db=self.use_db)

    def compute_wq_alpha5(self, code: str) -> dict:
        """Alpha#5: (vwap - close) / (vwap + close) * 100 — 均价偏离度"""
        return compute_wq_alpha5(code, use_db=self.use_db)

    def compute_wq_alpha15(self, code: str) -> dict:
        """Alpha#15: (high / low) - 1 — 日内波动幅度"""
        return compute_wq_alpha15(code, use_db=self.use_db)

    def compute_wq_alpha19(self, code: str) -> dict:
        """Alpha#19: (close - close.shift(5)) / close.shift(5) — 5日动量"""
        return compute_wq_alpha19(code, use_db=self.use_db)

    def integrate_factors(self, code: str,
                          existing_scores: dict) -> dict:
        """将因子计算结果与已有评分体系融合"""
        return integrate_factors(code, existing_scores, use_db=self.use_db)

    # ----------------------------------------------------------------
    # 🅱 多周期因子融合
    # ----------------------------------------------------------------

    def compute_multi_cycle_factors(self, code: str) -> dict:
        """
        三周期因子融合计算（日线 + 周线 + 月线）

        流程：
          1. 从 get_price_history(code, days=200) 获取200日行情
          2. 重采样为周线（5日聚合）和月线（22日聚合）
          3. 在三个周期上分别计算14个信号因子
          4. 返回 {daily: {...}, weekly: {...}, monthly: {...}}

        周线/月线至少需3根K线，否则回退到日线。
        """
        rows = get_price_history(code, days=200)
        if len(rows) < 3:
            return {"daily": {}, "weekly": {}, "monthly": {}}

        arr = _to_arrays(rows)
        opens = arr["open"]
        closes = arr["close"]
        highs = arr["high"]
        lows = arr["low"]
        volumes = arr["volume"]
        n = len(closes)

        # ---- 日线因子 ----
        daily = _compute_factor_signals_from_arrays(opens, closes, highs, lows, volumes)

        # ---- 周线（5日聚合） ----
        weekly_bars = n // 5
        if weekly_bars >= 3:
            w_opens, w_closes, w_highs, w_lows, w_volumes = _resample_ohlcv(
                opens, closes, highs, lows, volumes, 5)
            weekly = _compute_factor_signals_from_arrays(w_opens, w_closes, w_highs, w_lows, w_volumes)
        else:
            weekly = dict(daily)  # 数据不足，回退到日线

        # ---- 月线（22日聚合） ----
        monthly_bars = n // 22
        if monthly_bars >= 3:
            m_opens, m_closes, m_highs, m_lows, m_volumes = _resample_ohlcv(
                opens, closes, highs, lows, volumes, 22)
            monthly = _compute_factor_signals_from_arrays(m_opens, m_closes, m_highs, m_lows, m_volumes)
        else:
            monthly = dict(daily)  # 数据不足，回退到日线

        return {"daily": daily, "weekly": weekly, "monthly": monthly}


# ====================================================================
# K线形态因子
# ====================================================================

def compute_candle_factors(open_p: float, close_p: float,
                           high_p: float, low_p: float) -> dict:
    """
    计算K线形态因子

    Parameters
    ----------
    open_p : float   — 开盘价
    close_p : float  — 收盘价
    high_p : float   — 最高价
    low_p : float    — 最低价

    Returns
    -------
    dict with keys: kmid, klen, kmid_2, ksft, kup, klow

    公式（源自 vnpy Alpha 158）:
        kmid   = (close - open) / open
        klen   = (high - low) / open
        kmid_2 = (close - open) / (high - low + 1e-12)
        ksft   = (close * 2 - high - low) / open
        kup    = (high - max(open, close)) / open
        klow   = (min(open, close) - low) / open
    """
    if open_p <= 0:
        return {"kmid": 0.0, "klen": 0.0, "kmid_2": 0.0,
                "ksft": 0.0, "kup": 0.0, "klow": 0.0}

    denom = open_p  # 分母统一用 open
    hl_range = high_p - low_p + 1e-12

    kmid   = (close_p - open_p) / denom
    klen   = (high_p - low_p) / denom
    kmid_2 = (close_p - open_p) / hl_range
    ksft   = (close_p * 2 - high_p - low_p) / denom
    kup    = (high_p - max(open_p, close_p)) / denom
    klow   = (min(open_p, close_p) - low_p) / denom

    return {
        "kmid":   round(kmid, 6),
        "klen":   round(klen, 6),
        "kmid_2": round(kmid_2, 6),
        "ksft":   round(ksft, 6),
        "kup":    round(kup, 6),
        "klow":   round(klow, 6),
    }


# ====================================================================
# 时序统计因子
# ====================================================================

def _to_arrays(rows: list[dict]) -> dict:
    """将 price_history 的 dict 列表转为 numpy 数组（时间正序）"""
    n = len(rows)
    if n == 0:
        return {}
    opens   = np.array([r["open"]   for r in reversed(rows)], dtype=float)
    closes  = np.array([r["close"]  for r in reversed(rows)], dtype=float)
    highs   = np.array([r["high"]   for r in reversed(rows)], dtype=float)
    lows    = np.array([r["low"]    for r in reversed(rows)], dtype=float)
    volumes = np.array([r["volume"] for r in reversed(rows)], dtype=float)
    return {
        "open": opens, "close": closes,
        "high": highs, "low": lows, "volume": volumes,
    }


def _linear_regression(y: np.ndarray) -> tuple:
    """
    对序列 y 做普通最小二乘线性回归
    返回 (slope, intercept, r_squared, residuals)
    """
    n = len(y)
    if n < 3:
        return 0.0, 0.0, 0.0, np.zeros_like(y)
    x = np.arange(n, dtype=float)
    x_mean = x.mean()
    y_mean = y.mean()
    cov = ((x - x_mean) * (y - y_mean)).sum()
    var_x = ((x - x_mean) ** 2).sum()
    if var_x == 0:
        return 0.0, 0.0, 0.0, np.zeros_like(y)

    slope = cov / var_x
    intercept = y_mean - slope * x_mean
    y_pred = slope * x + intercept
    ss_res = ((y - y_pred) ** 2).sum()
    ss_tot = ((y - y_mean) ** 2).sum()
    r_squared = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0.0
    residuals = y - y_pred
    return slope, intercept, r_squared, residuals


def compute_ts_factors(code: str,
                       windows: Optional[list[int]] = None,
                       use_db: bool = True) -> dict:
    """
    从 price_history 读取历史数据计算时序因子

    Parameters
    ----------
    code    : str       — 股票代码
    windows : list[int] — 滚动窗口列表 (default: [5, 10, 20, 30, 60])
    use_db  : bool      — 是否从数据库读取（保留参数接口）

    Returns
    -------
    dict with keys like:
        roc_5, ma_5, std_5, beta_5, rsqr_5, resi_5, max_5, min_5, rank_5, rsv_5
        ... for each window size.
    如果数据不足（少于最小窗口+1），返回空 dict
    """
    if windows is None:
        windows = [5, 10, 20, 30, 60]

    # 需要至少 max(windows) + 1 天数据（+1 因为需要 N日前的价格）
    max_window = max(windows)
    rows = get_price_history(code, days=max_window + 5)  # 多取5个做缓冲
    if len(rows) < max_window + 1:
        return {}  # 数据不足，跳过

    arr = _to_arrays(rows)
    n = len(arr["close"])
    closes = arr["close"]
    highs  = arr["high"]
    lows   = arr["low"]

    result = {}

    for w in windows:
        if n < w + 1:
            continue

        # 取最近 w+1 个数据（最新数据在末尾）
        c = closes[-(w + 1):]
        h = highs[-(w + 1):]
        l = lows[-(w + 1):]

        latest_close = c[-1]
        n_window = w + 1

        # --- roc: delay(close, w) / close ---
        roc = c[0] / latest_close if latest_close != 0 else 1.0
        result[f"roc_{w}"] = round(roc, 6)

        # --- ma: avg(close, w) / close ---
        # 注意：c[1:] 是最近 w 天的 close
        window_closes = c[1:]  # 最近 w 个交易日
        ma_val = window_closes.mean() / latest_close if latest_close != 0 else 1.0
        result[f"ma_{w}"] = round(ma_val, 6)

        # --- std: stdev(close, w) / close ---
        std_val = window_closes.std(ddof=1) / latest_close if latest_close != 0 else 0.0
        result[f"std_{w}"] = round(std_val, 6)

        # --- beta, rsqr, resi: 线性回归 ---
        slope, intercept, r_sq, residuals = _linear_regression(window_closes)
        result[f"beta_{w}"] = round(slope, 6)
        result[f"rsqr_{w}"] = round(r_sq, 6)
        resi_val = residuals[-1] / latest_close if latest_close != 0 else 0.0
        result[f"resi_{w}"] = round(resi_val, 6)

        # --- max: max(high, w) / close ---
        window_highs = h[1:]
        max_val = window_highs.max() / latest_close if latest_close != 0 else 1.0
        result[f"max_{w}"] = round(max_val, 6)

        # --- min: min(low, w) / close ---
        window_lows = l[1:]
        min_val = window_lows.min() / latest_close if latest_close != 0 else 1.0
        result[f"min_{w}"] = round(min_val, 6)

        # --- rank: 当前价格在N日中的相对排名 [0,1] ---
        # rank = (c[-1] - min(c[1:])) / (max(c[1:]) - min(c[1:]) + 1e-12)
        rank_num = (latest_close - window_closes.min()) / \
                   (window_closes.max() - window_closes.min() + 1e-12)
        result[f"rank_{w}"] = round(float(np.clip(rank_num, 0, 1)), 6)

        # --- rsv: 威廉RSV ---
        # rsv = (close - min(low, N)) / (max(high, N) - min(low, N) + 1e-12)
        rsv_num = (latest_close - window_lows.min()) / \
                  (window_highs.max() - window_lows.min() + 1e-12)
        result[f"rsv_{w}"] = round(float(np.clip(rsv_num, 0, 1)), 6)

    return result


# ====================================================================
# 纯 numpy EMA 实现
# ====================================================================

def _ema(arr: np.ndarray, span: int) -> np.ndarray:
    """
    纯 numpy 实现指数移动平均 (EMA)
    EMA[0] = arr[0]
    EMA[i] = α * arr[i] + (1-α) * EMA[i-1],  α = 2/(span+1)
    """
    alpha = 2.0 / (span + 1)
    out = np.empty_like(arr)
    out[0] = arr[0]
    for i in range(1, len(arr)):
        out[i] = alpha * arr[i] + (1.0 - alpha) * out[i - 1]
    return out


# ====================================================================
# MACD 信号
# ====================================================================

def compute_macd(code: str, use_db: bool = True) -> dict:
    """
    计算 MACD（指数平滑异同移动平均线）

    步骤:
        DIF = EMA(close, 12) - EMA(close, 26)
        DEA = EMA(DIF, 9)
        MACD Histogram = (DIF - DEA) * 2

    Returns
    -------
    dict:
        macd_line       : float — DIF 最新值
        signal_line     : float — DEA 最新值
        macd_histogram  : float — 柱状图最新值
        macd_signal     : float — 归一化到 [-1,1]: tanh(柱状图/0.5)
        macd_cross      : str   — "golden_cross" / "death_cross" / "none"
    数据不足时返回空 dict
    """
    rows = get_price_history(code, days=35)  # 至少需要 26+9
    n = len(rows)
    if n < 27:  # 至少需要 26 天计算 DIF + 1
        return {}

    arr = _to_arrays(rows)
    closes = arr["close"]

    if len(closes) < 27:
        return {}

    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    dif = ema12 - ema26
    dea = _ema(dif, 9)
    histogram = (dif - dea) * 2.0

    # 取最新值
    macd_line = float(round(dif[-1], 6))
    signal_line = float(round(dea[-1], 6))
    macd_histogram = float(round(histogram[-1], 6))

    # 归一化信号: tanh(柱状图/0.5) 将典型值映射到 [-1,1]
    macd_signal = float(np.tanh(histogram[-1] / 0.5))

    # 判断交叉: 比较前一根和当前 DIF 与 DEA 关系
    if len(dif) >= 2 and len(dea) >= 2:
        prev_diff = dif[-2] - dea[-2]
        curr_diff = dif[-1] - dea[-1]
        if prev_diff < 0 and curr_diff >= 0:
            cross = "golden_cross"
        elif prev_diff > 0 and curr_diff <= 0:
            cross = "death_cross"
        else:
            cross = "none"
    else:
        cross = "none"

    return {
        "macd_line": macd_line,
        "signal_line": signal_line,
        "macd_histogram": macd_histogram,
        "macd_signal": float(round(macd_signal, 6)),
        "macd_cross": cross,
    }


# ====================================================================
# OBV 趋势
# ====================================================================

def compute_obv(code: str, window: int = 20, use_db: bool = True) -> dict:
    """
    计算 OBV（On-Balance Volume，能量潮）

    步骤:
        1. OBV 累积: 当日 close > 前日 close → +volume; 反之 -volume
        2. obv_trend: OBV 序列的线性回归斜率，tanh 归一化到 [-1,1]
        3. obv_price_divergence: 价格与 OBV 的 Pearson 相关方向，[-1,1]

    Returns
    -------
    dict:
        obv_value               : float — 当前 OBV 值
        obv_trend               : float — OBV 斜率，归一化到 [-1,1]
        obv_price_divergence    : float — 价格与 OBV 背离度 [-1,1]
    数据不足时返回空 dict
    """
    rows = get_price_history(code, days=window + 10)
    if len(rows) < window + 2:  # 至少需要 window+1 个价格变化来计算 OBV
        return {}

    arr = _to_arrays(rows)
    closes = arr["close"]
    volumes = arr["volume"]

    if len(closes) < window + 2:
        return {}

    # 计算 OBV 序列
    price_diff = np.diff(closes)
    obv_raw = np.zeros(len(closes), dtype=float)
    obv_raw[0] = 0.0
    for i in range(1, len(closes)):
        if price_diff[i - 1] > 1e-12:
            obv_raw[i] = obv_raw[i - 1] + volumes[i]
        elif price_diff[i - 1] < -1e-12:
            obv_raw[i] = obv_raw[i - 1] - volumes[i]
        else:
            obv_raw[i] = obv_raw[i - 1]

    obv_value = float(round(obv_raw[-1], 2))

    # OBV 斜率（最近 window 天）
    obv_window = obv_raw[-window:]
    slope, *_ = _linear_regression(obv_window)
    # 归一化斜率到 [-1,1]
    obv_trend = float(np.tanh(slope / (np.std(obv_window) + 1e-12) * 2.0))

    # 价格与 OBV 背离度: 最近 window 天的价格变化方向 vs OBV 变化方向
    price_window = closes[-window:]
    price_slope, *_ = _linear_regression(price_window)
    # 如果价格上升但 OBV 下降(负相关) => 负背离; 同向 => 正背离
    # 用斜率符号的乘积判断方向
    sign_product = np.sign(price_slope) * np.sign(slope) if abs(price_slope) > 1e-12 and abs(slope) > 1e-12 else 0.0
    # 用价格-OBV相关强度调制幅度
    if len(price_window) >= 3:
        price_norm = (price_window - price_window.mean()) / (price_window.std() + 1e-12)
        obv_norm = (obv_window - obv_window.mean()) / (obv_window.std() + 1e-12)
        correlation = float(np.clip((price_norm * obv_norm).mean(), -1.0, 1.0))
    else:
        correlation = 0.0
    obv_price_divergence = float(round(correlation, 6))

    return {
        "obv_value": obv_value,
        "obv_trend": float(round(obv_trend, 6)),
        "obv_price_divergence": obv_price_divergence,
    }


# ====================================================================
# MFI (Money Flow Index)
# ====================================================================

def compute_mfi(code: str, period: int = 14, use_db: bool = True) -> dict:
    """
    计算 MFI（资金流量指标）

    MFI = 100 - 100 / (1 + MFR)
    MFR = Sum(Positive Money Flow, period) / Sum(Negative Money Flow, period)
    Typical Price = (High + Low + Close) / 3
    Raw Money Flow = Typical Price × Volume

    Returns
    -------
    dict:
        mfi_value      : float — MFI 值 [0, 100]
        mfi_signal     : float — 归一化到 [-1,1]: (MFI-50)/50
        mfi_oversold   : bool  — MFI < 20
        mfi_overbought : bool  — MFI > 80
    数据不足时返回空 dict
    """
    rows = get_price_history(code, days=period + 10)
    if len(rows) < period + 2:
        return {}

    arr = _to_arrays(rows)
    highs = arr["high"]
    lows = arr["low"]
    closes = arr["close"]
    volumes = arr["volume"]

    if len(closes) < period + 2:
        return {}

    # Typical Price
    tp = (highs + lows + closes) / 3.0
    # Raw Money Flow
    rmf = tp * volumes

    # 逐日计算正/负资金流
    n = len(tp)
    pos_mf = np.zeros(n, dtype=float)
    neg_mf = np.zeros(n, dtype=float)
    for i in range(1, n):
        if tp[i] > tp[i - 1]:
            pos_mf[i] = rmf[i]
        elif tp[i] < tp[i - 1]:
            neg_mf[i] = rmf[i]
        # 持平则两者都为0

    # 取最近 period 天求和
    pos_sum = pos_mf[-period:].sum()
    neg_sum = neg_mf[-period:].sum()

    if neg_sum < 1e-12:
        mfi_value = 100.0 if pos_sum > 1e-12 else 50.0
    else:
        mfr = pos_sum / neg_sum
        mfi_value = 100.0 - 100.0 / (1.0 + mfr)

    mfi_signal = float(np.clip((mfi_value - 50.0) / 50.0, -1.0, 1.0))
    mfi_oversold = mfi_value < 20.0
    mfi_overbought = mfi_value > 80.0

    return {
        "mfi_value": float(round(mfi_value, 4)),
        "mfi_signal": float(round(mfi_signal, 6)),
        "mfi_oversold": bool(mfi_oversold),
        "mfi_overbought": bool(mfi_overbought),
    }


# ====================================================================
# CCI (Commodity Channel Index)
# ====================================================================

def compute_cci(code: str, period: int = 20, use_db: bool = True) -> dict:
    """
    计算 CCI（商品通道指数）

    TP = (High + Low + Close) / 3
    CCI = (TP - SMA(TP)) / (0.015 × Mean Deviation)

    Returns
    -------
    dict:
        cci_value  : float — CCI 原始值
        cci_signal : float — 归一化到 [-1,1]: tanh(CCI/100)
    数据不足时返回空 dict
    """
    rows = get_price_history(code, days=period + 10)
    if len(rows) < period + 2:
        return {}

    arr = _to_arrays(rows)
    highs = arr["high"]
    lows = arr["low"]
    closes = arr["close"]

    if len(closes) < period + 2:
        return {}

    # Typical Price
    tp = (highs + lows + closes) / 3.0

    # 取最近 period 天
    tp_window = tp[-period:]
    sma_tp = tp_window.mean()

    # Mean Deviation = sum(|TP - SMA|) / period
    mean_dev = np.abs(tp_window - sma_tp).mean()

    if mean_dev < 1e-12:
        cci_value = 0.0
    else:
        cci_value = (tp_window[-1] - sma_tp) / (0.015 * mean_dev)

    cci_signal = float(np.tanh(cci_value / 100.0))

    return {
        "cci_value": float(round(cci_value, 4)),
        "cci_signal": float(round(cci_signal, 6)),
    }


# ====================================================================
# WorldQuant 101 Formulaic Alphas (精选因子)
# ====================================================================

def compute_wq_alpha1(code: str, use_db: bool = True) -> dict:
    """
    WorldQuant Alpha#1: (close - open) / (high - low + 1e-8)

    描述: 日内强度 — 收盘相对开盘的位移占日内波幅的比例。
          +1 表示强势收盘（接近最高），-1 表示弱势收盘（接近最低）。
    数据不足时返回空 dict
    """
    rows = get_price_history(code, days=1)
    if not rows:
        return {}
    latest = rows[0]
    open_p   = float(latest.get("open", 0))
    close_p  = float(latest.get("close", 0))
    high_p   = float(latest.get("high", 0))
    low_p    = float(latest.get("low", 0))

    hl_range = high_p - low_p + 1e-8
    raw = (close_p - open_p) / hl_range  # typical range [-1, 1]
    signal = float(np.tanh(raw * 2.0))   # normalize to [-1, 1]

    return {
        "wq_alpha1": float(round(signal, 6)),
        "wq_alpha1_raw": float(round(raw, 6)),
    }


def compute_wq_alpha3(code: str, use_db: bool = True) -> dict:
    """
    WorldQuant Alpha#3: close / vwap

    描述: 收盘价与成交均价的比值。
          vwap = amount / volume （成交额 / 成交量）。
          >1 表示收盘高于均价（偏强），<1 表示低于均价（偏弱）。
    数据不足时返回空 dict
    """
    # 从 price_history 获取最新 close
    rows = get_price_history(code, days=1)
    if not rows:
        return {}
    latest = rows[0]
    close_p = float(latest.get("close", 0))
    if close_p <= 0:
        return {}

    # 从 daily_snapshots 获取 amount/volume 用于 VWAP
    snapshots = get_snapshots(code, days=1)
    if not snapshots:
        return {}
    snap = snapshots[0]
    amount = float(snap.get("amount", 0) or 0)
    volume = float(snap.get("volume", 0) or 0)
    if volume < 1e-8:
        return {}

    vwap = amount / volume
    if vwap <= 0:
        return {}

    raw = close_p / vwap          # around 1.0
    signal = float(np.tanh((raw - 1.0) * 10.0))  # normalize to [-1, 1]

    return {
        "wq_alpha3": float(round(signal, 6)),
        "wq_alpha3_raw": float(round(raw, 6)),
    }


def compute_wq_alpha5(code: str, use_db: bool = True) -> dict:
    """
    WorldQuant Alpha#5: (vwap - close) / (vwap + close) * 100

    描述: 均价偏离度 — 衡量当前价格相对于成交均价的偏离百分比。
          + 表示均价高于收盘（日内走势偏弱），- 表示均价低于收盘（偏强）。
    数据不足时返回空 dict
    """
    # 从 price_history 获取最新 close
    rows = get_price_history(code, days=1)
    if not rows:
        return {}
    latest = rows[0]
    close_p = float(latest.get("close", 0))
    if close_p <= 0:
        return {}

    # 从 daily_snapshots 获取 amount/volume 用于 VWAP
    snapshots = get_snapshots(code, days=1)
    if not snapshots:
        return {}
    snap = snapshots[0]
    amount = float(snap.get("amount", 0) or 0)
    volume = float(snap.get("volume", 0) or 0)
    if volume < 1e-8:
        return {}

    vwap = amount / volume
    if vwap <= 0:
        return {}

    denom = vwap + close_p
    if abs(denom) < 1e-8:
        return {}

    raw = (vwap - close_p) / denom * 100.0  # range roughly [-100, 100]
    signal = float(np.tanh(raw / 50.0))      # normalize to [-1, 1]

    return {
        "wq_alpha5": float(round(signal, 6)),
        "wq_alpha5_raw": float(round(raw, 6)),
    }


def compute_wq_alpha15(code: str, use_db: bool = True) -> dict:
    """
    WorldQuant Alpha#15: (high / low) - 1

    描述: 日内波动幅度 — 最高价相对最低价的涨幅比例。
          + 表示日内波动大，0 表示几乎无波动。
    数据不足时返回空 dict
    """
    rows = get_price_history(code, days=1)
    if not rows:
        return {}
    latest = rows[0]
    high_p = float(latest.get("high", 0))
    low_p  = float(latest.get("low", 0))
    if low_p <= 0:
        return {}

    raw = (high_p / low_p) - 1.0       # typical range [0, ~0.1]
    signal = float(np.tanh(raw * 20.0)) # normalize to [-1, 1]

    return {
        "wq_alpha15": float(round(signal, 6)),
        "wq_alpha15_raw": float(round(raw, 6)),
    }


def compute_wq_alpha19(code: str, use_db: bool = True) -> dict:
    """
    WorldQuant Alpha#19: (close - close.shift(5)) / close.shift(5)

    描述: 5日动量 — 当前收盘价相对于5个交易日前的涨跌幅。
          + 表示过去5日上涨，- 表示过去5日下跌。
    数据不足时返回空 dict
    """
    rows = get_price_history(code, days=6)
    if len(rows) < 6:
        return {}

    # rows are DESC (newest first), reverse to get chronological order
    closes = [float(r["close"]) for r in reversed(rows)]
    if len(closes) < 6:
        return {}

    close_now = closes[-1]
    close_5d  = closes[0]   # 5 trading days ago

    if abs(close_5d) < 1e-8:
        return {}

    raw = (close_now - close_5d) / close_5d   # typical range [-0.1, 0.1]
    signal = float(np.tanh(raw * 10.0))        # normalize to [-1, 1]

    return {
        "wq_alpha19": float(round(signal, 6)),
        "wq_alpha19_raw": float(round(raw, 6)),
    }


# ====================================================================
# 完整因子计算
# ====================================================================

def compute_all_factors(code: str, use_db: bool = True) -> dict:
    """
    融合K线形态因子 + 时序因子，返回完整因子 dict

    Parameters
    ----------
    code   : str  — 股票代码
    use_db : bool — 是否从数据库读取

    Returns
    -------
    dict with keys:
        candle: {...}     — K线形态因子
        ts: {...}         — 时序因子
        signals: {...}    — 信号因子（归一化到 [-1, 1]）
        descriptive: {...} — 描述因子（风险提示用）
    """
    # ---- 从数据库获取最新快照用于K线形态计算 ----
    rows = get_price_history(code, days=1)
    if not rows:
        return {"candle": {}, "ts": {}, "signals": {}, "descriptive": {}}

    latest = rows[0]  # DESC order, newest first
    open_p   = float(latest.get("open", 0))
    close_p  = float(latest.get("close", 0))
    high_p   = float(latest.get("high", 0))
    low_p    = float(latest.get("low", 0))

    candle_factors = compute_candle_factors(open_p, close_p, high_p, low_p)

    # ---- 时序因子 ----
    ts_factors = compute_ts_factors(code, use_db=use_db)

    # ---- 技术指标因子 (MACD / OBV / MFI / CCI) ----
    indicator_signals = {}
    try:
        macd = compute_macd(code, use_db=use_db)
        if macd:
            indicator_signals["macd_signal"] = macd.get("macd_signal", 0.0)
    except Exception:
        pass
    try:
        obv = compute_obv(code, use_db=use_db)
        if obv:
            indicator_signals["obv_trend"] = obv.get("obv_trend", 0.0)
    except Exception:
        pass
    try:
        mfi = compute_mfi(code, use_db=use_db)
        if mfi:
            indicator_signals["mfi_signal"] = mfi.get("mfi_signal", 0.0)
    except Exception:
        pass
    try:
        cci = compute_cci(code, use_db=use_db)
        if cci:
            indicator_signals["cci_signal"] = cci.get("cci_signal", 0.0)
    except Exception:
        pass

    # ---- WorldQuant 101 Formulaic Alphas ----
    wq_signals = {}
    try:
        a1 = compute_wq_alpha1(code, use_db=use_db)
        if a1:
            wq_signals["wq_alpha1"] = a1.get("wq_alpha1", 0.0)
    except Exception:
        pass
    try:
        a3 = compute_wq_alpha3(code, use_db=use_db)
        if a3:
            wq_signals["wq_alpha3"] = a3.get("wq_alpha3", 0.0)
    except Exception:
        pass
    try:
        a5 = compute_wq_alpha5(code, use_db=use_db)
        if a5:
            wq_signals["wq_alpha5"] = a5.get("wq_alpha5", 0.0)
    except Exception:
        pass
    try:
        a15 = compute_wq_alpha15(code, use_db=use_db)
        if a15:
            wq_signals["wq_alpha15"] = a15.get("wq_alpha15", 0.0)
    except Exception:
        pass
    try:
        a19 = compute_wq_alpha19(code, use_db=use_db)
        if a19:
            wq_signals["wq_alpha19"] = a19.get("wq_alpha19", 0.0)
    except Exception:
        pass

    # ---- 分离信号因子 & 描述因子，归一化到 [-1, 1] ----
    signals = {}
    for name in SIGNAL_FACTORS:
        raw = (candle_factors.get(name) or ts_factors.get(name)
               or indicator_signals.get(name) or wq_signals.get(name))
        if raw is not None:
            signals[name] = round(_normalize_signal(raw), 6)

    descriptive = {}
    for name in DESCRIPTIVE_FACTORS:
        raw = candle_factors.get(name) or ts_factors.get(name)
        if raw is not None:
            descriptive[name] = round(float(raw), 6)

    return {
        "candle": candle_factors,
        "ts": ts_factors,
        "signals": signals,
        "descriptive": descriptive,
    }


def _normalize_signal(value: float) -> float:
    """
    将原始因子值归一化到 [-1, 1]
    使用双曲正切 tanh 将任意实数压缩到 (-1, 1)
    """
    return float(np.tanh(value * 2.0))  # *2 增强灵敏度


# ====================================================================
# 多周期融合 — 辅助函数
# ====================================================================

def _resample_ohlcv(
    opens: np.ndarray,
    closes: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    volumes: np.ndarray,
    period: int,
) -> tuple:
    """
    将日线 OHLCV 数组聚合为周线/月线

    Parameters
    ----------
    opens, closes, highs, lows, volumes : np.ndarray — 时间正序
    period : int — 聚合周期（5=周线，22=月线）

    Returns
    -------
    tuple (r_opens, r_closes, r_highs, r_lows, r_volumes)
        周open  = 第1天开盘
        周close = 最后1天收盘
        周high  = 期间最高
        周low   = 期间最低
        周vol   = 期间成交量之和
    """
    n = len(opens)
    n_bars = n // period
    if n_bars < 1:
        return opens, closes, highs, lows, volumes

    trimmed = n_bars * period
    o = opens[-trimmed:].copy()
    c = closes[-trimmed:].copy()
    h = highs[-trimmed:].copy()
    l = lows[-trimmed:].copy()
    v = volumes[-trimmed:].copy()

    o_r = o.reshape(n_bars, period)
    c_r = c.reshape(n_bars, period)
    h_r = h.reshape(n_bars, period)
    l_r = l.reshape(n_bars, period)
    v_r = v.reshape(n_bars, period)

    return (
        o_r[:, 0],           # bar open   = first day's open
        c_r[:, -1],          # bar close  = last day's close
        h_r.max(axis=1),     # bar high   = max of daily highs
        l_r.min(axis=1),     # bar low    = min of daily lows
        v_r.sum(axis=1),     # bar volume = sum of daily volumes
    )


def _compute_factor_signals_from_arrays(
    opens: np.ndarray,
    closes: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    volumes: np.ndarray,
) -> dict:
    """
    从原始 OHLCV 数组（时间正序）计算全部14个信号因子

    返回 dict，key 与 SIGNAL_FACTORS 一致：
        ksft, rank_20, rsv_20, beta_20, resi_20,
        macd_signal, obv_trend, mfi_signal, cci_signal,
        wq_alpha1, wq_alpha3, wq_alpha5, wq_alpha15, wq_alpha19
    """
    n = len(closes)
    signals = {}
    if n < 3:
        return signals

    # ---- 1. K线形态因子（ksft） ----
    candle = compute_candle_factors(
        float(opens[-1]), float(closes[-1]),
        float(highs[-1]), float(lows[-1]),
    )
    if "ksft" in candle:
        signals["ksft"] = _normalize_signal(candle["ksft"])

    # ---- 2. 时序因子（rank_20 / rsv_20 / beta_20 / resi_20） ----
    ts_window = min(20, n - 1)
    if ts_window >= 5:
        c = closes[-(ts_window + 1):]
        h = highs[-(ts_window + 1):]
        l = lows[-(ts_window + 1):]

        latest_close = float(c[-1])
        window_closes = c[1:]
        window_highs = h[1:]
        window_lows = l[1:]

        # rank_20
        denom = window_closes.max() - window_closes.min() + 1e-12
        rank_val = (latest_close - window_closes.min()) / denom
        signals["rank_20"] = float(np.clip(rank_val, 0, 1))

        # rsv_20
        denom = window_highs.max() - window_lows.min() + 1e-12
        rsv_val = (latest_close - window_lows.min()) / denom
        signals["rsv_20"] = float(np.clip(rsv_val, 0, 1))

        # beta_20 & resi_20
        if ts_window >= 3:
            slope, _, _, residuals = _linear_regression(window_closes)
            signals["beta_20"] = _normalize_signal(slope)
            resi_val = float(residuals[-1]) / latest_close if latest_close != 0 else 0.0
            signals["resi_20"] = _normalize_signal(resi_val)

    # ---- 3. MACD 信号 ----
    if n >= 27:
        ema12 = _ema(closes, 12)
        ema26 = _ema(closes, 26)
        dif = ema12 - ema26
        dea = _ema(dif, 9)
        histogram = (dif - dea) * 2.0
        signals["macd_signal"] = float(np.tanh(histogram[-1] / 0.5))

    # ---- 4. OBV 趋势 ----
    if n >= 22:
        price_diff = np.diff(closes)
        obv_raw = np.zeros(n, dtype=float)
        for i in range(1, n):
            if price_diff[i - 1] > 1e-12:
                obv_raw[i] = obv_raw[i - 1] + volumes[i]
            elif price_diff[i - 1] < -1e-12:
                obv_raw[i] = obv_raw[i - 1] - volumes[i]
            else:
                obv_raw[i] = obv_raw[i - 1]

        obv_window = obv_raw[-20:]
        if len(obv_window) >= 3:
            slope, _, _, _ = _linear_regression(obv_window)
            obv_trend = float(np.tanh(slope / (np.std(obv_window) + 1e-12) * 2.0))
            signals["obv_trend"] = obv_trend

    # ---- 5. MFI 信号 ----
    mfi_p = min(14, n - 1)
    if mfi_p >= 5:
        tp = (highs + lows + closes) / 3.0
        rmf = tp * volumes
        pos_mf = np.zeros(n, dtype=float)
        neg_mf = np.zeros(n, dtype=float)
        for i in range(1, n):
            if tp[i] > tp[i - 1]:
                pos_mf[i] = rmf[i]
            elif tp[i] < tp[i - 1]:
                neg_mf[i] = rmf[i]
        pos_sum = pos_mf[-mfi_p:].sum()
        neg_sum = neg_mf[-mfi_p:].sum()
        if neg_sum < 1e-12:
            mfi_val = 100.0 if pos_sum > 1e-12 else 50.0
        else:
            mfi_val = 100.0 - 100.0 / (1.0 + pos_sum / neg_sum)
        signals["mfi_signal"] = float(np.clip((mfi_val - 50.0) / 50.0, -1.0, 1.0))

    # ---- 6. CCI 信号 ----
    cci_p = min(20, n)
    if cci_p >= 5:
        tp = (highs + lows + closes) / 3.0
        tp_win = tp[-cci_p:]
        sma_tp = tp_win.mean()
        mean_dev = np.abs(tp_win - sma_tp).mean()
        if mean_dev < 1e-12:
            cci_val = 0.0
        else:
            cci_val = (tp_win[-1] - sma_tp) / (0.015 * mean_dev)
        signals["cci_signal"] = float(np.tanh(cci_val / 100.0))

    # ---- 7. WQ Alpha#1: (close - open)/(high - low) ----
    hl = highs[-1] - lows[-1] + 1e-8
    raw1 = (closes[-1] - opens[-1]) / hl
    signals["wq_alpha1"] = float(np.tanh(raw1 * 2.0))

    # ---- 8. WQ Alpha#3: close / vwap ----
    total_vol = volumes.sum()
    if total_vol > 1e-8:
        vwap = np.sum((highs + lows + closes) / 3.0 * volumes) / total_vol
        if vwap > 0:
            raw3 = closes[-1] / vwap
            signals["wq_alpha3"] = float(np.tanh((raw3 - 1.0) * 10.0))

    # ---- 9. WQ Alpha#5: (vwap - close)/(vwap + close)*100 ----
    if total_vol > 1e-8:
        vwap = np.sum((highs + lows + closes) / 3.0 * volumes) / total_vol
        denom = vwap + closes[-1]
        if abs(denom) > 1e-8:
            raw5 = (vwap - closes[-1]) / denom * 100.0
            signals["wq_alpha5"] = float(np.tanh(raw5 / 50.0))

    # ---- 10. WQ Alpha#15: (high / low) - 1 ----
    if lows[-1] > 0:
        raw15 = (highs[-1] / lows[-1]) - 1.0
        signals["wq_alpha15"] = float(np.tanh(raw15 * 20.0))

    # ---- 11. WQ Alpha#19: 5-bar momentum ----
    if n >= 6:
        close_now = closes[-1]
        close_5b = closes[-6]
        if abs(close_5b) > 1e-8:
            raw19 = (close_now - close_5b) / close_5b
            signals["wq_alpha19"] = float(np.tanh(raw19 * 10.0))

    return signals


# ====================================================================
# 融合接口
# ====================================================================

def integrate_factors(code: str, existing_scores: dict,
                      use_db: bool = True) -> dict:
    """
    将因子计算结果与已有评分体系融合

    融合逻辑：
      1. 计算完整因子
      2. 将9个信号因子分别转为额外评分（每个 ±5 分）
      3. 累加得到 factor_score (范围: -45 ~ +45)
      4. 将因子数据写入 existing_scores["details"]["factors"]

    Parameters
    ----------
    code             : str  — 股票代码
    existing_scores  : dict — scorer.py 生成的评分 dict
                              （包含 total_score, base_score, zone_score 等）
    use_db           : bool — 是否从数据库读取

    Returns
    -------
    dict — 增强后的评分 dict，新增字段：
        factor_score : float
        details["factors"] : dict
    """
    factors = compute_all_factors(code, use_db=use_db)
    signals = factors.get("signals", {})

    # 信号因子 → 评分加成（每个 ±5 分）
    factor_adjustments = {}
    total_factor_score = 0.0
    for name in SIGNAL_FACTORS:
        val = signals.get(name)
        if val is None:
            continue
        # val ∈ [-1, 1] → score ∈ [-5, 5]
        score_adj = round(val * 5.0, 2)
        factor_adjustments[name] = score_adj
        total_factor_score += score_adj

    # 复制并增强
    enhanced = dict(existing_scores)
    enhanced["factor_score"] = round(total_factor_score, 2)

    details = dict(enhanced.get("details", {}))
    details["factors"] = {
        "signals": signals,
        "descriptive": factors.get("descriptive", {}),
        "candle_raw": factors.get("candle", {}),
        "ts_raw": factors.get("ts", {}),
        "adjustments": factor_adjustments,
        "total_factor_score": round(total_factor_score, 2),
    }
    enhanced["details"] = details

    return enhanced


# ====================================================================
# app.py 调用入口
# ====================================================================

_ENGINE_CACHE = None


def get_engine() -> AlphaFactorEngine:
    """缓存引擎实例（避免重复创建）"""
    global _ENGINE_CACHE
    if _ENGINE_CACHE is None:
        _ENGINE_CACHE = AlphaFactorEngine(use_db=True)
    return _ENGINE_CACHE


def get_current_signals() -> list[dict]:
    """
    供 app.py 调用的标准化入口。
    遍历所有监控标的，计算因子信号，返回统一列表格式。
    每个元素: { code, name, factors: {...}, signal: float }
    """
    from config import STOCK_DETAILS, STOCK_MAP

    engine = get_engine()
    results = []
    for code in STOCK_DETAILS:
        name = STOCK_MAP.get(code, {}).get("name", code)
        try:
            factors = engine.compute_all_factors(code)
            signals = factors.get("signals", {})
            if not signals:
                continue
            # 综合信号值：9个信号因子加权平均
            signal_vals = [v for v in signals.values() if v is not None]
            avg_signal = round(sum(signal_vals) / len(signal_vals), 4) if signal_vals else 0.0
            results.append({
                "code": code,
                "name": name,
                "factors": {
                    "signals": signals,
                    "descriptive": factors.get("descriptive", {}),
                },
                "signal": avg_signal,
            })
        except Exception:
            continue
    return results


# ====================================================================
# 独立运行测试
# ====================================================================
if __name__ == "__main__":
    print("=" * 60)
    print("🧪 AlphaFactorEngine 独立测试")
    print("=" * 60)

    engine = AlphaFactorEngine(use_db=True)

    # 测试1: K线形态因子（模拟数据）
    print("\n📊 K线形态因子（模拟: O=10, C=10.5, H=11, L=9.8）")
    candle = engine.compute_candle_factors(10.0, 10.5, 11.0, 9.8)
    for k, v in candle.items():
        print(f"  {k:8s} = {v:>10.6f}")

    # 测试2: 从库读取数据计算
    test_codes = ["002281", "000988", "603083", "600487"]
    for code in test_codes:
        print(f"\n📈 {code} 完整因子:")
        factors = engine.compute_all_factors(code)
        if not factors.get("ts"):
            print(f"  ⚠️ 时序因子数据不足，跳过")
            continue
        print(f"  信号因子: {factors['signals']}")
        print(f"  描述因子: {factors['descriptive']}")

    # 测试3: 独立技术指标因子
    print("\n📐 独立技术指标因子测试:")
    for code in test_codes:
        print(f"\n  {code}:")
        macd = engine.compute_macd(code)
        if macd:
            print(f"    MACD: macd_signal={macd.get('macd_signal', 'N/A'):.4f}, cross={macd.get('macd_cross', 'N/A')}")
        else:
            print(f"    MACD: ⚠️ 数据不足")
        obv = engine.compute_obv(code)
        if obv:
            print(f"    OBV:  obv_trend={obv.get('obv_trend', 'N/A'):.4f}, divergence={obv.get('obv_price_divergence', 'N/A'):.4f}")
        else:
            print(f"    OBV:  ⚠️ 数据不足")
        mfi = engine.compute_mfi(code)
        if mfi:
            print(f"    MFI:  mfi_signal={mfi.get('mfi_signal', 'N/A'):.4f}, value={mfi.get('mfi_value', 'N/A'):.1f}")
        else:
            print(f"    MFI:  ⚠️ 数据不足")
        cci = engine.compute_cci(code)
        if cci:
            print(f"    CCI:  cci_signal={cci.get('cci_signal', 'N/A'):.4f}, value={cci.get('cci_value', 'N/A'):.1f}")
        else:
            print(f"    CCI:  ⚠️ 数据不足")

    # 测试4: 融合接口
    print("\n🔗 融合接口测试:")
    mock_scores = {
        "date": "2025-01-01",
        "total_score": 75.0,
        "base_score": 70,
        "zone_score": 80,
        "momentum_score": 65,
        "volume_score": 60,
        "details": {"price": 42.5, "change_pct": 1.2},
    }
    for code in test_codes[:1]:
        enhanced = engine.integrate_factors(code, mock_scores)
        print(f"  {code}: factor_score={enhanced.get('factor_score', 'N/A')}")
        adj = enhanced.get("details", {}).get("factors", {}).get("adjustments", {})
        print(f"  调整明细: {adj}")

    print("\n✅ 测试完成")
