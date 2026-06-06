"""每周策略复盘报告生成器

功能：
- generate_weekly_review() -> str: 生成周复盘 Markdown 报告
- cmd_weekly_review(): CLI 入口，生成并打印

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


# ---------------------------------------------------------------------------
# 报告生成
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


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------

def cmd_weekly_review() -> str:
    """CLI 入口：生成并打印周复盘报告"""
    report = generate_weekly_review()
    print(report)
    return report


if __name__ == "__main__":
    cmd_weekly_review()
