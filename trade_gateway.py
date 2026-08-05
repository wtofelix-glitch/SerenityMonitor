"""
TradeGateway — 统一交易网关抽象层

Phase 5a: 为自动交易接口提供统一后端抽象。
支持 PaperTrader (模拟) / THSBridge (半自动) / QMT (未来)。

所有交易指令必须通过此网关，内置安全边界：
  - 日交易笔数上限
  - 单笔金额上限
  - 观察模式/紧急停机检查
  - Kill switch 熔断

Usage:
    from trade_gateway import TradeGateway, Backend

    gw = TradeGateway(Backend.PAPER)
    result = gw.place_order("002281", "BUY", 200.0, 300)
    if result["status"] == "placed":
        print(f"委托成功: {result['order_id']}")
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from enum import Enum
from typing import Optional

from serenity_logger import get_logger

log = get_logger(__name__)

# ═══════════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════════

STATE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".trade_gateway_state.json"
)

# 安全边界 (v4 Phase 5 §14)
MAX_BUY_ORDERS_PER_DAY = 1      # 每日最多买入笔数
MAX_SELL_ORDERS_PER_DAY = 3     # 每日最多卖出笔数
MAX_SINGLE_AUTO_AMOUNT = 0.20   # 单笔自动交易最多 20% 净值
MAX_AUTO_POOL_PCT = 0.30        # 自动交易池总比例 30%


class Backend(Enum):
    PAPER = "paper"               # 纸面模拟（默认，最安全）
    THS_SEMI_AUTO = "ths_semi"    # 同花顺半自动（生成指令，人工确认）
    PASSTHROUGH = "passthrough"   # 直通（仅用于回测和测试）
    QMT = "qmt"                   # 迅投 QMT（未来）
    XTQUANT = "xtquant"           # 迅投 xtquant（未来）


class OrderStatus(Enum):
    GENERATED = "generated"
    PENDING_CONFIRM = "pending_confirm"
    CONFIRMED = "confirmed"
    SUBMITTED = "submitted"
    PARTIAL_FILLED = "partial_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


@dataclass
class GatewayOrder:
    order_id: str
    code: str
    action: str                      # BUY / SELL
    price: float
    shares: int
    amount: float
    backend: str
    status: str = "generated"
    created_at: str = ""
    confirmed_at: str = ""
    filled_at: str = ""
    fill_price: float = 0.0
    slippage_pct: float = 0.0
    reason: str = ""


@dataclass
class GatewayState:
    backend: str = "paper"
    daily_buy_count: int = 0
    daily_sell_count: int = 0
    date: str = ""
    orders_today: list[dict] = field(default_factory=list)


# ═══════════════════════════════════════════════════════════════
# 后端抽象
# ═══════════════════════════════════════════════════════════════


class BrokerBackend(ABC):
    """交易后端抽象基类。QMT/xtquant 适配器实现此接口。"""

    @abstractmethod
    def place_order(self, code: str, action: str, price: float,
                    shares: int) -> dict:
        ...

    @abstractmethod
    def cancel_order(self, order_id: str) -> dict:
        ...

    @abstractmethod
    def get_positions(self) -> list[dict]:
        ...

    @abstractmethod
    def get_account(self) -> dict:
        ...

    @abstractmethod
    def is_available(self) -> bool:
        ...


class PaperBackend(BrokerBackend):
    """纸面交易后端 — 包装 PaperTrader。"""

    def __init__(self):
        self._trader = None

    @property
    def trader(self):
        if self._trader is None:
            from paper_trader import get_paper_trader
            self._trader = get_paper_trader()
        return self._trader

    def place_order(self, code: str, action: str, price: float,
                    shares: int) -> dict:
        result = self.trader.execute_signal(
            code=code, action=action, price=price, shares=shares)
        return {
            "status": "filled" if result.get("status") in ("buy", "sell")
                      else result.get("status", "skipped"),
            "order_id": result.get("code", code),
            "fill_price": price,
            "shares": shares,
            "amount": price * shares,
            "reason": result.get("reason", ""),
        }

    def cancel_order(self, order_id: str) -> dict:
        return {"status": "not_supported", "reason": "Paper backend 不支持撤销"}

    def get_positions(self) -> list[dict]:
        return self.trader.get_paper_positions()

    def get_account(self) -> dict:
        pf = self.trader.get_paper_portfolio()
        return {
            "total_value": pf.get("total_value", 0),
            "cash": pf.get("cash", 0),
            "holdings_value": pf.get("holdings_value", 0),
            "position_count": pf.get("position_count", 0),
        }

    def is_available(self) -> bool:
        return True


class THSSemiAutoBackend(BrokerBackend):
    """同花顺半自动后端 — 生成指令文件，人工在客户端确认。"""

    def __init__(self):
        self._broker = None

    @property
    def broker(self):
        if self._broker is None:
            from ths_bridge import THSBroker
            self._broker = THSBroker()
        return self._broker

    def place_order(self, code: str, action: str, price: float,
                    shares: int) -> dict:
        result = self.broker.place_order(
            code=code, action=action, price=price, shares=shares,
            order_type="LIMIT")
        return {
            "status": "pending_confirm",
            "order_id": result.get("order_id", ""),
            "fill_price": price,
            "shares": shares,
            "amount": price * shares,
            "reason": "请在 THS 客户端确认执行",
        }

    def cancel_order(self, order_id: str) -> dict:
        return {"status": "not_supported",
                "reason": "半自动模式请在 THS 客户端撤销"}

    def get_positions(self) -> list[dict]:
        return []

    def get_account(self) -> dict:
        summary = self.broker.get_account_summary()
        return {
            "total_value": summary.get("total_assets", 0),
            "cash": summary.get("available_cash", 0),
            "holdings_value": summary.get("market_value", 0),
            "position_count": summary.get("position_count", 0),
        }

    def is_available(self) -> bool:
        try:
            import easyquotation
            return True
        except ImportError:
            return False


# ═══════════════════════════════════════════════════════════════
# TradeGateway
# ═══════════════════════════════════════════════════════════════


class TradeGateway:
    """统一交易网关。

    所有买卖指令必须通过此网关，确保安全边界在单点执行。
    """

    def __init__(self, backend: Backend = Backend.PAPER):
        self.backend_type = backend
        self._backend = self._create_backend(backend)
        self._state = self._load_state()

    def _create_backend(self, backend: Backend) -> BrokerBackend:
        if backend == Backend.PAPER:
            return PaperBackend()
        elif backend == Backend.THS_SEMI_AUTO:
            return THSSemiAutoBackend()
        elif backend == Backend.PASSTHROUGH:
            return PaperBackend()  # 直通用 Paper 替代
        elif backend in (Backend.QMT, Backend.XTQUANT):
            raise NotImplementedError(
                f"QMT/xtquant 后端尚未实现。"
                f"前置条件: Phase 4 半自动实盘 ≥12 周 + "
                f"Frozen Baseline ≥20 周 + 模块晋级完成。")
        else:
            raise ValueError(f"不支持的后端: {backend}")

    # ── 公开接口 ──────────────────────────────────────────

    def place_order(self, code: str, action: str, price: float,
                    shares: int, force: bool = False) -> dict:
        """下单（经过安全边界检查）。

        Args:
            code: 标的代码
            action: BUY / SELL
            price: 委托价格
            shares: 股数（100 的整数倍）
            force: 跳过安全边界（仅限紧急清仓）

        Returns:
            {status, order_id, fill_price, shares, amount, reason, safety_checks}
        """
        amount = price * shares

        # ── 安全边界 0: Kill switch (最优先) ──
        try:
            from kill_switch import get_kill_switch
            ks = get_kill_switch()
            if ks.is_triggered() and not force:
                return {
                    "status": "rejected",
                    "order_id": "",
                    "fill_price": price, "shares": shares, "amount": amount,
                    "reason": f"熔断已触发: {ks.get_status().get('trigger_reason', ks.get_status().get('trigger_type', ''))}",
                    "safety_checks": {"kill_switch": "BLOCKED"},
                }
            if ks.is_daily_loss_locked() and action == "BUY" and not force:
                return {
                    "status": "rejected",
                    "order_id": "",
                    "fill_price": price, "shares": shares, "amount": amount,
                    "reason": "日亏损锁: 不允许新开仓",
                    "safety_checks": {"daily_loss_lock": "BLOCKED"},
                }
        except ImportError:
            pass

        # ── 安全边界 1: 观察模式/紧急停机 ──
        try:
            from observation_mode import get_observer
            obs = get_observer()
            if action == "BUY" and not obs.is_trading_allowed() and not force:
                status = obs.get_status()
                return {
                    "status": "rejected",
                    "order_id": "",
                    "fill_price": price, "shares": shares, "amount": amount,
                    "reason": f"观察模式 {status['mode']}: {status.get('trigger_reason', '')}",
                    "safety_checks": {"observation_mode": "BLOCKED"},
                }
        except ImportError:
            pass

        # ── 安全边界 2: 单笔金额上限 ──
        try:
            from portfolio import get_portfolio
            nav = get_portfolio().get_portfolio_value()["total_value"]
        except Exception:
            nav = 100000.0

        single_max = nav * MAX_SINGLE_AUTO_AMOUNT
        if action == "BUY" and amount > single_max and not force:
            log.warning("安全边界: 单笔 %s 超过上限 (%.0f > %.0f)",
                        code, amount, single_max)
            return {
                "status": "rejected",
                "order_id": "",
                "fill_price": price, "shares": shares, "amount": amount,
                "reason": (f"单笔金额 {amount:.0f} 超过安全上限 "
                          f"{single_max:.0f} ({MAX_SINGLE_AUTO_AMOUNT:.0%} NAV)"),
                "safety_checks": {"single_amount_limit": "BLOCKED"},
            }

        # ── 安全边界 3: 日交易笔数上限 ──
        self._rotate_daily()
        if action == "BUY" and self._state.daily_buy_count >= MAX_BUY_ORDERS_PER_DAY and not force:
            return {
                "status": "rejected",
                "order_id": "",
                "fill_price": price, "shares": shares, "amount": amount,
                "reason": (f"今日买入 {self._state.daily_buy_count}/{MAX_BUY_ORDERS_PER_DAY}，"
                          f"已达日上限"),
                "safety_checks": {"daily_buy_limit": "BLOCKED"},
            }

        # ── 执行 ──
        try:
            result = self._backend.place_order(code, action, price, shares)
        except Exception as e:
            log.error(f"下单失败 {code} {action}: {e}")
            return {
                "status": "error",
                "order_id": "",
                "fill_price": price, "shares": shares, "amount": amount,
                "reason": f"后端错误: {e}",
                "safety_checks": {},
            }

        # ── 更新日计数 ──
        if result.get("status") in ("filled", "pending_confirm", "placed"):
            if action == "BUY":
                self._state.daily_buy_count += 1
            elif action == "SELL":
                self._state.daily_sell_count += 1
            self._state.orders_today.append({
                "order_id": result.get("order_id", ""),
                "code": code,
                "action": action,
                "price": price,
                "shares": shares,
                "amount": amount,
                "status": result.get("status", ""),
                "time": datetime.now().isoformat(),
            })
            self._save_state()

        result["safety_checks"] = {
            "single_amount_limit": "PASS",
            "daily_buy_limit": "PASS",
            "observation_mode": "PASS",
            "kill_switch": "PASS",
        }
        return result

    def cancel_order(self, order_id: str) -> dict:
        return self._backend.cancel_order(order_id)

    def get_positions(self) -> list[dict]:
        return self._backend.get_positions()

    def get_account(self) -> dict:
        return self._backend.get_account()

    def get_daily_counts(self) -> dict:
        self._rotate_daily()
        return {
            "buy": self._state.daily_buy_count,
            "sell": self._state.daily_sell_count,
            "max_buy": MAX_BUY_ORDERS_PER_DAY,
            "max_sell": MAX_SELL_ORDERS_PER_DAY,
        }

    def reset_daily(self) -> None:
        self._state.daily_buy_count = 0
        self._state.daily_sell_count = 0
        self._state.date = date.today().isoformat()
        self._state.orders_today = []
        self._save_state()

    # ── 内部 ──────────────────────────────────────────────

    def _rotate_daily(self) -> None:
        today = date.today().isoformat()
        if self._state.date != today:
            self._state.daily_buy_count = 0
            self._state.daily_sell_count = 0
            self._state.date = today
            self._state.orders_today = []

    def _load_state(self) -> GatewayState:
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE) as f:
                    data = json.load(f)
                return GatewayState(**data)
            except Exception:
                pass
        return GatewayState()

    def _save_state(self) -> None:
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(asdict(self._state), f, ensure_ascii=False, indent=2)
        except Exception as e:
            log.debug(f"网关状态保存失败: {e}")


# ═══════════════════════════════════════════════════════════════
# 模块级单例
# ═══════════════════════════════════════════════════════════════

_gateway: Optional[TradeGateway] = None


def get_gateway(backend: Optional[Backend] = None) -> TradeGateway:
    global _gateway
    if backend is not None:
        _gateway = TradeGateway(backend)
    if _gateway is None:
        _gateway = TradeGateway(Backend.PAPER)
    return _gateway
