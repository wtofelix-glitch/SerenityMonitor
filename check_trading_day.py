#!/usr/bin/env python3
"""
A股交易日判断模块 — 供 Serenity 所有 no_agent cron 脚本调用

用法:
    from check_trading_day import is_trading_day, require_trading_day

    if not is_trading_day():
        sys.exit(0)  # 静默退出

    # 或在脚本最顶部直接：
    require_trading_day()

交易日规则:
    1. 周末（周六/周日）→ 非交易日
    2. 上交所/深交所公布的法定节假日 → 非交易日
    3. 调休补班（节假日前后的周末上班日）→ 交易日
"""

import sys
from datetime import date, datetime
from typing import Optional

# ── A股法定节假日（含调休日） ──────────────────────────────
# 数据来源：上交所/深交所年度休市通知
# 格式：date(yyyy, m, d)
# 更新策略：每年12月交易所公布次年安排后更新

HOLIDAYS: set[date] = {
    # ── 2025 年 ──
    # 元旦
    date(2025, 1, 1),
    # 春节
    date(2025, 1, 28), date(2025, 1, 29), date(2025, 1, 30),
    date(2025, 1, 31), date(2025, 2, 1), date(2025, 2, 2),
    date(2025, 2, 3), date(2025, 2, 4),
    # 清明节
    date(2025, 4, 4), date(2025, 4, 5), date(2025, 4, 6),
    # 劳动节
    date(2025, 5, 1), date(2025, 5, 2), date(2025, 5, 3),
    date(2025, 5, 4), date(2025, 5, 5),
    # 端午节
    date(2025, 5, 31), date(2025, 6, 1), date(2025, 6, 2),
    # 中秋节+国庆节
    date(2025, 10, 1), date(2025, 10, 2), date(2025, 10, 3),
    date(2025, 10, 4), date(2025, 10, 5), date(2025, 10, 6),
    date(2025, 10, 7), date(2025, 10, 8),

    # ── 2026 年 ──
    # 元旦
    date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3),
    # 春节
    date(2026, 2, 17), date(2026, 2, 18), date(2026, 2, 19),
    date(2026, 2, 20), date(2026, 2, 21), date(2026, 2, 22),
    date(2026, 2, 23),
    # 清明节
    date(2026, 4, 4), date(2026, 4, 5), date(2026, 4, 6),
    # 劳动节
    date(2026, 5, 1), date(2026, 5, 2), date(2026, 5, 3),
    date(2026, 5, 4), date(2026, 5, 5),
    # 端午节
    date(2026, 6, 19), date(2026, 6, 20), date(2026, 6, 21),
    # 中秋节+国庆节（预计）
    date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 3),
    date(2026, 10, 4), date(2026, 10, 5), date(2026, 10, 6),
    date(2026, 10, 7),
}

# ── 调休补班（这些周末要上班 = 交易日） ────────────────────
# 格式：date(yyyy, m, d)
# 调休日需要上班，所以是交易日
WORKDAYS: set[date] = {
    # 2025 调休补班
    date(2025, 1, 26),   # 春节前周日
    date(2025, 2, 8),    # 春节后周六
    date(2025, 4, 27),   # 劳动节前周日
    date(2025, 9, 28),   # 国庆前周日
    date(2025, 10, 11),  # 国庆后周六

    # 2026 调休补班（待交易所确认后更新）
    date(2026, 2, 14),   # 春节前周六
    date(2026, 2, 28),   # 春节后周六
    date(2026, 4, 25),   # 劳动节前周六
    date(2026, 9, 26),   # 国庆前周六
    date(2026, 10, 10),  # 国庆后周六
}


def is_trading_day(check_date: Optional[date] = None) -> bool:
    """判断指定日期（默认今天）是否为A股交易日"""
    d = check_date or date.today()

    # 周末判断
    if d.weekday() >= 5:  # 5=周六, 6=周日
        # 排除调休补班
        return d in WORKDAYS  # 补班的周末算交易日

    # 工作日判断：避开法定节假日
    return d not in HOLIDAYS


def require_trading_day(check_date: Optional[date] = None) -> None:
    """非交易日直接静默退出（exit code 0），不产生任何输出"""
    if not is_trading_day(check_date):
        sys.exit(0)


def next_trading_day(from_date: Optional[date] = None) -> date:
    """返回 from_date（默认今天）之后的下一个交易日"""
    d = (from_date or date.today())
    d = d.__class__.fromordinal(d.toordinal() + 1)
    while not is_trading_day(d):
        d = d.__class__.fromordinal(d.toordinal() + 1)
    return d


if __name__ == "__main__":
    """CLI 用法: python3 check_trading_day.py [YYYY-MM-DD]"""
    check = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date.today()
    result = is_trading_day(check)
    print(f"{'✅' if result else '❌'} {check.isoformat()} {'交易日' if result else '非交易日'}")
    sys.exit(0 if result else 1)
