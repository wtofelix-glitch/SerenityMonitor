#!/usr/bin/env python3
"""
Serenity 每日工作流 — 一站式运行全部子系统

在收盘后 (16:00+) 运行:
    python3 daily_workflow.py              # 评分 + 反思 + 自动调仓建议
    python3 daily_workflow.py --push       # 同上 + 微信推送
    python3 daily_workflow.py --full       # 含回测快照 + IC评估
    python3 daily_workflow.py --execute    # 🚀 生成计划并自动执行交易

步骤:
  0. 参考数据拉取 (fetch_reference) → price_history
  1. 评分 (score_all) → scoring_history
  2. 信号 (generate_signals) → signal_log
  3. Outcome 补填 (fill_outcomes) → signal_log.outcome_*
  4. 反思 (generate_reflections) → score_reflections
  5. 反思收益补填 (reflection fill_outcomes)
  6. 自动调仓 (auto_execute) → 行动计划
  7. [可选] T1 回补检查
  8. [可选] 回测快照
  🚀 [--execute] 自动执行交易 + 更新 NAV
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import date, datetime


def step(name: str):
    print(f"\n{'─'*50}")
    print(f"  {name}")
    print(f"{'─'*50}")


def main():
    do_push = '--push' in sys.argv
    do_full = '--full' in sys.argv
    do_execute = '--execute' in sys.argv
    today = date.today().isoformat()

    # ── 0. 参考数据拉取 (指数/ETF) ──────────────────
    step('0/7 参考数据拉取')
    try:
        from fetch_reference import main as fetch_ref
        fetch_ref()
    except Exception as e:
        print(f"  ⚠️ 参考数据拉取失败: {e}")

    # ── 1. 多因子评分 ──────────────────────────────────────
    step('1/7 多因子评分')
    try:
        from scorer import score_all
        results = score_all()
        print(f"  ✅ 完成: {len(results)} 只标的已评分")
        for r in results[:5]:
            print(f"     {r['name']:6s} {r['code']} {r['total_score']:.0f}分 {r['signal_action']}")
    except Exception as e:
        print(f"  ⚠️ 评分失败: {e}")

    # ── 2. 信号 ──────────────────────────────────────
    step('2/7 交易信号')
    try:
        from signal_engine import generate_signals
        from config import ALL_CODES
        signals = generate_signals(codes=ALL_CODES)
        buy_signals = [s for s in signals if s.get('action') in ('STRONG_BUY', 'BUY')]
        sell_signals = [s for s in signals if s.get('action') in ('SELL', 'STOP_LOSS')]
        print(f"  ✅ {len(signals)} 信号 | 🟢买入{len(buy_signals)} 🔴卖出{len(sell_signals)}")
        for s in buy_signals:
            print(f"     🟢 {s['name']}({s['code']}) {s['action']} {s['total_score']:.0f}分")
        for s in sell_signals:
            print(f"     🔴 {s['name']}({s['code']}) {s['action']} {s['total_score']:.0f}分")
    except Exception as e:
        print(f"  ⚠️ 信号生成失败: {e}")

    # ── 3. Outcome 补填 ──────────────────────────────
    step('3/7 信号绩效补填')
    try:
        from serenity_calc_outcomes import calculate_outcomes
        calculate_outcomes()
        from db import get_conn
        _c = get_conn()
        _filled = _c.execute(
            "SELECT COUNT(*) FROM signal_log WHERE outcome_1d IS NOT NULL"
        ).fetchone()[0]
        _total = _c.execute("SELECT COUNT(*) FROM signal_log").fetchone()[0]
        _c.close()
        print(f"  📊 Outcome 填充率: {_filled}/{_total} ({_filled/_total*100:.1f}%)")
    except Exception as e:
        print(f"  ⚠️ Outcome 补填失败: {e}")

    # ── 4. 反思生成 ──────────────────────────────────
    step('4/7 评分反思')
    try:
        from reflection_engine import generate_all_reflections
        refs = generate_all_reflections()
        if refs:
            print(f"  ✅ 生成 {len(refs)} 条反思")
    except Exception as e:
        print(f"  ⚠️ 反思生成失败: {e}")

    # ── 5. 反思收益补填 ──────────────────────────────
    step('5/7 反思收益补填')
    try:
        from reflection_engine import fill_outcomes
        fill_outcomes(days_back=30)
    except Exception as e:
        print(f"  ⚠️ 反思收益补填失败: {e}")

    # ── 6. 自动调仓 ──────────────────────────────────
    step('6/7 自动调仓建议')
    plan = {"sells": [], "buys": [], "swaps": [], "summary": ""}
    try:
        from auto_execute import generate_execution_plan
        plan = generate_execution_plan()
        print(plan['summary'])
    except Exception as e:
        print(f"  ⚠️ 自动调仓失败: {e}")

    # ── 🚀 自动执行（--execute 模式）──────────────────
    if do_execute and (plan.get('sells') or plan.get('buys')):
        step('🚀 自动执行交易')
        try:
            from auto_execute import _record_execution_orders, _retry_pending_executions
            _record_execution_orders(plan)
            total_orders = len(plan["sells"]) + len(plan["buys"])
            print(f"  📝 记录 {total_orders} 笔待执行订单")
            executed = _retry_pending_executions(dry_run=False)
            print(f"  ✅ 执行完成: {executed}/{total_orders} 笔")
        except Exception as e:
            print(f"  ❌ 自动执行失败: {e}")

        # 执行后重算 NAV
        try:
            from portfolio import PortfolioManager
            from db import get_conn
            pm = PortfolioManager()
            val = pm.get_portfolio_value()
            conn = get_conn()
            conn.execute(
                "INSERT INTO nav_history "
                "(date, total_value, cash, holdings_value, profit_pct, positions_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (today, round(val["total_value"], 2), round(val["cash"], 2),
                 round(val["holdings_value"], 2), round(val["total_profit_pct"], 2),
                 str(val["position_count"])))
            conn.commit()
            conn.close()
            print(f"  📊 NAV 已更新: {val['total_value']:.0f} 元 ({val['total_profit_pct']:+.1f}%)")
        except Exception as e:
            print(f"  ⚠️ NAV 更新失败: {e}")

    # ── 7. T1 回补检查 ────────────────────────────────
    try:
        from tier1_reentry import check_tier1_reentry
        results = check_tier1_reentry()
        if results:
            print(f"  🔄 T1 回补机会: {len(results)} 只")
    except Exception as e:
        print(f"  ⚠️ T1 回补检查失败: {e}")

    # ── 推送 ─────────────────────────────────────────
    if do_push and (plan.get('sells') or plan.get('buys') or plan.get('swaps')):
        try:
            from notifier import send_message
            send_message(
                f"📊 Serenity 每日简报 {today}",
                plan['summary'],
                content_type="markdown",
            )
            print("\n📡 已推送")
        except Exception as e:
            print(f"\n⚠️ 推送失败: {e}")

    # ── Telegram 推送执行计划 ────────────────────────
    try:
        from signal_push import push_execution_plan
        push_execution_plan(plan)
        print("\n📡 Telegram 已推送")
    except Exception as e:
        print(f"\n⚠️ Telegram 推送失败: {e}")

    # ── 完整模式：回测快照 + IC ──────────────────────
    if do_full:
        step('+ 回测快照')
        try:
            import subprocess
            subprocess.run([sys.executable, 'quick_backtest.py'],
                           cwd=os.path.dirname(__file__))
        except Exception as e:
            print(f"  ⚠️ 回测快照失败: {e}")

        step('+ 维度 IC')
        try:
            from weight_adjuster import adjust_weights
            adjust_weights()
        except Exception as e:
            print(f"  ⚠️ IC 评估失败: {e}")

    print(f"\n{'='*50}")
    mode = "执行" if do_execute else "分析"
    print(f"  ✅ 每日工作流完成 [{mode}模式] | {datetime.now().strftime('%H:%M')}")
    print(f"{'='*50}")


if __name__ == '__main__':
    main()
