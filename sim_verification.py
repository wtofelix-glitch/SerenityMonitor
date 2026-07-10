"""
模拟盘验证框架 — sim_verification.py

Phase 5b 前置步骤：在自动交易接口启用前，必须通过 ≥4 周模拟盘验证。

验证要求（v4 §14 Phase 5b）：
  1. 模拟盘运行 ≥4 周
  2. 覆盖 ≥1 次 3%+ 大盘波动
  3. 信号质量与回测一致（偏差 < 可接受范围）
  4. 滑点/成交率在可接受范围内

Usage:
    python3 sim_verification.py --check     # 检查验证状态
    python3 sim_verification.py --report    # 生成验证报告
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from typing import Optional

from db import get_conn
from serenity_logger import get_logger

log = get_logger(__name__)

STATE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".sim_verification_state.json"
)

# ── 验证阈值 ──
MIN_SIM_WEEKS = 4              # 最少模拟周数
MIN_MARKET_EVENTS = 1           # 最少 ≥3% 大盘波动次数
MIN_SIGNALS = 20                # 最少信号样本数
MAX_SLIPPAGE_PCT = 0.5          # 最大可接受滑点 (%)
MAX_BACKTEST_DRIFT_PCT = 2.0    # 每周最大回测 vs 实盘偏差 (%)


class SimVerification:
    """模拟盘验证状态追踪器。"""

    def __init__(self):
        self._state = self._load_state()
        self._ensure_state()

    def _ensure_state(self):
        """确保首次启动时初始化。"""
        if not self._state.get("started_at"):
            self._state["started_at"] = ""
            self._state["end_date"] = ""
            self._state["completed"] = False
            self._state["verdict"] = ""
            self._state["weeks"] = {}
            self._save()

    # ── 公开接口 ──────────────────────────────────────────

    def start(self) -> dict:
        """开始模拟盘验证期。记录起始时间。"""
        if self._state["started_at"]:
            return {"success": False, "reason": "验证期已开始，不可重复启动"}

        today = date.today().isoformat()
        self._state["started_at"] = today
        self._state["end_date"] = (date.today() + timedelta(weeks=MIN_SIM_WEEKS)).isoformat()
        self._save()

        log.warning(f"📝 模拟盘验证期开始: {today} → {self._state['end_date']}")
        return {"success": True, "started_at": today,
                "min_end": self._state["end_date"],
                "min_weeks": MIN_SIM_WEEKS}

    def record_week(self, week_key: str, metrics: dict) -> None:
        """记录当周的验证指标。

        Args:
            week_key: "2026-W28" 格式
            metrics: {signals, buys, sells, avg_slippage, win_rate,
                      sim_sharpe, backtest_sharpe, drift_pct,
                      market_events, notes}
        """
        self._state["weeks"][week_key] = {
            "recorded_at": datetime.now().isoformat(),
            **metrics,
        }
        self._save()

    def check(self) -> dict:
        """检查模拟盘验证是否完成并通过。

        Returns:
            {ready, checklist: [{item, status, detail}], overall}
        """
        today = date.today().isoformat()

        # ── 1. 运行时长 ──
        started = self._state.get("started_at", "")
        if not started:
            return {"ready": False, "overall": "NOT_STARTED",
                    "checklist": [], "reason": "模拟盘验证尚未开始"}

        weeks_elapsed = max(0, (today - date.fromisoformat(started)).days / 7)
        weeks_ok = weeks_elapsed >= MIN_SIM_WEEKS

        # ── 2. 市场事件覆盖 ──
        try:
            from observation_mode import get_market_event_history
            events = get_market_event_history()
            events_in_period = [
                e for e in events
                if e.get("date", "") >= started
            ]
        except Exception:
            events_in_period = []
        events_ok = len(events_in_period) >= MIN_MARKET_EVENTS

        # ── 3. 信号样本量 ──
        weeks_data = self._state.get("weeks", {})
        total_signals = sum(
            w.get("signals", 0) + w.get("buys", 0) + w.get("sells", 0)
            for w in weeks_data.values()
        )
        signals_ok = total_signals >= MIN_SIGNALS

        # ── 4. 滑点检查 ──
        avg_slippages = [
            w.get("avg_slippage", 0)
            for w in weeks_data.values()
            if w.get("avg_slippage") is not None
        ]
        slippage_ok = True
        slippage_detail = "无滑点数据"
        if avg_slippages:
            avg = sum(avg_slippages) / len(avg_slippages)
            slippage_ok = avg <= MAX_SLIPPAGE_PCT
            slippage_detail = f"平均滑点 {avg:.2f}% (上限 {MAX_SLIPPAGE_PCT}%)"

        # ── 5. 回测偏差 ──
        drifts = [
            abs(w.get("drift_pct", 0))
            for w in weeks_data.values()
            if w.get("drift_pct") is not None
        ]
        drift_ok = True
        drift_detail = "无偏差数据"
        if drifts:
            max_drift = max(drifts)
            drift_ok = max_drift <= MAX_BACKTEST_DRIFT_PCT
            drift_detail = f"最大周偏差 {max_drift:.2f}% (上限 {MAX_BACKTEST_DRIFT_PCT}%)"

        checklist = [
            {"item": "运行 ≥4 周", "status": "PASS" if weeks_ok else "PENDING",
             "detail": f"{weeks_elapsed:.1f}/{MIN_SIM_WEEKS} 周"},
            {"item": "≥1 次 ≥3% 大盘波动", "status": "PASS" if events_ok else "PENDING",
             "detail": f"{len(events_in_period)}/{MIN_MARKET_EVENTS} 次"},
            {"item": "≥20 条信号样本", "status": "PASS" if signals_ok else "PENDING",
             "detail": f"{total_signals}/{MIN_SIGNALS} 条"},
            {"item": "滑点在可接受范围", "status": "PASS" if slippage_ok else "FAIL",
             "detail": slippage_detail},
            {"item": "回测偏差在可接受范围", "status": "PASS" if drift_ok else "FAIL",
             "detail": drift_detail},
        ]

        all_pass = all(c["status"] == "PASS" for c in checklist)

        return {
            "ready": all_pass,
            "overall": "PASS" if all_pass else "IN_PROGRESS",
            "checklist": checklist,
            "weeks_elapsed": round(weeks_elapsed, 1),
            "signals_total": total_signals,
            "market_events": len(events_in_period),
            "started_at": started,
            "min_end_date": self._state.get("end_date", ""),
        }

    def generate_report(self) -> str:
        """生成模拟盘验证报告。"""
        status = self.check()
        lines = [
            "# 模拟盘验证报告",
            f"  日期: {date.today().isoformat()}",
            f"  状态: {status['overall']}",
            f"  验证期: {status.get('started_at', 'N/A')} → 至少 {status.get('min_end_date', 'N/A')}",
            f"  已运行: {status['weeks_elapsed']} 周",
            "─" * 48,
            "",
            "## 验证清单",
        ]

        for c in status["checklist"]:
            icon = "✅" if c["status"] == "PASS" else "⏳" if c["status"] == "PENDING" else "❌"
            lines.append(f"  {icon} {c['item']}: {c['detail']}")

        lines.extend([
            "",
            "## 周度明细",
        ])

        for wk in sorted(self._state.get("weeks", {}).keys()):
            w = self._state["weeks"][wk]
            lines.append(
                f"  {wk}: signals={w.get('signals', 0)}, "
                f"buys={w.get('buys', 0)}, sells={w.get('sells', 0)}, "
                f"slippage={w.get('avg_slippage', 0):.2f}%, "
                f"drift={w.get('drift_pct', 0):.2f}%, "
                f"market_events={w.get('market_events', 0)}"
            )

        lines.extend([
            "",
            "## 前置条件 (Phase 5b 启用自动交易接口前)",
            f"  [{'x' if status['ready'] else ' '}] 模拟盘验证 ≥4 周",
            f"  [{'x' if status['ready'] else ' '}] 覆盖 ≥1 次 3%+ 大盘波动",
            f"  [{'x' if status['ready'] else ' '}] 信号质量与回测一致",
        ])

        return "\n".join(lines)

    # ── 内部 ──────────────────────────────────────────────

    def _load_state(self) -> dict:
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE) as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save(self) -> None:
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(self._state, f, ensure_ascii=False, indent=2)
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════
# 模块级单例
# ═══════════════════════════════════════════════════════════════

_verifier: Optional[SimVerification] = None


def get_verifier() -> SimVerification:
    global _verifier
    if _verifier is None:
        _verifier = SimVerification()
    return _verifier


if __name__ == "__main__":
    import sys
    v = get_verifier()
    if "--check" in sys.argv:
        print(json.dumps(v.check(), ensure_ascii=False, indent=2))
    elif "--start" in sys.argv:
        print(json.dumps(v.start(), ensure_ascii=False, indent=2))
    else:
        print(v.generate_report())
