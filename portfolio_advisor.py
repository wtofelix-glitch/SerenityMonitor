#!/usr/bin/env python3
"""
最优组合建议 — 基于相关性 + 评分 + 大盘择时 的三维选股

原则:
  1. 优先 Tier 1 高分标的
  2. 持仓间相关性 < 0.5（分散风险）
  3. 大盘危险时降到 1 只

用法:
    python3 portfolio_advisor.py
"""
import sqlite3, os, sys, math
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from datetime import date

DB = os.path.join(os.path.dirname(__file__), 'serenity.db')

CODES = ['002281', '000988', '600141', '603083', '600487', '002428', '600460', '603986', '600176']
NAMES = {
    '002281': '光迅科技', '000988': '华工科技', '600141': '兴发集团',
    '603083': '剑桥科技', '600487': '亨通光电', '002428': '云南锗业',
    '600460': '士兰微', '603986': '兆易创新', '600176': '中国巨石',
}
TAGS = {
    '002281': 'CPO龙头', '000988': '激光+光引擎', '600141': '磷化工',
    '603083': '光模块', '600487': '光纤基建', '002428': '衬底材料',
    '600460': '功率半', '603986': 'AI存储', '600176': '电子布',
}


def get_latest_scores():
    conn = sqlite3.connect(DB)
    rows = conn.execute('''
        SELECT s.code, s.total_score FROM scoring_history s
        INNER JOIN (SELECT code, MAX(date) as md FROM scoring_history GROUP BY code) l
        ON s.code = l.code AND s.date = l.md
    ''').fetchall()
    conn.close()
    return {r[0]: r[1] for r in rows}


def get_correlation_matrix(days=60):
    """计算相关性矩阵"""
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    returns = {}
    for code in CODES:
        rows = conn.execute(
            'SELECT close FROM price_history WHERE code=? ORDER BY date', (code,)
        ).fetchall()
        if len(rows) < days:
            continue
        prices = [r['close'] for r in rows[-days:]]
        rets = [(prices[i]-prices[i-1])/prices[i-1]*100 for i in range(1,len(prices))]
        returns[code] = rets
    conn.close()

    n = min(len(r) for r in returns.values())
    aligned = {c: returns[c][-n:] for c in returns}

    corr = {}
    for c1 in aligned:
        corr[c1] = {}
        for c2 in aligned:
            x, y = aligned[c1], aligned[c2]
            mx, my = sum(x)/n, sum(y)/n
            sx = math.sqrt(sum((v-mx)**2 for v in x)/n)
            sy = math.sqrt(sum((v-my)**2 for v in y)/n)
            if sx == 0 or sy == 0:
                corr[c1][c2] = 0
            else:
                corr[c1][c2] = sum((x[i]-mx)*(y[i]-my) for i in range(n))/(n*sx*sy)
    return corr


def get_market_regime():
    try:
        from market_timing import get_market_signal
        return get_market_signal().get('overall_signal', '中性')
    except Exception:
        return '中性'


def suggest_portfolio(max_positions=2, max_correlation=0.55):
    scores = get_latest_scores()
    corr = get_correlation_matrix()
    regime = get_market_regime()

    if regime in ('危险',):
        max_positions = 1
        max_correlation = 1.0  # single position, no correlation constraint

    # Sort by score descending
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)

    # Filter: score >= 62 (CAUTION_BUY minimum)
    candidates = [(c, s) for c, s in ranked if s >= 62 and c in corr]

    # Greedy selection: pick highest score, then next with low correlation
    selected = []
    for code, score in candidates:
        if len(selected) >= max_positions:
            break
        # Check correlation with already selected
        ok = True
        for sel_code, _ in selected:
            if sel_code in corr.get(code, {}) and abs(corr[code][sel_code]) > max_correlation:
                ok = False
                break
        if ok:
            selected.append((code, score))

    # Correlation info
    corr_info = []
    if len(selected) >= 2:
        c1, c2 = selected[0][0], selected[1][0]
        if c1 in corr and c2 in corr[c1]:
            corr_info.append(f'{NAMES[c1]}-{NAMES[c2]} 相关性: {corr[c1][c2]:.2f}')

    return {
        'regime': regime,
        'max_positions': max_positions,
        'selected': selected,
        'corr_info': corr_info,
    }


def main():
    result = suggest_portfolio()

    print(f'\n🎯 最优组合建议 | {date.today()}  大盘: {result["regime"]}')
    print('=' * 60)

    if not result['selected']:
        print('\n⚠️ 当前无满足条件的标的（评分≥62且相关性<0.55）')
        return

    print(f'\n📈 建议持仓 ({result["max_positions"]} 只):')
    for code, score in result['selected']:
        tag = TAGS.get(code, '')
        print(f'  ⭐ {NAMES[code]:6s} ({code}) {score:.0f}分  [{tag}]')

    if result['corr_info']:
        for info in result['corr_info']:
            print(f'\n🔗 {info}')

    # Compare with current holdings
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    held = {
        r['code']: r for r in conn.execute(
            'SELECT * FROM stocks WHERE is_active=1'
        ).fetchall()
    }
    conn.close()

    suggested_codes = {c for c, _ in result['selected']}
    held_codes = set(held.keys())

    if held:
        print(f'\n📋 当前持仓 vs 建议:')
        for c in held_codes | suggested_codes:
            name = NAMES.get(c, c)
            in_held = '✅' if c in held_codes else '  '
            in_sug = '✅' if c in suggested_codes else '  '
            score = get_latest_scores().get(c, 0)
            print(f'  持仓{in_held} 建议{in_sug} {name:6s} ({c}) {score:.0f}分')

        overlap = held_codes & suggested_codes
        to_sell = held_codes - suggested_codes
        to_buy = suggested_codes - held_codes

        if to_sell or to_buy:
            print(f'\n📋 调仓指令:')
            for c in to_sell:
                print(f'  🔴 卖出 {NAMES.get(c,c)}({c})')
            for c in to_buy:
                print(f'  🟢 买入 {NAMES.get(c,c)}({c})')

    print(f'\n{"="*60}')


if __name__ == '__main__':
    main()
