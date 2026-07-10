#!/usr/bin/env python3
"""
Daily data-source health check — probes all configured data sources and
updates ``source_health_log`` so ``get_best_source()`` has up-to-date info.
"""

import sys
import os
import time

PROJECT = os.path.dirname(os.path.abspath(__file__))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from db import get_conn


def _test_source(label: str, fetch_fn, timeout: float = 10.0) -> tuple[bool, float, str]:
    """Return (reachable, latency_ms, error_or_empty)."""
    t0 = time.perf_counter()
    try:
        result = fetch_fn()
        elapsed = (time.perf_counter() - t0) * 1000
        # Accept any non-empty response (after-hours prices may be 0 but
        # the source is still reachable if we got structured data back).
        ok = bool(result)
        return ok, elapsed, "" if ok else "empty response (after-hours ok)"
    except Exception as exc:
        elapsed = (time.perf_counter() - t0) * 1000
        return False, elapsed, str(exc)[:200]


def main():
    from config import ALL_CODES

    sources = {}

    # -- Sina --
    try:
        from data_engine import sina_fetch_raw, parse_sina_line
        def _test_sina():
            raw = sina_fetch_raw(["sh600036", "sz002281"])
            lines = [l for l in raw.replace("\\n", "\n").split("\n") if "=" in l] if raw else []
            return [p for p in (parse_sina_line(line) for line in lines) if p is not None]
        sources["sina"] = _test_sina
    except Exception as exc:
        print(f"  sina: import failed ({exc})")

    # -- Tencent (codes WITHOUT sh/sz prefix) --
    try:
        from data_engine import _tencent_fetch_realtime
        sources["tencent"] = lambda: _tencent_fetch_realtime(["600036", "002281"])
    except Exception as exc:
        print(f"  tencent: import failed ({exc})")

    # -- AKShare (best-effort) --
    try:
        sources["akshare"] = lambda: __import__("data_engine")._akshare_fetch_realtime(["600036", "002281"])
    except Exception:
        print("  akshare: skipped (module unavailable)")

    # -- mootdx (best-effort) --
    try:
        from mootdx.quotes import Quotes
        client = Quotes.factory(market="std")
        sources["mootdx"] = lambda: client.bars(symbol="600036", frequency=9, offset=0, start=0)
    except Exception:
        print("  mootdx: skipped (module unavailable)")

    conn = get_conn()
    data_points = 0
    for label, fn in sources.items():
        ok, lat, err = _test_source(label, fn)
        data_points = 2 if ok else 0
        conn.execute(
            """INSERT INTO source_health_log
               (source, reachable, latency_ms, data_points, error, checked_at)
               VALUES (?, ?, ?, ?, ?, datetime('now', 'localtime'))""",
            (label, int(ok), round(lat), data_points, err or None),
        )
        conn.commit()
        status = "✅" if ok else "❌"
        print(f"  {status} {label:10s}  {lat:7.0f}ms  {err or ''}")

    conn.close()
    print(f"\nUpdated source_health_log with {len(sources)} source(s).")


if __name__ == "__main__":
    main()
