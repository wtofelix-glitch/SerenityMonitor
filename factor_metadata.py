"""
因子元数据 — 集中管理因子名称、标签、emoji、信号因子列表
所有模块应从此处导入，避免分散定义导致不一致。
"""

# ── 14 信号因子列表 ──
SIGNAL_FACTORS = [
    "ksft", "rank_20", "rsv_20", "beta_20", "resi_20",
    "macd_signal", "obv_trend", "mfi_signal", "cci_signal",
    "wq_alpha1", "wq_alpha3", "wq_alpha5", "wq_alpha15", "wq_alpha19",
]

# ── 因子中文名 ──
FACTOR_NAMES = {
    "ksft": "K线形态", "rank_20": "Rank", "rsv_20": "RSV",
    "beta_20": "Beta", "resi_20": "残差", "macd_signal": "MACD",
    "obv_trend": "OBV", "mfi_signal": "MFI", "cci_signal": "CCI",
    "wq_alpha1": "A1日内", "wq_alpha3": "A3均价", "wq_alpha5": "A5价偏",
    "wq_alpha15": "A15波幅", "wq_alpha19": "A19动量",
}

# ── 因子 emoji ──
FACTOR_EMOJIS = {
    "ksft": "\U0001F4CA", "rank_20": "\U0001F3C6", "rsv_20": "\U0001F4C8",
    "beta_20": "\U0001F4C9", "resi_20": "\U0001F4D0", "macd_signal": "\U0001F504",
    "obv_trend": "\U0001F4E6", "mfi_signal": "\U0001F4B0", "cci_signal": "\U0001F321️",
    "wq_alpha1": "\U0001F4CC", "wq_alpha3": "⚖️", "wq_alpha5": "\U0001F3AF",
    "wq_alpha15": "\U0001F4CF", "wq_alpha19": "⏩",
}

# ── 向后兼容别名 ──
FACTOR_CN = FACTOR_NAMES
FACTOR_LABELS = FACTOR_NAMES
FACTOR_KEYS = SIGNAL_FACTORS
