"""每周策略复盘报告生成器

功能：
- generate_weekly_review() -> str: 生成周复盘 Markdown 报告 (v2.x)
- generate_weekly_review_v3() -> str: 升级版 v3.0 周报（信号分解+IC变化+轮动+仓位建议）
- cmd_weekly_review(): CLI 入口，生成并打印
- cmd_weekly_review_v3(): CLI 入口，v3.0 周报

数据全部从 serenity.db 的 SQLite 获取，不依赖外部 API。
"""
from datetime import date, timedelta
from collections import defaultdict

from db import (
    get_signal_performance,
    load_all_stocks,
    get_price_history,
    get_latest_scores,
    get_recent_signals,
)
from config import STOCK_MAP, STOCK_DETAILS
from weight_adjuster import load_adjusted_weights, DEFAULT_WEIGHTS


# ---------------------------------------------------------------------------
# 辅助工具
# ---------------------------------------------------------------------------

def _bar(value: float, max_val: float, width: int = 20) -> str:
    """生成横向 ASCII 柱状条"""
    if max_val <= 0:
        return ""
    bar_len = int(abs(value) / max_val * width)
    bar_len = min(bar_len, width)
    return "█" * bar_len


def _week_range() -> tuple[str, str]:
    """返回本周一和本周五的 ISO 日期字符串"""
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    friday = monday + timedelta(days=4)
    return monday.isoformat(), friday.isoformat()


def _get_stock_name(code: str) -> str:
    """从 STOCK_MAP 获取股票中文名"""
    return STOCK_MAP.get(code, {}).get("name", code)


def _fetch_weekly_signals(days: int = 7) -> list[dict]:
    """获取本周（最近 N 天）的信号记录带 outcomes"""
    return get_recent_signals(days=days, limit=500)


def _compute_score_change(code: str, days: int = 7) -> float:
    """计算最近 N 天的总分变化（最新分 - 最差分）"""
    hist = get_price_history(code, days + 5)
    if len(hist) < 2:
        return 0.0
    # get_price_history returns DESC order; last entry is oldest
    oldest_score = None
    newest_score = hist[0].get("close", 0)
    # We use price change as proxy when scoring_history not available daily
    # But prefer scoring_history
    try:
        from db import get_conn
        conn = get_conn()
        rows = conn.execute(
            "SELECT total_score, date FROM scoring_history WHERE code=? ORDER BY date DESC LIMIT ?",
            (code, days)
        ).fetchall()
        conn.close()
        if len(rows) >= 2:
            newest_score = rows[0]["total_score"] or newest_score
            oldest_score = rows[-1]["total_score"] or oldest_score
            return round(newest_score - oldest_score, 1)
    except Exception:
        pass
    return 0.0


def _fetch_ic_trend(days: int = 14) -> dict:
    """获取各维度 IC 趋势（本周 vs 上周）"""
    result = {}
    try:
        from factor_ic import compute_rank_ic
        # This week
        this_week = compute_rank_ic(days=7, window=5)
        # Last week (approximate: days 7-14 ago)
        last_week = compute_rank_ic(days=14, window=5, offset_days=7)
        this_mean = this_week.get("mean_ic", {})
        last_mean = last_week.get("mean_ic", {})
        all_dims = set(list(this_mean.keys()) + list(last_mean.keys()))
        for dim in sorted(all_dims):
            tw = this_mean.get(dim, 0)
            lw = last_mean.get(dim, 0)
            result[dim] = {"this_week": round(tw, 4), "last_week": round(lw, 4), "change": round(tw - lw, 4)}
    except Exception:
        pass
    return result


def _fetch_sector_rotation_summary() -> list[dict]:
    """从 sector_rotation 获取本周行业排名"""
    try:
        from sector_rotation import SectorRotationEngine
        engine = SectorRotationEngine()
        return engine.get_sector_rank()
    except Exception:
        return []


def _compute_monte_carlo_estimate(target_pnl_pct: float = 100.0,
                                   avg_daily_return: float = 0.5,
                                   daily_std: float = 2.0,
                                   trading_days: int = 60,
                                   simulations: int = 2000) -> dict:
    """简化的蒙特卡洛模拟估算达到目标的概率"""
    try:
        import random
        import math

        hits = 0
        final_values = []
        for _ in range(simulations):
            value = 100.0  # starting at 100%
            for _ in range(trading_days):
                ret = random.gauss(avg_daily_return, daily_std)
                value *= (1 + ret / 100)
            final_values.append(value)
            if value >= (100 + target_pnl_pct):
                hits += 1

        final_values.sort()
        prob = hits / simulations * 100
        median = final_values[len(final_values) // 2] if final_values else 100.0
        p10 = final_values[int(len(final_values) * 0.1)] if len(final_values) > 10 else 0
        p90 = final_values[int(len(final_values) * 0.9)] if len(final_values) > 10 else 0
        return {
            "target_pnl_pct": target_pnl_pct,
            "probability_pct": round(prob, 1),
            "median_final_value_pct": round(median - 100, 1),
            "p10_pct": round(p10 - 100, 1),
            "p90_pct": round(p90 - 100, 1),
            "simulations": simulations,
            "trading_days": trading_days,
        }
    except Exception:
        return {"error": "Monte Carlo simulation failed", "probability_pct": 0}


# ---------------------------------------------------------------------------
# 报告生成 (v2.x — 向后兼容)
# ---------------------------------------------------------------------------

def generate_weekly_review() -> str:
    """生成完整周复盘策略报告（Markdown）"""
    today = date.today()
    monday_str, friday_str = _week_range()

    lines = []
    lines.append(f"📊 **Serenity Monitor | 周度策略复盘**")
    lines.append(f"   {monday_str} ~ {friday_str}")
    lines.append("")

    # ===================================================================
    # 一、本周收益总结
    # ===================================================================
    stocks = load_all_stocks()
    active = [s for s in stocks if s.get("is_active")]
    all_codes = list(STOCK_MAP.keys())

    lines.append("## 一、本周收益总结")
    lines.append("")

    # --- 持仓标的（本周涨跌幅 = 本周一收盘 → 今日收盘）---
    if active:
        lines.append("**持仓周涨跌幅（本周一 → 今日）**")
        active_rows = []
        max_abs_ret = 0.0
        for s in active:
            code = s["code"]
            hist = get_price_history(code, days=7)
            if len(hist) < 2:
                continue
            # hist 按 date DESC：hist[-1] 为最旧（本周一附近），hist[0] 为最新
            monday_close = hist[-1]["close"]
            latest_close = hist[0]["close"]
            if monday_close and monday_close > 0:
                ret = (latest_close - monday_close) / monday_close * 100
            else:
                ret = sum(
                    h["change_pct"] for h in hist if h.get("change_pct") is not None
                )
            name = _get_stock_name(code)
            active_rows.append((ret, name, code))
            max_abs_ret = max(max_abs_ret, abs(ret))

        if active_rows:
            for ret, name, code in sorted(active_rows, key=lambda x: -x[0]):
                emoji = "🔴" if ret >= 0 else "🟢"
                bar = _bar(ret, max_abs_ret, 15)
                lines.append(f"  {emoji} **{name}** ({code}) {bar} {ret:+.2f}%")
        lines.append("")

    # --- 候选标的 ---
    lines.append("**候选标的周涨跌幅**")
    cand_rows = []
    max_cand_abs = 0.0
    for code in all_codes:
        if code in {a["code"] for a in active}:
            continue
        hist = get_price_history(code, days=7)
        if len(hist) < 2:
            continue
        # hist 按 date DESC：hist[-1] 是 7 天前最早，hist[0] 是最近
        first_close = hist[-1]["close"]
        last_close = hist[0]["close"]
        if first_close and first_close > 0:
            ret = (last_close - first_close) / first_close * 100
        else:
            ret = 0
        name = _get_stock_name(code)
        cand_rows.append((ret, name, code))
        max_cand_abs = max(max_cand_abs, abs(ret))

    if cand_rows:
        for ret, name, code in sorted(cand_rows, key=lambda x: -x[0]):
            emoji = "🔴" if ret >= 0 else "🟢"
            bar = _bar(ret, max_cand_abs, 15)
            lines.append(f"  {emoji} {name} ({code}) {bar} {ret:+.2f}%")
    else:
        lines.append("  📭 无历史数据")
    lines.append("")

    # ===================================================================
    # 二、本周信号统计
    # ===================================================================
    lines.append("## 二、本周信号统计")
    lines.append("")

    sig_perf = get_signal_performance(days=7)
    total_signals = sum(v["count"] for v in sig_perf.values())

    if total_signals > 0:
        buy_count = sum(
            v["count"]
            for k, v in sig_perf.items()
            if k in ("BUY", "STRONG_BUY")
        )
        sell_count = sum(
            v["count"]
            for k, v in sig_perf.items()
            if k in ("SELL", "STOP_LOSS")
        )
        hold_count = sig_perf.get("HOLD", {}).get("count", 0)

        lines.append(f"**总信号数**: {total_signals}")
        lines.append("")

        # --- 买卖比例 ASCII 图 ---
        total_bs = buy_count + sell_count
        if total_bs > 0:
            buy_ratio = buy_count / total_bs
            sell_ratio = sell_count / total_bs
            bsw = 20
            buy_bars = int(buy_ratio * bsw)
            sell_bars = int(sell_ratio * bsw)
            lines.append("**买卖比例**")
            lines.append(
                f"  🟢 BUY   ({buy_count:>3}) "
                f"{'█' * buy_bars}{' ' * (bsw - buy_bars)} {buy_ratio:.0%}"
            )
            lines.append(
                f"  🔴 SELL  ({sell_count:>3}) "
                f"{'█' * sell_bars}{' ' * (bsw - sell_bars)} {sell_ratio:.0%}"
            )
            lines.append(f"  ⚪ HOLD  ({hold_count:>3})")
            lines.append("")

        # --- 各行动命中率表格 ---
        lines.append("**信号命中率**")
        lines.append("")
        header = f"  {'行动':<12} {'数量':>5} {'1日命中':>8} {'3日命中':>8} {'5日命中':>8}"
        lines.append(header)
        lines.append(f"  {'─' * len(header)}")
        for action in ("STRONG_BUY", "BUY", "HOLD", "SELL", "STOP_LOSS"):
            info = sig_perf.get(action)
            if not info:
                continue
            n = info["count"]
            o = info.get("outcomes", {})
            h1 = o.get("outcome_1d", {}).get("hit_rate", "-")
            h3 = o.get("outcome_3d", {}).get("hit_rate", "-")
            h5 = o.get("outcome_5d", {}).get("hit_rate", "-")
            _fmt = lambda v: f"{v:>5.1f}%" if isinstance(v, (int, float)) else "  N/A"
            lines.append(
                f"  {action:<12} {n:>5} {_fmt(h1):>8} {_fmt(h3):>8} {_fmt(h5):>8}"
            )
        lines.append("")
    else:
        lines.append("📭 本周无信号记录")
        lines.append("")

    # ===================================================================
    # 三、因子有效性排名（按 alpha_score 分组）
    # ===================================================================
    lines.append("## 三、因子有效性排名")
    lines.append("")

    recent_sigs = get_recent_signals(days=7, limit=200)

    # 按 alpha_score 每 10 分一桶分组
    alpha_groups: dict[int, list] = defaultdict(list)
    for sig in recent_sigs:
        alpha = sig.get("alpha_score")
        if alpha is None:
            continue
        bucket = (int(alpha) // 10) * 10
        alpha_groups[bucket].append(sig)

    if alpha_groups:
        bucket_stats = []
        for bucket, sigs in alpha_groups.items():
            n = len(sigs)
            hits_1d = sum(
                1 for s in sigs
                if s.get("outcome_1d") is not None and s["outcome_1d"] > 0
            )
            total_1d = sum(
                1 for s in sigs if s.get("outcome_1d") is not None
            )
            hits_3d = sum(
                1 for s in sigs
                if s.get("outcome_3d") is not None and s["outcome_3d"] > 0
            )
            total_3d = sum(
                1 for s in sigs if s.get("outcome_3d") is not None
            )
            sum_1d = sum(
                s["outcome_1d"] for s in sigs
                if s.get("outcome_1d") is not None
            )
            sum_3d = sum(
                s["outcome_3d"] for s in sigs
                if s.get("outcome_3d") is not None
            )
            hr1 = hits_1d / total_1d * 100 if total_1d > 0 else 0
            hr3 = hits_3d / total_3d * 100 if total_3d > 0 else 0
            ar1 = sum_1d / total_1d if total_1d > 0 else 0
            ar3 = sum_3d / total_3d if total_3d > 0 else 0
            bucket_stats.append((bucket, n, hr1, hr3, ar1, ar3))

        max_rate = max(b[2] for b in bucket_stats) if bucket_stats else 100

        # 表格
        lines.append(
            f"  {'α区间':<10} {'数量':>5} {'1D命中':>8} {'3D命中':>8} "
            f"{'1D均收':>8} {'3D均收':>8}"
        )
        lines.append(f"  {'─' * 55}")
        for bucket, n, hr1, hr3, ar1, ar3 in sorted(
            bucket_stats, key=lambda x: -x[2]
        ):
            lines.append(
                f"  {bucket:>3}-{bucket + 9:<5} {n:>5} "
                f"{hr1:>6.1f}% {hr3:>6.1f}% {ar1:>+6.2f}% {ar3:>+6.2f}%"
            )
        lines.append("")

        # ASCII 可视化
        lines.append("**1日命中率可视化**")
        for bucket, n, hr1, _hr3, _ar1, _ar3 in sorted(
            bucket_stats, key=lambda x: -x[2]
        ):
            bar = _bar(hr1, max_rate, 15)
            lines.append(f"  α{bucket:>2}-{bucket + 9}: {bar} {hr1:.0f}% (n={n})")
        lines.append("")
    else:
        lines.append("📭 本周无 alpha_score 数据")
        lines.append("")

    # ===================================================================
    # 四、下周关注 TOP3
    # ===================================================================
    lines.append("## 四、下周关注 TOP3")
    lines.append("")

    adj_weights = load_adjusted_weights()
    latest_all = get_latest_scores()

    if latest_all:
        # --- 动态权重状态 ---
        lines.append("**动态权重状态**")
        lines.append("")
        lines.append(
            f"  {'维度':<12} {'默认':>6} {'当前':>6} {'方向':>4}"
        )
        lines.append(f"  {'─' * 32}")
        for k in sorted(DEFAULT_WEIGHTS):
            cur = adj_weights.get(k, DEFAULT_WEIGHTS[k])
            default = DEFAULT_WEIGHTS[k]
            delta = cur - default
            arrow = "↑" if delta > 0.005 else ("↓" if delta < -0.005 else "→")
            lines.append(
                f"  {k:<12} {default:>5.0%} {cur:>5.0%} {arrow:>4}"
            )
        lines.append("")

        # --- 用动态权重计算综合评分 ---
        scored = []
        for item in latest_all:
            code = item["code"]
            if code not in STOCK_MAP:
                continue
            name = _get_stock_name(code)
            # 加权综合评分
            composite = (
                item.get("base_score", 0) * adj_weights.get("base", DEFAULT_WEIGHTS["base"])
                + item.get("zone_score", 0) * adj_weights.get("zone", DEFAULT_WEIGHTS["zone"])
                + item.get("momentum_score", 0) * adj_weights.get("momentum", DEFAULT_WEIGHTS["momentum"])
                + item.get("volume_score", 0) * adj_weights.get("volume", DEFAULT_WEIGHTS["volume"])
                + item.get("serenity_score", 0) * adj_weights.get("serenity", DEFAULT_WEIGHTS["serenity"])
                + item.get("factor_score", 0) * adj_weights.get("factor", DEFAULT_WEIGHTS["factor"])
                + item.get("technical_score", 0) * adj_weights.get("technical", DEFAULT_WEIGHTS["technical"])
            )
            scored.append({
                "code": code,
                "name": name,
                "composite": round(composite, 1),
                "total_score": item.get("total_score", 0),
                "serenity_score": item.get("serenity_score", 0),
                "factor_score": item.get("factor_score", 0),
            })

        scored.sort(key=lambda x: -x["composite"])
        top3 = scored[:3]

        lines.append("**TOP3 候选**")
        for i, s in enumerate(top3, 1):
            medals = {1: "🥇", 2: "🥈", 3: "🥉"}
            lines.append("")
            lines.append(f"  {medals.get(i, f'{i}.')} **{s['name']}** ({s['code']})")
            lines.append(f"     加权综合: {s['composite']:.1f} | 原始总分: {s['total_score']:.0f}")
            lines.append(f"     Serenity: {s['serenity_score']:.0f} | 因子: {s['factor_score']:.0f}")
            detail = STOCK_DETAILS.get(s["code"])
            if detail:
                lines.append(f"     📝 {detail.get('reason', '')[:80]}")

        # 推荐理由汇总
        lines.append("")
        lines.append("**操作建议**")
        for i, s in enumerate(top3, 1):
            detail = STOCK_DETAILS.get(s["code"])
            if detail:
                buy_zone = f"{detail['buy_zone_low']:.0f}-{detail['buy_zone_high']:.0f}"
                lines.append(
                    f"  {i}. {s['name']} — 买入区 {buy_zone}，"
                    f"目标 {detail['target_sell']:.0f}，"
                    f"标签: {detail.get('serenity_tag', '-')}"
                )
        lines.append("")
    else:
        lines.append("📭 无评分数据，请先运行评分")
        lines.append("")

    lines.append("---")
    lines.append(f"> ⏰ 生成时间: {today.isoformat()} | 数据来源: serenity.db")
    lines.append("")

    return "\n".join(lines)


# ===================================================================
# 🆕 v3.0: 升级版周报
# ===================================================================

def generate_weekly_review_v3() -> str:
    """
    v3.0 升级版周报 — 更丰富的数据维度与可执行建议

    包含:
    - 本周信号绩效分解（按行动类型）
    - 涨跌幅 TOP3 / BOTTOM3
    - 行业轮动摘要
    - IC 趋势分析（本周 vs 上周）
    - 下周监控清单（进入买入区的标的）
    - 仓位建议
    - 蒙特卡洛更新
    """
    today = date.today()
    monday_str, friday_str = _week_range()
    report_date = today.isoformat()

    lines = []
    lines.append("# 📈 **Serenity Monitor | v3.0 周度策略复盘**")
    lines.append("")
    lines.append(f"  **周期**: {monday_str} ~ {friday_str}")
    lines.append(f"  **生成**: {report_date}")
    lines.append("")

    # ===================================================================
    # 一、本周信号绩效分解（按行动类型）
    # ===================================================================
    lines.append("## 一、本周信号绩效分解")
    lines.append("")

    sig_perf = get_signal_performance(days=7)
    total_signals = sum(v["count"] for v in sig_perf.values())

    if total_signals > 0:
        sig_table = []
        for action in ("STRONG_BUY", "BUY", "CAUTION_BUY", "STRONG_HOLD", "HOLD", "WEAK_HOLD", "WATCH", "SELL", "STOP_LOSS"):
            info = sig_perf.get(action)
            if not info or info["count"] == 0:
                continue
            n = info["count"]
            o = info.get("outcomes", {})
            h1 = o.get("outcome_1d", {}).get("hit_rate", 0)
            h3 = o.get("outcome_3d", {}).get("hit_rate", 0)
            h5 = o.get("outcome_5d", {}).get("hit_rate", 0)
            r1 = o.get("outcome_1d", {}).get("avg_return", 0)
            r3 = o.get("outcome_3d", {}).get("avg_return", 0)
            r5 = o.get("outcome_5d", {}).get("avg_return", 0)
            sig_table.append((action, n, h1, h3, h5, r1, r3, r5))

        if sig_table:
            lines.append("| 信号类型 | 数量 | 1日胜率 | 3日胜率 | 5日胜率 | 1日均收 | 3日均收 | 5日均收 |")
            lines.append("|----------|------|---------|---------|---------|---------|---------|---------|")
            best_3d_hit = max(s[3] for s in sig_table) if sig_table else 0
            for action, n, h1, h3, h5, r1, r3, r5 in sig_table:
                star = "⭐" if h3 >= best_3d_hit and n >= 3 else ""
                lines.append(
                    f"| {action:<12s} | {n:>4d} | {h1:>5.1f}% | {h3:>5.1f}% | {h5:>5.1f}% | "
                    f"{r1:>+6.2f}% | {r3:>+6.2f}% | {r5:>+6.2f}% | {star}"
                )
        lines.append("")

        # 买卖信号汇总
        buy_types = {"STRONG_BUY", "BUY", "CAUTION_BUY"}
        sell_types = {"SELL", "STOP_LOSS"}
        buy_cnt = sum(info["count"] for k, info in sig_perf.items() if k in buy_types)
        sell_cnt = sum(info["count"] for k, info in sig_perf.items() if k in sell_types)
        hold_cnt = sum(info["count"] for k, info in sig_perf.items() if k not in buy_types | sell_types)
        lines.append(f"  🔹 **买入信号**: {buy_cnt} | **卖出信号**: {sell_cnt} | **持有/观望**: {hold_cnt}")
        buy_sell_ratio = round(buy_cnt / max(sell_cnt, 1), 2)
        lines.append(f"  🔹 **买卖比**: {buy_sell_ratio}")
        lines.append("")
    else:
        lines.append("  📭 本周无信号记录")
        lines.append("")

    # ===================================================================
    # 二、评分变化 TOP3 / BOTTOM3
    # ===================================================================
    lines.append("## 二、周评分变化 TOP3 / BOTTOM3")
    lines.append("")

    score_changes = []
    for code in STOCK_MAP:
        delta = _compute_score_change(code, days=7)
        if delta != 0.0:
            score_changes.append((delta, code, _get_stock_name(code)))

    score_changes.sort(key=lambda x: -x[0])

    if score_changes:
        top3 = score_changes[:3]
        bottom3 = score_changes[-3:] if len(score_changes) >= 3 else score_changes

        lines.append("**评分上升 TOP3**")
        for delta, code, name in top3:
            lines.append(f"  🔺 **{name}** ({code}) {delta:+.1f}分")

        lines.append("")
        lines.append("**评分下降 BOTTOM3**")
        for delta, code, name in reversed(bottom3):
            lines.append(f"  🔻 **{name}** ({code}) {delta:+.1f}分")
        lines.append("")

        # 评分集中度
        avg_delta = sum(d for d, _, _ in score_changes) / len(score_changes)
        std_delta = (sum((d - avg_delta) ** 2 for d, _, _ in score_changes) / len(score_changes)) ** 0.5
        lines.append(f"  📊 **评分变化平均**: {avg_delta:+.1f} | **标准差**: {std_delta:.1f}")
        if std_delta > 8:
            lines.append(f"  ⚠️ 评分分化较大(σ>{8:.0f})，注意个股间风格切换")
        lines.append("")
    else:
        lines.append("  📭 评分变化数据不足")
        lines.append("")

    # ===================================================================
    # 三、行业轮动摘要
    # ===================================================================
    lines.append("## 三、行业轮动摘要")
    lines.append("")

    sector_ranks = _fetch_sector_rotation_summary()
    if sector_ranks:
        lines.append("| 排名 | 行业 | 周涨跌幅 | 动量状态 |")
        lines.append("|------|------|----------|----------|")
        for r in sector_ranks:
            emoji = {"strong": "🔴", "neutral": "⚪", "weak": "🟢"}.get(r["momentum"], "⚪")
            lines.append(f"| #{r['rank']} | {r['sector']} | {r['change']:+.2f}% | {emoji} {r['momentum']} |")
        lines.append("")

        # Rotation signal
        top_change = sector_ranks[0]["change"] if sector_ranks else 0
        bottom_change = sector_ranks[-1]["change"] if sector_ranks else 0
        gap = abs(top_change - bottom_change)
        if gap > 8:
            lines.append(f"  ⚡ **轮动信号**: 行业分化({gap:.1f}pp)，建议关注领先行业")
        elif gap < 3:
            lines.append(f"  📊 **轮动信号**: 行业普同({gap:.1f}pp)，无显著轮动")
        else:
            lines.append(f"  📊 **轮动信号**: 正常分化({gap:.1f}pp)")
        lines.append("")
    else:
        lines.append("  📭 行业数据不足，跳过轮动分析")
        lines.append("")

    # ===================================================================
    # 四、IC 趋势分析
    # ===================================================================
    lines.append("## 四、IC 趋势分析（本周 vs 上周）")
    lines.append("")

    ic_trend = _fetch_ic_trend()
    if ic_trend:
        lines.append("| 维度 | 本周IC | 上周IC | 变化 | 方向 |")
        lines.append("|------|--------|--------|------|------|")
        for dim, vals in sorted(ic_trend.items(), key=lambda x: -abs(x[1].get("change", 0))):
            tw = vals.get("this_week", 0)
            lw = vals.get("last_week", 0)
            chg = vals.get("change", 0)
            arrow = "🔥" if chg > 0.02 else ("❄️" if chg < -0.02 else "➡️")
            lines.append(f"| {dim:<20s} | {tw:+.4f} | {lw:+.4f} | {chg:+.4f} | {arrow} |")
        lines.append("")

        # IC 正负统计
        pos_count = sum(1 for v in ic_trend.values() if v.get("this_week", 0) > 0)
        neg_count = sum(1 for v in ic_trend.values() if v.get("this_week", 0) <= 0)
        total_ic = len(ic_trend)
        lines.append(f"  🔹 **正IC维度**: {pos_count}/{total_ic} | **负IC维度**: {neg_count}/{total_ic}")
        improving = sum(1 for v in ic_trend.values() if v.get("change", 0) > 0.01)
        declining = sum(1 for v in ic_trend.values() if v.get("change", 0) < -0.01)
        lines.append(f"  🔹 **IC上升**: {improving} | **IC下降**: {declining}")
        lines.append("")
    else:
        lines.append("  📭 IC 数据不足")
        lines.append("")

    # ===================================================================
    # 五、下周监控清单
    # ===================================================================
    lines.append("## 五、下周监控清单")
    lines.append("")

    latest_all = get_latest_scores()
    if latest_all:
        entering_buy_zone = []
        for item in latest_all:
            code = item["code"]
            detail = STOCK_DETAILS.get(code, {})
            if not detail:
                continue
            name = _get_stock_name(code)
            total = item.get("total_score", 0) or 0
            zone_low = detail.get("buy_zone_low", 0)
            zone_high = detail.get("buy_zone_high", 0)

            # Stocks entering buy zone from above (score dropping into range)
            # Or stocks with score near buy threshold
            buy_threshold = 66  # v3.0 BUY threshold
            near_buy = buy_threshold - 8 <= total <= buy_threshold + 5

            # In buy zone price-wise
            try:
                hist = get_price_history(code, 3)
                price = hist[0]["close"] if hist else 0
            except Exception:
                price = 0
            in_zone_price = zone_low > 0 and zone_high > 0 and zone_low <= price <= zone_high
            below_zone = zone_low > 0 and price < zone_low

            entering_buy_zone.append({
                "code": code,
                "name": name,
                "score": total,
                "price": price,
                "zone_low": zone_low,
                "zone_high": zone_high,
                "near_buy": near_buy,
                "in_zone": in_zone_price,
                "below_zone": below_zone,
            })

        # Sort: highest score first
        entering_buy_zone.sort(key=lambda x: -x["score"])

        if entering_buy_zone:
            lines.append("**即将进入买入区的标的**")
            lines.append("")
            lines.append("| 标的 | 评分 | 现价 | 买入区 | 状态 |")
            lines.append("|------|------|------|--------|------|")
            for e in entering_buy_zone:
                if e["below_zone"]:
                    status = "📉 低于买入区(折扣)"
                elif e["in_zone"]:
                    status = "✅ 在买入区内"
                elif e["near_buy"]:
                    pct_to_low = round((e["price"] - e["zone_low"]) / e["zone_low"] * 100, 1) if e["zone_low"] > 0 else 0
                    status = f"👀 接近买入区(+{pct_to_low:.1f}%)"
                else:
                    status = "📊 观望"
                zone_str = f"{e['zone_low']:.0f}-{e['zone_high']:.0f}" if e["zone_low"] > 0 else "N/A"
                lines.append(f"| {e['name']:<6s} | {e['score']:>5.1f} | {e['price']:>7.2f} | {zone_str:>8s} | {status} |")
            lines.append("")
        else:
            lines.append("  📭 无数据")
            lines.append("")
    else:
        lines.append("  📭 无评分数据")
        lines.append("")

    # ===================================================================
    # 六、仓位建议
    # ===================================================================
    lines.append("## 六、下周仓位建议")
    lines.append("")

    try:
        from config import CAPITAL_CONFIG, get_effective_config
        eff = get_effective_config()
        cap = eff.get("capital", CAPITAL_CONFIG)

        init_cap = cap.get("initial_capital", 51066)
        target_cap = cap.get("target_capital", 102133)
        max_pos = cap.get("max_positions", 2)
        min_weight = cap.get("min_single_weight", 0.10)
        max_weight = cap.get("max_single_weight", 0.85)
        aggr = cap.get("aggressive_mode", False)

        lines.append(f"  🔹 **启动资金**: {init_cap:.2f} | **目标**: {target_cap:.2f}")
        lines.append(f"  🔹 **最大持仓数**: {max_pos}")
        lines.append(f"  🔹 **单票权重**: {min_weight:.0%} ~ {max_weight:.0%}")
        lines.append(f"  🔹 **模式**: {'🚀 激进翻倍' if aggr else '🛡️ 保守'}")

        # Position suggestion based on number of buy signals this week
        buy_cnt_week = sum(1 for _, info in sig_perf.items()
                           if info.get("count", 0) > 0 and _ in buy_types)
        if buy_cnt_week >= 3:
            pos_rec = f"建议开仓 {min(max_pos, 2)} 只，集中资金在评分最高的标的上"
        elif buy_cnt_week >= 1:
            pos_rec = f"建议保持谨慎，最多开仓 1 只新标，优先处理现有持仓"
        else:
            pos_rec = "本周无明显买入信号，建议空仓或仅持有高股息防御标的"

        lines.append(f"  📋 **操作建议**: {pos_rec}")

        # Risk reminder
        try:
            from market_sense import MarketSense
            ms = MarketSense()
            regime = ms.get_market_regime().get("regime_label", "震荡市")
            lines.append(f"  🌡️ **市场状态**: {regime}")
            if regime in ("熊市", "震荡市"):
                lines.append(f"  ⚠️ **风控提示**: {regime}环境下收紧止损至-3%，减少新开仓")
            elif regime in ("牛市", "结构性牛市"):
                lines.append(f"  ✅ **风控提示**: 趋势市中允许利润奔跑，跟踪止盈+10%→+8%")
        except Exception:
            pass

        lines.append("")
    except Exception:
        lines.append("  📭 仓位配置数据不足")
        lines.append("")

    # ===================================================================
    # 七、蒙特卡洛更新
    # ===================================================================
    lines.append("## 七、蒙特卡洛模拟 — 翻倍概率")
    lines.append("")

    # Use historical avg daily return and std from recent signals outcomes
    try:
        all_signals = get_recent_signals(days=30, limit=500)
        outcomes_1d = [s.get("outcome_1d") for s in all_signals if s.get("outcome_1d") is not None]
        if outcomes_1d:
            avg_daily = sum(outcomes_1d) / len(outcomes_1d)
            variance = sum((o - avg_daily) ** 2 for o in outcomes_1d) / len(outcomes_1d)
            daily_std = variance ** 0.5 if variance > 0 else 2.0
        else:
            avg_daily = 0.3
            daily_std = 2.0

        mc = _compute_monte_carlo_estimate(
            target_pnl_pct=100.0,
            avg_daily_return=avg_daily,
            daily_std=min(daily_std, 5.0),  # cap volatility
            trading_days=60,
            simulations=2000,
        )

        if "error" not in mc:
            lines.append(f"  🎯 **目标**: 60个交易日内翻倍 (+100%)")
            lines.append(f"  📊 **模拟次数**: {mc.get('simulations', 0)} 次")
            lines.append(f"  🎲 **翻倍概率**: **{mc['probability_pct']:.1f}%**")
            lines.append(f"  📈 **中位数最终值**: {mc['median_final_value_pct']:+.1f}%")
            lines.append(f"  📉 **P10 悲观情景**: {mc['p10_pct']:+.1f}%")
            lines.append(f"  📈 **P90 乐观情景**: {mc['p90_pct']:+.1f}%")
            lines.append("")

            prob = mc["probability_pct"]
            if prob >= 30:
                lines.append(f"  ✅ **评估**: 翻倍概率 {prob:.0f}% — 策略执行到位，保持仓位集中")
            elif prob >= 15:
                lines.append(f"  ⚠️ **评估**: 翻倍概率 {prob:.0f}% — 中等偏弱，需要提高信号胜率")
            else:
                lines.append(f"  ❌ **评估**: 翻倍概率 {prob:.0f}% — 当前配置难以达到目标，建议调整策略")
            lines.append("")

            # Probability bar
            bar_len = int(prob / 100 * 20)
            lines.append(f"  {'█' * bar_len}{'░' * (20 - bar_len)} {prob:.1f}%")
            lines.append("")
        else:
            lines.append(f"  ⚠️ {mc.get('error', '模拟失败')}")
            lines.append("")
    except Exception:
        lines.append("  📭 蒙特卡洛数据不足，跳过")
        lines.append("")

    # ===================================================================
    # 八、本周小结与下周行动
    # ===================================================================
    lines.append("## 八、下周行动清单")
    lines.append("")

    lines.append("- [ ] 运行 `python3 cli.py rescore` 重新评分")
    lines.append("- [ ] 运行 `python3 cli.py signal` 生成最新信号")
    lines.append("- [ ] 检查持仓止损止盈线")
    lines.append("- [ ] 检查进入买入区的监控标的")
    lines.append("- [ ] 如有新买入信号，执行 `python3 auto_execute.py`")

    # Auto-suggested actions based on data
    if ic_trend:
        worst_ic = min(ic_trend.items(), key=lambda x: x[1].get("this_week", 0))
        best_ic = max(ic_trend.items(), key=lambda x: x[1].get("this_week", 0))
        if worst_ic[1].get("this_week", 0) < -0.05:
            lines.append(f"  - ⚠️ {worst_ic[0]} IC={worst_ic[1]['this_week']:.3f} 持续为负,建议检查该维度权重")
        if best_ic[1].get("this_week", 0) > 0.05:
            lines.append(f"  - ✅ {best_ic[0]} IC={best_ic[1]['this_week']:.3f} 表现优异,可适当上调权重")

    lines.append("")
    lines.append("---")
    lines.append(f"> ⏰ **生成时间**: {report_date} | **数据来源**: serenity.db | **引擎**: v3.0")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------

def cmd_weekly_review() -> str:
    """CLI 入口：生成并打印周复盘报告（v2.x 兼容）"""
    report = generate_weekly_review()
    print(report)
    return report


def cmd_weekly_review_v3() -> str:
    """CLI 入口：生成并打印 v3.0 升级版周报"""
    report = generate_weekly_review_v3()
    print(report)
    return report


if __name__ == "__main__":
    cmd_weekly_review()
