"""
观察模式状态机 — observation_mode.py

实现 v4 §13.3 的停机条件。当触发条件满足时自动进入观察模式
或紧急停机，保护组合免受持续性损失。

观察模式 (OBSERVATION): 只记录信号，不新增仓位，只允许减仓
紧急停机 (EMERGENCY): 清空所有 T1-T3 仓位，仅保留 T4 底仓

状态持久化到 .observation_state.json，跨进程/重启保持。

Usage:
    from observation_mode import ObservationMode, get_observer

    obs = get_observer()
    status = obs.check()

    if status["mode"] != "NORMAL":
        print(f"⚠️ 当前处于 {status['mode']} 模式: {status['trigger_reason']}")
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from typing import Optional
from dataclasses import dataclass, field, asdict

from db import get_conn
from config import ALL_CODES, STOCK_MAP, TIER_1_CODES, TIER_2_CODES, TIER_3_CODES, TIER_4_CODES
from serenity_logger import get_logger

log = get_logger(__name__)

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".observation_state.json")

# 观察模式触发条件 (v4 §13.3)
OBSERVATION_TRIGGERS = {
    "consecutive_underperform": {
        "condition": "连续 4 周 Adaptive 净收益跑输 Equal Weight Basket",
        "weeks": 4,
        "metric": "adaptive_vs_eq",
    },
    "max_drawdown": {
        "condition": "最大回撤超过 -15%",
        "threshold": -0.15,
        "metric": "max_drawdown",
    },
    "correlated_stops": {
        "condition": "连续 3 次止损触发后，同主题标的的相关性仍 > 0.7",
        "consecutive_stops": 3,
        "corr_threshold": 0.7,
    },
    "data_quality": {
        "condition": "数据源质量异常（价格缺失/时点异常）连续出现 ≥2 次",
        "consecutive_anomalies": 2,
    },
    "audit_log_gap": {
        "condition": "decision_audit_log 记录缺失",
        "metric": "audit_log_gap",
    },
    "backtest_drift": {
        "condition": "回测收益与实盘收益偏差持续扩大（连续 4 周偏差 > 2%/周）",
        "weeks": 4,
        "weekly_drift_pct": 0.02,
    },
    "human_override_rate": {
        "condition": "人工干预比例超过 30%",
        "threshold": 0.30,
        "metric": "human_override_rate",
    },
}

# 紧急停机触发条件 (v4 §13.3)
EMERGENCY_TRIGGERS = {
    "drawdown_20pct": {
        "condition": "组合回撤超过 -20%",
        "threshold": -0.20,
    },
    "data_source_failure": {
        "condition": "核心数据源（Sina API）连续 3 天不可用",
        "consecutive_days": 3,
    },
    "lookahead_leak": {
        "condition": "检测到系统性未来函数泄漏",
        "metric": "lookahead_detected",
    },
    "theme_narrative_reversal": {
        "condition": "AI 供应链主题叙事出现根本性反转",
        "metric": "theme_reversal",
    },
}


@dataclass
class ObservationState:
    """观察模式状态"""
    mode: str = "NORMAL"                # NORMAL / OBSERVATION / EMERGENCY
    active: bool = False
    trigger_reason: str = ""            # 触发原因
    trigger_condition: str = ""         # 触发的具体条件名
    entered_at: str = ""                # ISO datetime
    days_active: int = 0
    positions_frozen: list[str] = field(default_factory=list)  # 被冻结的仓位
    liquidated_t1_t3: bool = False      # 是否已清仓 T1-T3
    weekly_counter: dict = field(default_factory=dict)  # 周度计数器
    events: list[dict] = field(default_factory=list)     # 事件日志


class ObservationMode:
    """观察模式状态机。

    每个交易日自动检查停机条件。状态持久化到 JSON 文件。
    """

    def __init__(self):
        self._state = self._load_state()

    def _load_state(self) -> ObservationState:
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE) as f:
                    data = json.load(f)
                return ObservationState(**data)
            except Exception:
                pass
        return ObservationState()

    def _save_state(self) -> None:
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(asdict(self._state), f, ensure_ascii=False, indent=2)
        except Exception as e:
            log.warning(f"观察模式状态保存失败: {e}")

    # ── 公开接口 ──────────────────────────────────────────

    def get_status(self) -> dict:
        """获取当前观察模式状态。"""
        return {
            "mode": self._state.mode,
            "active": self._state.active,
            "trigger_reason": self._state.trigger_reason,
            "trigger_condition": self._state.trigger_condition,
            "entered_at": self._state.entered_at,
            "days_active": self._state.days_active,
        }

    def check(self, portfolio_return_weekly: float | None = None,
              max_drawdown: float | None = None,
              equal_weight_return_weekly: float | None = None,
              audit_stats: dict | None = None,
              consecutive_stops: int = 0,
              data_anomalies: int = 0) -> dict:
        """运行所有停机条件检查。

        在每个交易日（或每周）调用。如果当前已在观察/紧急模式，
        检查是否可以恢复正常。

        Returns:
            {mode, triggered, trigger_type, reason, action_required}
        """
        # 如果已在紧急模式 → 不允许恢复
        if self._state.mode == "EMERGENCY":
            return {"mode": "EMERGENCY", "triggered": False,
                    "reason": "紧急停机中，需人工确认恢复",
                    "action_required": "HUMAN_REVIEW_REQUIRED"}

        # 检查紧急停机条件
        em = self._check_emergency(max_drawdown)
        if em["triggered"]:
            self._enter_emergency(em["condition"], em["reason"])
            return {"mode": "EMERGENCY", "triggered": True,
                    "trigger_type": "emergency",
                    "reason": em["reason"],
                    "action_required": "LIQUIDATE_T1_T3"}

        # 检查观察模式条件
        ob = self._check_observation(
            portfolio_return_weekly, equal_weight_return_weekly,
            audit_stats, consecutive_stops, data_anomalies, max_drawdown)
        if ob["triggered"]:
            self._enter_observation(ob["condition"], ob["reason"])
            return {"mode": "OBSERVATION", "triggered": True,
                    "trigger_type": "observation",
                    "reason": ob["reason"],
                    "action_required": "BLOCK_NEW_POSITIONS"}

        # 检查是否可以从观察模式恢复
        if self._state.mode == "OBSERVATION" and self._can_recover():
            self._recover()

        # 更新天数
        if self._state.active:
            self._state.days_active += 1
            self._save_state()

        return {"mode": self._state.mode, "triggered": False,
                "reason": "", "action_required": "NONE"}

    def is_trading_allowed(self) -> bool:
        """是否允许新开仓。"""
        if self._state.mode == "EMERGENCY":
            return False
        if self._state.mode == "OBSERVATION":
            return False  # 只允许减仓
        return True

    def is_sell_allowed(self) -> bool:
        """是否允许卖出（在任何模式下都允许）。"""
        return True

    def force_normal(self) -> None:
        """强制恢复 NORMAL 模式（人工确认后调用）。"""
        self._state.mode = "NORMAL"
        self._state.active = False
        self._state.trigger_reason = ""
        self._state.trigger_condition = ""
        self._state.entered_at = ""
        self._state.days_active = 0
        self._state.weekly_counter = {}
        self._save_state()
        log.warning("⚠️ 观察模式已人工恢复为 NORMAL")

    # ── 内部逻辑 ──────────────────────────────────────────

    def _check_emergency(self, max_drawdown: float | None) -> dict:
        """检查紧急停机条件。"""
        # 条件 1: 组合回撤超过 -20%
        if max_drawdown is not None and max_drawdown <= EMERGENCY_TRIGGERS["drawdown_20pct"]["threshold"]:
            return {"triggered": True, "condition": "drawdown_20pct",
                    "reason": f"组合回撤 {max_drawdown:.1%} 超过 -20% 紧急停机线"}

        # 条件 4: 主题叙事反转 (人工标记)
        if self._state.weekly_counter.get("theme_reversal_flagged"):
            return {"triggered": True, "condition": "theme_narrative_reversal",
                    "reason": "AI 供应链主题叙事出现根本性反转（人工标记）"}

        return {"triggered": False, "condition": "", "reason": ""}

    def _check_observation(self, weekly_ret: float | None,
                           eq_weekly_ret: float | None,
                           audit_stats: dict | None,
                           consecutive_stops: int,
                           data_anomalies: int,
                           max_drawdown: float | None) -> dict:
        """检查观察模式条件。"""
        today = date.today().isoformat()

        # 条件 1: 连续 4 周跑输 Equal Weight
        if weekly_ret is not None and eq_weekly_ret is not None:
            underperform = weekly_ret < eq_weekly_ret
            key = "adaptive_vs_eq_underperform"
            if underperform:
                self._state.weekly_counter[key] = self._state.weekly_counter.get(key, 0) + 1
            else:
                self._state.weekly_counter[key] = 0

            if self._state.weekly_counter.get(key, 0) >= OBSERVATION_TRIGGERS["consecutive_underperform"]["weeks"]:
                return {"triggered": True, "condition": "consecutive_underperform",
                        "reason": f"连续 {self._state.weekly_counter[key]} 周 Adaptive 跑输 Equal Weight"}

        # 条件 2: 最大回撤超过 -15%
        if max_drawdown is not None and max_drawdown <= OBSERVATION_TRIGGERS["max_drawdown"]["threshold"]:
            return {"triggered": True, "condition": "max_drawdown",
                    "reason": f"最大回撤 {max_drawdown:.1%} 超过 -15% 观察线"}

        # 条件 3: 连续止损 + 高相关性
        if consecutive_stops >= OBSERVATION_TRIGGERS["correlated_stops"]["consecutive_stops"]:
            try:
                from correlation_cluster import get_cluster
                cc = get_cluster()
                clusters = cc.identify_clusters()
                # 检查是否有大簇（≥2 只）
                for cid, members in clusters.items():
                    if len(members) >= 2:
                        corrs = []
                        for i, a in enumerate(members):
                            for b in members[i+1:]:
                                r = cc._corr_matrix.get(a, {}).get(b, 0)
                                if r != 0:
                                    corrs.append(abs(r))
                        avg_corr = sum(corrs) / len(corrs) if corrs else 0
                        if avg_corr > OBSERVATION_TRIGGERS["correlated_stops"]["corr_threshold"]:
                            return {"triggered": True, "condition": "correlated_stops",
                                    "reason": f"连续 {consecutive_stops} 次止损 + 簇 {cid} 平均相关度 {avg_corr:.2f} > 0.7"}
            except Exception:
                pass

        # 条件 6: 回测 vs 实盘偏差
        drift_count = self._state.weekly_counter.get("backtest_drift_weeks", 0)
        if drift_count >= OBSERVATION_TRIGGERS["backtest_drift"]["weeks"]:
            return {"triggered": True, "condition": "backtest_drift",
                    "reason": f"连续 {drift_count} 周回测 vs 实盘偏差 > 2%/周"}

        # 条件 7: 人工干预比例 > 30%
        if audit_stats and audit_stats.get("override_rate", 0) > OBSERVATION_TRIGGERS["human_override_rate"]["threshold"]:
            return {"triggered": True, "condition": "human_override_rate",
                    "reason": f"人工干预比例 {audit_stats['override_rate']:.0%} > 30%, 信号不被信任"}

        return {"triggered": False, "condition": "", "reason": ""}

    def _enter_observation(self, condition: str, reason: str) -> None:
        """进入观察模式。"""
        if self._state.mode == "OBSERVATION":
            self._state.days_active += 1
            self._save_state()
            return

        self._state.mode = "OBSERVATION"
        self._state.active = True
        self._state.trigger_condition = condition
        self._state.trigger_reason = reason
        self._state.entered_at = datetime.now().isoformat()
        self._state.days_active = 1
        self._state.events.append({
            "type": "ENTER_OBSERVATION",
            "condition": condition,
            "reason": reason,
            "timestamp": datetime.now().isoformat(),
        })
        self._save_state()
        log.warning(f"🔶 进入观察模式: {reason}")

    def _enter_emergency(self, condition: str, reason: str) -> None:
        """进入紧急停机。"""
        self._state.mode = "EMERGENCY"
        self._state.active = True
        self._state.trigger_condition = condition
        self._state.trigger_reason = reason
        self._state.entered_at = datetime.now().isoformat()
        self._state.days_active = 1
        self._state.events.append({
            "type": "ENTER_EMERGENCY",
            "condition": condition,
            "reason": reason,
            "timestamp": datetime.now().isoformat(),
        })
        self._save_state()
        log.error(f"🔴 紧急停机: {reason}")

    def _can_recover(self) -> bool:
        """判断是否可以从观察模式恢复。简单规则: 连续 2 周无新触发。"""
        count = self._state.weekly_counter.get("clean_weeks_since_last_trigger", 0)
        return count >= 2

    def _recover(self) -> None:
        """从观察模式恢复至正常。"""
        self._state.mode = "NORMAL"
        self._state.active = False
        self._state.events.append({
            "type": "RECOVER_TO_NORMAL",
            "timestamp": datetime.now().isoformat(),
        })
        self._save_state()
        log.info("🟢 观察模式已结束，恢复 NORMAL")

    def mark_theme_reversal(self, reason: str = "") -> None:
        """人工标记 AI 供应链主题叙事反转（触发紧急停机条件之一）。"""
        self._state.weekly_counter["theme_reversal_flagged"] = 1
        self._state.events.append({
            "type": "THEME_REVERSAL_FLAGGED",
            "reason": reason,
            "timestamp": datetime.now().isoformat(),
        })
        self._save_state()
        log.warning(f"⚠️ 主题叙事反转已标记: {reason}")

    def record_backtest_drift(self, drift_pct: float) -> None:
        """记录本周的回测 vs 实盘偏差。"""
        if abs(drift_pct) > OBSERVATION_TRIGGERS["backtest_drift"]["weekly_drift_pct"]:
            self._state.weekly_counter["backtest_drift_weeks"] = \
                self._state.weekly_counter.get("backtest_drift_weeks", 0) + 1
        else:
            self._state.weekly_counter["backtest_drift_weeks"] = 0
        self._save_state()


# 模块级单例
_observer: Optional[ObservationMode] = None


def get_observer() -> ObservationMode:
    global _observer
    if _observer is None:
        _observer = ObservationMode()
    return _observer
