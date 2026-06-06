#!/usr/bin/env python3
"""
每日状态看板 — 价格区间警报 + 持仓盈亏 + 信号绩效
用法: python3 cli.py status (已有) 或 python3 dashboard.py
"""
import sqlite3, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import STOCK_DETAILS, STOCK_MAP

DB = os.path.join(os.path.dirname(__file__), 'serenity.db')


def price_zone_alerts():
    """买入区间警报"""
    lines = []
    alerts = []
    conn = sqlite3.connect(DB)
    
    for code, detail in STOCK_DETAILS.items():
        name = STOCK_MAP.get(code, {}).get('name', code)
        low = detail.get('buy_zone_low', 0)
        high = detail.get('buy_zone_high', 0)
        target = detail.get('target_sell', 0)
        score = detail.get('score', 0)
        if low <= 0:
            continue
        
        row = conn.execute(
            'SELECT close FROM price_history WHERE code=? ORDER BY date DESC LIMIT 1', (code,)
        ).fetchone()
        if not row:
            continue
        price = row[0]
        
        in_zone = low <= price <= high
        if in_zone:
            icon = '🎯'
            status = '买入区'
        elif price < low:
            icon = '💡'
            dist = (low - price) / price * 100
            status = f'低于{dist:.0f}%'
        else:
            icon = '📈'
            dist = (price - high) / high * 100
            status = f'高于{dist:.0f}%'

        lines.append(f'{icon} {name:6s} {code} ¥{price:.2f} [{low:.0f}-{high:.0f}] {status}')
        
        if in_zone and score >= 62:
            alerts.append(f'🚨 {name}({code}) 进入买入区! ¥{price:.2f} 评分{score}')
    
    conn.close()
    return lines, alerts


def pnl_snapshot():
    """快速盈亏快照"""
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    
    rows = conn.execute(
        'SELECT action, SUM(COALESCE(trade_amount, 0)) as t FROM trades GROUP BY action'
    ).fetchall()
    bought = sum(r['t'] or 0 for r in rows if r['action'] == 'buy')
    sold = sum(r['t'] or 0 for r in rows if r['action'] == 'sell')
    cash = 50000 - bought + sold
    
    holdings = conn.execute('SELECT * FROM stocks WHERE is_active=1').fetchall()
    total_value = cash
    lines = []
    
    for h in holdings:
        p = conn.execute(
            'SELECT close FROM price_history WHERE code=? ORDER BY date DESC LIMIT 1', (h['code'],)
        ).fetchone()
        price = p[0] if p else h['buy_price']
        amt = h['trade_amount'] or 0
        buy_price = h['buy_price']
        shares = int(amt / buy_price / 100) * 100 if buy_price > 0 and amt > 0 else 0
        current_val = shares * price
        profit_pct = (price - buy_price) / buy_price * 100 if buy_price > 0 else 0
        total_value += current_val
        icon = '🟢' if profit_pct >= 0 else '🔴'
        lines.append(f'{icon} {h["name"]:6s} {shares}股 ¥{buy_price:.2f}→¥{price:.2f} {profit_pct:+.1f}%')
    
    total_return = (total_value - 50000) / 50000 * 100
    conn.close()
    
    return {
        'cash': cash, 'total_value': total_value, 'total_return': total_return,
        'holdings': lines,
    }


def main():
    print(f'\n🔔 价格区间 | {"="*40}')
    zone_lines, alerts = price_zone_alerts()
    for l in zone_lines:
        print(f'  {l}')
    if alerts:
        print(f'\n  ⚠️ 买入警报:')
        for a in alerts:
            print(f'    {a}')

    print(f'\n💰 盈亏快照 | {"="*40}')
    pnl = pnl_snapshot()
    print(f'  现金: {pnl["cash"]:,.0f}')
    for h in pnl['holdings']:
        print(f'  {h}')
    print(f'  总资产: {pnl["total_value"]:,.0f}  收益: {pnl["total_return"]:+.1f}%')
    print()


if __name__ == '__main__':
    main()
