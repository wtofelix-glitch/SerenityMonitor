"""
每日收盘简报生成器
15:30 收盘后调用，仅推送当前持仓标的的收盘简报
自动触发多因子评分（后台分析候选标的）
"""
from datetime import date
from typing import Optional

from data_engine import get_all_today_snapshots
from config import ALL_CODES
from db import save_snapshot, load_all_stocks, get_snapshots, save_price_history
from price_alert import check_alerts
from scorer import score_all
from notifier import push_daily_report, push_signal_summary
from quant_fusion import build_quantdinger_consensus


def _format_position_profit(current_price: float, buy_price: float) -> tuple[str, str, Optional[float]]:
    """Format holding PnL text; negative cost means prior gains covered cost."""
    if buy_price > 0:
        profit = (current_price - buy_price) / buy_price * 100
        emoji = "🔴" if profit >= 0 else "🟢"  # A股：红色涨/绿色跌
        return emoji, f"买入 {buy_price:.2f} | 盈亏 {profit:+.2f}%", profit
    if buy_price < 0:
        return "🔴", f"免费仓/净成本 {buy_price:.2f} | 现价 {current_price:.2f}", None
    return "⚪", "未记录成本 | 盈亏暂不可算", None


def _qd_decision_label(decision: str) -> str:
    return {
        "BUY": "偏进攻",
        "WATCH": "观察",
        "REDUCE": "降风险",
        "NO_DATA": "无数据",
    }.get(decision, decision or "观察")


def _format_qd_signal_line(label: str, items: list[dict], empty_text: str = "") -> str | None:
    sliced = items[:3]
    if not sliced:
        return f"  {label}: {empty_text}" if empty_text else None
    body = " | ".join(
        f"{item.get('name') or item['code']} {item['consensus_score']:.1f}/信{item['confidence']}"
        for item in sliced
    )
    return f"  {label}: {body}"


def _format_quantdinger_consensus_lines() -> list[str]:
    """格式化 QuantDinger 客观共识，日报只读展示，不改变仓位建议。"""
    try:
        consensus = build_quantdinger_consensus(limit=5)
    except Exception:
        return []

    if not consensus.get("latest_date"):
        return []

    lines = [
        "🧭 **QuantDinger 客观共识（只读闸门）**",
        (
            f"  全局 {consensus['universe_score']:.1f}"
            f"（{_qd_decision_label(consensus['universe_decision'])}）"
            f" | 质量 {consensus['quality_multiplier']:.2f}"
            f" | 一致 {consensus['agreement_ratio']:.2f}"
            f" | 覆盖 {consensus['coverage']}"
        ),
    ]

    opportunity_line = _format_qd_signal_line("🔴机会", consensus.get("top_opportunities", []))
    if opportunity_line:
        lines.append(opportunity_line)
    lines.append(_format_qd_signal_line(
        "🟢风险",
        consensus.get("risk_flags", []),
        "暂无多周期强降风险标的",
    ))

    lines.append("")
    return lines


def generate_daily_report() -> str:
    """
    生成今日收盘简报（仅推送持仓标的）
    保存当日数据到数据库
    自动检查价格预警
    自动触发多因子评分（含Serenity框架匹配度）
    """
    today = date.today().isoformat()
    snapshots = get_all_today_snapshots()

    # 保存到数据库 + 存储行情历史
    for snap in snapshots:
        save_snapshot(snap["code"], {
            "date": today,
            "open": snap.get("open"),
            "close": snap.get("close"),
            "high": snap.get("high"),
            "low": snap.get("low"),
            "volume": snap.get("volume"),
            "amount": snap.get("amount"),
            "change_pct": snap.get("change_pct"),
        })
        save_price_history(snap["code"], {
            "code": snap["code"], "date": today,
            "open": snap.get("open"), "close": snap.get("close"),
            "high": snap.get("high"), "low": snap.get("low"),
            "volume": snap.get("volume"), "change_pct": snap.get("change_pct"),
        })

    # 后台执行多因子评分（含Serenity框架匹配度）
    scores = score_all()

    # 检查预警（只对持仓标的）
    alerts = check_alerts()

    # 获取持仓状态
    stocks = load_all_stocks()
    active = [s for s in stocks if s["is_active"]]

    lines = []
    lines.append(f"📊 **Serenity Monitor | {today} 收盘简报**")
    lines.append("")

    # ====== 预警区 ======
    if alerts:
        lines.append("🚨 **价格预警**")
        for a in alerts:
            lines.append(f"- {a['msg']}")
        lines.append("")

    # ====== 候选评分排名 ======
    lines.append("🏆 **今日评分排名（含Serenity框架匹配度）**")
    for r in scores[:4]:
        rank_emoji = {1: "🥇", 2: "🥈", 3: "🥉"}.get(r["rank"], f"{r['rank']}.")
        serenity_str = f" | Serenity匹配 {r.get('serenity_score', 0):.0f}分" if r.get("serenity_score") else ""
        trap_str = f" 陷阱{r.get('uzi_trap_count', 0)}" if r.get("uzi_trap_count", 0) else ""
        uzi_str = f" | UZI {r.get('uzi_score', 0):.0f}/{r.get('uzi_rating', '-')}{trap_str}" if "uzi_score" in r else ""
        lines.append(f"{rank_emoji} {r['name']} {r['total_score']:.0f}分{serenity_str}{uzi_str} | {r['zone_label']}")
    lines.append("")

    lines.extend(_format_quantdinger_consensus_lines())

    # ====== 因子信号矩阵 ======
    try:
        from factor_engine import get_current_signals
        from weight_adjuster import load_adjusted_weights

        lines.append("📊 **因子信号矩阵**")
        factor_results = get_current_signals()
        if factor_results:
            held_codes = {a["code"] for a in active} if active else set()
            # 按综合信号排序，取前3
            factor_results.sort(key=lambda x: x["signal"], reverse=True)
            lines.append(f"  🟢最强: {factor_results[0]['name']}({factor_results[0]['signal']:+.3f})"
                         f" | {factor_results[1]['name']}({factor_results[1]['signal']:+.3f})"
                         f" | {factor_results[2]['name']}({factor_results[2]['signal']:+.3f})")
            # 最弱3个（倒序）
            weakest = sorted(factor_results, key=lambda x: x["signal"])[:3]
            lines.append(f"  🔴最弱: {weakest[0]['name']}({weakest[0]['signal']:+.3f})"
                         f" | {weakest[1]['name']}({weakest[1]['signal']:+.3f})"
                         f" | {weakest[2]['name']}({weakest[2]['signal']:+.3f})")
            # 持仓标的的因子信号
            if held_codes:
                held_signals = [r for r in factor_results if r["code"] in held_codes]
                if held_signals:
                    held_str = " | ".join(f'{r["name"]}{r["signal"]:+.2f}' for r in held_signals)
                    lines.append(f"  ⭐持仓: {held_str}")
        lines.append("")

        # ====== 动态权重 ======
        w = load_adjusted_weights()
        if w:
            from scorer import score_weight as default_w
            # 挑变化明显的
            changes = {k: w[k] - default_w.get(k, 0.15) for k in w}
            significant = {k: v for k, v in changes.items() if abs(v) > 0.005}
            if significant:
                lines.append("⚖️ **动态权重**")
                parts = []
                for k, delta in sorted(significant.items(), key=lambda x: -abs(x[1])):
                    arrow = "🟢" if delta > 0 else "🔴"
                    parts.append(f"{arrow}{k}{delta:+.1%}")
                if parts:
                    lines.append(f"  {' | '.join(parts)}")
                lines.append("")
    except Exception:
        pass

    # ====== AI因子解读 ======
    try:
        from factor_interpreter import interpret_all
        # 收集持仓和候选的因子数据
        held_factors = []
        other_factors = []
        for r in factor_results:
            snap = next((s for s in snapshots if s["code"] == r["code"]), {})
            item = {
                "name": r["name"],
                "code": r["code"],
                "factors": r.get("factors", {}),
                "signal": r.get("signal", 0),
                "change_pct": snap.get("change_pct", 0),
            }
            if active and r["code"] in {a["code"] for a in active}:
                held_factors.append(item)
            else:
                other_factors.append(item)

        interp_text = interpret_all(held_factors, other_factors)
        if interp_text.strip():
            lines.append("🧠 **AI因子解读**")
            lines.append(interp_text)
            lines.append("")
    except Exception:
        pass

    # ====== 持仓标的简报（核心内容）======
    if not active:
        lines.append("📭 **当前无持仓**")
        lines.append("")
        lines.append("💡 候选标的行情可通过仪表盘查看：")
        for s in snapshots:
            if s["code"] in ALL_CODES:
                emoji = "🔴" if s["change_pct"] >= 0 else "🟢"  # A股：红色涨/绿色跌
                lines.append(f"{emoji} {s['name']} ({s['code']}) → {s['close']:.2f} ({s['change_pct']:+.2f}%)")
        lines.append("")
        lines.append("> 建议关注买入机会")
    else:
        lines.append(f"⭐ **当前持仓 ({len(active)} 只)**")
        lines.append("")
        for s in snapshots:
            if s["code"] in {a["code"] for a in active}:
                stock_info = next((st for st in active if st["code"] == s["code"]), None)
                buy_price = stock_info["buy_price"] if stock_info else 0
                trade_amount = stock_info.get("trade_amount", 0) if stock_info else 0
                emoji, profit_line, profit = _format_position_profit(s["close"], buy_price)

                lines.append(f"{emoji} **{s['name']}** ({s['code']})")
                lines.append(f"  收盘 {s['close']:.2f} | 今日 {s['change_pct']:+.2f}%")
                lines.append(f"  {profit_line}")
                if trade_amount and profit is not None:
                    curr_value = trade_amount * (1 + profit / 100)
                    lines.append(f"  持仓 {trade_amount:.0f}元 → 现 {curr_value:.0f}元")
                elif trade_amount:
                    lines.append(f"  净投入 {trade_amount:.0f}元（负数表示已回本）")
                if stock_info and stock_info.get("target_high", 0):
                    target = stock_info["target_high"]
                    remain = (target - s["close"]) / s["close"] * 100
                    lines.append(f"  目标 {target:.2f} | 还需 {remain:+.1f}%")
                lines.append("")

    # ====== 操作建议 ======
    lines.append("💡 **操作建议**")
    if alerts:
        for a in alerts:
            if a["type"] == "target_high":
                lines.append(f"- 🟢 **{a['code']}** 已达目标价，建议卖出")
            elif a["type"] == "stop_loss":
                lines.append(f"- 🔴 **{a['code']}** 触发止损，建议执行止损")
    elif active:
        lines.append("- 持仓中，暂无预警触发")
        lines.append("- 保持观察等待目标价")
    else:
        lines.append("- 空仓观望，等待合适买点")
        lines.append("- 参考候选标的行情，择机建仓")

    # ====== 权重辩论 + 市场状态 ======
    try:
        from conviction_cli import _run_conviction_analysis
        conviction_text = _run_conviction_analysis()
        # 只取关键摘要行（图表区域压缩）
        conv_lines = conviction_text.split("\n")
        regime_line = [l for l in conv_lines if "市场状态:" in l]
        weight_lines = []
        for l in conv_lines:
            if "护城河" in l or "情绪" in l or "动量" in l:
                weight_lines.append(l.strip())
        # 仓位建议
        advice_line = [l.strip() for l in conv_lines if "黄色" in l or "仓位≤" in l or "仓位" in l and "建议" in l]
        
        lines.append("⚖️ **权重辩论摘要**")
        if regime_line:
            lines.append(f"  {regime_line[0].strip()}")
        for wl in weight_lines[:3]:  # 只取3条变化明显的
            lines.append(f"  {wl}")
        # 仓位建议
        if "📋 **仓位建议**" in conviction_text:
            # 找到仓位建议下方几行
            advice_start = False
            for l in conv_lines:
                if "📋 **仓位建议**" in l:
                    advice_start = True
                    continue
                if advice_start and l.strip() and not l.startswith("建议："):
                    lines.append(f"  {l.strip()}")
                    break
        lines.append("")
    except Exception:
        pass

    # ====== 大师智慧信号 ======
    try:
        from guru_wisdom import generate_report as _guru_report, get_recent_quotes, status as _guru_status
        _guru_stats = _guru_status()
        if _guru_stats["total_quotes"] > 0:
            lines.append("📜 **大师智慧信号**")
            # 近7天有新增则显示语录，否则显示统计
            if _guru_stats["recent_quotes_7d"] > 0:
                _recent = get_recent_quotes(3)
                for _q in _recent:
                    _emoji = {"bullish": "🟢", "bearish": "🔴", "neutral": "⚪"}.get(_q.get("sentiment", "neutral"), "⚪")
                    lines.append(f"  {_emoji} {_q['cn_name']}: {_q['content'][:50]}")
            lines.append(f"  📊 共 {_guru_stats['total_quotes']} 条语录 | {_guru_stats['gurus']} 位大师")
            lines.append("")
    except Exception:
        pass

    # ====== 信号汇总 ======
    try:
        from signal_engine import generate_signals
        from portfolio import get_portfolio
        signals = generate_signals(portfolio=get_portfolio())
        buy_signals = [s for s in signals if s.get("action") in ("STRONG_BUY", "BUY")]
        tp_signals = [s for s in signals if s.get("action") == "TAKE_PROFIT"]
        sell_signals = [s for s in signals if s.get("action") in ("SELL", "STOP_LOSS")]
        if buy_signals:
            lines.append("🟢 **买入信号**")
            for s in buy_signals[:3]:
                lines.append(f"  - {s['name']}({s['code']}) 评分 {s['total_score']:.0f}")
            lines.append("")
        if tp_signals:
            lines.append("🟢💰 **止盈提示**（持仓盈利达标）")
            for s in tp_signals[:3]:
                lines.append(f"  - {s['name']}({s['code']}) 当前评分 {s['total_score']:.0f}")
            lines.append("")
        if sell_signals:
            lines.append("🔴 **止损/卖出信号**")
            for s in sell_signals[:3]:
                lines.append(f"  - {s['name']}({s['code']}) 评分 {s['total_score']:.0f}")
            lines.append("")
    except Exception:
        pass

    lines.append("")
    lines.append(f"> ⏰ {today} 15:30 · 数据来源：新浪财经")

    report = "\n".join(lines)

    # 尝试微信推送
    try:
        push_daily_report(report)
        # 推送信号摘要
        from signal_engine import generate_signals
        from portfolio import get_portfolio
        signals = generate_signals(portfolio=get_portfolio())
        push_signal_summary(signals)
    except Exception as e:
        import logging
        logging.getLogger("Serenity.DailyReport").warning(f"推送失败: {e}")

    return report


def generate_simple_status() -> str:
    """CLI status 使用"""
    snapshots = get_all_today_snapshots()
    stocks = load_all_stocks()
    active = [s for s in stocks if s["is_active"]]

    lines = []
    lines.append("Serenity Monitor — 行情速览")
    lines.append("=" * 40)

    if active:
        lines.append("\n【当前持仓】")
        for s in snapshots:
            if s["code"] in {a["code"] for a in active}:
                st = next((x for x in active if x["code"] == s["code"]), None)
                buy_price = st["buy_price"] if st else 0
                _, profit_line, _ = _format_position_profit(s["close"], buy_price)
                lines.append(f"  {s['name']} ({s['code']})")
                lines.append(f"    价 {s['close']:.2f} | {s['change_pct']:+.2f}%")
                lines.append(f"    {profit_line}")
                if st and st.get("target_high"):
                    remain = (st["target_high"] - s["close"]) / s["close"] * 100
                    lines.append(f"    距目标 {st['target_high']:.2f} 还有 {remain:.1f}%")

    lines.append("\n【候选标的】")
    for s in snapshots:
        if s["code"] not in {a["code"] for a in active}:
            lines.append(f"  {s['name']}: {s['close']:.2f} ({s['change_pct']:+.2f}%)")

    report = "\n".join(lines)

    # 尝试微信推送
    try:
        push_daily_report(report)
        # 推送信号摘要
        from signal_engine import generate_signals
        from portfolio import get_portfolio
        signals = generate_signals(portfolio=get_portfolio())
        push_signal_summary(signals)
    except Exception as e:
        import logging
        logging.getLogger("Serenity.DailyReport").warning(f"推送失败: {e}")

    return report
