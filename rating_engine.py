"""
综合评级引擎 — 融合14因子信号 + 基本面信号 + Serenity评分
输出 A/B/C/D/E 五档评级
"""
import logging
from typing import Optional

from config import STOCK_MAP, ALL_CODES, STOCK_DETAILS, compute_serenity_score
from factor_engine import get_current_signals
try:
    from fundamental_engine import FundamentalEngine
    HAS_FUNDAMENTAL = True
except ImportError:
    HAS_FUNDAMENTAL = False
    FundamentalEngine = None

logger = logging.getLogger(__name__)

# 评级阈值
RATING_THRESHOLDS = {
    "A": 0.4,    # 综合得分 >= 0.4
    "B": 0.1,    # >= 0.1
    "C": -0.1,   # >= -0.1
    "D": -0.4,   # >= -0.4
    # E: < -0.4
}

RATING_EMOJIS = {
    "A": "🅰",
    "B": "🅱",
    "C": "🅲",
    "D": "🅳",
    "E": "🅴",
    "N/A": "❓",
}

SIGNAL_EMOJIS = {
    "STRONG_BUY": "🟢🟢🟢",
    "BUY": "🟢🟢",
    "CAUTION_BUY": "🟢",
    "HOLD": "⚪",
    "WATCH": "🟡",
    "SELL": "🔴🔴",
    "STOP_LOSS": "🔴🔴🔴",
}


# 全局缓存的引擎实例
_fund_engine = FundamentalEngine()
_factor_signals_cache = None
_factor_signals_ts = 0


def _get_factor_signals() -> dict:
    """获取因子信号，带缓存"""
    global _factor_signals_cache, _factor_signals_ts
    import time
    now = time.time()
    # 缓存 30 秒
    if _factor_signals_cache is not None and now - _factor_signals_ts < 30:
        return _factor_signals_cache
    results = get_current_signals()
    signals = {}
    for r in results:
        signals[r["code"]] = {
            "signal": r.get("signal", 0),
            "factors": r.get("factors", {}).get("signals", {}),
        }
    _factor_signals_cache = signals
    _factor_signals_ts = now
    return signals


def get_rating(code: str) -> dict:
    """
    获取单只标的的综合评级

    综合得分 = 0.40 * 因子信号平均 + 0.30 * 基本面信号 + 0.30 * Serenity归一化

    Returns
    -------
    dict: {
        "rating": "A"|"B"|"C"|"D"|"E"|"N/A",
        "score": float,
        "rating_emoji": str,
        "signal_label": str,
        "factors": {
            "factor_signal": float,
            "fundamental_signal": float or None,
            "serenity_score": float,
        }
    }
    """
    # 1. 因子信号
    factor_signals = _get_factor_signals()
    fs_info = factor_signals.get(code, {})
    factor_avg = fs_info.get("signal", 0)

    # 2. 基本面信号
    try:
        fund_signal = _fund_engine.get_fundamental_signal(code)
    except Exception:
        fund_signal = None
    fund_signal = fund_signal if fund_signal is not None else 0

    # 3. Serenity 评分 (0-100) → 归一化到 [-1, 1]
    serenity_raw = compute_serenity_score(code)
    serenity_norm = (serenity_raw / 50.0) - 1.0  # 0→-1, 50→0, 100→1

    # 4. 加权综合
    composite = (
        0.40 * factor_avg +
        0.30 * fund_signal +
        0.30 * serenity_norm
    )

    # 5. 映射到 A/B/C/D/E
    rating = "N/A"
    for r, threshold in sorted(RATING_THRESHOLDS.items(), key=lambda x: -x[1]):
        if composite >= threshold:
            rating = r
            break
    else:
        rating = "E"

    # 6. 信号标签
    if composite >= 0.4:
        signal_label = "STRONG_BUY"
    elif composite >= 0.1:
        signal_label = "BUY"
    elif composite >= -0.1:
        signal_label = "HOLD"
    elif composite >= -0.4:
        signal_label = "WATCH"
    else:
        signal_label = "SELL"

    return {
        "rating": rating,
        "score": round(composite, 4),
        "rating_emoji": RATING_EMOJIS.get(rating, "❓"),
        "signal_label": signal_label,
        "signal_emoji": SIGNAL_EMOJIS.get(signal_label, "⚪"),
        "factors": {
            "factor_signal": round(factor_avg, 4),
            "fundamental_signal": round(fund_signal, 4) if fund_signal is not None else None,
            "serenity_score": serenity_raw,
            "serenity_norm": round(serenity_norm, 4),
            "composite": round(composite, 4),
        },
    }


def get_portfolio_rating() -> dict:
    """
    获取持仓组合的综合评级

    Returns
    -------
    dict: {
        "overall_rating": "A"|...,
        "overall_score": float,
        "stocks": [{code, name, rating, score}],
        "count": int,
    }
    """
    from db import load_all_stocks
    stocks = load_all_stocks()
    active = [s for s in stocks if s.get("is_active")]

    if not active:
        return {"overall_rating": "N/A", "overall_score": 0, "stocks": [], "count": 0}

    results = []
    for s in active:
        code = s["code"]
        try:
            r = get_rating(code)
            results.append({
                "code": code,
                "name": s["name"],
                "rating": r["rating"],
                "rating_emoji": r["rating_emoji"],
                "score": r["score"],
                "signal_label": r["signal_label"],
                "signal_emoji": r["signal_emoji"],
            })
        except Exception as e:
            logger.debug("Rating failed for %s: %s", code, e)
            results.append({
                "code": code,
                "name": s["name"],
                "rating": "N/A",
                "rating_emoji": "❓",
                "score": 0,
                "signal_label": "N/A",
                "signal_emoji": "❓",
            })

    results.sort(key=lambda x: x["score"], reverse=True)
    avg_score = sum(r["score"] for r in results) / len(results) if results else 0

    # 综合评级取平均分
    overall_rating = "N/A"
    for r, threshold in sorted(RATING_THRESHOLDS.items(), key=lambda x: -x[1]):
        if avg_score >= threshold:
            overall_rating = r
            break
    else:
        overall_rating = "E"

    return {
        "overall_rating": overall_rating,
        "overall_score": round(avg_score, 4),
        "stocks": results,
        "count": len(results),
    }


def get_candidate_rank() -> list[dict]:
    """
    候选标的按综合得分排名

    Returns
    -------
    list[dict]: [{code, name, rating, rating_emoji, score}] 按得分降序
    """
    from db import load_all_stocks
    stocks = load_all_stocks()
    # 非持仓标的
    candidates = [s for s in stocks if not s.get("is_active")]

    results = []
    for s in candidates:
        code = s["code"]
        try:
            r = get_rating(code)
            results.append({
                "code": code,
                "name": s["name"],
                "rating": r["rating"],
                "rating_emoji": r["rating_emoji"],
                "score": r["score"],
            })
        except Exception:
            continue

    results.sort(key=lambda x: x["score"], reverse=True)
    return results
