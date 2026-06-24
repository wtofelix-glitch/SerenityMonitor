"""
统一信号引擎 — 融合多因子评分 + Alpha 因子 + 技术分析 + 价格行为
输出明确的 BUY / SELL / HOLD / STOP 信号
"""
from datetime import date, datetime
from typing import Optional, TYPE_CHECKING
import numpy as np

from config import (
    SIGNAL_CONFIG, STOCK_MAP, STOCK_DETAILS, ALL_CODES,
    compute_serenity_score, CAPITAL_CONFIG, RISK_CONFIG, STRATEGY_CONFIG,
)
from data_engine import fetch_realtime, fetch_single
from db import get_price_history
from factor_engine import AlphaFactorEngine
try:
    from fundamental_engine import FundamentalEngine
    _FUNDAMENTAL_AVAILABLE = True
except ImportError:
    _FUNDAMENTAL_AVAILABLE = False
    FundamentalEngine = None
if TYPE_CHECKING:
    from portfolio import PortfolioManager
from serenity_logger import get_logger

log = get_logger(__name__)

# ============================================================
# Conviction 动态调参引擎
# ============================================================
# 缓存避免每次调用从DB读
_conviction_thresholds_cache = {"regime": None, "thresholds": {}, "date": None}

def _fetch_conviction_thresholds() -> dict:
    """从 conviction_log 获取最新的辩论结果，返回动态阈值修正量
    
    Returns:
        dict with regime, buy_adjust (买入门槛偏移), sell_adjust (卖出门槛偏移)
    """
    global _conviction_thresholds_cache
    from datetime import date
    today = date.today().isoformat()
    
    # 缓存命中（同一天）
    if _conviction_thresholds_cache.get("date") == today:
        return _conviction_thresholds_cache["thresholds"]
    
    try:
        from db import get_latest_conviction
        cv = get_latest_conviction()
    except Exception:
        cv = None
    
    if not cv or cv.get("date") != today:
        # 没有今日的辩论数据 → 使用默认
        _conviction_thresholds_cache = {
            "date": today,
            "thresholds": {"regime": "震荡", "buy_adjust": 0, "sell_adjust": 0},
            "regime": "震荡",
        }
        return _conviction_thresholds_cache["thresholds"]
    
    regime = cv.get("regime", "震荡")
    weights = cv.get("debated_weights", {})
    m = weights.get("moat", 0.10)
    momentum = weights.get("momentum", 0.12)
    technical = weights.get("technical", 0.10)

    # 买入门槛调整：弱势→收紧（+5），强势→放宽（-5），震荡→微调
    # 再根据防守 vs 进攻因子比例二次修正
    defense_ratio = (m + weights.get("base", 0.12)) / max(momentum, 0.01)
    
    if regime == "弱势":
        base_buy = 5  # +5: 更严
        base_sell = -3  # -3: 卖出门槛更敏感（提前卖出）
        if defense_ratio > 1.5:
            base_buy += 3  # 防御偏好 → 更严
    elif regime == "强势":
        base_buy = -5  # -5: 放宽买入
        base_sell = 3  # +3: 卖出更保守
        if momentum > 0.18:
            base_buy -= 2  # 动量强 → 再放宽
    else:  # 震荡
        # 震荡市场：根据防守vs进攻偏向来微调
        if defense_ratio > 1.2:
            base_buy = 2  # 防御偏好 → 稍严
            base_sell = -1
        else:
            base_buy = -2  # 均衡/进攻偏好 → 稍宽
            base_sell = 1
    
    result = {
        "regime": regime,
        "buy_adjust": base_buy,
        "sell_adjust": base_sell,
        "defense_ratio": round(defense_ratio, 2),
        "moat_weight": m,
        "momentum_weight": momentum,
    }
    
    _conviction_thresholds_cache = {
        "date": today,
        "thresholds": result,
        "regime": regime,
    }
    return result


def _apply_conviction_to_signal_config() -> dict:
    """生成一份被 conviction 调整后的 SIGNAL_CONFIG 副本"""
    import copy
    cfg = copy.deepcopy(SIGNAL_CONFIG)
    cv = _fetch_conviction_thresholds()
    
    regime = cv["regime"]
    buy_adjust = cv["buy_adjust"]
    sell_adjust = cv["sell_adjust"]
    
    # 🆕 翻倍目标：激增模式额外调整
    from config import CAPITAL_CONFIG
    if CAPITAL_CONFIG.get("aggressive_mode"):
        buy_adjust -= 5      # 更容易买入 (buy_threshold 63→58)
        sell_adjust -= 7     # 更难触发卖出 (sell_threshold 45→38)
        # 兜底：conviction极端场景不抵消激增偏移
        # 弱势regime可能把buy推高到66 → clamp到61
        # 强势regime可能把sell推高到41 → clamp到42
        buy_adjust = min(buy_adjust, -2)    # buy_threshold ≤ 61
        sell_adjust = min(sell_adjust, -5)  # sell_threshold ≤ 40
    
    # 调整买入/卖出阈值
    cfg["buy_threshold"] = max(50, min(85, SIGNAL_CONFIG["buy_threshold"] + buy_adjust))
    cfg["strong_buy_threshold"] = max(60, min(90, SIGNAL_CONFIG["strong_buy_threshold"] + buy_adjust))
    cfg["sell_threshold"] = max(25, min(55, SIGNAL_CONFIG["sell_threshold"] + sell_adjust))
    cfg["hold_low"] = max(35, min(60, SIGNAL_CONFIG["hold_low"] + sell_adjust))
    cfg["pos_exit_threshold"] = max(35, min(60, SIGNAL_CONFIG["pos_exit_threshold"] + sell_adjust))
    
    # 震荡市场特有：hold_high 也微调
    if regime == "震荡":
        if buy_adjust < 0:  # 稍宽
            cfg["hold_high"] = max(55, SIGNAL_CONFIG["hold_high"] + buy_adjust)
    
    cfg["_conviction_regime"] = regime
    cfg["_conviction_adjust"] = cv
    
    return cfg

# ============================================================
# 信号级别定义
# ============================================================
SIGNAL_LEVELS = {
    "STRONG_BUY":   {"score": 85, "icon": "🟢🟢🟢", "desc": "强力买入",      "max_weight": 0.50},
    "BUY":          {"score": 75, "icon": "🟢🟢",   "desc": "买入",          "max_weight": 0.40},
    "CAUTION_BUY":  {"score": 65, "icon": "🟢",     "desc": "谨慎买入",      "max_weight": 0.25},
    "STRONG_HOLD":  {"score": 62, "icon": "🟢⚪",   "desc": "强劲持仓",      "max_weight": 0.0},
    "HOLD":         {"score": 50, "icon": "⚪",     "desc": "持有观望",      "max_weight": 0.0},
    "WEAK_HOLD":    {"score": 45, "icon": "🟡",     "desc": "弱持仓/关注",    "max_weight": 0.0},
    "CONSIDER_ADD": {"score": 60, "icon": "🟢+",    "desc": "可考虑加仓",     "max_weight": 0.15},
    "WATCH":        {"score": 40, "icon": "🟡",     "desc": "关注",          "max_weight": 0.0},
    "TAKE_PROFIT":  {"score": 65, "icon": "🟢💰",   "desc": "止盈提示",      "max_weight": 0.0},
    "SELL":         {"score": 30, "icon": "🔴🔴",   "desc": "卖出",          "max_weight": 0.0},
    "STOP_LOSS":    {"score": 0,  "icon": "🔴🔴🔴", "desc": "止损",          "max_weight": 0.0},
}


def get_signal_level(score: float, conviction_override: dict = None) -> str:
    """根据综合评分返回信号级别（支持 conviction 动态阈值）"""
    if conviction_override:
        # 使用 conviction 调整后的阈值
        target = conviction_override
    else:
        # 尝试从DB读取今日 conviction
        try:
            target = _apply_conviction_to_signal_config()
        except Exception:
            target = SIGNAL_CONFIG
    
    if score >= target["strong_buy_threshold"]:
        return "STRONG_BUY"
    elif score >= target["buy_threshold"]:
        return "BUY"
    elif score >= target["hold_high"]:
        return "CAUTION_BUY"
    elif score >= target["hold_low"]:
        return "HOLD"
    elif score >= target["sell_threshold"]:
        return "WATCH"
    else:
        return "SELL"


def get_position_signal(score: float, profit_pct: float, is_holding: bool,
                        conviction_override: dict = None) -> str:
    """持仓标的具体信号级别
    在 get_signal_level 基础上增加持仓专属级别
    """
    base = get_signal_level(score, conviction_override)

    if not is_holding:
        return base

    # 持仓专属调整
    if profit_pct > 5 and score >= 62:
        return "STRONG_HOLD"
    elif profit_pct > 5 and score >= 55:
        return "CONSIDER_ADD"
    elif score < 48:
        return "WEAK_HOLD"
    elif base in ("BUY", "CAUTION_BUY"):
        return "STRONG_HOLD"
    else:
        return base if base in ("HOLD",) else "HOLD"


# ============================================================
# 因子引擎适配器
# ============================================================

_factor_engine = AlphaFactorEngine(use_db=True)
_fund_engine = FundamentalEngine() if FundamentalEngine is not None else None


def compute_technical_factors(code: str) -> dict:
    """
    计算技术面因子指标
    - MA5/MA20 趋势
    - RSI 超买超卖
    - 布林带位置
    - 成交量变化
    - ATR 波动率
    """
    rows = get_price_history(code, 60)
    if len(rows) < 21:
        return {}

    # 时间正序
    closes = np.array([r["close"] for r in reversed(rows)], dtype=float)
    highs = np.array([r["high"] for r in reversed(rows)], dtype=float)
    lows = np.array([r["low"] for r in reversed(rows)], dtype=float)
    volumes = np.array([r["volume"] for r in reversed(rows)], dtype=float)
    n = len(closes)

    cfg = STRATEGY_CONFIG
    ma_s = cfg["ma_short"]
    ma_l = cfg["ma_long"]
    rsi_p = cfg["rsi_period"]
    bb_p = cfg["bb_period"]
    bb_std = cfg["bb_std"]
    atr_p = cfg["atr_period"]

    result = {}

    # MA 趋势
    if n >= ma_l:
        ma5 = closes[-ma_s:].mean() if n >= ma_s else closes.mean()
        ma20 = closes[-ma_l:].mean()
        result["ma5"] = round(ma5, 2)
        result["ma20"] = round(ma20, 2)
        result["ma5_above_ma20"] = ma5 > ma20
        result["price_above_ma5"] = closes[-1] > ma5
        result["price_above_ma20"] = closes[-1] > ma20

    # RSI
    if n >= rsi_p + 1:
        deltas = np.diff(closes[-(rsi_p + 1):])
        gains = deltas[deltas > 0].sum()
        losses = abs(deltas[deltas < 0].sum())
        avg_gain = gains / rsi_p
        avg_loss = losses / rsi_p
        if avg_loss == 0:
            rsi = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi = 100 - (100 / (1 + rs))
        result["rsi"] = round(rsi, 1)
        result["rsi_oversold"] = rsi <= cfg["rsi_oversold"]
        result["rsi_overbought"] = rsi >= cfg["rsi_overbought"]

    # 布林带
    if n >= bb_p:
        bb_mid = closes[-bb_p:].mean()
        bb_std_val = closes[-bb_p:].std(ddof=1)
        bb_upper = bb_mid + bb_std * bb_std_val
        bb_lower = bb_mid - bb_std * bb_std_val
        last_close = closes[-1]
        result["bb_position"] = round((last_close - bb_lower) / (bb_upper - bb_lower) * 100, 1) if bb_upper > bb_lower else 50
        result["bb_lower"] = round(bb_lower, 2)
        result["bb_upper"] = round(bb_upper, 2)
        result["bb_mid"] = round(bb_mid, 2)
        result["at_bb_lower"] = last_close <= bb_lower * 1.02
        result["at_bb_upper"] = last_close >= bb_upper * 0.98

    # 成交量变化
    if n >= 21:
        vol_ma20 = volumes[-20:].mean()
        vol_ma5 = volumes[-5:].mean()
        latest_vol = volumes[-1]
        result["volume_ratio"] = round(latest_vol / vol_ma20, 2) if vol_ma20 > 0 else 1
        result["volume_surge"] = latest_vol >= vol_ma20 * SIGNAL_CONFIG["volume_surge_ratio"]
        result["volume_dry"] = latest_vol <= vol_ma20 * SIGNAL_CONFIG["volume_dry_ratio"]
        result["volume_trend"] = "increasing" if vol_ma5 > vol_ma20 * 1.2 else ("decreasing" if vol_ma5 < vol_ma20 * 0.8 else "normal")

    # ATR 波动率
    if n >= atr_p + 1:
        true_ranges = []
        for i in range(1, atr_p + 1):
            tr = max(
                highs[-i] - lows[-i],
                abs(highs[-i] - closes[-i-1]),
                abs(lows[-i] - closes[-i-1])
            )
            true_ranges.append(tr)
        atr = np.mean(true_ranges)
        result["atr"] = round(atr, 2)
        result["atr_pct"] = round(atr / closes[-1] * 100, 2) if closes[-1] > 0 else 0

    return result


def get_dynamic_stop_loss(code: str, buy_price: float) -> dict:
    """
    根据 ATR 计算动态止损价

    Parameters
    ----------
    code      : 股票代码
    buy_price : 买入价格

    Returns
    -------
    dict with:
        stop_price : float  # 止损价
        stop_pct   : float  # 止损百分比（负值）
        method     : str    # "atr_dynamic" 或 "fixed"
        atr_pct    : float  # ATR百分比
    """
    if not RISK_CONFIG.get("use_atr_stop", True):
        return {
            "stop_price": round(buy_price * (1 + RISK_CONFIG["stop_loss_pct"]), 2),
            "stop_pct": RISK_CONFIG["stop_loss_pct"],
            "method": "fixed",
            "atr_pct": 0,
        }

    tech = compute_technical_factors(code)
    atr_pct = tech.get("atr_pct", 0)

    if atr_pct <= 0:
        # ATR 数据不足，回退到固定止损
        return {
            "stop_price": round(buy_price * (1 + RISK_CONFIG["stop_loss_pct"]), 2),
            "stop_pct": RISK_CONFIG["stop_loss_pct"],
            "method": "fixed",
            "atr_pct": 0,
        }

    mult = RISK_CONFIG["atr_stop_multiplier"]
    # atr_pct 已经是百分比格式（如 2.5 表示 2.5%），转为小数负值
    dynamic_pct = -(atr_pct * mult / 100)

    # 夹在 min/max 之间
    # min_pct = -0.05（最紧），max_pct = -0.15（最宽）
    # 负值情况下：min > max，所以用 max 做下限，min 做上限
    dynamic_pct = max(
        RISK_CONFIG["atr_stop_max_pct"],
        min(RISK_CONFIG["atr_stop_min_pct"], dynamic_pct)
    )

    return {
        "stop_price": round(buy_price * (1 + dynamic_pct), 2),
        "stop_pct": round(dynamic_pct, 4),
        "method": "atr_dynamic",
        "atr_pct": round(atr_pct, 2),
    }


def compute_trend_score(tech: dict) -> float:
    """技术面评分 0-100"""
    score = 50.0  # 中性

    if not tech:
        return score

    # 均线趋势 (+-15)
    if tech.get("ma5_above_ma20"):
        score += 10
        if tech.get("price_above_ma5"):
            score += 5
    else:
        score -= 10
        if not tech.get("price_above_ma20"):
            score -= 5

    # RSI (+-10)
    rsi = tech.get("rsi", 50)
    if 40 <= rsi <= 60:
        score += 5  # 中性偏强
    elif 30 <= rsi < 40:
        score += 8  # 超卖反弹机会
    elif rsi < 30:
        score += 5  # 深度超卖
    elif 60 < rsi <= 70:
        score -= 3  # 接近超买
    elif rsi > 70:
        score -= 8  # 超买

    # 布林带 (+-10)
    bb_pos = tech.get("bb_position", 50)
    if bb_pos < 20:
        score += 10  # 下轨附近，买入机会
    elif bb_pos > 80:
        score -= 10  # 上轨附近，卖出风险
    elif bb_pos < 40:
        score += 5
    elif bb_pos > 60:
        score -= 5

    # 成交量 (+-5)
    vol_ratio = tech.get("volume_ratio", 1)
    if tech.get("volume_surge"):
        score -= 5  # 异常放量警惕
    elif tech.get("volume_dry"):
        score -= 3  # 缩量
    elif 0.8 <= vol_ratio <= 1.5:
        score += 3  # 正常量能

    return max(0, min(100, score))


def compute_multi_factor_score(tech_score: float, serenity_score: float,
                                alpha_signals: dict, zone_info: dict,
                                fund_signal: float = None,
                                fourteen_factor_score: float = None) -> float:
    """
    综合多维度评分 (v2 — 加入14因子独立维度)

    权重分配:
    - 技术面: 20% (原25%)
    - Serenity适配: 20%
    - Alpha因子: 25% (原20%)
    - 14因子独立维度: 10% (新增)
    - 基本面: 15%
    - 价格位置: 15% (原20%)
    """
    # Alpha 因子信号（来自 factor_engine）
    alpha_val = _compute_alpha_composite(alpha_signals)

    # 价格位置: 是否在买入区间
    price = zone_info.get("price", 0)
    buy_low = zone_info.get("buy_zone_low", 0)
    buy_high = zone_info.get("buy_zone_high", 0)
    if buy_low > 0 and buy_high > 0 and price > 0:
        if price < buy_low:
            zone_score = 90  # 低于买入区 = 折扣机会
        elif price <= buy_high:
            zone_score = 75  # 在买入区内
        elif price <= buy_high * 1.15:
            zone_score = 50  # 略高
        elif price <= buy_high * 1.3:
            zone_score = 30  # 偏高
        else:
            zone_score = 15  # 远超
    else:
        zone_score = 50

    # 加权综合
    # 基本面信号: [-1, 1] → [0, 100]
    fund_score = 50 + (fund_signal or 0) * 50 if fund_signal is not None else 50

    total = (
        tech_score * 0.20 +          # 技术面 25%→20%
        serenity_score * 0.20 +      # Serenity 不变
        alpha_val * 0.25 +           # Alpha因子 20%→25%
        fund_score * 0.15 +          # 基本面 不变
        zone_score * 0.15 +          # 价格位置 20%→15%
        fourteen_factor_score * 0.10 # 新增14因子独立维度
    ) if fourteen_factor_score is not None else (
        tech_score * 0.25 +
        serenity_score * 0.20 +
        alpha_val * 0.20 +
        fund_score * 0.15 +
        zone_score * 0.20
    )
    return round(total, 1)


def _compute_alpha_composite(alpha_signals: dict) -> float:
    """Alpha因子综合评分 (0-100) — 放大1.8倍增强信号区分度"""
    if not alpha_signals:
        return 50

    vals = [v for v in alpha_signals.values() if v is not None]
    if not vals:
        return 50

    avg = sum(vals) / len(vals)  # 范围 [-1, 1]
    # 放大1.8倍增强区分度，映射到 [0, 100]
    return round(50 + np.clip(avg * 1.8, -1, 1) * 40, 1)


# ============================================================
# 🆕 卖出触发条件增强
# ============================================================

def compute_sell_triggers(code: str, tech: dict, total_score: float) -> list[dict]:
    """计算额外的卖出触发条件（除评分阈值外）

    A股卖出信号常被忽视，本函数增加技术面卖出触发器，
    让系统在评分尚可但技术面已转弱时提前预警。

    Returns:
        list of {trigger, weight, detail} — 空列表 = 无额外卖出信号
    """
    triggers = []

    # 1. 均线死叉（MA5 < MA20）— 强卖出
    if tech.get("ma5") and tech.get("ma20") and tech["ma5"] <= tech["ma20"]:
        triggers.append({
            "trigger": "ma_death_cross",
            "weight": 2,
            "detail": f"均线死叉 MA5({tech['ma5']:.1f})<MA20({tech['ma20']:.1f})",
        })

    # 2. 价格跌破 MA20 支撑 — 中强卖出
    if not tech.get("price_above_ma20", True) and tech.get("ma5") and tech.get("ma20"):
        triggers.append({
            "trigger": "price_below_ma20",
            "weight": 2,
            "detail": f"价格跌破MA20支撑",
        })

    # 3. 布林带上轨遇阻回落（价格在上轨处但已跌破MA5）
    bb_pos = tech.get("bb_position", 50)
    if bb_pos > 80 and not tech.get("price_above_ma5", True):
        triggers.append({
            "trigger": "bb_upper_rejected",
            "weight": 1,
            "detail": f"上轨遇阻回落(布林位{bb_pos:.0f}%)",
        })

    # 4. RSI 从超买区回落（RSI > 70 且不在超买区了）
    rsi = tech.get("rsi", 50)
    if tech.get("rsi_overbought") is False and rsi < 65:
        # 获取前几日 RSI 判断是否从超买回落
        prev_rsi = _compute_prev_rsi(code)
        if prev_rsi and prev_rsi >= 70:
            triggers.append({
                "trigger": "rsi_overbought_reversal",
                "weight": 2,
                "detail": f"RSI从{prev_rsi:.0f}回落至{rsi:.0f}(超买反转)",
            })

    # 5. 评分连续下降 >= 3 天 — 趋势转弱
    prev_scores = _get_recent_scores(code, 5)
    if len(prev_scores) >= 4:
        # 最近 N 天每天评分都低于前一天
        decline_days = 0
        for i in range(min(3, len(prev_scores) - 1)):
            if prev_scores[i] < prev_scores[i + 1]:
                decline_days += 1
            else:
                break
        if decline_days >= 3:
            triggers.append({
                "trigger": "score_decline_3d",
                "weight": 1,
                "detail": f"评分连降{decline_days}天({prev_scores[0]:.0f}→{prev_scores[-1]:.0f})",
            })

    # 6. 量价背离：缩量上涨（价格 > MA5 但成交量萎缩）
    vol_ratio = tech.get("volume_ratio", 1)
    if tech.get("volume_dry") and tech.get("price_above_ma5"):
        triggers.append({
            "trigger": "volume_price_divergence",
            "weight": 1,
            "detail": f"量价背离(缩量{vol_ratio:.1f}x)",
        })

    # 🛡️ v3.0 超卖保护：RSI<30 或布林下轨时所有触发器降权50%
    rsi = tech.get("rsi", 50)
    bb_pos = tech.get("bb_position", 50)
    if rsi is not None and rsi < 30:
        for t in triggers:
            t["weight"] = max(0, t["weight"] - 1)
        triggers.append({
            "trigger": "oversold_protection",
            "weight": 0,
            "detail": f"RSI={rsi:.0f}超卖保护(所有权重-1)",
        })
    elif bb_pos is not None and bb_pos < 20:
        for t in triggers:
            t["weight"] = max(0, t["weight"] - 1)
        triggers.append({
            "trigger": "bb_low_protection",
            "weight": 0,
            "detail": f"布林下轨{bb_pos:.0f}%保护(所有权重-1)",
        })

    return triggers


def _compute_prev_rsi(code: str, days_back: int = 5) -> Optional[float]:
    """计算 N 天前的 RSI 值"""
    try:
        rows = get_price_history(code, 65)  # 60 + 5
        if len(rows) < 25:
            return None
        closes = np.array([r["close"] for r in reversed(rows)], dtype=float)
        rsi_p = STRATEGY_CONFIG["rsi_period"]
        # 取 days_back 天前的数据计算 RSI
        if len(closes) < rsi_p + 1 + days_back:
            return None
        segment = closes[days_back:days_back + rsi_p + 1]
        deltas = np.diff(segment)
        gains = deltas[deltas > 0].sum()
        losses = abs(deltas[deltas < 0].sum())
        avg_gain = gains / rsi_p
        avg_loss = losses / rsi_p
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return round(100 - (100 / (1 + rs)), 1)
    except Exception:
        return None


def _get_recent_scores(code: str, days: int = 5) -> list[float]:
    """获取最近 N 天的评分序列（从新到旧）"""
    try:
        from db import get_conn
        conn = get_conn()
        rows = conn.execute(
            "SELECT total_score FROM scoring_history WHERE code = ? ORDER BY date DESC LIMIT ?",
            (code, days)
        ).fetchall()
        conn.close()
        return [r["total_score"] for r in rows if r["total_score"] is not None]
    except Exception:
        return []


# ============================================================
# 🆕 CAUTION_BUY 二次精筛
# ============================================================

def compute_caution_buy_filter(code: str, tech: dict, alpha_signals: dict,
                                scores: dict = None) -> dict:
    """CAUTION_BUY 信号二次精筛 — 过滤低质量信号

    CAUTION_BUY 历史上 22 条信号胜率仅 30.8%，
    加三道过滤提升信号质量：
    1. 量能确认：成交量不低于 MA20 的 80%（无缩量）
    2. 动量底线：momentum 分 >= 35（淘汰弱动量标的）
    3. 趋势配合：价格在 MA20 上方（上升趋势确认）

    Returns:
        dict: {passed: bool, filters: [{name, passed, detail}]}
    """
    filters = []

    # 过滤1: 量能确认（不缩量）
    vol_ratio = tech.get("volume_ratio", 1)
    vol_ok = vol_ratio >= 0.8 or tech.get("volume_surge", False)
    filters.append({
        "name": "量能确认",
        "passed": vol_ok,
        "detail": f"成交量{vol_ratio:.1f}x MA20" if vol_ok else f"缩量{vol_ratio:.1f}x MA20",
    })

    # 过滤2: 动量底线 — 均值回归模式下放宽（翻转后低分=原高分回调→买点）
    mom_score = None
    if scores:
        mom_score = scores.get("momentum_score", 0)
    if mom_score is None:
        mom_score = 50
    try:
        from scorer import get_operational_mode
        _opm = get_operational_mode()
        _is_mr = _opm.get("factor_invert", False)
    except Exception:
        _is_mr = False
    if _is_mr:
        # 均值回归: 翻转后15-65=原动量35-85, 这是回调后买点区间
        mom_ok = 15 <= mom_score <= 65
    else:
        mom_ok = mom_score >= 35
    filters.append({
        "name": "动量底线" + ("[MR]" if _is_mr else ""),
        "passed": mom_ok,
        "detail": f"动量分{mom_score:.0f}" if mom_ok else f"动量分{mom_score:.0f}({'15-65期望' if _is_mr else '<35'})",
    })

    # 过滤3: 趋势配合 — 均值回归模式接受MA20下方（超跌反弹机会）
    trend_ok = tech.get("price_above_ma20", False)
    if _is_mr:
        # 均值回归: 允许MA20下方，但要有放量确认（缩量下跌不抄底）
        trend_ok = True  # 不拦截，留给量能过滤
    filters.append({
        "name": "趋势配合" + ("[MR]" if _is_mr else ""),
        "passed": trend_ok,
        "detail": "均值回归放宽" if (_is_mr and not tech.get("price_above_ma20", False)) else (
            "价格在MA20上方" if trend_ok else "价格在MA20下方"),
    })

    passed = all(f["passed"] for f in filters)

    return {
        "passed": passed,
        "filters": filters,
        "confidence": sum(1 for f in filters if f["passed"]) / len(filters),
    }


# ============================================================
# 买入确认检查
# ============================================================

def confirm_buy_signal(code: str, tech: dict, alpha_signals: dict) -> dict:
    """
    买入信号多条件确认 — 支持均值回归模式自适

    Returns
    -------
    dict with: confirmed (bool), reasons (list), confidence (float)
    """
    try:
        from scorer import get_operational_mode
        _opm2 = get_operational_mode()
        _is_mr2 = _opm2.get("factor_invert", False)
    except Exception:
        _is_mr2 = False

    reasons = []
    confirm_count = 0
    total_checks = 0

    # 条件1: 价格在 MA5 上方（短线强势）— 均值回归模式反转
    total_checks += 1
    if _is_mr2:
        # 均值回归: 价格在MA5下方=超跌→买点
        if not tech.get("price_above_ma5"):
            confirm_count += 1
            reasons.append("✓ [MR] 价格跌破MA5(超跌反弹机会)")
        else:
            reasons.append("✗ [MR] 价格在MA5上方(未超跌)")
    else:
        if tech.get("price_above_ma5"):
            confirm_count += 1
            reasons.append("✓ 价格站上MA5")
        else:
            reasons.append("✗ 价格在MA5下方")

    # 条件2: 均线多头排列 — 均值回归模式接受死叉
    total_checks += 1
    if _is_mr2:
        # 均值回归: 死叉后=超卖→买点
        if not tech.get("ma5_above_ma20"):
            confirm_count += 1
            reasons.append("✓ [MR] MA5<MA20(死叉超卖→反弹)")
        else:
            reasons.append("✗ [MR] 多头排列(未回调)")
    else:
        if tech.get("ma5_above_ma20"):
            confirm_count += 1
            reasons.append("✓ MA5 > MA20 多头排列")
        else:
            reasons.append("✗ 均线空头排列")

    # 条件3: RSI 不超买
    total_checks += 1
    rsi = tech.get("rsi", 50)
    if rsi is not None and rsi < 70:
        confirm_count += 1
        reasons.append(f"✓ RSI={rsi:.0f} 未超买")
    else:
        reasons.append(f"✗ RSI={rsi:.0f} 超买区")

    # 条件4: 布林带不在上轨外
    total_checks += 1
    if not tech.get("at_bb_upper", False):
        confirm_count += 1
        reasons.append("✓ 布林带未触及上轨")
    else:
        reasons.append("✗ 价格在布林上轨外")

    # 条件5: 成交量正常或温和放大
    total_checks += 1
    vol_ratio = tech.get("volume_ratio", 1)
    if not tech.get("volume_surge", False) and not tech.get("volume_dry", False):
        confirm_count += 1
        reasons.append(f"✓ 成交量正常 ({vol_ratio:.1f}x)")
    elif tech.get("volume_surge"):
        reasons.append(f"✗ 异常放量 ({vol_ratio:.1f}x)")
    else:
        reasons.append(f"✗ 缩量 ({vol_ratio:.1f}x)")

    # 条件6: Alpha因子不负面
    total_checks += 1
    if alpha_signals:
        avg_alpha = sum(v for v in alpha_signals.values() if v is not None) / max(1, len(alpha_signals))
        if avg_alpha >= SIGNAL_CONFIG["factor_signal_confirm"]:
            confirm_count += 1
            reasons.append(f"✓ Alpha因子积极 ({avg_alpha:+.2f})")
        elif avg_alpha <= SIGNAL_CONFIG["factor_signal_reject"]:
            reasons.append(f"✗ Alpha因子负面 ({avg_alpha:+.2f})")
        else:
            confirm_count += 1
            reasons.append(f"○ Alpha因子中性 ({avg_alpha:+.2f})")
            total_checks -= 1  # 中性不计入
    else:
        reasons.append("○ 无Alpha因子数据")

    confidence = confirm_count / max(1, total_checks) if total_checks > 0 else 0.5
    confirmed = confidence >= 0.35  # 至少 1/3 条件满足（放宽确认门槛）

    return {
        "confirmed": confirmed,
        "confidence": round(confidence, 2),
        "confirm_count": confirm_count,
        "total_checks": total_checks,
        "reasons": reasons,
    }


# ============================================================
# 🆕 SELL 信号缓冲确认 — 防止超卖反弹前误卖
# 数据依据: 46条SELL信号后1日上涨概率80.4%，说明大部分SELL在局部底部
# ============================================================

def confirm_sell_signal(code: str, total_score: float, tech: dict,
                        price: float = 0, detail: dict = None) -> dict:
    """SELL 信号需要额外确认，防止超卖反弹前误卖

    三道缓冲：
    1. RSI 不在深度超卖区（RSI >= 35）
    2. 价格不在买入区间内（buy zone 内不卖）
    3. 非布林下轨极端位置（bb_position >= 15）

    Returns:
        {confirmed: bool, reasons: list, blocked_by: list}
    """
    blocked_by = []
    reasons = []

    # 缓冲1: RSI 深度超卖保护 — 超卖时不下卖单
    rsi = tech.get("rsi", 50)
    if rsi is not None and rsi < 35:
        blocked_by.append(f"RSI={rsi:.0f} 深度超卖，反弹概率高，暂不卖出")
    elif rsi is not None and rsi < 40:
        reasons.append(f"RSI={rsi:.0f} 偏弱但未到超卖区")

    # 缓冲2: 买入区保护 — 在买入区内的标的不卖
    if detail and price > 0:
        buy_low = detail.get("buy_zone_low", 0)
        buy_high = detail.get("buy_zone_high", 0)
        if buy_low > 0 and buy_high > 0 and buy_low <= price <= buy_high:
            blocked_by.append(f"价格{price:.2f}在买入区[{buy_low:.0f}-{buy_high:.0f}]内，不卖出")

    # 缓冲3: 布林下轨保护 — 极度超卖不卖
    bb_pos = tech.get("bb_position", 50)
    if bb_pos is not None and bb_pos < 15:
        blocked_by.append(f"布林下轨({bb_pos:.0f}%)，超跌不卖")

    confirmed = len(blocked_by) == 0

    return {
        "confirmed": confirmed,
        "reasons": reasons,
        "blocked_by": blocked_by,
    }


# ============================================================
# 主信号生成
# ============================================================

def generate_signals(codes: list[str] = None, portfolio: "PortfolioManager" = None,
                     scorer_total_scores: dict = None) -> list[dict]:
    """
    为所有标的生产完整的交易信号

    Parameters
    ----------
    codes               : 标的列表, 默认全部
    portfolio           : PortfolioManager 实例, 用于计算仓位建议
    scorer_total_scores : dict[code → float], scorer 的 10 维评分
                          传入后跳过 signal_engine 自身的 6 维评分计算

    Returns
    -------
    list[dict] — 每个信号含 action / score / 建议仓位等
    """
    if codes is None:
        codes = ALL_CODES
    if portfolio is None:
        from portfolio import get_portfolio
        portfolio = get_portfolio()

    # 获取实时数据
    realtime = fetch_realtime(codes)
    rt_map = {r["code"]: r for r in realtime}

    # 获取持仓列表
    position_codes = set(portfolio.position_codes)

    signals = []
    for code in codes:
        try:
            sig = _generate_single_signal(code, rt_map.get(code, {}), position_codes, portfolio, scorer_total_scores)
            if sig:
                signals.append(sig)
        except Exception as e:
            signals.append({
                "code": code,
                "name": STOCK_MAP.get(code, {}).get("name", code),
                "action": "ERROR",
                "total_score": 0,
                "error": str(e),
            })

    # 按评分排序
    signals.sort(key=lambda s: s.get("total_score", 0), reverse=True)
    return signals


def _generate_single_signal(code: str, realtime_data: dict,
                             position_codes: set, portfolio: "PortfolioManager",
                             scorer_total_scores: dict = None) -> Optional[dict]:
    """单个标的的信号生成"""
    name = STOCK_MAP.get(code, {}).get("name", code)
    price = realtime_data.get("price", 0)
    if price <= 0:
        rows = get_price_history(code, 1)
        price = float(rows[0]["close"]) if rows else 0
        if price <= 0:
            return None

    is_holding = code in position_codes

    # 1. 技术面因子
    tech = compute_technical_factors(code)

    # 2. Alpha 因子
    alpha_factors = _factor_engine.compute_all_factors(code)
    alpha_signals = alpha_factors.get("signals", {})

    # 2.5 基本面因子
    fund_signal = _fund_engine.get_fundamental_signal(code) if _fund_engine else None

    # 3. Serenity 评分
    serenity_score = compute_serenity_score(code)

    # 4. 基本面 + 买入区间
    detail = STOCK_DETAILS.get(code, {})
    zone_info = {
        "price": price,
        "buy_zone_low": detail.get("buy_zone_low", 0),
        "buy_zone_high": detail.get("buy_zone_high", 0),
        "target_sell": detail.get("target_sell", 0),
        "score": detail.get("score", 50),
    }

    # 5. 技术面评分
    tech_score = compute_trend_score(tech)

    # 6. 综合评分
    # 从 alpha_signals 中提取14因子值计算独立维度评分
    ff_signals = alpha_signals or {}
    ff_vals = [v for v in ff_signals.values() if v is not None]
    if ff_vals:
        ff_avg = sum(ff_vals) / len(ff_vals)
        fourteen_factor_score = np.clip(ff_avg * 2, -1, 1) * 50 + 50  # 映射到 0-100
    else:
        fourteen_factor_score = None

    # 优先使用 scorer 传入的 10 维评分，替代 signal_engine 自身的 6 维计算
    if scorer_total_scores and code in scorer_total_scores:
        total_score = scorer_total_scores[code]
    else:
        total_score = compute_multi_factor_score(tech_score, serenity_score, alpha_signals, zone_info, fund_signal, fourteen_factor_score)

    # 评分已由 scorer 通过 scorer_total_scores 传入（或回退到 compute_multi_factor_score）
    # 不再需要 scoring_history 的 DB 回退

    # 🆕 均值回归评分叠加 — MR 模式激活时，RSI 超卖加分/超买减分
    # 回测验证：MeanReversionStrategy 在当前市场显著优于 MultiFactorStrategy
    # 002281: MR +21.9% vs MF -3.4%, 000988: MR +9.3% vs MF -0.6%
    try:
        from market_sense import MarketSense
        _mr_mode = MarketSense().get_operational_mode()
    except Exception:
        _mr_mode = {}
    # 如果 scorer 已传入评分（含 MR 维度），跳过此处的 RSI 叠加，避免重复计算
    _use_scorer_scores = bool(scorer_total_scores and code in scorer_total_scores)
    if _mr_mode.get("factor_invert") and not _use_scorer_scores:
        rsi_val = tech.get("rsi", 50)
        if rsi_val is not None and not (isinstance(rsi_val, float) and (rsi_val != rsi_val)):
            if rsi_val < 30:
                total_score += 12
            elif rsi_val < 40:
                total_score += 5
            elif rsi_val > 75:
                total_score -= 10
            elif rsi_val > 65:
                total_score -= 5
            total_score = max(0, min(100, total_score))

    # 7. 信号级别（持仓/非持仓分开处理，带 conviction 动态调参）
    try:
        _conv_target = _apply_conviction_to_signal_config()
    except Exception:
        _conv_target = None
    
    action = get_position_signal(total_score,
                                 ((price - zone_info['target_sell'])/zone_info['target_sell']*100) if zone_info['target_sell'] > 0 else 0,
                                 is_holding, _conv_target) if is_holding else get_signal_level(total_score, _conv_target)

    # 7.3 🆕 技术面卖出触发器 — 均线死叉/上轨遇阻/连降等
    # 市场状态自适应：均值回归模式下降权卖出触发器（历史SELL信号+3%反弹）
    sell_triggers = compute_sell_triggers(code, tech, total_score)
    raw_sell_weight = sum(t["weight"] for t in sell_triggers)
    try:
        from scorer import get_operational_mode
        _op_mode = get_operational_mode()
        _stw = _op_mode.get("sell_trigger_weight", 1.0)
    except Exception:
        _stw = 1.0
    total_sell_weight = raw_sell_weight * _stw
    if total_sell_weight >= 3 and action not in ("SELL", "STOP_LOSS"):
        action = "SELL"
        log.info("  🔴 卖出触发器触发(原始%d×权重%.0f%%=%.1f点): %s", raw_sell_weight, _stw*100, total_sell_weight,
                 "; ".join(t["detail"] for t in sell_triggers))
    elif total_sell_weight >= 2 and total_score < 60 and action not in ("SELL", "STOP_LOSS"):
        if is_holding:
            action = "WEAK_HOLD"
        else:
            action = "WATCH"
        log.info("  🟡 卖出预警(原始%d×权重%.0f%%=%.1f点): %s", raw_sell_weight, _stw*100, total_sell_weight,
                 "; ".join(t["detail"] for t in sell_triggers))

    # 7.5 已达目标价 → 强制卖出
    if is_holding and price >= zone_info.get("target_sell", 0) and zone_info.get("target_sell", 0) > 0:
        action = "SELL"

    # 8. 如果是持仓，检查止盈止损
    stop_actions = portfolio.check_stop_conditions()
    for sa in stop_actions:
        if sa["code"] == code:
            if "STOP" in sa["action"]:
                action = "STOP_LOSS"
                total_score = 10
            elif "SELL_PARTIAL" in sa["action"]:
                # 止盈：保留原始评分，独立信号级别
                action = "TAKE_PROFIT"
                # total_score 不变（止盈不降分）

    # 8.5 🆕 SELL 信号缓冲确认 — 防止超卖反弹前误卖
    # 目标价强制卖出和止损不经过此缓冲
    if action == "SELL" and not (is_holding and price >= zone_info.get("target_sell", 0) and zone_info.get("target_sell", 0) > 0):
        sell_confirm = confirm_sell_signal(code, total_score, tech, price, detail)
        if not sell_confirm["confirmed"]:
            # 降级：持有→WEAK_HOLD，非持有→WATCH
            action = "WEAK_HOLD" if is_holding else "WATCH"
            log.info("  🛡️ SELL缓冲拦截 %s: %s → %s",
                     name, "SELL", action)

    # 9. 买入确认（仅对非持仓标的）
    buy_confirm = None
    suggested_amount = 0
    suggested_shares = 0
    if not is_holding and action in ("STRONG_BUY", "BUY", "CAUTION_BUY"):
        buy_confirm = confirm_buy_signal(code, tech, alpha_signals)

        # 🆕 v3.0 STRONG_BUY 加强确认 — 需4/5条件通过 + MA20上方 + 量能不缩
        if action == "STRONG_BUY":
            strong_fails = []
            if buy_confirm["confirm_count"] < 4:
                strong_fails.append(f"确认条件不足({buy_confirm['confirm_count']}/5)")
            if not tech.get("price_above_ma20", False):
                strong_fails.append("价格在MA20下方")
            if tech.get("volume_dry", False):
                strong_fails.append("成交量萎缩")
            if strong_fails:
                action = "BUY"  # 降级到BUY
                log.info("  🟡 STRONG_BUY→BUY %s: %s", name, "; ".join(strong_fails))

        if not buy_confirm["confirmed"]:
            # 降级
            if action == "STRONG_BUY":
                action = "BUY"
            elif action == "BUY":
                action = "CAUTION_BUY"
            elif action == "CAUTION_BUY":
                action = "HOLD"

        # 🆕 CAUTION_BUY 二次精筛：通过初筛后额外过滤
        if action == "CAUTION_BUY" and buy_confirm and buy_confirm["confirmed"]:
            # 获取动量维度分（用于动量底线过滤）
            dim_scores = {}
            try:
                from db import get_conn
                _conn2 = get_conn()
                _sc = _conn2.execute(
                    "SELECT momentum_score FROM scoring_history WHERE code=? AND date=? ORDER BY date DESC LIMIT 1",
                    (code, date.today().isoformat())
                ).fetchone()
                if _sc and _sc[0] is not None:
                    dim_scores["momentum_score"] = _sc[0]
                _conn2.close()
            except Exception:
                pass
            cb_filter = compute_caution_buy_filter(code, tech, alpha_signals, dim_scores)
            if not cb_filter["passed"]:
                action = "HOLD"
                log.info("  🟡 CAUTION_BUY 精筛拦截 %s: %s",
                         name, "; ".join(f"{f['name']}:{f['detail']}" for f in cb_filter["filters"] if not f["passed"]))
            # 保存过滤结果供显示
            _caution_filter = cb_filter

        # 仓位建议
        if action in ("STRONG_BUY", "BUY"):
            sizing = portfolio.calc_position_size(code, buy_confirm["confidence"] if buy_confirm else 0.5)
            suggested_amount = sizing["amount"]
            suggested_shares = sizing["shares"]

    # 10. 构建结果
    buy_zone_str = f"{zone_info['buy_zone_low']:.0f}-{zone_info['buy_zone_high']:.0f}" if zone_info['buy_zone_low'] > 0 else "N/A"

    result = {
        "code": code,
        "name": name,
        "price": price,
        "action": action,
        "signal_desc": SIGNAL_LEVELS.get(action, {}).get("desc", action),
        "total_score": total_score,
        "tech_score": round(tech_score, 1),
        "serenity_score": serenity_score,
        "alpha_score": _compute_alpha_composite(alpha_signals),
        "fundamental_score": round(fund_signal, 4) if fund_signal is not None else None,
        "is_holding": is_holding,
        "buy_zone": buy_zone_str,
        "target_sell": f"{zone_info['target_sell']:.0f}" if zone_info['target_sell'] > 0 else "N/A",
        "reason": detail.get("reason", ""),
        "tech_indicators": {
            "ma5": tech.get("ma5", 0),
            "ma20": tech.get("ma20", 0),
            "rsi": tech.get("rsi", 50),
            "bb_position": tech.get("bb_position", 50),
            "volume_ratio": tech.get("volume_ratio", 1),
            "ma5_above_ma20": tech.get("ma5_above_ma20", False),
        },
        "alpha_signals": alpha_signals,
        "change_pct": realtime_data.get("change_pct", 0) if realtime_data else 0,
        "sell_triggers": sell_triggers,
    }

    if buy_confirm:
        result["buy_confirm"] = buy_confirm
        result["suggested_amount"] = suggested_amount
        result["suggested_shares"] = suggested_shares

    # 记录信号日志（用于绩效追踪）
    try:
        from db import save_signal_log
        save_signal_log(
            code=code, action=action, total_score=total_score, price=price,
            is_holding=is_holding, tech_score=round(tech_score, 1),
            serenity_score=serenity_score,
            alpha_score=_compute_alpha_composite(alpha_signals),
            fundamental_score=round(fund_signal, 4) if fund_signal is not None else None,
            details={"rsi": tech.get("rsi", 50), "volume_ratio": tech.get("volume_ratio", 1),
                     "bb_position": tech.get("bb_position", 50)}
        )
    except Exception:
        pass  # 日志失败不影响信号输出

    return result
