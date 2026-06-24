"""
conviction_engine.py — 权重辩论引擎 + 多周期共识

借鉴:
- TradingAgents 的 Bull↔Bear 辩论模式 → 规则驱动的因子权重博弈
- QuantDinger 的 consensus 多周期加权 → 日/周/月评分共识

核心逻辑:
1. 权重辩论：根据市场状态（强势/震荡/弱势）动态调整 9 维评分权重
2. 多周期共识：日线(1d) + 周线(5d) + 月线(20d) 加权融合
3. 输出：融合信号 + 置信度 + 趋势展望
"""

from datetime import date, timedelta
from typing import Any

# 三态权重字典：强势 / 震荡 / 弱势（v3.0: 8维精简）
REGIME_WEIGHTS = {
    "强势": {
        "zone": 0.14, "momentum": 0.22, "volume": 0.06,
        "serenity": 0.14, "factor": 0.16,
        "technical": 0.12, "moat": 0.16,
    },
    "震荡": {
        "zone": 0.18, "momentum": 0.14, "volume": 0.04,
        "serenity": 0.18, "factor": 0.16,
        "technical": 0.10, "moat": 0.20,
    },
    "弱势": {
        "zone": 0.16, "momentum": 0.08, "volume": 0.02,
        "serenity": 0.16, "factor": 0.12,
        "technical": 0.08, "moat": 0.38,
    },
}

# 多周期共识权重
CYCLE_WEIGHTS = {
    "short": {"daily": 0.60, "weekly": 0.30, "monthly": 0.10},
    "medium": {"daily": 0.25, "weekly": 0.50, "monthly": 0.25},
    "long": {"daily": 0.10, "weekly": 0.30, "monthly": 0.60},
}


def _compute_regime(all_scores: list[dict]) -> str:
    """根据全量评分分布推断当前市场状态
    
    强势：平均分 ≥ 70，高分（≥75）占比 ≥ 40%
    弱势：平均分 < 55，低分（<60）占比 ≥ 60%
    震荡：其他
    """
    if not all_scores:
        return "震荡"
    
    avg = sum(r["total_score"] for r in all_scores) / len(all_scores)
    high_pct = sum(1 for r in all_scores if r["total_score"] >= 75) / len(all_scores)
    low_pct = sum(1 for r in all_scores if r["total_score"] < 60) / len(all_scores)
    
    if avg >= 70 and high_pct >= 0.40:
        return "强势"
    elif avg < 55 or low_pct >= 0.60:
        return "弱势"
    return "震荡"


def _per_stock_adjustment(stock_score: dict, regime: str) -> dict:
    """针对单个标的额外调整
    
    根据 zone_label 做二级博弈:
    - 深度折扣 → 低估，基本面权重↗，动量权重↘
    - 强势区间 → 动量权重↗，护城河权重↘
    - 买入区 → 均衡
    """
    zone = stock_score.get("zone_label", "")
    delta = {}
    
    if "深度折扣" in zone or "超卖" in zone:
        delta = {"base": +0.03, "moat": +0.02, "momentum": -0.03, "technical": -0.02}
    elif "强势" in zone or zone == "买入区 ✓":
        delta = {"momentum": +0.03, "volume": +0.02, "moat": -0.03, "base": -0.02}
    elif "持有" in zone or "持有区" in zone:
        delta = {"technical": +0.03, "momentum": -0.02, "volume": -0.02}
    
    return delta


def debate_weights(all_scores: list[dict]) -> tuple[dict, str]:
    """权重辩论：全量评分排名 → 市场状态 → 因子权重
    
    Args:
        all_scores: score_all() 返回的 result 列表
        
    Returns:
        (debated_weights: dict, regime: str)
    """
    regime = _compute_regime(all_scores)
    base = dict(REGIME_WEIGHTS.get(regime, REGIME_WEIGHTS["震荡"]))
    
    # 根据高/低分占比二次修正
    if all_scores:
        high_count = sum(1 for r in all_scores if r["total_score"] >= 75)
        low_count = sum(1 for r in all_scores if r["total_score"] < 60)
        if high_count >= 4:
            base["momentum"] = min(base.get("momentum", 0.12) + 0.02, 0.25)
            base["moat"] = max(base.get("moat", 0.10) - 0.02, 0.05)
        if low_count >= 4:
            base["moat"] = min(base.get("moat", 0.10) + 0.03, 0.20)
            base["technical"] = max(base.get("technical", 0.08) - 0.02, 0.04)
    
    # 归一化
    total = sum(base.values())
    if abs(total - 1.0) > 0.001:
        for k in base:
            base[k] = round(base[k] / total, 4)
    
    # 精度修正
    diff = 1.0 - sum(base.values())
    if diff != 0:
        keys = list(base.keys())
        base[keys[-1]] = round(base[keys[-1]] + diff, 4)
    
    return base, regime


def get_stock_adjusted_weights(stock_score: dict, debated: dict) -> dict:
    """判断标的二级调整后的最终权重"""
    regime = "震荡"  # 调用前从 debate_weights 获取
    delta = _per_stock_adjustment(stock_score, regime)
    adjusted = dict(debated)
    for k, v in delta.items():
        adjusted[k] = max(0.02, min(0.30, adjusted.get(k, 0.10) + v))
    total = sum(adjusted.values())
    for k in adjusted:
        adjusted[k] = round(adjusted[k] / total, 4)
    return adjusted


def _load_history(code: str, days: int = 20) -> list[dict]:
    """从数据库加载历史评分数据
    
    Args:
        code: 股票代码
        days: 回溯天数
        
    Returns: 按日期排好序的评分数据列表
    """
    from db import get_conn
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT date, total_score FROM scoring_history "
            "WHERE code=? ORDER BY date DESC LIMIT ?",
            (code, days)
        ).fetchall()
        return [{"date": r[0], "total_score": r[1]} for r in rows]
    except Exception:
        return []
    finally:
        conn.close()


def multi_cycle_consensus(code: str, today_score: float, 
                           cycle_weights: str = "medium") -> dict[str, Any]:
    """多周期共识：融合日线+周线+月线评分
    
    Args:
        code: 股票代码
        today_score: 今日评分
        cycle_weights: "short" / "medium" / "long"
        
    Returns:
        {
            "consensus_score": float,     # 多周期融合
            "raw_scores": {"daily": x, "weekly": y, "monthly": z},
            "trend": "up"/"down"/"flat",  # 趋势方向
            "confidence": float,           # 0.0~1.0
            "cycles_count": int,           # 有效周期数
            "detail": str,                 # 简要说明
        }
    """
    history = _load_history(code, 20)
    weights = CYCLE_WEIGHTS.get(cycle_weights, CYCLE_WEIGHTS["medium"])
    
    daily = today_score
    
    # 周线：最近5天均值（含今日）
    recent_5 = [r["total_score"] for r in history[:5]] + [today_score]
    weekly = sum(recent_5) / len(recent_5) if recent_5 else daily
    
    # 月线：最近20天均值（含今日）
    recent_20 = [r["total_score"] for r in history[:20]] + [today_score]
    monthly = sum(recent_20) / len(recent_20) if recent_20 else daily
    
    # 融合评分
    consensus = (
        daily * weights.get("daily", 0.25) +
        weekly * weights.get("weekly", 0.50) +
        monthly * weights.get("monthly", 0.25)
    )
    
    # 趋势判断
    all_vals = [r["total_score"] for r in history] + [today_score]
    recent_trend = 0.0
    if len(all_vals) >= 3:
        recent_trend = all_vals[-1] - all_vals[-3]
        if recent_trend >= 3:
            trend = "up"
        elif recent_trend <= -3:
            trend = "down"
        else:
            trend = "flat"
    else:
        trend = "flat"
    
    # 置信度：数据越丰富越可信
    cycles_count = len(history) + 1  # 包含今日
    if cycles_count >= 15:
        confidence = 0.85
    elif cycles_count >= 10:
        confidence = 0.70
    elif cycles_count >= 5:
        confidence = 0.55
    else:
        confidence = 0.40
    
    # 趋势稳定性修正置信度
    if trend == "up" and daily > weekly:
        confidence = min(1.0, confidence + 0.10)
    elif trend == "down" and daily < weekly:
        confidence = min(1.0, confidence + 0.10)
    elif abs(daily - weekly) > 10:
        confidence = max(0.3, confidence - 0.15)
    
    # 多周期一致性：三个周期同向则提升置信度
    if trend != "flat":
        same_dir = 0
        if trend == "up":
            same_dir = sum(1 for v in [daily, weekly, monthly] if v > today_score)
        else:
            same_dir = sum(1 for v in [daily, weekly, monthly] if v < today_score)
        if same_dir >= 2:
            confidence = min(1.0, confidence + 0.08)
    
    return {
        "consensus_score": round(consensus, 1),
        "raw_scores": {
            "daily": round(daily, 1),
            "weekly": round(weekly, 1),
            "monthly": round(monthly, 1),
        },
        "trend": trend,
        "confidence": round(confidence, 2),
        "cycles_count": cycles_count,
        "detail": {
            "up": f"过去3期评分上升 {recent_trend:.1f} 分，趋势向上" if trend == "up" else None,
            "down": f"过去3期评分下降 {abs(recent_trend):.1f} 分，趋势向下" if trend == "down" else None,
            "flat": "趋势平稳，多周期评分一致性良好" if abs(consensus - daily) < 5 else "各周期评分出现明显分化",
        }.get(trend, ""),
    }


def generate_multi_horizon_outlook(code: str, today_score: float) -> dict:
    """生成短/中/长三期展望
    
    直接映射 QuantDinger 的 24h/3d/1w/1m 模式：
    - 短期(1-3天) → daily 驱动
    - 中期(1-2周) → medium 加权
    - 长期(1月) → monthly 驱动
    """
    short = multi_cycle_consensus(code, today_score, "short")
    medium = multi_cycle_consensus(code, today_score, "medium")
    long_ = multi_cycle_consensus(code, today_score, "long")
    
    return {
        "code": code,
        "short_term": short,
        "medium_term": medium,
        "long_term": long_,
    }


def generate_position_advice(regime: str, debated: dict, scores: list[dict],
                              active_count: int = 0) -> str:
    """根据权重辩论结果生成仓位配比建议
    
    规则：
    - 弱势市场 → 减仓，护城河权重高 → 保守策略
    - 强势市场 → 增仓，动量权重高 → 进攻策略
    - 震荡市场 → 均衡配置
    """
    m = debated.get("moat", 0.10)
    momentum = debated.get("momentum", 0.10)

    # 仓位比例建议：基于防守 vs 进攻因子比值
    defense_ratio = (m + debated.get("base", 0.12)) / max(momentum, 0.01)
    
    if regime == "弱势":
        if defense_ratio > 1.5:
            advice = "🔴 弱势+防御偏高 → 建议仓位≤30%，仅保留高护城河标的"
        else:
            advice = "🔴 弱势行情 → 建议仓位≤50%，严格止损"
    elif regime == "强势":
        if momentum > 0.18:
            advice = "🟢 强势+动量领跑 → 建议仓位≤80%，动量选股为主"
        else:
            advice = "🟢 强势市场 → 建议仓位60-80%，注意轮动节奏"
    else:  # 震荡
        if defense_ratio > 1.2:
            advice = "🟡 震荡+防御偏好 → 建议仓位≤50%，高抛低吸"
        else:
            advice = "🟡 震荡行情 → 建议仓位40-60%，均衡配置"
    
    # 持仓过多警告
    if active_count >= 5 and regime in ("弱势", "震荡"):
        advice += f"\n   ⚠️ 当前持有{active_count}只，建议聚焦到2-3只核心标的"
    
    return advice


def run_and_save() -> dict:
    """运行时入口：辩论→持久化→返回结果
    
    被 daily_report 和 CLI 调用
    """
    from datetime import date
    from scorer import score_all
    from db import save_conviction_log
    
    today = date.today().isoformat()
    all_scores = score_all()
    debated_weights, regime = debate_weights(all_scores)
    
    # 统计
    avg_score = sum(r["total_score"] for r in all_scores) / len(all_scores) if all_scores else 0
    high_count = sum(1 for r in all_scores if r["total_score"] >= 75)
    low_count = sum(1 for r in all_scores if r["total_score"] < 60)
    
    # 获取持仓数
    active_count = 0
    try:
        from db import load_all_stocks
        active_count = len([s for s in load_all_stocks() if s.get("is_active")])
    except Exception:
        pass
    
    position_advice = generate_position_advice(regime, debated_weights, all_scores, active_count)
    
    # 持久化
    entry = {
        "date": today,
        "regime": regime,
        "debated_weights": debated_weights,
        "regime_weights": REGIME_WEIGHTS.get(regime, REGIME_WEIGHTS["震荡"]),
        "score_avg": round(avg_score, 1),
        "high_count": high_count,
        "low_count": low_count,
        "position_advice": position_advice,
    }
    save_conviction_log(entry)
    
    return {
        "date": today,
        "regime": regime,
        "weights": debated_weights,
        "score_avg": round(avg_score, 1),
        "high_count": high_count,
        "low_count": low_count,
        "position_advice": position_advice,
        "active_count": active_count,
    }
