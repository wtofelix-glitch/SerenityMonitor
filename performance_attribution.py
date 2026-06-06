"""
Serenity 绩效归因 — 评分驱动收益分解
回答：如果按评分选股，能赚多少？哪个维度贡献最大？

用法:
  python3 performance_attribution.py            # 全量归因
  python3 performance_attribution.py --top 3   # Top N 选股回测
  python3 performance_attribution.py --json     # JSON 输出
"""
import sys
import argparse
import numpy as np
from collections import defaultdict
from db import get_conn
from config import STOCK_MAP

# ── 维度 ──
FACTOR_DIMS = [
    "total_score", "base_score", "zone_score", "momentum_score",
    "volume_score", "serenity_score", "factor_score", "technical_score",
]
DIM_LABELS = {
    "total_score": "综合", "base_score": "基本面", "zone_score": "位置",
    "momentum_score": "动量", "volume_score": "量能", "serenity_score": "Serenity",
    "factor_score": "因子", "technical_score": "技术",
}


def load_data():
    """加载评分 + 价格数据"""
    conn = get_conn()

    # 评分
    sc_rows = conn.execute(
        f"SELECT code, date, {', '.join(FACTOR_DIMS)} FROM scoring_history ORDER BY date, code"
    ).fetchall()

    # 价格
    px_rows = conn.execute(
        "SELECT code, date, close FROM price_history ORDER BY code, date"
    ).fetchall()
    conn.close()

    # 组织价格: {code: {date: close}}
    prices = defaultdict(dict)
    for r in px_rows:
        prices[r["code"]][r["date"]] = r["close"]

    # 组织评分: {date: {code: {dim: val}}}
    scores = defaultdict(dict)
    all_dates = set()
    for r in sc_rows:
        d = dict(r)
        code = d.pop("code")
        date = d.pop("date")
        scores[date][code] = d
        all_dates.add(date)

    return scores, prices, sorted(all_dates)


def compute_forward_returns(prices, scores, horizon: int = 1):
    """
    对每个评分日期，计算后续 horizon 天的收益率。
    返回: [(date, code, score_dict, fwd_return_pct), ...]
    """
    records = []
    all_dates = sorted(prices.get(list(prices.keys())[0], {}).keys())

    for date in sorted(scores.keys()):
        if date not in all_dates:
            continue
        date_idx = all_dates.index(date)
        future_idx = date_idx + horizon
        if future_idx >= len(all_dates):
            continue
        future_date = all_dates[future_idx]

        for code, score_dict in scores[date].items():
            px_now = prices.get(code, {}).get(date)
            px_future = prices.get(code, {}).get(future_date)
            if px_now and px_future and px_now > 0:
                fwd_ret = (px_future - px_now) / px_now * 100
                records.append((date, code, score_dict, fwd_ret))

    return records


def top_n_backtest(records, n=3):
    """Top N 选股模拟：每天选评分最高的 N 只，等权持有 horizon 天"""
    by_date = defaultdict(list)
    for date, code, score_dict, fwd_ret in records:
        by_date[date].append((code, score_dict["total_score"], fwd_ret))

    daily_returns = []
    for date in sorted(by_date.keys()):
        ranked = sorted(by_date[date], key=lambda x: x[1], reverse=True)[:n]
        if ranked:
            avg_ret = np.mean([r[2] for r in ranked])
            daily_returns.append(avg_ret)

    if not daily_returns:
        return {"error": "无有效数据"}

    rets = np.array(daily_returns)
    total = np.prod(1 + rets / 100) - 1
    return {
        "total_return_pct": round(total * 100, 2),
        "n_days": len(daily_returns),
        "avg_daily_pct": round(np.mean(rets), 2),
        "sharpe": round(np.mean(rets) / np.std(rets) * np.sqrt(252), 2) if np.std(rets) > 0 else 0,
        "win_rate_pct": round(np.sum(rets > 0) / len(rets) * 100, 1),
        "max_daily_pct": round(np.max(rets), 2),
        "min_daily_pct": round(np.min(rets), 2),
        "cumulative": [round(np.prod(1 + rets[:i + 1] / 100) - 1, 4) * 100 for i in range(len(rets))],
    }


def factor_contribution(records):
    """
    拆解每个维度对收益的贡献。
    方法：对每个维度，按该维度评分的三分位分组，计算各组平均收益。
    Top 组收益 - Bottom 组收益 = 该因子的多空收益（贡献度）。
    """
    dim_data = defaultdict(list)
    for _, _, score_dict, fwd_ret in records:
        for dim in FACTOR_DIMS:
            val = score_dict.get(dim, 0)
            dim_data[dim].append((val, fwd_ret))

    contributions = {}
    for dim, pairs in dim_data.items():
        if len(pairs) < 9:  # 至少9条数据才能分三组
            contributions[dim] = {"error": "数据不足"}
            continue
        pairs_sorted = sorted(pairs, key=lambda x: x[0])
        n = len(pairs_sorted) // 3
        bottom = pairs_sorted[:n]
        middle = pairs_sorted[n:2 * n]
        top = pairs_sorted[2 * n:]

        top_ret = np.mean([r for _, r in top])
        mid_ret = np.mean([r for _, r in middle])
        bot_ret = np.mean([r for _, r in bottom])
        spread = top_ret - bot_ret

        contributions[dim] = {
            "label": DIM_LABELS.get(dim, dim),
            "top_ret_pct": round(top_ret, 3),
            "mid_ret_pct": round(mid_ret, 3),
            "bot_ret_pct": round(bot_ret, 3),
            "spread_pct": round(spread, 3),
            "n_samples": len(pairs),
        }

    return contributions


def benchmark_return(prices, codes, all_dates):
    """等权基准收益"""
    if not codes or not all_dates:
        return {"error": "无数据"}
    first_date = all_dates[0]
    last_date = all_dates[-1]
    returns = []
    for code in codes:
        p1 = prices.get(code, {}).get(first_date)
        p2 = prices.get(code, {}).get(last_date)
        if p1 and p2 and p1 > 0:
            returns.append((p2 - p1) / p1 * 100)
    if returns:
        return round(np.mean(returns), 2), len(returns)
    return 0, 0


def print_report(result: dict):
    """格式化输出"""
    if "top_n" in result:
        tn = result["top_n"]
        print(f"\n{'='*60}")
        print(f"🏆 Top {tn.get('n', '?')} 选股回测")
        print(f"{'='*60}")
        print(f"  总收益: {tn['total_return_pct']:+.2f}%")
        print(f"  交易天数: {tn['n_days']}天")
        print(f"  日均收益: {tn['avg_daily_pct']:+.3f}%")
        print(f"  夏普比率: {tn['sharpe']:.2f}")
        print(f"  胜率: {tn['win_rate_pct']}%")
        print(f"  最大单日: +{tn['max_daily_pct']}% / {tn['min_daily_pct']}%")

    if "benchmark" in result:
        bm = result["benchmark"]
        print(f"\n📊 等权基准: {bm['return_pct']:+.2f}% ({bm['n_stocks']}只)")

    if "contributions" in result:
        print(f"\n{'='*60}")
        print(f"📐 因子贡献度（Top 组 - Bottom 组收益差）")
        print(f"{'='*60}")
        print(f"{'维度':12s} {'Top组':>8s} {'Mid组':>8s} {'Bot组':>8s} {'多空差':>8s}")
        print("-" * 52)
        contribs = result["contributions"]
        for dim in sorted(contribs, key=lambda d: contribs[d].get("spread_pct", -99), reverse=True):
            c = contribs[dim]
            if "error" in c:
                continue
            print(
                f"{c['label']:10s} {c['top_ret_pct']:+8.2f}% {c['mid_ret_pct']:+8.2f}% "
                f"{c['bot_ret_pct']:+8.2f}% {c['spread_pct']:+8.2f}%"
            )

    if "summary" in result:
        print(f"\n💡 {result['summary']}")


def run_attribution(top_n=3, horizon=1):
    """主归因分析"""
    scores, prices, all_dates = load_data()
    records = compute_forward_returns(prices, scores, horizon)

    if not records:
        return {"error": "无有效评分-收益对"}

    print(f"📊 数据: {len(records)}条评分-收益对 | {len(set(d for d,_,_,_ in records))}个评分日")

    # 1. Top N 选股
    tn_result = top_n_backtest(records, n=top_n)
    tn_result["n"] = top_n

    # 2. 等权基准
    codes = list(prices.keys())
    bm_ret, bm_n = benchmark_return(prices, codes, all_dates)
    bm_result = {"return_pct": bm_ret, "n_stocks": bm_n}

    # 3. 因子贡献
    contribs = factor_contribution(records)

    # 4. 总结
    summary = "选股策略跑赢基准" if tn_result.get("total_return_pct", -999) > bm_ret else "选股策略跑输基准"
    if "sharpe" in tn_result and tn_result["sharpe"] > 1:
        summary += f" | 夏普{tns.get('sharpe',0):.2f}有效"

    return {
        "top_n": tn_result,
        "benchmark": bm_result,
        "contributions": contribs,
        "summary": summary,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Serenity 绩效归因")
    parser.add_argument("--top", type=int, default=3, help="Top N 选股")
    parser.add_argument("--horizon", type=int, default=1, help="前瞻天数")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = run_attribution(top_n=args.top, horizon=args.horizon)

    if args.json:
        import json
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print_report(result)
