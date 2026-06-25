"""Real-data gate, compliance state, and controlled execution guard."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any

from config import ALL_CODES, STOCK_MAP


SIGNAL_OUTCOME_EXPIRY_TRADING_DAYS = 15
"""Unsettled signal samples expire after this many trading days."""

MAX_HOLDING_TRADING_DAYS = 20
"""Live positions must be reviewed/exited after this many trading days."""

COMPLIANCE_STATUSES = (
    "not_reported",
    "reported_pending_review",
    "approved",
    "rejected",
)

ORDER_STATES = (
    "generated",
    "pending_confirm",
    "confirmed",
    "submitted",
    "partial_filled",
    "filled",
    "cancelled",
    "rejected",
    "expired",
)

BUY_ACTIONS = ("STRONG_BUY", "BUY", "CAUTION_BUY")
BENCHMARK_BY_TIER = {
    1: {"code": "000905", "name": "CSI500"},
    2: {"code": "000905", "name": "CSI500"},
    3: {"code": "000905", "name": "CSI500"},
    4: {"code": "000300", "name": "HS300"},
}

CONSECUTIVE_LOSS_RULE = {
    "mode": "OR",
    "lookback": 10,
    "max_consecutive": 3,
}


def default_strategy_config() -> dict[str, Any]:
    """Return every rule that can change whether old samples are reusable."""
    return {
        "strategy_family": "serenity_real_data_gate",
        "version_schema": 1,
        "tradable_universe_prefixes": ["000", "002", "600", "601", "603", "605"],
        "buy_actions": list(BUY_ACTIONS),
        "sample_size": 50,
        "min_point_win_rate": 0.60,
        "min_wilson_lower": 0.50,
        "min_avg_return_5d": 0.0,
        "min_excess_win_rate": 0.55,
        "min_avg_excess_5d": 0.0,
        "outcome_rule": {
            "entry": "T+1 open",
            "exit": "T+6 open",
            "expiry_trading_days": SIGNAL_OUTCOME_EXPIRY_TRADING_DAYS,
        },
        "benchmark_rule": {
            "tier_1_to_3": "CSI500",
            "tier_4": "HS300",
            "same_interval_as_stock": True,
        },
        "executable_sample_filters": [
            "settlement_status=settled",
            "executable_status=executable",
            "data_quality=high",
            "adjustment_mode=raw",
            "current_major_strategy_version",
        ],
        "data_source_priority": ["tencent", "sina", "akshare"],
        "data_conflict_pct": 0.01,
        "consecutive_loss_rule": dict(CONSECUTIVE_LOSS_RULE),
        "risk_rules": {
            "max_buy_orders_per_day": 1,
            "max_single_auto_position_pct": 0.20,
            "max_auto_pool_pct": 0.30,
            "stop_loss_pct": -0.06,
            "daily_loss_lock_pct": -0.02,
            "drawdown_lock_pct": -0.06,
            "max_holding_trading_days": MAX_HOLDING_TRADING_DAYS,
        },
        "order_states": list(ORDER_STATES),
        "compliance_gate": "SEMI_AUTO requires compliance_status.status == approved",
    }


def compute_strategy_hash(config: dict[str, Any] | None = None) -> str:
    payload = json.dumps(config or default_strategy_config(), sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def ensure_current_strategy_version(reset_reason: str = "auto hash check") -> dict[str, Any]:
    """Ensure strategy_versions has an active row for the current hash."""
    import db

    db.init_db()
    config = default_strategy_config()
    config_hash = compute_strategy_hash(config)
    conn = db.get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM strategy_versions WHERE config_hash=?",
            (config_hash,),
        ).fetchone()
        if row:
            conn.execute("UPDATE strategy_versions SET is_active=0 WHERE config_hash<>?", (config_hash,))
            conn.execute("UPDATE strategy_versions SET is_active=1 WHERE config_hash=?", (config_hash,))
            conn.commit()
            return dict(row)

        last = conn.execute("SELECT COALESCE(MAX(major), 0) AS m FROM strategy_versions").fetchone()
        major = int(last["m"] or 0) + 1
        version = f"v{major}.0"
        conn.execute("UPDATE strategy_versions SET is_active=0")
        conn.execute(
            """
            INSERT INTO strategy_versions
                (version, major, minor, config_hash, config_json, reset_reason, is_active)
            VALUES (?, ?, 0, ?, ?, ?, 1)
            """,
            (version, major, config_hash, json.dumps(config, ensure_ascii=False), reset_reason),
        )
        conn.commit()
        return {
            "version": version,
            "major": major,
            "minor": 0,
            "config_hash": config_hash,
            "config_json": json.dumps(config, ensure_ascii=False),
            "reset_reason": reset_reason,
            "is_active": 1,
        }
    finally:
        conn.close()


def get_current_strategy_version() -> dict[str, Any]:
    return ensure_current_strategy_version()


def wilson_lower_bound(wins: int, n: int, z: float = 1.96) -> float:
    if n <= 0:
        return 0.0
    phat = wins / n
    denom = 1 + z * z / n
    centre = phat + z * z / (2 * n)
    margin = z * math.sqrt((phat * (1 - phat) + z * z / (4 * n)) / n)
    return max(0.0, (centre - margin) / denom)


def is_trading_day(d: date) -> bool:
    from check_trading_day import is_trading_day as _is_trading_day
    return _is_trading_day(d)


def add_trading_days(date_str: str, days: int) -> str:
    d = date.fromisoformat(date_str)
    step = 1 if days >= 0 else -1
    remaining = abs(days)
    while remaining:
        d += timedelta(days=step)
        if is_trading_day(d):
            remaining -= 1
    return d.isoformat()


def trading_days_between(start: str, end: str) -> int:
    s = date.fromisoformat(start)
    e = date.fromisoformat(end)
    if e <= s:
        return 0
    count = 0
    d = s
    while d < e:
        d += timedelta(days=1)
        if is_trading_day(d):
            count += 1
    return count


def classify_backtest_price_source(adjustment_mode: str | None) -> str:
    mode = (adjustment_mode or "").lower()
    if mode in ("raw", "unadjusted"):
        return "gate_eligible"
    return "diagnostic_only"


def _fetch_gate_samples(conn, version: str, limit: int) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in BUY_ACTIONS)
    rows = conn.execute(
        f"""
        SELECT id, code, date, action, return_5d, outcome_5d,
               benchmark_return_5d, excess_5d, data_quality, adjustment_mode
        FROM signal_log
        WHERE action IN ({placeholders})
          AND strategy_version=?
          AND settlement_status='settled'
          AND executable_status='executable'
          AND data_quality='high'
          AND adjustment_mode IN ('raw', 'unadjusted')
          AND COALESCE(return_5d, outcome_5d) IS NOT NULL
          AND excess_5d IS NOT NULL
        ORDER BY date DESC, id DESC
        LIMIT ?
        """,
        (*BUY_ACTIONS, version, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def _find_consecutive_loss(samples_newest_first: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rule = CONSECUTIVE_LOSS_RULE
    max_consecutive = int(rule["max_consecutive"])
    lookback = int(rule["lookback"])
    mode = rule["mode"].upper()
    run: list[dict[str, Any]] = []
    for sample in samples_newest_first[:lookback]:
        ret = float(sample.get("return_5d") if sample.get("return_5d") is not None else sample.get("outcome_5d") or 0)
        excess = float(sample.get("excess_5d") or 0)
        bad = (ret < 0 and excess < 0) if mode == "AND" else (ret < 0 or excess < 0)
        if bad:
            run.append(sample)
            if len(run) >= max_consecutive:
                return run[-max_consecutive:]
        else:
            run = []
    return []


def _risk_lock_state(conn) -> tuple[bool, list[str]]:
    try:
        rows = conn.execute(
            "SELECT date, total_value, profit_pct FROM nav_history ORDER BY date DESC LIMIT 80"
        ).fetchall()
    except Exception:
        return False, []
    values = [dict(r) for r in rows if r["total_value"] is not None]
    reasons: list[str] = []
    if len(values) >= 2:
        latest, previous = values[0], values[1]
        prev_value = float(previous["total_value"] or 0)
        latest_value = float(latest["total_value"] or 0)
        if prev_value > 0:
            daily = (latest_value - prev_value) / prev_value
            if daily <= -0.02:
                reasons.append(f"daily_loss {daily * 100:.2f}% <= -2.00%")
    if values:
        latest_value = float(values[0]["total_value"] or 0)
        peak = max(float(v["total_value"] or 0) for v in values)
        if peak > 0:
            drawdown = (latest_value - peak) / peak
            if drawdown <= -0.06:
                reasons.append(f"drawdown {drawdown * 100:.2f}% <= -6.00%")
    return bool(reasons), reasons


def _get_compliance_status(conn) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM compliance_status WHERE id=1").fetchone()
    if not row:
        return {"status": "not_reported", "broker": "", "notes": ""}
    return dict(row)


def _date_distribution(samples: list[dict[str, Any]]) -> dict[str, int]:
    return dict(Counter(s["date"] for s in samples))


def _sector_distribution(samples: list[dict[str, Any]]) -> dict[str, int]:
    sectors: Counter[str] = Counter()
    for s in samples:
        info = STOCK_MAP.get(s["code"], {})
        sectors[str(info.get("tier", "unknown"))] += 1
    return dict(sectors)


def evaluate_auto_gate(explain: bool = False) -> dict[str, Any]:
    import db

    db.init_db()
    version_row = ensure_current_strategy_version()
    version = version_row["version"]
    config = default_strategy_config()
    required = int(config["sample_size"])
    conn = db.get_conn()
    try:
        samples = _fetch_gate_samples(conn, version, required)
        sample_count = len(samples)
        returns = [
            float(s.get("return_5d") if s.get("return_5d") is not None else s.get("outcome_5d"))
            for s in samples
        ]
        excesses = [float(s["excess_5d"]) for s in samples]
        wins = sum(1 for r in returns if r > 0)
        excess_wins = sum(1 for r in excesses if r > 0)
        win_rate = wins / sample_count if sample_count else 0.0
        excess_win_rate = excess_wins / sample_count if sample_count else 0.0
        wilson = wilson_lower_bound(wins, sample_count)
        avg_return = sum(returns) / sample_count if sample_count else 0.0
        avg_excess = sum(excesses) / sample_count if sample_count else 0.0
        consecutive_trigger = _find_consecutive_loss(samples)
        consecutive_ok = not consecutive_trigger
        risk_locked, risk_reasons = _risk_lock_state(conn)
        compliance = _get_compliance_status(conn)
        compliance_status = compliance.get("status", "not_reported")
        max_state = "SEMI_AUTO" if compliance_status == "approved" else "MANUAL"

        reasons: list[str] = []
        if sample_count < required:
            reasons.append(f"sample_count {sample_count} < {required}")
        if win_rate < config["min_point_win_rate"]:
            reasons.append(f"win_rate {win_rate:.2%} < 60.00%")
        if wilson < config["min_wilson_lower"]:
            reasons.append(f"wilson_lower {wilson:.2%} < 50.00%")
        if avg_return <= config["min_avg_return_5d"]:
            reasons.append(f"avg_return_5d {avg_return:.2f}% <= 0")
        if excess_win_rate < config["min_excess_win_rate"]:
            reasons.append(f"excess_win_rate {excess_win_rate:.2%} < 55.00%")
        if avg_excess <= config["min_avg_excess_5d"]:
            reasons.append(f"avg_excess_5d {avg_excess:.2f}% <= 0")
        if not consecutive_ok:
            reasons.append("latest samples contain 3 consecutive bad outcomes")
        if risk_locked:
            reasons.extend(risk_reasons)

        gate_passed = sample_count >= required and not reasons
        if risk_locked:
            state = "LOCKED"
        elif gate_passed:
            state = max_state
        else:
            state = "PAPER"

        result = {
            "date": date.today().isoformat(),
            "strategy_version": version,
            "strategy_hash": version_row["config_hash"],
            "gate_passed": gate_passed,
            "state": state,
            "max_state": max_state,
            "sample_count": sample_count,
            "required_sample_count": required,
            "win_rate": win_rate,
            "wilson_lower": wilson,
            "avg_return_5d": avg_return,
            "excess_win_rate": excess_win_rate,
            "avg_excess_5d": avg_excess,
            "consecutive_loss_ok": consecutive_ok,
            "consecutive_loss_trigger": consecutive_trigger,
            "compliance_status": compliance_status,
            "risk_locked": risk_locked,
            "reasons": reasons,
            "explain": {
                "date_distribution": _date_distribution(samples),
                "tier_distribution": _sector_distribution(samples),
                "latest_10": samples[:10],
                "consecutive_loss_rule": dict(CONSECUTIVE_LOSS_RULE),
                "wilson_note": "n=50 at 60% win rate does not clear the 50% Wilson lower-bound gate",
            } if explain else {},
        }
        _persist_gate_result(conn, result)
        return result
    finally:
        conn.close()


def _persist_gate_result(conn, result: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO auto_trade_gate
            (date, strategy_version, gate_status, state, sample_count,
             win_rate, wilson_lower, avg_return_5d, excess_win_rate,
             avg_excess_5d, consecutive_loss_ok, compliance_status,
             max_state, reasons_json, explain_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            result["date"],
            result["strategy_version"],
            "pass" if result["gate_passed"] else "blocked",
            result["state"],
            result["sample_count"],
            result["win_rate"],
            result["wilson_lower"],
            result["avg_return_5d"],
            result["excess_win_rate"],
            result["avg_excess_5d"],
            1 if result["consecutive_loss_ok"] else 0,
            result["compliance_status"],
            result["max_state"],
            json.dumps(result["reasons"], ensure_ascii=False),
            json.dumps(result["explain"], ensure_ascii=False, default=str),
        ),
    )
    conn.commit()


def get_latest_gate_result() -> dict[str, Any]:
    import db

    db.init_db()
    conn = db.get_conn()
    try:
        row = conn.execute("SELECT * FROM auto_trade_gate ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            conn.close()
            return evaluate_auto_gate(explain=True)
        data = dict(row)
        data["gate_passed"] = data.get("gate_status") == "pass"
        for field in ("reasons_json", "explain_json"):
            try:
                data[field.replace("_json", "")] = json.loads(data.get(field) or "[]")
            except Exception:
                data[field.replace("_json", "")] = [] if field == "reasons_json" else {}
        return data
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _choose_price_record(source_rows: dict[str, dict[str, Any]]) -> tuple[dict[str, Any] | None, str, str, float, str]:
    priority = ["tencent", "sina", "akshare"]
    values = {
        src: float(row.get("price") or row.get("close") or 0)
        for src, row in source_rows.items()
        if row and float(row.get("price") or row.get("close") or 0) > 0
    }
    if not values:
        return None, "", "missing", 0.0, "no valid source price"
    if len(values) == 1:
        src = next(iter(values))
        return source_rows[src], src, "high", 0.0, ""

    prices = list(values.values())
    conflict = (max(prices) - min(prices)) / min(prices) if min(prices) > 0 else 0.0
    for src in priority:
        if src not in values:
            continue
        peers = [p for p in prices if abs(values[src] - p) / min(values[src], p) <= 0.01]
        if len(peers) >= 2:
            return source_rows[src], src, "high", conflict, ""
    for src in priority:
        if src in values:
            return source_rows[src], src, "low", conflict, f"source conflict {conflict * 100:.2f}% > 1%"
    src = next(iter(values))
    return source_rows[src], src, "low", conflict, f"source conflict {conflict * 100:.2f}% > 1%"


def record_real_data(dry_run: bool = False, codes: list[str] | None = None) -> dict[str, Any]:
    import db
    from data_engine import fetch_realtime

    db.init_db()
    codes = [c for c in (codes or ALL_CODES) if str(c).startswith(("000", "002", "600", "601", "603", "605"))]
    source_payload: dict[str, dict[str, dict[str, Any]]] = {}
    errors: dict[str, str] = {}
    for source in ("tencent", "sina", "akshare"):
        try:
            rows = fetch_realtime(codes, source=source)
            source_payload[source] = {r["code"]: r for r in rows}
        except Exception as exc:
            errors[source] = str(exc)
            source_payload[source] = {}

    records = []
    conn = db.get_conn()
    try:
        for code in codes:
            rows_by_source = {
                src: payload[code]
                for src, payload in source_payload.items()
                if code in payload
            }
            chosen, source, quality, conflict, warning = _choose_price_record(rows_by_source)
            if not chosen:
                records.append({"code": code, "quality_status": "missing", "warning": warning})
                continue
            close_y = float(chosen.get("close_yesterday") or 0)
            price = float(chosen.get("price") or chosen.get("close") or 0)
            change_pct = round((price - close_y) / close_y * 100, 2) if close_y else chosen.get("change_pct", 0)
            payload = {
                "code": code,
                "date": chosen.get("date") or date.today().isoformat(),
                "open": chosen.get("open"),
                "close": price,
                "high": chosen.get("high"),
                "low": chosen.get("low"),
                "volume": chosen.get("volume"),
                "amount": chosen.get("amount"),
                "change_pct": change_pct,
                "source": source,
                "adjustment_mode": "raw",
                "quality_status": quality,
            }
            records.append({**payload, "warning": warning, "conflict_pct": conflict})
            if dry_run:
                continue
            conn.execute(
                """
                INSERT INTO daily_snapshots
                    (code, date, open, close, high, low, volume, amount, change_pct)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(code, date) DO UPDATE SET
                    open=excluded.open, close=excluded.close,
                    high=excluded.high, low=excluded.low,
                    volume=excluded.volume, amount=excluded.amount,
                    change_pct=excluded.change_pct
                """,
                (
                    code,
                    payload["date"],
                    payload["open"],
                    payload["close"],
                    payload["high"],
                    payload["low"],
                    payload["volume"],
                    payload["amount"],
                    payload["change_pct"],
                ),
            )
            conn.execute(
                """
                INSERT INTO price_history
                    (code, date, open, close, high, low, volume, change_pct,
                     source, adjustment_mode, quality_status, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'raw', ?, datetime('now', 'localtime'))
                ON CONFLICT(code, date) DO UPDATE SET
                    open=excluded.open, close=excluded.close, high=excluded.high,
                    low=excluded.low, volume=excluded.volume,
                    change_pct=excluded.change_pct, source=excluded.source,
                    adjustment_mode='raw', quality_status=excluded.quality_status,
                    recorded_at=excluded.recorded_at
                """,
                (
                    code,
                    payload["date"],
                    payload["open"],
                    payload["close"],
                    payload["high"],
                    payload["low"],
                    payload["volume"],
                    payload["change_pct"],
                    source,
                    quality,
                ),
            )
            conn.execute(
                """
                INSERT INTO data_quality_log
                    (code, date, source_values_json, chosen_source, quality_status,
                     conflict_pct, warning)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    code,
                    payload["date"],
                    json.dumps(rows_by_source, ensure_ascii=False, default=str),
                    source,
                    quality,
                    conflict,
                    warning,
                ),
            )
        if not dry_run:
            conn.commit()
    finally:
        conn.close()
    return {
        "dry_run": dry_run,
        "count": len(records),
        "saved": 0 if dry_run else len([r for r in records if r.get("quality_status") != "missing"]),
        "low_quality": [r for r in records if r.get("quality_status") == "low"],
        "missing": [r for r in records if r.get("quality_status") == "missing"],
        "source_errors": errors,
        "records": records,
    }


def _price_row(conn, code: str, date_str: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM price_history WHERE code=? AND date=?",
        (code, date_str),
    ).fetchone()
    return dict(row) if row else None


def settle_pending_signal_outcomes(dry_run: bool = False) -> dict[str, Any]:
    import db

    db.init_db()
    version = ensure_current_strategy_version()["version"]
    today = date.today().isoformat()
    conn = db.get_conn()
    settled = expired = pending = non_executable = 0
    details: list[dict[str, Any]] = []
    try:
        rows = conn.execute(
            """
            SELECT * FROM signal_log
            WHERE action IN ('STRONG_BUY', 'BUY', 'CAUTION_BUY')
              AND COALESCE(settlement_status, 'pending') IN ('', 'pending', 'unknown')
            ORDER BY date ASC
            """
        ).fetchall()
        for raw in rows:
            sig = dict(raw)
            signal_date = sig["date"]
            if trading_days_between(signal_date, today) > SIGNAL_OUTCOME_EXPIRY_TRADING_DAYS:
                expired += 1
                details.append({"id": sig["id"], "code": sig["code"], "status": "expired_unsettled"})
                if not dry_run:
                    conn.execute(
                        """
                        UPDATE signal_log
                        SET settlement_status='expired_unsettled',
                            executable_status='expired_unsettled',
                            non_executable_reason='signal outcome unresolved after expiry'
                        WHERE id=?
                        """,
                        (sig["id"],),
                    )
                continue

            entry_date = add_trading_days(signal_date, 1)
            exit_date = add_trading_days(signal_date, 6)
            entry = _price_row(conn, sig["code"], entry_date)
            exit_ = _price_row(conn, sig["code"], exit_date)
            if not entry or not exit_:
                pending += 1
                continue
            entry_open = float(entry.get("open") or 0)
            exit_open = float(exit_.get("open") or 0)
            quality = "high" if entry.get("quality_status") == "high" and exit_.get("quality_status") == "high" else "low"
            adjustment = entry.get("adjustment_mode") or exit_.get("adjustment_mode") or "raw"
            if entry_open <= 0 or exit_open <= 0 or classify_backtest_price_source(adjustment) != "gate_eligible":
                non_executable += 1
                reason = "missing executable open price" if entry_open <= 0 or exit_open <= 0 else "adjusted price source"
                details.append({"id": sig["id"], "code": sig["code"], "status": "non_executable", "reason": reason})
                if not dry_run:
                    conn.execute(
                        """
                        UPDATE signal_log
                        SET settlement_status='non_executable',
                            executable_status='non_executable',
                            non_executable_reason=?,
                            adjustment_mode=?
                        WHERE id=?
                        """,
                        (reason, adjustment, sig["id"]),
                    )
                continue

            tier = int(STOCK_MAP.get(sig["code"], {}).get("tier", 2))
            benchmark = BENCHMARK_BY_TIER.get(tier, BENCHMARK_BY_TIER[2])
            bench_entry = _price_row(conn, benchmark["code"], entry_date)
            bench_exit = _price_row(conn, benchmark["code"], exit_date)
            if not bench_entry or not bench_exit or not bench_entry.get("open") or not bench_exit.get("open"):
                pending += 1
                continue
            stock_return = (exit_open - entry_open) / entry_open * 100
            bench_return = (float(bench_exit["open"]) - float(bench_entry["open"])) / float(bench_entry["open"]) * 100
            excess = stock_return - bench_return
            settled += 1
            details.append({"id": sig["id"], "code": sig["code"], "status": "settled", "return_5d": stock_return, "excess_5d": excess})
            if not dry_run:
                conn.execute(
                    """
                    UPDATE signal_log
                    SET entry_date=?, entry_open=?, exit_date=?, exit_open=?,
                        outcome_5d=?, return_5d=?, benchmark_code=?,
                        benchmark_return_5d=?, excess_5d=?,
                        strategy_version=COALESCE(NULLIF(strategy_version, ''), ?),
                        settlement_status='settled',
                        executable_status='executable',
                        data_quality=?,
                        adjustment_mode=?
                    WHERE id=?
                    """,
                    (
                        entry_date,
                        entry_open,
                        exit_date,
                        exit_open,
                        stock_return,
                        stock_return,
                        benchmark["code"],
                        bench_return,
                        excess,
                        version,
                        quality,
                        adjustment,
                        sig["id"],
                    ),
                )
        if not dry_run:
            conn.commit()
    finally:
        conn.close()
    return {
        "dry_run": dry_run,
        "settled": settled,
        "pending": pending,
        "expired_unsettled": expired,
        "non_executable": non_executable,
        "details": details,
    }


def create_order_state(
    code: str,
    action: str,
    state: str = "generated",
    *,
    price: float = 0,
    shares: int = 0,
    amount: float = 0,
    reason: str = "",
    idempotency_key: str = "",
) -> dict[str, Any]:
    if state not in ORDER_STATES:
        raise ValueError(f"Invalid order state: {state}")
    import db

    db.init_db()
    today = date.today().isoformat()
    key = idempotency_key or f"{today}:{code}:{action}:{price}:{shares}:{amount}"
    order_id = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    conn = db.get_conn()
    try:
        previous = conn.execute(
            "SELECT state FROM order_state_log WHERE order_id=? ORDER BY id DESC LIMIT 1",
            (order_id,),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO order_state_log
                (order_id, idempotency_key, date, code, action, state,
                 previous_state, price, shares, amount, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_id,
                key,
                today,
                code,
                action.upper(),
                state,
                previous["state"] if previous else "",
                price,
                shares,
                amount,
                reason,
            ),
        )
        conn.commit()
        return {"order_id": order_id, "state": state, "idempotency_key": key}
    finally:
        conn.close()


def format_gate_report(result: dict[str, Any]) -> str:
    lines = [
        f"Serenity auto gate | {result['date']} | {result['state']}",
        f"strategy: {result['strategy_version']} {result['strategy_hash'][:12]}",
        f"samples: {result['sample_count']}/{result['required_sample_count']}",
        f"win: {result['win_rate']:.1%} | Wilson lower: {result['wilson_lower']:.1%}",
        f"avg return: {result['avg_return_5d']:.2f}% | excess win: {result['excess_win_rate']:.1%} | avg excess: {result['avg_excess_5d']:.2f}%",
        f"consecutive loss ok: {result['consecutive_loss_ok']} | compliance: {result['compliance_status']} | max state: {result['max_state']}",
    ]
    if result["reasons"]:
        lines.append("blocked reasons:")
        lines.extend(f"  - {r}" for r in result["reasons"])
    if result.get("explain"):
        trigger = result.get("consecutive_loss_trigger") or []
        if trigger:
            lines.append("consecutive loss trigger:")
            for s in trigger:
                ret = s.get("return_5d") if s.get("return_5d") is not None else s.get("outcome_5d")
                lines.append(f"  - {s['date']} {s['code']} return={ret:.2f}% excess={s['excess_5d']:.2f}%")
        lines.append(f"date distribution: {result['explain'].get('date_distribution', {})}")
        lines.append(f"tier distribution: {result['explain'].get('tier_distribution', {})}")
    return "\n".join(lines)


def format_record_report(result: dict[str, Any]) -> str:
    lines = [
        f"record-real-data dry_run={result['dry_run']} saved={result['saved']} count={result['count']}",
        f"low_quality={len(result['low_quality'])} missing={len(result['missing'])}",
    ]
    if result["source_errors"]:
        lines.append(f"source_errors={result['source_errors']}")
    for item in result["low_quality"][:8]:
        lines.append(f"  warning {item['code']}: {item.get('warning', '')}")
    return "\n".join(lines)
