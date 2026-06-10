#!/usr/bin/env python3
"""
回测参数优化报告 — 基于历史信号数据，测试不同阈值/权重的效果

用法:
    python3 backtest_report.py                    # 终端报告
    python3 backtest_report.py --json             # JSON 输出
    python3 backtest_report.py --save             # 保存到文件
"""
import sys, os, json
from datetime import date
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import get_conn
from config import SIGNAL_CONFIG


def load_signal_data() -> list[dict]:
    conn = get_conn()
    rows = conn.execute("""
        SELECT code, action, total_score, outcome_1d, outcome_3d, outcome_5d,
               date, price
        FROM signal_log
        WHERE outcome_1d IS NOT NULL
        ORDER BY date
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def classify_signal(score: float, t: dict) -> str:
    if score >= t["strong_buy_threshold"]:  return "STRONG_BUY"
    if score >= t["buy_threshold"]:         return "BUY"
    if score >= t["hold_high"]:             return "CAUTION_BUY"
    if score >= t["hold_low"]:              return "HOLD"
    if score >= t["sell_threshold"]:        return "WATCH"
    return "SELL"


def build_grid() -> list[dict]:
    grid = []
    for buy_t in [68, 70, 72]:
        for sb_t in [74, 76, 78]:
            if sb_t <= buy_t: continue
            for hh in [58, 60, 62]:
                if hh >= buy_t: continue
                for hl in [48, 50, 52]:
                    if hl >= hh: continue
                    for sl in [42, 45, 48]:
                        if sl >= hl: continue
                        grid.append({"strong_buy_threshold": sb_t, "buy_threshold": buy_t,
                                      "hold_high": hh, "hold_low": hl, "sell_threshold": sl,
                                      "label": f"SB{sb_t}_B{buy_t}_HH{hh}_HL{hl}_S{sl}"})
    return grid


def evaluate(records: list[dict], thresholds: dict) -> dict:
    stats = defaultdict(lambda: {"n": 0, "w1": 0, "r1": 0.0, "w3": 0, "r3": 0.0, "w5": 0, "r5": 0.0})
    for s in records:
        act = classify_signal(s["total_score"], thresholds)
        st = stats[act]
        st["n"] += 1
        o1 = s["outcome_1d"] or 0
        o3 = s["outcome_3d"] or 0
        o5 = s["outcome_5d"] or 0
        st["r1"] += o1;  st["r3"] += o3;  st["r5"] += o5
        if o1 > 0:  st["w1"] += 1
        if o3 > 0:  st["w3"] += 1
        if o5 > 0:  st["w5"] += 1

    def _as(a):
        st = stats.get(a, {"n": 0, "w1": 0, "r1": 0.0, "w3": 0, "r3": 0.0, "w5": 0, "r5": 0.0})
        t = max(st["n"], 1)
        return {"total": st["n"], "win_rate_1d": round(st["w1"]/t, 3),
                "avg_return_1d": round(st["r1"]/t, 2),  # 存储为百分比
                "win_rate_3d": round(st["w3"]/t, 3), "avg_return_3d": round(st["r3"]/t, 2)}

    actions = {a: _as(a) for a in ["STRONG_BUY", "BUY", "CAUTION_BUY", "HOLD", "SELL"]}
    buy_n = actions["STRONG_BUY"]["total"] + actions["BUY"]["total"]

    # 胜率直接用（0-1），收益率转成小数用于打分
    sb_wr = actions["STRONG_BUY"]["win_rate_1d"]
    b_wr  = actions["BUY"]["win_rate_1d"]
    c_wr  = actions["CAUTION_BUY"]["win_rate_1d"]
    s_wr  = actions["SELL"]["win_rate_1d"]
    sb_ret = actions["STRONG_BUY"]["avg_return_1d"] / 100.0
    b_ret  = actions["BUY"]["avg_return_1d"] / 100.0
    c_ret  = actions["CAUTION_BUY"]["avg_return_1d"] / 100.0
    s_ret  = actions["SELL"]["avg_return_1d"] / 100.0

    composite = (
        sb_wr * 30 + b_wr * 25 + c_wr * 15 + s_wr * 20 +
        max(-0.5, min(0.5, sb_ret)) * 200 +
        max(-0.5, min(0.5, b_ret)) * 150 +
        max(-0.3, min(0.3, c_ret)) * 100 +
        min(buy_n, 20) * 2
    )

    return {"thresholds": thresholds, "total": len(records),
            "buy_n": buy_n, "sell_n": actions["SELL"]["total"],
            "caution_n": actions["CAUTION_BUY"]["total"],
            "composite": round(composite, 1), "actions": actions}


def fmt_report(results: list[dict], top_n: int = 10) -> str:
    cur = SIGNAL_CONFIG
    current = evaluate(load_signal_data(), {
        "strong_buy_threshold": cur["strong_buy_threshold"],
        "buy_threshold": cur["buy_threshold"], "hold_high": cur["hold_high"],
        "hold_low": cur["hold_low"], "sell_threshold": cur["sell_threshold"],
    })
    best = results[0]

    lines = [
        f"{'':=^60}", "  📊 参数优化报告", f"{'':=^60}", "",
        f"  历史信号: {current['total']} 条含 outcome", f"  网格大小: {len(results)} 组", "",
        f"{'':=^60}", f"  阈值排名（Top {top_n}）", f"{'':=^60}", "",
        f"  {'排名':>3} {'SB':>3} {'B':>3} {'HH':>3} {'HL':>3} {'S':>3}  "
        f"{'买入':>4} {'卖出':>4} {'谨慎':>4} {'综合分':>7}  "
        f"SB胜率  B胜率  C胜率  S胜率",
        f"  {'':-^80}",
    ]
    for i, r in enumerate(results[:top_n], 1):
        t = r["thresholds"]
        a = r["actions"]
        lines.append(
            f"  {i:>3} {t['strong_buy_threshold']:>3} {t['buy_threshold']:>3} "
            f"{t['hold_high']:>3} {t['hold_low']:>3} {t['sell_threshold']:>3}  "
            f"{r['buy_n']:>4} {r['sell_n']:>4} {r['caution_n']:>4} {r['composite']:>7.1f}  "
            f"{a['STRONG_BUY']['win_rate_1d']*100:>5.1f}% "
            f"{a['BUY']['win_rate_1d']*100:>5.1f}% "
            f"{a['CAUTION_BUY']['win_rate_1d']*100:>5.1f}% "
            f"{a['SELL']['win_rate_1d']*100:>5.1f}%"
        )

    lines.extend(["", f"{'':=^60}", "  当前配置 vs Top 1", f"{'':=^60}", ""])
    for label, r in [("当前", current), ("Top1", best)]:
        t = r["thresholds"] if "thresholds" in r else cur
        lines.append(f"  {label}: SB{t['strong_buy_threshold']:.0f} B{t['buy_threshold']:.0f} "
                     f"HH{t['hold_high']:.0f} HL{t['hold_low']:.0f} S{t['sell_threshold']:.0f}  "
                     f"综合分{r['composite']:.1f}")
        for a in ["STRONG_BUY", "BUY", "CAUTION_BUY", "SELL"]:
            st = r["actions"].get(a, {})
            if st.get("total", 0) > 0:
                lines.append(f"    {a:<12} {st['total']:>3}条  1D{st['win_rate_1d']*100:>5.1f}% "
                             f"收益{st['avg_return_1d']:>+7.2f}%")

    lines.extend(["", f"{'':=^60}", f"  🏆 推荐配置", f"{'':=^60}"])
    bt = best["thresholds"]
    lines.append(f"  STRONG_BUY >= {bt['strong_buy_threshold']:.0f}分")
    lines.append(f"  BUY        >= {bt['buy_threshold']:.0f}分")
    lines.append(f"  CAUTION_BUY >= {bt['hold_high']:.0f}分")
    lines.append(f"  SELL       < {bt['sell_threshold']:.0f}分")
    lines.append(f"  综合分: {current['composite']:.1f} -> {best['composite']:.1f} ({best['composite']-current['composite']:+.1f})")
    return "\n".join(lines)


def main():
    save, do_json = "--save" in sys.argv, "--json" in sys.argv
    signals = load_signal_data()
    print(f"📦 加载 {len(signals)} 条信号\n🔬 扫描阈值组合...")
    grid = build_grid()
    results = sorted([evaluate(signals, g) for g in grid], key=lambda x: x["composite"], reverse=True)
    report = fmt_report(results)

    if do_json:
        print(json.dumps({"date": str(date.today()), "total_signals": len(signals),
                          "grid_size": len(grid), "top3": [{
                "thresholds": r["thresholds"], "composite": r["composite"], "actions": r["actions"]
            } for r in results[:3]],
            "current": evaluate(signals, {k: SIGNAL_CONFIG[k] for k in
                ["strong_buy_threshold","buy_threshold","hold_high","hold_low","sell_threshold"]})
        }, ensure_ascii=False, indent=2, default=str))
    else:
        print(report)

    if save:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "backtest_report.md")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(f"# 参数优化报告\n> {date.today()} | {len(signals)}条信号\n\n```\n{report}\n```\n")
        print(f"\n📝 {p}")


if __name__ == "__main__":
    main()
