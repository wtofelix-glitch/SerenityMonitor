#!/usr/bin/env python3
"""
Serenity 系统健康诊断
检查数据完整性、cron 状态、DB 健康

用法: python3 cli.py health
"""
import sqlite3, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from datetime import date, timedelta

DB_PATH = os.path.join(os.path.dirname(__file__), 'serenity.db')


def check_db_integrity():
    """检查数据库表完整性"""
    conn = sqlite3.connect(DB_PATH)
    tables = {
        'stocks': '标的配置',
        'daily_snapshots': '每日快照',
        'trades': '交易记录',
        'alerts': '预警记录',
        'scoring_history': '评分历史',
        'price_history': '价格历史',
        'signal_log': '信号日志',
        'anomalies': '异常事件',
        'score_reflections': '评分反思',
    }
    results = []
    for table, label in tables.items():
        try:
            cnt = conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
            results.append((table, label, cnt, 'OK'))
        except Exception as e:
            results.append((table, label, 0, f'ERR: {e}'))
    conn.close()
    return results


def check_price_data_coverage():
    """检查价格历史数据覆盖情况"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # Date range
    dr = conn.execute(
        'SELECT MIN(date) as s, MAX(date) as e, COUNT(DISTINCT date) as d FROM price_history'
    ).fetchone()

    # Per-stock coverage
    stocks = conn.execute(
        'SELECT code, COUNT(DISTINCT date) as days, MAX(date) as last_date FROM price_history GROUP BY code ORDER BY days DESC'
    ).fetchall()

    conn.close()

    return {
        'first_date': dr['s'],
        'last_date': dr['e'],
        'distinct_dates': dr['d'],
        'stocks': [(s['code'], s['days'], s['last_date']) for s in stocks],
    }


def check_scoring_coverage():
    """检查评分覆盖情况"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    dr = conn.execute(
        'SELECT COUNT(DISTINCT date) as d, MAX(date) as last FROM scoring_history'
    ).fetchone()

    latest = conn.execute('''
        SELECT code, total_score FROM scoring_history
        WHERE date = (SELECT MAX(date) FROM scoring_history)
        ORDER BY total_score DESC
    ''').fetchall()

    # Check for gaps > 2 days in last 30 days
    recent_dates = conn.execute('''
        SELECT DISTINCT date FROM scoring_history
        WHERE date >= date('now', '-30 days')
        ORDER BY date
    ''').fetchall()

    conn.close()

    gaps = []
    prev = None
    for r in recent_dates:
        d = r['date']
        if prev:
            diff = (date.fromisoformat(d) - date.fromisoformat(prev)).days
            if diff > 2:
                gaps.append((prev, d, diff))
        prev = d

    return {
        'distinct_dates': dr['d'],
        'last_date': dr['last'],
        'gaps_last_30d': gaps,
        'latest_scores': [(s['code'], s['total_score']) for s in latest],
    }


def check_signal_outcome_health():
    """检查信号 outcome 填充率"""
    conn = sqlite3.connect(DB_PATH)
    total = conn.execute('SELECT COUNT(*) FROM signal_log').fetchone()[0]
    filled = conn.execute(
        'SELECT COUNT(*) FROM signal_log WHERE outcome_1d IS NOT NULL'
    ).fetchone()[0]
    conn.close()
    return {'total': total, 'filled': filled, 'pct': round(filled / total * 100, 1) if total > 0 else 0}


def main():
    print(f'🔬 Serenity 系统健康诊断 | {date.today()}')
    print('=' * 60)

    # 1. DB Integrity
    print('\n📦 数据库表')
    for table, label, cnt, status in check_db_integrity():
        icon = '✅' if status == 'OK' else '❌'
        print(f'  {icon} {label:10s} ({table:20s}): {cnt:>6} 条')

    # 2. Price data coverage
    cov = check_price_data_coverage()
    print(f'\n📈 价格数据: {cov["first_date"]} ~ {cov["last_date"]} ({cov["distinct_dates"]} 个交易日)')
    for code, days, last in cov['stocks']:
        gap = (date.today() - date.fromisoformat(last)).days if last else 999
        icon = '✅' if gap <= 2 else '⚠️' if gap <= 5 else '🔴'
        print(f'  {icon} {code}: {days:>4}d 最后: {last} ({gap}d前)')

    # 3. Scoring coverage
    sc = check_scoring_coverage()
    print(f'\n📊 评分数据: {sc["distinct_dates"]} 天, 最后: {sc["last_date"]}')
    if sc['gaps_last_30d']:
        print('  ⚠️ 评分缺口:')
        for s, e, d in sc['gaps_last_30d']:
            print(f'    {s} → {e} (间隔 {d} 天)')
    else:
        print('  ✅ 近 30 天无评分缺口')
    print('  最新评分:')
    for code, score in sc['latest_scores']:
        print(f'    {code}: {score:.0f}')

    # 4. Outcome health
    oh = check_signal_outcome_health()
    icon = '✅' if oh['pct'] >= 70 else '⚠️' if oh['pct'] >= 30 else '🔴'
    print(f'\n📡 信号 outcome: {oh["filled"]}/{oh["total"]} ({oh["pct"]}%) {icon}')

    # 5. Score reflections
    conn = sqlite3.connect(DB_PATH)
    ref_total = conn.execute('SELECT COUNT(*) FROM score_reflections').fetchone()[0]
    ref_filled = conn.execute(
        'SELECT COUNT(*) FROM score_reflections WHERE actual_return_1d IS NOT NULL'
    ).fetchone()[0]
    conn.close()
    ref_pct = round(ref_filled / ref_total * 100, 1) if ref_total > 0 else 0
    icon_r = '✅' if ref_pct >= 50 else '⚠️' if ref_total > 0 else '⚪'
    print(f'🧠 反思收益: {ref_filled}/{ref_total} ({ref_pct}%) {icon_r}')

    # 6. File integrity
    print(f'\n📁 关键文件')
    required = ['config.py', 'scorer.py', 'db.py', 'data_engine.py',
                'portfolio.py', 'signal_engine.py', 'monitor.py',
                'auto_execute.py', 'daily_workflow.py']
    for f in required:
        path = os.path.join(os.path.dirname(__file__), f)
        ok = os.path.exists(path)
        print(f'  {"✅" if ok else "❌"} {f}')

    print(f'\n{"="*60}')
    print('🏁 诊断完成')


if __name__ == '__main__':
    main()
