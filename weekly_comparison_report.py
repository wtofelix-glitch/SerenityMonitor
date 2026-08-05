"""
周度三系统对比报告 — weekly_comparison_report.py

每周自动生成 Frozen Baseline / Adaptive / Equal Weight 三系统对比报告。
接入 crontab: 每周六 09:00 运行。

v4 §6.2-6.3: 判定规则需提前写死，不可事后修改。

Usage:
    python3 weekly_comparison_report.py              # 终端输出
    python3 weekly_comparison_report.py --push       # 终端 + 推送
    python3 weekly_comparison_report.py --save       # 仅保存到 reports/
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta
from typing import Optional

from db import get_conn
from config import ALL_CODES, STOCK_MAP, CAPITAL_CONFIG
from serenity_logger import get_logger

log = get_logger(__name__)

REPORT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
os.makedirs(REPORT_DIR, exist_ok=True)

FROZEN_WINDOW_WEEKS = 8
FROZEN_EQ_WINDOW_WEEKS = 12
MIN_WEEKS_NON_BULL = 2


def _get_week_start() -> date:
    """获取本周一的日期。"""
    today = date.today()
    return today - timedelta(days=today.weekday())


def _load_signal_stats(system: str, start: str, end: str) -> dict:
    """加载指定系统在某时间段内的信号统计。"""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT action, outcome_5d FROM signal_log "
            "WHERE date >= ? AND date <= ?",
            (start, end)
        ).fetchall()
        settled = [r for r in rows if r["outcome_5d"] is not None]
        total = len(rows)
        wins = sum(1 for r in settled if float(r["outcome_5d"]) > 0)
        by_action = {}
        for row in rows:
            action = row["action"]
            bucket = by_action.setdefault(action, {"n": 0, "settled": 0, "sum": 0.0})
            bucket["n"] += 1
            if row["outcome_5d"] is not None:
                bucket["settled"] += 1
                bucket["sum"] += float(row["outcome_5d"])
        return {
            "total_signals": total,
            "settled_signals": len(settled),
            "by_action": {
                action: {
                    "n": values["n"],
                    "avg_ret_5d": round(values["sum"] / max(values["settled"], 1), 4),
                }
                for action, values in by_action.items()
            },
            "win_rate": round(wins / max(len(settled), 1), 4),
        }
    finally:
        conn.close()


def _load_nav_weekly_return() -> Optional[float]:
    """只用不可变券商快照计算周收益；不足两点则不出数。"""
    conn = get_conn()
    try:
        ws = _get_week_start()
        rows = conn.execute(
            "SELECT snapshot_at, total_assets FROM portfolio_reconciliations "
            "WHERE date(snapshot_at) >= ? AND date(snapshot_at) <= ? ORDER BY snapshot_at",
            (ws.isoformat(), date.today().isoformat()),
        ).fetchall()
        if len(rows) >= 2 and float(rows[0]["total_assets"] or 0) > 0:
            return float(rows[-1]["total_assets"]) / float(rows[0]["total_assets"]) - 1.0
    finally:
        conn.close()
    return None


def _load_weekly_returns_since(start_date: str) -> list[float]:
    """加载从指定日期起每周的 NAV 收益率。"""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT date, profit_pct FROM nav_history WHERE date >= ? ORDER BY date",
            (start_date,)
        ).fetchall()
        if not rows:
            return []
        # 按周聚合
        weekly = {}
        for r in rows:
            d = date.fromisoformat(r["date"])
            wk = d.isocalendar()[1]
            yr = d.isocalendar()[0]
            key = f"{yr}-W{wk:02d}"
            weekly[key] = weekly.get(key, 0.0) + (r["profit_pct"] or 0)
        return list(weekly.values())
    finally:
        conn.close()


def generate_weekly_report() -> str:
    """生成周度三系统对比报告。"""
    today = date.today()
    ws = _get_week_start()
    week_label = f"{ws.isoformat()} ~ {today.isoformat()}"

    # 系统 A: Adaptive (当前系统 — 从 nav_history)
    adaptive_raw = _load_nav_weekly_return()
    adaptive_return = round(adaptive_raw * 100, 2) if adaptive_raw is not None else None
    adaptive_signals = _load_signal_stats("adaptive", ws.isoformat(), today.isoformat())

    # 系统 B: Frozen Baseline
    try:
        from frozen_baseline import BaselineComparator
        frozen_signals_count = len(BaselineComparator().get_signals_today())
    except Exception:
        frozen_signals_count = 0

    # 系统 C: Equal Weight Basket
    try:
        from equal_weight_basket import get_basket
        eq = get_basket()
        eq_return = round(eq.get_weekly_return() * 100, 2)
        eq_nav = eq.get_nav()
        eq_monthly = round(eq.get_monthly_return() * 100, 2)
        eq_snapshot = eq.snapshot()
        if eq_snapshot.get("data_points", 0) < 2:
            eq_return = None
    except Exception:
        eq_return = 0.0
        eq_nav = 0.0
        eq_monthly = 0.0

    # 审计日志统计
    try:
        from audit_logger import get_audit_logger
        al = get_audit_logger()
        audit_stats = al.get_stats()
    except Exception:
        audit_stats = {"total": 0, "executed": 0, "blocked": 0, "overridden": 0,
                       "settled": 0, "execution_rate": 0, "override_rate": 0}

    # 内核冻结状态
    try:
        from kernel_freeze import all_frozen_ids
        frozen_modules = all_frozen_ids()
    except Exception:
        frozen_modules = []

    # 观察模式
    try:
        from observation_mode import get_observer
        obs = get_observer()
        obs_status = obs.get_status()
    except Exception:
        obs_status = {"mode": "NORMAL", "active": False}

    # 判定规则评估
    verdict = _evaluate_verdict(adaptive_return, eq_return)
    adaptive_text = "—" if adaptive_return is None else f"{adaptive_return:+.2f}%"
    eq_text = "—" if eq_return is None else f"{eq_return:+.2f}%"

    lines = [
        "# SerenityMonitor 三系统并跑周报",
        f"  报告周期: {week_label}",
        f"  生成时间: {datetime.now().isoformat()}",
        "─" * 48,
        "",
        "## 系统状态",
        f"  交易内核: {len(frozen_modules)}/9 模块冻结",
        f"  观察模式: {'🔴 ' + obs_status['mode'] if obs_status.get('active') else '🟢 NORMAL'}",
        "",
        "## 三系统本周对比",
        "| 系统 | 周收益 | 信号数 | 胜率(5d) | 备注 |",
        "|------|--------|--------|---------|------|",
        f"| A: Adaptive | {adaptive_text} | {adaptive_signals['total_signals']} | {adaptive_signals['win_rate']:.0%} ({adaptive_signals['settled_signals']}) | 券商快照口径 |",
        f"| B: Frozen | — | ~{frozen_signals_count} | — | 固定规则, 无自适应 |",
        f"| C: Equal Weight | {eq_text} | — | — | 真实逐日账本, NAV ¥{eq_nav:,.0f} |",
        "",
        "## 判定规则",
        f"  Adaptive {adaptive_text} vs Frozen — (Frozen 收益数据待分数历史积累)",
        f"  Frozen vs Equal Weight {eq_text} (Frozen 分数历史积累中)",
        f"  规则: 连续 {FROZEN_WINDOW_WEEKS} 周 Adaptive < Frozen → 冻结",
        f"  规则: 连续 {FROZEN_EQ_WINDOW_WEEKS} 周 Frozen < Equal Weight → 暂停评分",
        f"  要求: 窗口内覆盖 ≥{MIN_WEEKS_NON_BULL} 周非单边上涨",
        "",
        "## 决策审计",
        f"  审计记录: {audit_stats['total']} 条",
        f"  已执行: {audit_stats['executed']} | 被阻止: {audit_stats['blocked']}",
        f"  人工干预: {audit_stats['overridden']} ({audit_stats['override_rate']:.0%})",
        f"  已结算: {audit_stats['settled']}",
        "",
        "## 本周判定",
        verdict,
        "",
        "## 观察模式",
        f"  状态: {obs_status['mode']}",
    ]

    if obs_status.get("active"):
        lines.append(f"  触发原因: {obs_status.get('trigger_reason', 'N/A')}")
        lines.append(f"  进入时间: {obs_status.get('entered_at', 'N/A')}")
        lines.append(f"  已持续: {obs_status.get('days_active', 0)} 天")

    lines.extend([
        "",
        "─" * 48,
        f"  下次报告: {(today + timedelta(days=7)).isoformat()}",
    ])

    return "\n".join(lines)


def _evaluate_verdict(adaptive_return: Optional[float], eq_return: Optional[float]) -> str:
    """评估本周判定规则（当前为初始状态，Frozen 数据积累中）。"""
    if adaptive_return is None or eq_return is None:
        return "⏳ 真实账本不足两个时点，本周不做收益优劣判定"
    if adaptive_return < 0 and eq_return > 0:
        return "⚠️ Adaptive 亏损 + Equal Weight 盈利 — 关注后续趋势"
    if adaptive_return > 0 and eq_return > 0:
        return "✅ 双系统盈利 — 正常观察期"
    if adaptive_return < 0 and eq_return < 0:
        return "🔶 市场整体下跌 — 区分 Alpha vs Beta 较困难, 继续观察"
    if adaptive_return > eq_return:
        return "✅ Adaptive 跑赢 Equal Weight — 初步正面"
    return "🔶 待 Frozen Baseline 数据积累后做统计判定"


def save_report(report: str) -> str:
    """保存报告到文件。"""
    path = os.path.join(REPORT_DIR, f"weekly_comparison_{date.today().isoformat()}.md")
    with open(path, "w") as f:
        f.write(report)
    return path


def push_report(report: str) -> bool:
    """推送报告。"""
    try:
        from notifier import send_message
        title = f"SerenityMonitor 周报 {date.today().isoformat()}"
        send_message(title, report[:4000], content_type=3)  # Markdown
        return True
    except Exception as e:
        log.warning(f"推送失败: {e}")
        return False


if __name__ == "__main__":
    from datetime import datetime

    report = generate_weekly_report()
    print(report)

    if "--save" in sys.argv or "--push" in sys.argv:
        path = save_report(report)
        print(f"\n📁 报告已保存: {path}")

    if "--push" in sys.argv:
        ok = push_report(report)
        print(f"📡 推送: {'成功' if ok else '失败'}")
