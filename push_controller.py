"""
统一推送控制器 — push_controller.py

消除重复推送、诊断噪声和碎片化消息。每天仅一次推送（收盘后 15:30），
只包含可执行信息。所有后台数据采集、哨兵结算、研究引擎输出不再推送。

规则:
  1. 每天最多推送 1 次（15:30 收盘后，仅交易日）
  2. 推送内容：净值 + 持仓 + 信号 + 调仓建议 + 告警
  3. 闸门状态/Wilson下界/合规状态/审计统计 → 看板查看，不推送
  4. 无调仓建议且无告警 → 静默，不推送
  5. 非交易日 → 静默，不推送

Usage:
    python3 push_controller.py              # 生成并推送每日简报
    python3 push_controller.py --dry-run    # 仅打印，不推送
    python3 push_controller.py --force      # 强制推送（忽略静默规则）
"""

from __future__ import annotations

import sys
import os
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from serenity_logger import get_logger

log = get_logger(__name__)


def _is_trading_day(d: date | None = None) -> bool:
    try:
        from check_trading_day import is_trading_day
        return is_trading_day(d)
    except Exception:
        return True  # 无法判断时默认允许


def _already_pushed_today() -> bool:
    """检查今天是否已经推送过（防止重复）。"""
    state_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".push_state.json")
    import json
    today = date.today().isoformat()
    try:
        if os.path.exists(state_file):
            with open(state_file) as f:
                data = json.load(f)
            if data.get("date") == today and data.get("pushed"):
                return True
    except Exception:
        pass
    return False


def _mark_pushed() -> None:
    state_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".push_state.json")
    import json
    with open(state_file, "w") as f:
        json.dump({"date": date.today().isoformat(), "pushed": True, "at": datetime.now().isoformat()}, f)


def _build_nav_section() -> str | None:
    """净值概览"""
    try:
        from portfolio import get_portfolio
        pm = get_portfolio()
        pv = pm.get_portfolio_value()
        cash = pv.get("cash", 0)
        holdings = pv.get("holdings_value", 0)
        total = pv.get("total_value", 0)
        profit_pct = pv.get("total_profit_pct", 0)
        profit_amt = pv.get("total_profit_amount", 0)
        pos_count = pv.get("position_count", 0)

        emoji = "🟢" if profit_pct >= 0 else "🔴"
        lines = [
            f"## 💰 净值概览",
            f"  总资产 ¥{total:,.0f} | {emoji} {profit_pct:+.1f}% (¥{profit_amt:+,.0f})",
            f"  现金 ¥{cash:,.0f} | 持仓 ¥{holdings:,.0f} | {pos_count} 只",
        ]

        # 持仓明细
        positions = pv.get("position_details", [])
        if positions:
            lines.append("")
            lines.append("| 标的 | 成本 | 现价 | 盈亏 | 信号 |")
            lines.append("|------|------|------|------|------|")
            for p in positions:
                code = p.get("code", "")
                name = p.get("name", code)
                cost = p.get("buy_price", 0)
                price = p.get("current_price", 0)
                pnl = p.get("profit_pct", 0)
                signal = p.get("signal_action", "") or (p.get("signal") if isinstance(p.get("signal"), str) else "")
                pnl_e = "🟢" if pnl >= 0 else "🔴"
                lines.append(f"| {name}({code}) | ¥{cost:.1f} | ¥{price:.1f} | {pnl_e} {pnl:+.1f}% | {signal} |")

        return "\n".join(lines)
    except Exception as e:
        return f"⚠️ 净值获取失败: {e}"


def _build_signal_section() -> str | None:
    """今日信号摘要（仅可执行信号）"""
    try:
        from scorer import score_all
        results = score_all()
        buys = [r for r in results if r.get("signal_action") in ("STRONG_BUY", "BUY")]
        sells = [r for r in results if r.get("signal_action") in ("SELL", "TAKE_PROFIT", "STOP_LOSS")]

        if not buys and not sells:
            return None  # 无信号时不推送这一节

        lines = ["## 📡 今日信号"]
        if buys:
            lines.append(f"  🟢 买入信号 ({len(buys)}):")
            for r in buys[:5]:
                lines.append(f"    {r['name']}({r['code']}) {r['signal_action']} {r['total_score']:.0f}分")
        if sells:
            lines.append(f"  🔴 卖出信号 ({len(sells)}):")
            for r in sells[:5]:
                lines.append(f"    {r['name']}({r['code']}) {r['signal_action']} {r['total_score']:.0f}分")
        return "\n".join(lines)
    except Exception as e:
        return f"⚠️ 信号获取失败: {e}"


def _build_execution_section() -> str | None:
    """调仓建议"""
    try:
        from auto_execute import generate_execution_plan
        plan = generate_execution_plan(dry_run=True)
        sells = plan.get("sells", [])
        buys = plan.get("buys", [])
        swaps = plan.get("swaps", [])

        if not sells and not buys and not swaps:
            return None

        lines = ["## 🎯 调仓建议"]
        if sells:
            for s in sells:
                lines.append(f"  🔴 卖出 {s.get('name','')}({s.get('code','')}) {s.get('shares',0)}股 | {s.get('reason','')}")
        if buys:
            for b in buys:
                lines.append(f"  🟢 买入 {b.get('name','')}({b.get('code','')}) {b.get('shares',0)}股 @{b.get('price',0)} | 评分{b.get('score',0):.0f}")
        if swaps:
            lines.append(f"  🔄 {len(swaps)} 笔换仓建议")

        if plan.get("summary"):
            lines.append(f"\n  {plan['summary'][:200]}")
        return "\n".join(lines)
    except Exception as e:
        return f"⚠️ 调仓计划获取失败: {e}"


def _build_alert_section() -> str | None:
    """告警摘要"""
    alerts = []
    try:
        from price_alert import check
        triggered = check()
        if triggered:
            for t in triggered[:5]:
                alerts.append(f"  ⚠️ {t.get('name','')}({t.get('code','')}) {t.get('condition','')} @{t.get('price',0)}")
    except Exception:
        pass

    if not alerts:
        return None
    return "## 🚨 告警\n" + "\n".join(alerts)


def _build_frozen_section() -> str | None:
    """内核冻结状态（仅状态变更时推送）"""
    try:
        from kernel_freeze import all_frozen_ids, FROZEN_MANIFEST
        total = len(FROZEN_MANIFEST)
        frozen = len(all_frozen_ids())
        if frozen == total:
            return None  # 全部冻结是默认状态，不推送
        return f"  🔓 内核冻结: {frozen}/{total} (有模块已解冻)"
    except Exception:
        return None


def build_daily_brief() -> str | None:
    """构建每日简报。返回 None 表示今日无需推送。"""
    sections = []

    nav = _build_nav_section()
    if nav:
        sections.append(nav)

    signals = _build_signal_section()
    if signals:
        sections.append(signals)

    execution = _build_execution_section()
    if execution:
        sections.append(execution)

    alerts = _build_alert_section()
    if alerts:
        sections.append(alerts)

    frozen = _build_frozen_section()
    if frozen:
        sections.append(frozen)

    if not sections:
        return None

    today = date.today().isoformat()
    header = f"# 📊 Serenity {today}\n"
    return header + "\n\n".join(sections)


def push_daily_brief(dry_run: bool = False, force: bool = False) -> bool:
    """推送每日简报。"""
    if not _is_trading_day():
        log.info("非交易日，跳过推送")
        return False

    if not force and _already_pushed_today():
        log.info("今日已推送，跳过重复")
        return False

    brief = build_daily_brief()

    if not brief and not force:
        log.info("无可执行内容，静默跳过")
        return False

    if not brief:
        brief = f"# 📊 Serenity {date.today().isoformat()}\n  今日无信号/调仓/告警"

    if dry_run:
        print(brief)
        print("\n--- [DRY RUN] 未实际推送 ---")
        return True

    try:
        from notifier import send_message
        send_message(
            f"📊 Serenity {date.today().isoformat()}",
            brief,
            content_type="markdown",
        )
        _mark_pushed()
        log.info("每日简报推送成功")
        return True
    except Exception as e:
        log.error(f"推送失败: {e}")
        return False


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    force = "--force" in sys.argv
    ok = push_daily_brief(dry_run=dry, force=force)
    sys.exit(0 if ok else 1)
