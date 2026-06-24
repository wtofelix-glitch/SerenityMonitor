"""
哨兵回测 — 信源历史准确性验证
计算每个信源的: 胜率 / 盈亏比 / 平均收益 / 方向正确率
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime, timedelta
from sentinel_engine import get_sentinel
from db import get_conn


def backtest_sources(days: int = 30):
    """回测所有信源的预测准确率"""
    engine = get_sentinel()
    perf = engine.get_source_performance(days=days)

    print(f"\n{'='*60}")
    print(f"  哨兵信源回测 — 近 {days} 天")
    print(f"{'='*60}")
    print(f"{'信源':<16} {'预测':>5} {'正确':>5} {'准确率':>8} {'均收益':>8}")
    print(f"{'-'*60}")

    for p in perf:
        print(f"{p['name']:<16} {p['total']:>5} {p['correct']:>5} {p['accuracy']:>7.1f}% {p['avg_1d_return']:>+7.2f}%")

    print(f"{'='*60}")

    # 信源排行
    if perf:
        best = max(perf, key=lambda x: x['accuracy'])
        worst = min(perf, key=lambda x: x['accuracy'])
        print(f"\n🏆 最准信源: {best['name']} ({best['accuracy']}%)")
        print(f"⚠️ 最差信源: {worst['name']} ({worst['accuracy']}%)")

    return perf


def backtest_ticker(ticker: str, days: int = 30):
    """回测单个标的所有信源预测"""
    conn = get_conn()
    rows = conn.execute("""
        SELECT sp.*, so.content_raw, so.signal_type, so.fetched_at,
               ss.name as source_name
        FROM sentinel_performance sp
        JOIN sentinel_observations so ON so.id = sp.observation_id
        JOIN sentinel_sources ss ON ss.id = sp.source_id
        WHERE sp.ticker = ? AND sp.settled_at >= date('now', ?)
        ORDER BY sp.settled_at DESC
    """, (ticker, f'-{days} days')).fetchall()
    conn.close()

    if not rows:
        print(f"\n{ticker}: 无回测数据")
        return []

    print(f"\n{'='*60}")
    print(f"  {ticker} 信源预测回测 — 近 {days} 天")
    print(f"{'='*60}")

    correct = sum(1 for r in rows if r["correct"])
    total = len(rows)
    print(f"总预测: {total} | 正确: {correct} | 准确率: {correct/total*100:.1f}%\n")

    for r in rows:
        icon = "✅" if r["correct"] else "❌"
        print(f"  {icon} {r['source_name']:<12} {r['direction']:<8} "
              f"预测: {r['signal_type']:<12} 实际: {r['outcome_1d']:>+6.2f}% "
              f"({r['fetched_at'][:16]})")

    return [dict(r) for r in rows]


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="哨兵信源回测")
    ap.add_argument("--days", type=int, default=30, help="回测天数")
    ap.add_argument("--ticker", type=str, help="指定标的代码")
    ap.add_argument("--settle", action="store_true", help="先结算未处理观测")

    args = ap.parse_args()

    if args.settle:
        engine = get_sentinel()
        n = engine.settle_outcomes(days_back=args.days)
        print(f"结算完成: {n} 条")

    if args.ticker:
        backtest_ticker(args.ticker, args.days)
    else:
        backtest_sources(args.days)
