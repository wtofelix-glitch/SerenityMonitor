"""
持仓复盘教练 — 每周自动分析交易得失, 提炼可复用教训
"""
import json, os, sys
from datetime import date, datetime, timedelta
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import get_conn
from config import STOCK_MAP
from serenity_logger import get_logger
log = get_logger(__name__)

def analyze_week() -> dict:
    """分析本周所有交易, 提炼教训"""
    conn = get_conn()
    week_ago = (date.today() - timedelta(days=7)).isoformat()
    trades = conn.execute("""
        SELECT t.*, s.name as stock_name FROM trades t
        LEFT JOIN stocks s ON s.code = t.code
        WHERE t.date >= ? AND t.code != 'CASH'
        ORDER BY t.date, t.rowid
    """, (week_ago,)).fetchall()
    conn.close()
    if not trades:
        return {"trades": 0, "lessons": [], "summary": "本周无交易"}

    # 按标的归组, 配对买入→卖出
    by_code = defaultdict(list)
    for t in trades:
        by_code[t["code"]].append(dict(t))

    lessons = []
    stats = {"buys": 0, "sells": 0, "wins": 0, "losses": 0, "total_pnl": 0.0}

    for code, tlist in by_code.items():
        buys = [t for t in tlist if t["action"] == "buy"]
        sells = [t for t in tlist if t["action"] == "sell"]
        stats["buys"] += len(buys)
        stats["sells"] += len(sells)

        for sell in sells:
            # 找最近的买入
            prev_buys = [b for b in buys if b["date"] <= sell["date"]]
            if prev_buys:
                buy = max(prev_buys, key=lambda b: b["date"])
                profit = (sell["price"] - buy["price"]) / buy["price"]
                stats["total_pnl"] += profit
                is_win = profit > 0
                if is_win: stats["wins"] += 1
                else: stats["losses"] += 1

                name = STOCK_MAP.get(code, {}).get("name", code)
                hold_days = (datetime.strptime(sell["date"], "%Y-%m-%d") - datetime.strptime(buy["date"], "%Y-%m-%d")).days

                lesson = {
                    "code": code, "name": name,
                    "buy_date": buy["date"], "buy_price": buy["price"],
                    "sell_date": sell["date"], "sell_price": sell["price"],
                    "profit_pct": round(profit * 100, 2),
                    "hold_days": hold_days,
                    "is_win": is_win,
                    "buy_reason": buy.get("note", "")[:80],
                    "sell_reason": sell.get("note", "")[:80],
                }

                # 自动提炼教训
                insights = []
                if is_win and profit > 0.05:
                    insights.append("✅ 盈利>5%: 卖出时机恰当, 继续使用当前止盈策略")
                elif is_win and profit < 0.02:
                    insights.append("⚠️ 小盈: 考虑是否过早止盈, 检查移动止盈参数")
                elif not is_win and profit > -0.05:
                    insights.append("⚠️ 小亏: 止损及时, 控制损失是关键")
                elif not is_win and profit < -0.05:
                    insights.append("🔴 大亏>5%: 检查是否止损设得太宽, 或入场信号过于激进")
                if hold_days < 3:
                    insights.append("📌 短线(<3天): 确认是基于信号还是情绪操作")
                elif hold_days > 20:
                    insights.append("📌 长线(>20天): 耐心持仓获得回报, 检查是否有更好的机会成本")
                lesson["insights"] = insights
                lessons.append(lesson)

    # 按盈亏排序
    lessons.sort(key=lambda l: l["profit_pct"], reverse=True)

    # 全局教训
    if stats["wins"] + stats["losses"] > 0:
        wr = stats["wins"] / (stats["wins"] + stats["losses"]) * 100
    else:
        wr = 0

    summary = f"本周 {stats['buys']}买 {stats['sells']}卖, 胜率 {wr:.0f}%, 均盈亏 {stats['total_pnl']/max(stats['buys']+stats['sells'],1)*100:+.1f}%"

    return {"trades": len(trades), "stats": stats, "win_rate": round(wr, 1),
            "lessons": lessons[:10], "summary": summary}

def coach_report() -> str:
    """生成可读的复盘报告"""
    r = analyze_week()
    if r["trades"] == 0:
        return "📝 持仓复盘教练: 本周无交易, 保持观察。"

    lines = [f"📝 **持仓复盘教练** — {(date.today() - timedelta(days=7)).strftime('%m/%d')}→{date.today().strftime('%m/%d')}"]
    lines.append(f"📊 {r['summary']}")
    lines.append("")

    for i, l in enumerate(r["lessons"][:8]):
        emoji = "🟢" if l["is_win"] else "🔴"
        lines.append(f"{emoji} **{l['name']}**({l['code']}) {l['profit_pct']:+.1f}% · {l['hold_days']}天")
        lines.append(f"   买 ¥{l['buy_price']:.2f} → 卖 ¥{l['sell_price']:.2f}")
        for insight in l["insights"]:
            lines.append(f"   {insight}")
        lines.append("")

    return "\n".join(lines)
