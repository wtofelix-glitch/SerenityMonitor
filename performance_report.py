"""实盘绩效看板 — 图表 + Markdown 文本摘要

功能:
  1. generate_performance_chart(days=30) — 生成 P&L 图表 PNG
  2. generate_report_text() — 生成 Markdown 文本摘要
  3. cmd_perf_report() — 调用生成图表并 print 文本摘要
"""
from datetime import date, timedelta
from collections import defaultdict
from typing import Optional

from db import get_conn, get_signal_performance, get_recent_signals

CHART_PATH = "/Users/mac/workspace/SerenityMonitor/performance_report.png"


# ---------------------------------------------------------------------------
# 辅助: 将 STRONG_BUY → BUY, STOP_LOSS → SELL
# ---------------------------------------------------------------------------
def _normalize_action(action: str) -> str:
    """将动作归一化为 BUY / SELL / HOLD 三大类"""
    if action in ("STRONG_BUY",):
        return "BUY"
    if action in ("STOP_LOSS",):
        return "SELL"
    return action  # BUY / SELL / HOLD 保持原样


# ---------------------------------------------------------------------------
# 1. 生成 P&L 图表
# ---------------------------------------------------------------------------
def generate_performance_chart(days: int = 30) -> Optional[str]:
    """生成四子图 P&L 图表，保存到 performance_report.png。
    图表生成失败时优雅降级（返回 None），成功返回图片路径。
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np

        # ---- 中文 Fallback 字体 ----
        plt.rcParams["font.sans-serif"] = [
            "Arial Unicode MS", "Heiti SC", "PingFang SC",
        ]
        plt.rcParams["axes.unicode_minus"] = False

    except ImportError:
        print("[绩效看板] matplotlib 不可用，跳过图表生成")
        return None

    try:
        # ================================================================
        # 数据准备
        # ================================================================
        sig_perf = get_signal_performance(days)
        recent_sigs = get_recent_signals(code=None, days=days, limit=200)

        # ---- 1) 按时间排序的信号（用于累计 P&L）----
        signals_sorted = sorted(
            recent_sigs,
            key=lambda r: (r.get("date", ""), r.get("time", "")),
        )
        # 过滤出有 outcome_1d 的记录
        pnl_signals = [
            s for s in signals_sorted
            if s.get("outcome_1d") is not None
        ]
        cumulative = []
        running = 0.0
        for s in pnl_signals:
            running += s["outcome_1d"]
            cumulative.append(running)

        # ---- 2) 按归一化 action 分组收集 avg_return（用于柱状图）----
        action_returns: dict[str, list[float]] = defaultdict(list)
        for s in recent_sigs:
            val = s.get("outcome_1d")
            if val is not None:
                act = _normalize_action(s.get("action", ""))
                action_returns[act].append(val)

        bar_actions = ["BUY", "SELL", "HOLD"]
        bar_avgs = []
        for a in bar_actions:
            vals = action_returns.get(a, [])
            bar_avgs.append(round(sum(vals) / len(vals), 2) if vals else 0)

        # ---- 3) 每日信号数（用于左下柱状图）----
        daily_counts: dict[str, int] = defaultdict(int)
        for s in signals_sorted:
            daily_counts[s.get("date", "")] += 1
        daily_dates = sorted(daily_counts.keys())
        daily_vals = [daily_counts[d] for d in daily_dates]

        # ---- 4) 盈亏分布（用于右下饼图）----
        win = sum(1 for s in pnl_signals if s["outcome_1d"] > 0)
        lose = sum(1 for s in pnl_signals if s["outcome_1d"] <= 0)
        flat = sum(1 for s in pnl_signals if s["outcome_1d"] == 0)

        # ================================================================
        # 绘图
        # ================================================================
        fig, axes = plt.subplots(2, 2, figsize=(14, 10))
        fig.suptitle(f"实盘绩效看板（近 {days} 天）", fontsize=16, fontweight="bold")

        # ---- 左上：累计 P&L 曲线 ----
        ax1 = axes[0, 0]
        if cumulative:
            x = range(len(cumulative))
            ax1.plot(x, cumulative, color="#E74C3C", linewidth=1.8, marker="o", markersize=3)
            ax1.fill_between(x, cumulative, alpha=0.15, color="#E74C3C")
        ax1.set_title("累计 P&L 曲线", fontsize=12)
        ax1.set_xlabel("信号序号（按时间）")
        ax1.set_ylabel("累计收益 (%)")
        ax1.axhline(y=0, color="gray", linestyle="--", linewidth=0.7)
        ax1.grid(True, alpha=0.3)

        # ---- 右上：按信号类型分组柱状图 ----
        ax2 = axes[0, 1]
        colors_bar = ["#27AE60" if v >= 0 else "#E74C3C" for v in bar_avgs]
        ax2.bar(bar_actions, bar_avgs, color=colors_bar, width=0.5, edgecolor="black")
        for i, v in enumerate(bar_avgs):
            y_pos = v + 0.05 if v >= 0 else v - 0.05
            ax2.text(i, y_pos, f"{v:+.2f}%", ha="center", va="bottom" if v >= 0 else "top",
                     fontsize=10, fontweight="bold")
        ax2.set_title("信号类型平均收益（1日）", fontsize=12)
        ax2.set_ylabel("平均收益率 (%)")
        ax2.axhline(y=0, color="gray", linestyle="--", linewidth=0.7)
        ax2.grid(True, axis="y", alpha=0.3)

        # ---- 左下：每日信号数 ----
        ax3 = axes[1, 0]
        if daily_dates:
            short_dates = [d[-5:] for d in daily_dates]  # 只显示 MM-DD
            ax3.bar(range(len(daily_dates)), daily_vals, color="#3498DB", width=0.6, edgecolor="black")
            ax3.set_xticks(range(len(daily_dates)))
            ax3.set_xticklabels(short_dates, rotation=45, fontsize=8)
        ax3.set_title("每日信号数统计", fontsize=12)
        ax3.set_ylabel("信号数")
        ax3.grid(True, axis="y", alpha=0.3)

        # ---- 右下：盈亏分布饼图 ----
        ax4 = axes[1, 1]
        pie_labels = []
        pie_sizes = []
        pie_colors = []
        explode = []
        if win > 0:
            pie_labels.append(f"盈利 ({win})")
            pie_sizes.append(win)
            pie_colors.append("#27AE60")
            explode.append(0.05)
        if lose - flat > 0:
            pie_labels.append(f"亏损 ({lose - flat})")
            pie_sizes.append(lose - flat)
            pie_colors.append("#E74C3C")
            explode.append(0.05)
        if flat > 0:
            pie_labels.append(f"持平 ({flat})")
            pie_sizes.append(flat)
            pie_colors.append("#95A5A6")
            explode.append(0.05)

        if pie_sizes:
            ax4.pie(
                pie_sizes, labels=pie_labels, autopct="%1.1f%%",
                colors=pie_colors, explode=explode,
                startangle=90, shadow=True,
                textprops={"fontsize": 10},
            )
        ax4.set_title("盈亏分布（1日）", fontsize=12)

        plt.tight_layout(rect=[0, 0, 1, 0.95])
        plt.savefig(CHART_PATH, dpi=150, bbox_inches="tight")
        plt.close(fig)

        print(f"[绩效看板] 图表已保存: {CHART_PATH}")
        return CHART_PATH

    except Exception as e:
        print(f"[绩效看板] 图表生成失败: {e}")
        # 尝试关闭可能残留的 figure
        try:
            import matplotlib.pyplot as plt
            plt.close("all")
        except Exception:
            pass
        return None


# ---------------------------------------------------------------------------
# 2. 生成 Markdown 文本摘要
# ---------------------------------------------------------------------------
def generate_report_text(days: int = 30) -> str:
    """生成纯文本 Markdown 绩效摘要（不含图表）"""
    try:
        perf = get_signal_performance(days)
        recent = get_recent_signals(code=None, days=days, limit=200)

        lines = []
        lines.append(f"## 📊 实盘绩效看板（近 {days} 天）")
        lines.append("")

        total_signals = sum(v["count"] for v in perf.values())
        lines.append(f"- **总信号数**: {total_signals}")
        lines.append("")

        # ---- 信号类型统计 ----
        lines.append("### 按信号类型统计")
        lines.append("")
        header = f"  {'类型':<12} {'数量':>5} {'1D命中率':>8} {'1D均收益':>9} {'3D命中率':>8} {'3D均收益':>9}"
        lines.append(header)
        lines.append(f"  {'─' * len(header)}")
        for action in sorted(perf.keys()):
            d = perf[action]
            n = d["count"]
            o = d.get("outcomes", {})
            h1 = o.get("outcome_1d", {}).get("hit_rate", None)
            a1 = o.get("outcome_1d", {}).get("avg_return", None)
            h3 = o.get("outcome_3d", {}).get("hit_rate", None)
            a3 = o.get("outcome_3d", {}).get("avg_return", None)

            def fmt(v, suffix=""):
                if v is None:
                    return "   N/A"
                return f"{v:>5.1f}{suffix}" if isinstance(v, (int, float)) else "   N/A"

            lines.append(
                f"  {action:<12} {n:>5} {fmt(h1, '%'):>8} {fmt(a1, '%'):>9}"
                f" {fmt(h3, '%'):>8} {fmt(a3, '%'):>9}"
            )

        lines.append("")

        # ---- BUY 合并统计 ----
        buy_count = sum(
            v["count"] for k, v in perf.items()
            if k in ("BUY", "STRONG_BUY")
        )
        lines.append(f"### BUY+STRONG_BUY 合并")
        lines.append(f"- 总买入信号: {buy_count}")
        lines.append("")

        # ---- 每日信号密度 ----
        daily_count = defaultdict(int)
        for s in recent:
            daily_count[s.get("date", "")] += 1
        if daily_count:
            lines.append("### 每日信号密度")
            lines.append("")
            for dt in sorted(daily_count.keys()):
                n = daily_count[dt]
                bar = "█" * min(n, 30)
                lines.append(f"  {dt}: {bar} {n}")
            lines.append("")

        # ---- 胜率概览 ----
        total_1d = 0
        total_1d_hits = 0
        for d in perf.values():
            o = d.get("outcomes", {}).get("outcome_1d")
            if o:
                total_1d += o["count"]
                total_1d_hits += o["count"] * o["hit_rate"] / 100

        if total_1d > 0:
            win_rate = round(total_1d_hits / total_1d * 100, 1)
            lines.append(f"### 总览")
            lines.append(f"- **综合 1 日命中率**: {win_rate}%")
            lines.append(f"- **统计样本数**: {total_1d}")
            lines.append(f"- **统计周期**: 近 {days} 天")

        return "\n".join(lines)

    except Exception as e:
        return f"生成文本报告出错: {e}"


# ---------------------------------------------------------------------------
# 3. CLI 入口
# ---------------------------------------------------------------------------
def cmd_perf_report(days: int = 30):
    """生成图表 + 打印文本摘要"""
    # 尝试生成图表
    chart_path = generate_performance_chart(days=days)

    # 打印文本摘要
    text = generate_report_text(days=days)
    print(text)

    # 如果图表生成成功，告知路径
    if chart_path:
        print(f"\n📈 图表已同步生成: {chart_path}")
        print(f"   MEDIA:{chart_path}")


if __name__ == "__main__":
    cmd_perf_report()
