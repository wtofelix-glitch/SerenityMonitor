"""
Serenity Scorer — 多因子每日评分引擎
集成信号引擎 → 输出可执行评分 + 买卖信号
"""
from datetime import date
import json

from data_engine import get_all_today_snapshots
from config import STOCK_DETAILS, STOCK_MAP, ALL_CODES, compute_serenity_score
from db import save_score_history, save_price_history, get_price_history, get_avg_volume
from factor_engine import AlphaFactorEngine
from moat_factor import compute_moat_score  # v2.0 护城河因子
from signal_engine import generate_signals, compute_technical_factors, compute_trend_score, confirm_buy_signal, get_signal_level, get_position_signal
from sentiment_engine import compute_sentiment_score
from serenity_logger import get_logger

log = get_logger(__name__)

try:
    from metrics import observe_score_duration, SCORE_COUNT, SCORE_ERRORS, SIGNAL_ACTIONS
    METRICS_AVAILABLE = True
except ImportError:
    METRICS_AVAILABLE = False
    # metrics 不可用时，装饰器降级为空操作
    def observe_score_duration(f):
        return f

# 因子引擎
try:
    from factor_engine import AlphaFactorEngine
    FACTOR_ENGINE = AlphaFactorEngine()
    FACTOR_ENGINE_AVAILABLE = True
except ImportError:
    FACTOR_ENGINE = None
    FACTOR_ENGINE_AVAILABLE = False

# 市场风格感知
try:
    from market_sense import MarketSense
    MARKET_SENSE_AVAILABLE = True
except ImportError:
    MARKET_SENSE_AVAILABLE = False

# 动态权重 — 优先加载 weight_adjuster 的调整后权重
try:
    from weight_adjuster import load_adjusted_weights
    score_weight = load_adjusted_weights()
except Exception:
    score_weight = {
        "base": 0.14,       # 基本面适配度
        "zone": 0.14,       # 价格位置
        "momentum": 0.14,   # 动量
        "volume": 0.04,     # 成交量
        "serenity": 0.14,   # Serenity 框架匹配度
        "factor": 0.14,     # 因子引擎评分
        "technical": 0.09,  # 技术面评分
        "sentiment": 0.08,  # 🆕 新闻情绪评分
        "moat": 0.07,       # v2.0 护城河因子
        "guru_wisdom": 0.04,  # 🆕 大师智慧因子
    }

# 非侵入式 fallback：确保所有 10 个维度都有默认值
_SCORE_WEIGHT_DEFAULTS = {
    "base": 0.14, "zone": 0.14, "momentum": 0.13, "volume": 0.04,
    "serenity": 0.13, "factor": 0.13, "technical": 0.08, "sentiment": 0.07,
    "moat": 0.07, "guru_wisdom": 0.04,
}
for k, v in _SCORE_WEIGHT_DEFAULTS.items():
    score_weight.setdefault(k, v)

# 归一化：确保权重和为 1.0（防止旧缓存权重缺少新维度）
_sw_total = sum(score_weight.values())
if abs(_sw_total - 1.0) > 0.001:
    for k in score_weight:
        score_weight[k] = round(score_weight[k] / _sw_total, 4)
    # 循环精度修正
    _diff = round(1.0 - sum(score_weight.values()), 4)
    if abs(_diff) > 0:
        max_key = max(score_weight, key=score_weight.get)
        score_weight[max_key] = round(score_weight[max_key] + _diff, 4)


# ============================================================
# 🆕 市场状态自适应权重偏移 (regime-adaptive weight shifts)
# 在 weight_adjuster 的 IC 基础权重之上，叠加市场状态微调
# 震荡市 → 提技术面/价格位置，降动量/情绪（择股重于择时）
# 牛市   → 提动量/情绪/因子，降护城河/基本面（趋势为王）
# 熊市   → 提护城河/基本面，降动量/情绪（防御优先）
# ============================================================
REGIME_WEIGHT_SHIFTS = {
    "牛市":     {"momentum": +0.06, "sentiment": +0.04, "factor": +0.02, "moat": -0.04, "base": -0.04, "zone": -0.04},
    "熊市":     {"moat": +0.08, "base": +0.04, "zone": -0.02, "momentum": -0.06, "sentiment": -0.04},
    "震荡市":   {"technical": +0.04, "zone": +0.04, "momentum": -0.02, "sentiment": -0.02, "moat": -0.04},
    "结构性牛市": {"momentum": +0.04, "sentiment": +0.03, "moat": -0.03, "base": -0.02, "zone": -0.02},
}


def _apply_regime_shifts(weights: dict, regime_label: str) -> dict:
    """叠加市场状态偏移，归一化后返回新权重"""
    shifts = REGIME_WEIGHT_SHIFTS.get(regime_label, {})
    if not shifts:
        return weights
    shifted = dict(weights)
    for dim, delta in shifts.items():
        shifted[dim] = shifted.get(dim, 0.1) + delta
    # 保底：不允许任何维度 ≤ 0.02
    for dim in shifted:
        shifted[dim] = max(shifted[dim], 0.02)
    # 归一化
    total = sum(shifted.values())
    for dim in shifted:
        shifted[dim] = round(shifted[dim] / total, 4)
    return shifted


# 自动感知市场状态并应用偏移
_active_regime = "震荡市"
try:
    if MARKET_SENSE_AVAILABLE:
        _ms = MarketSense()
        _active_regime = _ms.get_market_regime().get("regime_label", "震荡市")
except Exception:
    pass
score_weight = _apply_regime_shifts(score_weight, _active_regime)
log.info("市场状态: %s → 权重已自适应调整", _active_regime)

# ============================================================
# 🆕 操作模式感知：均值回归 vs 趋势跟踪
# 当市场处于下跌通道(-3%+)时，自动翻转负IC因子
# 数据支撑: volume(-0.154), technical(-0.141), momentum(-0.134)均为负IC
# ============================================================
_OPERATIONAL_MODE = None
_SELL_TRIGGER_WEIGHT = 1.0
_BUY_THRESHOLD_SHIFT = 0

# IC维度→评分变量映射（用于均值回归因子翻转）
_IC_TO_SCORE_KEY = {
    "momentum_score": "momentum",
    "volume_score": "volume",
    "technical_score": "technical",
    "base_score": "base",
    "factor_score": "factor",
    "serenity_score": "serenity",
    "moat_score": "moat",
    "zone_score": "zone",
}
_CACHED_INVERT_DIMS = None  # 缓存IC驱动的待翻转因子集合

def _get_invert_score_keys() -> set:
    """返回应永久翻转的因子评分键集合（基于mean_IC数据，非硬编码）
    
    策略: 任何因子的 mean_IC < -0.02 即永久翻转其评分方向。
    这不等同于均值回归模式 — 均值回归模式额外调整信号门槛和止盈参数。
    每次调用会刷新缓存（评分周期通常一天一次，IC变化不频繁）。
    """
    global _CACHED_INVERT_DIMS
    if _CACHED_INVERT_DIMS is not None:
        return _CACHED_INVERT_DIMS
    
    neg_dims = set()
    try:
        # 1. 优先从缓存的 adjusted_weights 中读取 mean_ic
        import json, os
        aw_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".adjusted_weights.json")
        if os.path.exists(aw_path):
            with open(aw_path) as f:
                aw = json.load(f)
            mean_ic = aw.get("source_ic", {}).get("mean_ic", {})
        else:
            from factor_ic import compute_rank_ic
            ic_data = compute_rank_ic(days=30, window=14)
            mean_ic = ic_data.get("mean_ic", {})
        
        # 2. 也纳入 scoring_history 直接计算的 IC（补充 sentiment 等无缓存维度）
        try:
            from factor_ic import compute_rank_ic as _ic_direct
            _direct = _ic_direct(days=21, window=14)
            _direct_mean = _direct.get("mean_ic", {})
            for k, v in _direct_mean.items():
                if k not in mean_ic:
                    mean_ic[k] = v
        except Exception:
            pass
        
        for ic_key, score_key in _IC_TO_SCORE_KEY.items():
            if mean_ic.get(ic_key, 0) < -0.02:
                neg_dims.add(score_key)
    except Exception:
        # 回退：动量/量能/技术面在A股长期负IC(追涨杀跌反效)
        neg_dims = {"momentum", "volume", "technical"}
    
    _CACHED_INVERT_DIMS = neg_dims
    if neg_dims:
        log.info("🔧 因子永久翻转(IC<0): %s", sorted(neg_dims))
    return neg_dims

try:
    if MARKET_SENSE_AVAILABLE:
        _ms2 = MarketSense()
        _OPERATIONAL_MODE = _ms2.get_operational_mode()
        _SELL_TRIGGER_WEIGHT = _OPERATIONAL_MODE.get("sell_trigger_weight", 1.0)
        _BUY_THRESHOLD_SHIFT = _OPERATIONAL_MODE.get("buy_threshold_shift", 0)
        if _OPERATIONAL_MODE.get("factor_invert"):
            log.warning("⚠️ 均值回归模式激活 — 负IC因子(动量/量能/技术面)将自动翻转")
except Exception:
    pass


def get_operational_mode() -> dict:
    """返回当前操作模式，供 signal_engine 等模块调用"""
    if _OPERATIONAL_MODE:
        return _OPERATIONAL_MODE
    return {"mode": "neutral", "factor_invert": False, "sell_trigger_weight": 1.0, "buy_threshold_shift": 0}


def apply_factor_inversion(factor_scores: dict) -> dict:
    """永久翻转负IC因子的得分方向（IC数据驱动，不限市场状态）
    
    只翻转 mean_IC < -0.02 的因子，而非硬编码。
    动量高 → 已涨多 → 应减分 (翻转: 100-动量分)
    量能高 → 放量跌 → 应减分 (翻转: 100-量能分)  
    技术面高 → 死叉前高 → 应减分 (翻转: 100-技术分)
    
    注意: 此函数不检查操作模式。均值回归模式额外调整买入门槛和止盈参数。
    """
    invert_keys = _get_invert_score_keys()
    if not invert_keys:
        return factor_scores
    inverted = dict(factor_scores)
    for key in invert_keys:
        if key in inverted and inverted[key] is not None:
            inverted[key] = max(0, min(100, 100 - inverted[key]))
    return inverted


def compute_mean_reversion_score(rsi: float, in_mr_mode: bool = False) -> tuple:
    """均值回归评分 — 对标回测引擎 MeanReversionStrategy (+22%~+62%)
    
    逻辑: RSI 超卖→高分(买点)，RSI 超买→低分(卖点)
    与 backtest_engine.MeanReversionStrategy 保持一致:
      signal = (40 - RSI) / 15  (RSI<40)
      signal = -(RSI - 75) / 25 (RSI>75)
      其他区间 signal = 0
    
    Returns (score_0_100, signal_strength, label)
    """
    if rsi is None or (isinstance(rsi, float) and rsi != rsi):
        return 50.0, 0.0, "neutral"
    
    if rsi < 40:
        signal = (40 - rsi) / 15  # 0~2.67, 超卖→买点
        if rsi < 20:
            label = "deep_oversold"
        elif rsi < 30:
            label = "oversold"
        else:
            label = "mild_oversold"
    elif rsi > 75:
        signal = -(rsi - 75) / 25  # 0~-1.0, 超买→卖点
        if rsi > 85:
            label = "deep_overbought"
        else:
            label = "overbought"
    else:
        signal = 0.0
        label = "neutral"
    
    # 映射到 0-100: signal ∈ [-1, 2.67] → score ∈ [0, 100]
    score = max(0, min(100, 50 + signal * 20))
    return score, signal, label


def compute_zone_score(price: float, detail: dict) -> tuple:
    zone_low = detail.get("buy_zone_low", 0)
    zone_high = detail.get("buy_zone_high", 0)
    target = detail.get("target_sell", 0)

    if target > 0 and price >= target:
        return 20, "已达目标", "done"
    elif zone_low > 0 and zone_high > 0 and zone_low <= price <= zone_high:
        ratio = (price - zone_low) / (zone_high - zone_low) if zone_high > zone_low else 0.5
        score = 100 - (ratio * 40)
        return score, "买入区 ✓", ""
    elif zone_low > 0 and price < zone_low:
        discount = ((zone_low - price) / zone_low * 100)
        if discount <= 10:
            return 90, f"低于买入区 {discount:.0f}%", "below"
        else:
            return 95, f"深度折扣 {discount:.0f}%", "below"
    else:
        if target > 0:
            progress = ((price - zone_high) / (target - zone_high) * 100) if target > zone_high else 50
            score = max(30, 60 - progress * 0.3)
            return score, "高于买入区", "above"
        return 40, "高于买入区", "above"


def compute_momentum_score(change_pct: float, price: float, detail: dict) -> float:
    target = detail.get("target_sell", 0)
    room = ((target - price) / price * 100) if target > 0 and price > 0 else 0

    if change_pct < -3:
        return 60 if room > 20 else 30
    elif change_pct < 0:
        return 85 if room > 15 else 50
    elif change_pct < 2:
        return 75
    elif change_pct < 5:
        return 65 if room > 10 else 40
    else:
        return 50 if room > 15 else 20


def compute_volume_score(code: str, current_volume: float) -> float:
    avg_vol = get_avg_volume(code, 10)
    if avg_vol <= 0:
        return 60
    ratio = current_volume / avg_vol if avg_vol > 0 else 1
    if 0.8 <= ratio <= 1.5:
        return 80
    elif 0.5 <= ratio < 0.8:
        return 65
    elif ratio > 3:
        return 20
    elif ratio > 1.5:
        return 50
    else:
        return 40


@observe_score_duration
def score_all() -> list[dict]:
    """对所有候选标的进行评分 + 生成交易信号"""
    today = date.today().isoformat()
    snapshots = get_all_today_snapshots()

    # ── 批量并行计算情绪分（14 个 HTTP → 并行加速） ──
    try:
        from sentiment_engine import compute_sentiment_scores_batch
        codes_batch = [s["code"] for s in snapshots]
        batch_sentiment = compute_sentiment_scores_batch(codes_batch)
    except Exception:
        batch_sentiment = {}

    # 市场风格感知
    _label = "震荡市"
    _market_summary = ""
    if MARKET_SENSE_AVAILABLE:
        try:
            ms = MarketSense()
            _regime = ms.get_market_regime()
            _label = _regime.get('regime_label', '震荡市')
            _market_summary = ms.generate_summary()
        except Exception:
            pass

    # 生成信号
    from portfolio import get_portfolio
    portfolio = get_portfolio()
    # 先做快速评分 pass（无 DB 写入），用于传递给 signal_engine
    # 避免 signal_engine 的 6 维权重自行计算导致评分不一致
    _quick_scores = {}
    for snap in snapshots:
        code = snap["code"]
        price = snap["close"]
        change_pct = snap["change_pct"]
        detail = STOCK_DETAILS.get(code, {})
        # 快速估算 10 维总分（与下方完整计算逻辑一致）
        from config import TIER_4_CODES, compute_serenity_score, compute_serenity_score_compensated
        _is_def = code in TIER_4_CODES
        _is_def_mkt = "震荡" in _label or "弱势" in _label or "熊" in _label
        _ser = compute_serenity_score_compensated(code) if (_is_def and _is_def_mkt) else compute_serenity_score(code)
        try:
            _moat = compute_moat_score(code)["moat_score"]
        except Exception:
            _moat = 50
        # 🔧 quick pass 也永久翻转负IC因子
        _qmom = compute_momentum_score(change_pct, price, detail)
        _qvol = compute_volume_score(code, snap.get("volume", 0) or 0)
        _qinv = _get_invert_score_keys()
        if "momentum" in _qinv:
            _qmom = max(0, min(100, 100 - _qmom))
        if "volume" in _qinv:
            _qvol = max(0, min(100, 100 - _qvol))
        _qs_total = (
            detail.get("score", 50) * score_weight["base"] +
            compute_zone_score(price, detail)[0] * score_weight["zone"] +
            _qmom * score_weight["momentum"] +
            _qvol * score_weight["volume"] +
            _ser * score_weight["serenity"] +
            50 * score_weight["factor"] +         # factor=50 fallback for quick pass
            50 * score_weight["technical"] +      # technical=50 fallback
            50 * score_weight["sentiment"] +      # sentiment=50 fallback
            _moat * score_weight["moat"] +
            50 * score_weight.get("guru_wisdom", 0.04)  # guru=50 fallback
        )
        _quick_scores[code] = round(_qs_total, 1)
    
    all_signals = generate_signals(portfolio=portfolio, scorer_total_scores=_quick_scores)
    signal_map = {s["code"]: s for s in all_signals}

    results = []
    for snap in snapshots:
        code = snap["code"]
        detail = STOCK_DETAILS.get(code, {})
        price = snap["close"]
        change_pct = snap["change_pct"]
        volume = snap.get("volume", 0) or 0

        # 五大因子评分
        base_score = detail.get("score", 50)
        zone_score, zone_label, zone_class = compute_zone_score(price, detail)
        momentum_score = compute_momentum_score(change_pct, price, detail)
        volume_score = compute_volume_score(code, volume)

        # Serenity 匹配分 — T4 防御组合在弱势/震荡市场使用补偿权重
        # 解决传统蓝筹因旧五维偏重 CPO/AI 导致的系统性低分
        from config import TIER_4_CODES, compute_serenity_score_compensated
        is_defensive = code in TIER_4_CODES
        is_defensive_market = "震荡" in _label or "弱势" in _label or "熊" in _label
        if is_defensive and is_defensive_market:
            serenity_score = compute_serenity_score_compensated(code)
        else:
            serenity_score = compute_serenity_score(code)

        # 护城河因子评分 (v2.0 新增 — 基于巴菲特框架的量化实现)
        try:
            moat_result = compute_moat_score(code)
            moat_score = moat_result["moat_score"]
        except Exception as e:
            log.warning(f"[scorer] {code}: moat_factor 计算异常: {e}")
            moat_score = 50.0
            moat_result = {"moat_score": 50, "data_available": False, "signals": ["护城河因子计算异常"]}

        # 因子引擎评分
        factor_score = 50
        factor_details = {}
        multi_cycle_score = 50
        cycle_factors_raw = {}
        if FACTOR_ENGINE_AVAILABLE:
            try:
                all_factors = FACTOR_ENGINE.compute_all_factors(code)
                signals = all_factors.get("signals", {})
                signal_sum = sum(float(v) for v in signals.values() if v is not None)
                factor_score = max(0, min(100, 50 + signal_sum * 2))
                factor_details = {"signals": signals, "descriptive": all_factors.get("descriptive", {})}

                # 🅱 多周期因子融合
                try:
                    mcf = FACTOR_ENGINE.compute_multi_cycle_factors(code)
                    daily_sig = mcf.get("daily", {})
                    weekly_sig = mcf.get("weekly", {})
                    monthly_sig = mcf.get("monthly", {})

                    def _avg_signal(d):
                        vals = [float(v) for v in d.values() if v is not None]
                        return sum(vals) / len(vals) if vals else 0.0

                    avg_daily = _avg_signal(daily_sig)
                    avg_weekly = _avg_signal(weekly_sig)
                    avg_monthly = _avg_signal(monthly_sig)

                    # 三周期加权：daily 40% + weekly 40% + monthly 20%
                    fused = avg_daily * 0.40 + avg_weekly * 0.40 + avg_monthly * 0.20
                    # 映射到 0-100
                    multi_cycle_score = max(0, min(100, 50 + fused * 2))
                    
                    # 🆕 周期一致性加分：日/周/月同向 → 强确认
                    _sign = lambda x: 1 if x > 0.01 else (-1 if x < -0.01 else 0)
                    _agree = sum(1 for s in [_sign(avg_daily), _sign(avg_weekly), _sign(avg_monthly)] if s == 1)
                    _agree_neg = sum(1 for s in [_sign(avg_daily), _sign(avg_weekly), _sign(avg_monthly)] if s == -1)
                    _agreement = max(_agree, _agree_neg)
                    if _agreement >= 3:
                        cycle_bonus = 8   # 三周期共振
                    elif _agreement >= 2:
                        cycle_bonus = 3   # 两周期同向
                    else:
                        cycle_bonus = 0   # 周期分歧
                    multi_cycle_score = max(0, min(100, multi_cycle_score + cycle_bonus))

                    cycle_factors_raw = {
                        "daily": daily_sig,
                        "weekly": weekly_sig,
                        "monthly": monthly_sig,
                        "agreement": _agreement,
                        "cycle_bonus": cycle_bonus,
                    }
                except Exception:
                    multi_cycle_score = factor_score
                    cycle_factors_raw = {}
            except Exception:
                factor_score = 50

        # 🆕 技术面评分
        tech = compute_technical_factors(code)
        technical_score = compute_trend_score(tech)
        
        # 🆕 均值回归评分（对标回测引擎 MeanReversionStrategy）
        rsi_val = tech.get("rsi", 50)
        mr_score, mr_signal_strength, mr_label = compute_mean_reversion_score(
            rsi_val, _OPERATIONAL_MODE.get("factor_invert", False) if _OPERATIONAL_MODE else False
        )

        # 🆕 新闻情绪评分（优先用批量并行结果，带 fallback）
        if batch_sentiment and code in batch_sentiment:
            sentiment_score = batch_sentiment[code]
        else:
            try:
                sentiment_score = compute_sentiment_score(code)
            except Exception:
                sentiment_score = 50.0

        # 🆕 大师智慧因子（段永平/巴菲特/芒格/但斌等13位大师对个股的情绪信号）
        try:
            from guru_wisdom import get_guru_factor, get_guru_sentiment
            guru_factor_value = get_guru_factor(code)
            guru_score = 50 + guru_factor_value * 500  # [-0.05, +0.05] → [25, 75]
            # 保留原始情绪数据用于简报
            guru_sentiment = get_guru_sentiment(code)
            guru_gurus_count = guru_sentiment.get("gurus_count", 0)
            guru_net_score = guru_sentiment.get("net_score", 0)
        except Exception as e:
            guru_score = 50.0
            guru_gurus_count = 0
            guru_net_score = 0

        # 🔧 永久翻转负IC因子（不限市场状态，IC数据驱动）
        invert_keys = _get_invert_score_keys()
        _flipped = []
        if "momentum" in invert_keys:
            _orig = momentum_score
            momentum_score = max(0, min(100, 100 - momentum_score))
            _flipped.append(f"动量{_orig:.0f}→{momentum_score:.0f}")
        if "volume" in invert_keys:
            _orig = volume_score
            volume_score = max(0, min(100, 100 - volume_score))
            _flipped.append(f"量能{_orig:.0f}→{volume_score:.0f}")
        if "technical" in invert_keys:
            _orig = technical_score
            technical_score = max(0, min(100, 100 - technical_score))
            _flipped.append(f"技术{_orig:.0f}→{technical_score:.0f}")
        # 翻转说明（输出到日志，仅首个标的）
        if code == ALL_CODES[0] and _flipped:
            log.info("🔧 因子翻转: %s", " ".join(_flipped))

        # 加权总分（11维度 → 含均值回归 + 护城河 + 大师智慧）
        # 均值回归维度: MR模式下动态提权(15%), 正常模式低权(5%)
        _mr_weight = 0.15 if (_OPERATIONAL_MODE and _OPERATIONAL_MODE.get("factor_invert")) else 0.05
        # 调整其他权重使总权重保持≈1.0（从base/zone/factor中均摊）
        _adj_scale = (1.0 - _mr_weight) / (1.0 - 0.05)  # 从正常100%压缩到(100-mr_weight)%
        # 🆕 多周期融合维度权重（从低IC维度匀出5%）
        _mc_weight = 0.05
        _adj_scale2 = _adj_scale * (1.0 - _mc_weight)  # 二次压缩
        total = (
            base_score * score_weight["base"] * _adj_scale2 +
            zone_score * score_weight["zone"] * _adj_scale2 +
            momentum_score * score_weight["momentum"] * _adj_scale2 +
            volume_score * score_weight["volume"] * _adj_scale2 +
            serenity_score * score_weight["serenity"] * _adj_scale2 +
            factor_score * score_weight["factor"] * _adj_scale2 +
            technical_score * score_weight["technical"] * _adj_scale2 +
            sentiment_score * score_weight["sentiment"] * _adj_scale2 +
            moat_score * score_weight["moat"] * _adj_scale2 +
            guru_score * score_weight.get("guru_wisdom", 0.04) * _adj_scale2 +
            mr_score * _mr_weight +  # 🆕 均值回归维度
            multi_cycle_score * _mc_weight  # 🆕 多周期共振维度
        )

        # 🆕 信号集成 — 使用 scorer 统一 9 维度评分体系
        signal_info = signal_map.get(code, {})
        buy_confirm = signal_info.get("buy_confirm", {})
        signal_confidence = buy_confirm.get("confidence", 0) if buy_confirm else 0.5

        # 用 scorer 的 8 维加权总分计算信号级别（替代 generate_signals 内部评分）
        held_codes = {p["code"] for p in portfolio.get_portfolio_value().get("position_details", [])}
        is_held = code in held_codes
        if is_held:
            profit_pct = 0
            for p in portfolio.get_portfolio_value().get("position_details", []):
                if p["code"] == code:
                    cost = p.get("buy_price", price)
                    profit_pct = (price - cost) / cost * 100 if cost > 0 else 0
                    break
            signal_action = get_position_signal(total, profit_pct, True)
            # 已达目标价 → 强制卖出
            if zone_class == "done" and price >= detail.get("target_sell", 0):
                signal_action = "SELL"
        else:
            signal_action = get_signal_level(total)
            # 对买入信号做确认检查（复用 generate_signals 的确认结果）
            if signal_action in ("STRONG_BUY", "BUY", "CAUTION_BUY"):
                if not buy_confirm.get("confirmed", True):
                    downgrade = {"STRONG_BUY": "BUY", "BUY": "CAUTION_BUY", "CAUTION_BUY": "HOLD"}
                    signal_action = downgrade.get(signal_action, signal_action)

        # 保存行情
        save_price_history(code, {
            "code": code, "date": today,
            "open": snap.get("open"), "close": price,
            "high": snap.get("high"), "low": snap.get("low"),
            "volume": volume, "change_pct": change_pct,
        })

        # 评分记录
        scores = {
            "date": today,
            "total_score": round(total, 1),
            "base_score": base_score,
            "zone_score": zone_score,
            "momentum_score": momentum_score,
            "volume_score": volume_score,
            "serenity_score": serenity_score,
            "factor_score": round(factor_score, 1),
            "technical_score": round(technical_score, 1),  # 🆕
            "mr_score": round(mr_score, 1),                # 🆕 均值回归
            "sentiment_score": round(sentiment_score, 1),  # 🆕 情绪
            "moat_score": moat_score,                      # v2.0 护城河因子
            "guru_score": round(guru_score, 1),            # 🆕 大师智慧因子
            "multi_cycle_factor": round(multi_cycle_score, 1),  # 🅱 三周期融合分
            "cycle_factors": cycle_factors_raw,  # 🅱 三周期原始值
            "details": {
                "price": price,
                "change_pct": change_pct,
                "volume": volume,
                "zone_label": zone_label,
                "target_sell": detail.get("target_sell", 0),
                "growth": round(((detail.get("target_sell", 0) - price) / price * 100), 1) if price > 0 and detail.get("target_sell", 0) > 0 else 0,
                "signal_action": signal_action,
                "signal_confidence": signal_confidence,
                "tech_ma5": tech.get("ma5", 0),
                "tech_ma20": tech.get("ma20", 0),
                "tech_rsi": tech.get("rsi", 50),
                "tech_bb_pos": tech.get("bb_position", 50),
                "mr_signal": mr_signal_strength,  # 🆕 均值回归信号强度
                "mr_label": mr_label,              # 🆕 均值回归标签
                "factor_signals": factor_details.get("signals", {}),
            }
        }
        save_score_history(code, scores)

        # P5: 实时回写评分到 stocks 表
        try:
            from db import get_conn as _get_conn
            _conn = _get_conn()
            _conn.execute("UPDATE stocks SET score = ? WHERE code = ?", (round(total, 1), code))
            _conn.commit()
        except Exception:
            pass

        result = {
            "code": code,
            "name": STOCK_MAP.get(code, {}).get("name", code),
            "total_score": round(total, 1),
            "base_score": base_score,
            "zone_score": round(zone_score, 1),
            "momentum_score": round(momentum_score, 1),
            "volume_score": round(volume_score, 1),
            "serenity_score": serenity_score,
            "factor_score": round(factor_score, 1),
            "technical_score": round(technical_score, 1),
            "sentiment_score": round(sentiment_score, 1),  # 🆕 情绪
            "moat_score": round(moat_score, 1),            # v2.0 护城河因子
            "guru_score": round(guru_score, 1),            # 🆕 大师智慧因子
            "guru_net_score": round(guru_net_score, 3),    # 🆕 大师净情绪
            "guru_gurus_count": guru_gurus_count,          # 🆕 提及大师数
            "multi_cycle_factor": round(multi_cycle_score, 1),  # 🅱 三周期融合分
            "cycle_factors": cycle_factors_raw,  # 🅱 三周期原始值
            "zone_label": zone_label,
            "close": price,
            "change_pct": change_pct,
            "signal_action": signal_action,
            "signal_confidence": signal_confidence,
            "strategy_tag": "quant",
            "market_regime": _label,
            "market_summary": _market_summary,
        }
        results.append(result)

    # 排序
    results.sort(key=lambda x: x["total_score"], reverse=True)
    for i, r in enumerate(results):
        r["rank"] = i + 1

    # 🆕 反思学习环：保存评分预测
    try:
        from reflection_engine import generate_reflection
        from db import save_reflection
        for r in results:
            ref = generate_reflection(r["code"])
            if "error" not in ref:
                ref["date"] = today
                total = r.get("total_score", 50)
                if total >= 72:
                    ref["predicted_direction"] = "BUY"
                elif total < 42:
                    ref["predicted_direction"] = "SELL"
                else:
                    ref["predicted_direction"] = "HOLD"
                save_reflection(r["code"], ref)
    except Exception:
        pass  # 反思失败不影响主评分

    # ── 更新 Prometheus 指标 ──
    if METRICS_AVAILABLE:
        try:
            SCORE_COUNT.inc(len(results))
            for r in results:
                SIGNAL_ACTIONS.labels(action=r.get("signal_action", "HOLD")).inc()
        except Exception:
            pass

    return results


if __name__ == "__main__":
    results = score_all()
    print(f"📊 Serenity 多因子评分 + 信号 | {date.today()}")
    print("=" * 70)
    for r in results:
        signal_icon = {"STRONG_BUY": "🟢🟢🟢", "BUY": "🟢🟢", "CAUTION_BUY": "🟢",
                       "HOLD": "⚪", "WATCH": "🟡", "SELL": "🔴🔴", "STOP_LOSS": "🔴🔴🔴", "ERROR": "❌"}.get(
            r.get("signal_action", ""), "⚪")
        print(f"#{r['rank']} {r['name']:6s} | 总分 {r['total_score']:5.1f} | "
              f"{signal_icon} {r['signal_action']:<12} | "
              f"技{r['technical_score']:.0f} 因{r['factor_score']:.0f} 位{r['zone_score']:.0f} | "
              f"{r['zone_label']}")
    print("=" * 70)
