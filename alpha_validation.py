"""P0 alpha validation package.

This module is deliberately read-only. It answers the final review's core
question: whether SerenityMonitor has evidence of cost-after, benchmark-adjusted
out-of-sample alpha, before more production engineering is added.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Optional, Any

from config import (
    ALL_CODES,
    CAPITAL_CONFIG,
    TIER_1_CODES,
    TIER_2_CODES,
    TIER_4_CODES,
)
from db import get_conn


BUY_ACTIONS = ("STRONG_BUY", "BUY", "CAUTION_BUY")
P0_REQUIRED_SAMPLES = 50
P0_REQUIRED_OOS_WINDOWS = 3
P0_REQUIRED_POSITIVE_WINDOWS = 2
MAX_BENCHMARK_START_LAG_DAYS = 5
MOMENTUM_LOOKBACK_POINTS = 20
MOMENTUM_TOP_N = 3


def _pct(first: float, last: float) -> Optional[float]:
    if first <= 0:
        return None
    return (last - first) / first * 100


def _status(label: str, ok: Optional[bool]) -> str:
    if ok is True:
        return "PASS"
    if ok is False:
        return "BLOCK"
    return "INSUFFICIENT"


def _max_drawdown(values: list[float]) -> Optional[float]:
    if not values:
        return None
    peak = values[0]
    worst = 0.0
    for value in values:
        if value > peak:
            peak = value
        if peak > 0:
            worst = min(worst, (value - peak) / peak * 100)
    return round(worst, 2)


def _drawdown_info(dates: list[str], values: list[float]) -> dict[str, Any]:
    if not values:
        return {"max_drawdown_pct": None, "max_drawdown_start": None, "max_drawdown_end": None}
    peak = values[0]
    peak_date = dates[0] if dates else None
    worst = 0.0
    worst_start = peak_date
    worst_end = peak_date
    for idx, value in enumerate(values):
        row_date = dates[idx] if idx < len(dates) else None
        if value > peak:
            peak = value
            peak_date = row_date
        if peak > 0:
            drawdown = (value - peak) / peak * 100
            if drawdown < worst:
                worst = drawdown
                worst_start = peak_date
                worst_end = row_date
    return {
        "max_drawdown_pct": round(worst, 2),
        "max_drawdown_start": worst_start,
        "max_drawdown_end": worst_end,
    }


def _date_lag_days(start: str, actual: str) -> int:
    try:
        return (date.fromisoformat(actual) - date.fromisoformat(start)).days
    except ValueError:
        return 999999


def _cost_model() -> dict[str, Any]:
    """Return a conservative round-trip cost model in percentage points."""
    try:
        from execution_simulator import (
            SLIPPAGE_BASIS_POINTS,
            STAMP_TAX_RATE,
            TRANSFER_FEE_RATE,
        )
    except Exception:
        SLIPPAGE_BASIS_POINTS = 0.001
        STAMP_TAX_RATE = 0.0005
        TRANSFER_FEE_RATE = 0.00001

    commission_rate = float(CAPITAL_CONFIG.get("commission_rate", 0.00025))
    stamp_tax_rate = max(float(CAPITAL_CONFIG.get("stamp_tax_rate", 0.0)), STAMP_TAX_RATE)
    slippage_rate = SLIPPAGE_BASIS_POINTS
    transfer_fee_rate = TRANSFER_FEE_RATE
    total_rate = (
        commission_rate * 2
        + stamp_tax_rate
        + transfer_fee_rate * 2
        + slippage_rate * 2
    )
    return {
        "commission_pct": round(commission_rate * 2 * 100, 4),
        "stamp_tax_pct": round(stamp_tax_rate * 100, 4),
        "transfer_fee_pct": round(transfer_fee_rate * 2 * 100, 4),
        "slippage_pct": round(slippage_rate * 2 * 100, 4),
        "round_trip_pct": round(total_rate * 100, 4),
        "note": "commission both sides + sell stamp tax + transfer fee both sides + baseline slippage both sides",
    }


def _current_config_hash() -> str:
    try:
        from auto_gate import compute_strategy_hash, default_strategy_config

        return compute_strategy_hash(default_strategy_config())
    except Exception:
        return ""


def _table_columns(conn, table: str) -> set[str]:
    try:
        return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return set()


def _has_table(conn, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _active_strategy(conn) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM strategy_versions WHERE is_active=1 "
        "ORDER BY created_at DESC, id DESC LIMIT 1"
    ).fetchone()
    current_hash = _current_config_hash()
    if not row:
        return {
            "version": "",
            "config_hash": "",
            "current_config_hash": current_hash,
            "hash_matches_current": False,
            "created_at": "",
        }
    data = dict(row)
    data["current_config_hash"] = current_hash
    data["hash_matches_current"] = bool(current_hash and data.get("config_hash") == current_hash)
    return data


def _buy_sample_rows(conn, version: str) -> list[dict[str, Any]]:
    if not version:
        return []
    placeholders = ",".join("?" for _ in BUY_ACTIONS)
    rows = conn.execute(
        f"""
        SELECT code, date, action, return_5d, outcome_5d, excess_5d, exit_date
        FROM signal_log
        WHERE action IN ({placeholders})
          AND strategy_version=?
          AND settlement_status='settled'
          AND executable_status='executable'
          AND data_quality='high'
          AND adjustment_mode IN ('raw', 'unadjusted')
          AND COALESCE(return_5d, outcome_5d) IS NOT NULL
          AND excess_5d IS NOT NULL
        ORDER BY date ASC, id ASC
        """,
        (*BUY_ACTIONS, version),
    ).fetchall()
    return [dict(row) for row in rows]


def _sample_state(rows: list[dict[str, Any]], cost_pct: float) -> dict[str, Any]:
    returns = [
        float(row["return_5d"] if row.get("return_5d") is not None else row.get("outcome_5d"))
        for row in rows
    ]
    excesses = [float(row["excess_5d"]) for row in rows]
    net_returns = [value - cost_pct for value in returns]
    net_excesses = [value - cost_pct for value in excesses]
    n = len(rows)
    return {
        "sample_count": n,
        "required_sample_count": P0_REQUIRED_SAMPLES,
        "win_rate": sum(1 for value in returns if value > 0) / n if n else 0.0,
        "avg_return_5d": sum(returns) / n if n else 0.0,
        "net_win_rate": sum(1 for value in net_returns if value > 0) / n if n else 0.0,
        "avg_net_return_5d": sum(net_returns) / n if n else 0.0,
        "excess_win_rate": sum(1 for value in excesses if value > 0) / n if n else 0.0,
        "avg_excess_5d": sum(excesses) / n if n else 0.0,
        "net_excess_win_rate": sum(1 for value in net_excesses if value > 0) / n if n else 0.0,
        "avg_net_excess_5d": sum(net_excesses) / n if n else 0.0,
        "status": _status("samples", n >= P0_REQUIRED_SAMPLES if n else None),
    }


def _pending_state(conn, current_version: str = "") -> dict[str, Any]:
    placeholders = ",".join("?" for _ in BUY_ACTIONS)
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS pending, MIN(date) AS min_date, MAX(date) AS max_date
        FROM signal_log
        WHERE action IN ({placeholders})
          AND COALESCE(settlement_status, 'pending') IN ('', 'pending', 'unknown')
        """,
        BUY_ACTIONS,
    ).fetchone()
    by_status = conn.execute(
        f"""
        SELECT COALESCE(settlement_status, 'pending') AS status, COUNT(*) AS count
        FROM signal_log
        WHERE action IN ({placeholders})
        GROUP BY COALESCE(settlement_status, 'pending')
        ORDER BY count DESC
        """,
        BUY_ACTIONS,
    ).fetchall()
    by_version = conn.execute(
        f"""
        SELECT COALESCE(NULLIF(strategy_version, ''), 'legacy_unversioned') AS version,
               COUNT(*) AS count
        FROM signal_log
        WHERE action IN ({placeholders})
          AND COALESCE(settlement_status, 'pending') IN ('', 'pending', 'unknown')
        GROUP BY COALESCE(NULLIF(strategy_version, ''), 'legacy_unversioned')
        ORDER BY count DESC
        """,
        BUY_ACTIONS,
    ).fetchall()
    by_strategy_version = {item["version"]: item["count"] for item in by_version}
    return {
        "pending": int(row["pending"] or 0) if row else 0,
        "min_date": row["min_date"] if row else None,
        "max_date": row["max_date"] if row else None,
        "by_status": {item["status"]: item["count"] for item in by_status},
        "current_strategy_version": current_version,
        "current_version_pending": int(by_strategy_version.get(current_version, 0)) if current_version else 0,
        "by_strategy_version": by_strategy_version,
    }


def _price_return(conn, code: str, start: str, end: str) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT date, close
        FROM price_history
        WHERE code=? AND date>=? AND date<=? AND close>0
        ORDER BY date ASC
        """,
        (code, start, end),
    ).fetchall()
    if len(rows) < 2:
        return {"code": code, "status": "missing", "points": len(rows), "return_pct": None}
    status = "ready"
    if _date_lag_days(start, rows[0]["date"]) > MAX_BENCHMARK_START_LAG_DAYS:
        status = "partial"
    first = float(rows[0]["close"])
    last = float(rows[-1]["close"])
    values = [float(row["close"]) for row in rows]
    drawdown = _drawdown_info([row["date"] for row in rows], values)
    return {
        "code": code,
        "status": status,
        "points": len(rows),
        "start": rows[0]["date"],
        "end": rows[-1]["date"],
        "return_pct": round(_pct(first, last) or 0.0, 2),
        **drawdown,
    }


def _price_rows(conn, code: str, start: str, end: str) -> list[tuple[str, float]]:
    rows = conn.execute(
        """
        SELECT date, close
        FROM price_history
        WHERE code=? AND date>=? AND date<=? AND close>0
        ORDER BY date ASC
        """,
        (code, start, end),
    ).fetchall()
    return [(row["date"], float(row["close"])) for row in rows]


def _last_close_at(rows: list[tuple[str, float]], target: str) -> Optional[float]:
    last = None
    for row_date, close in rows:
        if row_date > target:
            break
        last = close
    return last


def _equal_weight_benchmark(conn, name: str, codes: list[str], start: str, end: str) -> dict[str, Any]:
    used_rows: dict[str, list[tuple[str, float]]] = {}
    excluded: list[str] = []
    for code in codes:
        rows = _price_rows(conn, code, start, end)
        if len(rows) < 2 or _date_lag_days(start, rows[0][0]) > MAX_BENCHMARK_START_LAG_DAYS:
            excluded.append(code)
            continue
        used_rows[code] = rows
    if not used_rows:
        return {
            "name": name,
            "status": "missing",
            "return_pct": None,
            "max_drawdown_pct": None,
            "members": 0,
            "expected_members": len(codes),
            "excluded": excluded,
        }
    common_start = max(rows[0][0] for rows in used_rows.values())
    common_end = min(rows[-1][0] for rows in used_rows.values())
    dates = sorted({
        row_date
        for rows in used_rows.values()
        for row_date, _ in rows
        if common_start <= row_date <= common_end
    })
    values = []
    value_dates = []
    for row_date in dates:
        ratios = []
        for rows in used_rows.values():
            base = rows[0][1]
            close = _last_close_at(rows, row_date)
            if close is None:
                break
            ratios.append(close / base)
        if len(ratios) == len(used_rows):
            values.append(sum(ratios) / len(ratios))
            value_dates.append(row_date)
    if len(values) < 2:
        return {
            "name": name,
            "status": "missing",
            "return_pct": None,
            "max_drawdown_pct": None,
            "members": len(used_rows),
            "expected_members": len(codes),
            "excluded": excluded,
        }
    drawdown = _drawdown_info(value_dates, values)
    return {
        "name": name,
        "status": "ready" if len(used_rows) == len(codes) else "partial",
        "return_pct": round(_pct(values[0], values[-1]) or 0.0, 2),
        **drawdown,
        "start": value_dates[0],
        "end": value_dates[-1],
        "points": len(values),
        "members": len(used_rows),
        "expected_members": len(codes),
        "excluded": excluded,
    }


def _momentum_candidates(conn, codes: list[str], start: str) -> list[dict[str, Any]]:
    candidates = []
    for code in codes:
        rows = conn.execute(
            """
            SELECT date, close
            FROM price_history
            WHERE code=? AND date<? AND close>0
            ORDER BY date DESC
            LIMIT ?
            """,
            (code, start, MOMENTUM_LOOKBACK_POINTS + 1),
        ).fetchall()
        ordered = list(reversed(rows))
        if len(ordered) < 2:
            continue
        momentum = _pct(float(ordered[0]["close"]), float(ordered[-1]["close"]))
        if momentum is None:
            continue
        candidates.append({
            "code": code,
            "momentum_pct": round(momentum, 2),
            "points": len(ordered),
            "start": ordered[0]["date"],
            "end": ordered[-1]["date"],
        })
    return sorted(candidates, key=lambda item: item["momentum_pct"], reverse=True)


def _simple_momentum_benchmark(conn, codes: list[str], start: str, end: str) -> dict[str, Any]:
    candidates = _momentum_candidates(conn, codes, start)
    selected = candidates[:MOMENTUM_TOP_N]
    if not selected:
        return {
            "name": "simple_momentum_top3",
            "status": "missing",
            "return_pct": None,
            "max_drawdown_pct": None,
            "members": 0,
            "expected_members": MOMENTUM_TOP_N,
            "selected_codes": [],
        }
    result = _equal_weight_benchmark(
        conn,
        "simple_momentum_top3",
        [item["code"] for item in selected],
        start,
        end,
    )
    result["selected_codes"] = [item["code"] for item in selected]
    result["momentum_inputs"] = selected
    if len(selected) < MOMENTUM_TOP_N and result["status"] == "ready":
        result["status"] = "partial"
    return result


def _portfolio_from_rows(
    rows: list[Any],
    columns: set[str],
    *,
    source: str,
    required_points: int = 2,
    broker_candidate: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    if not rows:
        return {
            "status": "missing",
            "source": source,
            "return_pct": None,
            "max_drawdown_pct": None,
            "points": 0,
            "required_points": required_points,
            "broker_candidate": broker_candidate,
        }
    if len(rows) < required_points:
        return {
            "status": "insufficient",
            "source": source,
            "start": rows[0]["date"],
            "end": rows[-1]["date"],
            "points": len(rows),
            "required_points": required_points,
            "return_pct": None,
            "max_drawdown_pct": None,
            "quality": _nav_quality(rows, columns),
            "broker_candidate": broker_candidate,
        }
    values = [float(row["total_value"]) for row in rows]
    dates = [row["date"] for row in rows]
    initial = float(CAPITAL_CONFIG.get("initial_capital", values[0]) or values[0])
    start_value = values[0]
    drawdown = _drawdown_info(dates, values)
    return {
        "status": "ready",
        "source": source,
        "start": rows[0]["date"],
        "end": rows[-1]["date"],
        "points": len(rows),
        "required_points": required_points,
        "initial_capital": initial,
        "start_value": start_value,
        "latest_value": values[-1],
        "return_pct": round(_pct(start_value, values[-1]) or 0.0, 2),
        "since_initial_pct": round(_pct(initial, values[-1]) or 0.0, 2),
        **drawdown,
        "quality": _nav_quality(rows, columns),
        "broker_candidate": broker_candidate,
    }


def _broker_portfolio_state(conn) -> dict[str, Any]:
    if not _has_table(conn, "portfolio_reconciliations"):
        return {
            "status": "missing",
            "source": "broker_reconciled",
            "points": 0,
            "required_points": 2,
            "reason": "table_missing",
        }
    columns = _table_columns(conn, "portfolio_reconciliations")
    rows = conn.execute(
        """
        SELECT substr(snapshot_at, 1, 10) AS date,
               total_assets AS total_value,
               cash,
               holdings_value,
               positions_json
        FROM portfolio_reconciliations
        WHERE total_assets>0
        ORDER BY snapshot_at ASC, id ASC
        """
    ).fetchall()
    return _portfolio_from_rows(
        rows,
        columns,
        source="broker_reconciled",
        required_points=2,
    )


def _diagnostic_portfolio_state(
    conn,
    broker_candidate: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    if not _has_table(conn, "nav_history"):
        return {
            "status": "missing",
            "source": "diagnostic_nav_history",
            "points": 0,
            "required_points": 2,
            "broker_candidate": broker_candidate,
        }
    columns = _table_columns(conn, "nav_history")
    selected = ["date", "total_value"]
    for optional in ("cash", "holdings_value", "positions_json"):
        if optional in columns:
            selected.append(optional)
    rows = conn.execute(
        f"SELECT {', '.join(selected)} FROM nav_history WHERE total_value>0 ORDER BY date ASC"
    ).fetchall()
    return _portfolio_from_rows(
        rows,
        columns,
        source="diagnostic_nav_history",
        required_points=2,
        broker_candidate=broker_candidate,
    )


def _portfolio_state(conn) -> dict[str, Any]:
    broker = _broker_portfolio_state(conn)
    if broker.get("status") == "ready":
        return broker
    diagnostic = _diagnostic_portfolio_state(conn, broker_candidate=broker)
    diagnostic["source_note"] = (
        "broker_reconciled has insufficient points; using diagnostic nav_history"
        if broker.get("points", 0) > 0 else
        "broker_reconciled unavailable; using diagnostic nav_history"
    )
    return diagnostic


def _nav_quality(rows: list[Any], columns: set[str]) -> dict[str, Any]:
    legacy_position_rows = []
    negative_cash = []
    negative_holdings = []
    for row in rows:
        row_date = row["date"]
        if "positions_json" in columns:
            raw = row["positions_json"]
            if raw not in (None, "", "[]"):
                try:
                    parsed = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    parsed = None
                if not isinstance(parsed, list):
                    legacy_position_rows.append({"date": row_date, "value": raw})
        if "cash" in columns and row["cash"] is not None and float(row["cash"]) < 0:
            negative_cash.append({"date": row_date, "value": float(row["cash"])})
        if (
            "holdings_value" in columns
            and row["holdings_value"] is not None
            and float(row["holdings_value"]) < 0
        ):
            negative_holdings.append({"date": row_date, "value": float(row["holdings_value"])})
    status = "PASS"
    if negative_cash or negative_holdings:
        status = "BLOCK"
    return {
        "status": status,
        "invalid_position_rows": legacy_position_rows,
        "legacy_position_rows": legacy_position_rows,
        "negative_cash_rows": negative_cash,
        "negative_holdings_rows": negative_holdings,
    }


def _trade_quality(conn, start: Optional[str], end: Optional[str]) -> dict[str, Any]:
    columns = _table_columns(conn, "trades")
    required = {"date", "price", "quantity"}
    if not start or not end or not required.issubset(columns):
        return {"status": "PASS", "negative_trade_rows": [], "cost_basis_adjustment_rows": []}
    trade_amount_expr = "trade_amount" if "trade_amount" in columns else "0 AS trade_amount"
    note_expr = "note" if "note" in columns else "'' AS note"
    rows = conn.execute(
        f"""
        SELECT id, code, action, date, price, quantity, {trade_amount_expr}, {note_expr}
        FROM trades
        WHERE date>=? AND date<=?
          AND (price<0 OR quantity<0 OR {('trade_amount<0' if 'trade_amount' in columns else '0')})
        ORDER BY date, id
        """,
        (start, end),
    ).fetchall()
    invalid = []
    adjustments = []
    for row in rows:
        item = dict(row)
        note = str(item.get("note") or "")
        action = str(item.get("action") or "").lower()
        is_cost_basis_adjustment = (
            action == "buy"
            and float(item.get("price") or 0) < 0
            and "净成本" in note
            and "前期获利覆盖" in note
        )
        if is_cost_basis_adjustment:
            adjustments.append(item)
        else:
            invalid.append(item)
    return {
        "status": "BLOCK" if invalid else "PASS",
        "negative_trade_rows": invalid,
        "cost_basis_adjustment_rows": adjustments,
    }


def _data_quality_state(conn, portfolio: dict[str, Any]) -> dict[str, Any]:
    nav_quality = portfolio.get("quality", {"status": "PASS"})
    if portfolio.get("source") == "broker_reconciled":
        trade_quality = {
            "status": "SKIP",
            "negative_trade_rows": [],
            "reason": "broker-reconciled NAV does not depend on local trade ledger",
        }
    else:
        trade_quality = _trade_quality(conn, portfolio.get("start"), portfolio.get("end"))
    status = "PASS"
    if nav_quality.get("status") == "BLOCK" or trade_quality.get("status") == "BLOCK":
        status = "BLOCK"
    return {
        "status": status,
        "nav": nav_quality,
        "trades": trade_quality,
    }


def _benchmark_state(conn, portfolio: dict[str, Any]) -> dict[str, Any]:
    if portfolio.get("status") != "ready":
        return {
            "status": "missing",
            "benchmarks": [],
            "same_pool_excess_pct": None,
            "same_pool_drawdown_delta_pct": None,
        }
    start = portfolio["start"]
    end = portfolio["end"]
    benchmarks = [
        {"name": "HS300", **_price_return(conn, "000300", start, end)},
        {"name": "CSI500", **_price_return(conn, "000905", start, end)},
        {"name": "CSI1000 ETF", **_price_return(conn, "sh512100", start, end)},
        {"name": "AI theme ETF", **_price_return(conn, "sh515050", start, end)},
        {"name": "Semiconductor ETF", **_price_return(conn, "sh512480", start, end)},
        _equal_weight_benchmark(conn, "same_pool_equal_weight", list(ALL_CODES), start, end),
        _equal_weight_benchmark(conn, "tier_1_equal_weight", list(TIER_1_CODES), start, end),
        _equal_weight_benchmark(conn, "tier_2_equal_weight", list(TIER_2_CODES), start, end),
        _equal_weight_benchmark(conn, "high_dividend_defensive_equal_weight", list(TIER_4_CODES), start, end),
        _simple_momentum_benchmark(conn, list(ALL_CODES), start, end),
    ]
    ready = sum(1 for item in benchmarks if item.get("status") == "ready")
    same_pool = next((item for item in benchmarks if item["name"] == "same_pool_equal_weight"), {})
    same_pool_return = same_pool.get("return_pct")
    same_pool_drawdown = same_pool.get("max_drawdown_pct")
    excess = (
        round(float(portfolio["return_pct"]) - float(same_pool_return), 2)
        if same_pool.get("status") == "ready" and same_pool_return is not None else None
    )
    drawdown_delta = (
        round(float(portfolio["max_drawdown_pct"]) - float(same_pool_drawdown), 2)
        if same_pool.get("status") == "ready" and same_pool_drawdown is not None else None
    )
    return {
        "status": "ready" if ready == len(benchmarks) else "partial" if ready else "missing",
        "ready_count": ready,
        "total_count": len(benchmarks),
        "same_pool_excess_pct": excess,
        "same_pool_drawdown_delta_pct": drawdown_delta,
        "benchmarks": benchmarks,
    }


def _nav_row_for_date(conn, source: str, row_date: str) -> Optional[dict[str, Any]]:
    if not row_date:
        return None
    if source == "broker_reconciled" and _has_table(conn, "portfolio_reconciliations"):
        row = conn.execute(
            """
            SELECT substr(snapshot_at, 1, 10) AS date,
                   total_assets AS total_value,
                   cash,
                   holdings_value
            FROM portfolio_reconciliations
            WHERE substr(snapshot_at, 1, 10)=?
            ORDER BY snapshot_at DESC, id DESC
            LIMIT 1
            """,
            (row_date,),
        ).fetchone()
        return dict(row) if row else None
    if source == "diagnostic_nav_history" and _has_table(conn, "nav_history"):
        row = conn.execute(
            """
            SELECT date, total_value, cash, holdings_value
            FROM nav_history
            WHERE date=?
            ORDER BY date DESC
            LIMIT 1
            """,
            (row_date,),
        ).fetchone()
        return dict(row) if row else None
    return None


def _cashflow_reconciliation_for_window(
    conn,
    start: str,
    end: str,
    cashflow_gap: Optional[float],
    cashflow_tolerance: Optional[float],
) -> dict[str, Any]:
    if not start or not end or cashflow_gap is None:
        return {"status": "missing"}
    if not _has_table(conn, "cashflow_reconciliations"):
        return {"status": "missing"}

    row = conn.execute(
        """
        SELECT * FROM cashflow_reconciliations
        WHERE window_start=? AND window_end=?
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (start, end),
    ).fetchone()
    if not row:
        return {"status": "missing"}

    item = dict(row)
    stored_gap = float(item.get("unexplained_cash_effect") or 0)
    stored_tolerance = float(item.get("tolerance") or 0)
    remaining_gap = float(item.get("remaining_gap") or 0)
    effective_tolerance = max(float(cashflow_tolerance or 0), stored_tolerance, 1.0)
    gap_delta = round(stored_gap - float(cashflow_gap), 2)
    try:
        evidence_items = json.loads(item.get("evidence_items_json") or "[]")
    except (TypeError, json.JSONDecodeError):
        evidence_items = []
    status = (
        "verified"
        if abs(gap_delta) <= effective_tolerance and abs(remaining_gap) <= stored_tolerance
        else "mismatch"
    )
    return {
        "status": status,
        "window": f"{start}~{end}",
        "gap": round(stored_gap, 2),
        "current_gap": round(float(cashflow_gap), 2),
        "gap_delta": gap_delta,
        "tolerance": round(stored_tolerance, 2),
        "remaining_gap": round(remaining_gap, 2),
        "evidence_hash": item.get("evidence_hash", ""),
        "evidence_items": evidence_items,
        "created_at": item.get("created_at", ""),
    }


def _drawdown_attribution(conn, portfolio: dict[str, Any]) -> dict[str, Any]:
    start = portfolio.get("max_drawdown_start")
    end = portfolio.get("max_drawdown_end")
    source = portfolio.get("source", "")
    if not start or not end:
        return {"status": "missing", "reason": "drawdown_window_unavailable"}

    start_row = _nav_row_for_date(conn, source, start)
    end_row = _nav_row_for_date(conn, source, end)
    if not start_row or not end_row:
        return {"status": "missing", "reason": "nav_window_rows_unavailable"}

    def number(row: dict[str, Any], key: str) -> Optional[float]:
        value = row.get(key)
        return float(value) if value is not None else None

    start_total = number(start_row, "total_value")
    end_total = number(end_row, "total_value")
    start_cash = number(start_row, "cash")
    end_cash = number(end_row, "cash")
    start_holdings = number(start_row, "holdings_value")
    end_holdings = number(end_row, "holdings_value")

    trades: list[dict[str, Any]] = []
    buy_amount = 0.0
    sell_amount = 0.0
    if _has_table(conn, "trades"):
        columns = _table_columns(conn, "trades")
        trade_amount_expr = "trade_amount" if "trade_amount" in columns else "0 AS trade_amount"
        note_expr = "note" if "note" in columns else "'' AS note"
        rows = conn.execute(
            f"""
            SELECT id, code, action, date, price, quantity, {trade_amount_expr}, {note_expr}
            FROM trades
            WHERE date>=? AND date<=?
            ORDER BY date, id
            """,
            (start, end),
        ).fetchall()
        for row in rows:
            item = dict(row)
            amount = float(item.get("trade_amount") or 0)
            if amount == 0 and item.get("price") is not None and item.get("quantity") is not None:
                amount = float(item["price"]) * float(item["quantity"])
            item["amount"] = round(amount, 2)
            if str(item.get("action") or "").lower() == "buy":
                buy_amount += amount
            elif str(item.get("action") or "").lower() == "sell":
                sell_amount += amount
            trades.append(item)

    net_trade_cashflow = round(sell_amount - buy_amount, 2)
    cashflow_gap = None
    cashflow_tolerance = None
    cashflow_reconciled = None
    if cash_change := (None if start_cash is None or end_cash is None else end_cash - start_cash):
        cashflow_gap = round(cash_change - net_trade_cashflow, 2)
        cashflow_tolerance = round(max(50.0, abs(start_total or 0) * 0.005), 2)
        cashflow_reconciled = abs(cashflow_gap) <= cashflow_tolerance
    elif start_cash is not None and end_cash is not None:
        cashflow_gap = round(0.0 - net_trade_cashflow, 2)
        cashflow_tolerance = round(max(50.0, abs(start_total or 0) * 0.005), 2)
        cashflow_reconciled = abs(cashflow_gap) <= cashflow_tolerance
    cashflow_evidence = _cashflow_reconciliation_for_window(
        conn,
        start,
        end,
        cashflow_gap,
        cashflow_tolerance,
    )

    return {
        "status": "ready",
        "source": source,
        "start": start,
        "end": end,
        "start_total": None if start_total is None else round(start_total, 2),
        "end_total": None if end_total is None else round(end_total, 2),
        "start_cash": None if start_cash is None else round(start_cash, 2),
        "end_cash": None if end_cash is None else round(end_cash, 2),
        "start_holdings": None if start_holdings is None else round(start_holdings, 2),
        "end_holdings": None if end_holdings is None else round(end_holdings, 2),
        "total_change": None if start_total is None or end_total is None else round(end_total - start_total, 2),
        "total_change_pct": None if start_total in (None, 0) or end_total is None else round(_pct(start_total, end_total) or 0.0, 2),
        "cash_change": None if start_cash is None or end_cash is None else round(end_cash - start_cash, 2),
        "holdings_change": None if start_holdings is None or end_holdings is None else round(end_holdings - start_holdings, 2),
        "buy_amount": round(buy_amount, 2),
        "sell_amount": round(sell_amount, 2),
        "net_trade_cashflow": net_trade_cashflow,
        "cashflow_gap": cashflow_gap,
        "cashflow_tolerance": cashflow_tolerance,
        "cashflow_reconciled": cashflow_reconciled,
        "cashflow_evidence": cashflow_evidence,
        "trades": trades,
    }


def _apply_drawdown_cashflow_quality(
    data_quality: dict[str, Any],
    drawdown_attribution: dict[str, Any],
) -> None:
    if drawdown_attribution.get("source") == "broker_reconciled":
        data_quality["drawdown_cashflow"] = {
            "status": "SKIP",
            "reason": "broker-reconciled NAV does not depend on local trade ledger",
        }
        return
    reconciled = drawdown_attribution.get("cashflow_reconciled")
    if reconciled is None:
        data_quality["drawdown_cashflow"] = {"status": "INSUFFICIENT"}
        return
    evidence = drawdown_attribution.get("cashflow_evidence") or {}
    evidence_verified = evidence.get("status") == "verified"
    status = "PASS" if reconciled or evidence_verified else "BLOCK"
    data_quality["drawdown_cashflow"] = {
        "status": status,
        "gap": drawdown_attribution.get("cashflow_gap"),
        "tolerance": drawdown_attribution.get("cashflow_tolerance"),
        "window": f"{drawdown_attribution.get('start')}~{drawdown_attribution.get('end')}",
        "evidence": evidence,
    }
    if evidence_verified and not reconciled:
        data_quality["drawdown_cashflow"]["reason"] = "external_cashflow_evidence_verified"
    if status == "BLOCK":
        data_quality["status"] = "BLOCK"


def _oos_state(rows: list[dict[str, Any]], cost_pct: float) -> dict[str, Any]:
    windows: dict[str, list[float]] = {}
    for row in rows:
        date_key = (row.get("exit_date") or row.get("date") or "")[:7]
        if not date_key:
            continue
        windows.setdefault(date_key, []).append(float(row["excess_5d"]) - cost_pct)
    details = []
    for key in sorted(windows):
        vals = windows[key]
        details.append({
            "window": key,
            "samples": len(vals),
            "avg_net_excess_5d": round(sum(vals) / len(vals), 2),
            "positive": sum(vals) / len(vals) > 0,
        })
    positive = sum(1 for item in details if item["positive"])
    enough = len(details) >= P0_REQUIRED_OOS_WINDOWS
    return {
        "window_count": len(details),
        "required_windows": P0_REQUIRED_OOS_WINDOWS,
        "positive_windows": positive,
        "required_positive_windows": P0_REQUIRED_POSITIVE_WINDOWS,
        "status": _status(
            "oos",
            (positive >= P0_REQUIRED_POSITIVE_WINDOWS and enough) if details else None,
        ),
        "windows": details,
    }


def _concentration_state(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"status": "INSUFFICIENT", "top_code": None, "top_share": None}
    by_code: dict[str, float] = {}
    for row in rows:
        by_code[row["code"]] = by_code.get(row["code"], 0.0) + float(row["excess_5d"])
    gross_abs = sum(abs(value) for value in by_code.values())
    if gross_abs <= 0:
        return {"status": "INSUFFICIENT", "top_code": None, "top_share": None}
    top_code, top_value = max(by_code.items(), key=lambda item: abs(item[1]))
    top_share = abs(top_value) / gross_abs
    return {
        "status": "PASS" if top_share <= 0.50 else "BLOCK",
        "top_code": top_code,
        "top_share": round(top_share, 3),
        "contributions": {code: round(value, 2) for code, value in sorted(by_code.items())},
    }


def build_alpha_validation_report(conn=None) -> dict[str, Any]:
    """Build a read-only P0 alpha validation report."""
    should_close = conn is None
    conn = conn or get_conn()
    try:
        strategy = _active_strategy(conn)
        rows = _buy_sample_rows(conn, strategy.get("version", ""))
        cost_model = _cost_model()
        cost_pct = float(cost_model["round_trip_pct"])
        samples = _sample_state(rows, cost_pct)
        pending = _pending_state(conn, strategy.get("version", ""))
        portfolio = _portfolio_state(conn)
        data_quality = _data_quality_state(conn, portfolio)
        benchmarks = _benchmark_state(conn, portfolio)
        drawdown_attribution = _drawdown_attribution(conn, portfolio)
        _apply_drawdown_cashflow_quality(data_quality, drawdown_attribution)
        oos = _oos_state(rows, cost_pct)
        concentration = _concentration_state(rows)
        nav_quality_ok = data_quality["status"] == "PASS"

        same_pool_excess = benchmarks.get("same_pool_excess_pct")
        same_pool_status = _status(
            "same_pool",
            (same_pool_excess > 0) if nav_quality_ok and same_pool_excess is not None else None,
        )
        same_pool_drawdown_delta = benchmarks.get("same_pool_drawdown_delta_pct")
        same_pool_drawdown_status = _status(
            "same_pool_drawdown",
            (
                same_pool_drawdown_delta >= 0
            ) if nav_quality_ok and same_pool_drawdown_delta is not None else None,
        )
        cost_after_status = _status(
            "cost_after",
            (samples["avg_net_excess_5d"] > 0) if samples["sample_count"] else None,
        )
        benchmark_coverage_status = (
            "PASS" if benchmarks.get("ready_count", 0) == benchmarks.get("total_count", 0)
            else "WATCH" if benchmarks.get("ready_count", 0) > 0
            else "INSUFFICIENT"
        )

        criteria = [
            {
                "key": "strategy_freeze",
                "label": "active strategy hash matches code config",
                "status": "PASS" if strategy.get("hash_matches_current") else "WATCH",
            },
            {
                "key": "nav_data_quality",
                "label": "NAV and trade records are structurally valid for benchmark comparison",
                "status": data_quality["status"],
            },
            {
                "key": "real_samples",
                "label": "current-version real executable BUY samples >= 50",
                "status": samples["status"],
            },
            {
                "key": "benchmark_coverage",
                "label": "benchmark coverage for same-pool/theme/index comparison",
                "status": benchmark_coverage_status,
            },
            {
                "key": "same_pool_excess",
                "label": "portfolio beats same-pool equal weight over NAV window",
                "status": same_pool_status,
            },
            {
                "key": "same_pool_drawdown",
                "label": "portfolio max drawdown is not worse than same-pool equal weight",
                "status": same_pool_drawdown_status,
            },
            {
                "key": "cost_after_edge",
                "label": "current-version BUY excess remains positive after realistic costs",
                "status": cost_after_status,
            },
            {
                "key": "oos_windows",
                "label": "at least 2 of 3 independent OOS windows positive net excess",
                "status": oos["status"],
            },
            {
                "key": "single_name_concentration",
                "label": "alpha not dominated by a single stock >50%",
                "status": concentration["status"],
            },
        ]
        pass_count = sum(1 for item in criteria if item["status"] == "PASS")
        block_count = sum(1 for item in criteria if item["status"] == "BLOCK")
        insufficient_count = sum(1 for item in criteria if item["status"] == "INSUFFICIENT")
        verdict = (
            "P0_DATA_INVALID"
            if any(item["key"] == "nav_data_quality" and item["status"] == "BLOCK" for item in criteria)
            else
            "P0_FAIL"
            if block_count
            else "P0_NOT_PROVEN"
            if insufficient_count or pass_count < len(criteria)
            else "P0_PASS"
        )

        return {
            "title": "Serenity P0 Alpha Validation",
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "verdict": verdict,
            "strategy": strategy,
            "cost_model": cost_model,
            "data_quality": data_quality,
            "samples": samples,
            "pending": pending,
            "portfolio": portfolio,
            "benchmarks": benchmarks,
            "drawdown_attribution": drawdown_attribution,
            "oos": oos,
            "concentration": concentration,
            "criteria": criteria,
        }
    finally:
        if should_close:
            conn.close()


def format_alpha_validation_report(report: Optional[dict[str, Any]] = None) -> str:
    report = report or build_alpha_validation_report()
    strategy = report["strategy"]
    samples = report["samples"]
    portfolio = report["portfolio"]
    benchmarks = report["benchmarks"]
    oos = report["oos"]
    concentration = report["concentration"]
    cost_model = report["cost_model"]
    data_quality = report["data_quality"]
    drawdown_attr = report["drawdown_attribution"]

    lines = [
        report["title"],
        "=" * 72,
        f"generated_at: {report['generated_at']}",
        f"verdict: {report['verdict']}",
        "",
        "Strategy",
        f"- active_version: {strategy.get('version') or '-'}",
        f"- hash_matches_current: {strategy.get('hash_matches_current')}",
        f"- created_at: {strategy.get('created_at') or '-'}",
        "",
        "Cost Model",
        f"- round_trip_cost: {cost_model['round_trip_pct']:.3f}%",
        f"- commission: {cost_model['commission_pct']:.3f}% | "
        f"stamp_tax: {cost_model['stamp_tax_pct']:.3f}% | "
        f"transfer_fee: {cost_model['transfer_fee_pct']:.3f}% | "
        f"slippage: {cost_model['slippage_pct']:.3f}%",
        "",
        "Data Quality",
        f"- status: {data_quality['status']}",
        f"- nav_source: {portfolio.get('source', '-')}",
        f"- nav_source_note: {portfolio.get('source_note', '-')}",
        f"- broker_reconciled_points: "
        f"{(portfolio.get('broker_candidate') or {}).get('points', portfolio.get('points', 0))}/"
        f"{(portfolio.get('broker_candidate') or {}).get('required_points', portfolio.get('required_points', 2))}",
        f"- trade_ledger_check: {data_quality['trades'].get('status')}",
        f"- drawdown_cashflow_check: {data_quality.get('drawdown_cashflow', {}).get('status', '-')}",
        f"- drawdown_cashflow_gap: {data_quality.get('drawdown_cashflow', {}).get('gap', '-')}",
        f"- drawdown_cashflow_tolerance: {data_quality.get('drawdown_cashflow', {}).get('tolerance', '-')}",
        f"- legacy_nav_position_rows: "
        f"{len(data_quality['nav'].get('legacy_position_rows', []))}",
        f"- negative_trade_rows: "
        f"{len(data_quality['trades'].get('negative_trade_rows', []))}",
        f"- cost_basis_adjustment_rows: "
        f"{len(data_quality['trades'].get('cost_basis_adjustment_rows', []))}",
        "",
        "Real BUY Samples",
        f"- samples: {samples['sample_count']}/{samples['required_sample_count']} ({samples['status']})",
        f"- win_rate: {samples['win_rate']:.1%}",
        f"- avg_return_5d: {samples['avg_return_5d']:+.2f}%",
        f"- net_win_rate: {samples['net_win_rate']:.1%}",
        f"- avg_net_return_5d: {samples['avg_net_return_5d']:+.2f}%",
        f"- excess_win_rate: {samples['excess_win_rate']:.1%}",
        f"- avg_excess_5d: {samples['avg_excess_5d']:+.2f}%",
        f"- net_excess_win_rate: {samples['net_excess_win_rate']:.1%}",
        f"- avg_net_excess_5d: {samples['avg_net_excess_5d']:+.2f}%",
        f"- pending_buy_signals: {report['pending']['pending']} "
        f"({report['pending'].get('min_date') or '-'} ~ {report['pending'].get('max_date') or '-'})",
        f"- current_version_pending: {report['pending'].get('current_version_pending', 0)} "
        f"({report['pending'].get('current_strategy_version') or '-'})",
        f"- pending_by_strategy_version: {report['pending'].get('by_strategy_version') or {}}",
        "",
        "Portfolio vs Benchmarks",
    ]
    if portfolio.get("status") == "ready":
        lines.extend([
            f"- nav_window: {portfolio['start']} ~ {portfolio['end']} ({portfolio['points']} points)",
            f"- portfolio_return_window: {portfolio['return_pct']:+.2f}%",
            f"- portfolio_return_since_initial: {portfolio.get('since_initial_pct', 0):+.2f}%",
            f"- portfolio_max_drawdown: {portfolio['max_drawdown_pct']}% "
            f"({portfolio.get('max_drawdown_start') or '-'} ~ {portfolio.get('max_drawdown_end') or '-'})",
            f"- same_pool_excess: {benchmarks.get('same_pool_excess_pct')}",
            f"- same_pool_drawdown_delta: {benchmarks.get('same_pool_drawdown_delta_pct')}",
        ])
    else:
        lines.append("- nav_history missing")

    legacy_nav = data_quality["nav"].get("legacy_position_rows", [])
    negative_trades = data_quality["trades"].get("negative_trade_rows", [])
    cost_basis_adjustments = data_quality["trades"].get("cost_basis_adjustment_rows", [])
    if legacy_nav:
        lines.append("- legacy NAV position rows:")
        for item in legacy_nav[:6]:
            lines.append(f"  - {item['date']}: positions_json={item['value']!r}")
    if negative_trades:
        lines.append("- invalid trade rows:")
        for item in negative_trades[:6]:
            lines.append(
                f"  - #{item['id']} {item['date']} {item['action']} "
                f"{item['code']} price={item['price']} qty={item['quantity']} "
                f"amount={item.get('trade_amount')}"
            )
    if cost_basis_adjustments:
        lines.append("- cost-basis adjustment rows:")
        for item in cost_basis_adjustments[:6]:
            lines.append(
                f"  - #{item['id']} {item['date']} {item['action']} "
                f"{item['code']} price={item['price']} qty={item['quantity']} "
                f"amount={item.get('trade_amount')}"
            )

    if drawdown_attr.get("status") == "ready":
        def signed(value: Optional[float]) -> str:
            return "N/A" if value is None else f"{value:+.2f}"

        lines.extend([
            "- drawdown attribution:",
            f"  - window: {drawdown_attr['start']} ~ {drawdown_attr['end']} "
            f"({drawdown_attr.get('source')})",
            f"  - total_change: {signed(drawdown_attr['total_change'])} "
            f"({signed(drawdown_attr['total_change_pct'])}%)",
            f"  - cash_change: {signed(drawdown_attr['cash_change'])} | "
            f"holdings_change: {signed(drawdown_attr['holdings_change'])}",
            f"  - buys: {drawdown_attr['buy_amount']:+.2f} | "
            f"sells: {drawdown_attr['sell_amount']:+.2f} | "
            f"net_trade_cashflow: {drawdown_attr['net_trade_cashflow']:+.2f}",
            f"  - cashflow_gap: {signed(drawdown_attr.get('cashflow_gap'))} | "
            f"tolerance: {signed(drawdown_attr.get('cashflow_tolerance'))} | "
            f"reconciled: {drawdown_attr.get('cashflow_reconciled')}",
        ])
        for item in drawdown_attr.get("trades", [])[:8]:
            lines.append(
                f"    - #{item['id']} {item['date']} {item['action']} "
                f"{item['code']} amount={item['amount']:+.2f} "
                f"price={item['price']} qty={item['quantity']}"
            )

    lines.append("- benchmark coverage:")
    for item in benchmarks.get("benchmarks", []):
        ret = item.get("return_pct")
        ret_text = "N/A" if ret is None else f"{ret:+.2f}%"
        extra = ""
        if "members" in item:
            extra = f" members={item['members']}/{item['expected_members']}"
        else:
            extra = f" points={item.get('points', 0)}"
        dd = item.get("max_drawdown_pct")
        dd_text = "" if dd is None else f" dd={dd:+.2f}%"
        dd_window = ""
        if item.get("max_drawdown_start") or item.get("max_drawdown_end"):
            dd_window = (
                f" dd_window={item.get('max_drawdown_start') or '-'}"
                f"~{item.get('max_drawdown_end') or '-'}"
            )
        selected = ""
        if item.get("selected_codes"):
            selected = f" selected={','.join(item['selected_codes'])}"
        excluded = ""
        if item.get("excluded"):
            shown = ",".join(item["excluded"][:8])
            suffix = "" if len(item["excluded"]) <= 8 else f"...(+{len(item['excluded']) - 8})"
            excluded = f" excluded={shown}{suffix}"
        lines.append(
            f"  - {item['name']}: {item.get('status')} "
            f"{ret_text}{dd_text}{dd_window}{extra}{selected}{excluded}"
        )

    lines.extend([
        "",
        "OOS Windows",
        f"- windows: {oos['window_count']}/{oos['required_windows']} ({oos['status']})",
        f"- positive_windows: {oos['positive_windows']}/{oos['required_positive_windows']}",
    ])
    for item in oos.get("windows", []):
        lines.append(
            f"  - {item['window']}: samples={item['samples']} "
            f"avg_net_excess={item['avg_net_excess_5d']:+.2f}%"
        )

    lines.extend([
        "",
        "Single Name Concentration",
        f"- status: {concentration['status']}",
        f"- top_code: {concentration.get('top_code') or '-'}",
        f"- top_share_abs_excess: {concentration.get('top_share')}",
        "",
        "P0 Criteria",
    ])
    for item in report["criteria"]:
        lines.append(f"- [{item['status']}] {item['label']}")

    lines.extend(["", "Next"])
    lines.append("- Keep production expansion frozen until the P0 verdict becomes P0_PASS.")
    lines.append("- Accumulate current-version settled executable samples before increasing automation.")
    broker_candidate = portfolio.get("broker_candidate") or {}
    broker_points = int(broker_candidate.get("points", portfolio.get("points", 0)) or 0)
    broker_required = int(broker_candidate.get("required_points", portfolio.get("required_points", 2)) or 2)
    if broker_points < broker_required:
        lines.append("- Run `python3 cli.py broker-snapshot-template` and import a fresh broker snapshot.")
    if data_quality["status"] == "BLOCK":
        lines.append("- Repair invalid NAV/trade rows before using portfolio-vs-benchmark evidence.")
    if any(item.get("status") in {"partial", "missing"} for item in benchmarks.get("benchmarks", [])):
        lines.append("- Backfill incomplete benchmark members before trusting same-pool comparisons.")
    if any(item["key"] == "same_pool_drawdown" and item["status"] == "BLOCK" for item in report["criteria"]):
        lines.append("- Investigate the portfolio drawdown window before increasing position aggressiveness.")
    lines.append("- Compare every future validation against same-pool equal weight, not only broad indices.")
    return "\n".join(lines)


def cmd_alpha_validation() -> None:
    print(format_alpha_validation_report())


if __name__ == "__main__":
    cmd_alpha_validation()
