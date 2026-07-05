"""
A 股微观结构约束检查 — market_microstructure.py

实现 T+1 锁定、涨跌停、停牌、一字板等 A 股特有的交易约束检查。
所有交易信号在生成执行计划前，必须通过此模块的可成交性验证。

Usage:
    from market_microstructure import MarketMicrostructure

    ms = MarketMicrostructure()
    result = ms.can_buy("002281", date.today(), 200.0, 300)
    if not result.executable:
        print(f"不可买入: {result.block_reason}")

覆盖的约束（v4 §4.4）:
    [x] T+1 锁定（当日买入 → 当日不可卖出）
    [x] 涨停不可买（尤其是一字板）
    [x] 跌停不可卖（尤其是一字板）
    [x] 停牌不可交易
    [x] 100 股整数手
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Optional

from db import get_conn, get_price_history
from serenity_logger import get_logger

log = get_logger(__name__)

# ═══════════════════════════════════════════════════════════════
# 数据结构
# ═══════════════════════════════════════════════════════════════


class LimitStatus(Enum):
    """涨跌停状态"""
    NORMAL = "normal"               # 正常交易
    LIMIT_UP = "limit_up"            # 涨停（可能仍有成交量）
    LIMIT_DOWN = "limit_down"        # 跌停（可能仍有成交量）
    LIMIT_UP_HARD = "limit_up_hard"  # 一字涨停（基本无成交量）
    LIMIT_DOWN_HARD = "limit_down_hard"  # 一字跌停（基本无成交量）
    SUSPENDED = "suspended"          # 停牌
    UNKNOWN = "unknown"              # 无法判断（数据缺失）


@dataclass
class TradeabilityResult:
    """可成交性检查结果"""
    executable: bool = True
    block_reason: str = ""
    limit_status: LimitStatus = LimitStatus.NORMAL
    t1_locked: bool = False
    t1_locked_shares: int = 0
    is_mainboard: bool = True
    lot_aligned: bool = True
    checks_passed: list[str] = field(default_factory=list)
    checks_failed: list[str] = field(default_factory=list)


@dataclass
class PositionLock:
    """T+1 锁定仓位记录"""
    code: str
    buy_date: str           # ISO date
    buy_price: float
    shares: int
    unlock_date: str        # ISO date (buy_date + 1 trading day)
    current_float_pnl_pct: float = 0.0  # 当前浮动盈亏


# ═══════════════════════════════════════════════════════════════
# 主类
# ═══════════════════════════════════════════════════════════════


class MarketMicrostructure:
    """A 股微观结构约束检查器。

    在 scorer/signal_engine 和 auto_execute 之间插入，
    确保所有交易建议在 A 股实际规则下可执行。
    """

    def __init__(self):
        # T+1 锁定的持仓记录 (code → PositionLock)
        self._t1_locks: dict[str, PositionLock] = {}
        # 当日的涨跌停缓存 (code → LimitStatus)
        self._limit_cache: dict[str, LimitStatus] = {}
        # 停牌缓存
        self._suspended_cache: set[str] = set()
        # 加载已有的 T+1 锁定记录
        self._load_existing_locks()

    # ── T+1 锁定 ──────────────────────────────────────────

    def _load_existing_locks(self) -> None:
        """从 trades 表和 t1_lock_records 表加载已有的 T+1 锁定仓位。"""
        conn = get_conn()
        try:
            today = date.today().isoformat()
            # 查询今日买入的仓位（即被 T+1 锁定的）
            rows = conn.execute(
                "SELECT code, date, price, quantity FROM trades "
                "WHERE LOWER(action)='buy' AND date=?",
                (today,)
            ).fetchall()
            for row in rows:
                code = row["code"]
                # 计算解锁日（下一个交易日）
                from check_trading_day import next_trading_day
                unlock = next_trading_day(date.today())
                self._t1_locks[code] = PositionLock(
                    code=code,
                    buy_date=row["date"],
                    buy_price=row["price"],
                    shares=row["quantity"],
                    unlock_date=unlock.isoformat(),
                )
                log.debug(f"T+1 锁定: {code} {row['quantity']}股 @{row['price']}, 解锁日 {unlock}")
        except Exception as e:
            log.warning(f"加载 T+1 锁定记录失败: {e}")
        finally:
            conn.close()

    def is_t1_locked(self, code: str, check_date: date | None = None) -> bool:
        """检查指定标的在指定日期是否受 T+1 锁定（当日买入不可卖出）。"""
        if check_date is None:
            check_date = date.today()
        lock = self._t1_locks.get(code)
        if lock is None:
            return False
        return lock.unlock_date > check_date.isoformat() and lock.unlock_date >= check_date.isoformat()

    def get_t1_lock(self, code: str) -> Optional[PositionLock]:
        """获取 T+1 锁定信息。"""
        return self._t1_locks.get(code)

    def add_t1_lock(self, code: str, buy_date: date, buy_price: float, shares: int) -> PositionLock:
        """记录一笔新的 T+1 锁定（买入成交后调用）。"""
        from check_trading_day import next_trading_day
        unlock = next_trading_day(buy_date)
        lock = PositionLock(
            code=code,
            buy_date=buy_date.isoformat(),
            buy_price=buy_price,
            shares=shares,
            unlock_date=unlock.isoformat(),
        )
        self._t1_locks[code] = lock
        log.info(f"新增 T+1 锁定: {code} {shares}股 @{buy_price}, 解锁日 {unlock}")
        return lock

    def get_total_t1_locked_value(self, price_map: dict[str, float]) -> float:
        """计算所有 T+1 锁定仓位的当前市值。"""
        total = 0.0
        for code, lock in self._t1_locks.items():
            price = price_map.get(code, lock.buy_price)
            total += price * lock.shares
        return total

    def get_total_t1_locked_pct(self, total_nav: float, price_map: dict[str, float]) -> float:
        """计算 T+1 锁定仓位占总 NAV 的百分比。"""
        if total_nav <= 0:
            return 0.0
        return self.get_total_t1_locked_value(price_map) / total_nav

    # ── 涨跌停 ────────────────────────────────────────────

    def get_limit_status(self, code: str, price: float | None = None,
                         change_pct: float | None = None,
                         volume: float | None = None,
                         avg_volume: float | None = None) -> LimitStatus:
        """检测涨跌停状态。

        涨停判断：change_pct >= 9.9%（主板 ±10%，留 0.1% 容差）
        跌停判断：change_pct <= -9.9%
        一字板判断：涨跌停 AND (volume == 0 OR volume/avg_volume < 0.05)
        """
        if code in self._suspended_cache:
            return LimitStatus.SUSPENDED

        # 如果没有涨跌幅数据，返回未知
        if change_pct is None:
            return LimitStatus.UNKNOWN

        # 涨跌停判断（主板 ±10%，用 ±9.9% 作为阈值留容差）
        if change_pct >= 9.9:
            # 一字涨停判断：几乎没有成交量
            if volume is not None and avg_volume is not None and avg_volume > 0:
                vol_ratio = volume / avg_volume
                if vol_ratio < 0.05:
                    return LimitStatus.LIMIT_UP_HARD
            elif volume is not None and volume < 100:  # 几乎无成交
                return LimitStatus.LIMIT_UP_HARD
            return LimitStatus.LIMIT_UP

        if change_pct <= -9.9:
            if volume is not None and avg_volume is not None and avg_volume > 0:
                vol_ratio = volume / avg_volume
                if vol_ratio < 0.05:
                    return LimitStatus.LIMIT_DOWN_HARD
            elif volume is not None and volume < 100:
                return LimitStatus.LIMIT_DOWN_HARD
            return LimitStatus.LIMIT_DOWN

        return LimitStatus.NORMAL

    def detect_limit_from_snapshot(self, code: str, snapshot: dict) -> LimitStatus:
        """从 daily_snapshot 或实时行情 dict 中检测涨跌停状态。"""
        change_pct = snapshot.get("change_pct")
        volume = snapshot.get("volume")
        # avg_volume 从 db 获取
        avg_volume = None
        try:
            from db import get_avg_volume
            avg_volume = get_avg_volume(code, days=20)
        except Exception:
            pass
        return self.get_limit_status(code, change_pct=change_pct,
                                     volume=volume, avg_volume=avg_volume)

    def set_suspended(self, code: str, suspended: bool = True) -> None:
        """标记停牌状态。"""
        if suspended:
            self._suspended_cache.add(code)
        else:
            self._suspended_cache.discard(code)

    def is_suspended(self, code: str) -> bool:
        """检查是否停牌。"""
        return code in self._suspended_cache

    # ── 综合可成交性检查 ──────────────────────────────────

    def can_buy(self, code: str, check_date: date, price: float,
                quantity: int, snapshot: dict | None = None) -> TradeabilityResult:
        """检查是否可以买入。

        Args:
            code: 股票代码
            check_date: 检查日期
            price: 委托价格
            quantity: 委托数量
            snapshot: 实时行情快照（可选，用于涨跌停检测）

        Returns:
            TradeabilityResult
        """
        result = TradeabilityResult()

        # 检查 1: 是否为主板标的
        if not self._is_mainboard(code):
            result.executable = False
            result.block_reason = f"{code} 非主板标的，不在交易范围内"
            result.checks_failed.append("mainboard_only")
            return result
        result.checks_passed.append("mainboard_only")

        # 检查 2: 是否停牌
        if self.is_suspended(code):
            result.executable = False
            result.block_reason = f"{code} 处于停牌状态"
            result.limit_status = LimitStatus.SUSPENDED
            result.checks_failed.append("not_suspended")
            return result
        result.checks_passed.append("not_suspended")

        # 检查 3: 涨跌停状态
        limit_status = LimitStatus.NORMAL
        if snapshot:
            limit_status = self.detect_limit_from_snapshot(code, snapshot)
        result.limit_status = limit_status

        if limit_status in (LimitStatus.LIMIT_UP, LimitStatus.LIMIT_UP_HARD):
            result.executable = False
            result.block_reason = f"{code} 处于涨停状态 ({limit_status.value})，无法买入"
            result.checks_failed.append("not_limit_up")
            return result
        result.checks_passed.append("not_limit_up")

        # 检查 4: 100 股整数手
        if quantity % 100 != 0:
            result.executable = False
            result.lot_aligned = False
            result.block_reason = f"委托数量 {quantity} 不是 100 股的整数倍"
            result.checks_failed.append("lot_aligned")
            return result
        result.checks_passed.append("lot_aligned")

        # 检查 5: 数量大于 0
        if quantity <= 0:
            result.executable = False
            result.block_reason = "委托数量必须大于 0"
            result.checks_failed.append("positive_quantity")
            return result
        result.checks_passed.append("positive_quantity")

        return result

    def can_sell(self, code: str, position_shares: int, check_date: date,
                 price: float, quantity: int | None = None,
                 snapshot: dict | None = None) -> TradeabilityResult:
        """检查是否可以卖出。

        Args:
            code: 股票代码
            position_shares: 当前持仓数量
            check_date: 检查日期
            price: 委托价格
            quantity: 委托数量（None 表示全部卖出）
            snapshot: 实时行情快照

        Returns:
            TradeabilityResult
        """
        result = TradeabilityResult()
        sell_qty = quantity if quantity is not None else position_shares

        # 检查 1: T+1 锁定
        lock = self._t1_locks.get(code)
        if lock and lock.unlock_date > check_date.isoformat():
            result.executable = False
            result.t1_locked = True
            result.t1_locked_shares = lock.shares
            result.block_reason = (
                f"{code} 受 T+1 锁定（{lock.buy_date} 买入 {lock.shares}股），"
                f"最早 {lock.unlock_date} 才能卖出"
            )
            result.checks_failed.append("t1_lock")
            return result
        result.checks_passed.append("t1_lock")

        # 检查 2: 持仓充足
        if position_shares <= 0:
            result.executable = False
            result.block_reason = f"{code} 当前无持仓"
            result.checks_failed.append("has_position")
            return result
        result.checks_passed.append("has_position")

        # 检查 3: 卖出数量不超过持仓
        if sell_qty > position_shares:
            result.executable = False
            result.block_reason = f"委托数量 {sell_qty} 超过持仓 {position_shares}"
            result.checks_failed.append("sufficient_shares")
            return result
        result.checks_passed.append("sufficient_shares")

        # 检查 4: 是否停牌
        if self.is_suspended(code):
            result.executable = False
            result.block_reason = f"{code} 处于停牌状态"
            result.limit_status = LimitStatus.SUSPENDED
            result.checks_failed.append("not_suspended")
            return result
        result.checks_passed.append("not_suspended")

        # 检查 5: 跌停状态
        limit_status = LimitStatus.NORMAL
        if snapshot:
            limit_status = self.detect_limit_from_snapshot(code, snapshot)
        result.limit_status = limit_status

        if limit_status in (LimitStatus.LIMIT_DOWN, LimitStatus.LIMIT_DOWN_HARD):
            result.executable = False
            result.block_reason = f"{code} 处于跌停状态 ({limit_status.value})，无法卖出"
            result.checks_failed.append("not_limit_down")
            return result
        result.checks_passed.append("not_limit_down")

        # 检查 6: 100 股整数手
        if sell_qty % 100 != 0:
            result.executable = False
            result.lot_aligned = False
            result.block_reason = f"委托数量 {sell_qty} 不是 100 股的整数倍"
            result.checks_failed.append("lot_aligned")
            return result
        result.checks_passed.append("lot_aligned")

        return result

    def check_t1_lock_aggregate(self, new_buy_code: str, new_buy_amount: float,
                                 total_nav: float, price_map: dict[str, float],
                                 max_t1_locked_pct: float = 0.40) -> TradeabilityResult:
        """T+1 锁定的组合层聚合校验（v4 §10.2）。

        检查：加上这笔新买入后，当日 T+1 锁定仓位总额是否超过组合上限。
        不是分别独立校验每笔是否 ≤25%，而是检查聚合后的 T+1 锁定总额。

        Args:
            new_buy_code: 新买入标的代码
            new_buy_amount: 新买入金额
            total_nav: 组合总净值
            price_map: {code: price} 当前价格映射
            max_t1_locked_pct: T+1 锁定仓位总上限（默认 40%）

        Returns:
            TradeabilityResult
        """
        result = TradeabilityResult()

        current_t1_value = self.get_total_t1_locked_value(price_map)
        new_t1_value = current_t1_value + new_buy_amount
        new_t1_pct = new_t1_value / total_nav if total_nav > 0 else 1.0

        if new_t1_pct > max_t1_locked_pct:
            result.executable = False
            result.block_reason = (
                f"T+1 锁定仓位总额将达 {new_t1_pct:.1%}（当前 {current_t1_value:.0f} + "
                f"新增 {new_buy_amount:.0f}），超过上限 {max_t1_locked_pct:.0%}"
            )
            result.checks_failed.append("t1_lock_aggregate")
            return result
        result.checks_passed.append("t1_lock_aggregate")
        return result

    # ── 工具 ──────────────────────────────────────────────

    @staticmethod
    def _is_mainboard(code: str) -> bool:
        """检查是否为主板标的（600/601/603/605/000/001/002/003 开头）。"""
        mainboard_prefixes = ("600", "601", "603", "605",
                              "000", "001", "002", "003")
        return any(code.startswith(p) for p in mainboard_prefixes)

    def clear_day_cache(self) -> None:
        """每日清除缓存（新交易日开始时调用）。"""
        self._limit_cache.clear()
        # T+1 锁在 _load_existing_locks 中重新加载


# ═══════════════════════════════════════════════════════════════
# 模块级单例
# ═══════════════════════════════════════════════════════════════

_microstructure_instance: Optional[MarketMicrostructure] = None


def get_microstructure() -> MarketMicrostructure:
    """获取 MarketMicrostructure 单例。"""
    global _microstructure_instance
    if _microstructure_instance is None:
        _microstructure_instance = MarketMicrostructure()
    return _microstructure_instance
