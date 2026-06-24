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
from uzi_insight import evaluate_uzi_insight

log = get_logger(__name__)

# v3.0: UZI 不再作为评分维度，仅用于看板信息展示
_UZI_INFO_ONLY = True

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
        "zone": 0.20,        # 价格位置（v3.0: 动态60日通道）
        "momentum": 0.18,     # 动量
        "volume": 0.04,       # 成交量
        "serenity": 0.18,     # Serenity 框架匹配度
        "factor": 0.20,       # 因子引擎（三周期融合）
        "technical": 0.10,    # 技术面 + 情绪
        "moat": 0.10,         # 护城河因子
    }

# v3.0: 7 维核心（移除 base — IC=-0.13 持续14天为负，静态手动评分无预测力）
_SCORE_WEIGHT_DEFAULTS = {
    "zone": 0.20, "momentum": 0.18, "volume": 0.04,
    "serenity": 0.18, "factor": 0.20, "technical": 0.10, "moat": 0.10,
}
for k, v in _SCORE_WEIGHT_DEFAULTS.items():
    score_weight.setdefault(k, v)
# 确保移除 base 键（旧缓存可能有）
score_weight.pop("base", None)
score_weight.pop("guru_wisdom", None)
score_weight.pop("sentiment", None)

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
    "牛市":     {"momentum": +0.06, "factor": +0.04, "moat": -0.06, "zone": -0.04},
    "熊市":     {"moat": +0.10, "momentum": -0.08, "volume": -0.04, "zone": +0.02},
    "震荡市":   {"technical": +0.04, "zone": +0.04, "momentum": -0.04, "moat": -0.04},
    "结构性牛市": {"momentum": +0.04, "factor": +0.02, "moat": -0.04, "zone": -0.02},
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
    """返回应翻转的因子评分键集合（条件翻转，仅均值回归模式）

    🔧 v3.0 重大变更: 因子翻转从"永久"改为"条件"。
    仅在均值回归模式（MarketSense 检测到下跌通道，factor_invert=True）时翻转。
    趋势市和中性市保留原始因子方向，避免强趋势中卖强买弱。

    数据依据:
    - STRONG_BUY(最高信心) 5日胜率仅21.4% — 在趋势市中翻转动量=买弱卖强
    - SELL信号后80%概率上涨 — 翻转技术面=在局部底部卖出
    - 但均值回归模式(下跌通道)下翻转仍有效 — MeanReversionStrategy 回测显著优于趋势策略

    每次调用检查当前操作模式，不使用持久缓存。
    """
    # 仅在均值回归模式时翻转因子
    if not _OPERATIONAL_MODE or not _OPERATIONAL_MODE.get("factor_invert"):
        return set()

    # MR 模式：从 IC 缓存读取负 IC 因子
    neg_dims = set()
    try:
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
        # 回退：MR模式下默认翻转动量/量能/技术面
        neg_dims = {"momentum", "volume", "technical"}

    if neg_dims:
        log.info("🔧 [MR模式] 因子翻转激活: %s", sorted(neg_dims))
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
    """条件翻转负IC因子的得分方向（仅均值回归模式）

    🔧 v3.0: 仅在 MarketSense 检测到均值回归模式(factor_invert=True)时翻转。
    趋势市/中性市保留原始因子方向。

    翻转逻辑: 100 - 原始分（动量高=已涨多→应减分, 量能高=放量跌→应减分）
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


def compute_zone_score(price: float, detail: dict, code: str = None) -> tuple:
    """v3.0 动态价格位置评分 — 基于近期价格区间

    旧版问题: 纯静态买入区配置（月前手写），zone_score IC=-0.19
    新版逻辑: 引入60日价格通道作为动态参照
      - 动态区: 价格在60日低点附近 → 折扣机会
      - 静态区: 价格在买入区内 → 兼顾配置
      - 过热区: 价格逼近60日高点 → 风险提示

    返回: (score_0_100, label, class)
    """
    zone_low = detail.get("buy_zone_low", 0)
    zone_high = detail.get("buy_zone_high", 0)
    target = detail.get("target_sell", 0)

    # 获取60日价格通道（动态参照）
    dynamic_low = 0
    dynamic_high = 0
    if code:
        try:
            from db import get_price_history
            rows = get_price_history(code, 60)
            if len(rows) >= 20:
                closes = [r["close"] for r in rows]
                dynamic_low = min(closes)
                dynamic_high = max(closes)
        except Exception:
            pass

    has_dynamic = dynamic_high > dynamic_low and dynamic_low > 0

    # 已达目标价 → 强烈卖出信号
    if target > 0 and price >= target:
        return 20, "已达目标", "done"

    # 动态区：仅在有真实历史数据时启用
    if has_dynamic:
        dynamic_range = dynamic_high - dynamic_low

        # 动态深度折扣：价格接近60日低点 → 高分
        if price <= dynamic_low * 1.05:
            discount_pct = (dynamic_low - price) / dynamic_low * 100 if dynamic_low > 0 else 0
            return min(95, 80 + max(0, discount_pct)), "接近60日低点", "dynamic_low"

        # 动态折扣：价格在60日区间低位30%以内
        price_position = (price - dynamic_low) / dynamic_range
        if price_position < 0.30:
            return 75, "动态折扣区间", "dynamic_discount"

        # 动态高位：接近60日高点
        if price_position > 0.85:
            return 30, "接近60日高点", "dynamic_high"
        elif price_position > 0.60:
            return 45, "价格偏高", "above_high"

    # 静态买入区（兼顾配置）
    if zone_low > 0 and zone_high > 0 and zone_low <= price <= zone_high:
        ratio = (price - zone_low) / (zone_high - zone_low)
        return 85 - (ratio * 20), "买入区 ✓", ""

    # 低于买入区 → 折扣
    if zone_low > 0 and price < zone_low:
        discount = ((zone_low - price) / zone_low * 100)
        if discount <= 10:
            return 88, f"低于买入区 {discount:.0f}%", "below"
        else:
            return 92, f"深度折扣 {discount:.0f}%", "below"

    # 高于买入区
    if target > 0:
        progress = ((price - zone_high) / (target - zone_high) * 100) if target > zone_high else 50
        return max(30, 60 - progress * 0.3), "高于买入区", "above"
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
        # 🔧 v3.0 条件翻转（仅MR模式）
        _qmom = compute_momentum_score(change_pct, price, detail)
        _qvol = compute_volume_score(code, snap.get("volume", 0) or 0)
        _qinv = _get_invert_score_keys()
        if "momentum" in _qinv:
            _qmom = max(0, min(100, 100 - _qmom))
        if "volume" in _qinv:
            _qvol = max(0, min(100, 100 - _qvol))
        # v3.0: 7维简化评分（移除 base, guru_wisdom, sentiment独立, mean_reversion, multi_cycle, UZI附加层）
        _qs_total = (
            compute_zone_score(price, detail, code)[0] * score_weight.get("zone", 0.20) +
            _qmom * score_weight.get("momentum", 0.18) +
            _qvol * score_weight.get("volume", 0.04) +
            _ser * score_weight.get("serenity", 0.18) +
            50 * score_weight.get("factor", 0.20) +
            50 * score_weight.get("technical", 0.10) +
            _moat * score_weight.get("moat", 0.10)
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
        zone_score, zone_label, zone_class = compute_zone_score(price, detail, code)
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

        # 📊 大师智慧因子（v3.0: 仅信息展示，不计入评分）
        # 大多数股票无大师提及（guru_score=50常数），作为评分因子无区分力
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

        try:
            uzi_result = evaluate_uzi_insight(
                code,
                snapshot=snap,
                detail=detail,
                moat_result=moat_result,
                serenity_score=serenity_score,
                sentiment_score=sentiment_score,
            )
        except Exception as e:
            log.warning(f"[scorer] {code}: UZI insight 计算异常: {e}")
            uzi_result = {
                "uzi_score": 50.0,
                "rating": "unknown",
                "verdict": "Watch",
                "ai_chain_hit": False,
                "ai_chain_keywords": [],
                "ai_chain_tier": "未知",
                "evidence_grade": "unknown",
                "evidence_ledger": {"total": 0, "counts": {}, "titles": []},
                "trap_signals": [],
                "penalty_total": 0.0,
                "reasons": ["UZI insight 计算异常"],
            }

        # v3.0 7维简化加权总分（移除 base: IC=-0.13）
        # sentiment 合并入 technical（80% 技术面 + 20% 情绪）
        _merged_technical = technical_score * 0.80 + sentiment_score * 0.20
        total = (
            zone_score * score_weight.get("zone", 0.20) +
            momentum_score * score_weight.get("momentum", 0.18) +
            volume_score * score_weight.get("volume", 0.04) +
            serenity_score * score_weight.get("serenity", 0.18) +
            factor_score * score_weight.get("factor", 0.20) +
            _merged_technical * score_weight.get("technical", 0.10) +
            moat_score * score_weight.get("moat", 0.10)
        )
        total = round(total, 1)

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
            "uzi_score": round(uzi_result["uzi_score"], 1), # UZI卡位/证据层
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
                "uzi_insight": uzi_result,
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
            "uzi_score": round(uzi_result["uzi_score"], 1),
            "uzi_rating": uzi_result["rating"],
            "uzi_verdict": uzi_result["verdict"],
            "uzi_evidence": uzi_result["evidence_grade"],
            "uzi_penalty_total": uzi_result["penalty_total"],
            "uzi_trap_count": len(uzi_result.get("trap_signals", [])),
            "uzi_chain_tier": uzi_result["ai_chain_tier"],
            "uzi_reasons": uzi_result["reasons"],
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
