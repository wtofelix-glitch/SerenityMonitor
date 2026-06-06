"""实盘信号绩效追踪模块

功能:
  1. calculate_outcomes()  — 每日收盘后补填 signal_log 的 outcome 字段
  2. generate_performance_report(days=30) — 生成绩效统计 Markdown 报告
  3. cmd_signal_performance() — 命令行调用 print 报告
"""

from db import get_unfilled_outcomes, update_signal_outcome, get_price_history, get_conn
from db import get_signal_performance


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


# ---------------------------------------------------------------------------
# 1. 每日补填 outcomes
# ---------------------------------------------------------------------------

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
                # 行情数据中没有 >= 信号日期的记录
                continue

            for field, offset in periods:
                # 已填充则跳过
                if current.get(field) is not None:
                    continue

                target_idx = start_idx + offset
                if target_idx >= len(sorted_prices):
                    # 数据不足，留待下次
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


# ---------------------------------------------------------------------------
# 2. 生成绩效报告 (Markdown)
# ---------------------------------------------------------------------------

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
            # 加权平均收益率
            total_old = m["avg_return"] * m["count"]
            m["count"] += stats["count"]
            m["avg_return"] = round(
                (total_old + stats["avg_return"] * stats["count"]) / m["count"], 2
            ) if m["count"] > 0 else 0.0
            m["_hits"] += int(stats["count"] * stats["hit_rate"] / 100)
            m["hit_rate"] = round(m["_hits"] / m["count"] * 100, 1) if m["count"] > 0 else 0.0
    return merged


def generate_performance_report(days: int = 30) -> str:
    """调用 db.get_signal_performance(days), 输出 markdown:
    - 按信号类型分组（1日/3日/5日/10日 命中率和平均收益率）
    - BUY/STRONG_BUY 合并统计
    - 总信号数、总盈利信号占比
    """
    try:
        perf = get_signal_performance(days)

        lines = []
        lines.append(f"## 📊 信号绩效报告（近 {days} 天）")

        # ---- 各 action 分项 ----
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

        # ---- BUY + STRONG_BUY 合并 ----
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

        # ---- 总览 ----
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


# ---------------------------------------------------------------------------
# 3. 命令行入口
# ---------------------------------------------------------------------------

def cmd_signal_performance():
    """命令行调用：生成并打印绩效报告"""
    try:
        report = generate_performance_report()
        print(report)
    except Exception as e:
        print(f"cmd_signal_performance 出错: {e}")
