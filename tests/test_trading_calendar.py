"""Regression tests for the official A-share trading calendar."""

from datetime import date

from check_trading_day import (
    WEEKEND_MAKEUP_WORKDAYS,
    calendar_coverage_status,
    is_trading_day,
    next_trading_day,
)


def test_makeup_work_weekends_remain_exchange_holidays():
    assert date(2025, 1, 26) in WEEKEND_MAKEUP_WORKDAYS
    assert date(2026, 2, 14) in WEEKEND_MAKEUP_WORKDAYS
    assert not is_trading_day(date(2025, 1, 26))
    assert not is_trading_day(date(2026, 2, 14))
    assert not is_trading_day(date(2026, 2, 28))


def test_2026_exchange_holidays_include_full_spring_and_mid_autumn_breaks():
    assert not is_trading_day(date(2026, 2, 16))
    assert next_trading_day(date(2026, 2, 13)) == date(2026, 2, 24)
    assert not is_trading_day(date(2026, 9, 25))
    assert next_trading_day(date(2026, 9, 24)) == date(2026, 9, 28)


def test_calendar_coverage_is_explicit():
    assert calendar_coverage_status(date(2026, 7, 2))["verified"] is True
    assert calendar_coverage_status(date(2027, 1, 4))["verified"] is False
