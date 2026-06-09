"""
discount_analyzer.py — 深度折扣诊断引擎

检测评分分化的标的的总评分 vs Serenity匹配度的差异，
分析分化原因（哪些维度在拉动/拖累），
结合Conviction给出买入/放弃建议。
"""

from datetime import date
from typing import Optional


def analyze_discount(code: str) -> str:
    """对单个标的进行深度折扣诊断"""
    today = date.today().isoformat()
    
    # 1. 获取最新评分
    from db import get_latest_scores, load_all_stocks, get_snapshots
    scores = get_latest_scores()
    score_row = next((s for s in scores if s["code"] == code), None)
    if not score_row:
        return f"❌ {code} 无评分数据"
    
    stock = next((s for s in load_all_stocks() if s["code"] == code), None)
    snaps = get_snapshots(code, 10)
    
    name = stock["name"] if stock else code
    
    lines = []
    lines.append(f"🔍 **{name}({code}) 深度折扣诊断 | {today}**")
    lines.append("")
    
    total = score_row.get("total_score", 0)
    serenity = score_row.get("serenity_score", 50)
    moat = score_row.get("moat_score", 50)
    factor = score_row.get("factor_score", 50)
    technical = score_row.get("technical_score", 50)
    sentiment = score_row.get("sentiment_score", 50)
    zone = score_row.get("zone_score", 50)
    
    # 2. 维度贡献分析
    lines.append("📊 **九维评分拆解**")
    gap = total - serenity  # 总评分 vs Serenity匹配度的差距
    dims = [
        ("Serenity匹配", serenity, 0),
        ("护城河", moat, 0),
        ("因子引擎", factor, 0),
        ("技术面", technical, 0),
        ("情绪", sentiment, 0),
        ("价格位置", zone, 0),
    ]
    
    for label, val, _ in dims:
        if val >= 75:
            emoji = "🟢"
        elif val >= 60:
            emoji = "🟡"
        elif val >= 40:
            emoji = "🔴"
        else:
            emoji = "⛔"
        lines.append(f"  {emoji} {label:<12} {val:>5.0f}/100")
    
    lines.append("")
    lines.append(f"📌 **核心矛盾**：总评分 {total:.0f} vs Serenity匹配 {serenity:.0f}，差距 **{gap:+.0f}分**")
    
    # 3. 找拉动因素和拖累因素
    pullers = []
    for label, val, _ in dims:
        if val > total + 5 and label != "Serenity匹配":
            pullers.append((val, label))
    pullers.sort(reverse=True)
    
    drainers = []
    for label, val, _ in dims:
        if val < total - 10:
            drainers.append((val, label))
    drainers.sort()
    
    if pullers:
        lines.append(f"  🚀 **拉动因素**：{'、'.join(l for _,l in pullers)} — 评分高于均值，拉动总分")
    if drainers:
        lines.append(f"  🛑 **拖累因素**：{'、'.join(l for _,l in drainers)} — 评分显著低于均值，压低总分")
    
    # 4. 历史趋势
    if len(snaps) >= 3:
        closes = [s["close"] for s in reversed(snaps) if s.get("close")]
        if len(closes) >= 3:
            price_trend = (closes[-1] - closes[0]) / closes[0] * 100
            trend_str = f"过去10天价格 **{price_trend:+.1f}%**"
            if price_trend < -3:
                trend_str += " 📉 持续下跌中"
            elif price_trend > 3:
                trend_str += " 📈 持续上涨中"
            else:
                trend_str += " → 横盘震荡"
            lines.append(f"  📈 **价格趋势**：{trend_str}")
    
    # 5. 从scorer取细节
    details = score_row.get("details", {})
    if isinstance(details, str):
        import json
        try:
            details = json.loads(details)
        except (json.JSONDecodeError, TypeError):
            details = {}
    
    zone_label = details.get("zone_label", "")
    growth = details.get("growth", 0)
    target_sell = details.get("target_sell", 0)
    price = details.get("price", 0)
    rsi = details.get("tech_rsi", 50)
    bb_pos = details.get("tech_bb_pos", 50)
    ma5 = details.get("tech_ma5", 0)
    ma20 = details.get("tech_ma20", 0)
    chg_pct = details.get("change_pct", 0)
    
    lines.append("")
    lines.append(f"  🏷️ **位置标签**：{zone_label}")
    lines.append(f"  💰 **当前价格**：{price:.2f}")
    if target_sell:
        if total > serenity:
            upside = ((target_sell - price) / price * 100) if price else 0
            lines.append(f"  🎯 **目标价**：{target_sell:.0f}（潜在涨幅 {upside:+.1f}%）")
        else:
            lines.append(f"  🎯 **目标价**：{target_sell:.0f}")
    lines.append(f"  📊 **RSI**：{rsi:.0f} | **布林位置**：{bb_pos:.0f}%")
    
    # 6. 综合研判
    lines.append("")
    lines.append("🧠 **综合研判**")

    # 判断标的类型
    from config import TIER_4_CODES
    is_t4_defensive = code in TIER_4_CODES

    # 非防御组合的分化判断变量
    is_value_trap = gap > 20 and (serenity < 30 or (moat < 50 and technical < 40))
    is_deep_cycle = zone >= 80 and factor < 50 and serenity < 35
    is_opportunity = gap > 15 and zone >= 70 and moat >= 55

    # 衡量是机会还是陷阱 — T4 防御组合用宽松标准
    if is_t4_defensive:
        # 防御组合：Serenity 已补偿，主要看护城河和位置
        if moat >= 70 and zone >= 70:
            lines.append("  ✅ **防御价值显现** — 高护城河+深度价格位置，弱势市场避风港")
            lines.append(f"    Serenity匹配度已补偿至{serenity:.0f}分，护城河{moat:.0f}分")
            lines.append("    建议：震荡市可小仓位配置，作为组合防御层")
        elif moat >= 60 and zone >= 70:
            lines.append("  🟡 **周期底部特征** — 护城河中上+深度折扣，但整体偏周期")
            lines.append("    建议：耐心等待催化剂（政策/涨价/分红公告）")
        else:
            lines.append("  ⚠️ **防御标的评分分化** — 虽有补偿但护城河和位置未达买入标准")
            lines.append("    建议：暂时观望")
    elif is_value_trap:
        lines.append("  ❌ **估值陷阱信号强烈** — Serenity极度不匹配，建议回避")
        if moat < 50:
            lines.append("    护城河评分偏低，说明基本面存在隐忧，非短期折价能解释")
        lines.append("    建议：等待Serenity匹配度回升至40以上再考虑")
    elif is_deep_cycle:
        lines.append("  🔄 **周期底部特征** — 处于深度折扣+因子偏弱，但位置极佳")
        lines.append("    这是顺周期股在行业低谷的典型表现")
        lines.append("    建议：小仓位左侧布局，等待催化剂（水泥涨价/基建政策）")
    elif is_opportunity:
        lines.append("  ✅ **可能是低估机会** — Serenity不匹配但其他维度认可")
        lines.append(f"    情绪和价格位置同步积极，护城河中等偏上（{moat:.0f}分）")
        lines.append("    建议：纳入观察，等Serenity匹配度升至40+再建仓")
    else:
        # 一般分化，给出客观判断
        if gap > 15:
            lines.append("  ⚠️ **评分显著分化** — 多方因子存在分歧")
            if sentiment >= 75 and technical < 50:
                lines.append("    情绪乐观但技术面偏弱，短期反弹潜力有限")
            elif zone >= 80:
                lines.append("    位置好但基本面不配合，需等待催化剂")
            lines.append("    建议：保持关注，重检基本面改善信号")
        else:
            lines.append("  ⚪ **评分相对一致** — 分化在合理范围内")
            lines.append("    建议：根据综合评分执行常规操作")
    
    # 7. Conviction 联动
    lines.append("")
    try:
        from db import get_latest_conviction
        cv = get_latest_conviction()
        if cv and cv.get("date") == today:
            regime = cv.get("regime", "震荡")
            pos_advice = cv.get("position_advice", "")
            regime_emoji = {"强势": "🟢", "震荡": "🟡", "弱势": "🔴"}.get(regime, "⚪")
            lines.append(f"📋 **Conviction联动**")
            lines.append(f"  {regime_emoji} 市场状态：{regime}")
            if pos_advice:
                lines.append(f"  {pos_advice}")
    except Exception:
        pass
    
    lines.append("")
    lines.append(f"> {today} · 数据：Serenity Monitor")
    
    return "\n".join(lines)
