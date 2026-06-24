"""
GitHub A股量化项目精华融合层。

本模块不引入外部项目代码，也不触碰交易/仓位计算逻辑；它把本次调研得到的
可复用方法沉淀为 Serenity 自身的只读体检与改进建议。
"""
from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any

from config import ALL_CODES, STOCK_MAP
from db import get_conn
from security_check import build_security_report


TOP_GITHUB_PROJECTS: list[dict[str, Any]] = [
    {
        "repo": "bbfamily/abu",
        "url": "https://github.com/bbfamily/abu",
        "stars": 17558,
        "license": "GPL-3.0",
        "essence": "量化研究、回测、机器学习与报告体系完整",
        "fusion": "把策略评估沉淀为持续体检，而不是只看当天分数",
    },
    {
        "repo": "shidenggui/easytrader",
        "url": "https://github.com/shidenggui/easytrader",
        "stars": 9909,
        "license": "MIT",
        "essence": "交易执行抽象、远程客户端与券商适配边界清晰",
        "fusion": "Serenity 保持执行边界，只做闸门诊断，不新增真实下单通道",
    },
    {
        "repo": "1nchaos/adata",
        "url": "https://github.com/1nchaos/adata",
        "stars": 4797,
        "license": "Apache-2.0",
        "essence": "多数据源融合切换的 A股数据 SDK",
        "fusion": "体检行情覆盖率、数据新鲜度与备用源数量",
    },
    {
        "repo": "wbh604/UZI-Skill",
        "url": "https://github.com/wbh604/UZI-Skill",
        "stars": 4280,
        "license": "MIT",
        "essence": "多维投研审核与自我复核门禁",
        "fusion": "把 signal outcome 与 score reflection 作为自我复盘门禁",
    },
    {
        "repo": "Micro-sheep/efinance",
        "url": "https://github.com/Micro-sheep/efinance",
        "stars": 3809,
        "license": "MIT",
        "essence": "轻量行情/财务/基金数据接口，强调限流与备选源",
        "fusion": "把数据源可用性变成每日健康指标",
    },
    {
        "repo": "mpquant/Ashare",
        "url": "https://github.com/mpquant/Ashare",
        "stars": 3603,
        "license": "NOASSERTION",
        "essence": "极简 A股行情 API，新浪/腾讯双源自动切换",
        "fusion": "利用 Serenity 现有新浪/腾讯/AKShare接口做源级韧性检查",
    },
    {
        "repo": "oficcejo/aiagents-stock",
        "url": "https://github.com/oficcejo/aiagents-stock",
        "stars": 1531,
        "license": "NOASSERTION",
        "essence": "多 AI 智能体股票分析，数据源失败重试与替换",
        "fusion": "把失败重试、随机 UA、备用源思想转成可观测建议",
    },
    {
        "repo": "khscience/OSkhQuant",
        "url": "https://github.com/khscience/OSkhQuant",
        "stars": 1318,
        "license": "NOASSERTION",
        "essence": "低耦合可视化回测，数据/策略/执行分层",
        "fusion": "融合层独立成模块，只读 DB，不污染评分和交易模块",
    },
    {
        "repo": "DR-lin-eng/stock-scanner",
        "url": "https://github.com/DR-lin-eng/stock-scanner",
        "stars": 963,
        "license": "MIT",
        "essence": "财务、技术、新闻与 AI 综合扫描，缓存与降级规则",
        "fusion": "给 Serenity 增加跨模块体检摘要与下一步命令",
    },
    {
        "repo": "zhanghan1990/zipline-chinese",
        "url": "https://github.com/zhanghan1990/zipline-chinese",
        "stars": 687,
        "license": "Apache-2.0",
        "essence": "Zipline A股交易日历、手续费与本地数据改造",
        "fusion": "评估价格历史、评分历史和 outcome 是否足够支撑回测",
    },
]

QUANTDINGER_SOURCE = {
    "repo": "brokermr810/QuantDinger",
    "url": "https://github.com/brokermr810/QuantDinger",
    "stars": 8682,
    "updated_at": "2026-06-24",
}

QUANTDINGER_ESSENCE: list[dict[str, str]] = [
    {
        "pattern": "研究 -> 策略 -> 回测 -> 执行 -> 监控闭环",
        "fusion": "Serenity 的 fusion 报告不只列健康项，还输出客观共识与下一步命令。",
    },
    {
        "pattern": "objective_score 先于 LLM",
        "fusion": "九维评分、UZI、技术面先归一到 -100~+100，LLM 只能解释，不能覆盖规则分。",
    },
    {
        "pattern": "多周期客观共识",
        "fusion": "用最新分、5日均分、20日均分加权投票，输出 agreement 与 quality_multiplier。",
    },
    {
        "pattern": "结构化输出与趋势展望",
        "fusion": "每只标的返回 decision、confidence、timeframes、trend_outlook，便于 CLI/API/看板复用。",
    },
    {
        "pattern": "Agent Gateway 安全模型",
        "fusion": "Serenity 先只暴露只读共识 API；写操作仍走现有 token/硬闸，不引入外部下单通道。",
    },
    {
        "pattern": "回测执行对齐实盘",
        "fusion": "把回测准备度纳入 fusion 体检，避免数据不足时过早优化策略。",
    },
]


ASSESSMENT_LABELS = {
    "data_resilience": "数据源韧性",
    "feedback_loop": "信号反馈闭环",
    "execution_boundary": "执行安全边界",
    "backtest_readiness": "回测/研究就绪度",
    "quantdinger_consensus": "QuantDinger客观共识",
}


def get_top_projects() -> list[dict[str, Any]]:
    """返回本次调研沉淀的 Top10 项目清单副本。"""
    return [dict(project) for project in TOP_GITHUB_PROJECTS]


def get_quantdinger_essence() -> list[dict[str, str]]:
    """返回 QuantDinger 可复用设计模式清单副本。"""
    return [dict(item) for item in QUANTDINGER_ESSENCE]


def _pct(part: int | float, total: int | float) -> float:
    if not total:
        return 0.0
    return round(float(part) / float(total) * 100, 1)


def _safe_scalar(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple[Any, ...] = (),
    default: Any = 0,
) -> Any:
    try:
        row = conn.execute(sql, params).fetchone()
    except sqlite3.Error:
        return default
    if not row:
        return default
    value = row[0]
    return default if value is None else value


def _days_since(date_text: str | None) -> int | None:
    if not date_text:
        return None
    try:
        return (date.today() - date.fromisoformat(str(date_text)[:10])).days
    except ValueError:
        return None


def _status(score: int) -> str:
    if score >= 80:
        return "good"
    if score >= 55:
        return "watch"
    return "risk"


def _status_text(status: str) -> str:
    return {
        "good": "稳健",
        "watch": "待加强",
        "risk": "需修缮",
    }.get(status, status)


def _stock_filter_sql(column: str = "code") -> tuple[str, tuple[str, ...]]:
    placeholders = ", ".join("?" for _ in ALL_CODES)
    return f"{column} IN ({placeholders})", tuple(ALL_CODES)


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        columns = set()
        for row in conn.execute(f"PRAGMA table_info({table})"):
            try:
                columns.add(row["name"])
            except (TypeError, KeyError, IndexError):
                columns.add(row[1])
        return columns
    except sqlite3.Error:
        return set()


def _row_get(row: sqlite3.Row | dict[str, Any], key: str, default: Any = None) -> Any:
    try:
        if isinstance(row, dict):
            return row.get(key, default)
        if key in row.keys():
            return row[key]
    except Exception:
        pass
    return default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _center_score(score_0_100: Any) -> float | None:
    try:
        value = float(score_0_100)
    except (TypeError, ValueError):
        return None
    return _clamp((value - 50.0) * 2.0, -100.0, 100.0)


def _decision_from_objective(score: float) -> str:
    if score >= 20:
        return "BUY"
    if score <= -20:
        return "REDUCE"
    return "WATCH"


def _decision_text(decision: str) -> str:
    return {
        "BUY": "偏进攻",
        "WATCH": "观察",
        "REDUCE": "降风险",
    }.get(decision, decision)


def _trend_strength(score: float) -> str:
    value = abs(float(score))
    if value >= 70:
        return "strong"
    if value >= 40:
        return "moderate"
    if value >= 20:
        return "mild"
    return "neutral"


def _objective_from_scoring_row(
    row: sqlite3.Row | dict[str, Any],
    columns: set[str],
) -> dict[str, Any]:
    weights = [
        ("total_score", 0.40, "总分"),
        ("technical_score", 0.18, "技术"),
        ("factor_score", 0.14, "Alpha"),
        ("sentiment_score", 0.10, "情绪"),
        ("serenity_score", 0.08, "Serenity匹配"),
        ("moat_score", 0.06, "护城河"),
        ("uzi_score", 0.04, "UZI"),
    ]
    weighted_sum = 0.0
    weight_sum = 0.0
    breakdown = []
    for column, weight, label in weights:
        if column not in columns:
            continue
        centered = _center_score(_row_get(row, column))
        if centered is None:
            continue
        weighted_sum += centered * weight
        weight_sum += weight
        breakdown.append({
            "key": column,
            "label": label,
            "score": round(centered, 2),
            "weight": weight,
        })

    overall = weighted_sum / weight_sum if weight_sum else 0.0
    return {
        "overall_score": round(overall, 2),
        "decision": _decision_from_objective(overall),
        "components": breakdown,
        "component_coverage": round(weight_sum, 2),
    }


def _recent_total_scores(conn: sqlite3.Connection, code: str, limit: int) -> list[float]:
    try:
        rows = conn.execute(
            """
            SELECT total_score
            FROM scoring_history
            WHERE code = ?
            ORDER BY date DESC
            LIMIT ?
            """,
            (code, limit),
        ).fetchall()
    except sqlite3.Error:
        return []
    scores = []
    for row in rows:
        centered = _center_score(row["total_score"])
        if centered is not None:
            scores.append(centered)
    return scores


def _price_return_score(conn: sqlite3.Connection, code: str, limit: int) -> float | None:
    try:
        rows = conn.execute(
            """
            SELECT close
            FROM price_history
            WHERE code = ? AND close IS NOT NULL
            ORDER BY date DESC
            LIMIT ?
            """,
            (code, max(2, limit)),
        ).fetchall()
    except sqlite3.Error:
        return None
    if len(rows) < 2:
        return None
    latest = float(rows[0]["close"] or 0)
    oldest = float(rows[-1]["close"] or 0)
    if latest <= 0 or oldest <= 0:
        return None
    return _clamp(((latest - oldest) / oldest) * 400.0, -50.0, 50.0)


def _timeframe_score(
    conn: sqlite3.Connection,
    code: str,
    days: int,
    current_score: float,
    base_weight: float,
) -> dict[str, Any]:
    recent = _recent_total_scores(conn, code, days)
    if not recent:
        score = current_score
        data_points = 0
    else:
        avg_score = sum(recent) / len(recent)
        score = avg_score * 0.75 + current_score * 0.25
        data_points = len(recent)

    price_score = _price_return_score(conn, code, days)
    if price_score is not None:
        score = score * 0.85 + price_score * 0.15

    return {
        "days": days,
        "score": round(score, 2),
        "decision": _decision_from_objective(score),
        "strength": _trend_strength(score),
        "weight": base_weight,
        "data_points": data_points,
        "price_return_score": round(price_score, 2) if price_score is not None else None,
    }


def _build_code_consensus(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    columns: set[str],
) -> dict[str, Any]:
    code = row["code"]
    name = STOCK_MAP.get(code, {}).get("name", code)
    objective = _objective_from_scoring_row(row, columns)
    current_score = float(objective["overall_score"])
    timeframes = {
        "latest": {
            "days": 1,
            "score": round(current_score, 2),
            "decision": objective["decision"],
            "strength": _trend_strength(current_score),
            "weight": 1.30,
            "data_points": 1,
            "price_return_score": None,
        },
        "week": _timeframe_score(conn, code, 5, current_score, 1.15),
        "month": _timeframe_score(conn, code, 20, current_score, 1.00),
    }

    weighted_sum = 0.0
    weight_sum = 0.0
    for item in timeframes.values():
        score = float(item["score"])
        weight = float(item["weight"]) * (1.0 + min(1.0, abs(score) / 100.0))
        weighted_sum += score * weight
        weight_sum += weight
    consensus_score = weighted_sum / weight_sum if weight_sum else current_score
    decision = _decision_from_objective(consensus_score)
    agreement = sum(1 for item in timeframes.values() if item["decision"] == decision)
    agreement_ratio = agreement / max(1, len(timeframes))

    history_points = max(item["data_points"] for item in timeframes.values())
    history_quality = _clamp(history_points / 20.0, 0.45, 1.0)
    component_quality = _clamp(float(objective["component_coverage"]), 0.45, 1.0)
    quality_multiplier = round(history_quality * component_quality, 3)
    confidence = int(_clamp(
        (50 + abs(consensus_score) * 0.35)
        * (0.85 + 0.30 * agreement_ratio)
        * quality_multiplier,
        0,
        98,
    ))

    return {
        "code": code,
        "name": name,
        "date": row["date"],
        "objective_score": objective,
        "timeframes": timeframes,
        "consensus_score": round(consensus_score, 2),
        "consensus_decision": decision,
        "agreement_ratio": round(agreement_ratio, 3),
        "quality_multiplier": quality_multiplier,
        "confidence": confidence,
        "trend_outlook": {
            "next_1d": timeframes["latest"],
            "next_1w": timeframes["week"],
            "next_1m": timeframes["month"],
        },
    }


def _empty_quantdinger_consensus(
    *,
    latest_date: str | None = None,
    covered: int = 0,
    total: int | None = None,
) -> dict[str, Any]:
    total = len(ALL_CODES) if total is None else total
    return {
        "source": dict(QUANTDINGER_SOURCE),
        "latest_date": latest_date,
        "coverage": f"{covered}/{total}",
        "coverage_pct": _pct(covered, total),
        "universe_score": 0.0,
        "universe_decision": "NO_DATA",
        "quality_multiplier": 0.0,
        "agreement_ratio": 0.0,
        "signals": [],
        "top_opportunities": [],
        "risk_flags": [],
    }


def build_quantdinger_consensus(
    conn: sqlite3.Connection | None = None,
    *,
    limit: int = 8,
) -> dict[str, Any]:
    """构建 QuantDinger 风格的多周期客观共识，只读 DB。"""
    should_close = conn is None
    if conn is None:
        conn = get_conn()
    try:
        stock_filter, stock_params = _stock_filter_sql()
        columns = _table_columns(conn, "scoring_history")
        if "total_score" not in columns:
            return _empty_quantdinger_consensus(total=0)

        latest_date = _safe_scalar(
            conn,
            f"SELECT MAX(date) FROM scoring_history WHERE {stock_filter}",
            stock_params,
            default=None,
        )
        if not latest_date:
            return _empty_quantdinger_consensus()

        rows = conn.execute(
            f"""
            SELECT *
            FROM scoring_history
            WHERE date = ? AND {stock_filter}
            ORDER BY total_score DESC
            """,
            (latest_date, *stock_params),
        ).fetchall()
        signals = [_build_code_consensus(conn, row, columns) for row in rows]
        signals.sort(key=lambda item: item["consensus_score"], reverse=True)

        coverage_pct = _pct(len(signals), len(ALL_CODES))
        if signals:
            universe_score = sum(item["consensus_score"] for item in signals) / len(signals)
            avg_quality = sum(item["quality_multiplier"] for item in signals) / len(signals)
            avg_agreement = sum(item["agreement_ratio"] for item in signals) / len(signals)
        else:
            universe_score = avg_quality = avg_agreement = 0.0

        top_opportunities = [
            item for item in signals
            if item["consensus_decision"] == "BUY"
        ][:limit]
        risk_flags = [
            item for item in reversed(signals)
            if item["consensus_decision"] == "REDUCE"
        ][:limit]

        return {
            "source": dict(QUANTDINGER_SOURCE),
            "latest_date": latest_date,
            "coverage": f"{len(signals)}/{len(ALL_CODES)}",
            "coverage_pct": coverage_pct,
            "universe_score": round(universe_score, 2),
            "universe_decision": _decision_from_objective(universe_score),
            "quality_multiplier": round(avg_quality, 3),
            "agreement_ratio": round(avg_agreement, 3),
            "signals": signals[:limit],
            "top_opportunities": top_opportunities,
            "risk_flags": risk_flags,
            "policy": "BUY>=+20, REDUCE<=-20；A股 REDUCE 表示降风险/回避，不表示做空。",
        }
    finally:
        if should_close:
            conn.close()


def _score_data_resilience(coverage_pct: float, days_stale: int | None,
                           provider_count: int) -> int:
    provider_score = 30 if provider_count >= 2 else 12
    coverage_score = min(40, round(coverage_pct * 0.4))
    if days_stale is None:
        freshness_score = 0
    elif days_stale <= 1:
        freshness_score = 30
    elif days_stale <= 3:
        freshness_score = 22
    elif days_stale <= 7:
        freshness_score = 12
    else:
        freshness_score = 4
    return int(min(100, provider_score + coverage_score + freshness_score))


def assess_data_resilience(conn: sqlite3.Connection) -> dict[str, Any]:
    """评估行情数据覆盖、新鲜度与备用源。"""
    stock_filter, stock_params = _stock_filter_sql()
    latest_date = _safe_scalar(
        conn,
        f"SELECT MAX(date) FROM price_history WHERE {stock_filter}",
        stock_params,
        default=None,
    )
    latest_count = 0
    if latest_date:
        latest_count = int(_safe_scalar(
            conn,
            f"SELECT COUNT(DISTINCT code) FROM price_history "
            f"WHERE date=? AND {stock_filter}",
            (latest_date, *stock_params),
            default=0,
        ))
    history_days = int(_safe_scalar(
        conn,
        f"SELECT COUNT(DISTINCT date) FROM price_history WHERE {stock_filter}",
        stock_params,
        default=0,
    ))
    days_stale = _days_since(latest_date)

    providers = ["sina"]
    try:
        import data_engine

        if hasattr(data_engine, "_tencent_fetch_realtime"):
            providers.append("tencent")
        if hasattr(data_engine, "_akshare_fetch_realtime"):
            providers.append("akshare")
    except Exception:
        pass

    coverage_pct = _pct(latest_count, len(ALL_CODES))
    score = _score_data_resilience(coverage_pct, days_stale, len(set(providers)))
    stale_text = "无历史数据" if days_stale is None else f"{days_stale} 天前"

    return {
        "key": "data_resilience",
        "label": ASSESSMENT_LABELS["data_resilience"],
        "score": score,
        "status": _status(score),
        "summary": (
            f"最近行情 {latest_date or '无'}，覆盖 {latest_count}/{len(ALL_CODES)}，"
            f"备用源 {len(set(providers))} 个"
        ),
        "metrics": {
            "latest_price_date": latest_date or "无",
            "latest_price_age": stale_text,
            "latest_coverage": f"{latest_count}/{len(ALL_CODES)} ({coverage_pct}%)",
            "history_days": history_days,
            "providers": ", ".join(sorted(set(providers))),
        },
    }


def assess_feedback_loop(conn: sqlite3.Connection) -> dict[str, Any]:
    """评估信号 outcome 与评分反思是否形成闭环。"""
    total_signals = int(_safe_scalar(conn, "SELECT COUNT(*) FROM signal_log"))
    outcome_1d = int(_safe_scalar(
        conn,
        "SELECT COUNT(*) FROM signal_log WHERE outcome_1d IS NOT NULL",
    ))
    outcome_5d = int(_safe_scalar(
        conn,
        "SELECT COUNT(*) FROM signal_log WHERE outcome_5d IS NOT NULL",
    ))
    total_reflections = int(_safe_scalar(conn, "SELECT COUNT(*) FROM score_reflections"))
    filled_reflections = int(_safe_scalar(
        conn,
        "SELECT COUNT(*) FROM score_reflections WHERE actual_return_1d IS NOT NULL",
    ))
    ic_reflections = int(_safe_scalar(
        conn,
        "SELECT COUNT(*) FROM score_reflections "
        "WHERE dimension_ic IS NOT NULL AND dimension_ic != '{}'",
    ))

    outcome_pct = _pct(outcome_1d, total_signals)
    reflection_pct = _pct(filled_reflections, total_reflections)
    score = int(min(
        100,
        round(outcome_pct * 0.45)
        + round(_pct(outcome_5d, total_signals) * 0.25)
        + round(reflection_pct * 0.20)
        + min(10, ic_reflections),
    ))

    return {
        "key": "feedback_loop",
        "label": ASSESSMENT_LABELS["feedback_loop"],
        "score": score,
        "status": _status(score),
        "summary": (
            f"信号 outcome_1d {outcome_1d}/{total_signals}，"
            f"反思收益 {filled_reflections}/{total_reflections}"
        ),
        "metrics": {
            "signals_total": total_signals,
            "signals_outcome_1d": f"{outcome_1d}/{total_signals} ({outcome_pct}%)",
            "signals_outcome_5d": f"{outcome_5d}/{total_signals} ({_pct(outcome_5d, total_signals)}%)",
            "reflections_filled": f"{filled_reflections}/{total_reflections} ({reflection_pct}%)",
            "dimension_ic_rows": ic_reflections,
        },
    }


def assess_execution_boundary(conn: sqlite3.Connection) -> dict[str, Any]:
    """评估执行侧是否保持安全边界。"""
    security = build_security_report()
    execution_total = int(_safe_scalar(conn, "SELECT COUNT(*) FROM execution_log"))
    execution_failed = int(_safe_scalar(
        conn,
        "SELECT COUNT(*) FROM execution_log WHERE status='failed'",
    ))

    execution_score = 100 if execution_failed == 0 else max(20, 100 - execution_failed * 10)
    score = int(round(security["score"] * 0.75 + execution_score * 0.25))
    if security["fail_count"]:
        status = "risk"
    elif security["warn_count"]:
        status = "watch"
    else:
        status = _status(score)
    token_checks = {c["id"]: c for c in security["checks"]}

    return {
        "key": "execution_boundary",
        "label": ASSESSMENT_LABELS["execution_boundary"],
        "score": score,
        "status": status,
        "summary": (
            "写接口硬闸通过" if not security["fail_count"]
            else f"写接口存在 {security['fail_count']} 个失败项"
        ),
        "metrics": {
            "security_check_score": f"{security['score']}/100",
            "security_warnings": security["warn_count"],
            "dashboard_token": token_checks.get("dashboard_token", {}).get("status_text", "未知"),
            "bridge_token": token_checks.get("bridge_token", {}).get("status_text", "未知"),
            "execution_logs": execution_total,
            "execution_failed": execution_failed,
            "policy": "不融合外部真实下单代码，继续保留 Serenity 执行边界",
        },
    }


def assess_backtest_readiness(conn: sqlite3.Connection) -> dict[str, Any]:
    """评估当前数据是否足够支撑回测与因子研究。"""
    stock_filter, stock_params = _stock_filter_sql()
    price_days = int(_safe_scalar(
        conn,
        f"SELECT COUNT(DISTINCT date) FROM price_history WHERE {stock_filter}",
        stock_params,
    ))
    scoring_days = int(_safe_scalar(
        conn,
        f"SELECT COUNT(DISTINCT date) FROM scoring_history WHERE {stock_filter}",
        stock_params,
    ))
    latest_scoring_date = _safe_scalar(
        conn,
        f"SELECT MAX(date) FROM scoring_history WHERE {stock_filter}",
        stock_params,
        default=None,
    )
    latest_scoring_count = 0
    if latest_scoring_date:
        latest_scoring_count = int(_safe_scalar(
            conn,
            f"SELECT COUNT(DISTINCT code) FROM scoring_history "
            f"WHERE date=? AND {stock_filter}",
            (latest_scoring_date, *stock_params),
        ))
    signals_with_outcome = int(_safe_scalar(
        conn,
        "SELECT COUNT(*) FROM signal_log WHERE outcome_1d IS NOT NULL",
    ))
    mainboard_ok = all(code.startswith(("000", "002", "600", "601", "603", "605"))
                       for code in ALL_CODES)

    score = int(min(
        100,
        min(40, round(price_days / 60 * 40))
        + min(25, round(scoring_days / 20 * 25))
        + min(25, round(signals_with_outcome / 30 * 25))
        + (10 if mainboard_ok else 0),
    ))

    return {
        "key": "backtest_readiness",
        "label": ASSESSMENT_LABELS["backtest_readiness"],
        "score": score,
        "status": _status(score),
        "summary": (
            f"价格历史 {price_days} 天，评分历史 {scoring_days} 天，"
            f"已结算信号 {signals_with_outcome} 条"
        ),
        "metrics": {
            "price_history_days": price_days,
            "scoring_history_days": scoring_days,
            "latest_scoring": (
                f"{latest_scoring_date} ({latest_scoring_count}/{len(ALL_CODES)})"
                if latest_scoring_date else "无"
            ),
            "signals_with_outcome": signals_with_outcome,
            "mainboard_scope": "符合" if mainboard_ok else "存在非主板代码",
        },
    }


def assess_quantdinger_consensus(conn: sqlite3.Connection) -> dict[str, Any]:
    """评估 QuantDinger 风格客观共识层是否可用。"""
    consensus = build_quantdinger_consensus(conn)
    coverage_score = min(35, round(consensus["coverage_pct"] * 0.35))
    quality_score = min(40, round(float(consensus["quality_multiplier"]) * 40))
    agreement_score = min(15, round(float(consensus["agreement_ratio"]) * 15))
    signal_score = 10 if consensus["signals"] else 0
    score = int(coverage_score + quality_score + agreement_score + signal_score)
    status = _status(score)
    if consensus["universe_decision"] == "NO_DATA":
        status = "risk"

    top = consensus["top_opportunities"][:1] or consensus["signals"][:1]
    if top:
        lead = top[0]
        lead_text = (
            f"{lead['code']} {_decision_text(lead['consensus_decision'])} "
            f"{lead['consensus_score']:.1f}"
        )
    else:
        lead_text = "无有效信号"

    return {
        "key": "quantdinger_consensus",
        "label": ASSESSMENT_LABELS["quantdinger_consensus"],
        "score": score,
        "status": status,
        "summary": (
            f"全局共识 {consensus['universe_score']:.1f}"
            f"({_decision_text(consensus['universe_decision'])})，领先信号 {lead_text}"
        ),
        "metrics": {
            "source": f"{consensus['source']['repo']} ({consensus['source']['stars']:,} stars)",
            "latest_scoring_date": consensus["latest_date"] or "无",
            "coverage": f"{consensus['coverage']} ({consensus['coverage_pct']}%)",
            "quality_multiplier": consensus["quality_multiplier"],
            "agreement_ratio": consensus["agreement_ratio"],
            "policy": consensus.get("policy", ""),
        },
        "consensus": consensus,
    }


def _build_recommendations(assessments: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    recommendations: list[dict[str, str]] = []

    data_status = assessments["data_resilience"]["status"]
    if data_status != "good":
        recommendations.append({
            "priority": "P1",
            "area": "数据源韧性",
            "inspired_by": "adata / Ashare / efinance / aiagents-stock",
            "action": "补齐最近行情历史，并把新浪失败时的腾讯/AKShare切换纳入日常观察。",
            "command": "python3 cli.py workflow",
        })

    feedback_status = assessments["feedback_loop"]["status"]
    if feedback_status != "good":
        recommendations.append({
            "priority": "P1",
            "area": "信号反馈闭环",
            "inspired_by": "UZI-Skill / stock-scanner",
            "action": "先补填 signal outcome，再看命中率和反思 IC，避免评分只凭直觉迭代。",
            "command": "python3 cli.py signal-perf && python3 cli.py reflection-ic",
        })

    safety_status = assessments["execution_boundary"]["status"]
    if safety_status == "risk":
        recommendations.append({
            "priority": "P1",
            "area": "执行安全边界",
            "inspired_by": "easytrader",
            "action": "先修复写接口硬闸失败项，再考虑公网或自动化调用。",
            "command": "python3 cli.py security-check",
        })
    elif safety_status == "watch":
        recommendations.append({
            "priority": "P2",
            "area": "执行安全边界",
            "inspired_by": "easytrader",
            "action": "硬闸已通过；公网或隧道访问前补齐 dashboard/bridge token。",
            "command": "export SERENITY_DASHBOARD_TOKEN=... SERENITY_BRIDGE_TOKEN=...",
        })

    backtest_status = assessments["backtest_readiness"]["status"]
    if backtest_status != "good":
        recommendations.append({
            "priority": "P2",
            "area": "回测/研究就绪度",
            "inspired_by": "abu / OSkhQuant / zipline-chinese",
            "action": "积累价格历史、评分历史和已结算信号后，再做因子 IC 与绩效归因。",
            "command": "python3 cli.py factor-ic && python3 cli.py perf-attr",
        })

    qd_status = assessments["quantdinger_consensus"]["status"]
    qd_consensus = assessments["quantdinger_consensus"].get("consensus", {})
    if qd_status == "risk":
        recommendations.append({
            "priority": "P1",
            "area": "QuantDinger客观共识",
            "inspired_by": "QuantDinger fast_analysis.py",
            "action": "先补齐 scoring_history 与 price_history，再启用共识裁决作为每日选股闸门。",
            "command": "python3 cli.py rescore && python3 cli.py workflow",
        })
    elif qd_consensus.get("risk_flags"):
        recommendations.append({
            "priority": "P1",
            "area": "QuantDinger客观共识",
            "inspired_by": "多周期共识 + 质量降权",
            "action": "存在多周期降风险信号；先复核风险标的，再决定是否继续持有。",
            "command": "python3 cli.py fusion && python3 cli.py portfolio",
        })

    if not recommendations:
        recommendations.append({
            "priority": "OK",
            "area": "融合状态",
            "inspired_by": "QuantDinger + Top10 综合",
            "action": "核心闭环健康；保持每日 workflow、health 与 fusion 三联检查。",
            "command": "python3 cli.py workflow && python3 cli.py health && python3 cli.py fusion",
        })

    return recommendations


def build_fusion_report(conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """构建融合体检报告。conn 传入时由调用方负责关闭。"""
    should_close = conn is None
    if conn is None:
        conn = get_conn()
    try:
        assessments = {
            "data_resilience": assess_data_resilience(conn),
            "feedback_loop": assess_feedback_loop(conn),
            "execution_boundary": assess_execution_boundary(conn),
            "backtest_readiness": assess_backtest_readiness(conn),
            "quantdinger_consensus": assess_quantdinger_consensus(conn),
        }
        total_score = round(
            sum(item["score"] for item in assessments.values()) / len(assessments)
        )
        return {
            "title": "Serenity x QuantDinger 量化融合体检",
            "quantdinger_source": dict(QUANTDINGER_SOURCE),
            "quantdinger_essence": get_quantdinger_essence(),
            "top_projects": get_top_projects(),
            "assessments": assessments,
            "quantdinger_consensus": assessments["quantdinger_consensus"]["consensus"],
            "fusion_score": total_score,
            "status": _status(total_score),
            "recommendations": _build_recommendations(assessments),
        }
    finally:
        if should_close:
            conn.close()


def format_fusion_report(report: dict[str, Any] | None = None) -> str:
    """格式化 CLI 输出。"""
    report = report or build_fusion_report()
    lines: list[str] = [
        report["title"],
        "=" * 72,
        f"融合总分: {report['fusion_score']}/100 ({_status_text(report['status'])})",
        "",
        "QuantDinger 精华已融合",
    ]
    qd_source = report["quantdinger_source"]
    lines.append(
        f"- 来源: {qd_source['repo']} ({qd_source['stars']:,} stars, updated {qd_source['updated_at']})"
    )
    for item in report["quantdinger_essence"]:
        lines.append(f"- {item['pattern']}: {item['fusion']}")

    qd = report["quantdinger_consensus"]
    lines.extend([
        "",
        "QuantDinger 客观共识",
        f"- 最新评分日: {qd['latest_date'] or '无'} | 覆盖: {qd['coverage']} ({qd['coverage_pct']}%)",
        (
            f"- 全局倾向: {qd['universe_score']:.1f} "
            f"({_decision_text(qd['universe_decision'])}) | "
            f"agreement {qd['agreement_ratio']:.2f} | quality {qd['quality_multiplier']:.2f}"
        ),
    ])
    if qd["top_opportunities"]:
        lines.append("- Top 机会:")
        for item in qd["top_opportunities"][:5]:
            lines.append(
                f"  · {item['code']}: {item['consensus_score']:.1f} "
                f"{_decision_text(item['consensus_decision'])}, "
                f"confidence {item['confidence']}, agreement {item['agreement_ratio']:.2f}"
            )
    if qd["risk_flags"]:
        lines.append("- 风险标的:")
        for item in qd["risk_flags"][:5]:
            lines.append(
                f"  · {item['code']}: {item['consensus_score']:.1f} "
                f"{_decision_text(item['consensus_decision'])}, "
                f"confidence {item['confidence']}, quality {item['quality_multiplier']:.2f}"
            )
    if not qd["top_opportunities"] and not qd["risk_flags"]:
        lines.append("- 当前无强方向标的，按 WATCH 处理。")

    lines.extend([
        "",
        "GitHub Top10 精华源",
    ])
    for idx, project in enumerate(report["top_projects"], 1):
        lines.append(
            f"{idx:>2}. {project['repo']} ({project['stars']:,} stars) - "
            f"{project['essence']}"
        )

    lines.extend(["", "Serenity 当前融合体检"])
    for assessment in report["assessments"].values():
        lines.append(
            f"- {assessment['label']}: {assessment['score']}/100 "
            f"({_status_text(assessment['status'])})"
        )
        lines.append(f"  {assessment['summary']}")
        for key, value in assessment["metrics"].items():
            lines.append(f"  · {key}: {value}")

    lines.extend(["", "下一步修缮动作"])
    for item in report["recommendations"]:
        lines.append(
            f"- [{item['priority']}] {item['area']} | 来源: {item['inspired_by']}"
        )
        lines.append(f"  {item['action']}")
        lines.append(f"  命令: {item['command']}")

    return "\n".join(lines)
