"""
Signal Performance — 信号绩效追踪与维度有效性分析

功能:
  1. calculate_outcomes()        — 每日收盘后补填 signal_log 的 outcome 字段
  2. compute_signal_performance() — 从 signal_log 聚合各信号类型的胜率/平均收益
  3. update_signal_performance_table() — 写入 signal_performance DB 表
  4. compute_dimension_effectiveness() — 各评分维度的预测有效性分析
  5. get_performance_report()    — 结构化报告（仪表盘可消费）
  6. generate_performance_report() — Markdown 格式报告
  7. cmd_signal_performance()    — 命令行入口
"""

import logging
from datetime import date, datetime
from typing import Optional

from db import (
    get_conn,
    get_unfilled_outcomes,
    update_signal_outcome,
    get_price_history,
)
from config import STOCK_MAP, ALL_CODES

logger = logging.getLogger(__name__)

# ============================================================
# 常量
# ============================================================

BULLISH_SIGNALS = {"STRONG_BUY", "BUY", "CAUTION_BUY", "STRONG_HOLD"}
BEARISH_SIGNALS = {"SELL", "STRONG_SELL", "CAUTION_SELL", "STOP_LOSS", "TAKE_PROFIT"}
NEUTRAL_SIGNALS = {"HOLD", "WATCH", "WEAK_HOLD"}

SCORING_DIMENSIONS = [
    "base_score", "zone_score", "momentum_score", "volume_score",
    "serenity_score", "factor_score", "technical_score",
    "sentiment_score", "moat_score",
]


# ============================================================
# 1. 每日补填 outcomes（已有逻辑）
# ============================================================

def _fetch_signal_outcomes(signal_id: int) -> dict:
    """获取单条信号的 outcome 字段当前值"""
    conn = get_conn()
    row = conn.execute(
        "SELECT outcome_1d, outcome_3d, outcome_5d, outcome_10d FROM signal_log WHERE id=?",
        (signal_id,),
    ).fetchone()
    conn.close()
    if not row:
        return {}
    return dict(row)


def calculate_outcomes() -> int:
    """每天收盘后执行，从 signal_log 读取所有 outcome 未填满的信号，
    对比今日收盘价计算已过 N 日的涨跌幅，调用 update_signal_outcome() 更新。

    Returns:
        本次更新的字段数量
    """
    try:
        signals = get_unfilled_outcomes(since_days=60)
        if not signals:
            print("[绩效追踪] 没有需要填充 outcomes 的信号")
            return 0

        updated = 0
        periods = [
            ("outcome_1d", 1),
            ("outcome_3d", 3),
            ("outcome_5d", 5),
            ("outcome_10d", 10),
        ]

        for sig in signals:
            sid = sig["id"]
            code = sig["code"]
            signal_date = sig["date"]
            signal_price = sig["price"]

            # 跳过无效价格
            if not signal_price or signal_price <= 0:
                continue

            # 获取当前各 outcome 值，跳过已填充的
            current = _fetch_signal_outcomes(sid)

            # 获取行情（按 date DESC）
            price_rows = get_price_history(code, 60)
            if not price_rows:
                continue

            # 按日期升序排列，便于向前查找 N 个交易日
            sorted_prices = sorted(price_rows, key=lambda r: r["date"])

            # 找到信号日期对应的起始索引（取第一个 >= signal_date 的位置）
            try:
                start_idx = next(
                    i for i, r in enumerate(sorted_prices)
                    if r["date"] >= signal_date
                )
            except StopIteration:
                continue

            for field, offset in periods:
                if current.get(field) is not None:
                    continue

                target_idx = start_idx + offset
                if target_idx >= len(sorted_prices):
                    continue

                target_close = sorted_prices[target_idx]["close"]
                if not target_close or target_close <= 0:
                    continue

                pct = (target_close - signal_price) / signal_price * 100
                update_signal_outcome(sid, field, round(pct, 2))
                updated += 1

        print(f"[绩效追踪] signal outcome 更新完成，共更新 {updated} 个字段")
        return updated

    except Exception as e:
        print(f"[绩效追踪] calculate_outcomes 出错: {e}")
        return 0


# ============================================================
# 2. 信号级绩效聚合
# ============================================================

def compute_signal_performance() -> list[dict]:
    """从 signal_log 计算每只股票各信号类型的绩效

    统计：信号总数、1/3/5 日胜率与平均收益率

    Returns
    -------
    list[dict]
        [{code, action, total_signals, wins_1d, wins_3d, wins_5d,
          avg_return_1d, avg_return_3d, avg_return_5d}, ...]
    """
    conn = get_conn()
    rows = conn.execute("""
        SELECT
            code,
            action,
            COUNT(*)                                               AS total_signals,
            SUM(CASE WHEN outcome_1d > 0 THEN 1 ELSE 0 END)       AS wins_1d,
            SUM(CASE WHEN outcome_3d > 0 THEN 1 ELSE 0 END)       AS wins_3d,
            SUM(CASE WHEN outcome_5d > 0 THEN 1 ELSE 0 END)       AS wins_5d,
            ROUND(AVG(CASE WHEN outcome_1d IS NOT NULL
                           THEN outcome_1d END), 2)                AS avg_return_1d,
            ROUND(AVG(CASE WHEN outcome_3d IS NOT NULL
                           THEN outcome_3d END), 2)                AS avg_return_3d,
            ROUND(AVG(CASE WHEN outcome_5d IS NOT NULL
                           THEN outcome_5d END), 2)                AS avg_return_5d
        FROM signal_log
        GROUP BY code, action
        ORDER BY code, total_signals DESC
    """).fetchall()
    conn.close()

    results = []
    for r in rows:
        results.append({
            "code": r["code"],
            "action": r["action"],
            "total_signals": r["total_signals"],
            "wins_1d": r["wins_1d"] or 0,
            "wins_3d": r["wins_3d"] or 0,
            "wins_5d": r["wins_5d"] or 0,
            "avg_return_1d": r["avg_return_1d"],
            "avg_return_3d": r["avg_return_3d"],
            "avg_return_5d": r["avg_return_5d"],
        })
    return results


def update_signal_performance_table():
    """计算最新绩效并写入 signal_performance 表 (UPSERT)

    信号数量太少（< 3 条）的条目跳过更新（保留历史统计）。
    """
    perf_data = compute_signal_performance()
    conn = get_conn()
    updated = 0
    skipped = 0
    for p in perf_data:
        if p["total_signals"] < 3:
            skipped += 1
            continue
        conn.execute("""
            INSERT INTO signal_performance
                (code, action, total_signals, wins_1d, wins_3d, wins_5d,
                 avg_return_1d, avg_return_3d, avg_return_5d)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(code, action) DO UPDATE SET
                total_signals=excluded.total_signals,
                wins_1d=excluded.wins_1d,
                wins_3d=excluded.wins_3d,
                wins_5d=excluded.wins_5d,
                avg_return_1d=excluded.avg_return_1d,
                avg_return_3d=excluded.avg_return_3d,
                avg_return_5d=excluded.avg_return_5d,
                last_updated=datetime('now','localtime')
        """, (
            p["code"], p["action"], p["total_signals"],
            p["wins_1d"], p["wins_3d"], p["wins_5d"],
            p["avg_return_1d"], p["avg_return_3d"], p["avg_return_5d"],
        ))
        updated += 1
    conn.commit()
    conn.close()
    logger.info("signal_performance 表已更新: %d 条写入, %d 条跳过(数据不足)", updated, skipped)
    return {"updated": updated, "skipped": skipped}


# ============================================================
# 3. 维度有效性分析
# ============================================================

def compute_dimension_effectiveness() -> list[dict]:
    """分析各评分维度的预测有效性

    对 scoring_history 维度分与 signal_log outcome 做 JOIN，
    按维度分 bin（每 10 分一档），计算每个区间的平均 1 日收益率，
    以及维度分与 outcome_1d 的相关系数。

    Returns
    -------
    list[dict]
        [{dimension, samples, positive_pct, rank_corr_1d,
          bins: [{range, count, avg_return, win_rate}, ...]}, ...]
    """
    conn = get_conn()

    rows = conn.execute("""
        SELECT
            sh.base_score, sh.zone_score, sh.momentum_score,
            sh.volume_score, sh.serenity_score, sh.factor_score,
            sh.technical_score, sh.sentiment_score, sh.moat_score,
            sl.outcome_1d
        FROM scoring_history sh
        JOIN signal_log sl ON sh.code = sl.code AND sh.date = sl.date
        WHERE sl.outcome_1d IS NOT NULL
    """).fetchall()
    conn.close()

    if not rows:
        return []

    dim_data: dict[str, list[Optional[float]]] = {d: [] for d in SCORING_DIMENSIONS}
    outcome_1d_list = []

    for r in rows:
        for dim in SCORING_DIMENSIONS:
            val = r[dim]
            if val is not None:
                dim_data[dim].append(val)
        outcome_1d_list.append(r["outcome_1d"])

    results = []
    for dim in SCORING_DIMENSIONS:
        scores = dim_data[dim]
        if len(scores) < 5:
            continue

        bins: dict[str, dict] = {}
        total_positive = 0
        total_valid = 0
        rank_pairs = []

        for i, s in enumerate(scores):
            if s is None or outcome_1d_list[i] is None:
                continue
            bin_key = f"{int(s // 10 * 10)}-{int(s // 10 * 10 + 10)}"
            if bin_key not in bins:
                bins[bin_key] = {"count": 0, "sum_return": 0.0, "wins": 0}
            bins[bin_key]["count"] += 1
            bins[bin_key]["sum_return"] += outcome_1d_list[i]
            if outcome_1d_list[i] > 0:
                bins[bin_key]["wins"] += 1
                total_positive += 1
            total_valid += 1
            rank_pairs.append((s, outcome_1d_list[i]))

        if total_valid < 5:
            continue

        bin_details = []
        for key in sorted(bins.keys(), key=lambda k: int(k.split("-")[0])):
            b = bins[key]
            bin_details.append({
                "range": key,
                "count": b["count"],
                "avg_return": round(b["sum_return"] / b["count"], 2) if b["count"] else 0,
                "win_rate": round(b["wins"] / b["count"], 3) if b["count"] else 0,
            })

        # 皮尔逊相关系数
        n = len(rank_pairs)
        mean_x = sum(p[0] for p in rank_pairs) / n
        mean_y = sum(p[1] for p in rank_pairs) / n
        num = sum((p[0] - mean_x) * (p[1] - mean_y) for p in rank_pairs)
        den = (
            sum((p[0] - mean_x) ** 2 for p in rank_pairs) ** 0.5
            * sum((p[1] - mean_y) ** 2 for p in rank_pairs) ** 0.5
        )
        rank_corr = round(num / den, 3) if den != 0 else 0.0

        results.append({
            "dimension": dim,
            "samples": total_valid,
            "positive_pct": round(total_positive / total_valid, 3) if total_valid else 0,
            "rank_corr_1d": rank_corr,
            "bins": bin_details,
        })

    results.sort(key=lambda x: abs(x["rank_corr_1d"]), reverse=True)
    return results


# ============================================================
# 4. 结构化性能报告（仪表盘可消费）
# ============================================================

def get_performance_report() -> dict:
    """生成完整的信号绩效结构化报告

    Returns
    -------
    dict
        signal_by_action — 按信号类型汇总
        dimensions       — 维度有效性
        summary          — 全局统计
    """
    conn = get_conn()

    action_summary = conn.execute("""
        SELECT
            action,
            COUNT(*)                                               AS total,
            ROUND(AVG(CASE WHEN outcome_1d IS NOT NULL
                           THEN outcome_1d END), 2)                AS avg_return_1d,
            ROUND(AVG(CASE WHEN outcome_3d IS NOT NULL
                           THEN outcome_3d END), 2)                AS avg_return_3d,
            ROUND(AVG(CASE WHEN outcome_5d IS NOT NULL
                           THEN outcome_5d END), 2)                AS avg_return_5d,
            SUM(CASE WHEN outcome_1d > 0 THEN 1 ELSE 0 END)
                * 1.0 / NULLIF(SUM(CASE WHEN outcome_1d IS NOT NULL
                                        THEN 1 ELSE 0 END), 0)     AS win_rate_1d,
            SUM(CASE WHEN outcome_3d > 0 THEN 1 ELSE 0 END)
                * 1.0 / NULLIF(SUM(CASE WHEN outcome_3d IS NOT NULL
                                        THEN 1 ELSE 0 END), 0)     AS win_rate_3d
        FROM signal_log
        GROUP BY action
        ORDER BY total DESC
    """).fetchall()

    signal_by_action = []
    for r in action_summary:
        signal_by_action.append({
            "action": r["action"],
            "total": r["total"],
            "avg_return_1d": r["avg_return_1d"],
            "avg_return_3d": r["avg_return_3d"],
            "avg_return_5d": r["avg_return_5d"],
            "win_rate_1d": round(r["win_rate_1d"], 4) if r["win_rate_1d"] is not None else None,
            "win_rate_3d": round(r["win_rate_3d"], 4) if r["win_rate_3d"] is not None else None,
        })

    totals = conn.execute("""
        SELECT
            COUNT(*)                                               AS total,
            SUM(CASE WHEN outcome_1d IS NOT NULL THEN 1 ELSE 0 END) AS with_outcome,
            SUM(CASE WHEN outcome_1d > 0 THEN 1 ELSE 0 END)
                * 1.0 / NULLIF(SUM(CASE WHEN outcome_1d IS NOT NULL
                                        THEN 1 ELSE 0 END), 0)     AS win_rate,
            AVG(CASE WHEN outcome_1d IS NOT NULL
                     THEN outcome_1d END)                          AS avg_return
        FROM signal_log
    """).fetchone()
    conn.close()

    dims = compute_dimension_effectiveness()

    best_action = max(
        [s for s in signal_by_action if s["win_rate_1d"] is not None and s["total"] >= 5],
        key=lambda s: s["win_rate_1d"],
        default=None,
    )
    best_dim = dims[0] if dims else None

    return {
        "signal_by_action": signal_by_action,
        "dimensions": dims,
        "summary": {
            "total_signals": totals["total"],
            "signals_with_outcome": totals["with_outcome"],
            "overall_win_rate_1d": round(totals["win_rate"], 4) if totals["win_rate"] else None,
            "overall_avg_return_1d": round(totals["avg_return"], 4) if totals["avg_return"] else None,
            "best_action": best_action["action"] if best_action else None,
            "best_action_win_rate": best_action["win_rate_1d"] if best_action else None,
            "best_dimension": best_dim["dimension"] if best_dim else None,
            "best_dimension_corr": best_dim["rank_corr_1d"] if best_dim else None,
        },
    }


# ============================================================
# 5. 数据完整性检查
# ============================================================

def validate_data_integrity() -> list[dict]:
    """检查信号数据完整性，返回潜在问题列表"""
    conn = get_conn()
    issues = []

    # 1) outcome 缺失超过 10 天
    old_missing = conn.execute("""
        SELECT id, code, date, action
        FROM signal_log
        WHERE outcome_1d IS NULL
          AND date < date('now', '-10 days')
        ORDER BY date DESC
        LIMIT 20
    """).fetchall()
    for r in old_missing:
        issues.append({
            "type": "missing_outcome",
            "severity": "warning",
            "message": f"{r['code']} {r['date']} {r['action']} 信号已过 10 天但 outcome 未填",
        })

    # 2) 同一标的同一天多条信号
    duplicates = conn.execute("""
        SELECT code, date, COUNT(*) as cnt
        FROM signal_log
        GROUP BY code, date
        HAVING cnt > 1
        ORDER BY cnt DESC
        LIMIT 10
    """).fetchall()
    for r in duplicates:
        issues.append({
            "type": "duplicate_signal",
            "severity": "info",
            "message": f"{r['code']} {r['date']} 有 {r['cnt']} 条信号",
        })

    conn.close()
    return issues


# ============================================================
# 6. Markdown 格式报告（已有兼容接口）
# ============================================================

def _merge_buy_stats(perf: dict) -> dict:
    """合并 BUY 和 STRONG_BUY 的统计数据"""
    merged = {"count": 0, "outcomes": {}}
    for key in ("BUY", "STRONG_BUY"):
        data = perf.get(key)
        if not data:
            continue
        merged["count"] += data["count"]
        for period, stats in data.get("outcomes", {}).items():
            if period not in merged["outcomes"]:
                merged["outcomes"][period] = {
                    "count": 0, "avg_return": 0.0, "hit_rate": 0.0,
                    "_hits": 0,
                }
            m = merged["outcomes"][period]
            total_old = m["avg_return"] * m["count"]
            m["count"] += stats["count"]
            m["avg_return"] = round(
                (total_old + stats["avg_return"] * stats["count"]) / m["count"], 2
            ) if m["count"] > 0 else 0.0
            m["_hits"] += int(stats["count"] * stats["hit_rate"] / 100)
            m["hit_rate"] = round(m["_hits"] / m["count"] * 100, 1) if m["count"] > 0 else 0.0
    return merged


def generate_performance_report(days: int = 30) -> str:
    """生成 Markdown 格式的绩效报告"""
    try:
        perf = get_signal_performance(days)

        lines = []
        lines.append(f"## 📊 信号绩效报告（近 {days} 天）")

        for action in sorted(perf.keys()):
            d = perf[action]
            count = d["count"]
            lines.append(f"\n### {action}")
            lines.append(f"- 信号数: {count}")

            for period in ("outcome_1d", "outcome_3d", "outcome_5d", "outcome_10d"):
                o = d["outcomes"].get(period)
                if o:
                    label = period.replace("outcome_", "")
                    lines.append(
                        f"  - {label}: 命中率 **{o['hit_rate']}%**"
                        f" ｜平均收益 `{o['avg_return']:+.2f}%`"
                        f" ｜样本 {o['count']}"
                    )

        merged = _merge_buy_stats(perf)
        if merged["count"] > 0:
            lines.append(f"\n### BUY + STRONG_BUY 合并统计")
            lines.append(f"- 总信号数: {merged['count']}")
            for period in ("outcome_1d", "outcome_3d", "outcome_5d", "outcome_10d"):
                o = merged["outcomes"].get(period)
                if o:
                    label = period.replace("outcome_", "")
                    lines.append(
                        f"  - {label}: 命中率 **{o['hit_rate']}%**"
                        f" ｜平均收益 `{o['avg_return']:+.2f}%`"
                        f" ｜样本 {o['count']}"
                    )

        total_signals = sum(d["count"] for d in perf.values())
        total_records = 0
        total_hits = 0
        for d in perf.values():
            for period in ("outcome_1d", "outcome_3d", "outcome_5d", "outcome_10d"):
                o = d["outcomes"].get(period)
                if o:
                    total_records += o["count"]
                    total_hits += o["count"] * o["hit_rate"] / 100

        lines.append(f"\n### 总览")
        lines.append(f"- 总信号数: {total_signals}")
        if total_records > 0:
            overall_hit = round(total_hits / total_records * 100, 1)
            lines.append(f"- 总盈利信号占比: **{overall_hit}%**")
        lines.append(f"- 统计周期: 近 {days} 天")
        lines.append(f"- 统计范围: 所有信号类型")

        return "\n".join(lines)

    except Exception as e:
        return f"生成绩效报告出错: {e}"


# ============================================================
# 7. CLI 入口
# ============================================================

def _fmt(val, suffix="", na="N/A"):
    """格式化数值，None 显示为 N/A"""
    if val is None:
        return na
    return f"{val}{suffix}"


def cmd_signal_performance():
    """命令行入口：更新统计表 + 打印完整报告"""
    print(f"\n📊  信号绩效分析  |  {datetime.now():%Y-%m-%d %H:%M}")
    print("=" * 72)

    # 步骤 1: 先补填 outcomes（如果还没填）
    filled = calculate_outcomes()
    if filled:
        print(f"  ✅ outcome 回填: {filled} 个字段")
    print()

    # 步骤 2: 更新统计表
    result = update_signal_performance_table()
    print(f"  📋 signal_performance 表: {result['updated']} 条写入, {result['skipped']} 条跳过")
    print()

    # 步骤 3: 全局统计
    report = get_performance_report()
    summary = report["summary"]
    print(f"📈  全局统计")
    print(f"  信号总数: {summary['total_signals']}")
    print(f"  有 outcome: {summary['signals_with_outcome']}")
    print(f"  整体 1 日胜率: {_fmt(summary['overall_win_rate_1d'], '%')}")
    print(f"  整体 1 日平均收益: {_fmt(summary['overall_avg_return_1d'], '%')}")
    if summary["best_action"]:
        print(f"  最佳信号: {summary['best_action']} ({_fmt(summary['best_action_win_rate'], '%')})")
    if summary["best_dimension"]:
        print(f"  最佳预测维度: {summary['best_dimension']} (corr={summary['best_dimension_corr']})")
    print()

    # 步骤 4: 按信号类型
    print(f"📋  信号类型绩效")
    header = f"  {'信号类型':<16s} {'总数':>5s} {'1日收益%':>8s} {'3日收益%':>8s} {'1日胜率':>8s} {'3日胜率':>8s}"
    sep = f"  {'─'*56}"
    print(header)
    print(sep)
    for s in report["signal_by_action"]:
        ar1 = _fmt(s["avg_return_1d"], "%")
        ar3 = _fmt(s["avg_return_3d"], "%")
        wr1 = _fmt(s["win_rate_1d"], "%") if s["win_rate_1d"] is not None else "   N/A"
        wr3 = _fmt(s["win_rate_3d"], "%") if s["win_rate_3d"] is not None else "   N/A"
        print(f"  {s['action']:<16s} {s['total']:>5d} {ar1:>8s} {ar3:>8s} {wr1:>8s} {wr3:>8s}")

    # 步骤 5: 维度有效性
    if report["dimensions"]:
        print()
        print(f"🔬  评分维度预测有效性 (按 |corr| 降序)")
        dim_header = f"  {'维度':<20s} {'样本':>6s} {'正收益%':>8s} {'corr_1d':>9s}  {'强区间'}"
        dim_sep = f"  {'─'*60}"
        print(dim_header)
        print(dim_sep)
        for d in report["dimensions"]:
            best_bin = max(d["bins"], key=lambda b: b["avg_return"]) if d["bins"] else {}
            bb_label = best_bin.get("range", "") if best_bin else ""
            print(
                f"  {d['dimension']:<20s} {d['samples']:>6d}"
                f" {_fmt(d['positive_pct'], '%'):>8s}"
                f" {d['rank_corr_1d']:>9.3f}"
                f"  {bb_label}"
            )

    # 步骤 6: 数据完整性
    issues = validate_data_integrity()
    if issues:
        print()
        print(f"⚠️  数据完整性提示 ({len(issues)} 条)")
        for issue in issues:
            print(f"  [{issue['severity']}] {issue['message']}")
    else:
        print()
        print("✅ 数据完整性检查通过")

    print()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cmd_signal_performance()
