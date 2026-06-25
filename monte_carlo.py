"""
Monte Carlo 压力测试 v3.1
用实盘组合日收益率分布，模拟 1000 条 60 天路径，
回答：翻倍概率多大？最差回撤多少？
"""
from __future__ import annotations

import argparse
import json
import numpy as np
from db import get_conn

FRACTION_KELLY = 0.8
TARGET_MULTIPLE = 2.0
RISK_FREE_RATE = 0.02


def _load_portfolio_returns() -> np.ndarray:
    """从 nav_history 推导实盘日收益率"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT profit_pct FROM nav_history ORDER BY date"
    ).fetchall()
    conn.close()

    if len(rows) < 5:
        return _load_price_returns()

    rets = []
    prev = None
    for r in rows:
        pct = r["profit_pct"]
        if pct is not None and prev is not None:
            # profit_pct 变化率 → 日收益近似
            ret = (pct - prev) / (1 + abs(prev) / 100) / 100
            if abs(ret) < 0.15:
                rets.append(ret)
        prev = pct

    if len(rets) >= 5:
        return np.array(rets, dtype=np.float64)
    return _load_price_returns()


def _load_price_returns() -> np.ndarray:
    """从 price_history 加载 A 股日收益率"""
    conn = get_conn()
    rows = conn.execute("""
        SELECT code, date, change_pct
        FROM price_history
        WHERE code NOT LIKE 'sh%' AND code NOT LIKE '51%'
        ORDER BY code, date
    """).fetchall()
    conn.close()

    rets, seen = [], set()
    for r in rows:
        c = r["change_pct"]
        if c is not None and not np.isnan(c) and c != 0:
            key = (r["date"], r["code"])
            if key not in seen:
                seen.add(key)
                val = c / 100.0
                if abs(val) < 0.20:
                    rets.append(val)
    arr = np.array(rets, dtype=np.float64)
    if len(arr) < 10:
        raise ValueError(f"收益率不足: {len(arr)}")
    return arr


def run_monte_carlo(
    n_sims: int = 1000,
    n_days: int = 60,
    initial_capital: float = 51066,
    seed: int | None = None,
) -> dict:
    """执行 Monte Carlo 压力测试。

    优先使用实盘组合日收益率（nav_history），回退到个股采样。
    """
    returns = _load_portfolio_returns()
    rng = np.random.default_rng(seed)

    mean_ret = float(np.mean(returns))
    std_ret = float(np.std(returns, ddof=1))
    kelly = min(mean_ret / (std_ret**2) * FRACTION_KELLY, 0.9) if std_ret > 1e-12 else 0.2
    kelly = max(kelly, 0.1)

    final_vals, total_rets, max_dds, sharpes = [], [], [], []
    daily_rfr = RISK_FREE_RATE / 252

    for _ in range(n_sims):
        path = rng.choice(returns, size=n_days, replace=True)
        eq = float(initial_capital)
        peak = eq
        dds = []
        eq_hist = [eq]

        for r in path:
            eq *= (1.0 + r * kelly)
            dds.append((eq - peak) / peak if peak > 0 else 0.0)
            peak = max(peak, eq)
            eq_hist.append(eq)
            if eq <= 0:
                break

        final_vals.append(eq)
        total_rets.append(eq / initial_capital - 1.0)
        max_dds.append(min(dds) if dds else 0.0)

        if len(eq_hist) >= 3:
            dr = np.diff(eq_hist) / eq_hist[:-1]
            sr = (np.mean(dr) - daily_rfr) / np.std(dr) * np.sqrt(252) if np.std(dr) > 1e-12 else 0
            sharpes.append(sr)
        else:
            sharpes.append(0.0)

    fv = np.array(final_vals)
    tr = np.array(total_rets)
    dd = np.array(max_dds)
    sr = np.array(sharpes)

    result = {
        "params": {
            "n_sims": n_sims, "n_days": n_days,
            "initial_capital": initial_capital, "kelly": round(kelly, 3),
            "target_multiple": TARGET_MULTIPLE,
        },
        "data_quality": {
            "n_returns": len(returns),
            "mean_daily": round(mean_ret, 6),
            "std_daily": round(std_ret, 6),
            "annual_ret": round(mean_ret * 252, 4),
            "annual_vol": round(std_ret * np.sqrt(252), 4),
        },
        "statistics": {
            "target_prob": round(float(np.mean(tr >= TARGET_MULTIPLE - 1)), 4),
            "median_final": round(float(np.median(fv)), 2),
            "p5_final": round(float(np.percentile(fv, 5)), 2),
            "p95_final": round(float(np.percentile(fv, 95)), 2),
            "mean_max_dd": round(float(np.mean(dd)), 4),
            "median_max_dd": round(float(np.median(dd)), 4),
            "p95_max_dd": round(float(np.percentile(dd, 95)), 4),
            "mean_sharpe": round(float(np.mean(sr)), 2),
            "median_sharpe": round(float(np.median(sr)), 2),
        },
        "final_values": [round(v, 2) for v in final_vals],
        "total_returns": [round(v, 4) for v in total_rets],
        "max_drawdowns": [round(v, 4) for v in max_dds],
    }
    return result


def format_mc_report(r: dict) -> str:
    s, p, dq = r["statistics"], r["params"], r["data_quality"]
    gain = sum(1 for v in r["total_returns"] if v > 0) / max(p["n_sims"], 1) * 100

    return "\n".join([
        "=" * 60,
        "  Monte Carlo 压力测试报告",
        "=" * 60,
        f"\n  [参数]  {p['n_sims']}路径 × {p['n_days']}天 | 初始¥{p['initial_capital']:,.0f} | Kelly{kelly_to_str(p['kelly'])}",
        f"\n  [数据]  {dq['n_returns']}样本 | 日均{dq['mean_daily']:+.4f} | 波动{dq['annual_vol']:.0%}年化",
        f"\n  [结果]",
        f"  翻倍({p['target_multiple']}x)概率: {s['target_prob']:.1%}",
        f"  中位数: ¥{s['median_final']:,.0f} ({s['median_final']/p['initial_capital']-1:+.1%})",
        f"  最差 5%: ¥{s['p5_final']:,.0f} ({s['p5_final']/p['initial_capital']-1:+.1%})",
        f"  最佳 5%: ¥{s['p95_final']:,.0f} ({s['p95_final']/p['initial_capital']-1:+.1%})",
        f"\n  [风险] 平均回撤 {s['mean_max_dd']:.1%} | 中位 {s['median_max_dd']:.1%} | 极端 {s['p95_max_dd']:.1%}",
        f"  [Sharpe] 均值 {s['mean_sharpe']:.2f} | 中位 {s['median_sharpe']:.2f}",
        f"  [盈亏] {gain:.0f}%路径正收益",
        "\n" + "=" * 60,
    ])


def kelly_to_str(k: float) -> str:
    if k >= 0.7: return f"{k:.0%}(激进)"
    if k >= 0.4: return f"{k:.0%}(正常)"
    return f"{k:.0%}(保守)"


def main():
    ap = argparse.ArgumentParser(description="Monte Carlo 翻倍概率压力测试")
    ap.add_argument("--n-sims", type=int, default=1000)
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--initial", type=float, default=51066)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    r = run_monte_carlo(args.n_sims, args.days, args.initial, args.seed)
    if args.json:
        print(json.dumps({
            "params": r["params"], "data_quality": r["data_quality"],
            "statistics": r["statistics"],
        }, ensure_ascii=False, indent=2))
    else:
        print(format_mc_report(r))


if __name__ == "__main__":
    main()
