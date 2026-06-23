"""
GitHub A股量化项目精华融合层。

本模块不引入外部项目代码，也不触碰交易/仓位计算逻辑；它把本次调研得到的
可复用方法沉淀为 Serenity 自身的只读体检与改进建议。
"""
from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any

from config import ALL_CODES
from db import get_conn


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


ASSESSMENT_LABELS = {
    "data_resilience": "数据源韧性",
    "feedback_loop": "信号反馈闭环",
    "execution_boundary": "执行安全边界",
    "backtest_readiness": "回测/研究就绪度",
}


def get_top_projects() -> list[dict[str, Any]]:
    """返回本次调研沉淀的 Top10 项目清单副本。"""
    return [dict(project) for project in TOP_GITHUB_PROJECTS]


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
    from security_check import build_security_report

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

    if not recommendations:
        recommendations.append({
            "priority": "OK",
            "area": "融合状态",
            "inspired_by": "Top10 综合",
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
        }
        total_score = round(
            sum(item["score"] for item in assessments.values()) / len(assessments)
        )
        return {
            "title": "Serenity GitHub A股量化融合体检",
            "top_projects": get_top_projects(),
            "assessments": assessments,
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
        "GitHub Top10 精华源",
    ]
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
