#!/usr/bin/env python3
"""
Serenity 快速回测 — 验证评分策略历史表现
用法: python3 quick_backtest.py [days]

输出策略的历史收益率、胜率、最大回撤、夏普比率
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import date, timedelta
from db import get_conn, get_price_history
from config import ALL_CODES, STOCK_MAP


def backtest(days: int = 60):
    """回测评分策略：模拟按每日信号买入Top2的策略表现"""
    conn = get_conn()
    
    # 获取所有评分历史
    rows = conn.execute("""
        SELECT code, date, total_score
        FROM scoring_history
        WHERE date >= date('now', ?)
        ORDER BY date ASC, total_score DESC
    """, (f'-{days} days',)).fetchall()
    conn.close()
    
    if not rows:
        print("无评分历史数据，请先运行每日评分")
        return
    
    # 按日期组织评分数据
    from collections import defaultdict
    daily_scores = defaultdict(list)
    for r in rows:
        daily_scores[r["date"]].append({
            "code": r["code"],
            "score": r["total_score"],
            "action": "",
        })
    
    # 对每日按评分排序
    for d in daily_scores:
        daily_scores[d].sort(key=lambda s: s["score"], reverse=True)
    
    # 模拟：每天买入评分Top2，持有至期末
    dates = sorted(daily_scores.keys())
    if len(dates) < 2:
        print("数据不足（需至少2天数据）")
        return
    
    # 获取价格数据
    prices = {}
    for code in ALL_CODES:
        hist = get_price_history(code, days + 5)
        prices[code] = {r["date"]: r["close"] for r in hist}
    
    # 计算：如果每天按信号买入Top2，等权重
    start_date = dates[0]
    end_date = dates[-1]
    
    top_picks = {}
    for d in dates:
        top2 = [s["code"] for s in daily_scores[d][:2]]
        top_picks[d] = top2
    
    # 日收益率计算
    daily_returns = []
    for i in range(len(dates) - 1):
        today = dates[i]
        tomorrow = dates[i + 1]
        picks = top_picks[today]
        day_return = 0
        valid = 0
        for code in picks:
            p_today = prices.get(code, {}).get(today, 0)
            p_tomorrow = prices.get(code, {}).get(tomorrow, 0)
            if p_today > 0 and p_tomorrow > 0:
                day_return += (p_tomorrow - p_today) / p_today
                valid += 1
        if valid > 0:
            daily_returns.append(day_return / valid)
    
    if not daily_returns:
        print("无法计算收益率（价格数据缺失）")
        return
    
    import math
    
    # 累积收益
    cumulative = 1.0
    for r in daily_returns:
        cumulative *= (1 + r)
    total_return = (cumulative - 1) * 100
    
    # 年化收益（假设252交易日）
    annual_return = (cumulative ** (252 / len(daily_returns)) - 1) * 100
    
    # 胜率
    win_count = sum(1 for r in daily_returns if r > 0)
    win_rate = win_count / len(daily_returns) * 100
    
    # 最大回撤
    peak = 1.0
    max_dd = 0.0
    cum = 1.0
    for r in daily_returns:
        cum *= (1 + r)
        peak = max(peak, cum)
        max_dd = max(max_dd, (peak - cum) / peak)
    
    # 夏普比率（假设无风险利率3%）
    risk_free_daily = 0.03 / 252
    excess = [r - risk_free_daily for r in daily_returns]
    mean_excess = sum(excess) / len(excess)
    std_dev = math.sqrt(sum((r - mean_excess) ** 2 for r in excess) / len(excess))
    sharpe = (mean_excess / std_dev * math.sqrt(252)) if std_dev > 0 else 0
    
    avg_daily = sum(daily_returns) / len(daily_returns) * 100
    
    print(f"\n{'='*55}")
    print(f"  📊 Serenity 策略回测报告")
    print(f"{'='*55}")
    print(f"  回测区间: {start_date} → {end_date}")
    print(f"  交易日数: {len(daily_returns)}")
    print(f"  策略: 每日买入评分Top2，等权重")
    print()
    print(f"  📈 收益指标")
    print(f"    总收益:     {total_return:+.2f}%")
    print(f"    年化收益:   {annual_return:+.2f}%")
    print(f"    日均收益:   {avg_daily:+.3f}%")
    print()
    print(f"  🎯 胜率")
    print(f"    上涨天数:   {win_count}/{len(daily_returns)}")
    print(f"    胜率:       {win_rate:.1f}%")
    print()
    print(f"  ⚠️ 风险指标")
    print(f"    最大回撤:   {max_dd*100:.2f}%")
    print(f"    夏普比率:   {sharpe:.2f}")
    print()
    print(f"  💡 与目标对比")
    print(f"    翻倍需月收益: +28.4%")
    monthly_implied = (cumulative ** (30 / len(daily_returns)) - 1) * 100
    print(f"    策略月收益:   {monthly_implied:+.2f}%")
    if monthly_implied >= 28.4:
        print(f"    ✅ 策略月收益足以支撑翻倍目标")
    elif monthly_implied >= 15:
        print(f"    ⚡ 策略月收益接近目标，需适当优化")
    else:
        print(f"    ⚠️ 策略月收益不足，需调整选股或仓位")
    print()
    
    # 近期Top信号统计
    print(f"  🔝 近期最强信号标的")
    code_wins = defaultdict(lambda: {"wins": 0, "total": 0, "return": 0})
    for d in dates:
        for s in daily_scores[d][:3]:
            c = s["code"]
            code_wins[c]["total"] += 1
    for c in sorted(code_wins, key=lambda x: code_wins[x]["total"], reverse=True)[:5]:
        name = STOCK_MAP.get(c, {}).get("name", c)
        print(f"    {name} ({c}): {code_wins[c]['total']}次入选Top3")
    
    print(f"\n{'='*55}\n")

    return {
        "total_return": round(total_return, 2),
        "annual_return": round(annual_return, 2),
        "win_rate": round(win_rate, 1),
        "max_drawdown": round(max_dd * 100, 2),
        "sharpe": round(sharpe, 2),
        "monthly_return": round(monthly_implied, 2),
        "days": len(daily_returns),
    }



def main(days: int = None):
    """CLI entry point — safe to call from cli.py without sys.argv dependency"""
    if days is None:
        try:
            days = int(sys.argv[1]) if len(sys.argv) > 1 else 60
        except (ValueError, IndexError):
            days = 60
    return backtest(days)

if __name__ == "__main__":
    main()
