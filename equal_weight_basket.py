"""
等权基准系统 — equal_weight_basket.py

15 只标的等权买入持有，月频再平衡。作为 Frozen Baseline 和
Adaptive System 的硬性停机判据对比基准。

v4 §6 定义: 系统 C — Equal Weight Basket（等权基准）

关键参数:
  - 再平衡频率: 每月首个交易日
  - 再平衡时扣除交易成本（佣金+印花税+滑点）
  - 初始资金: 与 CAPITAL_CONFIG 对齐

Usage:
    from equal_weight_basket import EqualWeightBasket

    eq = EqualWeightBasket()
    nav = eq.get_nav()
    weekly_return = eq.get_weekly_return()
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional
import json

from config import ALL_CODES, STOCK_MAP, CAPITAL_CONFIG
from db import get_conn, get_price_history, get_latest_snapshot
from serenity_logger import get_logger

log = get_logger(__name__)

# ═══════════════════════════════════════════════════════════════
# 常量
# ═══════════════════════════════════════════════════════════════

N_STOCKS = len(ALL_CODES)  # 15
EQUAL_WEIGHT = 1.0 / N_STOCKS  # ~6.67% per stock

# 交易成本（与 execution_simulator.py 对齐）
COMMISSION = 0.00025     # 0.025%
STAMP_TAX = 0.0005       # 0.05%（卖出单向）
SLIPPAGE = 0.001         # 0.1% 滑点

INITIAL_CAPITAL = CAPITAL_CONFIG.get("initial_capital", 51066.41)


class EqualWeightBasket:
    """等权基准 — 15 只标的每月再平衡。

    提供与 Frozen Baseline 和 Adaptive System 相同的行情输入下的
    收益基准，用于判定策略是否有 Alpha。
    """

    def __init__(self, initial_capital: float | None = None):
        self.initial_capital = initial_capital or INITIAL_CAPITAL
        self.n_stocks = len(ALL_CODES)
        self.weight = 1.0 / self.n_stocks
        self._nav: float = self.initial_capital
        self._last_rebalance: str | None = None

    # ── 净值计算 ─────────────────────────────────────────

    def get_nav(self, as_of: date | None = None) -> float:
        """获取当前净值。使用最新快照价格计算。"""
        if as_of is None:
            as_of = date.today()

        total = 0.0
        per_stock_capital = self.initial_capital / self.n_stocks

        for code in ALL_CODES:
            snap = get_latest_snapshot(code)
            if snap:
                price = getattr(snap, "close", 0) or 0
                if price > 0:
                    shares = int(per_stock_capital / price / 100) * 100
                    total += shares * price

        return total if total > 0 else self.initial_capital

    def get_daily_return(self, snapshots: list[dict] | None = None) -> float:
        """计算当日等权平均收益率。

        如果 snapshots 为空，从 daily_snapshots 表获取。
        """
        if snapshots:
            returns = []
            for s in snapshots:
                code = s.get("code", "")
                if code in ALL_CODES:
                    chg = s.get("change_pct", 0) or 0
                    returns.append(chg)
            if returns:
                return sum(returns) / len(returns)

        # 回退到数据库
        conn = get_conn()
        try:
            today = date.today().isoformat()
            rows = conn.execute(
                "SELECT code, change_pct FROM daily_snapshots WHERE date=? AND code IN ({})".format(
                    ",".join("?" * len(ALL_CODES))
                ),
                (today, *ALL_CODES)
            ).fetchall()
            returns = [r["change_pct"] or 0 for r in rows]
            return sum(returns) / len(returns) if returns else 0.0
        except Exception:
            return 0.0
        finally:
            conn.close()

    def get_weekly_return(self) -> float:
        """获取本周累计收益率。"""
        conn = get_conn()
        try:
            today = date.today()
            # 找本周第一个交易日
            start = today - timedelta(days=7)
            rows = conn.execute(
                "SELECT date, AVG(change_pct) as avg_ret FROM daily_snapshots "
                "WHERE code IN ({}) AND date >= ? AND date <= ? "
                "GROUP BY date ORDER BY date".format(
                    ",".join("?" * len(ALL_CODES))
                ),
                (*ALL_CODES, start.isoformat(), today.isoformat())
            ).fetchall()
            if rows:
                # 累计收益（简单累加）
                return sum(r["avg_ret"] or 0 for r in rows)
        except Exception:
            pass
        finally:
            conn.close()
        return 0.0

    def get_monthly_return(self) -> float:
        """获取本月累计收益率。"""
        conn = get_conn()
        try:
            today = date.today()
            start = today.replace(day=1)
            rows = conn.execute(
                "SELECT date, AVG(change_pct) as avg_ret FROM daily_snapshots "
                "WHERE code IN ({}) AND date >= ? AND date <= ? "
                "GROUP BY date ORDER BY date".format(
                    ",".join("?" * len(ALL_CODES))
                ),
                (*ALL_CODES, start.isoformat(), today.isoformat())
            ).fetchall()
            if rows:
                return sum(r["avg_ret"] or 0 for r in rows)
        except Exception:
            pass
        finally:
            conn.close()
        return 0.0

    # ── 再平衡 ────────────────────────────────────────────

    def is_rebalance_day(self, d: date | None = None) -> bool:
        """判断是否为月度再平衡日（每月首个交易日）。"""
        if d is None:
            d = date.today()
        from check_trading_day import is_trading_day
        # 查找本月第一个交易日
        first = d.replace(day=1)
        for _ in range(10):
            if is_trading_day(first):
                return d == first
            first += timedelta(days=1)
        return False

    def rebalance_cost_estimate(self) -> float:
        """估算月度再平衡的交易成本。"""
        # 估算每只标的偏离等权后需调整的比例
        # 简化：假设 15 只标的中 5 只需要调整，每只调整 1% 权重
        nav = self.get_nav()
        adjusted_amount = nav * 0.01 * 5  # 5 只 × 1% 权重调整
        cost = adjusted_amount * (COMMISSION * 2 + STAMP_TAX + SLIPPAGE)
        return cost

    # ── 快照 ──────────────────────────────────────────────

    def snapshot(self) -> dict:
        """返回当前基准快照。"""
        nav = self.get_nav()
        return {
            "date": date.today().isoformat(),
            "nav": round(nav, 2),
            "total_return_pct": round((nav - self.initial_capital) / self.initial_capital * 100, 2),
            "daily_return": round(self.get_daily_return(), 3),
            "weekly_return": round(self.get_weekly_return(), 3),
            "monthly_return": round(self.get_monthly_return(), 3),
            "n_stocks": self.n_stocks,
            "equal_weight_pct": round(self.weight * 100, 2),
            "is_rebalance_day": self.is_rebalance_day(),
        }


# ═══════════════════════════════════════════════════════════════
# 模块级实例
# ═══════════════════════════════════════════════════════════════

_basket: Optional[EqualWeightBasket] = None


def get_basket() -> EqualWeightBasket:
    global _basket
    if _basket is None:
        _basket = EqualWeightBasket()
    return _basket
