#!/usr/bin/env python3
"""
管道遍历回测 — 在 2023-2025 历史上重跑完整 Serenity 管道
每日: 评分→信号→模拟执行→5日结算
输出: 夏普/最大回撤/年化收益/胜率曲线/净值曲线
"""
import json
from datetime import date, datetime, timedelta
from collections import defaultdict

from serenity_logger import get_logger
log = get_logger(__name__)


def load_price_db() -> dict[str, dict[str, float]]:
    """加载 price_history 为 {code: {date: close}}"""
    from db import get_conn
    conn = get_conn()
    rows = conn.execute("SELECT code, date, close FROM price_history WHERE close>0 ORDER BY date").fetchall()
    conn.close()
    db: dict[str, dict[str, float]] = defaultdict(dict)
    for r in rows:
        db[r["code"]][r["date"]] = float(r["close"])
    return dict(db)


def get_all_trading_dates(prices: dict[str, dict[str, float]], start: str, end: str) -> list[str]:
    """获取所有交易日"""
    all_dates = set()
    for code_dates in prices.values():
        for d in code_dates:
            if start <= d <= end:
                all_dates.add(d)
    return sorted(all_dates)


def run_walkforward(start: str = "2025-01-01", end: str = "2026-06-30",
                     initial_capital: float = 50000) -> dict:
    """运行遍历回测

    模拟逻辑:
    - 每天: 对前一日评分最高的前 3 只 BUY 信号(非持仓)以固定仓位入场
    - 5 日后自动平仓
    - 记录每笔交易的收益
    - 跟踪净值曲线
    """
    prices = load_price_db()
    dates = get_all_trading_dates(prices, start, end)
    if len(dates) < 20:
        return {"error": f"数据不足, 仅 {len(dates)} 个交易日"}

    from config import ALL_CODES

    cash = initial_capital
    positions: list[dict] = []  # [{code, entry_date, entry_price, shares, cost}]
    equity_curve: list[dict] = []
    trades: list[dict] = []

    position_pct = 0.15  # 单笔 15% 仓位
    max_positions = 5

    for i, today in enumerate(dates):
        # 1. 结算持仓 — 入场满 5 日的平仓
        for p in list(positions):
            days_held = dates.index(today) - dates.index(p["entry_date"]) if today in dates and p["entry_date"] in dates else 0
            if days_held >= 5:
                exit_price = prices.get(p["code"], {}).get(today, p["entry_price"])
                pnl = (exit_price - p["entry_price"]) * p["shares"]
                cash += p["cost"] + pnl
                trades.append({
                    "code": p["code"], "entry_date": p["entry_date"], "exit_date": today,
                    "entry_price": p["entry_price"], "exit_price": exit_price,
                    "shares": p["shares"], "pnl": round(pnl, 2),
                    "pnl_pct": round((exit_price - p["entry_price"]) / p["entry_price"] * 100, 2),
                })
                positions.remove(p)

        # 2. 计算当日评分(基于价格数据模拟简化评分)
        today_scores = []
        for code in ALL_CODES:
            closes = []
            for d in dates[max(0, dates.index(today) - 20):dates.index(today) + 1]:
                p = prices.get(code, {}).get(d)
                if p: closes.append(p)
            if len(closes) < 10:
                continue

            close = closes[-1]
            # 简化评分: momentum(40%) + mean_reversion(30%) + trend(30%)
            mtm = (closes[-1] - closes[-5]) / closes[-5] * 100 if len(closes) >= 5 else 0
            mr = (closes[-1] - min(closes[-10:])) / max(closes[-10:]) if closes[-10:] else 1 if len(closes) >= 10 else 0
            ma5 = sum(closes[-5:]) / 5 if len(closes) >= 5 else close
            trend = (close - ma5) / ma5 * 100

            score = 50 + mtm * 0.8 + mr * 15 + trend * 0.5
            score = max(10, min(95, score))

            # 简单信号判定
            if score >= 62:
                action = "BUY"
            elif score >= 50:
                action = "HOLD"
            elif score >= 40:
                action = "WATCH"
            else:
                action = "SELL"

            today_scores.append({"code": code, "score": round(score, 1), "action": action, "close": close})

        today_scores.sort(key=lambda x: -x["score"])

        # 3. 执行买入 — 对非持仓的 BUY 信号, 有仓位时入场
        held_codes = {p["code"] for p in positions}
        candidates = [s for s in today_scores if s["action"] == "BUY" and s["code"] not in held_codes]

        for c in candidates:
            if len(positions) >= max_positions:
                break
            if cash < initial_capital * 0.05:  # 现金不足 5%
                break

            trade_amount = min(cash * position_pct, initial_capital * position_pct)
            shares = int(trade_amount / c["close"] / 100) * 100
            if shares < 100:
                continue

            cost = shares * c["close"]
            cash -= cost
            positions.append({
                "code": c["code"], "entry_date": today, "entry_price": c["close"],
                "shares": shares, "cost": cost,
            })

        # 4. 记录净值
        holdings_value = sum(p["shares"] * prices.get(p["code"], {}).get(today, p["entry_price"]) for p in positions)
        total_value = cash + holdings_value
        equity_curve.append({
            "date": today, "cash": round(cash, 2),
            "holdings_value": round(holdings_value, 2),
            "total_value": round(total_value, 2),
            "positions": len(positions),
        })

    # 平仓所有未了结持仓
    last_date = dates[-1]
    for p in list(positions):
        exit_price = prices.get(p["code"], {}).get(last_date, p["entry_price"])
        pnl = (exit_price - p["entry_price"]) * p["shares"]
        cash += p["cost"] + pnl
        trades.append({
            "code": p["code"], "entry_date": p["entry_date"], "exit_date": last_date,
            "entry_price": p["entry_price"], "exit_price": exit_price,
            "shares": p["shares"], "pnl": round(pnl, 2),
            "pnl_pct": round((exit_price - p["entry_price"]) / p["entry_price"] * 100, 2),
        })
    positions.clear()

    # 统计指标
    final_value = cash
    total_return = (final_value - initial_capital) / initial_capital * 100
    years = (date.fromisoformat(end) - date.fromisoformat(start)).days / 365.25
    years = max(years, 0.1)
    annual_return = ((final_value / initial_capital) ** (1 / years) - 1) * 100 if years > 0 else 0

    daily_returns = []
    for j in range(1, len(equity_curve)):
        r = (equity_curve[j]["total_value"] - equity_curve[j - 1]["total_value"]) / equity_curve[j - 1]["total_value"]
        daily_returns.append(r)

    ann_vol = (sum(r**2 for r in daily_returns) / max(len(daily_returns), 1)) ** 0.5 * (252 ** 0.5)
    sharpe = annual_return / (ann_vol * 100) if ann_vol > 0 else 0

    peak = initial_capital
    mdd = 0
    for eq in equity_curve:
        if eq["total_value"] > peak:
            peak = eq["total_value"]
        dd = (eq["total_value"] - peak) / peak * 100
        mdd = min(mdd, dd)

    wins = sum(1 for t in trades if t["pnl"] > 0)
    win_rate = wins / len(trades) * 100 if trades else 0
    avg_return = sum(t["pnl_pct"] for t in trades) / len(trades) if trades else 0
    profit_factor = sum(t["pnl"] for t in trades if t["pnl"] > 0) / abs(sum(t["pnl"] for t in trades if t["pnl"] < 0)) if sum(t["pnl"] for t in trades if t["pnl"] < 0) != 0 else 999

    return {
        "start": start, "end": end, "days": len(dates), "years": round(years, 1),
        "initial_capital": initial_capital, "final_value": round(final_value, 2),
        "total_return_pct": round(total_return, 2),
        "annual_return_pct": round(annual_return, 2),
        "sharpe": round(sharpe, 2),
        "max_drawdown_pct": round(mdd, 2),
        "ann_volatility_pct": round(ann_vol * 100, 1),
        "total_trades": len(trades),
        "win_rate_pct": round(win_rate, 1),
        "avg_return_pct": round(avg_return, 2),
        "profit_factor": round(profit_factor, 1),
        "equity_curve": equity_curve[::max(1, len(equity_curve) // 50)],  # 采样
        "recent_trades": trades[-10:],
    }


def generate_report(result: dict) -> str:
    """生成回测报告"""
    if "error" in result:
        return f"❌ 回测失败: {result['error']}"

    lines = [
        f"# 📈 Serenity 管道遍历回测",
        f"**{result['start']} ~ {result['end']}** ({result['days']} 交易日, {result['years']}年)",
        "",
        "## 核心指标",
        f"| 指标 | 数值 |",
        f"|------|------|",
        f"| 初始资金 | ¥{result['initial_capital']:,.0f} |",
        f"| 期末资金 | ¥{result['final_value']:,.0f} |",
        f"| 总收益 | {result['total_return_pct']:+.2f}% |",
        f"| 年化收益 | {result['annual_return_pct']:.2f}% |",
        f"| Sharpe | {result['sharpe']:.2f} |",
        f"| 最大回撤 | {result['max_drawdown_pct']:.1f}% |",
        f"| 年化波动 | {result['ann_volatility_pct']:.1f}% |",
        f"| 总交易 | {result['total_trades']} 笔 |",
        f"| 胜率 | {result['win_rate_pct']:.1f}% |",
        f"| 平均收益 | {result['avg_return_pct']:+.2f}% |",
        f"| 盈亏比 | {result['profit_factor']:.1f} |",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    start = sys.argv[1] if len(sys.argv) > 1 else "2025-06-01"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-07-01"

    print(f"🔄 遍历回测 {start} ~ {end} ...")
    result = run_walkforward(start=start, end=end, initial_capital=50000)
    print(generate_report(result))
