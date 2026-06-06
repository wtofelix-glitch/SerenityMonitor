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
from signal_engine import generate_signals, compute_technical_factors, compute_trend_score, confirm_buy_signal, get_signal_level, get_position_signal
from sentiment_engine import compute_sentiment_score

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
        "base": 0.15,       # 基本面适配度
        "zone": 0.15,       # 价格位置
        "momentum": 0.15,   # 动量
        "volume": 0.05,     # 成交量（下调）
        "serenity": 0.15,   # Serenity 框架匹配度
        "factor": 0.15,     # 因子引擎评分
        "technical": 0.10,  # 技术面评分（下调）
        "sentiment": 0.10,  # 🆕 新闻情绪评分
    }

# 非侵入式 fallback：确保所有 7 个维度都有默认值
_SCORE_WEIGHT_DEFAULTS = {
    "base": 0.15, "zone": 0.15, "momentum": 0.15, "volume": 0.05,
    "serenity": 0.15, "factor": 0.15, "technical": 0.10, "sentiment": 0.10,
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


def score_all() -> list[dict]:
    """对所有候选标的进行评分 + 生成交易信号"""
    today = date.today().isoformat()
    snapshots = get_all_today_snapshots()

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
    all_signals = generate_signals(portfolio=portfolio)
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
        serenity_score = compute_serenity_score(code)

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

                    cycle_factors_raw = {
                        "daily": daily_sig,
                        "weekly": weekly_sig,
                        "monthly": monthly_sig,
                    }
                except Exception:
                    multi_cycle_score = factor_score
                    cycle_factors_raw = {}
            except Exception:
                factor_score = 50

        # 🆕 技术面评分
        tech = compute_technical_factors(code)
        technical_score = compute_trend_score(tech)

        # 🆕 新闻情绪评分
        sentiment_score = compute_sentiment_score(code)

        # 加权总分（8维度 → 含情绪）
        total = (
            base_score * score_weight["base"] +
            zone_score * score_weight["zone"] +
            momentum_score * score_weight["momentum"] +
            volume_score * score_weight["volume"] +
            serenity_score * score_weight["serenity"] +
            factor_score * score_weight["factor"] +
            technical_score * score_weight["technical"] +
            sentiment_score * score_weight["sentiment"]
        )

        # 🆕 信号集成 — 使用 scorer 统一 8 维度评分体系
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
            "sentiment_score": round(sentiment_score, 1),  # 🆕 情绪
            "multi_cycle_factor": round(multi_cycle_score, 1),  # 🅱 三周期融合分
            "cycle_factors": cycle_factors_raw,  # 🅱 三周期原始值
            "details": {
                "price": price,
                "change_pct": change_pct,
                "volume": volume,
                "zone_label": zone_label,
                "target_sell": detail.get("target_sell", 0),
                "growth": round(((detail.get("target_sell", 0) - price) / price * 100), 1) if detail.get("target_sell", 0) > 0 else 0,
                "signal_action": signal_action,
                "signal_confidence": signal_confidence,
                "tech_ma5": tech.get("ma5", 0),
                "tech_ma20": tech.get("ma20", 0),
                "tech_rsi": tech.get("rsi", 50),
                "tech_bb_pos": tech.get("bb_position", 50),
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
