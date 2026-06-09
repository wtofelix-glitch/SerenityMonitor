"""
conviction_cli.py — 权重辩论 + 多周期共识的 CLI 接入层

不与 cli.py 主命令系统耦合，直接在 cli.py 顶部 import 后注册。
"""

from datetime import date


def _run_conviction_analysis() -> str:
    """运行完整的权重辩论 + 多周期共识分析（含持久化到 conviction_log）"""
    today = date.today().isoformat()
    
    # 1. 获取评分
    from scorer import score_all
    scores = score_all()
    if not scores:
        return "暂无评分数据"
    
    ranked = sorted(scores, key=lambda x: x["total_score"], reverse=True)
    for i, r in enumerate(ranked, 1):
        r["rank"] = i
    
    # 2. 权重辩论 + 持久化
    from conviction_engine import debate_weights, multi_cycle_consensus, run_and_save
    debated_weights, regime = debate_weights(scores)
    
    # 持久化到 DB
    conviction_result = run_and_save()
    
    # 3. 生成输出
    lines = []
    lines.append(f"⚖️ **Serenity 权重辩论 + 多周期共识 | {today}**")
    lines.append(f"市场状态: {regime}")
    lines.append("")
    
    # 辩论权重
    lines.append("📊 **辩论后因子权重**")
    lines.append(f"{'因子':<12} {'权重':>6} {'偏移':>8}  {'说明'}")
    lines.append("─" * 55)
    
    # 基准（震荡态）
    from conviction_engine import REGIME_WEIGHTS
    baseline = REGIME_WEIGHTS["震荡"]
    
    for k in ["base", "zone", "momentum", "volume", "serenity", "factor", "technical", "sentiment", "moat"]:
        w = debated_weights.get(k, 0)
        base = baseline.get(k, 0)
        delta = w - base
        names = {
            "base": "基本面", "zone": "价格位置", "momentum": "动量",
            "volume": "成交量", "serenity": "Serenity匹配", "factor": "因子引擎",
            "technical": "技术面", "sentiment": "情绪", "moat": "护城河"
        }
        arrows = {True: "⬆", False: "⬇"}.get(delta > 0.005, "→")
        if abs(delta) < 0.005: delta_str = "持平"
        else: delta_str = f"{arrows} {delta:+.1%}"
        lines.append(f"  {names.get(k, k):<10} {w:.1%}  {delta_str:>8}")
    lines.append("")
    
    # 多周期共识 TOP5
    lines.append("🔄 **多周期共识 TOP5**")
    lines.append(f"{'标的':<8} {'1日':>5} {'5日':>5} {'20日':>6} {'共识':>5} {'趋势':>4} {'置信':>4}")
    lines.append("─" * 45)
    
    for r in ranked[:5]:
        code = r.get("code", "")
        ts = r["total_score"]
        consensus = multi_cycle_consensus(code, ts, "medium")
        cs = consensus["consensus_score"]
        raw = consensus["raw_scores"]
        trend_emoji = {"up": "📈", "down": "📉", "flat": "→"}.get(consensus["trend"], "→")
        conf = consensus["confidence"]
        lines.append(f"  {r['name']:<6} {raw['daily']:>5.0f} {raw['weekly']:>5.0f} {raw['monthly']:>6.0f} {cs:>5.0f} {trend_emoji:>4} {conf:.0%}")
    lines.append("")
    
    # 持仓多周期
    from db import load_all_stocks
    stocks = load_all_stocks()
    active = [s for s in stocks if s.get("is_active")]
    
    if active:
        lines.append("⭐ **持仓多周期展望**")
        active_codes = {a["code"] for a in active}
        for r in ranked:
            if r.get("code") in active_codes:
                code = r.get("code", "")
                ts = r["total_score"]
                consensus = multi_cycle_consensus(code, ts, "short")
                cs = consensus["consensus_score"]
                trend_emoji = {"up": "📈", "down": "📉", "flat": "→"}.get(consensus["trend"], "→")
                conf = consensus["confidence"]
                lines.append(f"  {r['name']:<6} 今日{ts:.0f} | 共识{cs:.0f} | {trend_emoji} {consensus['trend']} | 置信{conf:.0%}")
                lines.append(f"          {consensus['detail']}")
        lines.append("")
    
    # 简短总结 + 仓位建议
    lines.append("**💡 综合判断**")
    avg_score = sum(r["total_score"] for r in ranked) / len(ranked)
    lines.append(f"今日全线 {len(ranked)} 只标的均分 {avg_score:.0f}，判为 **{regime}市场**。")
    lines.append(f"权重辩论后，护城河因子权重调整为 {debated_weights['moat']:.1%}（基准震荡态 {baseline['moat']:.1%}）")
    
    # 仓位建议
    if conviction_result.get("position_advice"):
        lines.append(f"\n📋 **仓位建议**")
        lines.append(f"  {conviction_result['position_advice']}")
    
    if regime == "弱势":
        lines.append("建议：控制仓位，偏重护城河型防御标的，等待右侧信号。")
    
    return "\n".join(lines)
