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
from frozen_baseline import FROZEN_SINCE

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

    def __init__(self, initial_capital: Optional[float] = None):
        self.initial_capital = initial_capital or INITIAL_CAPITAL
        self.n_stocks = len(ALL_CODES)
        self.weight = 1.0 / self.n_stocks
        self._nav: float = self.initial_capital
        self._last_rebalance: Optional[str] = None
        self._curve_cache: Optional[tuple[str, list[dict]]]= None

    # ── 净值计算 ─────────────────────────────────────────

    def get_nav(self, as_of: Optional[date] = None) -> float:
        """获取基于真实逐日持仓账本的净值。"""
        if as_of is None:
            as_of = date.today()
        curve = self._build_nav_curve(as_of)
        return curve[-1]["nav"] if curve else self.initial_capital

    def _build_nav_curve(self, as_of: Optional[date] = None) -> list[dict]:
        """按开盘调仓、收盘计价，记录现金、持仓和交易成本。"""
        as_of = as_of or date.today()
        cache_key = as_of.isoformat()
        if self._curve_cache and self._curve_cache[0] == cache_key:
            return self._curve_cache[1]

        by_code: dict[str, dict[str, dict]] = {}
        all_dates: set[str] = set()
        for code in ALL_CODES:
            rows = get_price_history(code, days=800)
            code_rows = {
                row["date"]: row for row in rows
                if FROZEN_SINCE <= row["date"] <= cache_key
                and float(row.get("open") or 0) > 0
                and float(row.get("close") or 0) > 0
            }
            by_code[code] = code_rows
            all_dates.update(code_rows)

        cash = float(self.initial_capital)
        shares = {code: 0 for code in ALL_CODES}
        last_close: dict[str, float] = {}
        curve: list[dict] = []
        last_rebalance_month = ""

        for date_str in sorted(all_dates):
            available = {
                code: rows[date_str] for code, rows in by_code.items()
                if date_str in rows
            }
            if not available:
                continue

            month = date_str[:7]
            should_rebalance = not curve or month != last_rebalance_month
            cost_today = 0.0
            if should_rebalance:
                open_nav = cash + sum(
                    shares[code] * float(
                        available.get(code, {}).get("open") or last_close.get(code, 0)
                    )
                    for code in ALL_CODES
                )
                target = open_nav / len(available)

                # 先卖后买，卖出所得可以用于当日其他标的调仓。
                for code, row in available.items():
                    px = float(row["open"])
                    target_shares = int(target / px / 100) * 100
                    sell_qty = max(0, shares[code] - target_shares)
                    if sell_qty:
                        proceeds = sell_qty * px
                        fee = proceeds * (COMMISSION + STAMP_TAX + SLIPPAGE)
                        cash += proceeds - fee
                        cost_today += fee
                        shares[code] -= sell_qty

                for code, row in available.items():
                    px = float(row["open"])
                    target_shares = int(target / px / 100) * 100
                    buy_qty = max(0, target_shares - shares[code])
                    buy_qty = min(buy_qty, int(cash / (px * (1 + COMMISSION + SLIPPAGE)) / 100) * 100)
                    if buy_qty:
                        amount = buy_qty * px
                        fee = amount * (COMMISSION + SLIPPAGE)
                        cash -= amount + fee
                        cost_today += fee
                        shares[code] += buy_qty

                last_rebalance_month = month

            for code, row in available.items():
                last_close[code] = float(row["close"])
            nav = cash + sum(shares[code] * last_close.get(code, 0) for code in ALL_CODES)
            curve.append({
                "date": date_str,
                "nav": round(nav, 2),
                "cash": round(cash, 2),
                "cost": round(cost_today, 2),
                "positions": sum(1 for qty in shares.values() if qty > 0),
            })

        self._curve_cache = (cache_key, curve)
        return curve

    def _period_return(self, start: date, end: date) -> float:
        curve = [row for row in self._build_nav_curve(end) if row["date"] >= start.isoformat()]
        if len(curve) < 2 or curve[0]["nav"] <= 0:
            return 0.0
        return curve[-1]["nav"] / curve[0]["nav"] - 1.0

    def get_daily_return(self, snapshots: Optional[list[dict]] = None) -> float:
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
                return sum(returns) / len(returns) / 100.0

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
            return sum(returns) / len(returns) / 100.0 if returns else 0.0
        except Exception:
            return 0.0
        finally:
            conn.close()

    def get_weekly_return(self) -> float:
        """获取本周累计收益率。"""
        today = date.today()
        return self._period_return(today - timedelta(days=today.weekday()), today)

    def get_monthly_return(self) -> float:
        """获取本月累计收益率。"""
        today = date.today()
        return self._period_return(today.replace(day=1), today)

    # ── 再平衡 ────────────────────────────────────────────

    def is_rebalance_day(self, d: Optional[date] = None) -> bool:
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
        curve = self._build_nav_curve()
        return curve[-1]["cost"] if curve else 0.0

    # ── 快照 ──────────────────────────────────────────────

    def snapshot(self) -> dict:
        """返回当前基准快照。"""
        nav = self.get_nav()
        return {
            "date": date.today().isoformat(),
            "nav": round(nav, 2),
            "total_return_pct": round((nav - self.initial_capital) / self.initial_capital * 100, 2),
            "daily_return": round(self.get_daily_return() * 100, 3),
            "weekly_return": round(self.get_weekly_return() * 100, 3),
            "monthly_return": round(self.get_monthly_return() * 100, 3),
            "data_points": len(self._build_nav_curve()),
            "method": "event_ledger_raw_prices",
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
