"""
Kill Switch — kill_switch.py

Phase 5b: 统一熔断/安全开关。从散落在 risk_manager / auto_execute /
auto_gate 中的熔断逻辑整合为单一可信开关模块。

触发条件（持久化，跨进程）：
  - CIRCUIT: 组合回撤 > 12% → 强制清仓 T1-T3
  - DAILY_LOSS: 单日亏损 > 2% → 当日不允许新开仓
  - DRAW DOWN: 组合回撤 > 8% → 降低仓位上限
  - MANUAL: 人工触发

Usage:
    from kill_switch import get_kill_switch

    ks = get_kill_switch()
    if ks.is_triggered():
        print("熔断已触发，禁止任何新开仓")
    if ks.is_daily_loss_locked():
        print("日亏损锁，当日不新增仓位")
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from typing import Optional

from serenity_logger import get_logger

log = get_logger(__name__)

STATE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".kill_switch_state.json"
)

# ── 阈值 ──
CIRCUIT_BREAKER_DD = 0.12       # 组合回撤 > 12% → 熔断
DAILY_LOSS_LOCK_PCT = -0.02     # 单日亏损 > 2% → 日亏损锁
DRAWDOWN_WARNING_PCT = -0.08    # 组合回撤 > 8% → 降低仓位上限


@dataclass
class KillSwitchState:
    triggered: bool = False
    trigger_type: str = ""            # CIRCUIT / DAILY_LOSS / DRAWDOWN / MANUAL
    trigger_reason: str = ""
    triggered_at: str = ""
    daily_loss_locked: bool = False
    daily_loss_pct: float = 0.0
    drawdown_warning: bool = False
    position_cap_multiplier: float = 1.0  # 1.0 = 正常, 0.5 = 减半
    events: list[dict] = field(default_factory=list)
    updated_at: str = ""


class KillSwitch:
    """统一熔断器。

    每次检查组合状态时调用。当一个周期内触发条件消失后自动复位。
    """

    def __init__(self):
        self._state = self._load_state()

    # ── 公开接口 ──────────────────────────────────────────

    def is_triggered(self) -> bool:
        """是否处于熔断状态（禁止新开仓）。"""
        return self._state.triggered

    def is_daily_loss_locked(self) -> bool:
        """是否处于日亏损锁（当日不新增仓位）。"""
        return self._state.daily_loss_locked

    def get_status(self) -> dict:
        """获取当前熔断状态摘要。"""
        return {
            "triggered": self._state.triggered,
            "trigger_type": self._state.trigger_type,
            "trigger_reason": self._state.trigger_reason,
            "triggered_at": self._state.triggered_at,
            "daily_loss_locked": self._state.daily_loss_locked,
            "daily_loss_pct": round(self._state.daily_loss_pct, 4),
            "drawdown_warning": self._state.drawdown_warning,
            "position_cap_multiplier": self._state.position_cap_multiplier,
            "event_count": len(self._state.events),
            "updated_at": self._state.updated_at,
        }

    def check(self, current_value: float, initial_capital: float,
              daily_profit_pct: float = 0.0,
              today_start_value: Optional[float] = None) -> dict:
        """检查是否需要触发熔断。

        Args:
            current_value: 当前组合总净值
            initial_capital: 启动资金
            daily_profit_pct: 当日盈亏百分比（相对于开盘净值）
            today_start_value: 当日开盘净值（用于计算单日亏损）

        Returns:
            {triggered, trigger_type, reason, action_required}
        """
        updated = False

        # ── 日亏损锁检查 ──
        if daily_profit_pct is not None and daily_profit_pct <= DAILY_LOSS_LOCK_PCT:
            if not self._state.daily_loss_locked:
                self._state.daily_loss_locked = True
                self._state.daily_loss_pct = daily_profit_pct
                updated = True
                log.warning(f"🔒 日亏损锁触发: {daily_profit_pct:.2%}")
        elif daily_profit_pct is not None and daily_profit_pct > 0:
            # 当日转正 → 复位日亏损锁
            if self._state.daily_loss_locked:
                self._state.daily_loss_locked = False
                self._state.daily_loss_pct = 0.0
                updated = True

        # ── 组合回撤检查 (相对初始资金) ──
        drawdown = (current_value - initial_capital) / initial_capital if initial_capital > 0 else 0

        if drawdown <= CIRCUIT_BREAKER_DD:
            if not self._state.triggered or self._state.trigger_type != "CIRCUIT":
                self._state.triggered = True
                self._state.trigger_type = "CIRCUIT"
                self._state.trigger_reason = (
                    f"组合回撤 {drawdown:.1%} > {CIRCUIT_BREAKER_DD:.0%} 熔断线"
                )
                self._state.triggered_at = datetime.now().isoformat()
                self._state.events.append({
                    "type": "CIRCUIT_BREAKER_TRIGGERED",
                    "drawdown": round(drawdown, 4),
                    "current_value": round(current_value, 2),
                    "timestamp": datetime.now().isoformat(),
                })
                updated = True
                log.error(f"🔴 熔断触发: {self._state.trigger_reason}")
        elif drawdown <= DRAWDOWN_WARNING_PCT:
            self._state.drawdown_warning = True
            self._state.position_cap_multiplier = 0.5
            updated = True
        else:
            # 回撤恢复 → 自动复位
            if self._state.triggered and self._state.trigger_type == "CIRCUIT":
                self._state.triggered = False
                self._state.trigger_type = ""
                self._state.trigger_reason = ""
                self._state.events.append({
                    "type": "CIRCUIT_BREAKER_RESET",
                    "drawdown": round(drawdown, 4),
                    "timestamp": datetime.now().isoformat(),
                })
                updated = True
            if self._state.drawdown_warning:
                self._state.drawdown_warning = False
                self._state.position_cap_multiplier = 1.0
                updated = True

        if updated:
            self._state.updated_at = datetime.now().isoformat()
            self._save_state()

        return {
            "triggered": self._state.triggered,
            "trigger_type": self._state.trigger_type,
            "reason": self._state.trigger_reason,
            "action_required": (
                "LIQUIDATE_T1_T3" if self._state.trigger_type == "CIRCUIT"
                else "REDUCE_POSITIONS" if self._state.drawdown_warning
                else "BLOCK_BUYS" if self._state.daily_loss_locked
                else "NONE"
            ),
        }

    def manual_trigger(self, reason: str) -> None:
        """人工触发熔断。"""
        self._state.triggered = True
        self._state.trigger_type = "MANUAL"
        self._state.trigger_reason = reason
        self._state.triggered_at = datetime.now().isoformat()
        self._state.events.append({
            "type": "MANUAL_KILL_SWITCH",
            "reason": reason,
            "timestamp": datetime.now().isoformat(),
        })
        self._state.updated_at = datetime.now().isoformat()
        self._save_state()
        log.warning(f"🔴 人工熔断: {reason}")

    def manual_reset(self, reason: str = "") -> None:
        """人工复位熔断。仅允许在确认问题已解决后调用。"""
        self._state.triggered = False
        self._state.trigger_type = ""
        self._state.trigger_reason = ""
        self._state.daily_loss_locked = False
        self._state.drawdown_warning = False
        self._state.position_cap_multiplier = 1.0
        self._state.events.append({
            "type": "MANUAL_RESET",
            "reason": reason,
            "timestamp": datetime.now().isoformat(),
        })
        self._state.updated_at = datetime.now().isoformat()
        self._save_state()
        log.warning(f"🟢 熔断人工复位: {reason}")

    def get_position_cap_multiplier(self) -> float:
        """获取当前仓位上限乘数。熔断/风控期间自动降低。"""
        return self._state.position_cap_multiplier

    # ── 内部 ──────────────────────────────────────────────

    def _load_state(self) -> KillSwitchState:
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE) as f:
                    data = json.load(f)
                return KillSwitchState(**data)
            except Exception:
                pass
        return KillSwitchState()

    def _save_state(self) -> None:
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(asdict(self._state), f, ensure_ascii=False, indent=2)
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════
# 模块级单例
# ═══════════════════════════════════════════════════════════════

_kill_switch: Optional[KillSwitch] = None


def get_kill_switch() -> KillSwitch:
    global _kill_switch
    if _kill_switch is None:
        _kill_switch = KillSwitch()
    return _kill_switch
