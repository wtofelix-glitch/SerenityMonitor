"""
Serenity 2.0 — 可注入模拟时钟

解决离线回放时间轴问题：
  · 生产模式: 使用真实系统时间
  · 离线回放: 使用注入的模拟时间
  · 结算时点: 明确 T+1 的 cutoff

用法:
    from serenity_v2.clock import set_clock, get_clock, SimClock

    # 离线回放
    set_clock(SimClock("2026-07-23T09:30:00+08:00"))
    get_clock().now()  # → 模拟时间
    get_clock().today()  # → 模拟日期
    get_clock().is_settlement_cutoff_passed()  # → 是否过结算时点

    # 生产模式（默认）
    get_clock()  # → RealClock
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, date, timezone, timedelta
from typing import Optional

CST = timezone(timedelta(hours=8))


# ---------------------------------------------------------------------------
# 时钟抽象
# ---------------------------------------------------------------------------

class Clock(ABC):
    """可注入时钟基类。"""

    @abstractmethod
    def now(self) -> datetime:
        """当前时间（含时区）。"""
        ...

    def today(self) -> date:
        """当前日期。"""
        return self.now().date()

    def is_business_day(self) -> bool:
        """是否交易日（简化：周一至周五）。"""
        return self.today().weekday() < 5

    def next_business_day(self) -> date:
        """下一个交易日。"""
        d = self.today() + timedelta(days=1)
        while d.weekday() >= 5:
            d = d + timedelta(days=1)
        return d

    def settlement_date_for_buy(self, buy_date: Optional[date] = None) -> date:
        """买入成交的结算日（T+1 的下一个交易日）。"""
        d = buy_date or self.today()
        return self._next_business_day_from(d)

    @staticmethod
    def _next_business_day_from(d: date) -> date:
        d = d + timedelta(days=1)
        while d.weekday() >= 5:
            d = d + timedelta(days=1)
        return d

    def is_settlement_cutoff_passed(self) -> bool:
        """
        【已弃用 — 请使用 eod_finalized_through_trade_date】
        当日结算时点（15:30）是否已过。

        这是一条日终处理截止规则，不应与持仓 T+1 可卖状态混淆。
        即使此方法返回 False（如 9:35），T+1 买入仍因
        settle_t1(reference_date=today) 变为可卖。
        """
        now = self.now()
        cutoff = now.replace(hour=15, minute=30, second=0, microsecond=0)
        return now >= cutoff

    def position_available_as_of(self) -> date:
        """
        持仓可卖结算已覆盖到哪个交易日。

        T+1 结算规则：买入日 T 的批次在 settlement_date（T+1 的
        下一个交易日）当日开盘即释放。因此对于任意交易日，
        当日日期即为 position_available_as_of（只要该日是交易日）。

        例如：2026-07-23 09:35 → 返回 2026-07-23。
        原因：7/22 买入的批次 settlement_date=7/23，7/23 开盘后可结算。

        与 eod_finalized_through_trade_date 的区别：
          · 此方法返回当日日期（持仓已可在当日交易）
          · eod_finalized_through_trade_date 返回日终处理完成日（15:30后）
        """
        today = self.today()
        if today.weekday() >= 5:
            # 非交易日 → 回退到前一交易日
            d = today - timedelta(days=1)
            while d.weekday() >= 5:
                d = d - timedelta(days=1)
            return d
        return today

    def eod_finalized_through_trade_date(self) -> date:
        """
        日终账户数据已完成处理的截止交易日。

        15:30 前 → 最近完成日终的交易日为前一交易日。
        15:30 后 → 当日日终可完成。

        例如：
          2026-07-23 09:35 → 返回 2026-07-22（今日日终未到）
          2026-07-23 15:31 → 返回 2026-07-23（今日日终已可处理）

        与 position_available_as_of 的区别见其文档。
        """
        now = self.now()
        today = self.today()
        cutoff = now.replace(hour=15, minute=30, second=0, microsecond=0)

        if now >= cutoff:
            return today
        else:
            d = today - timedelta(days=1)
            while d.weekday() >= 5:
                d = d - timedelta(days=1)
            return d

    def is_trading_session(self) -> bool:
        """是否在连续竞价时段（9:30-15:00）。"""
        now = self.now()
        t = now.hour * 60 + now.minute
        return 570 <= t < 900  # 9:30-15:00

    def is_premarket(self) -> bool:
        """是否在集合竞价时段（9:15-9:25）。"""
        now = self.now()
        t = now.hour * 60 + now.minute
        return 555 <= t < 565  # 9:15-9:25

    def market_session(self) -> str:
        """
        当前所属交易时段（A股完整阶段，半开区间 [start, end)）。

        非交易日无条件返回 CLOSED。

        返回值:
          OPENING_AUCTION_CANCELABLE  [09:15, 09:20)  可撤单集合竞价
          OPENING_AUCTION_NO_CANCEL   [09:20, 09:25)  不可撤单集合竞价
          OPENING_MATCH_EVENT         [09:25, 09:26)  开盘撮合（时点事件，用整分钟近似）
          PRE_OPEN_PAUSE              [09:26, 09:30)  开盘前暂停
          CONTINUOUS_AM               [09:30, 11:30)  上午连续竞价
          LUNCH_BREAK                 [11:30, 13:00)  午休
          CONTINUOUS_PM               [13:00, 14:57)  下午连续竞价
          CLOSING_AUCTION             [14:57, 15:00)  收盘集合竞价
          POSTMARKET                  [15:00, 15:30)  盘后
          CLOSED                      其他时间（含非交易日）
        """
        # 非交易日 → 无条件 CLOSED
        if self.today().weekday() >= 5:
            return "CLOSED"

        now = self.now()
        t = now.hour * 60 + now.minute

        if 555 <= t < 570:  # 9:15-9:30 开盘集合竞价阶段
            if t < 560:     # [09:15, 09:20)
                return "OPENING_AUCTION_CANCELABLE"
            elif t < 565:   # [09:20, 09:25)
                return "OPENING_AUCTION_NO_CANCEL"
            elif t < 566:   # [09:25, 09:26) 撮合时点
                return "OPENING_MATCH_EVENT"
            else:           # [09:26, 09:30)
                return "PRE_OPEN_PAUSE"
        elif 570 <= t < 690:  # [09:30, 11:30)
            return "CONTINUOUS_AM"
        elif 690 <= t < 780:  # [11:30, 13:00)
            return "LUNCH_BREAK"
        elif 780 <= t < 900:  # 13:00-15:00 下午阶段
            if t < 897:       # [13:00, 14:57)
                return "CONTINUOUS_PM"
            else:             # [14:57, 15:00)
                return "CLOSING_AUCTION"
        elif 900 <= t < 930:  # [15:00, 15:30)
            return "POSTMARKET"
        else:
            return "CLOSED"

# ---------------------------------------------------------------------------
# 真实时钟
# ---------------------------------------------------------------------------

class RealClock(Clock):
    """使用真实系统时间。"""

    def now(self) -> datetime:
        return datetime.now(tz=CST)


# ---------------------------------------------------------------------------
# 模拟时钟
# ---------------------------------------------------------------------------

class SimClock(Clock):
    """可注入的模拟时钟。"""

    def __init__(self, iso_time: str):
        """
        iso_time: ISO 8601 格式时间字符串
        例: "2026-07-23T09:30:00+08:00"
        """
        self._now = datetime.fromisoformat(iso_time)
        if self._now.tzinfo is None:
            self._now = self._now.replace(tzinfo=CST)

    def now(self) -> datetime:
        return self._now

    def advance(self, **kwargs):
        """推进时间。支持 days, hours, minutes, seconds。"""
        self._now = self._now + timedelta(**kwargs)
        return self

    def set(self, iso_time: str):
        """设置到指定时间。"""
        self._now = datetime.fromisoformat(iso_time)
        if self._now.tzinfo is None:
            self._now = self._now.replace(tzinfo=CST)
        return self


# ---------------------------------------------------------------------------
# 全局时钟管理
# ---------------------------------------------------------------------------

_clock: Optional[Clock] = None


def set_clock(clock: Clock) -> None:
    """设置全局时钟。"""
    global _clock
    _clock = clock


def get_clock() -> Clock:
    """获取当前时钟。未设置时使用真实时钟。"""
    global _clock
    if _clock is None:
        _clock = RealClock()
    return _clock


def reset_clock() -> None:
    """重置为真实时钟（测试用）。

    始终将全局时钟重置为 None，下次 get_clock() 将返回 RealClock。
    调用者如果在 SimClock 之后调用此函数，必须确保 SimClock 已被正确处理。
    """
    global _clock
    _clock = None
