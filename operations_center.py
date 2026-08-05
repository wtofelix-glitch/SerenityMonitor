"""Auditable daily operations loop for Serenity.

This module reads authoritative broker facts, derives operational checks, and
drives PAPER-only drills. It never submits a live order or overwrites a broker
reconciliation snapshot.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
import hashlib
import json
from typing import Optional, Any

import db
from config import get_stock_name
from auto_gate import (
    BROKER_DRAWDOWN_MIN_POINTS,
    BROKER_SNAPSHOT_STALE_HOURS,
    assess_broker_risk,
    trading_days_between,
)


RECONCILIATION_WARNING_PCT = 0.001
RECONCILIATION_BLOCK_PCT = 0.01
DAILY_LOSS_LOCK_PCT = -2.0
PORTFOLIO_DRAWDOWN_LOCK_PCT = -6.0
AUTO_SINGLE_POSITION_LIMIT_PCT = 20.0
AUTO_POOL_LIMIT_PCT = 30.0
MAX_HOLDING_TRADING_DAYS = 20
MAX_PAPER_BUYS_PER_DAY = 1
BROKER_FUTURE_TOLERANCE_MINUTES = 5
BROKER_EQUATION_TOLERANCE_PCT = 0.001
MAIN_BOARD_PREFIXES = ("000", "002", "600", "601", "603", "605")


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _parse_snapshot_time(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").strip())
    except ValueError as exc:
        raise ValueError("snapshot_at must be an ISO datetime") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed.replace(microsecond=0)


def _validate_broker_snapshot(payload: dict[str, Any], now: datetime) -> dict[str, Any]:
    snapshot_time = _parse_snapshot_time(payload.get("snapshot_at"))
    if snapshot_time > now + timedelta(minutes=BROKER_FUTURE_TOLERANCE_MINUTES):
        raise ValueError("snapshot_at is in the future")

    total_assets = _number(payload.get("total_assets"))
    holdings_value = _number(payload.get("holdings_value"))
    cash = _number(payload.get("cash"))
    if total_assets <= 0 or holdings_value < 0 or cash < 0:
        raise ValueError("total_assets must be positive and cash/holdings non-negative")
    equation_tolerance = max(1.0, total_assets * BROKER_EQUATION_TOLERANCE_PCT)
    if abs(total_assets - cash - holdings_value) > equation_tolerance:
        raise ValueError("asset equation mismatch: total_assets != cash + holdings_value")

    positions = []
    seen_codes: set[str] = set()
    for raw in payload.get("positions") or []:
        code = str(raw.get("code") or "").strip()
        if not code.startswith(MAIN_BOARD_PREFIXES):
            raise ValueError(f"position {code or '<missing>'} is outside the main-board universe")
        if code in seen_codes:
            raise ValueError(f"duplicate position code: {code}")
        seen_codes.add(code)
        shares_raw = _number(raw.get("shares") or raw.get("quantity"))
        shares = int(shares_raw)
        market_value = _number(raw.get("market_value"))
        if shares <= 0 or shares != shares_raw or market_value < 0:
            raise ValueError(f"position {code} has invalid shares or market_value")
        position = {
            **raw,
            "code": code,
            "name": str(raw.get("name") or get_stock_name(code)),
            "shares": shares,
            "market_value": round(market_value, 2),
        }
        if "profit_pct" in raw:
            position["profit_pct"] = round(_number(raw.get("profit_pct")), 4)
        positions.append(position)
    position_total = sum(item["market_value"] for item in positions)
    position_tolerance = max(1.0, holdings_value * BROKER_EQUATION_TOLERANCE_PCT)
    if abs(position_total - holdings_value) > position_tolerance:
        raise ValueError("position market values do not match holdings_value")

    return {
        "snapshot_at": snapshot_time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": str(payload.get("source") or "broker_manual"),
        "total_assets": round(total_assets, 2),
        "holdings_value": round(holdings_value, 2),
        "cash": round(cash, 2),
        "floating_profit": round(_number(payload.get("floating_profit")), 2),
        "daily_profit": round(_number(payload.get("daily_profit")), 2),
        "daily_profit_pct": round(_number(payload.get("daily_profit_pct")), 4),
        "position_ratio_pct": round(holdings_value / total_assets * 100, 4),
        "positions": sorted(positions, key=lambda item: item["code"]),
        "evidence_path": str(payload.get("evidence_path") or ""),
        "notes": str(payload.get("notes") or ""),
    }


def _broker_snapshot_fingerprint(snapshot: dict[str, Any]) -> str:
    material = {
        key: snapshot.get(key)
        for key in (
            "snapshot_at", "source", "total_assets", "holdings_value", "cash",
            "floating_profit", "daily_profit", "daily_profit_pct",
            "positions", "evidence_path", "notes",
        )
    }
    material["positions"] = sorted(
        material.get("positions") or [], key=lambda item: str(item.get("code") or "")
    )
    return json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def import_broker_snapshot(
    payload: dict[str, Any],
    *,
    dry_run: bool = False,
    internal: Optional[dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Validate immutable broker evidence, persist it, then refresh risk state."""
    now = now or datetime.now()
    snapshot = _validate_broker_snapshot(dict(payload or {}), now)
    existing = db.get_portfolio_reconciliation(snapshot["snapshot_at"])
    if existing:
        if _broker_snapshot_fingerprint(existing) != _broker_snapshot_fingerprint(snapshot):
            raise ValueError("snapshot_at conflicts with existing evidence")
        saved = existing
        idempotent = True
    else:
        latest = db.get_latest_portfolio_reconciliation()
        if latest and snapshot["snapshot_at"] < latest["snapshot_at"]:
            raise ValueError("snapshot_at is older than latest broker evidence")
        saved = snapshot if dry_run else db.save_portfolio_reconciliation(snapshot)
        idempotent = False

    if internal is None:
        try:
            internal = _portfolio_facts()
        except Exception:
            internal = {"total_value": 0, "holdings_value": 0, "cash": 0, "positions": []}
    reconciliation = run_reconciliation(
        internal=internal,
        broker=saved,
        now=now,
        dry_run=dry_run or idempotent,
    )
    quality = get_data_quality_summary()
    tasks = generate_risk_tasks(
        broker=saved,
        quality=quality,
        reconciliation=reconciliation,
        dry_run=dry_run or idempotent,
    )
    return {
        "saved": not dry_run and not idempotent,
        "dry_run": dry_run,
        "idempotent": idempotent,
        "snapshot": saved,
        "reconciliation": reconciliation,
        "risk_assessment": assess_broker_risk(latest_snapshot=saved, now=now),
        "risk_recovery": build_concentration_recovery_plan(saved),
        "tasks": tasks,
    }


def lint_broker_snapshot_payload(
    payload: dict[str, Any],
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Validate a broker snapshot payload without writing anything."""
    now = now or datetime.now()
    payload = dict(payload or {})
    errors: list[str] = []
    warnings: list[str] = []

    required = ("snapshot_at", "total_assets", "holdings_value", "cash")
    for key in required:
        if payload.get(key) in (None, ""):
            errors.append(f"missing required field: {key}")

    positions = payload.get("positions")
    if positions is None:
        errors.append("missing required field: positions")
    elif not isinstance(positions, list):
        errors.append("positions must be a list")
    else:
        for idx, position in enumerate(positions):
            code = str((position or {}).get("code") or "").strip()
            if not code:
                errors.append(f"positions[{idx}].code is required")
            if (position or {}).get("shares") in (None, "") and (position or {}).get("quantity") in (None, ""):
                errors.append(f"positions[{idx}].shares is required")
            if (position or {}).get("market_value") in (None, ""):
                errors.append(f"positions[{idx}].market_value is required")
            if (position or {}).get("profit_pct") in (None, ""):
                warnings.append(f"positions[{idx}].profit_pct is optional but useful for risk tasks")

    normalized = None
    if not errors:
        try:
            normalized = _validate_broker_snapshot(payload, now)
        except ValueError as exc:
            errors.append(str(exc))

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "normalized": normalized,
        "next_command": "" if errors else "python3 cli.py broker-snapshot <json-file> --dry-run",
    }


def build_broker_snapshot_template(now: Optional[datetime] = None) -> dict[str, Any]:
    """Build a fill-in broker snapshot template without mutating state."""
    now = (now or datetime.now()).replace(microsecond=0)
    latest = db.get_latest_portfolio_reconciliation()
    risk = assess_broker_risk(now=now)
    try:
        internal = _portfolio_facts()
    except Exception:
        internal = {"total_value": 0, "holdings_value": 0, "cash": 0, "positions": []}

    position_hints = []
    for item in internal.get("positions") or []:
        code = str(item.get("code") or "")
        if not code:
            continue
        shares = int(_number(item.get("shares") or item.get("quantity")))
        position_hints.append({
            "code": code,
            "name": item.get("name") or get_stock_name(code),
            "shares": shares,
            "market_value": item.get("current_value") or item.get("market_value"),
        })

    current_points = int(risk.get("point_count") or 0)
    missing_points = max(0, BROKER_DRAWDOWN_MIN_POINTS - current_points)
    snapshot_stamp = now.strftime("%Y%m%d_%H%M")
    template = {
        "snapshot_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "source": "broker_manual",
        "total_assets": None,
        "holdings_value": None,
        "cash": None,
        "daily_profit": 0,
        "daily_profit_pct": 0,
        "positions": [
            {
                "code": item["code"],
                "name": item["name"],
                "shares": item["shares"],
                "market_value": None,
                "profit_pct": None,
            }
            for item in position_hints
        ],
        "evidence_path": "",
        "notes": "Fill from broker account screenshot/export. Do not use internal NAV as broker evidence.",
    }

    return {
        "status": "ready" if missing_points == 0 and not risk.get("lock_required") else "needs_broker_snapshot",
        "required_points": BROKER_DRAWDOWN_MIN_POINTS,
        "current_points": current_points,
        "missing_points": missing_points,
        "latest_snapshot_at": latest.get("snapshot_at") if latest else "",
        "risk_reasons": risk.get("reasons") or [],
        "suggested_filename": f"broker_snapshot_{snapshot_stamp}.json",
        "import_command": f"python3 cli.py broker-snapshot broker_snapshot_{snapshot_stamp}.json --dry-run",
        "template": template,
        "internal_position_hints": position_hints,
        "instructions": [
            "Copy total assets, cash, holdings value, and positions from the broker account.",
            "Ensure total_assets ~= cash + holdings_value.",
            "Ensure sum(position.market_value) ~= holdings_value.",
            "Run the import command with --dry-run first, then without --dry-run after validation passes.",
        ],
    }


def build_cashflow_reconciliation_template(now: Optional[datetime] = None) -> dict[str, Any]:
    """Build a fill-in template for the P0 diagnostic NAV cashflow blocker."""
    now = (now or datetime.now()).replace(microsecond=0)
    try:
        from alpha_validation import build_alpha_validation_report

        report = build_alpha_validation_report()
    except Exception as exc:
        return {
            "status": "error",
            "error": str(exc),
            "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        }

    data_quality = report.get("data_quality") or {}
    cashflow = data_quality.get("drawdown_cashflow") or {}
    attribution = report.get("drawdown_attribution") or {}
    status = str(cashflow.get("status") or "")
    if status != "BLOCK":
        return {
            "status": "not_needed",
            "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "cashflow_status": status or "UNKNOWN",
            "verdict": report.get("verdict", ""),
            "window": cashflow.get("window", ""),
        }

    gap = _number(cashflow.get("gap", attribution.get("cashflow_gap")))
    tolerance = _number(cashflow.get("tolerance", attribution.get("cashflow_tolerance")))
    if gap < 0:
        direction = "unexplained_cash_outflow"
        likely_causes = [
            "missing buy trade",
            "cash withdrawal or transfer out",
            "fees/taxes not represented in trades",
            "diagnostic nav_history cash is too low",
        ]
        cash_effect_note = "negative cash_effect means cash decreased beyond recorded trades"
    elif gap > 0:
        direction = "unexplained_cash_inflow"
        likely_causes = [
            "missing sell trade",
            "cash deposit or transfer in",
            "dividend or interest",
            "diagnostic nav_history cash is too high",
        ]
        cash_effect_note = "positive cash_effect means cash increased beyond recorded trades"
    else:
        direction = "balanced"
        likely_causes = []
        cash_effect_note = "cashflow is balanced"

    window_start = attribution.get("start", "")
    window_end = attribution.get("end", "")
    stamp = now.strftime("%Y%m%d_%H%M")
    template = {
        "window": {
            "start": window_start,
            "end": window_end,
            "source": attribution.get("source", ""),
        },
        "cash_reconciliation": {
            "start_total": attribution.get("start_total"),
            "end_total": attribution.get("end_total"),
            "start_cash": attribution.get("start_cash"),
            "end_cash": attribution.get("end_cash"),
            "start_holdings": attribution.get("start_holdings"),
            "end_holdings": attribution.get("end_holdings"),
            "cash_change": attribution.get("cash_change"),
            "recorded_sell_amount": attribution.get("sell_amount"),
            "recorded_buy_amount": attribution.get("buy_amount"),
            "recorded_net_trade_cashflow": attribution.get("net_trade_cashflow"),
            "unexplained_cash_effect": round(gap, 2),
            "tolerance": round(tolerance, 2),
            "cash_effect_note": cash_effect_note,
        },
        "evidence_items": [
            {
                "date": window_end or window_start,
                "type": None,
                "cash_effect": round(gap, 2),
                "amount": round(abs(gap), 2),
                "code": "",
                "shares": None,
                "price": None,
                "evidence_path": "",
                "notes": "",
            }
        ],
        "broker_snapshot_preferred": True,
        "broker_snapshot_command": "python3 cli.py broker-snapshot-template --write",
        "notes": (
            "Use broker/account evidence first. Do not edit trade or NAV history "
            "without matching broker statement, screenshot, or export."
        ),
    }

    return {
        "status": "needs_cashflow_reconciliation",
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "verdict": report.get("verdict", ""),
        "window": cashflow.get("window", f"{window_start}~{window_end}"),
        "direction": direction,
        "gap": round(gap, 2),
        "tolerance": round(tolerance, 2),
        "likely_causes": likely_causes,
        "drawdown_attribution": attribution,
        "template": template,
        "suggested_filename": f"cashflow_reconciliation_{stamp}.json",
        "instructions": [
            "Prefer importing fresh broker NAV snapshots; broker-reconciled NAV is authoritative.",
            "If repairing legacy diagnostic NAV, identify the missing trade, cash transfer, dividend, fee, or NAV correction.",
            "Attach evidence_path before any manual ledger correction.",
            "Run `python3 cli.py cashflow-reconciliation <json-file> --dry-run`, then import without --dry-run after validation passes.",
            "Re-run `python3 cli.py alpha-validation` after evidence is imported or reconciled.",
        ],
    }


def lint_cashflow_reconciliation_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate a filled cashflow reconciliation payload without mutating state."""
    payload = dict(payload or {})
    errors: list[str] = []
    warnings: list[str] = []
    allowed_types = {
        "missing_buy_trade",
        "missing_sell_trade",
        "cash_transfer_out",
        "cash_transfer_in",
        "fee_tax",
        "dividend_interest",
        "nav_cash_correction",
        "other",
    }

    window = payload.get("window") if isinstance(payload.get("window"), dict) else {}
    start = str(window.get("start") or "").strip()
    end = str(window.get("end") or "").strip()
    for key, value in (("window.start", start), ("window.end", end)):
        if not value:
            errors.append(f"missing required field: {key}")
            continue
        try:
            date.fromisoformat(value)
        except ValueError:
            errors.append(f"{key} must be YYYY-MM-DD")

    reconciliation = (
        payload.get("cash_reconciliation")
        if isinstance(payload.get("cash_reconciliation"), dict) else {}
    )
    gap = _number(reconciliation.get("unexplained_cash_effect"))
    tolerance = _number(reconciliation.get("tolerance"))
    if reconciliation.get("unexplained_cash_effect") in (None, ""):
        errors.append("missing required field: cash_reconciliation.unexplained_cash_effect")
    if tolerance <= 0:
        errors.append("cash_reconciliation.tolerance must be positive")

    evidence_items = payload.get("evidence_items")
    if not isinstance(evidence_items, list) or not evidence_items:
        errors.append("evidence_items must be a non-empty list")
        evidence_items = []

    total_effect = 0.0
    normalized_items = []
    for idx, raw in enumerate(evidence_items):
        item = raw if isinstance(raw, dict) else {}
        item_type = str(item.get("type") or "").strip()
        if not item_type:
            errors.append(f"evidence_items[{idx}].type is required")
        elif item_type not in allowed_types:
            errors.append(
                f"evidence_items[{idx}].type must be one of {sorted(allowed_types)}"
            )

        item_date = str(item.get("date") or "").strip()
        if not item_date:
            errors.append(f"evidence_items[{idx}].date is required")
        else:
            try:
                parsed_date = date.fromisoformat(item_date)
                if start and end:
                    try:
                        if not (date.fromisoformat(start) <= parsed_date <= date.fromisoformat(end)):
                            warnings.append(f"evidence_items[{idx}].date is outside the drawdown window")
                    except ValueError:
                        pass
            except ValueError:
                errors.append(f"evidence_items[{idx}].date must be YYYY-MM-DD")

        if item.get("cash_effect") in (None, ""):
            errors.append(f"evidence_items[{idx}].cash_effect is required")
            effect = 0.0
        else:
            effect = _number(item.get("cash_effect"))
            if effect == 0:
                errors.append(f"evidence_items[{idx}].cash_effect must be non-zero")
        total_effect += effect

        amount = _number(item.get("amount"))
        if item.get("amount") in (None, ""):
            errors.append(f"evidence_items[{idx}].amount is required")
        elif abs(abs(effect) - amount) > 1.0:
            warnings.append(
                f"evidence_items[{idx}].amount differs from abs(cash_effect) by more than 1"
            )

        evidence_path = str(item.get("evidence_path") or "").strip()
        if not evidence_path:
            errors.append(f"evidence_items[{idx}].evidence_path is required")

        if item_type in {"missing_buy_trade", "cash_transfer_out", "fee_tax"} and effect > 0:
            warnings.append(f"evidence_items[{idx}] type usually reduces cash but cash_effect is positive")
        if item_type in {"missing_sell_trade", "cash_transfer_in", "dividend_interest"} and effect < 0:
            warnings.append(f"evidence_items[{idx}] type usually increases cash but cash_effect is negative")

        if item_type in {"missing_buy_trade", "missing_sell_trade"}:
            if not str(item.get("code") or "").strip():
                errors.append(f"evidence_items[{idx}].code is required for missing trade evidence")
            if int(_number(item.get("shares"))) <= 0:
                errors.append(f"evidence_items[{idx}].shares must be positive for missing trade evidence")
            if _number(item.get("price")) <= 0:
                errors.append(f"evidence_items[{idx}].price must be positive for missing trade evidence")

        normalized_items.append({
            "date": item_date,
            "type": item_type,
            "cash_effect": round(effect, 2),
            "amount": round(amount, 2),
            "code": str(item.get("code") or "").strip(),
            "shares": int(_number(item.get("shares"))) if item.get("shares") not in (None, "") else None,
            "price": _number(item.get("price")) if item.get("price") not in (None, "") else None,
            "evidence_path": evidence_path,
            "notes": str(item.get("notes") or "").strip(),
        })

    remaining_gap = round(gap - total_effect, 2)
    if not errors and abs(remaining_gap) > tolerance:
        errors.append(
            f"evidence cash_effect total leaves remaining_gap {remaining_gap}, "
            f"exceeding tolerance {round(tolerance, 2)}"
        )

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "gap": round(gap, 2),
        "tolerance": round(tolerance, 2),
        "evidence_cash_effect_total": round(total_effect, 2),
        "remaining_gap": remaining_gap,
        "normalized_items": normalized_items,
        "next_step": (
            "Attach evidence and then decide whether to import broker snapshots or perform a documented ledger repair"
            if not errors else ""
        ),
    }


def _cashflow_reconciliation_hash(payload: dict[str, Any], lint: dict[str, Any]) -> str:
    window = payload.get("window") if isinstance(payload.get("window"), dict) else {}
    canonical = {
        "window_start": str(window.get("start") or ""),
        "window_end": str(window.get("end") or ""),
        "gap": lint.get("gap"),
        "tolerance": lint.get("tolerance"),
        "items": lint.get("normalized_items") or [],
    }
    raw = json.dumps(canonical, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def import_cashflow_reconciliation(
    payload: dict[str, Any],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Validate and persist a P0 cashflow reconciliation evidence package."""
    lint = lint_cashflow_reconciliation_payload(payload)
    if not lint.get("valid"):
        return {
            "valid": False,
            "imported": False,
            "dry_run": dry_run,
            "errors": lint.get("errors", []),
            "warnings": lint.get("warnings", []),
            "lint": lint,
        }

    window = payload.get("window") if isinstance(payload.get("window"), dict) else {}
    reconciliation = (
        payload.get("cash_reconciliation")
        if isinstance(payload.get("cash_reconciliation"), dict) else {}
    )
    evidence_hash = _cashflow_reconciliation_hash(payload, lint)
    record = {
        "window_start": str(window.get("start") or ""),
        "window_end": str(window.get("end") or ""),
        "source": str(window.get("source") or "manual"),
        "unexplained_cash_effect": lint["gap"],
        "tolerance": lint["tolerance"],
        "evidence_cash_effect_total": lint["evidence_cash_effect_total"],
        "remaining_gap": lint["remaining_gap"],
        "evidence_hash": evidence_hash,
        "evidence_items": lint["normalized_items"],
        "payload": payload,
        "notes": str(payload.get("notes") or reconciliation.get("notes") or ""),
    }
    if dry_run:
        return {
            "valid": True,
            "imported": False,
            "dry_run": True,
            "evidence_hash": evidence_hash,
            "record": record,
            "lint": lint,
        }

    saved = db.save_cashflow_reconciliation(record)
    db.resolve_operational_task("p0:drawdown_cashflow")
    return {
        "valid": True,
        "imported": True,
        "dry_run": False,
        "evidence_hash": evidence_hash,
        "record": saved,
        "lint": lint,
    }


def build_sample_readiness_report(as_of: Optional[str] = None) -> dict[str, Any]:
    """Summarize progress toward the P0 real BUY sample requirement."""
    try:
        from alpha_validation import build_alpha_validation_report
        from auto_gate import BENCHMARK_CODES, diagnose_signal_settlements
        from config import ALL_CODES

        alpha = build_alpha_validation_report()
        diagnostics = diagnose_signal_settlements(as_of=as_of)
    except Exception as exc:
        return {"status": "error", "error": str(exc), "as_of": as_of or date.today().isoformat()}

    samples = alpha.get("samples") or {}
    required = int(samples.get("required_sample_count") or 0)
    settled = int(samples.get("sample_count") or 0)
    remaining = max(0, required - settled)
    details = diagnostics.get("details") or []
    current_version = diagnostics.get("current_strategy_version") or ""

    current_pending = [
        item for item in details
        if item.get("strategy_version") == current_version
    ]
    legacy_pending = [
        item for item in details
        if item.get("strategy_version") != current_version
    ]
    awaiting = [item for item in current_pending if not item.get("due")]
    ready = [
        item for item in current_pending
        if item.get("due") and item.get("ready_to_settle")
    ]
    blocked = [
        item for item in current_pending
        if item.get("due") and not item.get("ready_to_settle")
    ]

    forecast: dict[str, int] = {}
    for item in awaiting:
        exit_date = str(item.get("exit_date") or "")
        if exit_date:
            forecast[exit_date] = forecast.get(exit_date, 0) + 1

    by_reason: dict[str, int] = {}
    for item in blocked + legacy_pending:
        for reason in item.get("reasons") or []:
            by_reason[reason] = by_reason.get(reason, 0) + 1

    by_strategy_version: dict[str, int] = {}
    for item in details:
        version = str(item.get("strategy_version") or "legacy_unversioned")
        by_strategy_version[version] = by_strategy_version.get(version, 0) + 1

    as_of_date = diagnostics.get("as_of") or as_of or date.today().isoformat()
    default_collection = {str(code) for code in [*ALL_CODES, *BENCHMARK_CODES]}
    required_codes: set[str] = set()
    required_price_rows: list[dict[str, Any]] = []
    conn = db.get_conn()
    try:
        for item in current_pending:
            required_pairs = (
                ("stock_entry", item.get("code"), item.get("entry_date")),
                ("stock_exit", item.get("code"), item.get("exit_date")),
                ("benchmark_entry", item.get("benchmark_code"), item.get("entry_date")),
                ("benchmark_exit", item.get("benchmark_code"), item.get("exit_date")),
            )
            for role, code, row_date in required_pairs:
                code = str(code or "")
                row_date = str(row_date or "")
                if not code or not row_date:
                    continue
                required_codes.add(code)
                status = "future"
                row_summary: dict[str, Any] = {}
                if row_date <= as_of_date:
                    row = conn.execute(
                        """
                        SELECT open, close, adjustment_mode, quality_status
                        FROM price_history
                        WHERE code=? AND date=?
                        """,
                        (code, row_date),
                    ).fetchone()
                    if not row:
                        status = "missing"
                    elif _number(row["open"]) <= 0 or _number(row["close"]) <= 0:
                        status = "invalid_price"
                    elif str(row["quality_status"] or "").lower() != "high":
                        status = "low_quality"
                    elif str(row["adjustment_mode"] or "raw").lower() not in {"raw", "unadjusted"}:
                        status = "adjusted"
                    else:
                        status = "ready"
                    row_summary = dict(row) if row else {}
                required_price_rows.append({
                    "signal_id": item.get("id"),
                    "signal_code": item.get("code"),
                    "role": role,
                    "code": code,
                    "date": row_date,
                    "status": status,
                    "row": row_summary,
                })
    finally:
        conn.close()

    price_status_counts: dict[str, int] = {}
    for item in required_price_rows:
        status_key = item["status"]
        price_status_counts[status_key] = price_status_counts.get(status_key, 0) + 1
    due_price_rows_today = [
        item for item in required_price_rows
        if item["date"] == as_of_date
    ]
    missing_due_price_rows = [
        item for item in required_price_rows
        if item["date"] <= as_of_date and item["status"] != "ready"
    ]
    next_required_dates = sorted({
        item["date"] for item in required_price_rows
        if item["status"] == "future"
    })
    next_record_date = _next_record_real_data_date(
        missing_due_price_rows,
        next_required_dates,
    )
    next_record_command = _record_real_data_command(next_record_date)
    dry_run_record_command = (
        f"python3 cli.py record-real-data --dry-run --as-of {next_record_date}"
        if next_record_date else "python3 cli.py record-real-data --dry-run"
    )

    status = (
        "ready" if settled >= required else
        "settlement_ready" if ready else
        "version_mismatch_no_current_pending" if not current_pending and legacy_pending else
        "awaiting_exit_date" if awaiting and not blocked else
        "blocked" if blocked else
        "insufficient_pending"
    )

    next_commands = [
        next_record_command,
        "python3 cli.py p0-sample-readiness",
        "python3 cli.py alpha-validation",
    ]
    if status == "version_mismatch_no_current_pending":
        next_commands.insert(0, "python3 cli.py workflow --dry-run")
        next_commands.insert(1, "python3 cli.py signal")

    return {
        "status": status,
        "as_of": diagnostics.get("as_of"),
        "current_strategy_version": current_version,
        "sample_count": settled,
        "required_sample_count": required,
        "remaining_required": remaining,
        "pending_total": int(diagnostics.get("pending") or 0),
        "pending_current_version": len(current_pending),
        "pending_legacy_or_other_version": len(legacy_pending),
        "due": int(diagnostics.get("due") or 0),
        "ready_to_settle_current_version": len(ready),
        "blocked_due_current_version": len(blocked),
        "awaiting_exit_current_version": len(awaiting),
        "pending_by_strategy_version": dict(sorted(by_strategy_version.items())),
        "required_collection_codes": sorted(required_codes),
        "missing_from_default_collection": sorted(required_codes - default_collection),
        "price_row_status_counts": dict(sorted(price_status_counts.items())),
        "due_price_rows_today": due_price_rows_today,
        "missing_due_price_rows": missing_due_price_rows,
        "next_required_price_dates": next_required_dates,
        "next_record_date": next_record_date,
        "next_record_command": next_record_command,
        "dry_run_record_command": dry_run_record_command,
        "forecast_by_exit_date": dict(sorted(forecast.items())),
        "blocked_reason_counts": dict(sorted(by_reason.items())),
        "next_commands": next_commands,
        "details": {
            "ready_to_settle": ready[:20],
            "blocked_due": blocked[:20],
            "awaiting_exit": awaiting[:20],
            "legacy_or_other_version": legacy_pending[:20],
            "required_price_rows": required_price_rows[:80],
        },
    }


def _next_record_real_data_date(
    missing_due_price_rows: list[dict[str, Any]],
    next_required_dates: list[str],
) -> str:
    missing_dates = sorted({
        str(item.get("date") or "")
        for item in missing_due_price_rows
        if item.get("date")
    })
    if missing_dates:
        return missing_dates[0]
    return next_required_dates[0] if next_required_dates else ""


def _record_real_data_command(as_of: str = "") -> str:
    if as_of:
        return f"python3 cli.py record-real-data --as-of {as_of}"
    return "python3 cli.py record-real-data"


def build_p0_evidence_status(as_of: Optional[str] = None) -> dict[str, Any]:
    """Build one read-only status object for all P0 evidence blockers."""
    try:
        from alpha_validation import build_alpha_validation_report

        alpha = build_alpha_validation_report()
    except Exception as exc:
        return {
            "status": "error",
            "error": str(exc),
            "as_of": as_of or date.today().isoformat(),
        }

    broker = build_broker_snapshot_template()
    cashflow = build_cashflow_reconciliation_template()
    samples = build_sample_readiness_report(as_of=as_of)

    criteria = [
        {
            "key": item.get("key"),
            "label": item.get("label"),
            "status": item.get("status"),
        }
        for item in alpha.get("criteria") or []
    ]
    blockers = [item for item in criteria if item.get("status") not in {"PASS", "WATCH"}]
    watches = [item for item in criteria if item.get("status") == "WATCH"]

    next_actions: list[dict[str, Any]] = []
    if int(broker.get("missing_points") or 0) > 0 or broker.get("status") != "ready":
        next_actions.append({
            "priority": 1,
            "key": "broker_snapshot",
            "title": "Import fresh broker NAV evidence",
            "command": "python3 cli.py broker-snapshot-template --write",
            "why": (
                f"broker evidence {broker.get('current_points', 0)}/"
                f"{broker.get('required_points', 2)}"
            ),
        })

    if cashflow.get("status") == "needs_cashflow_reconciliation":
        next_actions.append({
            "priority": 2,
            "key": "drawdown_cashflow",
            "title": "Reconcile drawdown-window cashflow",
            "command": "python3 cli.py cashflow-reconciliation-template --write",
            "lint_command": "python3 cli.py cashflow-reconciliation-lint <json-file>",
            "dry_run_import_command": "python3 cli.py cashflow-reconciliation <json-file> --dry-run",
            "import_command": "python3 cli.py cashflow-reconciliation <json-file>",
            "why": f"cashflow gap {cashflow.get('gap')} > tolerance {cashflow.get('tolerance')}",
        })

    missing_due = samples.get("missing_due_price_rows") or []
    record_date = samples.get("next_record_date") or _next_record_real_data_date(
        missing_due,
        samples.get("next_required_price_dates") or [],
    )
    record_command = samples.get("next_record_command") or _record_real_data_command(record_date)
    dry_run_record_command = samples.get("dry_run_record_command") or (
        f"python3 cli.py record-real-data --dry-run --as-of {record_date}"
        if record_date else "python3 cli.py record-real-data --dry-run"
    )
    if missing_due:
        next_actions.append({
            "priority": 3,
            "key": "sample_price_rows",
            "title": "Record missing due price rows for current-version samples",
            "command": record_command,
            "dry_run_command": dry_run_record_command,
            "why": f"{len(missing_due)} due price rows are missing or invalid",
            "codes": sorted({item.get("code") for item in missing_due if item.get("code")}),
            "record_date": record_date,
        })
    elif samples.get("status") == "version_mismatch_no_current_pending":
        next_actions.append({
            "priority": 4,
            "key": "current_version_signals",
            "title": "Generate current-version BUY signal samples",
            "command": "python3 cli.py signal",
            "why": "pending BUY signals are from older strategy versions",
        })
    elif int(samples.get("pending_current_version") or 0) > 0:
        next_actions.append({
            "priority": 5,
            "key": "sample_collection_calendar",
            "title": "Keep collecting required entry/exit price rows",
            "command": record_command,
            "dry_run_command": dry_run_record_command,
            "why": "current-version samples are awaiting entry/exit dates",
            "next_required_price_dates": samples.get("next_required_price_dates") or [],
            "next_record_date": record_date,
            "required_collection_codes": samples.get("required_collection_codes") or [],
        })

    if int(samples.get("sample_count") or 0) < int(samples.get("required_sample_count") or 0):
        next_actions.append({
            "priority": 6,
            "key": "sample_count",
            "title": "Accumulate settled current-version real BUY samples",
            "command": "python3 cli.py p0-sample-readiness",
            "why": (
                f"samples {samples.get('sample_count', 0)}/"
                f"{samples.get('required_sample_count', 0)}"
            ),
        })

    next_actions = sorted(next_actions, key=lambda item: item["priority"])
    strategy = alpha.get("strategy") or {}
    return {
        "status": alpha.get("verdict", ""),
        "as_of": samples.get("as_of") or as_of or date.today().isoformat(),
        "strategy": {
            "version": strategy.get("version", ""),
            "config_hash": strategy.get("config_hash", ""),
            "hash_matches_current": strategy.get("hash_matches_current"),
            "created_at": strategy.get("created_at", ""),
        },
        "blockers": blockers,
        "watches": watches,
        "broker": {
            "status": broker.get("status"),
            "current_points": broker.get("current_points"),
            "required_points": broker.get("required_points"),
            "missing_points": broker.get("missing_points"),
            "latest_snapshot_at": broker.get("latest_snapshot_at"),
            "risk_reasons": broker.get("risk_reasons") or [],
        },
        "cashflow": {
            "status": cashflow.get("status"),
            "window": cashflow.get("window"),
            "direction": cashflow.get("direction"),
            "gap": cashflow.get("gap"),
            "tolerance": cashflow.get("tolerance"),
        },
        "samples": {
            "status": samples.get("status"),
            "sample_count": samples.get("sample_count"),
            "required_sample_count": samples.get("required_sample_count"),
            "pending_current_version": samples.get("pending_current_version"),
            "pending_by_strategy_version": samples.get("pending_by_strategy_version"),
            "price_row_status_counts": samples.get("price_row_status_counts"),
            "next_required_price_dates": samples.get("next_required_price_dates"),
            "next_record_date": record_date,
            "next_record_command": record_command,
            "dry_run_record_command": dry_run_record_command,
            "missing_due_price_rows": samples.get("missing_due_price_rows"),
        },
        "next_actions": next_actions,
        "commands": {
            "alpha_validation": "python3 cli.py alpha-validation",
            "auto_gate": "python3 cli.py auto-gate --explain",
            "broker_template": "python3 cli.py broker-snapshot-template --write",
            "cashflow_template": "python3 cli.py cashflow-reconciliation-template --write",
            "cashflow_import": "python3 cli.py cashflow-reconciliation <json-file>",
            "sample_readiness": "python3 cli.py p0-sample-readiness",
            "record_real_data": record_command,
        },
    }


def _portfolio_facts() -> dict[str, Any]:
    from portfolio import PortfolioManager

    raw = PortfolioManager().get_portfolio_value()
    return {
        "total_value": _number(raw.get("total_value")),
        "cash": _number(raw.get("cash")),
        "holdings_value": _number(raw.get("holdings_value")),
        "positions": raw.get("positions") or raw.get("position_details") or [],
    }


def _positions_by_code(positions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(item.get("code")): item for item in positions if item.get("code")}


def _drift_pct(actual: float, expected: float) -> float:
    return abs(actual - expected) / abs(expected) if expected else (1.0 if actual else 0.0)


def run_reconciliation(
    *,
    internal: Optional[dict[str, Any]] = None,
    broker: Optional[dict[str, Any]] = None,
    now: Optional[datetime] = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Compare internal portfolio facts with the latest broker snapshot."""
    now = now or datetime.now()
    internal = internal or _portfolio_facts()
    broker = broker if broker is not None else db.get_latest_portfolio_reconciliation()
    audit_date = now.date().isoformat()

    if not broker:
        audit = {
            "audit_date": audit_date,
            "broker_snapshot_at": "",
            "status": "blocked",
            "asset_drift": 0,
            "cash_drift": 0,
            "holdings_drift": 0,
            "position_mismatches": [],
            "details": {"reason": "missing_broker_snapshot", "stale": True},
        }
    else:
        snapshot_at = str(broker.get("snapshot_at") or "")
        try:
            snapshot_time = datetime.fromisoformat(snapshot_at)
            snapshot_age_hours = max(0.0, (now - snapshot_time).total_seconds() / 3600)
        except ValueError:
            snapshot_age_hours = float("inf")
        stale = snapshot_age_hours > BROKER_SNAPSHOT_STALE_HOURS

        internal_positions = _positions_by_code(internal.get("positions") or [])
        broker_positions = _positions_by_code(broker.get("positions") or [])
        mismatches = []
        for code in sorted(set(internal_positions) | set(broker_positions)):
            left = internal_positions.get(code) or {}
            right = broker_positions.get(code) or {}
            internal_shares = int(_number(left.get("shares") or left.get("quantity")))
            broker_shares = int(_number(right.get("shares") or right.get("quantity")))
            if internal_shares != broker_shares:
                mismatches.append({
                    "code": code,
                    "internal_shares": internal_shares,
                    "broker_shares": broker_shares,
                })

        broker_total = _number(broker.get("total_assets"))
        broker_cash = _number(broker.get("cash"))
        broker_holdings = _number(broker.get("holdings_value"))
        asset_drift = _number(internal.get("total_value")) - broker_total
        cash_drift = _number(internal.get("cash")) - broker_cash
        holdings_drift = _number(internal.get("holdings_value")) - broker_holdings
        max_drift_pct = max(
            _drift_pct(_number(internal.get("total_value")), broker_total),
            _drift_pct(_number(internal.get("cash")), broker_cash),
            _drift_pct(_number(internal.get("holdings_value")), broker_holdings),
        )
        if mismatches or max_drift_pct > RECONCILIATION_BLOCK_PCT:
            status = "blocked"
        elif stale or max_drift_pct > RECONCILIATION_WARNING_PCT:
            status = "warning"
        else:
            status = "matched"
        audit = {
            "audit_date": audit_date,
            "broker_snapshot_at": snapshot_at,
            "status": status,
            "asset_drift": round(asset_drift, 2),
            "cash_drift": round(cash_drift, 2),
            "holdings_drift": round(holdings_drift, 2),
            "position_mismatches": mismatches,
            "details": {
                "snapshot_age_hours": None if snapshot_age_hours == float("inf") else round(snapshot_age_hours, 1),
                "stale": stale,
                "max_drift_pct": round(max_drift_pct * 100, 3),
                "internal": {
                    "total_assets": _number(internal.get("total_value")),
                    "cash": _number(internal.get("cash")),
                    "holdings_value": _number(internal.get("holdings_value")),
                },
                "broker": {
                    "total_assets": broker_total,
                    "cash": broker_cash,
                    "holdings_value": broker_holdings,
                },
            },
        }

    if not dry_run:
        audit = db.save_reconciliation_audit(audit)
        task_key = "reconciliation:portfolio"
        if audit["status"] == "matched":
            db.resolve_operational_task(task_key)
        else:
            reason = audit.get("details", {}).get("reason")
            summary = "No broker snapshot is available" if reason else (
                f"Asset drift {audit['asset_drift']:+.2f}; "
                f"position mismatches {len(audit['position_mismatches'])}"
            )
            db.upsert_operational_task({
                "dedupe_key": task_key,
                "task_type": "reconciliation",
                "severity": "critical" if audit["status"] == "blocked" else "warning",
                "title": "Portfolio reconciliation requires review",
                "summary": summary,
                "details": audit,
            })
    return audit


def get_data_quality_summary(days: int = 30) -> dict[str, Any]:
    since = (date.today() - timedelta(days=max(1, days))).isoformat()
    raw = db.get_data_quality_overview(since)
    counts = raw.get("counts") or {}
    total = int(counts.get("total") or 0)
    high = int(counts.get("high_count") or 0)
    try:
        from auto_gate import diagnose_signal_settlements
        settlement_diagnostics = diagnose_signal_settlements()
    except Exception as exc:
        settlement_diagnostics = {"error": str(exc), "pending": 0, "due_blocked": 0}
    return {
        "window_days": days,
        "total": total,
        "high_confidence": high,
        "high_confidence_pct": round(high / total * 100, 1) if total else 0,
        "low_confidence": int(counts.get("low_count") or 0),
        "missing": int(counts.get("missing_count") or 0),
        "conflicts": int(counts.get("conflict_count") or 0),
        "warnings": raw.get("warnings") or [],
        "settlements": raw.get("settlements") or {},
        "settlement_diagnostics": settlement_diagnostics,
    }


def _task(
    key: str,
    task_type: str,
    severity: str,
    title: str,
    summary: str,
    *,
    code: str = "",
    details: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    return {
        "dedupe_key": key,
        "task_type": task_type,
        "severity": severity,
        "code": code,
        "title": title,
        "summary": summary,
        "details": details or {},
    }


def build_concentration_recovery_plan(
    broker: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Quantify concentration gaps for manual review without creating orders."""
    broker = broker if broker is not None else (db.get_latest_portfolio_reconciliation() or {})
    total_assets = _number(broker.get("total_assets"))
    positions = []
    invested_value = 0.0
    single_limit_value = total_assets * AUTO_SINGLE_POSITION_LIMIT_PCT / 100
    for position in broker.get("positions") or []:
        market_value = _number(position.get("market_value"))
        invested_value += market_value
        weight = market_value / total_assets * 100 if total_assets else 0
        positions.append({
            "code": str(position.get("code") or ""),
            "name": position.get("name") or get_stock_name(str(position.get("code") or "")),
            "market_value": round(market_value, 2),
            "current_weight_pct": round(weight, 2),
            "reference_single_limit_pct": AUTO_SINGLE_POSITION_LIMIT_PCT,
            "excess_value_above_single_limit": round(
                max(0.0, market_value - single_limit_value), 2
            ),
        })
    return {
        "status": "review_required" if any(
            item["excess_value_above_single_limit"] > 0 for item in positions
        ) else "within_reference",
        "total_assets": round(total_assets, 2),
        "current_invested_pct": round(
            invested_value / total_assets * 100, 2
        ) if total_assets else 0,
        "reference_auto_pool_pct": AUTO_POOL_LIMIT_PCT,
        "reference_auto_pool_value": round(total_assets * AUTO_POOL_LIMIT_PCT / 100, 2),
        "excess_value_above_auto_pool": round(
            max(0.0, invested_value - total_assets * AUTO_POOL_LIMIT_PCT / 100), 2
        ),
        "manual_confirmation_required": True,
        "creates_orders": False,
        "positions": positions,
    }


def _p0_validation_tasks() -> list[dict[str, Any]]:
    """Convert the P0 alpha validation report into operational tasks."""
    try:
        from alpha_validation import build_alpha_validation_report

        report = build_alpha_validation_report()
    except Exception as exc:
        return [_task(
            "p0:alpha_validation",
            "p0_validation",
            "critical",
            "P0 alpha validation could not run",
            f"alpha-validation error: {exc}",
            details={"error": str(exc)},
        )]

    verdict = str(report.get("verdict") or "")
    if verdict == "P0_PASS":
        return []

    severity = "critical" if verdict in {"P0_FAIL", "P0_DATA_INVALID"} else "warning"
    criteria = [
        {"key": item.get("key"), "status": item.get("status"), "label": item.get("label")}
        for item in report.get("criteria", [])
        if item.get("status") != "PASS"
    ]
    tasks = [_task(
        "p0:alpha_validation",
        "p0_validation",
        severity,
        "Resolve P0 alpha validation blockers",
        f"P0 verdict is {verdict}; automation remains gated",
        details={"verdict": verdict, "criteria": criteria},
    )]

    portfolio = report.get("portfolio") or {}
    broker_candidate = portfolio.get("broker_candidate") or {}
    broker_points = int(broker_candidate.get("points", portfolio.get("points", 0)) or 0)
    broker_required = int(broker_candidate.get("required_points", portfolio.get("required_points", 2)) or 2)
    if broker_points < broker_required:
        try:
            template = build_broker_snapshot_template()
        except Exception as exc:
            template = {"error": str(exc)}
        tasks.append(_task(
            "p0:broker_snapshot",
            "p0_validation",
            "critical",
            "Import fresh broker NAV evidence",
            f"Broker NAV evidence has {broker_points}/{broker_required} required points",
            details={
                "broker_points": broker_points,
                "broker_required": broker_required,
                "template": template,
            },
        ))

    data_quality = report.get("data_quality") or {}
    cashflow = data_quality.get("drawdown_cashflow") or {}
    if cashflow.get("status") == "BLOCK":
        tasks.append(_task(
            "p0:drawdown_cashflow",
            "p0_validation",
            "critical",
            "Reconcile drawdown-window cashflow",
            (
                f"Cashflow gap {cashflow.get('gap')} exceeds tolerance "
                f"{cashflow.get('tolerance')}"
            ),
            details={
                "drawdown_cashflow": cashflow,
                "drawdown_attribution": report.get("drawdown_attribution") or {},
                "template_command": "python3 cli.py cashflow-reconciliation-template --write",
                "lint_command": "python3 cli.py cashflow-reconciliation-lint <json-file>",
                "dry_run_import_command": "python3 cli.py cashflow-reconciliation <json-file> --dry-run",
                "import_command": "python3 cli.py cashflow-reconciliation <json-file>",
                "broker_snapshot_preferred": True,
            },
        ))

    samples = report.get("samples") or {}
    if int(samples.get("sample_count") or 0) < int(samples.get("required_sample_count") or 0):
        try:
            readiness = build_sample_readiness_report()
        except Exception as exc:
            readiness = {"status": "error", "error": str(exc)}
        missing_due_rows = readiness.get("missing_due_price_rows") or []
        sample_severity = "critical" if missing_due_rows else "warning"
        sample_summary = (
            f"Real BUY samples {samples.get('sample_count', 0)}/"
            f"{samples.get('required_sample_count', 0)}"
        )
        if missing_due_rows:
            sample_summary += f"; {len(missing_due_rows)} due price rows missing or invalid"
        tasks.append(_task(
            "p0:real_samples",
            "p0_validation",
            sample_severity,
            "Accumulate current-version real BUY samples",
            sample_summary,
            details={
                "samples": samples,
                "sample_readiness": readiness,
                "readiness_command": "python3 cli.py p0-sample-readiness",
                "record_command": (
                    readiness.get("next_record_command")
                    or "python3 cli.py record-real-data"
                ),
                "dry_run_record_command": (
                    readiness.get("dry_run_record_command")
                    or "python3 cli.py record-real-data --dry-run"
                ),
            },
        ))

    return tasks


def generate_risk_tasks(
    *,
    broker: Optional[dict[str, Any]] = None,
    quality: Optional[dict[str, Any]] = None,
    reconciliation: Optional[dict[str, Any]] = None,
    settlement: Optional[dict[str, Any]] = None,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """Derive review tasks from reconciled facts without changing trade logic."""
    broker = broker if broker is not None else (db.get_latest_portfolio_reconciliation() or {})
    quality = quality or get_data_quality_summary()
    reconciliation = reconciliation or db.get_latest_reconciliation_audit() or {}
    settlement = settlement or quality.get("settlement_diagnostics") or {}
    tasks: list[dict[str, Any]] = []
    active_keys: set[str] = set()

    broker_risk = assess_broker_risk(latest_snapshot=broker)
    daily_pct = _number(broker_risk.get("daily_profit_pct"))
    if daily_pct <= DAILY_LOSS_LOCK_PCT:
        tasks.append(_task(
            "risk:daily_loss", "risk", "critical", "Daily loss lock triggered",
            f"Broker daily P&L {daily_pct:.2f}% <= {DAILY_LOSS_LOCK_PCT:.0f}%",
            details={"daily_profit_pct": daily_pct, "threshold": DAILY_LOSS_LOCK_PCT},
        ))

    current_drawdown = broker_risk.get("drawdown_pct")
    if current_drawdown is not None and current_drawdown <= PORTFOLIO_DRAWDOWN_LOCK_PCT:
        tasks.append(_task(
            "risk:drawdown", "risk", "critical", "Portfolio drawdown lock triggered",
            f"Current drawdown {current_drawdown:.2f}% <= {PORTFOLIO_DRAWDOWN_LOCK_PCT:.0f}%",
            details={"drawdown_pct": current_drawdown, "threshold": PORTFOLIO_DRAWDOWN_LOCK_PCT},
        ))

    evidence_reasons = [
        reason for reason in broker_risk.get("reasons") or []
        if not reason.startswith(("broker_daily_loss", "broker_drawdown "))
    ]
    if evidence_reasons:
        tasks.append(_task(
            "risk:evidence",
            "risk",
            "critical",
            "Refresh broker risk evidence",
            "; ".join(evidence_reasons),
            details=broker_risk,
        ))

    total_assets = _number(broker.get("total_assets"))
    stocks = {item["code"]: item for item in db.load_all_stocks() if item.get("code") != "CASH"}
    for position in broker.get("positions") or []:
        code = str(position.get("code") or "")
        name = position.get("name") or get_stock_name(code)
        weight = _number(position.get("market_value")) / total_assets * 100 if total_assets else 0
        profit_pct = _number(position.get("profit_pct"))
        if weight > AUTO_SINGLE_POSITION_LIMIT_PCT:
            tasks.append(_task(
                f"risk:concentration:{code}", "risk", "warning", f"Review concentration: {name}",
                f"Position weight {weight:.1f}% exceeds the automatic-position reference limit {AUTO_SINGLE_POSITION_LIMIT_PCT:.0f}%",
                code=code,
                details={"weight_pct": round(weight, 2), "reference_limit_pct": AUTO_SINGLE_POSITION_LIMIT_PCT, "book": "reconciled_manual"},
            ))
        if profit_pct <= -6.0:
            tasks.append(_task(
                f"risk:hard_stop:{code}", "risk", "critical", f"Review hard stop: {name}",
                f"Position return {profit_pct:.2f}% is at or below -6%",
                code=code,
                details={"profit_pct": profit_pct, "threshold": -6.0},
            ))
        buy_date = str((stocks.get(code) or {}).get("buy_date") or "")
        if buy_date:
            holding_days = trading_days_between(buy_date, date.today().isoformat())
            if holding_days >= MAX_HOLDING_TRADING_DAYS:
                tasks.append(_task(
                    f"risk:max_holding:{code}", "risk", "warning", f"Review holding period: {name}",
                    f"Held for {holding_days} trading days; limit is {MAX_HOLDING_TRADING_DAYS}",
                    code=code,
                    details={"holding_trading_days": holding_days, "limit": MAX_HOLDING_TRADING_DAYS},
                ))

    quality_issues = quality.get("low_confidence", 0) + quality.get("missing", 0) + quality.get("conflicts", 0)
    if quality_issues:
        tasks.append(_task(
            "data_quality:review", "data_quality", "warning", "Review market-data quality",
            f"{quality_issues} low-confidence, missing, or conflicting records in the current window",
            details=quality,
        ))

    due_blocked = int(settlement.get("due_blocked") or 0)
    if due_blocked:
        reason_counts = settlement.get("reason_counts") or {}
        reasons = ", ".join(
            f"{key}={value}" for key, value in sorted(reason_counts.items())
            if key != "awaiting_exit_date"
        )
        tasks.append(_task(
            "data_quality:settlement_backlog",
            "data_quality",
            "critical",
            "Resolve due signal settlement backlog",
            f"{due_blocked} due signals are blocked" + (f"; {reasons}" if reasons else ""),
            details=settlement,
        ))

    if reconciliation.get("status") in {"blocked", "warning"}:
        tasks.append(_task(
            "reconciliation:portfolio", "reconciliation",
            "critical" if reconciliation.get("status") == "blocked" else "warning",
            "Portfolio reconciliation requires review",
            f"Reconciliation status: {reconciliation.get('status')}",
            details=reconciliation,
        ))

    tasks.extend(_p0_validation_tasks())

    for item in tasks:
        active_keys.add(item["dedupe_key"])
        if not dry_run:
            db.upsert_operational_task(item)

    known_prefixes = (
        "risk:daily_loss", "risk:drawdown", "risk:evidence", "risk:concentration:",
        "risk:hard_stop:", "risk:max_holding:", "data_quality:review",
        "data_quality:settlement_backlog", "p0:",
    )
    if not dry_run:
        for existing in db.get_operational_tasks(status="open", limit=200):
            key = existing["dedupe_key"]
            if key.startswith(known_prefixes) and key not in active_keys:
                db.resolve_operational_task(key)
        return db.get_operational_tasks(status="open", limit=50)
    return tasks


def _order_price(order: dict[str, Any]) -> float:
    if _number(order.get("price")) > 0:
        return _number(order.get("price"))
    shares = int(_number(order.get("shares")))
    amount = _number(order.get("amount") or order.get("estimated_proceeds"))
    return amount / shares if shares > 0 else 0


def _paper_integrity(portfolio: dict[str, Any]) -> dict[str, Any]:
    invalid_positions = [
        {
            "code": item.get("code", ""),
            "avg_cost": _number(item.get("avg_cost")),
            "shares": int(_number(item.get("shares"))),
        }
        for item in portfolio.get("positions") or []
        if _number(item.get("avg_cost")) <= 0 or _number(item.get("shares")) <= 0
    ]
    valid = (
        not invalid_positions
        and _number(portfolio.get("cash")) >= 0
        and _number(portfolio.get("total_value")) >= 0
    )
    return {"valid": valid, "invalid_positions": invalid_positions}


def rebuild_paper_baseline(reason: str = "rebuild from broker-reconciled portfolio") -> dict[str, Any]:
    """Archive a legacy PAPER ledger, then seed a fresh reconciled baseline."""
    archive = db.archive_and_clear_paper_account(reason)
    import paper_trader

    paper_trader._paper_instance = None
    trader = paper_trader.get_paper_trader()
    portfolio = trader.get_paper_portfolio()
    integrity = _paper_integrity(portfolio)
    if integrity["valid"]:
        db.resolve_operational_task("paper:invalid_baseline")
    else:
        db.upsert_operational_task({
            "dedupe_key": "paper:invalid_baseline",
            "task_type": "paper",
            "severity": "critical",
            "title": "PAPER baseline requires rebuild",
            "summary": "Rebuilt baseline still contains invalid facts",
            "details": integrity,
        })
    return {"archive": archive, "integrity": integrity, "portfolio": portfolio}


def run_paper_drill(
    *,
    plan: Optional[dict[str, Any]] = None,
    trader: Any = None,
    run_date: Optional[str] = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Execute an idempotent PAPER-only rehearsal through canonical states."""
    run_date = run_date or date.today().isoformat()

    if plan is None:
        from auto_execute import generate_execution_plan
        plan = generate_execution_plan(dry_run=True)
    plan = {**plan, "date": plan.get("date") or run_date}

    orders: list[tuple[str, dict[str, Any]]] = []
    orders.extend(("SELL", item) for item in plan.get("sells", []))
    orders.extend(("BUY", item) for item in plan.get("buys", [])[:MAX_PAPER_BUYS_PER_DAY])
    preview = []
    for action, order in orders:
        preview.append({
            "code": order.get("code", ""),
            "action": action,
            "price": round(_order_price(order), 4),
            "shares": int(_number(order.get("shares"))),
            "amount": round(_number(order.get("amount") or order.get("estimated_proceeds")), 2),
        })
    if dry_run:
        return {
            "run_date": run_date,
            "status": "dry_run",
            "plan": plan,
            "orders_generated": len(preview),
            "orders_filled": 0,
            "results": preview,
        }

    from paper_trader import get_paper_trader

    trader = trader or get_paper_trader()
    baseline = trader.get_paper_portfolio()
    integrity = _paper_integrity(baseline)
    if not integrity["valid"]:
        blocked = db.save_paper_drill_run({
            "run_date": run_date,
            "status": "blocked_invalid_baseline",
            "plan": plan,
            "results": [{"state": "rejected", "reason": "invalid PAPER baseline", **integrity}],
            "orders_generated": len(preview),
            "orders_filled": 0,
        })
        db.upsert_operational_task({
            "dedupe_key": "paper:invalid_baseline",
            "task_type": "paper",
            "severity": "critical",
            "title": "PAPER baseline requires rebuild",
            "summary": "Invalid cost or share facts detected; simulated execution is blocked",
            "details": integrity,
        })
        blocked["portfolio"] = baseline
        return blocked
    db.resolve_operational_task("paper:invalid_baseline")

    existing = db.get_latest_paper_drill_run()
    if existing and existing.get("run_date") == run_date and existing.get("status") == "completed":
        return {**existing, "idempotent": True}

    from auto_gate import create_order_state

    results = []
    filled = 0
    for item in preview:
        code = item["code"]
        action = item["action"]
        key = f"paper:{run_date}:{action}:{code}:{item['shares']}:{item['amount']:.2f}"
        latest = db.get_order_state_by_key(key)
        if latest and latest.get("state") == "filled":
            results.append({**item, "state": "filled", "idempotent": True})
            filled += 1
            continue
        if item["price"] <= 0:
            create_order_state(code, action, "rejected", reason="missing PAPER execution price", idempotency_key=key)
            results.append({**item, "state": "rejected", "reason": "missing price"})
            continue
        for state in ("generated", "pending_confirm", "confirmed", "submitted"):
            create_order_state(
                code, action, state, price=item["price"], shares=item["shares"],
                amount=item["amount"], reason="daily PAPER drill", idempotency_key=key,
            )
        execution = trader.execute_signal(
            code, action.lower(), item["price"], shares=item["shares"],
            amount=item["amount"], reason="daily PAPER drill",
        )
        final_state = "rejected" if execution.get("status") == "error" else "filled"
        create_order_state(
            code, action, final_state, price=item["price"], shares=item["shares"],
            amount=item["amount"], reason=str(execution.get("reason") or "PAPER fill"),
            idempotency_key=key,
        )
        results.append({**item, "state": final_state, "execution": execution})
        filled += int(final_state == "filled")

    portfolio = trader.get_paper_portfolio()
    db.save_paper_nav_snapshot({"date": run_date, **portfolio})
    saved = db.save_paper_drill_run({
        "run_date": run_date,
        "status": "completed",
        "plan": plan,
        "results": results,
        "orders_generated": len(preview),
        "orders_filled": filled,
    })
    saved["portfolio"] = portfolio
    return saved


def get_history_analytics(limit: int = 252) -> dict[str, Any]:
    rows = db.get_operations_analytics_rows(limit=limit)
    broker_nav = rows.get("broker_nav") or []
    source = "broker_reconciled" if broker_nav else "diagnostic_nav_history"
    source_rows = broker_nav or rows.get("nav") or []
    nav_points = []
    peak = 0.0
    max_drawdown = 0.0
    for row in source_rows:
        value = _number(row.get("total_value"))
        peak = max(peak, value)
        drawdown = (value - peak) / peak * 100 if peak else 0
        max_drawdown = min(max_drawdown, drawdown)
        nav_points.append({
            "date": row.get("date"),
            "value": round(value, 2),
            "cash": round(_number(row.get("cash")), 2),
            "holdings": round(_number(row.get("holdings_value")), 2),
            "return_pct": round(_number(row.get("profit_pct")), 2),
            "drawdown_pct": round(drawdown, 2),
        })
    current_drawdown = nav_points[-1]["drawdown_pct"] if nav_points else 0
    current_return = nav_points[-1]["return_pct"] if nav_points else 0
    versions = []
    for row in rows.get("versions") or []:
        versions.append({
            **row,
            "win_rate": round(_number(row.get("win_rate")) * 100, 1),
            "excess_win_rate": round(_number(row.get("excess_win_rate")) * 100, 1),
            "avg_return_5d": round(_number(row.get("avg_return_5d")), 2),
            "avg_excess_5d": round(_number(row.get("avg_excess_5d")), 2),
        })
    return {
        "nav": {
            "points": nav_points,
            "source": source,
            "diagnostic_only": source != "broker_reconciled",
            "point_count": len(nav_points),
            "current_return_pct": current_return,
            "current_drawdown_pct": current_drawdown,
            "max_drawdown_pct": round(max_drawdown, 2),
            "peak_value": round(peak, 2),
        },
        "gates": rows.get("gates") or [],
        "strategy_versions": versions,
        "paper": rows.get("paper") or [],
    }


def get_dashboard_data() -> dict[str, Any]:
    broker = db.get_latest_portfolio_reconciliation() or {}
    try:
        current_internal = _portfolio_facts()
    except Exception as exc:
        current_internal = {"total_value": 0, "holdings_value": 0, "cash": 0, "positions": []}
        current_internal["error"] = str(exc)
    reconciliation = run_reconciliation(
        internal=current_internal,
        broker=broker,
        dry_run=True,
    )
    quality = get_data_quality_summary()
    persisted_tasks = db.get_operational_tasks(status="open", limit=30)
    derived_tasks = generate_risk_tasks(
        broker=broker,
        quality=quality,
        reconciliation=reconciliation,
        dry_run=True,
    )
    task_by_key = {task["dedupe_key"]: task for task in persisted_tasks}
    task_by_key.update({task["dedupe_key"]: task for task in derived_tasks})
    tasks = sorted(
        task_by_key.values(),
        key=lambda task: (task.get("severity") != "critical", task.get("dedupe_key", "")),
    )
    return {
        "reconciliation": reconciliation,
        "quality": quality,
        "tasks": tasks,
        "paper": db.get_latest_paper_drill_run(),
        "operations_run": db.get_latest_operations_run(),
        "analytics": get_history_analytics(),
        "risk_assessment": assess_broker_risk(latest_snapshot=broker),
        "risk_recovery": build_concentration_recovery_plan(broker),
    }


def run_operations_cycle(
    *,
    dry_run: bool = True,
    plan: Optional[dict[str, Any]] = None,
    include_market_data: bool = False,
) -> dict[str, Any]:
    """Run the complete daily loop; live effects are limited to local audit/PAPER state."""
    now = datetime.now()
    mode = "dry_run" if dry_run else "paper"
    run_id = f"{now.date().isoformat()}:{mode}"
    steps: dict[str, Any] = {}
    status = "completed"
    try:
        if include_market_data:
            from auto_gate import evaluate_auto_gate, record_real_data, settle_pending_signal_outcomes
            steps["market_data"] = record_real_data(dry_run=dry_run)
            steps["settlement"] = settle_pending_signal_outcomes(dry_run=dry_run)
            steps["gate"] = evaluate_auto_gate(explain=True)
        reconciliation = run_reconciliation(now=now, dry_run=dry_run)
        quality = get_data_quality_summary()
        paper = run_paper_drill(plan=plan, run_date=now.date().isoformat(), dry_run=dry_run)
        tasks = generate_risk_tasks(
            quality=quality,
            reconciliation=reconciliation,
            dry_run=dry_run,
        )
        steps.update({
            "reconciliation": reconciliation,
            "quality": quality,
            "tasks": tasks,
            "paper": paper,
        })
    except Exception as exc:
        status = "partial_failure"
        steps["error"] = str(exc)
        if dry_run:
            raise
    result = {
        "run_id": run_id,
        "run_date": now.date().isoformat(),
        "mode": mode,
        "status": status,
        "steps": steps,
        "completed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    if not dry_run:
        return db.save_operations_run(result)
    return result
