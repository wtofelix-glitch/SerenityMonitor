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
  0b. 真实行情记录 + T+1/T+6 outcome 结算 + 自动闸门评估
  1. 评分 (score_all) → scoring_history
  2. 信号 (generate_signals) → signal_log
  3. Outcome 补填 → signal_log.outcome_*
  3b. 信号绩效统计 → signal_performance 表
  4. 反思 (generate_reflections) → score_reflections
  5. 反思收益补填 (reflection fill_outcomes)
  6. 自动调仓 (auto_execute) → 行动计划
  7. T1 回补检查
  7b. 净值简报 (portfolio)
  7c. 行业轮动简报 (sector_rotation)
  7d. 信号绩效简报 (signal_performance)
  8. [可选] 回测快照
  📡 推送 (含净值 + 行业 + 绩效 + 执行计划)
  🚀 [--execute] 受控半自动排队（pending_confirm），v1 不提交实盘订单
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import date, datetime
from config import CAPITAL_CONFIG


def step(name: str):
    print(f"\n{'─'*50}")
    print(f"  {name}")
    print(f"{'─'*50}")


def run_real_data_gate_step(dry_run: bool = False) -> dict:
    """Record real market data, settle gate outcomes, then evaluate the gate."""
    import auto_gate
    import check_trading_day

    if check_trading_day.is_trading_day():
        record = auto_gate.record_real_data(dry_run=dry_run)
        print("  " + auto_gate.format_record_report(record).replace("\n", "\n  "))
    else:
        record = {
            "skipped": True,
            "dry_run": dry_run,
            "saved": 0,
            "count": 0,
            "low_quality": [],
            "missing": [],
            "source_errors": {},
        }
        print("  ⏭️ 非交易日，不记录新行情；继续处理已有样本结算")

    settle = auto_gate.settle_pending_signal_outcomes(dry_run=dry_run)
    print(
        "  settle outcomes "
        f"dry_run={settle['dry_run']} "
        f"settled={settle['settled']} "
        f"pending={settle['pending']} "
        f"expired_unsettled={settle['expired_unsettled']} "
        f"non_executable={settle['non_executable']}"
    )
    if settle.get("reason_counts"):
        print(f"  settlement blockers {settle['reason_counts']}")

    gate = auto_gate.evaluate_auto_gate(explain=True)
    print("  " + auto_gate.format_gate_report(gate).replace("\n", "\n  "))
    return {"skipped": False, "record": record, "settle": settle, "gate": gate}


def send_real_data_audit_push(result: dict, today: str | None = None) -> bool:
    """Push a compact audit trail for real data recording and gate state."""
    if result.get("skipped"):
        return False

    import notifier

    today = today or date.today().isoformat()
    record = result.get("record") or {}
    settle = result.get("settle") or {}
    gate = result.get("gate") or {}
    required = gate.get("required_sample_count") or 50
    state = gate.get("state") or "PAPER"
    compliance = gate.get("compliance_status") or "not_reported"

    lines = [
        f"## Serenity 真实数据审计 {today}",
        "",
        f"- 记录: {record.get('saved', 0)}/{record.get('count', 0)}",
        f"- 低置信: {len(record.get('low_quality') or [])}",
        f"- 缺失: {len(record.get('missing') or [])}",
        f"- 结算: {settle.get('settled', 0)}",
        f"- 待结算: {settle.get('pending', 0)}",
        f"- expired_unsettled: {settle.get('expired_unsettled', 0)}",
        f"- non_executable: {settle.get('non_executable', 0)}",
        "",
        f"- 闸门: {state}",
        f"- 样本: {gate.get('sample_count', 0)}/{required}",
        f"- 胜率: {(gate.get('win_rate') or 0) * 100:.1f}%",
        f"- Wilson 下界: {(gate.get('wilson_lower') or 0) * 100:.1f}%",
        f"- 合规: {compliance}",
    ]
    errors = record.get("source_errors") or {}
    if errors:
        lines.extend(["", "- 数据源 warning: " + ", ".join(sorted(errors.keys()))])

    summary = f"{state} {gate.get('sample_count', 0)}/{required} {compliance}"
    notifier.send_message(
        f"Serenity 真实数据审计 {today}",
        "\n".join(lines),
        content_type="markdown",
        summary=summary,
    )
    return True


def _stage_pending_confirm_orders(plan: dict) -> int:
    """Stage generated orders into order_state_log without submitting trades."""
    import auto_gate

    count = 0
    for group in ("sells", "buys"):
        for order in plan.get(group, []):
            code = order["code"]
            action = "SELL" if group == "sells" else "BUY"
            price = order.get("price") or (
                order.get("estimated_proceeds", 0) / max(order.get("shares", 1), 1)
            )
            amount = order.get("amount") or order.get("estimated_proceeds", 0)
            key = f"{plan['date']}:{action}:{code}:{order.get('shares', 0)}:{round(float(amount or 0), 2)}"
            reason = str(order.get("reason") or order.get("reasons") or "")
            auto_gate.create_order_state(
                code, action, "generated", price=price,
                shares=order.get("shares", 0), amount=amount,
                reason=reason, idempotency_key=key,
            )
            auto_gate.create_order_state(
                code, action, "pending_confirm", price=price,
                shares=order.get("shares", 0), amount=amount,
                reason="awaiting manual confirmation", idempotency_key=key,
            )
            count += 1
    return count


def run_controlled_execution_step(plan: dict, dry_run: bool = False) -> dict:
    """Gate `--execute` behind SEMI_AUTO and stage orders only in v1."""
    import auto_gate

    gate = auto_gate.evaluate_auto_gate(explain=False)
    print("  " + auto_gate.format_gate_report(gate).replace("\n", "\n  "))
    total_orders = len(plan.get("sells", [])) + len(plan.get("buys", []))
    if total_orders == 0:
        print("  ✅ 无需排队订单")
        return {"blocked": False, "staged": 0, "gate": gate}
    if dry_run:
        print(f"  DRY-RUN: {total_orders} 笔订单未排队、未提交")
        return {"blocked": False, "staged": 0, "gate": gate, "dry_run": True}
    if gate.get("state") != "SEMI_AUTO":
        print("  blocked: SEMI_AUTO requires passing gate + compliance_status=approved")
        return {"blocked": True, "staged": 0, "gate": gate}

    staged = _stage_pending_confirm_orders(plan)
    print(f"  ✅ SEMI_AUTO 已排队 {staged} 笔 pending_confirm；v1 不提交实盘订单")
    return {"blocked": False, "staged": staged, "gate": gate}


def main():
    do_push = '--push' in sys.argv
    do_full = '--full' in sys.argv
    do_execute = '--execute' in sys.argv
    dry_run = '--dry-run' in sys.argv
    today = date.today().isoformat()
    from check_trading_day import is_trading_day
    trading_day = is_trading_day()
    real_data_gate_result = {"skipped": True, "record": {}, "settle": {}, "gate": {}}

    # ── 0. 参考数据拉取 (指数/ETF) ──────────────────
    step('0/8 参考数据拉取')
    try:
        if trading_day:
            from fetch_reference import main as fetch_ref
            fetch_ref()
        else:
            print("  ⏭️ 非交易日，跳过参考数据拉取")
    except Exception as e:
        print(f"  ⚠️ 参考数据拉取失败: {e}")

    # ── 0b. 真实数据记录 + 自动闸门 ───────────────────────
    step('0b/8 真实数据记录 + 自动闸门')
    try:
        real_data_gate_result = run_real_data_gate_step(dry_run=dry_run)
    except Exception as e:
        print(f"  ⚠️ 真实数据/自动闸门失败: {e}")

    if not trading_day:
        if do_push:
            try:
                send_real_data_audit_push(real_data_gate_result, today=today)
            except Exception as e:
                print(f"  ⚠️ 非交易日审计推送失败: {e}")
        print(f"\n{'='*50}")
        print(f"  非交易日维护完成：未评分、未生成信号、未执行 PAPER 交易")
        print(f"{'='*50}")
        return

    # ── 1. 多因子评分 ──────────────────────────────────────
    step('1/8 多因子评分')
    _scorer_results = None
    try:
        from scorer import score_all
        _scorer_results = score_all()
        print(f"  ✅ 完成: {len(_scorer_results)} 只标的已评分")
        for r in _scorer_results[:5]:
            print(f"     {r['name']:6s} {r['code']} {r['total_score']:.0f}分 {r['signal_action']}")
    except Exception as e:
        print(f"  ⚠️ 评分失败: {e}")

    # ── 1b. Frozen Baseline 对比 (v4 Phase 2) ──────────────
    step('1b Frozen Baseline 三系统对比')
    _frozen_cmp = None
    try:
        from frozen_baseline import get_comparator as _get_cmp
        from equal_weight_basket import get_basket as _get_basket
        _cmp = _get_cmp()
        _eq = _get_basket()

        # 构建 snapshot 格式
        _snaps = [{
            "code": r["code"], "close": r.get("close", 0),
            "change_pct": r.get("change_pct", 0),
            "volume": r.get("close", 0) * 10000,  # 粗略估算
        } for r in _scorer_results] if _scorer_results else []

        if _snaps:
            _frozen_cmp = _cmp.compare_signals(_snaps, _scorer_results or [])
            _eq_ret = _eq.get_daily_return(_snaps)
            _div = _frozen_cmp["divergence_count"]
            _agree = _frozen_cmp["agreement_matrix"]["agreement_rate"]
            print(f"  ✅ Frozen vs Adaptive: {_div} 分歧, 一致率 {_agree:.0%}")
            print(f"  ✅ Equal Weight 日收益: {_eq_ret:+.2%}")
            if _div > 0:
                for d in _frozen_cmp["divergence_details"]:
                    print(f"     ⚠️ {d['code']}: Frozen={d['frozen']} Adaptive={d['adaptive']}")
    except Exception as e:
        print(f"  ⚠️ Frozen Baseline 对比失败: {e}")

    # ── 2. 信号 ──────────────────────────────────────
    step('2/8 交易信号')
    try:
        from signal_engine import generate_signals
        from config import ALL_CODES
        # 使用 Step1 的统一评分结果（确保与 auto-execute 信号一致）
        scorer_scores = {r["code"]: r["total_score"] for r in _scorer_results} if _scorer_results else None
        signals = generate_signals(codes=ALL_CODES, scorer_total_scores=scorer_scores)
        buy_signals = [s for s in signals if s.get('action') in ('STRONG_BUY', 'BUY')]
        sell_signals = [s for s in signals if s.get('action') in ('SELL', 'STOP_LOSS')]
        print(f"  ✅ {len(signals)} 信号 | 🟢买入{len(buy_signals)} 🔴卖出{len(sell_signals)}")
        for s in buy_signals:
            print(f"     🟢 {s['name']}({s['code']}) {s['action']} {s['total_score']:.0f}分")
        for s in sell_signals:
            print(f"     🔴 {s['name']}({s['code']}) {s['action']} {s['total_score']:.0f}分")
    except Exception as e:
        print(f"  ⚠️ 信号生成失败: {e}")

    # ── 2b. 纸面自动交易（为闸门积累样本） ──────────────
    step('2b/8 纸面自动交易')
    try:
        from paper_trader import PaperTrader
        pt = PaperTrader()
        paper_results = pt.auto_trade_from_signals(today.isoformat())
        if paper_results:
            bought = [r for r in paper_results if r.get("status") == "buy"]
            print(f"  ✅ 纸面开仓 {len(bought)} 笔")
            for r in bought:
                print(f"     📝 {r.get('name','?')}({r.get('code','?')}) {r.get('shares',0)}股 @¥{r.get('price',0):.2f}")
        else:
            print(f"  ℹ️ 无符合条件的纸面交易")
    except Exception as e:
        print(f"  ⚠️ 纸面交易失败: {e}")

    # ── 2c. 哨兵结算 — 大师预测追踪闭环 ──────────────
    step('2c/8 哨兵观测结算')
    try:
        from db import settle_sentinel_outcomes
        sentinel_result = settle_sentinel_outcomes(lookback_days=7)
        print(f"  ✅ 结算 {sentinel_result['settled']} 条观测")
        rank = sentinel_result.get("accuracy_ranking", [])
        if rank:
            print(f"  📊 大师准确率 TOP3:")
            for r in rank[:3]:
                total = r['total_predictions']
                correct = r['correct_count']
                acc = correct / total * 100 if total > 0 else 0
                print(f"     {r['source_name']}: {acc:.0f}% ({correct}/{total})")
    except Exception as e:
        print(f"  ⚠️ 哨兵结算失败: {e}")

    # ── 3. Outcome 补填 ──────────────────────────────
    step('3/8 信号绩效补填')
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

    # ── 3b. 信号绩效分析 ────────────────────────────
    step('3b/8 信号绩效统计')
    try:
        from signal_performance import update_signal_performance_table
        result = update_signal_performance_table()
        print(f"  ✅ {result['updated']} 条写入, {result['skipped']} 条跳过")
    except Exception as e:
        print(f"  ⚠️ 信号绩效统计失败: {e}")

    # ── 3c. 信号质量检查 ────────────────────────────
    step('3c/8 信号质量检查')
    try:
        from signal_performance import check_signal_quality, format_quality_alerts
        quality_alerts = check_signal_quality(min_samples=5)
        quality_msg = format_quality_alerts(quality_alerts)
        print(f"  {quality_msg}")
    except Exception as e:
        print(f"  ⚠️ 信号质量检查失败: {e}")

    # ── 4. 反思生成 ──────────────────────────────────
    step('4/8 评分反思')
    try:
        from reflection_engine import generate_all_reflections
        refs = generate_all_reflections()
        if refs:
            print(f"  ✅ 生成 {len(refs)} 条反思")
    except Exception as e:
        print(f"  ⚠️ 反思生成失败: {e}")

    # ── 5. 反思收益补填 ──────────────────────────────
    step('5/8 反思收益补填')
    try:
        from reflection_engine import fill_outcomes, persist_dimension_ic
        fill_outcomes(days_back=30)
        ic_stats = persist_dimension_ic(days_back=30, window=20)
        if ic_stats.get("rows"):
            print(f"  ✅ 维度IC写回: {ic_stats['rows']} 行 / {ic_stats['dates']} 天")
    except Exception as e:
        print(f"  ⚠️ 反思收益补填失败: {e}")

    # ── 4b. UZI 证据自动采集 ──────────────────────────
    step('4b/8 UZI证据采集')
    try:
        from uzi_evidence_collector import collect_evidence_for_all
        ev_result = collect_evidence_for_all()
        if ev_result.get("added", 0) > 0:
            print(f"  ✅ 新增 {ev_result['added']} 条证据, {ev_result['updated']} 条去重")
        else:
            print(f"  ℹ️ 无新增证据 (已检查 {ev_result.get('checked', 0)} 只标的)")
    except ImportError:
        print(f"  ⚠️ uzi_evidence_collector 暂不可用")
    except Exception as e:
        print(f"  ⚠️ UZI证据采集失败: {e}")

    # ── 5b. 评分权重自动调优 ─────────────────────────
    step('5b/8 权重自动调优')
    try:
        from reflection_engine import apply_reflection_adjustments
        apply_reflection_adjustments(days=20)
    except Exception as e:
        print(f"  ⚠️ 权重调优失败: {e}")

    # ── 5c. 维度IC自动分析 ──────────────────────────
    step('5c/8 维度IC自动分析')
    try:
        from factor_ic import recommend_dimension_changes, format_recommendation_report
        rec = recommend_dimension_changes(days=30, window=14)
        print(f"  {rec['summary']}")
    except Exception as e:
        print(f"  ⚠️ IC分析失败: {e}")

    # ── 6. 自动调仓 ──────────────────────────────────
    step('6/8 自动调仓建议')
    plan = {"sells": [], "buys": [], "swaps": [], "summary": ""}
    try:
        from auto_execute import generate_execution_plan
        plan = generate_execution_plan()
        print(plan['summary'])
    except Exception as e:
        print(f"  ⚠️ 自动调仓失败: {e}")

    # ── v5.2 月度参数寻优(每月第一个交易日) ──────────
    try:
        from backtest_engine import grid_search
        if today.day <= 7 and today.weekday() < 5:
            step('6b/8 回测参数寻优')
            best = grid_search()
            if best.get("params"):
                print(f"  ✅ 最优: buy_th={best['params']['buy_threshold']} sl={best['params']['stop_loss_pct']} sharpe={best['sharpe']:.2f} wr={best['win_rate']}%")
    except Exception:
        pass

    # ── v5.2 全市场扫描(每周六) ─────────────────────
    try:
        if today.weekday() == 5:  # 周六
            step('6c/8 全市场扫描')
            from candidate_scanner_full import scan_mainboard
            top = scan_mainboard(top_n=10)
            if top:
                print(f"  ✅ 全市场 TOP10:")
                for i, c in enumerate(top[:10]):
                    print(f"     {i+1}. {c['name']:6s}({c['code']}) {c['total_score']:.0f}分 {c.get('suggestion','')}")
    except Exception as e:
        print(f"  ⚠️ 全市场扫描失败: {e}")

    # ── 🚀 v5.0 自动决策（--auto 模式）──────────────
    do_auto = '--auto' in sys.argv
    do_live = '--live' in sys.argv
    if do_auto and (plan.get('sells') or plan.get('buys')):
        step('🚀 闸门自动决策')
        try:
            from auto_execute import auto_execute_if_gate_allows
            result = auto_execute_if_gate_allows(plan)
            if result.get("staged", 0) > 0:
                print(f"  ✅ {result['staged']} 笔已排队")
            elif result.get("paper_trades", 0) > 0:
                print(f"  📝 {result['paper_trades']} 笔纸面交易")
        except Exception as e:
            print(f"  ❌ 自动决策失败: {e}")

    # ── v5.6 微仓实盘: --live 模式 ─────────────────────
    if do_live and (plan.get('sells') or plan.get('buys')):
        step('💰 微仓实盘执行')
        try:
            from ths_bridge import auto_execute_to_ths
            # 微仓限制: 单笔 ≤ ¥5000, 每日 ≤ ¥10000
            MAX_PER_TRADE = 5000
            MAX_DAILY = 10000
            scaled_plan = {
                "sells": plan.get("sells", []),
                "buys": [{
                    **b,
                    "shares": min(b.get("shares", 100), int(MAX_PER_TRADE / max(b.get("price", 50), 1) / 100) * 100),
                    "amount": min(b.get("amount", 0) or b.get("price", 50) * b.get("shares", 100), MAX_PER_TRADE),
                    "reasons": b.get("reasons", ["自动信号"])[:1] + ["微仓实盘"]
                } for b in plan.get("buys", [])[:2]],  # 每日最多2笔
            }
            total_amount = sum(b.get("amount", 0) for b in scaled_plan["buys"])
            if total_amount > MAX_DAILY:
                scaled_plan["buys"] = scaled_plan["buys"][:1]
            result = auto_execute_to_ths(scaled_plan, dry_run=False)
            buys_ok = [r for r in result.get("buys", []) if r.get("status") == "pending_confirm"]
            sells_ok = [r for r in result.get("sells", []) if r.get("status") == "pending_confirm"]
            print(f"  💰 微仓: {len(buys_ok)}买 {len(sells_ok)}卖 (单笔≤¥{MAX_PER_TRADE:,})")
            for r in buys_ok:
                print(f"     📋 {r.get('instruction', r.get('code','?'))[:80]}")
        except Exception as e:
            print(f"  ❌ 微仓实盘失败: {e}")

    # ── 7. T1 回补检查 ────────────────────────────────
    try:
        from tier1_reentry import check_tier1_reentry
        results = check_tier1_reentry()
        if results:
            print(f"  🔄 T1 回补机会: {len(results)} 只")
    except Exception as e:
        print(f"  ⚠️ T1 回补检查失败: {e}")

    # ── 7a. 纸面交易日终结算 ────────────────────────────
    step('7a/8 纸面交易结算')
    try:
        from paper_trader import get_paper_trader
        pt = get_paper_trader()
        auto = pt.auto_execute_signals(max_buy=2)
        mtm = pt.mark_to_market()
        lines = []
        if auto.get("bought", 0) > 0:
            for t in auto["trades"]:
                lines.append(f"{t['name']} {t['shares']}股@{t['price']:.2f}")
        if lines:
            print(f"  🟢 买入: {', '.join(lines)}")
        print(f"  📊 净值 ¥{mtm.get('total_value',0):,.0f} ({mtm.get('total_pnl_pct',0):+.1f}%)")
    except Exception as e:
        print(f"  ⚠️ 纸面交易失败: {e}")

    # ── 7b. 净值简报 ───────────────────────────────────
    nav_summary_lines = []
    try:
        from portfolio import PortfolioManager
        pm = PortfolioManager()
        val = pm.get_portfolio_value()
        nav_summary_lines = [
            f"💰 组合净值概览",
            f"  总资产: {val['total_value']:.0f} 元",
            f"  现金: {val['cash']:.0f} 元",
            f"  持仓市值: {val['holdings_value']:.0f} 元",
            f"  总盈亏: {val['total_profit_pct']:+.2f}%",
            f"  持仓数: {val['position_count']} 只",
        ]
        print(f"\n  💰 净值概览:")
        for line in nav_summary_lines:
            print(f"     {line}")
    except Exception as e:
        print(f"  ⚠️ 净值获取失败: {e}")

    # ── 7f. v4.0 复利进度 + 权重进化 (周一执行) ───────────
    try:
        nav_growth = val["total_value"] / CAPITAL_CONFIG["initial_capital"]
        target_mult = CAPITAL_CONFIG["target_capital"] / CAPITAL_CONFIG["initial_capital"]
        progress_pct = (nav_growth - 1) / (target_mult - 1) * 100
        print(f"\n  🎯 翻倍进度: {nav_growth:.2f}x (目标 {target_mult:.1f}x, 进度 {progress_pct:.0f}%)")

        # 周一度量进化
        if today.weekday() == 0:
            from scorer import load_evolved_weights, get_stock_predictability
            evolved = load_evolved_weights()
            print(f"\n  🧬 权重进化 (周一):")
            defaults = {"zone": 0.20, "momentum": 0.18, "volume": 0.04, "serenity": 0.17, "factor": 0.19, "technical": 0.10, "moat": 0.09, "capital": 0.03}
            for dim in evolved:
                old = defaults.get(dim, 0)
                new = evolved.get(dim, 0)
                delta = new - old
                if abs(delta) >= 0.005:
                    print(f"    {dim}: {old:.2f} → {new:.2f} ({delta:+.2f})")

            # 选股可靠性排行
            print(f"\n  📊 选股可预测性评级:")
            from config import ALL_CODES
            grades = []
            for code in ALL_CODES:
                pred = get_stock_predictability(code)
                grades.append((code, pred["grade"], pred["predictability"], pred["sample_count"]))
            grades.sort(key=lambda x: -x[2])
            for code, grade, pct, n in grades:
                icon = "🟢" if grade == "A" else "🟡" if grade == "B" else "🟠" if grade == "C" else "🔴"
                print(f"    {icon} {code} {grade}级 ({pct}% 准确率, {n}样本)")
    except Exception as e:
        print(f"  ⚠️ 进度/进化失败: {e}")

    # ── 7c. 行业轮动简报 ──────────────────────────────
    sector_brief = ""
    sector_detail = ""
    try:
        from sector_rotation import get_sector_rotation_summary, get_sector_rotation_detail
        sector_brief = get_sector_rotation_summary()
        sector_detail = get_sector_rotation_detail()
        print(f"\n  📊 行业轮动:")
        for line in sector_brief.split("\n")[:4]:
            print(f"     {line}")
    except Exception as e:
        print(f"  ⚠️ 行业轮动扫描失败: {e}")

    # ── 7d. 信号绩效简报 ──────────────────────────────
    perf_summary_lines = []
    try:
        from signal_performance import get_performance_report
        perf = get_performance_report()
        ps = perf["summary"]
        perf_summary_lines = [
            f"📊 信号绩效统计",
            f"  信号总数: {ps['total_signals']} | 已结算: {ps['signals_with_outcome']}",
        ]
        if ps["overall_win_rate_1d"] is not None:
            perf_summary_lines.append(f"  整体胜率: {ps['overall_win_rate_1d']*100:.1f}% | 均收益: {ps['overall_avg_return_1d']:+.2f}%")
        if ps["best_action"]:
            perf_summary_lines.append(f"  最佳信号: {ps['best_action']} ({ps['best_action_win_rate']*100:.1f}%)")
        print(f"\n  📊 信号绩效:")
        for line in perf_summary_lines:
            print(f"     {line}")
    except Exception as e:
        print(f"  ⚠️ 信号绩效统计失败: {e}")

    # ── 推送（含净值 + 行业简报 + 绩效摘要）─────────────────
    if do_push:
        try:
            if send_real_data_audit_push(real_data_gate_result, today=today):
                print("\n📡 真实数据审计已推送")
        except Exception as e:
            print(f"\n⚠️ 真实数据审计推送失败: {e}")

        push_lines = []
        # 净值概览
        if nav_summary_lines:
            push_lines.append("```")
            push_lines.extend(nav_summary_lines)
            push_lines.append("```")
            push_lines.append("")
        # 调仓计划
        if plan.get('sells') or plan.get('buys') or plan.get('swaps'):
            push_lines.append(plan['summary'])
            push_lines.append("")
        # 行业简报
        if sector_brief:
            push_lines.append(sector_brief)
            push_lines.append("")
        # 绩效简报
        if perf_summary_lines:
            push_lines.append("\n".join(perf_summary_lines))
            push_lines.append("")

        if push_lines:
            push_msg = "\n".join(push_lines)
            try:
                from notifier import send_message
                send_message(
                    f"📊 Serenity 每日简报 {today}",
                    push_msg,
                    content_type="markdown",
                )
                print("\n📡 推送成功")
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

    # ── 操作摘要 ────────────────────────────────────────
    try:
        from db import get_conn
        conn = get_conn()
        rows = conn.execute("""
            SELECT code, action, total_score, is_holding
            FROM signal_log
            WHERE date = (SELECT MAX(date) FROM signal_log)
            ORDER BY total_score DESC
        """).fetchall()
        conn.close()
        if rows:
            held = [f"⭐{r[0]}({r[2]:.0f})" for r in rows if r[3]]
            buys = [f"{r[0]}({r[2]:.0f})" for r in rows if not r[3] and r[1] in ('STRONG_BUY','BUY','CAUTION_BUY')]
            sells = [f"{r[0]}({r[2]:.0f})" for r in rows if r[1] in ('SELL','WEAK_HOLD')]
            print(f"\n{'='*50}")
            print(f"📋 Serenity 操作摘要")
            print(f"  持有: {', '.join(held) if held else '无'}")
            print(f"  可买: {', '.join(buys[:3]) if buys else '无'}{'...' if len(buys) > 3 else ''}")
            print(f"  卖出: {', '.join(sells[:3]) if sells else '无'}{'...' if len(sells) > 3 else ''}")
    except Exception:
        pass

    # ── 买入候选（观察池筛选） ──────────────────────────
    if _scorer_results:
        try:
            from db import get_buy_candidates
            candidates = get_buy_candidates(_scorer_results, top_n=3)
            if candidates:
                print(f"\n{'─'*50}")
                print(f"🔍 观察池候选推荐")
                for i, c in enumerate(candidates):
                    print(f"  {i+1}. {c['name']}({c['code']}) {c['total_score']:.0f}分 {c.get('action','')} 动量{c['momentum_score']:.0f} UZI{c['uzi_score']:.0f}")
        except Exception:
            pass

    # 信号多样性检查
    if not buys:
        print(f"\n  ⚠️ 信号多样性警告: 今日无买入类信号，建议关注候选推荐")

    # 同步持仓到 Mission Control
    try:
        import subprocess
        bridge = os.path.expanduser("~/.hermes/scripts/serenity_bridge.sh")
        if os.path.exists(bridge):
            result = subprocess.run(["bash", bridge], capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                print(f"\n  📡 Mission Control 同步成功")
            else:
                print(f"\n  ⚠️ Mission Control 同步失败: {result.stderr.strip()}")
    except Exception as e:
        print(f"\n  ⚠️ Mission Control 同步异常: {e}")

    print(f"\n{'='*50}")
    mode = "执行" if do_execute else "分析"
    print(f"  ✅ 每日工作流完成 [{mode}模式] | {datetime.now().strftime('%H:%M')}")
    print(f"{'='*50}")


if __name__ == '__main__':
    main()
