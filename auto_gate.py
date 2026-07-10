"""Real-data gate, compliance state, and controlled execution guard."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Optional, Any

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
BENCHMARK_CODES = tuple(sorted({item["code"] for item in BENCHMARK_BY_TIER.values()}))
LEGACY_STRATEGY_VERSION = "legacy_unversioned"
BROKER_SNAPSHOT_STALE_HOURS = 36
BROKER_DRAWDOWN_MIN_POINTS = 2

CONSECUTIVE_LOSS_RULE = {
    "mode": "OR",
    "lookback": 10,
    "max_consecutive": 3,
}


def default_strategy_config() -> dict[str, Any]:
    """Return every rule that can change whether old samples are reusable."""
    from config import STOCK_MAP, BENCHMARK_UNIVERSE_SIZE
    from kernel_freeze import all_frozen_ids
    return {
        "strategy_family": "serenity_real_data_gate",
        "version_schema": 3,
        "tradable_universe_prefixes": ["000", "002", "600", "601", "603", "605"],
        "stock_pool": sorted(STOCK_MAP.keys()),
        "benchmark_universe_size": BENCHMARK_UNIVERSE_SIZE,
        "kernel_frozen_modules": sorted(all_frozen_ids()),
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
        "signal_date_rule": "exchange_trading_days_only",
        "benchmark_rule": {
            "tier_1_to_3": "CSI500",
            "tier_4": "HS300",
            "same_interval_as_stock": True,
            "live_collection_required": True,
            "historical_backfill": "diagnostic_only",
        },
        "paper_sample_rule": "diagnostic_only_not_gate_eligible",
        "executable_sample_filters": [
            "settlement_status=settled",
            "executable_status=executable",
            "stock_and_benchmark_data_quality=high",
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
            "drawdown_evidence_source": "broker_reconciliations",
            "broker_drawdown_min_points": BROKER_DRAWDOWN_MIN_POINTS,
            "broker_snapshot_stale_hours": BROKER_SNAPSHOT_STALE_HOURS,
        },
        "p0_alpha_validation_gate": {
            "required_verdict": "P0_PASS",
            "not_proven_state": "PAPER",
            "hard_lock_verdicts": ["P0_FAIL", "P0_DATA_INVALID"],
        },
        "order_states": list(ORDER_STATES),
        "compliance_gate": "SEMI_AUTO requires compliance_status.status == approved",
    }


def compute_strategy_hash(config: Optional[dict[str, Any]] = None) -> str:
    payload = json.dumps(config or default_strategy_config(), sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def estimate_net_expected_return(
    code: str,
    action: str,
    *,
    min_samples: int = 5,
    expected_cost_pct: float = 0.302,
) -> dict[str, Any]:
    """估算当前版本下同类信号的 5 日扣费后期望收益。

    优先使用已结算的高质量样本；数据不足时回退到 outcome_5d。
    """
    import db

    conn = db.get_conn()
    try:
        # 第一优先级：strategy_versions 过滤的已结算样本
        version_row = conn.execute(
            "SELECT version FROM strategy_versions WHERE is_active=1 "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        version = version_row["version"] if version_row else None

        rows = []
        if version:
            rows = conn.execute(
                "SELECT return_5d FROM signal_log WHERE code=? AND action=? "
                "AND strategy_version=? AND settlement_status='settled' "
                "AND return_5d IS NOT NULL "
                "ORDER BY date DESC LIMIT 50",
                (code, action, version),
            ).fetchall()

        # 第二优先级：任意版本的 return_5d
        if not rows:
            rows = conn.execute(
                "SELECT return_5d FROM signal_log WHERE code=? AND action=? "
                "AND return_5d IS NOT NULL "
                "ORDER BY date DESC LIMIT 50",
                (code, action),
            ).fetchall()

        # 第三优先级：outcome_5d（旧字段名）
        if not rows:
            rows = conn.execute(
                "SELECT outcome_5d FROM signal_log WHERE code=? AND action=? "
                "AND outcome_5d IS NOT NULL "
                "ORDER BY date DESC LIMIT 50",
                (code, action),
            ).fetchall()
    finally:
        conn.close()

    values = []
    for row in rows:
        # Row can be sqlite3.Row or tuple; extract first column safely
        try:
            val = row[0]
        except (IndexError, TypeError):
            continue
        if val is not None:
            try:
                values.append(float(val))
            except (ValueError, TypeError):
                pass

    if not values:
        return {"ready": False, "samples": 0, "gross_pct": None, "net_pct": None}
    gross = sum(values) / len(values)
    net = gross - expected_cost_pct
    return {
        "ready": len(values) >= min_samples and net > 0,
        "samples": len(values),
        "gross_pct": round(gross, 4),
        "net_pct": round(net, 4),
        "expected_cost_pct": expected_cost_pct,
        "min_samples": min_samples,
    }


def ensure_current_strategy_version(
    reset_reason: str = "auto hash check",
    change_source: str = "",
    *,
    force_new_major: bool = False
) -> dict[str, Any]:
    """确保当前配置有独立激活纪元；可显式强制切断旧样本。

    Args:
        reset_reason: 为何触发版本变更（如 'decision audit identity check'）
        change_source: 本次 hash 变化对应的配置项变更描述
                       （如 'stock_pool_change: +601318'，
                         'weight_adjuster: factor_weight changed'）
    """
    import db

    db.init_db()
    config = default_strategy_config()
    config_hash = compute_strategy_hash(config)
    conn = db.get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM strategy_versions WHERE is_active=1 "
            "ORDER BY created_at DESC, id DESC LIMIT 1"
        ).fetchone()
        if row and row["config_hash"] == config_hash and not force_new_major:
            return dict(row)

        last = conn.execute("SELECT COALESCE(MAX(major), 0) AS m FROM strategy_versions").fetchone()
        major = int(last["m"] or 0) + 1
        version = f"v{major}.0"
        conn.execute("UPDATE strategy_versions SET is_active=0")
        conn.execute(
            """
            INSERT INTO strategy_versions
                (version, major, minor, config_hash, config_json, reset_reason, change_source, is_active)
            VALUES (?, ?, 0, ?, ?, ?, ?, 1)
            """,
            (version, major, config_hash, json.dumps(config, ensure_ascii=False),
             reset_reason, change_source),
        )
        conn.commit()
        return {
            "version": version,
            "major": major,
            "minor": 0,
            "config_hash": config_hash,
            "config_json": json.dumps(config, ensure_ascii=False),
            "reset_reason": reset_reason,
            "change_source": change_source,
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


def classify_backtest_price_source(adjustment_mode: Optional[str]) -> str:
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


def _fetch_paper_samples(conn, limit: int) -> list[dict[str, Any]]:
    """从纸面交易中提取样本 — 纸面买入后 5 个交易日结算"""
    rows = conn.execute(
        """
        SELECT pt.code, pt.date, pt.price, pt.quantity, pt.action,
               COALESCE(ph.close, pt.price) as current_price
        FROM paper_trades pt
        LEFT JOIN price_history ph ON ph.code=pt.code AND ph.date >= pt.date
        WHERE pt.action='buy'
        ORDER BY pt.date DESC, pt.rowid DESC
        LIMIT ?
        """,
        (limit * 3,),
    ).fetchall()

    samples: list[dict[str, Any]] = []
    for r in rows:
        rdict = dict(r)
        # 用历史价格模拟 5 日收益
        price_rows = conn.execute(
            "SELECT close FROM price_history WHERE code=? AND date > ? ORDER BY date ASC LIMIT 5",
            (rdict["code"], rdict["date"]),
        ).fetchall()
        if len(price_rows) >= 3:  # 至少需要 3 个有效数据点
            exit_price = float(price_rows[-1]["close"])
            return_5d = (exit_price - float(rdict["price"])) / float(rdict["price"])
            samples.append({
                "code": rdict["code"],
                "date": rdict["date"],
                "action": "BUY",
                "return_5d": round(return_5d * 100, 2),
                "outcome_5d": round(return_5d * 100, 2),
                "excess_5d": round(return_5d * 100, 2),  # 简化：超额收益=绝对收益
            })

    return samples[:limit]


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


def assess_broker_risk(
    *,
    conn=None,
    latest_snapshot: Optional[dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Assess executable risk only from timestamped broker account evidence."""
    import db

    own_connection = conn is None
    conn = conn or db.get_conn()
    now = now or datetime.now()
    try:
        rows = conn.execute(
            """
            SELECT snapshot_at,total_assets,cash,holdings_value,daily_profit_pct
            FROM portfolio_reconciliations
            ORDER BY snapshot_at ASC,id ASC
            """
        ).fetchall()
        snapshots = [dict(row) for row in rows]
    except Exception as exc:
        return {
            "source": "broker_reconciliations",
            "verified": False,
            "lock_required": True,
            "point_count": 0,
            "drawdown_pct": None,
            "daily_profit_pct": None,
            "snapshot_age_hours": None,
            "reasons": [f"broker_risk_evidence_unavailable: {exc}"],
        }
    finally:
        if own_connection:
            conn.close()

    if latest_snapshot:
        snapshot_at = str(latest_snapshot.get("snapshot_at") or "")
        snapshots = [row for row in snapshots if row.get("snapshot_at") != snapshot_at]
        snapshots.append({
            "snapshot_at": snapshot_at,
            "total_assets": latest_snapshot.get("total_assets"),
            "cash": latest_snapshot.get("cash"),
            "holdings_value": latest_snapshot.get("holdings_value"),
            "daily_profit_pct": latest_snapshot.get("daily_profit_pct"),
        })
        snapshots.sort(key=lambda item: str(item.get("snapshot_at") or ""))

    reasons: list[str] = []
    if not snapshots:
        reasons.append("broker_snapshot_missing")
        return {
            "source": "broker_reconciliations",
            "verified": False,
            "lock_required": True,
            "point_count": 0,
            "drawdown_pct": None,
            "daily_profit_pct": None,
            "snapshot_age_hours": None,
            "reasons": reasons,
        }

    latest = snapshots[-1]
    try:
        snapshot_time = datetime.fromisoformat(str(latest.get("snapshot_at") or ""))
        age_hours = max(0.0, (now - snapshot_time).total_seconds() / 3600)
    except ValueError:
        age_hours = float("inf")
    if age_hours > BROKER_SNAPSHOT_STALE_HOURS:
        reasons.append(
            f"broker_snapshot_stale {age_hours:.1f}h > {BROKER_SNAPSHOT_STALE_HOURS}h"
        )

    daily_profit_pct = float(latest.get("daily_profit_pct") or 0)
    if daily_profit_pct <= -2.0:
        reasons.append(f"broker_daily_loss {daily_profit_pct:.2f}% <= -2.00%")

    valid_values = [float(item.get("total_assets") or 0) for item in snapshots]
    valid_values = [value for value in valid_values if value > 0]
    drawdown_pct = None
    if len(valid_values) < BROKER_DRAWDOWN_MIN_POINTS:
        reasons.append(
            f"broker_drawdown_history {len(valid_values)} < {BROKER_DRAWDOWN_MIN_POINTS}"
        )
    else:
        peak = max(valid_values)
        drawdown_pct = (valid_values[-1] - peak) / peak * 100
        if drawdown_pct <= -6.0:
            reasons.append(f"broker_drawdown {drawdown_pct:.2f}% <= -6.00%")

    verified = (
        len(valid_values) >= BROKER_DRAWDOWN_MIN_POINTS
        and age_hours <= BROKER_SNAPSHOT_STALE_HOURS
    )
    return {
        "source": "broker_reconciliations",
        "verified": verified,
        "lock_required": bool(reasons),
        "point_count": len(valid_values),
        "drawdown_pct": None if drawdown_pct is None else round(drawdown_pct, 2),
        "daily_profit_pct": round(daily_profit_pct, 2),
        "snapshot_age_hours": None if age_hours == float("inf") else round(age_hours, 1),
        "latest_snapshot_at": latest.get("snapshot_at"),
        "reasons": reasons,
    }


def _risk_lock_state(conn) -> tuple[bool, list[str]]:
    assessment = assess_broker_risk(conn=conn)
    return bool(assessment["lock_required"]), list(assessment["reasons"])


def assess_p0_alpha_validation(conn=None) -> dict[str, Any]:
    """Assess whether automation is allowed by the P0 alpha evidence package."""
    import db

    own_connection = conn is None
    conn = conn or db.get_conn()
    try:
        from alpha_validation import build_alpha_validation_report

        report = build_alpha_validation_report(conn)
    except Exception as exc:
        return {
            "source": "alpha_validation",
            "verified": False,
            "hard_lock_required": True,
            "verdict": "ERROR",
            "reasons": [f"p0_alpha_validation_error: {exc}"],
            "criteria": [],
        }
    finally:
        if own_connection:
            conn.close()

    verdict = report.get("verdict", "")
    config = default_strategy_config()["p0_alpha_validation_gate"]
    required = config["required_verdict"]
    hard_lock_verdicts = set(config["hard_lock_verdicts"])
    reasons: list[str] = []
    if verdict != required:
        reasons.append(f"p0_alpha_validation {verdict} != {required}")
    blocking_criteria = [
        {"key": item.get("key"), "status": item.get("status")}
        for item in report.get("criteria", [])
        if item.get("status") != "PASS"
    ]
    return {
        "source": "alpha_validation",
        "verified": verdict == required,
        "hard_lock_required": verdict in hard_lock_verdicts or verdict == "ERROR",
        "verdict": verdict,
        "reasons": reasons,
        "criteria": blocking_criteria,
    }


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
        paper_samples = _fetch_paper_samples(conn, required)
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
        risk_assessment = assess_broker_risk(conn=conn)
        risk_locked = bool(risk_assessment["lock_required"])
        risk_reasons = list(risk_assessment["reasons"])
        p0_assessment = assess_p0_alpha_validation(conn=conn)
        p0_reasons = list(p0_assessment["reasons"])
        p0_hard_locked = bool(p0_assessment["hard_lock_required"])
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
        if p0_reasons:
            reasons.extend(p0_reasons)

        gate_passed = sample_count >= required and not reasons
        if risk_locked or p0_hard_locked:
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
            "risk_assessment": risk_assessment,
            "p0_alpha_validation": p0_assessment,
            "reasons": reasons,
            "explain": {
                "date_distribution": _date_distribution(samples),
                "tier_distribution": _sector_distribution(samples),
                "latest_10": samples[:10],
                "paper_sample_count": len(paper_samples),
                "paper_date_distribution": _date_distribution(paper_samples),
                "paper_tier_distribution": _sector_distribution(paper_samples),
                "paper_latest_10": paper_samples[:10],
                "paper_note": "paper samples are diagnostic only and are not counted by the real-data gate",
                "consecutive_loss_rule": dict(CONSECUTIVE_LOSS_RULE),
                "wilson_note": "n=50 at 60% win rate does not clear the 50% Wilson lower-bound gate",
            } if explain else {},
        }
        _persist_gate_result(conn, result)
        return result
    finally:
        conn.close()


def _persist_gate_result(conn, result: dict[str, Any]) -> None:
    # v5.5 UPSERT: 用 INSERT OR REPLACE + 唯一索引防止重复, 替代 DELETE+INSERT
    # 如果 date+strategy_version 已存在则更新, 否则插入
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


def _choose_price_record(
    source_rows: dict[str, dict[str, Any]],
) -> tuple[Optional[dict[str, Any]], str, str, float, str]:
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


def record_real_data(
    dry_run: bool = False,
    codes: Optional[list[str]] = None,
    *,
    as_of: Optional[str] = None,
) -> dict[str, Any]:
    import db
    from data_engine import fetch_realtime

    db.init_db()
    collection_date = date.fromisoformat(as_of) if as_of else date.today()
    collection_date_str = collection_date.isoformat()
    if collection_date > date.today() and not dry_run:
        return {
            "skipped": True,
            "skip_reason": "future_as_of_requires_dry_run",
            "date": collection_date_str,
            "dry_run": dry_run,
            "count": 0,
            "saved": 0,
            "low_quality": [],
            "missing": [],
            "source_errors": {},
            "records": [],
        }
    if not is_trading_day(collection_date):
        return {
            "skipped": True,
            "skip_reason": "non_trading_day",
            "date": collection_date_str,
            "dry_run": dry_run,
            "count": 0,
            "saved": 0,
            "low_quality": [],
            "missing": [],
            "source_errors": {},
            "records": [],
        }
    requested = list(codes) if codes is not None else _default_real_data_collection_codes()
    codes = list(dict.fromkeys(
        str(code) for code in requested
        if str(code).startswith(("000", "002", "600", "601", "603", "605"))
    ))
    source_payload: dict[str, dict[str, dict[str, Any]]] = {}
    errors: dict[str, str] = {}

    for source in ("tencent", "sina"):
        try:
            rows = fetch_realtime(codes, source=source)
            source_payload[source] = {r["code"]: r for r in rows}
        except Exception as exc:
            errors[source] = str(exc)
            source_payload[source] = {}

    primary_covered = all(
        any(code in source_payload.get(source, {}) for source in ("tencent", "sina"))
        for code in codes
    )
    if not primary_covered:
        try:
            rows = fetch_realtime(codes, source="akshare")
            source_payload["akshare"] = {r["code"]: r for r in rows}
        except Exception as exc:
            errors["akshare"] = str(exc)
            source_payload["akshare"] = {}

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
            source_date = str(chosen.get("date") or "")
            warning_parts = [warning] if warning else []
            date_mismatch = bool(source_date and source_date != collection_date_str)
            if date_mismatch:
                quality = "low"
                warning_parts.append(
                    f"source date {source_date} != collection date {collection_date_str}"
                )
            payload = {
                "code": code,
                "date": collection_date_str,
                "source_date": source_date,
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
            warning = "; ".join(warning_parts)
            records.append({
                **payload,
                "warning": warning,
                "conflict_pct": conflict,
                "source_date_mismatch": date_mismatch,
            })
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
        "date": collection_date_str,
        "count": len(records),
        "saved": 0 if dry_run else len([r for r in records if r.get("quality_status") != "missing"]),
        "low_quality": [r for r in records if r.get("quality_status") == "low"],
        "missing": [r for r in records if r.get("quality_status") == "missing"],
        "source_date_mismatches": [r for r in records if r.get("source_date_mismatch")],
        "source_errors": errors,
        "records": records,
    }


def _default_real_data_collection_codes() -> list[str]:
    """Return the default live quote universe required for P0 sample settlement."""
    import db

    requested = [*ALL_CODES, *BENCHMARK_CODES]
    conn = db.get_conn()
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT code FROM signal_log
            WHERE action IN ('STRONG_BUY', 'BUY', 'CAUTION_BUY')
              AND COALESCE(settlement_status, 'pending') IN ('', 'pending', 'unknown')
            ORDER BY code
            """
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
        code = str(row["code"] or "")
        if not code:
            continue
        requested.append(code)
        tier = int(STOCK_MAP.get(code, {}).get("tier", 2))
        requested.append(BENCHMARK_BY_TIER.get(tier, BENCHMARK_BY_TIER[2])["code"])
    return list(dict.fromkeys(requested))


def _price_row(conn, code: str, date_str: str) -> Optional[dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM price_history WHERE code=? AND date=?",
        (code, date_str),
    ).fetchone()
    return dict(row) if row else None


def _settlement_context(
    conn,
    signal: dict[str, Any],
    as_of: str,
    current_version: str,
) -> dict[str, Any]:
    signal_date = signal["date"]
    entry_date = add_trading_days(signal_date, 1)
    exit_date = add_trading_days(signal_date, 6)
    strategy_version = (signal.get("strategy_version") or "").strip()
    tier = int(STOCK_MAP.get(signal["code"], {}).get("tier", 2))
    benchmark = BENCHMARK_BY_TIER.get(tier, BENCHMARK_BY_TIER[2])
    elapsed = trading_days_between(signal_date, as_of)
    context: dict[str, Any] = {
        "id": signal["id"],
        "code": signal["code"],
        "signal_date": signal_date,
        "entry_date": entry_date,
        "exit_date": exit_date,
        "benchmark_code": benchmark["code"],
        "strategy_version": strategy_version or LEGACY_STRATEGY_VERSION,
        "current_strategy_version": current_version,
        "trading_days_elapsed": elapsed,
        "due": exit_date <= as_of,
        "reasons": [],
    }
    if not is_trading_day(date.fromisoformat(signal_date)):
        context["reasons"].append("non_trading_signal_date")
    if not context["due"]:
        context["reasons"].append("awaiting_exit_date")
        context["blocking_reason"] = "awaiting_exit_date"
        context["ready_to_settle"] = False
        return context

    rows = {
        "stock_entry": _price_row(conn, signal["code"], entry_date),
        "stock_exit": _price_row(conn, signal["code"], exit_date),
        "benchmark_entry": _price_row(conn, benchmark["code"], entry_date),
        "benchmark_exit": _price_row(conn, benchmark["code"], exit_date),
    }
    for key, row in rows.items():
        if row is None:
            context["reasons"].append(f"missing_{key}")
        elif float(row.get("open") or 0) <= 0:
            context["reasons"].append(f"invalid_{key}_open")

    complete_rows = [row for row in rows.values() if row is not None]
    if len(complete_rows) == len(rows):
        adjustments = {
            (row.get("adjustment_mode") or "raw").lower()
            for row in complete_rows
        }
        if any(classify_backtest_price_source(mode) != "gate_eligible" for mode in adjustments):
            context["reasons"].append("adjusted_price_source")
        qualities = {(row.get("quality_status") or "unknown").lower() for row in complete_rows}
        if qualities != {"high"}:
            context["reasons"].append("low_confidence_price")
        context["adjustment_mode"] = "raw" if adjustments <= {"raw", "unadjusted"} else sorted(adjustments)[0]
        context["data_quality"] = "high" if qualities == {"high"} else "low"

    market_blockers = [
        reason for reason in context["reasons"]
        if reason.startswith(("missing_", "invalid_"))
    ]
    if not strategy_version:
        context["reasons"].append("legacy_unversioned")
    context["ready_to_settle"] = not market_blockers
    context["blocking_reason"] = market_blockers[0] if market_blockers else (
        "legacy_unversioned" if not strategy_version else "ready"
    )
    context["_rows"] = rows
    return context


def _public_settlement_context(context: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        **{key: value for key, value in context.items() if key != "_rows"},
        **extra,
    }


def _settlement_reason_counts(details: list[dict[str, Any]]) -> dict[str, int]:
    return dict(Counter(
        reason
        for item in details
        for reason in item.get("reasons", [])
    ))


def diagnose_signal_settlements(as_of: Optional[str] = None) -> dict[str, Any]:
    """Explain every pending sample without mutating the audit ledger."""
    import db

    db.init_db()
    current_version = ensure_current_strategy_version()["version"]
    as_of = as_of or date.today().isoformat()
    conn = db.get_conn()
    try:
        rows = conn.execute(
            """
            SELECT * FROM signal_log
            WHERE action IN ('STRONG_BUY', 'BUY', 'CAUTION_BUY')
              AND COALESCE(settlement_status, 'pending') IN ('', 'pending', 'unknown')
            ORDER BY date ASC, id ASC
            """
        ).fetchall()
        details = [
            _settlement_context(conn, dict(row), as_of, current_version)
            for row in rows
        ]
    finally:
        conn.close()

    due = [item for item in details if item["due"]]
    due_blocked = [item for item in due if not item["ready_to_settle"]]
    return {
        "as_of": as_of,
        "current_strategy_version": current_version,
        "pending": len(details),
        "due": len(due),
        "due_blocked": len(due_blocked),
        "awaiting_exit_date": sum(not item["due"] for item in details),
        "ready_to_settle": sum(item["ready_to_settle"] for item in due),
        "reason_counts": _settlement_reason_counts(details),
        "details": [_public_settlement_context(item) for item in details],
    }


def settle_pending_signal_outcomes(
    dry_run: bool = False,
    *,
    as_of: Optional[str] = None,
) -> dict[str, Any]:
    import db

    db.init_db()
    version = ensure_current_strategy_version()["version"]
    today = as_of or date.today().isoformat()
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
            context = _settlement_context(conn, sig, today, version)
            if "non_trading_signal_date" in context["reasons"]:
                non_executable += 1
                details.append(_public_settlement_context(
                    context, status="non_executable"
                ))
                if not dry_run:
                    conn.execute(
                        """
                        UPDATE signal_log
                        SET settlement_status='non_executable',
                            executable_status='non_executable',
                            non_executable_reason='non_trading_signal_date'
                        WHERE id=?
                        """,
                        (sig["id"],),
                    )
                continue
            if not context["ready_to_settle"]:
                if (
                    context["due"]
                    and context["trading_days_elapsed"] > SIGNAL_OUTCOME_EXPIRY_TRADING_DAYS
                ):
                    expired += 1
                    status = "expired_unsettled"
                    reason = ", ".join(context["reasons"])
                    if not dry_run:
                        conn.execute(
                            """
                            UPDATE signal_log
                            SET settlement_status='expired_unsettled',
                                executable_status='expired_unsettled',
                                non_executable_reason=?
                            WHERE id=?
                            """,
                            (reason, sig["id"]),
                        )
                else:
                    pending += 1
                    status = "pending"
                details.append(_public_settlement_context(context, status=status))
                continue

            price_rows = context["_rows"]
            entry = price_rows["stock_entry"]
            exit_ = price_rows["stock_exit"]
            bench_entry = price_rows["benchmark_entry"]
            bench_exit = price_rows["benchmark_exit"]
            entry_open = float(entry["open"])
            exit_open = float(exit_["open"])
            stock_return = (exit_open - entry_open) / entry_open * 100
            bench_return = (float(bench_exit["open"]) - float(bench_entry["open"])) / float(bench_entry["open"]) * 100
            excess = stock_return - bench_return
            strategy_version = context["strategy_version"]
            executable = (
                strategy_version != LEGACY_STRATEGY_VERSION
                and context.get("data_quality") == "high"
                and classify_backtest_price_source(context.get("adjustment_mode")) == "gate_eligible"
            )
            status = "settled" if executable else "non_executable"
            reason = "" if executable else ", ".join(context["reasons"] or ["low confidence price"])
            if executable:
                settled += 1
            else:
                non_executable += 1
            details.append(_public_settlement_context(
                context,
                status=status,
                return_5d=stock_return,
                excess_5d=excess,
            ))
            if not dry_run:
                conn.execute(
                    """
                    UPDATE signal_log
                    SET entry_date=?, entry_open=?, exit_date=?, exit_open=?,
                        outcome_5d=?, return_5d=?, benchmark_code=?,
                        benchmark_return_5d=?, excess_5d=?,
                        strategy_version=?, settlement_status=?,
                        executable_status=?, non_executable_reason=?,
                        data_quality=?,
                        adjustment_mode=?
                    WHERE id=?
                    """,
                    (
                        context["entry_date"],
                        entry_open,
                        context["exit_date"],
                        exit_open,
                        stock_return,
                        stock_return,
                        context["benchmark_code"],
                        bench_return,
                        excess,
                        strategy_version,
                        status,
                        "executable" if executable else "non_executable",
                        reason,
                        context.get("data_quality", "low"),
                        context.get("adjustment_mode", "raw"),
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
        "reason_counts": _settlement_reason_counts(details),
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
        f"p0 alpha: {result.get('p0_alpha_validation', {}).get('verdict', '-')}",
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
        paper_count = result["explain"].get("paper_sample_count", 0)
        if paper_count:
            lines.append(f"paper diagnostics: {paper_count} samples (not gate eligible)")
        lines.append(f"date distribution: {result['explain'].get('date_distribution', {})}")
        lines.append(f"tier distribution: {result['explain'].get('tier_distribution', {})}")
    return "\n".join(lines)


def format_record_report(result: dict[str, Any]) -> str:
    if result.get("skipped"):
        return (
            f"record-real-data skipped date={result.get('date', '')} "
            f"reason={result.get('skip_reason', 'unknown')}"
        )
    lines = [
        f"record-real-data date={result.get('date', '')} dry_run={result['dry_run']} "
        f"saved={result['saved']} count={result['count']}",
        f"low_quality={len(result['low_quality'])} missing={len(result['missing'])}",
    ]
    if result.get("source_date_mismatches"):
        lines.append(f"source_date_mismatch={len(result['source_date_mismatches'])}")
    if result["source_errors"]:
        lines.append(f"source_errors={result['source_errors']}")
    for item in result["low_quality"][:8]:
        lines.append(f"  warning {item['code']}: {item.get('warning', '')}")
    return "\n".join(lines)
