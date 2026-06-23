"""
Serenity Alpha Gate — 选股候选研究闸门。

本模块借鉴 Qlib / RQAlpha / vn.py 一类量化项目的共同边界：
研究、回测、执行分层。这里仅做只读诊断，不修改仓位、不触发交易。
"""
from __future__ import annotations

import math
import sqlite3
from datetime import datetime
from typing import Any

from config import STOCK_MAP
from db import get_conn


MAINBOARD_PREFIXES = ("000", "002", "600", "601", "603", "605")

BULLISH_ACTIONS = {"STRONG_BUY", "BUY", "CAUTION_BUY", "STRONG_HOLD", "HOLD"}

ACTION_CONFIDENCE = {
    "STRONG_BUY": 96,
    "BUY": 84,
    "CAUTION_BUY": 68,
    "STRONG_HOLD": 60,
    "HOLD": 50,
    "CAUTION": 38,
    "SELL": 18,
    "STOP_LOSS": 5,
    "SCORE_ONLY": 45,
}

METHOD_SOURCES = [
    {
        "repo": "microsoft/qlib",
        "url": "https://github.com/microsoft/qlib",
        "essence": "把因子研究、模型验证与生产链路分层",
        "fusion": "候选进入执行前必须先看 IC、胜率与稳定性",
    },
    {
        "repo": "ricequant/rqalpha",
        "url": "https://github.com/ricequant/rqalpha",
        "essence": "事件驱动回测与策略边界清晰",
        "fusion": "Serenity 继续只读评估，不在研究闸门里改交易/仓位逻辑",
    },
    {
        "repo": "vnpy/vnpy",
        "url": "https://github.com/vnpy/vnpy",
        "essence": "行情、策略、风控、执行模块化",
        "fusion": "把 Alpha Gate 放在信号和执行之间，专门降低误报",
    },
]


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, float(value)))


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        value = float(value)
        if math.isnan(value) or math.isinf(value):
            return default
        return value
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.Error:
        return set()


def _safe_scalar(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple[Any, ...] = (),
    default: Any = None,
) -> Any:
    try:
        row = conn.execute(sql, params).fetchone()
    except sqlite3.Error:
        return default
    if not row:
        return default
    value = row[0]
    return default if value is None else value


def _is_mainboard(code: str) -> bool:
    return str(code).startswith(MAINBOARD_PREFIXES)


def _stock_name(code: str) -> str:
    return STOCK_MAP.get(str(code), {}).get("name", str(code))


def _status(score: float) -> str:
    if score >= 75:
        return "PASS"
    if score >= 58:
        return "WATCH"
    return "BLOCK"


def _status_text(status: str) -> str:
    return {
        "PASS": "可重点盯盘",
        "WATCH": "等待确认",
        "BLOCK": "暂不追",
        "good": "健康",
        "watch": "待观察",
        "risk": "需修缮",
    }.get(status, status)


def get_method_sources() -> list[dict[str, str]]:
    """返回外部方法源副本，方便 UI/报告复用。"""
    return [dict(item) for item in METHOD_SOURCES]


def _normalize_ic_payload(ic_data: dict[str, Any] | None) -> dict[str, Any]:
    if ic_data is not None:
        return ic_data
    try:
        from factor_ic import compute_rank_ic

        return compute_rank_ic(days=45, window=20)
    except Exception as exc:
        return {"error": f"Rank IC 计算失败: {exc}"}


def build_factor_health(ic_data: dict[str, Any] | None = None) -> dict[str, Any]:
    """把 Rank IC 压缩为一个可用于候选闸门的因子健康分。"""
    payload = _normalize_ic_payload(ic_data)
    if payload.get("error"):
        return {
            "score": 42,
            "status": "risk",
            "summary": payload["error"],
            "dimensions": [],
            "best": [],
            "weak": [],
        }

    try:
        from factor_ic import DIMENSION_LABELS
    except Exception:
        DIMENSION_LABELS = {}

    dimensions: list[dict[str, Any]] = []
    all_dims = sorted(set(payload.get("mean_ic", {})) | set(payload.get("latest", {})))
    for dim in all_dims:
        n_days = _safe_int(payload.get("n_days", {}).get(dim), 0)
        mean_ic = _safe_float(payload.get("mean_ic", {}).get(dim), 0.0)
        latest = _safe_float(payload.get("latest", {}).get(dim), 0.0)
        ic_ir = _safe_float(payload.get("ic_ir", {}).get(dim), 0.0)
        win_rate = _safe_float(payload.get("win_rate", {}).get(dim), 0.0)
        quality = 50 + mean_ic * 180 + (win_rate - 50) * 0.45 + ic_ir * 4
        if n_days < 5:
            quality -= 10
        dimensions.append({
            "key": dim,
            "label": DIMENSION_LABELS.get(dim, dim),
            "latest_ic": round(latest, 4),
            "mean_ic": round(mean_ic, 4),
            "ic_ir": round(ic_ir, 4),
            "win_rate": round(win_rate, 1),
            "n_days": n_days,
            "quality": round(_clamp(quality), 1),
        })

    usable = [d for d in dimensions if d["n_days"] > 0]
    if not usable:
        score = 45
    else:
        focus = [
            d for d in usable
            if d["key"] in {
                "total_score",
                "momentum_score",
                "factor_score",
                "technical_score",
                "serenity_score",
                "zone_score",
                "moat_score",
            }
        ] or usable
        score = round(sum(d["quality"] for d in focus) / len(focus), 1)

    best = sorted(usable, key=lambda d: d["quality"], reverse=True)[:3]
    weak = sorted(usable, key=lambda d: d["quality"])[:3]
    status = "good" if score >= 70 else "watch" if score >= 50 else "risk"
    summary = (
        "因子 IC 有正向可用信号"
        if status == "good"
        else "因子 IC 仍需样本验证"
        if status == "watch"
        else "因子 IC 当前拖累候选置信度"
    )

    return {
        "score": score,
        "status": status,
        "summary": summary,
        "dimensions": dimensions,
        "best": best,
        "weak": weak,
    }


def build_factor_adjustments(factor_health: dict[str, Any]) -> list[dict[str, str]]:
    """根据因子健康度生成只读调参建议，不直接改权重。"""
    adjustments: list[dict[str, str]] = []
    for item in factor_health.get("weak", []):
        if item.get("quality", 100) >= 45:
            continue
        severity = "P1" if item.get("quality", 100) < 30 else "P2"
        if item.get("mean_ic", 0) < 0 and item.get("win_rate", 100) < 40:
            action = "建议临时降权或冻结新增加分，直到连续样本恢复正 IC。"
        else:
            action = "建议保留观察，但不要让该维度单独触发追击。"
        adjustments.append({
            "priority": severity,
            "dimension": item.get("label", item.get("key", "unknown")),
            "metric": (
                f"quality {item.get('quality', 0)}, "
                f"meanIC {item.get('mean_ic', 0):+.4f}, "
                f"win {item.get('win_rate', 0):.1f}%"
            ),
            "action": action,
        })

    for item in factor_health.get("best", []):
        if item.get("quality", 0) < 65:
            continue
        adjustments.append({
            "priority": "KEEP",
            "dimension": item.get("label", item.get("key", "unknown")),
            "metric": (
                f"quality {item.get('quality', 0)}, "
                f"meanIC {item.get('mean_ic', 0):+.4f}, "
                f"win {item.get('win_rate', 0):.1f}%"
            ),
            "action": "可作为候选加分的主支撑，但仍需结合信号胜率闸门。",
        })

    if not adjustments:
        adjustments.append({
            "priority": "INFO",
            "dimension": "因子样本",
            "metric": "暂无明确强弱分化",
            "action": "继续积累 scoring_history 与 price_history，再复跑 Rank IC。",
        })
    return adjustments


def _latest_signal_candidates(conn: sqlite3.Connection, limit: int) -> list[dict[str, Any]]:
    if not _table_exists(conn, "signal_log"):
        return []
    latest_date = _safe_scalar(conn, "SELECT MAX(date) FROM signal_log")
    if not latest_date:
        return []

    try:
        rows = conn.execute(
            """
            SELECT *
            FROM signal_log
            WHERE date=?
            ORDER BY total_score DESC, id DESC
            LIMIT ?
            """,
            (latest_date, max(limit * 3, limit)),
        ).fetchall()
    except sqlite3.Error:
        return []

    candidates: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        code = str(item.get("code", ""))
        action = str(item.get("action") or "SCORE_ONLY")
        total_score = _safe_float(item.get("total_score"))
        if not _is_mainboard(code):
            continue
        if action not in BULLISH_ACTIONS and total_score < 70:
            continue
        item["code"] = code
        item["name"] = _stock_name(code)
        item["action"] = action
        item["total_score"] = total_score
        item["source"] = "signal_log"
        candidates.append(item)
    return candidates


def _latest_score_candidates(conn: sqlite3.Connection, limit: int) -> list[dict[str, Any]]:
    if not _table_exists(conn, "scoring_history"):
        return []
    latest_date = _safe_scalar(conn, "SELECT MAX(date) FROM scoring_history")
    if not latest_date:
        return []

    try:
        rows = conn.execute(
            """
            SELECT *
            FROM scoring_history
            WHERE date=?
            ORDER BY total_score DESC
            LIMIT ?
            """,
            (latest_date, max(limit * 3, limit)),
        ).fetchall()
    except sqlite3.Error:
        return []

    candidates: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        code = str(item.get("code", ""))
        total_score = _safe_float(item.get("total_score"))
        if not _is_mainboard(code) or total_score < 60:
            continue
        item["code"] = code
        item["name"] = _stock_name(code)
        item["action"] = "SCORE_ONLY"
        item["total_score"] = total_score
        item["source"] = "scoring_history"
        candidates.append(item)
    return candidates


def load_candidate_rows(conn: sqlite3.Connection, limit: int = 10) -> list[dict[str, Any]]:
    """读取最新信号/评分候选；signal_log 优先，scoring_history 兜底。"""
    by_code: dict[str, dict[str, Any]] = {}
    for item in _latest_signal_candidates(conn, limit):
        by_code[item["code"]] = item
    for item in _latest_score_candidates(conn, limit):
        by_code.setdefault(item["code"], item)
    return sorted(
        by_code.values(),
        key=lambda row: _safe_float(row.get("total_score")),
        reverse=True,
    )[:limit]


def _perf_from_row(row: dict[str, Any]) -> dict[str, Any]:
    total = _safe_int(row.get("total_signals"))
    samples_1d = _safe_int(row.get("samples_1d"), total)
    samples_3d = _safe_int(row.get("samples_3d"), total)
    samples_5d = _safe_int(row.get("samples_5d"), total)
    wins_1d = _safe_int(row.get("wins_1d"))
    wins_3d = _safe_int(row.get("wins_3d"))
    wins_5d = _safe_int(row.get("wins_5d"))
    return {
        "total_signals": total,
        "samples_1d": samples_1d,
        "samples_3d": samples_3d,
        "samples_5d": samples_5d,
        "hit_rate_1d": round(wins_1d / samples_1d * 100, 1) if samples_1d else 0.0,
        "hit_rate_3d": round(wins_3d / samples_3d * 100, 1) if samples_3d else 0.0,
        "hit_rate_5d": round(wins_5d / samples_5d * 100, 1) if samples_5d else 0.0,
        "avg_return_1d": round(_safe_float(row.get("avg_return_1d")), 2),
        "avg_return_3d": round(_safe_float(row.get("avg_return_3d")), 2),
        "avg_return_5d": round(_safe_float(row.get("avg_return_5d")), 2),
    }


def _aggregate_action_rows(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    buckets: dict[str, dict[str, float]] = {}
    for row in rows:
        action = str(row.get("action") or "")
        total = _safe_int(row.get("total_signals"))
        if not action or total <= 0:
            continue
        bucket = buckets.setdefault(action, {
            "total_signals": 0,
            "samples_1d": 0,
            "samples_3d": 0,
            "samples_5d": 0,
            "wins_1d": 0,
            "wins_3d": 0,
            "wins_5d": 0,
            "ret_1d": 0.0,
            "ret_3d": 0.0,
            "ret_5d": 0.0,
        })
        samples_1d = _safe_int(row.get("samples_1d"), total)
        samples_3d = _safe_int(row.get("samples_3d"), total)
        samples_5d = _safe_int(row.get("samples_5d"), total)
        bucket["total_signals"] += total
        bucket["samples_1d"] += samples_1d
        bucket["samples_3d"] += samples_3d
        bucket["samples_5d"] += samples_5d
        bucket["wins_1d"] += _safe_int(row.get("wins_1d"))
        bucket["wins_3d"] += _safe_int(row.get("wins_3d"))
        bucket["wins_5d"] += _safe_int(row.get("wins_5d"))
        bucket["ret_1d"] += _safe_float(row.get("avg_return_1d")) * samples_1d
        bucket["ret_3d"] += _safe_float(row.get("avg_return_3d")) * samples_3d
        bucket["ret_5d"] += _safe_float(row.get("avg_return_5d")) * samples_5d

    result: dict[str, dict[str, Any]] = {}
    for action, bucket in buckets.items():
        total = int(bucket["total_signals"])
        result[action] = _perf_from_row({
            "total_signals": total,
            "samples_1d": bucket["samples_1d"],
            "samples_3d": bucket["samples_3d"],
            "samples_5d": bucket["samples_5d"],
            "wins_1d": bucket["wins_1d"],
            "wins_3d": bucket["wins_3d"],
            "wins_5d": bucket["wins_5d"],
            "avg_return_1d": (
                bucket["ret_1d"] / bucket["samples_1d"]
                if bucket["samples_1d"] else 0
            ),
            "avg_return_3d": (
                bucket["ret_3d"] / bucket["samples_3d"]
                if bucket["samples_3d"] else 0
            ),
            "avg_return_5d": (
                bucket["ret_5d"] / bucket["samples_5d"]
                if bucket["samples_5d"] else 0
            ),
        })
    return result


def _build_perf_index(rows: list[dict[str, Any]]) -> dict[str, Any]:
    exact = {
        (str(row.get("code")), str(row.get("action"))): _perf_from_row(row)
        for row in rows
    }
    return {"exact": exact, "action": _aggregate_action_rows(rows)}


def _load_perf_from_signal_log(conn: sqlite3.Connection) -> dict[str, Any] | None:
    if not _table_exists(conn, "signal_log"):
        return None
    columns = _columns(conn, "signal_log")
    required = {"code", "action", "outcome_1d", "outcome_3d", "outcome_5d"}
    if not required.issubset(columns):
        return None
    try:
        rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT code, action, total_signals,
                       samples_1d, samples_3d, samples_5d,
                       wins_1d, wins_3d, wins_5d,
                       avg_return_1d, avg_return_3d, avg_return_5d
                FROM (
                    SELECT
                        code,
                        action,
                        COUNT(*) AS total_signals,
                        SUM(CASE WHEN outcome_1d IS NOT NULL THEN 1 ELSE 0 END) AS samples_1d,
                        SUM(CASE WHEN outcome_3d IS NOT NULL THEN 1 ELSE 0 END) AS samples_3d,
                        SUM(CASE WHEN outcome_5d IS NOT NULL THEN 1 ELSE 0 END) AS samples_5d,
                        SUM(CASE WHEN outcome_1d > 0 THEN 1 ELSE 0 END) AS wins_1d,
                        SUM(CASE WHEN outcome_3d > 0 THEN 1 ELSE 0 END) AS wins_3d,
                        SUM(CASE WHEN outcome_5d > 0 THEN 1 ELSE 0 END) AS wins_5d,
                        ROUND(AVG(CASE WHEN outcome_1d IS NOT NULL THEN outcome_1d END), 2)
                            AS avg_return_1d,
                        ROUND(AVG(CASE WHEN outcome_3d IS NOT NULL THEN outcome_3d END), 2)
                            AS avg_return_3d,
                        ROUND(AVG(CASE WHEN outcome_5d IS NOT NULL THEN outcome_5d END), 2)
                            AS avg_return_5d
                    FROM signal_log
                    GROUP BY code, action
                )
                """
            ).fetchall()
        ]
    except sqlite3.Error:
        return None
    if not rows or not any(
        _safe_int(row.get("samples_1d"))
        or _safe_int(row.get("samples_3d"))
        or _safe_int(row.get("samples_5d"))
        for row in rows
    ):
        return None
    return _build_perf_index(rows)


def _load_perf_from_summary_table(conn: sqlite3.Connection) -> dict[str, Any]:
    if not _table_exists(conn, "signal_performance"):
        return {"exact": {}, "action": {}}

    columns = _columns(conn, "signal_performance")
    required = {
        "code",
        "action",
        "total_signals",
        "wins_1d",
        "wins_3d",
        "wins_5d",
        "avg_return_1d",
        "avg_return_3d",
        "avg_return_5d",
    }
    if not required.issubset(columns):
        return {"exact": {}, "action": {}}

    try:
        rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT code, action, total_signals,
                       wins_1d, wins_3d, wins_5d,
                       avg_return_1d, avg_return_3d, avg_return_5d
                FROM signal_performance
                """
            ).fetchall()
        ]
    except sqlite3.Error:
        return {"exact": {}, "action": {}}
    return _build_perf_index(rows)


def load_signal_performance(conn: sqlite3.Connection) -> dict[str, Any]:
    """读取信号绩效，优先按成熟 outcome 重算，再回退到汇总表。"""
    return _load_perf_from_signal_log(conn) or _load_perf_from_summary_table(conn)


def _candidate_perf(
    perf_index: dict[str, Any],
    code: str,
    action: str,
) -> dict[str, Any]:
    exact = perf_index.get("exact", {}).get((code, action))
    if exact:
        exact = dict(exact)
        exact["scope"] = "code_action"
        return exact
    action_perf = perf_index.get("action", {}).get(action)
    if action_perf:
        action_perf = dict(action_perf)
        action_perf["scope"] = "action"
        return action_perf
    return {
        "scope": "none",
        "total_signals": 0,
        "samples_1d": 0,
        "samples_3d": 0,
        "samples_5d": 0,
        "hit_rate_1d": 0.0,
        "hit_rate_3d": 0.0,
        "hit_rate_5d": 0.0,
        "avg_return_1d": 0.0,
        "avg_return_3d": 0.0,
        "avg_return_5d": 0.0,
    }


def _preferred_horizon(perf: dict[str, Any]) -> tuple[str, int, float, float]:
    for key, label in (("5d", "5日"), ("3d", "3日"), ("1d", "1日")):
        samples = _safe_int(perf.get(f"samples_{key}"))
        if samples > 0:
            return (
                label,
                samples,
                _safe_float(perf.get(f"hit_rate_{key}")),
                _safe_float(perf.get(f"avg_return_{key}")),
            )
    return ("5日", 0, 0.0, 0.0)


def _score_perf(perf: dict[str, Any]) -> float:
    _, samples, hit_rate, avg_return = _preferred_horizon(perf)
    if samples <= 0:
        return 46.0
    sample_bonus = min(12, math.log(samples + 1, 2) * 3)
    score = _clamp(42 + (hit_rate - 50) * 0.75 + avg_return * 3.2 + sample_bonus)
    if samples < 3:
        return min(score, 58.0)
    if samples < 5:
        return min(score, 75.0)
    return score


def _score_trend(conn: sqlite3.Connection, code: str) -> dict[str, Any]:
    if not _table_exists(conn, "scoring_history"):
        return {"score": 50.0, "delta": 0.0, "samples": 0}
    columns = _columns(conn, "scoring_history")
    if "total_score" not in columns:
        return {"score": 50.0, "delta": 0.0, "samples": 0}
    try:
        rows = conn.execute(
            """
            SELECT date, total_score
            FROM scoring_history
            WHERE code=?
            ORDER BY date DESC
            LIMIT 5
            """,
            (code,),
        ).fetchall()
    except sqlite3.Error:
        return {"score": 50.0, "delta": 0.0, "samples": 0}
    scores = [_safe_float(dict(row).get("total_score")) for row in rows]
    if len(scores) < 2:
        return {"score": 50.0, "delta": 0.0, "samples": len(scores)}
    latest = scores[0]
    oldest = scores[-1]
    delta = latest - oldest
    drawdown = latest - max(scores)
    score = 55 + delta * 1.1 + drawdown * 1.5 + min(10, len(scores) * 2)
    return {
        "score": round(_clamp(score), 1),
        "delta": round(delta, 1),
        "drawdown": round(drawdown, 1),
        "samples": len(scores),
    }


def score_candidate(
    conn: sqlite3.Connection,
    row: dict[str, Any],
    factor_health: dict[str, Any],
    perf_index: dict[str, Any],
) -> dict[str, Any]:
    """为单个候选计算闸门分。"""
    code = str(row.get("code"))
    action = str(row.get("action") or "SCORE_ONLY")
    total_score = _safe_float(row.get("total_score"))
    perf = _candidate_perf(perf_index, code, action)
    trend = _score_trend(conn, code)

    score_component = _clamp(total_score)
    action_component = ACTION_CONFIDENCE.get(action, 45)
    perf_component = _score_perf(perf)
    factor_component = _safe_float(factor_health.get("score"), 45)
    trend_component = _safe_float(trend.get("score"), 50)

    gate_score = (
        score_component * 0.32
        + action_component * 0.18
        + perf_component * 0.25
        + factor_component * 0.17
        + trend_component * 0.08
    )

    good: list[str] = []
    risks: list[str] = []
    horizon, samples, hit_rate, avg_return = _preferred_horizon(perf)

    if total_score >= 80:
        good.append(f"综合评分 {total_score:.0f}，进入强候选区")
    elif total_score < 65:
        risks.append(f"综合评分 {total_score:.0f}，候选质量偏弱")

    if action in {"STRONG_BUY", "BUY"}:
        good.append(f"最新信号为 {action}")
    elif action in {"CAUTION_BUY", "HOLD", "SCORE_ONLY"}:
        risks.append(f"信号为 {action}，需要更多确认")

    if samples >= 3:
        if hit_rate >= 60:
            good.append(f"同类信号 {horizon}胜率 {hit_rate:.1f}%")
        elif hit_rate < 45:
            risks.append(f"同类信号 {horizon}胜率仅 {hit_rate:.1f}%")
        if avg_return > 0:
            good.append(f"{horizon}平均收益 {avg_return:+.2f}%")
        elif avg_return < 0:
            risks.append(f"{horizon}平均收益 {avg_return:+.2f}%")
    else:
        gate_score -= 6
        risks.append(f"成熟信号样本 {samples} 条，统计置信度不足")

    if factor_component >= 70:
        good.append("因子 IC 健康，支持顺势筛选")
    elif factor_component < 50:
        gate_score -= 6
        risks.append("因子 IC 健康度偏低，需降低追击冲动")

    if trend.get("samples", 0) >= 3 and trend.get("delta", 0) < -8:
        gate_score -= 4
        risks.append(f"近 {trend['samples']} 次评分回落 {abs(trend['delta']):.1f} 分")

    if samples >= 3 and (hit_rate < 40 or avg_return < -1.0):
        gate_score = min(gate_score, 57)
    if factor_component < 35:
        gate_score = min(gate_score, 57)

    gate_score = round(_clamp(gate_score), 1)
    status = _status(gate_score)
    velocity = (
        "加速观察"
        if status == "PASS" and total_score >= 82 and hit_rate >= 60 and avg_return > 0
        else "标准观察"
        if status == "PASS"
        else "等待确认"
        if status == "WATCH"
        else "剔除/不追"
    )

    return {
        "code": code,
        "name": row.get("name") or _stock_name(code),
        "action": action,
        "source": row.get("source", "unknown"),
        "total_score": round(total_score, 1),
        "gate_score": gate_score,
        "status": status,
        "status_text": _status_text(status),
        "velocity": velocity,
        "components": {
            "score": round(score_component, 1),
            "action": round(action_component, 1),
            "signal_history": round(perf_component, 1),
            "factor_health": round(factor_component, 1),
            "score_trend": round(trend_component, 1),
        },
        "performance": perf,
        "performance_horizon": {
            "label": horizon,
            "samples": samples,
            "hit_rate": hit_rate,
            "avg_return": avg_return,
        },
        "trend": trend,
        "strengths": good[:4],
        "risks": risks[:4],
    }


def _build_recommendations(report: dict[str, Any]) -> list[dict[str, str]]:
    recommendations: list[dict[str, str]] = []
    summary = report["summary"]
    factor_health = report["factor_health"]

    if summary["pass_count"]:
        recommendations.append({
            "priority": "P1",
            "area": "明日盯盘",
            "action": "只把 PASS 候选放进高优先级观察池；WATCH 只等补确认，BLOCK 不追。",
            "command": "python3 cli.py alpha-gate",
        })
    else:
        recommendations.append({
            "priority": "P1",
            "area": "候选质量",
            "action": "当前无 PASS 候选，先补评分/信号与 outcome，再让闸门重新筛。",
            "command": "python3 cli.py workflow && python3 cli.py alpha-gate",
        })

    if factor_health["status"] != "good":
        recommendations.append({
            "priority": "P1",
            "area": "因子有效性",
            "action": "优先复核弱 IC 维度，避免负 IC 因子继续推高候选分。",
            "command": "python3 cli.py factor-ic && python3 cli.py perf-attr",
        })

    if summary["data_gap_count"]:
        recommendations.append({
            "priority": "P2",
            "area": "样本闭环",
            "action": "补齐 signal_performance 样本，候选分才会从主观评分转为历史胜率约束。",
            "command": "python3 cli.py signal-perf",
        })

    return recommendations


def build_alpha_gate_report(
    conn: sqlite3.Connection | None = None,
    limit: int = 10,
    ic_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构建 Alpha Gate 报告。conn 传入时由调用方负责关闭。"""
    should_close = conn is None
    if conn is None:
        conn = get_conn()
    try:
        factor_health = build_factor_health(ic_data)
        perf_index = load_signal_performance(conn)
        candidates = [
            score_candidate(conn, row, factor_health, perf_index)
            for row in load_candidate_rows(conn, limit=limit)
        ]
        candidates.sort(key=lambda item: item["gate_score"], reverse=True)
        candidates = candidates[:limit]
        summary = {
            "total_candidates": len(candidates),
            "pass_count": sum(1 for c in candidates if c["status"] == "PASS"),
            "watch_count": sum(1 for c in candidates if c["status"] == "WATCH"),
            "block_count": sum(1 for c in candidates if c["status"] == "BLOCK"),
            "data_gap_count": sum(
                1 for c in candidates
                if _safe_int(c["performance_horizon"].get("samples")) < 3
            ),
        }
        report = {
            "title": "Serenity Alpha Gate 选股候选研究闸门",
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "factor_health": factor_health,
            "factor_adjustments": build_factor_adjustments(factor_health),
            "candidates": candidates,
            "summary": summary,
            "method_sources": get_method_sources(),
        }
        report["recommendations"] = _build_recommendations(report)
        return report
    finally:
        if should_close:
            conn.close()


def format_alpha_gate_report(report: dict[str, Any] | None = None) -> str:
    """格式化 CLI 输出。"""
    report = report or build_alpha_gate_report()
    factor_health = report["factor_health"]
    summary = report["summary"]

    lines = [
        report["title"],
        "=" * 72,
        f"生成时间: {report['generated_at']}",
        (
            f"候选概况: PASS {summary['pass_count']} / WATCH {summary['watch_count']} / "
            f"BLOCK {summary['block_count']} / 样本不足 {summary['data_gap_count']}"
        ),
        (
            f"因子健康: {factor_health['score']}/100 "
            f"({_status_text(factor_health['status'])}) - {factor_health['summary']}"
        ),
    ]

    if factor_health["best"]:
        lines.append("强势因子:")
        for item in factor_health["best"]:
            lines.append(
                f"  - {item['label']}: quality {item['quality']}, "
                f"meanIC {item['mean_ic']:+.4f}, win {item['win_rate']:.1f}%"
            )
    if factor_health["weak"]:
        lines.append("需复核因子:")
        for item in factor_health["weak"]:
            lines.append(
                f"  - {item['label']}: quality {item['quality']}, "
                f"meanIC {item['mean_ic']:+.4f}, win {item['win_rate']:.1f}%"
            )

    lines.extend(["", "因子调参建议"])
    for item in report["factor_adjustments"]:
        lines.append(
            f"- [{item['priority']}] {item['dimension']}: {item['metric']} | "
            f"{item['action']}"
        )

    lines.extend(["", "候选闸门"])
    if not report["candidates"]:
        lines.append("  暂无候选：请先运行 python3 cli.py rescore 或 workflow 生成评分/信号。")
    for idx, candidate in enumerate(report["candidates"], 1):
        perf = candidate["performance"]
        horizon = candidate["performance_horizon"]
        components = candidate["components"]
        lines.append(
            f"{idx:>2}. {candidate['name']}({candidate['code']}) "
            f"{candidate['status']} {_status_text(candidate['status'])} "
            f"| gate {candidate['gate_score']}/100 | {candidate['action']} | {candidate['velocity']}"
        )
        lines.append(
            "    分解: "
            f"评分 {components['score']} / 动作 {components['action']} / "
            f"历史 {components['signal_history']} / 因子 {components['factor_health']} / "
            f"趋势 {components['score_trend']}"
        )
        lines.append(
            f"    绩效: 总信号 {perf['total_signals']} | "
            f"成熟样本 {horizon['samples']}({horizon['label']}) | "
            f"胜率 {horizon['hit_rate']:.1f}% | "
            f"均收 {horizon['avg_return']:+.2f}% | scope {perf['scope']}"
        )
        if candidate["strengths"]:
            lines.append("    支撑: " + "；".join(candidate["strengths"]))
        if candidate["risks"]:
            lines.append("    风险: " + "；".join(candidate["risks"]))

    lines.extend(["", "外部方法源"])
    for item in report["method_sources"]:
        lines.append(f"- {item['repo']}: {item['essence']} -> {item['fusion']}")

    lines.extend(["", "下一步动作"])
    for item in report["recommendations"]:
        lines.append(f"- [{item['priority']}] {item['area']}: {item['action']}")
        lines.append(f"  命令: {item['command']}")

    return "\n".join(lines)


def cmd_alpha_gate() -> None:
    """CLI 入口。"""
    print(format_alpha_gate_report())


if __name__ == "__main__":
    cmd_alpha_gate()
