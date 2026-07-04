#!/usr/bin/env python3
"""
Serenity 量化周报生成器 — 每周日自动生成 Markdown 报告
内容: 本周信号胜率 · 净值对比 · 决策复盘 · IC进化 · 策略参数变化
推送: 微信/Telegram
"""

from datetime import date, datetime, timedelta
import json

from serenity_logger import get_logger

log = get_logger(__name__)


def generate_weekly_report() -> str:
    """生成完整周报 Markdown"""
    today = date.today()
    week_start = today - timedelta(days=today.weekday())  # 周一
    week_start_str = week_start.isoformat()
    today_str = today.isoformat()

    lines = []
    lines.append(f"# 📊 Serenity 量化周报")
    lines.append(f"**{week_start_str} ~ {today_str}** | 自动生成于 {datetime.now().strftime('%H:%M')}")
    lines.append("")

    # ═══ 1. 信号表现 ═══
    lines.append("## 🎯 本周信号")
    try:
        from db import get_conn
        conn = get_conn()

        # 信号统计
        sig_rows = conn.execute(
            "SELECT action, COUNT(*) as n, ROUND(AVG(total_score),1) as avg_score, "
            "ROUND(AVG(CASE WHEN outcome_5d IS NOT NULL THEN outcome_5d END),2) as avg_5d, "
            "ROUND(SUM(CASE WHEN outcome_5d>0 THEN 1.0 ELSE 0.0 END)/COUNT(*)*100,1) as wr "
            "FROM signal_log WHERE date BETWEEN ? AND ? "
            "GROUP BY action ORDER BY n DESC",
            (week_start_str, today_str),
        ).fetchall()

        total = sum(r["n"] for r in sig_rows)
        wins = sum(r["n"] * r["wr"] / 100 for r in sig_rows if r["wr"])
        overall_wr = wins / total * 100 if total > 0 else 0

        lines.append(f"| 信号 | 次数 | 均分 | 5日均收益 | 胜率 |")
        lines.append(f"|------|------|------|----------|------|")
        for r in sig_rows:
            icon = "🟢" if (r["avg_5d"] or 0) > 0 else "🔴"
            lines.append(f"| {icon} {r['action']} | {r['n']} | {r['avg_score']} | {r['avg_5d']}% | {r['wr']}% |")
        lines.append(f"| **合计** | **{total}** | — | — | **{overall_wr:.1f}%** |")
        lines.append("")
    except Exception as e:
        lines.append(f"⚠️ 信号统计失败: {e}")
        lines.append("")

    # ═══ 2. 净值对比 ═══
    lines.append("## 💰 净值曲线")
    try:
        nav_rows = conn.execute(
            "SELECT date, total_value, profit_pct FROM nav_history WHERE date BETWEEN ? AND ? ORDER BY date",
            (week_start_str, today_str),
        ).fetchall()

        if len(nav_rows) >= 2:
            start_val = nav_rows[0]["total_value"]
            end_val = nav_rows[-1]["total_value"]
            week_pnl = (end_val - start_val) / start_val * 100
            lines.append(f"| 日期 | 总资产 | 累计盈亏 |")
            lines.append(f"|------|--------|---------|")
            for r in nav_rows:
                lines.append(f"| {r['date']} | ¥{r['total_value']:,.0f} | {r['profit_pct']:+.2f}% |")
            lines.append(f"")
            lines.append(f"📈 本周收益: **{week_pnl:+.2f}%** | 期末净值: **¥{end_val:,.0f}**")
            lines.append("")
    except Exception as e:
        lines.append(f"⚠️ 净值数据不足")
        lines.append("")

    # ═══ 3. 决策复盘 ═══
    lines.append("## 🧠 决策复盘")
    try:
        # 最优决策: 5日收益最高的买入信号
        best = conn.execute(
            "SELECT code, name, action, total_score, outcome_5d, date FROM signal_log "
            "LEFT JOIN stocks ON signal_log.code = stocks.code "
            "WHERE outcome_5d IS NOT NULL AND date BETWEEN ? AND ? "
            "AND action IN ('BUY','CAUTION_BUY','STRONG_BUY') "
            "ORDER BY outcome_5d DESC LIMIT 3",
            (week_start_str, today_str),
        ).fetchall()

        # 最差决策
        worst = conn.execute(
            "SELECT code, name, action, total_score, outcome_5d, date FROM signal_log "
            "LEFT JOIN stocks ON signal_log.code = stocks.code "
            "WHERE outcome_5d IS NOT NULL AND date BETWEEN ? AND ? "
            "ORDER BY outcome_5d ASC LIMIT 3",
            (week_start_str, today_str),
        ).fetchall()

        lines.append(f"**🏆 最佳决策**")
        for r in best:
            lines.append(f"- {r['date']}: {r['name']}({r['code']}) {r['action']} {r['total_score']:.0f}分 → **+{r['outcome_5d']:.2f}%**")
        lines.append(f"")
        lines.append(f"**💀 最差决策**")
        for r in worst:
            lines.append(f"- {r['date']}: {r['name']}({r['code']}) {r['action']} {r['total_score']:.0f}分 → **{r['outcome_5d']:.2f}%**")
        lines.append("")

        # 交易日志反思回填
        from db import auto_fill_journal_reflections
        filled = auto_fill_journal_reflections()
        if filled.get("filled", 0) > 0:
            lines.append(f"📝 自动回填交易反思: {filled['filled']} 条")
            lines.append("")
    except Exception:
        pass

    # ═══ 4. IC 进化趋势 ═══
    lines.append("## 🧬 IC 进化")
    try:
        ref_rows = conn.execute(
            "SELECT date, dimension_ic FROM score_reflections WHERE date BETWEEN ? AND ? AND dimension_ic IS NOT NULL ORDER BY date DESC LIMIT 30",
            (week_start_str, today_str),
        ).fetchall()

        if ref_rows:
            # 聚合各维度IC
            dim_avg: dict[str, list[float]] = {}
            for r in ref_rows:
                ics = json.loads(r["dimension_ic"]) if isinstance(r["dimension_ic"], str) else (r["dimension_ic"] or {})
                for dim, val in ics.items():
                    dim_clean = dim.replace("_score", "").replace("_ic", "")
                    dim_avg.setdefault(dim_clean, []).append(float(val))

            lines.append(f"| 维度 | 本周均IC | 方向 |")
            lines.append(f"|------|---------|------|")
            for dim, vals in sorted(dim_avg.items()):
                avg = sum(vals) / len(vals)
                direction = "✅ 有效" if avg > 0.05 else "🟡 弱" if avg > -0.05 else "❌ 反转"
                lines.append(f"| {dim} | {avg:.3f} | {direction} |")
            lines.append("")
    except Exception:
        pass

    conn.close()

    # ═══ 5. 策略参数变化 ═══
    lines.append("## ⚙️ 策略参数")
    try:
        from config import CAPITAL_CONFIG
        lines.append(f"- 最大持仓: {CAPITAL_CONFIG['max_positions']} 只")
        lines.append(f"- 初始资金: ¥{CAPITAL_CONFIG['initial_capital']:,.0f}")
        lines.append(f"- 目标资金: ¥{CAPITAL_CONFIG['target_capital']:,.0f}")
        lines.append(f"- 目标周期: {CAPITAL_CONFIG['target_months']} 个月")
        lines.append(f"- Kelly基线: 0.20 (NAV增长自动提升)")

        # 最新参数优化
        conn2 = __import__('db').get_conn()
        opt = conn2.execute("SELECT * FROM param_optimization ORDER BY date DESC LIMIT 5").fetchall()
        conn2.close()
        if opt:
            lines.append(f"- 最近优化: {opt[0]['date']} | buy_th={opt[0].get('best_value','?')}")
        lines.append("")
    except Exception:
        pass

    # ═══ Footer ═══
    lines.append("---")
    lines.append(f"*报告由 Serenity v5.2 自动生成 · {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")

    return "\n".join(lines)


def push_weekly_report(report: str) -> dict:
    """推送周报到 Telegram + 微信(通过Hermes)"""
    results = {"telegram": False, "wechat": False, "file": ""}

    # 保存报告
    today_str = date.today().isoformat()
    report_dir = "/Users/mac/workspace/SerenityMonitor/reports"
    import os
    os.makedirs(report_dir, exist_ok=True)
    filename = f"{report_dir}/weekly_{today_str}.md"
    with open(filename, "w") as f:
        f.write(report)
    results["file"] = filename
    log.info("周报已保存: %s", filename)

    # Telegram 推送(摘要版)
    try:
        import subprocess
        summary = "\n".join(report.split("\n")[:40])  # 前40行作为摘要
        tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "")
        if tg_token and tg_chat:
            cmd = f'curl -s -X POST "https://api.telegram.org/bot{tg_token}/sendMessage" -d "chat_id={tg_chat}" -d "text={summary}" -d "parse_mode=Markdown" --max-time 10'
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=15)
        results["telegram"] = "ok" in result.stdout.lower()
    except Exception as e:
        log.warning("Telegram推送失败: %s", e)

    # 微信推送(通过Hermes delivery)
    try:
        with open(filename) as f:
            content = f.read()
        # 写到 Hermes 投递目录
        delivery_path = os.path.expanduser("~/.hermes/pending_deliveries/weekly_report.md")
        os.makedirs(os.path.dirname(delivery_path), exist_ok=True)
        with open(delivery_path, "w") as f:
            f.write(content)
        results["wechat"] = True
        log.info("周报已推送到微信投递队列")
    except Exception as e:
        log.warning("微信推送失败: %s", e)

    return results


def main():
    import sys
    do_push = "--push" in sys.argv

    print("📊 生成量化周报...")
    report = generate_weekly_report()
    print(report[:500])
    print("...")
    print(f"\n完整报告: {len(report)} 字符")

    if do_push:
        print("\n📡 推送中...")
        result = push_weekly_report(report)
        print(f"  Telegram: {'✅' if result['telegram'] else '❌'}")
        print(f"  微信: {'✅' if result['wechat'] else '❌'}")
        print(f"  文件: {result['file']}")


if __name__ == "__main__":
    main()
