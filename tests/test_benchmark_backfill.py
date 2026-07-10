"""Benchmark backfills remain diagnostic and cannot manufacture gate samples."""

import fetch_history


def test_gate_benchmarks_have_explicit_shanghai_instrument_metadata():
    assert fetch_history._instrument_info("000300") == {"name": "沪深300", "market": "sh"}
    assert fetch_history._instrument_info("000905") == {"name": "中证500", "market": "sh"}


def test_benchmark_backfill_is_persisted_as_diagnostic_only(monkeypatch):
    saved = []
    monkeypatch.setattr(fetch_history, "save_price_history", lambda code, row: saved.append((code, row)))

    count = fetch_history.save_to_db("000905", [{
        "day": "2026-06-12",
        "open": 100,
        "close": 101,
        "high": 102,
        "low": 99,
        "volume": 1000,
        "change_pct": 1,
    }])

    assert count == 1
    assert saved[0][1]["adjustment_mode"] == "raw"
    assert saved[0][1]["quality_status"] == "diagnostic_only"
