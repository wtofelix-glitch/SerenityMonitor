"""Real signal samples are created only on exchange trading days."""

from datetime import date

import signal_engine


def test_signal_persistence_guard_rejects_weekend():
    assert signal_engine._should_persist_signal(date(2026, 7, 4)) is False
    assert signal_engine._should_persist_signal(date(2026, 7, 5)) is False


def test_signal_persistence_guard_accepts_trading_day():
    assert signal_engine._should_persist_signal(date(2026, 7, 3)) is True
