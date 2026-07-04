#!/usr/bin/env python3
"""
策略参数 A/B 测试框架
同时跑 3 套不同参数的纸面账户 → 每周自动比较 → 最优参数自动切换实盘
"""
import json, os
from datetime import date, datetime, timedelta
from collections import defaultdict
from serenity_logger import get_logger

log = get_logger(__name__)

# 3 套预设参数变体
VARIANTS = {
    "A_conservative": {
        "buy_threshold": 68, "stop_loss_pct": -0.05, "position_pct": 0.12,
        "desc": "保守: 高门槛+紧止损+小仓位"
    },
    "B_balanced": {
        "buy_threshold": 62, "stop_loss_pct": -0.06, "position_pct": 0.18,
        "desc": "均衡: 中门槛+标准止损+标准仓位"
    },
    "C_aggressive": {
        "buy_threshold": 55, "stop_loss_pct": -0.08, "position_pct": 0.25,
        "desc": "激进: 低门槛+宽止损+大仓位"
    },
}

STATE_FILE = "/Users/mac/workspace/SerenityMonitor/.ab_state.json"
RESULTS_FILE = "/Users/mac/workspace/SerenityMonitor/reports/ab_weekly_ranking.json"


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"active_variant": "B_balanced", "promoted_at": None, "history": []}


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def simulate_variant_trades(variant_name: str, params: dict, signals: list[dict], prices: dict) -> list[dict]:
    """用给定参数在历史信号上模拟交易"""
    trades = []
    cash = 50000
    holdings = {}
    position_pct = params["position_pct"]
    buy_th = params["buy_threshold"]
    sl_pct = params["stop_loss_pct"]

    for sig in signals:
        code = sig.get("code", "")
        action = sig.get("action", "")
        score = sig.get("total_score", 50)
        price = sig.get("price", prices.get(code, 10))
        date_str = sig.get("date", "")

        # 检查止损
        for held_code, held in list(holdings.items()):
            current = prices.get(held_code, held["price"])
            pnl_pct = (current - held["price"]) / held["price"]
            if pnl_pct <= sl_pct:
                cash += current * held["shares"]
                trades.append({"code": held_code, "action": "sell", "price": current, "shares": held["shares"], "date": date_str, "reason": f"止损 {pnl_pct*100:.1f}%", "variant": variant_name})
                del holdings[held_code]

        # 买入信号
        if action in ("BUY", "STRONG_BUY", "CAUTION_BUY") and score >= buy_th:
            if len(holdings) >= 5 or price <= 0:
                continue
            amount = cash * position_pct
            shares = int(amount / price / 100) * 100
            if shares < 100:
                continue
            cost = shares * price
            if cost > cash:
                continue
            cash -= cost
            holdings[code] = {"price": price, "shares": shares, "date": date_str}
            trades.append({"code": code, "action": "buy", "price": price, "shares": shares, "date": date_str, "reason": f"信号{action}({score:.0f}分)", "variant": variant_name})

    # 最终平仓
    for held_code, held in holdings.items():
        final_price = prices.get(held_code, held["price"])
        cash += final_price * held["shares"]
        trades.append({"code": held_code, "action": "sell", "price": final_price, "shares": held["shares"], "date": "final", "reason": "最终平仓", "variant": variant_name})

    return trades


def evaluate_variant(trades: list[dict], initial_capital: float = 50000) -> dict:
    """计算一组交易的绩效指标"""
    if not trades:
        return {"sharpe": 0, "win_rate": 0, "total_return": 0, "trades": 0}

    cash = initial_capital
    daily_values = [(trades[0]["date"], initial_capital)]
    profits = []

    sells = [t for t in trades if t["action"] == "sell"]
    wins = [t for t in sells if (t.get("price", 0) - t.get("cost_per_share", 0)) * t.get("shares", 0) > 0]

    for t in sells:
        buy_trade = next((b for b in trades if b["code"] == t["code"] and b["action"] == "buy"), None)
        if buy_trade:
            pnl = (t["price"] - buy_trade["price"]) * min(t["shares"], buy_trade["shares"])
            t["pnl"] = round(pnl, 2)
            profits.append(pnl + (t["price"] * t["shares"]))  # approximate

    wr = len(wins) / len(sells) * 100 if sells else 0
    # Simplified Sharpe: mean(profit) / std(profit)
    if len(profits) >= 2:
        mean_p = sum(profits) / len(profits)
        std_p = (sum((p - mean_p) ** 2 for p in profits) / len(profits)) ** 0.5
        sharpe = mean_p / std_p if std_p > 0 else 0
    else:
        sharpe = 0

    return {"sharpe": round(sharpe, 3), "win_rate": round(wr, 1), "trades": len(trades), "sells": len(sells), "wins": len(wins)}


def run_weekly_ab_test() -> dict:
    """运行周度 A/B 测试 — 用本周信号回测 3 个变体"""
    from db import get_conn

    conn = get_conn()
    week_ago = (date.today() - timedelta(days=7)).isoformat()
    rows = conn.execute(
        "SELECT code, action, total_score, date, price FROM signal_log WHERE date >= ? ORDER BY date",
        (week_ago,),
    ).fetchall()
    conn.close()

    if len(rows) < 5:
        return {"error": f"本周信号不足({len(rows)}条)", "date": date.today().isoformat()}

    signals = [dict(r) for r in rows]

    # 价格查找
    conn = get_conn()
    price_rows = conn.execute(
        "SELECT code, close, date FROM price_history WHERE date >= ?",
        (week_ago,),
    ).fetchall()
    conn.close()

    prices: dict[str, float] = {}
    for pr in price_rows:
        prices[pr["code"]] = float(pr["close"])

    results = {}
    for vname, params in VARIANTS.items():
        trades = simulate_variant_trades(vname, params, signals, prices)
        metrics = evaluate_variant(trades)
        results[vname] = {**metrics, "params": params, "description": params["desc"]}

    # 排名
    ranked = sorted(results.items(), key=lambda x: -x[1]["sharpe"])
    winner = ranked[0] if ranked else None

    # 更新状态
    state = load_state()
    state["history"].append({
        "date": date.today().isoformat(),
        "ranking": [{"variant": name, "sharpe": m["sharpe"], "win_rate": m["win_rate"]} for name, m in ranked],
        "winner": winner[0] if winner else None,
    })
    # 保留最近 12 周
    state["history"] = state["history"][-12:]

    # 如果连续 2 周最优 → 自动切换
    if len(state["history"]) >= 2:
        last_two_winners = [h["winner"] for h in state["history"][-2:]]
        if last_two_winners[0] == last_two_winners[1] and last_two_winners[0] != state["active_variant"]:
            old_variant = state["active_variant"]
            state["active_variant"] = last_two_winners[0]
            state["promoted_at"] = date.today().isoformat()
            log.warning("🔄 A/B测试: 自动切换 %s → %s (连续2周最优)", old_variant, state["active_variant"])

    save_state(state)

    return {"date": date.today().isoformat(), "active_variant": state["active_variant"], "results": results, "ranking": [{"variant": name, **m} for name, m in ranked], "winner": winner[0] if winner else None}


def generate_report() -> str:
    result = run_weekly_ab_test()
    if "error" in result:
        return f"❌ {result['error']}"

    lines = [
        f"# 🧪 A/B 测试周报 — {result['date']}",
        f"当前活跃: **{result['active_variant']}** ({VARIANTS[result['active_variant']]['desc']})",
        "",
        "## 本周排名",
        "| 变体 | 策略 | Sharpe | 胜率 | 交易数 |",
        "|------|------|--------|------|--------|",
    ]

    for r in result["ranking"]:
        lines.append(f"| {'🏆' if r['variant']==result['winner'] else '  '} {r['variant']} | {VARIANTS[r['variant']]['desc'][:12]} | {r['sharpe']:.3f} | {r['win_rate']:.1f}% | {r['trades']} |")

    lines.append("")
    lines.append(f"🏆 本周冠军: **{result['winner']}**")

    return "\n".join(lines)


if __name__ == "__main__":
    report = generate_report()
    print(report)
