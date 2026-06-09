"""
反思学习环 — 基于 TradingAgents 反思机制
评分→次日比对→维度IC→自动权重调整

工作原理：
1. 每日评分后自动记录预测（save_reflection）
2. 次日补填实际收益（fill_outcomes）
3. 按维度计算 Rank IC
4. IC>0 上调权重 / IC<0 下调权重
5. 生成反思文本 → 写入 score_reflections 表

用法：
    python3 reflection_engine.py                    # 生成今日反思
    python3 reflection_engine.py --fill-outcomes    # 补填未完善的实际收益
    python3 reflection_engine.py --show             # 显示近期反思
    python3 reflection_engine.py --apply            # 应用反思→调整权重
"""

import json
import sys
from datetime import date, timedelta
from typing import Optional
from collections import defaultdict

from db import (
    get_conn, save_reflection, update_reflection_outcome,
    get_reflections, get_unfilled_reflections, get_reflection_dimension_ic,
    get_price_history, get_signal_performance,
)
from config import STOCK_MAP, ALL_CODES

# 从 scoring_history 获取最新评分数据
def _get_latest_scores() -> list[dict]:
    conn = get_conn()
    rows = conn.execute("""
        SELECT s1.* FROM scoring_history s1
        INNER JOIN (
            SELECT code, MAX(date) as max_date FROM scoring_history
            GROUP BY code
        ) s2 ON s1.code = s2.code AND s1.date = s2.max_date
        ORDER BY s1.total_score DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _get_scores_on_date(date_str: str) -> dict:
    """获取某一天所有标的的评分，返回 {code: score_dict}"""
    conn = get_conn()
    rows = conn.execute("""
        SELECT * FROM scoring_history WHERE date=?
    """, (date_str,)).fetchall()
    conn.close()
    return {r["code"]: dict(r) for r in rows}


# -----------------------------------------------------------------
# 维度 IC 计算
# -----------------------------------------------------------------

DIMENSION_KEYS = [
    "base_score", "zone_score", "momentum_score", "volume_score",
    "serenity_score", "factor_score", "technical_score", "sentiment_score",
]


def compute_dimension_ic(days: int = 20) -> dict:
    """
    计算各评分维度与次日收益的 Rank IC。

    方法：对每个交易日，取各维度评分与次日涨跌幅的 Spearman rank correlation，
    然后对最近N天取均值。

    Returns:
        {dimension: mean_ic, ...}
    """
    today = date.today()
    dim_ic_sums = defaultdict(float)
    dim_ic_counts = defaultdict(int)

    for offset in range(1, days + 1):
        score_date = (today - timedelta(days=offset)).isoformat()
        next_date = (today - timedelta(days=offset - 1)).isoformat()

        today_scores = _get_scores_on_date(score_date)
        tomorrow_scores = _get_scores_on_date(next_date)

        if not today_scores or not tomorrow_scores:
            continue

        # 计算每个维度的 rank IC
        for dim in DIMENSION_KEYS:
            pairs = []
            for code in today_scores:
                if code not in tomorrow_scores:
                    continue
                dim_score = today_scores[code].get(dim)
                next_snap = tomorrow_scores[code]
                # 从 details JSON 提取 change_pct
                try:
                    details = json.loads(next_snap.get("details", "{}"))
                except (json.JSONDecodeError, TypeError):
                    details = {}
                actual_return = details.get("change_pct", 0)
                if dim_score is not None and actual_return is not None:
                    pairs.append((dim_score, actual_return))

            if len(pairs) < 3:
                continue

            # Spearman rank correlation
            from scipy.stats import spearmanr
            try:
                x = [p[0] for p in pairs]
                y = [p[1] for p in pairs]
                # 跳过常量数组（spearmanr 无法计算关联系数）
                if len(set(x)) < 2 or len(set(y)) < 2:
                    continue
                ic, _ = spearmanr(x, y)
                if ic is not None and not (isinstance(ic, float) and (ic != ic)):  # not NaN
                    dim_ic_sums[dim] += ic
                    dim_ic_counts[dim] += 1
            except ImportError:
                # 无 scipy 时降级为简化计算
                # 用正负号一致率替代 IC
                hits, total = 0, 0
                for p in pairs:
                    if p[0] > 50 and p[1] > 0:
                        hits += 1
                    elif p[0] < 50 and p[1] < 0:
                        hits += 1
                    total += 1
                simple_ic = (hits / total * 2 - 1) if total > 0 else 0
                dim_ic_sums[dim] += simple_ic
                dim_ic_counts[dim] += 1
            except Exception:
                pass

    # 均值
    result = {}
    for dim in DIMENSION_KEYS:
        if dim_ic_counts[dim] > 0:
            result[dim] = round(dim_ic_sums[dim] / dim_ic_counts[dim], 4)
        else:
            result[dim] = 0.0

    return result


# -----------------------------------------------------------------
# 反思生成
# -----------------------------------------------------------------

def generate_reflection(code: str) -> dict:
    """
    对单只标的生成反思：
    - 获取最近评分趋势
    - 比对实际收益
    - 分析哪些维度有效
    - 输出反思文本
    """
    scores = _get_latest_scores()
    latest = next((s for s in scores if s["code"] == code), None)
    if not latest:
        return {"code": code, "error": "无评分数据"}

    # 最近3天反思
    reflections = get_reflections(code, days=3)
    recent_returns = []
    for r in reflections:
        if r.get("actual_return_1d") is not None:
            recent_returns.append(r["actual_return_1d"])

    # 维度IC
    dim_ic = compute_dimension_ic(days=10)
    dim_ic_filtered = {k: v for k, v in dim_ic.items() if abs(v) > 0.05}

    # 生成反思文本
    lines = []
    name = STOCK_MAP.get(code, {}).get("name", code)
    lines.append(f"📊 {name}({code}) 反思报告")

    total = latest.get("total_score", 0)
    lines.append(f"当前评分: {total:.1f}")

    if recent_returns:
        avg_ret = sum(recent_returns) / len(recent_returns)
        lines.append(f"近3日实际收益均值: {avg_ret:+.2f}%")
        if avg_ret > 0:
            lines.append("✅ 评分方向正确")
        else:
            lines.append("⚠️ 评分方向偏误，需纠偏")

    if dim_ic_filtered:
        lines.append(f"\n全市场有效维度(IC>|0.05|):")
        for dim, ic in sorted(dim_ic_filtered.items(), key=lambda x: -abs(x[1])):
            icon = "🟢" if ic > 0 else "🔴"
            lines.append(f"  {icon} {dim}: IC={ic:.3f}")

    reflection_text = "\n".join(lines)

    return {
        "code": code,
        "name": name,
        "total_score": total,
        "dimension_scores": {
            "base_score": latest.get("base_score", 0),
            "zone_score": latest.get("zone_score", 0),
            "momentum_score": latest.get("momentum_score", 0),
            "volume_score": latest.get("volume_score", 0),
            "serenity_score": latest.get("serenity_score", 0),
            "factor_score": latest.get("factor_score", 0),
            "technical_score": latest.get("technical_score", 0),
            "sentiment_score": latest.get("sentiment_score", 0),
        },
        "dimension_ic": dim_ic_filtered,
        "reflection_text": reflection_text,
    }


def generate_all_reflections() -> list[dict]:
    """对所有标的生成反思并入库"""
    today = date.today().isoformat()
    results = []
    for code in ALL_CODES:
        try:
            ref = generate_reflection(code)
            if "error" not in ref:
                ref["date"] = today
                # 预测方向
                total_score = ref.get("total_score", 50)
                if total_score >= 72:
                    ref["predicted_direction"] = "BUY"
                elif total_score < 42:
                    ref["predicted_direction"] = "SELL"
                else:
                    ref["predicted_direction"] = "HOLD"

                save_reflection(code, ref)
                results.append(ref)
        except Exception as e:
            print(f"⚠️ {code} 反思生成失败: {e}")
    return results


# -----------------------------------------------------------------
# 结果补填
# -----------------------------------------------------------------

def fill_outcomes(days_back: int = 10):
    """补填尚未完成的实际收益"""
    today = date.today()
    unfilled = get_unfilled_reflections(since_days=days_back)

    for ref in unfilled:
        code = ref["code"]
        ref_date = ref["date"]
        ref_id = ref["id"]

        # 获取 ref_date 前后共 60 天的价格历史
        prices = get_price_history(code, days=60)
        # 按日期正序排列
        prices_sorted = sorted(prices, key=lambda p: p["date"])
        
        # 找到 ref_date 对应的索引
        ref_idx = None
        for i, p in enumerate(prices_sorted):
            if p["date"] >= ref_date:
                ref_idx = i
                break
        
        if ref_idx is None:
            continue
        
        ref_price = prices_sorted[ref_idx].get("close", 0)
        if ref_price <= 0:
            continue

        updates = {}
        for offset_days, key in [(1, "actual_return_1d"), (3, "actual_return_3d"), (5, "actual_return_5d")]:
            target_idx = ref_idx + offset_days
            if target_idx < len(prices_sorted):
                future_close = prices_sorted[target_idx].get("close", 0)
                if future_close and future_close > 0:
                    ret = (future_close - ref_price) / ref_price * 100
                    updates[key] = round(ret, 2)

        if updates:
            update_reflection_outcome(code, ref_date, **updates)
            print(f"✅ {code} {ref_date}: {updates}")


# -----------------------------------------------------------------
# 权重调整建议
# -----------------------------------------------------------------

def suggest_weight_adjustments(days: int = 20) -> dict:
    """
    基于维度IC生成权重调整建议。
    
    IC 为正 → 建议上调（最多 +50%）
    IC 为负 → 建议下调（最多 -50%）
    """
    from weight_adjuster import DEFAULT_WEIGHTS, IC_TO_WEIGHT

    dim_ic = get_reflection_dimension_ic(days=days)
    if not dim_ic:
        print("⚠️ 维度IC数据不足，无法建议调整")
        return {}

    suggestions = {}
    for ic_dim, weight_key in IC_TO_WEIGHT.items():
        ic_val = dim_ic.get(ic_dim, 0.0)
        factor = 1.0 + ic_val * 1.667
        factor = max(0.5, min(1.5, factor))
        new_weight = round(DEFAULT_WEIGHTS[weight_key] * factor, 4)
        suggestions[weight_key] = {
            "current": DEFAULT_WEIGHTS[weight_key],
            "suggested": new_weight,
            "ic": ic_val,
            "change_pct": round((new_weight - DEFAULT_WEIGHTS[weight_key]) / DEFAULT_WEIGHTS[weight_key] * 100, 1),
        }

    # 归一化建议权重
    total = sum(s["suggested"] for s in suggestions.values())
    if total > 0:
        for k in suggestions:
            suggestions[k]["suggested_normalized"] = round(suggestions[k]["suggested"] / total, 4)

    return suggestions


# -----------------------------------------------------------------
# CLI
# -----------------------------------------------------------------

def show_reflections(days: int = 7):
    """显示近期反思记录"""
    refs = get_reflections(days=days)
    if not refs:
        print("📭 暂无反思记录")
        return

    print(f"📊 反思学习环 — 最近 {days} 天")
    print("=" * 80)
    for r in refs[:20]:
        name = r.get("name", r["code"])
        actual = r.get("actual_return_1d", "?")
        actual_str = f"{actual:+.1f}%" if isinstance(actual, (int, float)) else "?"
        pred = r.get("predicted_direction", "?")
        icon = "✅" if (pred == "BUY" and isinstance(actual, (int, float)) and actual > 0) or \
                      (pred == "SELL" and isinstance(actual, (int, float)) and actual < 0) else "❌"
        print(f"{icon} {name:6s} | {r['date']} | 评分{r['total_score']:.0f} | "
              f"预测{pred:<4} | 实际{actual_str}")
    print("=" * 80)


def show_dimension_ic(days: int = 20):
    """显示维度IC报告"""
    print(f"📊 全市场维度 IC 报告 — 最近 {days} 天")
    print("=" * 70)
    dim_ic = compute_dimension_ic(days=days)
    print(f"  {'维度':<20} {'IC':>8} {'有效性':>10}")
    print(f"  {'─'*40}")
    for dim in DIMENSION_KEYS:
        ic = dim_ic.get(dim, 0.0)
        bar = "🟢" if ic > 0.05 else ("🔴" if ic < -0.05 else "⚪")
        label = "有效" if abs(ic) > 0.05 else "弱"
        print(f"  {dim:<20} {ic:>+8.4f} {bar} {label:>6}")
    print("=" * 70)
    print()

    suggestions = suggest_weight_adjustments(days=days)
    if suggestions:
        print(f"📊 权重调整建议:（基于 Reflection IC）")
        print(f"  {'维度':<12} {'当前':>8} {'建议':>8} {'IC':>8} {'变化':>10}")
        print(f"  {'─'*50}")
        from weight_adjuster import DEFAULT_WEIGHTS
        for k in DEFAULT_WEIGHTS:
            s = suggestions.get(k, {})
            cur = s.get("current", 0)
            sug = s.get("suggested_normalized", s.get("suggested", cur))
            ic = s.get("ic", 0.0)
            delta = sug - cur
            arrow = "🟢+" if delta > 0.005 else ("🔴" if delta < -0.005 else "⚪ ")
            print(f"  {k:<12} {cur:>7.1%} {sug:>7.1%} {ic:>+8.4f} {arrow}{delta:>+.1%}")


def apply_reflection_adjustments(days: int = 20):
    """应用反思建议 → 写入 adjusted_weights.json"""
    from weight_adjuster import save_adjusted_weights, DEFAULT_WEIGHTS

    suggestions = suggest_weight_adjustments(days=days)
    if not suggestions:
        print("⚠️ 无足够数据，跳过调整")
        return

    new_weights = {}
    for k in DEFAULT_WEIGHTS:
        s = suggestions.get(k, {})
        new_weights[k] = s.get("suggested_normalized", s.get("suggested", DEFAULT_WEIGHTS[k]))

    # 归一化
    total = sum(new_weights.values())
    if total > 0:
        new_weights = {k: round(v / total, 4) for k, v in new_weights.items()}
        diff = round(1.0 - sum(new_weights.values()), 4)
        if diff != 0:
            max_key = max(new_weights, key=new_weights.get)
            new_weights[max_key] = round(new_weights[max_key] + diff, 4)

    save_adjusted_weights(new_weights)
    print("✅ 反思权重已应用")
    show_dimension_ic(days=days)


def main():
    if "--fill-outcomes" in sys.argv:
        days = 10
        if "--days" in sys.argv:
            try:
                idx = sys.argv.index("--days")
                days = int(sys.argv[idx + 1])
            except (ValueError, IndexError):
                pass
        print(f"🔍 补填最近 {days} 天的反思收益...")
        fill_outcomes(days_back=days)
    elif "--show" in sys.argv:
        days = 7
        if "--days" in sys.argv:
            try:
                idx = sys.argv.index("--days")
                days = int(sys.argv[idx + 1])
            except (ValueError, IndexError):
                pass
        show_reflections(days=days)
    elif "--ic" in sys.argv:
        days = 20
        if "--days" in sys.argv:
            try:
                idx = sys.argv.index("--days")
                days = int(sys.argv[idx + 1])
            except (ValueError, IndexError):
                pass
        show_dimension_ic(days=days)
    elif "--apply" in sys.argv:
        days = 20
        if "--days" in sys.argv:
            try:
                idx = sys.argv.index("--days")
                days = int(sys.argv[idx + 1])
            except (ValueError, IndexError):
                pass
        apply_reflection_adjustments(days=days)
    else:
        # 默认：生成今日反思
        print("🧠 生成今日评分反思...")
        results = generate_all_reflections()
        show_reflections(days=1)


if __name__ == "__main__":
    main()
