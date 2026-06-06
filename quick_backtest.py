#!/usr/bin/env python3
"""快速回测: 基于 scoring_history 模拟按信号执行的收益"""
import sqlite3, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from datetime import date

conn = sqlite3.connect(os.path.join(os.path.dirname(__file__), 'serenity.db'))
conn.row_factory = sqlite3.Row

# Load scoring history → map each (code, date) to score
score_rows = conn.execute(
    'SELECT code, date, total_score, zone_score FROM scoring_history ORDER BY date'
).fetchall()

score_map: dict[str, dict[str, float]] = {}
for r in score_rows:
    code = r['code']
    if code not in score_map:
        score_map[code] = {}
    score_map[code][r['date']] = {
        'total': r['total_score'],
        'zone': r['zone_score'],
    }

# Prices
price_rows = conn.execute(
    'SELECT code, date, close FROM price_history ORDER BY date'
).fetchall()

price_map: dict[str, dict[str, float]] = {}
all_dates = set()
for r in price_rows:
    code = r['code']
    d = r['date']
    all_dates.add(d)
    if code not in price_map:
        price_map[code] = {}
    price_map[code][d] = r['close']

all_dates = sorted(all_dates)


def run_backtest():
    """Execute the backtest and print results"""
# Pre-compute ATR stops for all dates (simple 14-period ATR)
_atr_cache = {}
for code in price_map:
    closes_list = sorted(
        [(d, price_map[code][d]) for d in price_map[code]],
        key=lambda x: x[0]
    )
    for i in range(14, len(closes_list)):
        d = closes_list[i][0]
        trs = []
        for j in range(i - 13, i + 1):
            curr_c = closes_list[j][1]
            prev_c = closes_list[j-1][1] if j > 0 else curr_c
            tr = abs(curr_c - prev_c)
            trs.append(tr)
        atr14 = sum(trs) / 14
        atr_stop = closes_list[i][1] - atr14 * 2.5
        _atr_cache[f'{code}_{d}'] = atr_stop

# Backtest
init = 50000
cash = init
positions = {}
trades = []
MAX_POS = 2
MAX_PCT = 0.60
ENTER = 72   # BUY threshold
EXIT = 48    # SELL threshold

for d_idx, d in enumerate(all_dates):
    # Get scores that exist for this date
    day_scores = {}
    for code in score_map:
        if d in score_map[code]:
            day_scores[code] = score_map[code][d]

    # Get prices
    day_prices = {}
    for code in price_map:
        if d in price_map[code]:
            day_prices[code] = price_map[code][d]

    # Exits
    for code in list(positions.keys()):
        pos = positions[code]
        price = day_prices.get(code, 0)
        if price <= 0:
            continue
        profit_pct = (price - pos['entry_price']) / pos['entry_price']
        should_sell = False
        reason = ''

        sc = day_scores.get(code, {})
        score = sc.get('total', 50)
        zone_score = sc.get('zone', 50)

        if score < EXIT:
            should_sell = True
            reason = f'LOW_SCORE({score:.0f})'
        elif zone_score <= 20 and profit_pct >= 0:
            should_sell = True
            reason = f'ZONE_DONE(zone={zone_score:.0f})'

        if not should_sell and profit_pct <= -0.08:
            should_sell = True
            reason = f'HARD_STOP({profit_pct*100:.0f}%)'

        if should_sell:
            proceeds = pos['shares'] * price
            cash += proceeds
            hold_days = (date.fromisoformat(d) - date.fromisoformat(pos['entry_date'])).days
            cost = pos['shares'] * pos['entry_price']
            trades.append({
                'code': code, 'type': 'SELL', 'date': d,
                'entry_price': pos['entry_price'], 'exit_price': price,
                'shares': pos['shares'],
                'profit_pct': round(profit_pct * 100, 1),
                'profit_amount': round(proceeds - cost, 0),
                'hold_days': hold_days, 'reason': reason,
            })
            del positions[code]

    # Entries
    candidates = []
    for code, sc in day_scores.items():
        if code in positions:
            continue
        score = sc.get('total', 0)
        if score >= ENTER:
            candidates.append((code, score, day_prices.get(code, 0)))

    candidates.sort(key=lambda x: x[1], reverse=True)
    open_slots = MAX_POS - len(positions)

    for code, score, price in candidates:
        if open_slots <= 0:
            break
        if price <= 0:
            continue
        pos_cash = cash * MAX_PCT / max(open_slots, 1)
        shares = int(pos_cash / price / 100) * 100
        if shares < 100:
            continue
        cost = shares * price
        if cost > cash * 0.9:
            continue
        cash -= cost
        positions[code] = {'shares': shares, 'entry_price': price, 'entry_date': d}
        trades.append({'code': code, 'type': 'BUY', 'date': d, 'price': price, 'shares': shares, 'cost': round(cost, 0)})
        open_slots -= 1

# Close remaining
last_date = all_dates[-1]
for code, pos in positions.items():
    price = price_map.get(code, {}).get(last_date, 0)
    if price > 0:
        proceeds = pos['shares'] * price
        cash += proceeds
        cost = pos['shares'] * pos['entry_price']
        profit_pct = (price - pos['entry_price']) / pos['entry_price']
        trades.append({
            'code': code, 'type': 'CLOSE', 'date': last_date,
            'entry_price': pos['entry_price'], 'exit_price': price,
            'shares': pos['shares'],
            'profit_pct': round(profit_pct * 100, 1),
            'profit_amount': round(proceeds - cost, 0),
            'hold_days': (date.fromisoformat(last_date) - date.fromisoformat(pos['entry_date'])).days,
            'reason': 'END',
        })

conn.close()

# Results
total_return = (cash - init) / init * 100
n_buys = sum(1 for t in trades if t['type'] == 'BUY')
n_sells = sum(1 for t in trades if t['type'] != 'BUY')
closed = [t for t in trades if t['type'] in ('SELL', 'CLOSE')]
wins = [t for t in closed if t.get('profit_pct', 0) > 0]
losses = [t for t in closed if t.get('profit_pct', 0) <= 0]

print(f"\n📊 回测结果 (scoring_history 信号)")
print(f"{'='*55}")
print(f"初始: {init:,.0f}  最终: {cash:,.0f}  收益: {total_return:+.1f}%")
print(f"交易: {n_buys}买 {n_sells}卖")
if closed:
    wr = len(wins)/len(closed)*100
    avg_w = sum(t['profit_pct'] for t in wins)/len(wins) if wins else 0
    avg_l = sum(t['profit_pct'] for t in losses)/len(losses) if losses else 0
    print(f"胜率: {len(wins)}/{len(closed)} ({wr:.0f}%)  均盈{avg_w:+.1f}%  均亏{avg_l:+.1f}%")

print(f"\n📋 交易明细:")
for t in trades:
    if t['type'] == 'BUY':
        print(f"  🟢 {t['date']} BUY  {t['code']} {t['shares']}股 @{t['price']:.2f} = {t['cost']:,.0f}元")
    else:
        pct = t.get('profit_pct', 0)
        icon = "🟢" if pct > 0 else "🔴"
        print(f"  {icon} {t['date']} {t['type']:5s} {t['code']} {t['shares']}股 "
              f"{t['entry_price']:.2f}->{t['exit_price']:.2f} "
              f"{pct:+.1f}% {t.get('profit_amount',0):+,.0f}元 [{t.get('reason','')}]")

# Per-stock pnl
print(f"\n📊 各标的:")
stock_pnl = {}
for t in closed:
    c = t['code']
    if c not in stock_pnl:
        stock_pnl[c] = {'n': 0, 'w': 0, 'pnl': 0}
    stock_pnl[c]['n'] += 1
    if t.get('profit_pct', 0) > 0:
        stock_pnl[c]['w'] += 1
    stock_pnl[c]['pnl'] += t.get('profit_amount', 0)
for c, s in sorted(stock_pnl.items(), key=lambda x: x[1]['pnl'], reverse=True):
    wr_s = f"{s['w']}/{s['n']}" if s['n'] > 0 else "?"
    print(f"  {c}: {wr_s} 合计{s['pnl']:+,.0f}元")



def main():
    run_backtest()

if __name__ == '__main__':
    run_backtest()
