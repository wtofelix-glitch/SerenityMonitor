#!/usr/bin/env python3
"""
蒙特卡洛压力测试 — 10,000 种极端场景模拟
输出: VaR/CVaR/最大回撤分布/2008/2015/COVID 场景
"""
import random, math
from datetime import date, datetime
from collections import defaultdict
from serenity_logger import get_logger

log = get_logger(__name__)

# 历史极端场景参数 (日收益率, 持续天数)
STRESS_SCENARIOS = {
    "2008金融危机": {"daily_return_mean": -0.025, "daily_vol": 0.04, "days": 60, "correlation_boost": 0.4},
    "2015股灾":     {"daily_return_mean": -0.030, "daily_vol": 0.05, "days": 30, "correlation_boost": 0.5},
    "2020 COVID":   {"daily_return_mean": -0.035, "daily_vol": 0.06, "days": 20, "correlation_boost": 0.3},
    "温和调整":     {"daily_return_mean": -0.010, "daily_vol": 0.02, "days": 40, "correlation_boost": 0.2},
    "慢熊":         {"daily_return_mean": -0.005, "daily_vol": 0.015, "days": 120, "correlation_boost": 0.15},
}


def get_portfolio_data() -> dict:
    """获取当前持仓 + 历史波动率"""
    from db import get_conn
    from portfolio import PortfolioManager

    pm = PortfolioManager()
    pv = pm.get_portfolio_value()

    # 直接从 stocks 表取持仓(更可靠)
    conn = get_conn()
    stocks_rows = conn.execute(
        "SELECT code, name, buy_price, trade_amount, score FROM stocks WHERE is_active=1 AND code!='CASH'"
    ).fetchall()

    positions = []
    total_value = pv.get("total_value", 0)
    for r in stocks_rows:
        row = dict(r)
        shares = int(float(row.get("trade_amount", 0)) / float(row.get("buy_price", 1))) if float(row.get("buy_price", 0)) > 0 else 0
        current_value = shares * float(row.get("buy_price", 0))
        positions.append({
            "code": row["code"],
            "name": row.get("name", row["code"]),
            "shares": shares,
            "buy_price": float(row.get("buy_price", 0)),
            "current_value": round(current_value, 2),
        })
        total_value += current_value

    if total_value == 0:
        total_value = pv.get("total_value", 0) or 50000

    # 每只股票的历史波动率
    vols = {}
    for p in positions:
        rows = conn.execute(
            "SELECT close FROM price_history WHERE code=? ORDER BY date DESC LIMIT 60",
            (p["code"],),
        ).fetchall()
        closes = [r["close"] for r in rows if r["close"]]
        if len(closes) >= 20:
            rets = [(closes[i] - closes[i + 1]) / closes[i + 1] for i in range(len(closes) - 1)]
            vols[p["code"]] = (sum(r**2 for r in rets) / len(rets)) ** 0.5
        else:
            vols[p["code"]] = 0.02
    conn.close()

    return {
        "total_value": total_value,
        "cash": pv.get("cash", 0),
        "positions": positions,
        "volatilities": vols,
    }


def run_monte_carlo(portfolio: dict, n_sims: int = 10000, horizon_days: int = 20) -> dict:
    """运行 N 次 Monte Carlo 模拟，返回损益分布"""
    positions = portfolio["positions"]
    vols = portfolio["volatilities"]
    total_value = portfolio["total_value"]

    if not positions:
        return {"error": "无持仓"}

    # 每只股票权重
    weights = []
    for p in positions:
        w = p.get("current_value", 0) / total_value if total_value > 0 else 0
        weights.append(w)

    final_values = []
    drawdowns = []

    for _ in range(n_sims):
        current_value = total_value
        peak = total_value
        max_dd = 0

        for day in range(horizon_days):
            # 每只股票独立随机游走
            daily_change = 0
            for i, p in enumerate(positions):
                vol = vols.get(p["code"], 0.02)
                daily_return = random.gauss(0, vol)  # 假设零均值, 实际波动率
                daily_change += weights[i] * daily_return * p.get("current_value", 0)

            current_value += daily_change
            current_value = max(current_value, 0)  # 不会为负

            if current_value > peak:
                peak = current_value
            dd = (current_value - peak) / peak * 100
            if dd < max_dd:
                max_dd = dd

        final_values.append(current_value)
        drawdowns.append(max_dd)

    # 统计
    final_values.sort()
    var_95_idx = int(n_sims * 0.05)
    var_95 = -(final_values[var_95_idx] - total_value) / total_value * 100
    cvar_95 = -(sum(final_values[:var_95_idx]) / var_95_idx - total_value) / total_value * 100

    losses = [fv for fv in final_values if fv < total_value]
    loss_prob = len(losses) / n_sims * 100

    return {
        "simulations": n_sims,
        "horizon_days": horizon_days,
        "initial_value": total_value,
        "mean_final": round(sum(final_values) / n_sims, 2),
        "worst_case": round(min(final_values), 2),
        "best_case": round(max(final_values), 2),
        "VaR_95_pct": round(var_95, 2),
        "CVaR_95_pct": round(cvar_95, 2),
        "loss_probability_pct": round(loss_prob, 1),
        "mean_max_drawdown_pct": round(sum(drawdowns) / n_sims, 2),
        "worst_drawdown_pct": round(min(drawdowns), 2),
    }


def run_stress_scenarios(portfolio: dict) -> dict:
    """运行历史极端场景压力测试"""
    results = {}
    positions = portfolio["positions"]
    total_value = portfolio["total_value"]

    for scenario_name, params in STRESS_SCENARIOS.items():
        daily_mean = params["daily_return_mean"]
        daily_vol = params["daily_vol"]
        days = params["days"]
        corr_boost = params["correlation_boost"]

        # 模拟: 所有股票同步下跌(相关性飙升)
        current = total_value
        peak = total_value
        max_dd = 0

        for _ in range(days):
            # 市场整体下跌 + 个股噪音
            market_move = daily_mean
            # 个股跟随市场 + 额外下跌(相关性)
            portfolio_return = market_move * (1 + corr_boost) + random.gauss(0, daily_vol * 0.5)
            current *= (1 + portfolio_return)
            current = max(current, 0)
            if current > peak:
                peak = current
            dd = (current - peak) / peak * 100
            if dd < max_dd:
                max_dd = dd

        loss_pct = (current - total_value) / total_value * 100
        results[scenario_name] = {
            "final_value": round(current, 2),
            "loss_pct": round(loss_pct, 2),
            "max_drawdown_pct": round(max_dd, 2),
            "days": days,
        }

    return results


def generate_report() -> str:
    portfolio = get_portfolio_data()

    if "error" in portfolio:
        return f"❌ {portfolio['error']}"

    mc = run_monte_carlo(portfolio, n_sims=10000, horizon_days=20)
    stress = run_stress_scenarios(portfolio)

    if "error" in mc or "error" in portfolio:
        return f"❌ {mc.get('error', portfolio.get('error', '?'))}"

    lines = [
        f"# 🛡️ 压力测试报告",
        f"**{date.today().isoformat()}** — Monte Carlo 10,000 次模拟",
        "",
        "## 组合概况",
        f"- 总资产: ¥{portfolio['total_value']:,.0f}",
        f"- 现金: ¥{portfolio['cash']:,.0f}",
        f"- 持仓: {len(portfolio['positions'])} 只",
        "",
        "## Monte Carlo (20日, 10000次)",
        f"| 指标 | 数值 |",
        f"|------|------|",
        f"| VaR(95%) | **{mc['VaR_95_pct']}%** (¥{portfolio['total_value']*mc['VaR_95_pct']/100:,.0f}) |",
        f"| CVaR(95%) | **{mc['CVaR_95_pct']}%** (¥{portfolio['total_value']*mc['CVaR_95_pct']/100:,.0f}) |",
        f"| 亏损概率 | {mc['loss_probability_pct']}% |",
        f"| 平均最大回撤 | {mc['mean_max_drawdown_pct']}% |",
        f"| 最坏回撤 | {mc['worst_drawdown_pct']}% |",
        f"| 最好预期 | ¥{mc['best_case']:,.0f} |",
        f"| 最坏预期 | ¥{mc['worst_case']:,.0f} |",
        "",
        "## 极端场景",
        f"| 场景 | 持续天数 | 预期亏损 | 最大回撤 |",
        f"|------|---------|---------|---------|",
    ]

    for name, s in stress.items():
        lines.append(f"| {name} | {s['days']}天 | **{s['loss_pct']:+.1f}%** | {s['max_drawdown_pct']:.1f}% |")

    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    sys.path.insert(0, ".")

    report = generate_report()
    print(report)

    if "--push" in sys.argv:
        import os
        delivery = os.path.expanduser("~/.hermes/pending_deliveries/stress_test.md")
        os.makedirs(os.path.dirname(delivery), exist_ok=True)
        with open(delivery, "w") as f:
            f.write(report)
        print("\n📡 已推送到微信投递队列")
