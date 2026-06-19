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
    """回测评分策略：按实际信号(BUY/SELL/HOLD)模拟交易，而非简单Top2"""
    conn = get_conn()
    
    # 获取所有评分历史（含信号）
    rows = conn.execute("""
        SELECT code, date, total_score, details
        FROM scoring_history
        WHERE date >= date('now', ?)
        ORDER BY date ASC, total_score DESC
    """, (f'-{days} days',)).fetchall()
    conn.close()
    
    # Parse signal_action from Python repr details field
    import ast
    parsed_rows = []
    for r in rows:
        action = "HOLD"
        try:
            det = ast.literal_eval(r["details"]) if r["details"] else {}
            action = det.get("signal_action", "HOLD")
        except Exception:
            pass
        parsed_rows.append({
            "code": r["code"],
            "date": r["date"],
            "score": r["total_score"],
            "action": action,
        })
    
    if not rows:
        print("无评分历史数据，请先运行每日评分")
        return
    
    # 按日期组织
    from collections import defaultdict
    daily_data = defaultdict(list)
    for r in parsed_rows:
        daily_data[r["date"]].append(r)
    
    for d in daily_data:
        daily_data[d].sort(key=lambda s: s["score"], reverse=True)
    
    dates = sorted(daily_data.keys())
    if len(dates) < 2:
        print("数据不足（需至少2天数据）")
        return
    
    # 获取价格数据
    prices = {}
    for code in ALL_CODES:
        hist = get_price_history(code, days + 5)
        prices[code] = {r["date"]: r["close"] for r in hist}
    
    # 模拟交易: 每天按信号买卖
    # positions: {code: shares}
    positions = {}
    initial_capital = 100000
    cash = initial_capital
    trades_log = []
    
    start_date = dates[0]
    end_date = dates[-1]
    
    daily_values = []
    daily_returns = []
    
    for i, today in enumerate(dates):
        today_data = daily_data[today]
        
        # 1. 处理卖出信号
        to_sell = []
        for code, shares in list(positions.items()):
            p_today = prices.get(code, {}).get(today, 0)
            if p_today <= 0:
                continue
            sell_signal = any(s["code"] == code and s["action"] in ("SELL", "STRONG_SELL") 
                            for s in today_data)
            if sell_signal and shares > 0:
                cash += shares * p_today
                trades_log.append(f"{today} SELL {code} @ {p_today:.2f} ({shares:.0f}股)")
                to_sell.append(code)
        
        for code in to_sell:
            del positions[code]
        
        # 2. 处理买入信号 (最多2只持仓)
        buy_candidates = [s for s in today_data 
                         if s["action"] in ("CAUTION_BUY", "BUY", "STRONG_BUY")
                         and s["code"] not in positions]
        
        max_new = max(0, 2 - len(positions))
        if max_new > 0 and buy_candidates:
            per_stock_cash = cash / max_new
            for s in buy_candidates[:max_new]:
                code = s["code"]
                p_today = prices.get(code, {}).get(today, 0)
                if p_today > 0 and per_stock_cash > 0:
                    shares = int(per_stock_cash / p_today / 100) * 100  # A股100股整数倍
                    if shares >= 100:
                        cost = shares * p_today
                        cash -= cost
                        positions[code] = shares
                        trades_log.append(f"{today} BUY  {code} @ {p_today:.2f} x{shares}股")
        
        # 3. 计算当日组合价值
        position_value = sum(
            positions[code] * prices.get(code, {}).get(today, 0)
            for code in positions
            if prices.get(code, {}).get(today, 0) > 0
        )
        total_value = cash + position_value
        daily_values.append(total_value)
        
        if i > 0:
            daily_ret = (daily_values[-1] - daily_values[-2]) / daily_values[-2]
            daily_returns.append(daily_ret)
    
    if not daily_returns:
        print("无法计算收益率（价格数据缺失）")
        return
    
    import math
    
    cumulative = 1.0
    for r in daily_returns:
        cumulative *= (1 + r)
    total_return = (cumulative - 1) * 100
    
    annual_return = (cumulative ** (252 / len(daily_returns)) - 1) * 100
    
    win_count = sum(1 for r in daily_returns if r > 0)
    win_rate = win_count / len(daily_returns) * 100
    
    peak = 1.0
    max_dd = 0.0
    cum = 1.0
    for r in daily_returns:
        cum *= (1 + r)
        peak = max(peak, cum)
        max_dd = max(max_dd, (peak - cum) / peak)
    
    risk_free_daily = 0.03 / 252
    excess = [r - risk_free_daily for r in daily_returns]
    mean_excess = sum(excess) / len(excess)
    std_dev = math.sqrt(sum((r - mean_excess) ** 2 for r in excess) / len(excess))
    sharpe = (mean_excess / std_dev * math.sqrt(252)) if std_dev > 0 else 0
    
    avg_daily = sum(daily_returns) / len(daily_returns) * 100
    
    print(f"\n{'='*55}")
    print(f"  📊 Serenity 策略回测报告 (信号驱动)")
    print(f"{'='*55}")
    print(f"  回测区间: {start_date} → {end_date}")
    print(f"  交易日数: {len(daily_returns)}")
    print(f"  策略: 每日按BUY信号买入(≤2只)，SELL信号卖出")
    print(f"  交易笔数: {len(trades_log)}")
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
    monthly_implied = (cumulative ** (30 / len(daily_returns)) - 1) * 100
    print(f"  💡 与目标对比")
    print(f"    翻倍需月收益: +28.4%")
    print(f"    策略月收益:   {monthly_implied:+.2f}%")
    if monthly_implied >= 28.4:
        print(f"    ✅ 策略月收益足以支撑翻倍目标")
    elif monthly_implied >= 15:
        print(f"    ⚡ 策略月收益接近目标，需适当优化")
    else:
        print(f"    ⚠️ 策略月收益不足，需调整选股或仓位")
    
    # 最近交易
    if trades_log:
        print(f"\n  📋 最近交易")
        for t in trades_log[-10:]:
            print(f"    {t}")
    
    print(f"\n{'='*55}\n")

    return {
        "total_return": round(total_return, 2),
        "annual_return": round(annual_return, 2),
        "win_rate": round(win_rate, 1),
        "max_drawdown": round(max_dd * 100, 2),
        "sharpe": round(sharpe, 2),
        "monthly_return": round(monthly_implied, 2),
        "days": len(daily_returns),
        "trades": len(trades_log),
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
