#!/usr/bin/env python3
"""
滑点追踪器 — 纸面交易 vs 实盘交易每日对比
输出: 滑点率、手续费差距、执行延迟、理论收益 vs 实际收益
"""
from datetime import date, datetime, timedelta
from serenity_logger import get_logger

log = get_logger(__name__)


def get_paper_trades() -> list[dict]:
    from db import get_conn
    conn = get_conn()
    rows = conn.execute(
        "SELECT code, action, price, quantity, date, trade_amount FROM paper_trades ORDER BY date DESC LIMIT 50"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_real_trades() -> list[dict]:
    from db import get_conn
    conn = get_conn()
    rows = conn.execute(
        "SELECT code, action, price, quantity, date, trade_amount FROM trades WHERE code!='CASH' ORDER BY date DESC LIMIT 50"
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        if "trade_amount" in d:
            d["amount"] = d.pop("trade_amount")
        result.append(d)
    return result


def compute_slippage() -> dict:
    """计算纸面 vs 实盘滑点"""
    paper = get_paper_trades()
    real = get_real_trades()

    # 按(code, date, action) 匹配
    paper_map = {}
    for t in paper:
        key = (t["code"], t["date"][:10], t["action"])
        paper_map.setdefault(key, []).append(t)

    real_map = {}
    for t in real:
        key = (t["code"], t["date"][:10], t["action"])
        real_map.setdefault(key, []).append(t)

    matched = []
    for key in paper_map:
        if key in real_map:
            p_trades = paper_map[key]
            r_trades = real_map[key]
            for p, r in zip(p_trades, r_trades):
                p_price = float(p.get("price", 0))
                r_price = float(r.get("price", 0))
                if p_price > 0 and r_price > 0:
                    slippage_pct = (r_price - p_price) / p_price * 100  # 正=实盘更贵(不利买入)
                    matched.append({
                        "code": key[0], "date": key[1], "action": key[2],
                        "paper_price": round(p_price, 2), "real_price": round(r_price, 2),
                        "slippage_pct": round(slippage_pct, 2),
                        "paper_shares": p.get("quantity", 0), "real_shares": r.get("quantity", 0),
                    })

    if not matched:
        return {"slippage_avg_pct": 0, "slippage_max_pct": 0, "matched_trades": 0, "details": []}

    slips = [m["slippage_pct"] for m in matched]
    avg_slip = sum(slips) / len(slips)
    max_slip = max(abs(s) for s in slips)
    buy_slips = [m["slippage_pct"] for m in matched if m["action"] == "buy"]
    sell_slips = [m["slippage_pct"] for m in matched if m["action"] == "sell"]

    return {
        "slippage_avg_pct": round(avg_slip, 3),
        "slippage_max_pct": round(max_slip, 2),
        "buy_avg_slippage": round(sum(buy_slips) / len(buy_slips), 3) if buy_slips else 0,
        "sell_avg_slippage": round(sum(sell_slips) / len(sell_slips), 3) if sell_slips else 0,
        "matched_trades": len(matched),
        "details": matched[-10:],
    }


def get_paper_pnl() -> dict:
    """纸面账户 P&L"""
    from paper_trader import PaperTrader
    pt = PaperTrader()
    return pt.get_paper_portfolio()


def get_live_pnl() -> dict:
    """实盘 P&L"""
    from portfolio import PortfolioManager
    pm = PortfolioManager()
    return pm.get_portfolio_value()


def generate_comparison_report() -> str:
    """生成对比报告"""
    slip = compute_slippage()
    paper = get_paper_pnl()
    live = get_live_pnl()

    lines = [
        f"# 📊 滑点追踪报告",
        f"**{date.today().isoformat()}**",
        "",
        "## 账户对比",
        f"| 账户 | 总资产 | 盈亏% |",
        f"|------|--------|-------|",
        f"| 纸面 | ¥{paper['total_value']:,.0f} | {paper['total_profit_pct']:+.2f}% |",
        f"| 实盘 | ¥{live['total_value']:,.0f} | {live['total_profit_pct']:+.2f}% |",
        f"| **差异** | **¥{live['total_value'] - paper['total_value']:,.0f}** | **{live['total_profit_pct'] - paper['total_profit_pct']:+.2f}%** |",
        "",
        "## 滑点分析",
        f"- 匹配交易: {slip['matched_trades']} 笔",
        f"- 平均滑点: {slip['slippage_avg_pct']:.3f}%",
        f"- 最大滑点: {slip['slippage_max_pct']:.2f}%",
        f"- 买入平均: {slip['buy_avg_slippage']:.3f}%",
        f"- 卖出平均: {slip['sell_avg_slippage']:.3f}%",
    ]

    return "\n".join(lines)


if __name__ == "__main__":
    report = generate_comparison_report()
    print(report)

    import sys
    if "--push" in sys.argv:
        try:
            import subprocess, os
            delivery = os.path.expanduser("~/.hermes/pending_deliveries/slippage_report.md")
            os.makedirs(os.path.dirname(delivery), exist_ok=True)
            with open(delivery, "w") as f:
                f.write(report)
            print(f"\n📡 已推送到微信投递队列")
        except Exception as e:
            print(f"推送失败: {e}")
