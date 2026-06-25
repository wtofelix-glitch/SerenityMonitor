"""
Monte Carlo 压力测试
Simulate 1000 random 90-day paths from historical daily returns
to answer: probability of doubling, and worst-case drawdown.

Model: max 2 positions, Kelly sizing, realistic commissions + stamp tax.

用法：
    python3 monte_carlo.py                          # 默认参数
    python3 monte_carlo.py --n-sims 2000 --days 60  # 2000次·60天
    python3 monte_carlo.py --json                   # JSON 输出
"""
from __future__ import annotations

import argparse
import json
import sys

import numpy as np
from db import get_conn

# ── 交易费用参数（A股）───────────────────────────────

COMMISSION_RATE = 0.0003  # 佣金 万3
STAMP_TAX_RATE = 0.001    # 印花税 千1（卖方单边）
MIN_COMMISSION = 5.0      # 最低佣金 5元/笔
SLIPPAGE = 0.001          # 滑点 0.1%

# ── 仓位管理参数 ────────────────────────────────────

MAX_POSITIONS = 2         # 最大持仓数量
FRACTION_KELLY = 0.5      # Kelly 分数（半凯利，偏保守）
TARGET_MULTIPLE = 2.0     # 目标倍数（翻倍）
RISK_FREE_RATE = 0.02     # 无风险利率（年化）


def _load_returns() -> np.ndarray:
    """从 price_history 加载所有持仓标的历史日收益率"""
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT code, date, change_pct
        FROM price_history
        WHERE code NOT LIKE 'sh%'
        ORDER BY code, date
        """
    ).fetchall()
    conn.close()

    returns: list[float] = []
    for r in rows:
        c = r["change_pct"]
        if c is not None and not np.isnan(c):
            val = c / 100.0  # 转为小数
            if abs(val) < 0.20:  # 过滤异常值（涨跌停附近仍保留）
                returns.append(val)
    arr = np.array(returns, dtype=np.float64)
    if len(arr) < 10:
        raise ValueError(
            f"可用日收益率不足: {len(arr)} (需要 >= 10)"
        )
    return arr


def _kelly_fraction(mean_ret: float, std_ret: float) -> float:
    """计算 Kelly 最优仓位比例（单标的）"""
    if std_ret < 1e-12:
        return 0.0
    f = mean_ret / (std_ret ** 2)
    return max(0.0, min(f * FRACTION_KELLY, 0.8))  # 上限 80%


def _calc_txn_cost(trade_value: float, is_sell: bool = False) -> float:
    """计算单笔交易成本（佣金+滑点，卖出时加印花税）"""
    commission = max(trade_value * COMMISSION_RATE, MIN_COMMISSION)
    slippage = trade_value * SLIPPAGE
    tax = trade_value * STAMP_TAX_RATE if is_sell else 0.0
    return commission + slippage + tax


def _simulate_one_path(
    returns_pool: np.ndarray,
    n_days: int,
    f_kelly: float,
    initial_capital: float,
    rng: np.random.Generator,
) -> dict:
    """模拟一条 90 天路径，返回 {final_value, peak, drawdowns, ...}"""
    equity = float(initial_capital)
    cash = float(initial_capital)
    daily_values: list[float] = [equity]
    running_peak = equity
    drawdowns: list[float] = []

    for _ in range(n_days):
        # 决定当前仓位：持有不超过 MAX_POSITIONS 只，每只 f_kelly / MAX_POSITIONS
        pos_fraction = f_kelly / MAX_POSITIONS
        total_invested = cash * pos_fraction * MAX_POSITIONS
        total_invested = min(total_invested, cash)

        n_positions = MAX_POSITIONS
        per_position = total_invested / n_positions if n_positions > 0 else 0.0

        # 买入成本
        buy_cost = _calc_txn_cost(total_invested, is_sell=False)

        # 如果买入后现金不够，调整
        if total_invested + buy_cost > cash:
            total_invested = max(0.0, cash - buy_cost)
            per_position = total_invested / n_positions if n_positions > 0 else 0.0
        cash -= total_invested + buy_cost

        # 每只持仓的日收益
        position_values = []
        for _ in range(n_positions):
            r = rng.choice(returns_pool)
            position_values.append(per_position * (1.0 + r))

        # 卖出
        sell_proceeds = sum(position_values)
        sell_cost = _calc_txn_cost(sell_proceeds, is_sell=True)
        cash += sell_proceeds - sell_cost

        equity = cash
        daily_values.append(equity)
        running_peak = max(running_peak, equity)
        dd = (equity - running_peak) / running_peak if running_peak > 0 else 0.0
        drawdowns.append(dd)

    return {
        "final_value": equity,
        "peak": running_peak,
        "drawdowns": drawdowns,
        "daily_values": daily_values,
        "return_total": equity / initial_capital - 1.0,
    }


def run_monte_carlo(
    n_sims: int = 1000,
    n_days: int = 90,
    initial_capital: float = 100_000.0,
    seed: int | None = None,
) -> dict:
    """
    执行 Monte Carlo 压力测试。

    参数:
        n_sims: 模拟路径数（默认 1000）
        n_days: 模拟天数（默认 90）
        initial_capital: 初始资金（默认 10 万）
        seed: 随机种子（可选）

    返回:
        {
            "params": {...},
            "final_values": [list of float],
            "total_returns": [list of float],
            "max_drawdowns": [list of float],
            "sharpe_ratios": [list of float],
            "statistics": {
                "target_prob": float,       # 翻倍概率
                "median_final": float,
                "p5_final": float,          # 5th 百分位
                "p95_final": float,         # 95th 百分位
                "mean_max_dd": float,
                "median_max_dd": float,
                "p95_max_dd": float,        # 95th 百分位最大回撤（最差）
                "mean_sharpe": float,
                "median_sharpe": float,
                "n_sims": int,
                "n_days": int,
                "initial_capital": float,
            },
        }
    """
    returns_pool = _load_returns()
    rng = np.random.default_rng(seed)

    # 估算 Kelly 比例
    mean_ret = float(np.mean(returns_pool))
    std_ret = float(np.std(returns_pool, ddof=1))
    f_kelly = _kelly_fraction(mean_ret, std_ret)

    # 日无风险利率
    daily_rfr = RISK_FREE_RATE / 252.0

    final_values: list[float] = []
    total_returns: list[float] = []
    max_drawdowns: list[float] = []
    sharpe_ratios: list[float] = []

    for _ in range(n_sims):
        result = _simulate_one_path(returns_pool, n_days, f_kelly,
                                    initial_capital, rng)
        fv = result["final_value"]
        final_values.append(fv)
        tr = result["return_total"]
        total_returns.append(tr)

        mdd = abs(min(result["drawdowns"])) if result["drawdowns"] else 0.0
        max_drawdowns.append(mdd)

        # 年化 Sharpe
        daily_rets = np.diff(result["daily_values"]) / result["daily_values"][:-1]
        if len(daily_rets) > 1 and np.std(daily_rets, ddof=1) > 1e-12:
            annual_ret = float(np.mean(daily_rets)) * 252
            annual_vol = float(np.std(daily_rets, ddof=1)) * np.sqrt(252)
            sharpe = (annual_ret - RISK_FREE_RATE) / annual_vol if annual_vol > 0 else 0.0
        else:
            sharpe = 0.0
        sharpe_ratios.append(sharpe)

    fv_arr = np.array(final_values)
    tr_arr = np.array(total_returns)
    mdd_arr = np.array(max_drawdowns)
    sr_arr = np.array(sharpe_ratios)

    stats = {
        "target_prob": round(float(np.mean(tr_arr >= TARGET_MULTIPLE - 1.0)), 4),
        "median_final": round(float(np.median(fv_arr)), 2),
        "p5_final": round(float(np.percentile(fv_arr, 5)), 2),
        "p95_final": round(float(np.percentile(fv_arr, 95)), 2),
        "mean_max_dd": round(float(np.mean(mdd_arr)), 4),
        "median_max_dd": round(float(np.median(mdd_arr)), 4),
        "p95_max_dd": round(float(np.percentile(mdd_arr, 95)), 4),
        "mean_sharpe": round(float(np.mean(sr_arr)), 4),
        "median_sharpe": round(float(np.median(sr_arr)), 4),
        "n_sims": n_sims,
        "n_days": n_days,
        "initial_capital": initial_capital,
        "kelly_fraction": round(f_kelly, 4),
        "mean_daily_return": round(mean_ret, 6),
        "std_daily_return": round(std_ret, 6),
    }

    return {
        "params": {
            "n_sims": n_sims,
            "n_days": n_days,
            "initial_capital": initial_capital,
            "seed": seed,
            "kelly_fraction": round(f_kelly, 4),
            "max_positions": MAX_POSITIONS,
            "commission_rate": COMMISSION_RATE,
            "stamp_tax_rate": STAMP_TAX_RATE,
            "target_multiple": TARGET_MULTIPLE,
        },
        "data_quality": {
            "n_historical_returns": len(returns_pool),
            "date_range": "",
            "mean_daily_return": round(mean_ret, 6),
            "std_daily_return": round(std_ret, 6),
            "annualized_return": round(mean_ret * 252, 4),
            "annualized_vol": round(std_ret * np.sqrt(252), 4),
        },
        "final_values": [round(v, 2) for v in final_values],
        "total_returns": [round(v, 4) for v in total_returns],
        "max_drawdowns": [round(v, 4) for v in max_drawdowns],
        "sharpe_ratios": [round(v, 4) for v in sharpe_ratios],
        "statistics": stats,
    }


def format_mc_report(result: dict) -> str:
    """格式化 Monte Carlo 报告"""
    s = result["statistics"]
    p = result["params"]
    dq = result["data_quality"]

    lines: list[str] = []
    lines.append("=" * 60)
    lines.append("  Monte Carlo 压力测试报告")
    lines.append("=" * 60)

    lines.append(f"\n  [参数设置]")
    lines.append(f"  模拟路径:   {p['n_sims']:,}")
    lines.append(f"  模拟天数:   {p['n_days']} 天 (~{p['n_days'] // 21} 个月)")
    lines.append(f"  初始资金:   ¥{p['initial_capital']:,.0f}")
    lines.append(f"  半凯利仓位: {p['kelly_fraction']:.1%}")
    lines.append(f"  最多持仓:   {p['max_positions']} 只")
    lines.append(f"  佣金:       {p['commission_rate']:.1%} (最低¥5)")
    lines.append(f"  印花税:     {p['stamp_tax_rate']:.1%} (卖出)")
    lines.append(f"  目标:       {p['target_multiple']:.0f}x 翻倍")

    lines.append(f"\n  [历史数据质量]")
    lines.append(f"  可用收益率样本: {dq['n_historical_returns']:,} 个")
    lines.append(f"  日收益率均值:   {dq['mean_daily_return']:+.6f}")
    lines.append(f"  日收益率标准差: {dq['std_daily_return']:.6f}")
    lines.append(f"  年化收益率:     {dq['annualized_return']:+.2%}")
    lines.append(f"  年化波动率:     {dq['annualized_vol']:.2%}")

    lines.append(f"\n  [核心结果]")
    lines.append(f"  翻倍概率:   {s['target_prob']:.1%}")
    lines.append(f"  最终资金中位数:   ¥{s['median_final']:>10,.2f}")
    lines.append(f"  最终资金 5% 分位: ¥{s['p5_final']:>10,.2f}  ← 最差情况")
    lines.append(f"  最终资金 95% 分位:¥{s['p95_final']:>10,.2f}  ← 最佳情况")

    lines.append(f"\n  [风险指标]")
    lines.append(f"  最大回撤均值:     {s['mean_max_dd']:.2%}")
    lines.append(f"  最大回撤中位数:   {s['median_max_dd']:.2%}")
    lines.append(f"  最大回撤 95% 分位: {s['p95_max_dd']:.2%}  ← 最差回撤")

    lines.append(f"\n  [Sharpe 比率]")
    lines.append(f"  Sharpe 均值:   {s['mean_sharpe']:.2f}")
    lines.append(f"  Sharpe 中位数: {s['median_sharpe']:.2f}")

    # 盈亏区间
    gain_pct = sum(1 for v in result["total_returns"] if v > 0) / max(s["n_sims"], 1) * 100
    lines.append(f"\n  [盈亏统计]")
    lines.append(f"  正收益概率: {gain_pct:.1f}%")
    lines.append(f"  胜率（翻倍）: {s['target_prob']:.1%}")

    lines.append("\n" + "=" * 60)
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Monte Carlo 压力测试 — 翻倍概率与最大回撤",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  python3 monte_carlo.py
  python3 monte_carlo.py --n-sims 2000 --days 60
  python3 monte_carlo.py --json
  python3 monte_carlo.py --initial 500000 --seed 42
        """,
    )
    parser.add_argument("--n-sims", type=int, default=1000,
                        help="模拟路径数（默认 1000）")
    parser.add_argument("--days", type=int, default=90,
                        help="模拟天数（默认 90）")
    parser.add_argument("--initial", type=float, default=100_000.0,
                        help="初始资金（默认 100000）")
    parser.add_argument("--seed", type=int, default=None,
                        help="随机种子")
    parser.add_argument("--json", action="store_true",
                        help="输出 JSON 格式")
    args = parser.parse_args()

    result = run_monte_carlo(
        n_sims=args.n_sims,
        n_days=args.days,
        initial_capital=args.initial,
        seed=args.seed,
    )

    if args.json:
        # 精简输出，不含全量序列
        slim = {
            "params": result["params"],
            "data_quality": result["data_quality"],
            "statistics": result["statistics"],
        }
        print(json.dumps(slim, ensure_ascii=False, indent=2))
    else:
        print(format_mc_report(result))


if __name__ == "__main__":
    main()
