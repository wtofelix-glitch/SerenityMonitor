#!/usr/bin/env python3
"""
Serenity 综合回测报告 — 绩效指标 · 策略对比 · 交互式 HTML
生成独立可交互的 HTML 报告（Plotly），包含净值曲线、回撤、月度收益热力图、交易明细、策略对比。

数据来源：backtest_engine.py

用法:
    python3 backtest_report.py [code]                # 单标的综合报告
    python3 backtest_report.py --compare             # 全策略对比报告
    python3 backtest_report.py --all                 # 所有标的 × 所有策略
"""
import sys
import os
import numpy as np
from datetime import datetime
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots
from plotly.colors import n_colors

from db import get_price_history, get_conn
from config import STOCK_MAP, ALL_CODES

# ── 颜色主题 ──
UP_COLOR = "#FF1744"
DOWN_COLOR = "#00C853"
GOLD_COLOR = "#FFD700"
BG_COLOR = "#1a1a2e"
CARD_BG = "rgba(255,255,255,0.05)"
TEXT_COLOR = "#e0e0e0"
MUTED = "rgba(255,255,255,0.5)"

REPORTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
os.makedirs(REPORTS_DIR, exist_ok=True)


# =============================================================
# 综合报告 — 单标的多策略
# =============================================================

def generate_report(code: str, strategies: list = None) -> Optional[str]:
    """
    生单标的综合回测报告

    Args:
        code: 股票代码
        strategies: 策略列表（默认使用全部5种）

    Returns:
        HTML 文件路径, 或 None
    """
    from backtest_engine import (
        TrendFollowingStrategy, MultiFactorStrategy, MeanReversionStrategy,
        HybridStrategy, MultiFactorWithSignalsStrategy,
        run_backtest, calc_performance_metrics,
    )

    if strategies is None:
        strategies = [
            TrendFollowingStrategy(),
            MultiFactorStrategy(),
            MeanReversionStrategy(),
            HybridStrategy(),
            MultiFactorWithSignalsStrategy(use_factors=True),
        ]

    name = STOCK_MAP.get(code, {}).get("name", code)

    # 运行全部策略
    results = []
    for strat in strategies:
        r = run_backtest(code, strat, initial_capital=50000)
        if "error" not in r:
            r = calc_performance_metrics(r)
            results.append(r)

    if not results:
        print(f"⚠️ {code} 无有效回测结果")
        return None

    # 选择最佳策略（按 Sharpe 排序）
    results.sort(key=lambda r: r.get("sharpe_ratio", 0), reverse=True)
    best = results[0]

    # ── 构建 HTML ──
    html_parts = [
        _head_html(f"回测报告 — {name}({code})"),
        _summary_section(code, name, results),
        _nav_curve_section(results),
        _metrics_table(results),
        _drawdown_section(best),
        _monthly_heatmap(best),
        _trade_table(best),
        _strategy_comparison_chart(results),
        _foot_html(),
    ]

    html = "\n".join(html_parts)

    date_str = datetime.now().strftime("%Y%m%d_%H%M")
    filename = f"backtest_{code}_{date_str}.html"
    path = os.path.join(REPORTS_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"✅ 报告生成: {path}")
    return path


def _head_html(title: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}}
body{{background:{BG_COLOR};color:{TEXT_COLOR};padding:16px}}
h1{{font-size:22px;font-weight:700;margin-bottom:16px;
    background:linear-gradient(90deg,{GOLD_COLOR},#FFA500);
    -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}}
h2{{font-size:16px;font-weight:600;color:{MUTED};margin:20px 0 12px}}
.card{{background:{CARD_BG};backdrop-filter:blur(10px);border:1px solid rgba(255,255,255,0.1);
       border-radius:14px;padding:16px;margin-bottom:16px}}
.grid{{display:flex;gap:12px;flex-wrap:wrap}}
.stat-card{{flex:1;min-width:130px;background:rgba(255,255,255,0.03);border-radius:10px;padding:12px;text-align:center}}
.stat-label{{font-size:11px;color:{MUTED};margin-bottom:4px}}
.stat-value{{font-size:20px;font-weight:700}}
.stat-sub{{font-size:10px;color:rgba(255,255,255,0.3);margin-top:2px}}
.up{{color:{UP_COLOR}}}
.down{{color:{DOWN_COLOR}}}
.neutral{{color:{GOLD_COLOR}}}
table{{width:100%;border-collapse:collapse;font-size:12px;margin-top:8px}}
th{{padding:8px;text-align:left;color:{MUTED};border-bottom:1px solid rgba(255,255,255,0.1);font-weight:600;font-size:11px}}
td{{padding:6px 8px;border-bottom:1px solid rgba(255,255,255,0.04)}}
tr:hover td{{background:rgba(255,255,255,0.03)}}
.month-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(70px,1fr));gap:6px;margin-top:8px}}
.month-cell{{padding:8px;text-align:center;border-radius:6px;font-size:12px;font-weight:600}}
.trade-buy{{color:{UP_COLOR}}}
.trade-sell{{color:{DOWN_COLOR}}}
.footer{{text-align:center;padding:20px;color:rgba(255,255,255,0.2);font-size:11px}}
</style>
</head>
<body>
<h1>{title}</h1>
"""


def _foot_html() -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"""
<div class="footer">Serenity Backtest Report · Generated {now}</div>
</body></html>"""


def _summary_section(code: str, name: str, results: list) -> str:
    best = results[0]
    worst = results[-1]

    total_return = best.get("total_return_pct", 0)
    sharpe = best.get("sharpe_ratio", 0)
    max_dd = best.get("max_drawdown_pct", 0)
    win_rate = best.get("win_rate_pct", 0)

    return f"""
<div class="grid">
  <div class="stat-card">
    <div class="stat-label">最佳策略</div>
    <div class="stat-value" style="font-size:16px;color:{GOLD_COLOR}">{best['strategy']}</div>
    <div class="stat-sub">Sharpe {sharpe} · 胜率 {win_rate}%</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">总收益</div>
    <div class="stat-value {'up' if total_return >= 0 else 'down'}">{total_return:+.1f}%</div>
    <div class="stat-sub">¥{best.get('initial_capital',0):,.0f} → ¥{best.get('final_value',0):,.0f}</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">夏普比率</div>
    <div class="stat-value {'up' if sharpe >= 1 else 'neutral' if sharpe >= 0 else 'down'}">{sharpe}</div>
    <div class="stat-sub">{'优秀 ≥1' if sharpe >= 1 else '正常 ≥0' if sharpe >= 0 else '需改进'}</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">最大回撤</div>
    <div class="stat-value down">-{max_dd:.1f}%</div>
    <div class="stat-sub">持续 {best.get('max_drawdown_duration_days',0)} 天</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">盈亏比</div>
    <div class="stat-value {'up' if best.get('profit_factor',0) >= 1.5 else 'neutral'}">{best.get('profit_factor',0):.2f}</div>
    <div class="stat-sub">盈利/亏损</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">交易数</div>
    <div class="stat-value">{best.get('trades',0)}</div>
    <div class="stat-sub">胜率 {best.get('win_rate_pct',0)}%</div>
  </div>
</div>

<h2>📋 策略排名 (按 Sharpe)</h2>
<table>
<thead><tr>
  <th>排名</th><th>策略</th><th>收益率</th><th>Sharpe</th><th>Sortino</th><th>Calmar</th>
  <th>最大回撤</th><th>胜率</th><th>盈亏比</th><th>交易数</th><th>持仓(天)</th>
</tr></thead>
<tbody>
""" + "\n".join(
        f"""<tr>
  <td>#{i+1}</td>
  <td style="font-weight:600">{r['strategy']}</td>
  <td class="{'up' if r['total_return_pct']>=0 else 'down'}">{r['total_return_pct']:+.1f}%</td>
  <td>{r.get('sharpe_ratio',0)}</td>
  <td>{r.get('sortino_ratio',0)}</td>
  <td>{r.get('calmar_ratio',0)}</td>
  <td class="down">{r.get('max_drawdown_pct',0):.1f}%</td>
  <td>{r.get('win_rate_pct',0)}%</td>
  <td>{r.get('profit_factor',0):.2f}</td>
  <td>{r.get('trades',0)}</td>
  <td>{r.get('avg_hold_days',0)}</td>
</tr>""" for i, r in enumerate(results)
    ) + """
</tbody></table>
"""


def _nav_curve_section(results: list) -> str:
    """净值曲线对比"""
    fig = go.Figure()

    strategy_colors = n_colors('rgb(255,23,68)', 'rgb(255,215,0)', len(results), colortype='rgb')

    for i, r in enumerate(results):
        curve = r.get("equity_curve", [])
        if not curve:
            continue
        dates = [c[0] for c in curve]
        vals = [c[1] for c in curve]
        fig.add_trace(go.Scatter(
            x=dates, y=vals, name=r["strategy"],
            line=dict(width=2, color=strategy_colors[i]),
            hovertemplate="%{x}<br>¥%{y:,.0f}<extra></extra>",
        ))

    fig.update_layout(
        title=f"{results[0]['name']}({results[0]['code']}) 策略净值曲线",
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        xaxis=dict(gridcolor="rgba(255,255,255,0.06)"),
        yaxis=dict(title="总资产 (¥)", gridcolor="rgba(255,255,255,0.06)"),
        margin=dict(l=10, r=20, t=40, b=10), height=400,
        font=dict(color=TEXT_COLOR), hovermode="x unified",
        legend=dict(orientation="h", y=1.1, font=dict(size=10)),
    )

    return f'<div class="card">{fig.to_html(full_html=False, include_plotlyjs="cdn")}</div>'


def _drawdown_section(result: dict) -> str:
    """回撤曲线"""
    curve = result.get("equity_curve", [])
    if not curve:
        return ""

    values = [v for _, v in curve]
    peak = values[0]
    drawdowns = []
    for v in values:
        if v > peak:
            peak = v
        dd = (v - peak) / peak * 100 if peak > 0 else 0
        drawdowns.append(round(dd, 2))

    dates = [c[0] for c in curve]

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=dates, y=drawdowns, fill="tozeroy",
        fillcolor="rgba(0,200,83,0.15)",
        line=dict(color=DOWN_COLOR, width=1.5),
        hovertemplate="%{x}<br>回撤: %{y:.1f}%<extra></extra>",
    ))

    fig.update_layout(
        title=f"回撤曲线 — {result['strategy']}（最大 -{result.get('max_drawdown_pct',0):.1f}%）",
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        xaxis=dict(gridcolor="rgba(255,255,255,0.06)"),
        yaxis=dict(title="回撤 %", gridcolor="rgba(255,255,255,0.06)",
                   range=[min(drawdowns) * 1.2, max(drawdowns) * 1.2 + 2] if drawdowns else None),
        margin=dict(l=10, r=20, t=40, b=10), height=250,
        font=dict(color=TEXT_COLOR), hovermode="x unified",
    )

    return f'<div class="card">{fig.to_html(full_html=False, include_plotlyjs="cdn")}</div>'


def _metrics_table(results: list) -> str:
    """绩效指标雷达图"""
    metrics = ["sharpe_ratio", "sortino_ratio", "profit_factor", "win_rate_pct", "total_return_pct"]
    labels = ["Sharpe", "Sortino", "盈亏比", "胜率%", "收益率%"]

    fig = go.Figure()
    for r in results:
        vals = []
        for m in metrics:
            v = r.get(m, 0)
            # Normalize sharpe-like metrics to 0-10 scale for radar
            if m in ("sharpe_ratio", "sortino_ratio"):
                v = max(0, min(10, (v + 3) / 3 * 5))
            elif m == "profit_factor":
                v = max(0, min(10, v * 2))
            elif m == "win_rate_pct":
                v = v / 10
            elif m == "total_return_pct":
                v = max(0, min(10, (v + 50) / 20))
            vals.append(round(v, 1))

        fig.add_trace(go.Scatterpolar(
            r=vals, theta=labels, fill="toself",
            name=r["strategy"],
        ))

    fig.update_layout(
        title="策略绩效雷达对比",
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=40, r=40, t=40, b=10), height=350,
        font=dict(color=TEXT_COLOR),
        legend=dict(orientation="h", y=1.1, font=dict(size=9)),
        polar=dict(
            bgcolor="rgba(0,0,0,0)",
            radialaxis=dict(visible=True, range=[0, 10],
                           gridcolor="rgba(255,255,255,0.08)"),
        ),
    )

    return f'<div class="card">{fig.to_html(full_html=False, include_plotlyjs="cdn")}</div>'


def _monthly_heatmap(result: dict) -> str:
    """月度收益热力图"""
    monthly = result.get("monthly_returns", {})
    if not monthly:
        return ""

    months = sorted(monthly.keys())
    vals = [monthly[m] for m in months]

    # Build color matrix (1 row)
    colors = []
    for v in vals:
        if v >= 5:
            colors.append(f"rgba(255,23,68,{min(1, v/15)})")
        elif v > 0:
            colors.append(f"rgba(255,23,68,0.3)")
        elif v == 0:
            colors.append("rgba(255,255,255,0.05)")
        elif v >= -3:
            colors.append(f"rgba(0,200,83,0.3)")
        else:
            colors.append(f"rgba(0,200,83,{min(1, abs(v)/15)})")

    cells = "".join(
        f'<div class="month-cell" style="background:{colors[i]}">{v:+.1f}%</div>'
        for i, v in enumerate(vals)
    )
    labels = "".join(f'<div style="font-size:9px;color:{MUTED};text-align:center">{m[5:]}</div>' for m in months)

    return f"""
<div class="card">
  <h2>📅 月度收益 — {result['strategy']}</h2>
  <div style="display:flex;gap:2px;flex-wrap:wrap;align-items:end">
    {cells}
  </div>
  <div style="display:flex;gap:2px;flex-wrap:wrap;margin-top:4px">
    {labels}
  </div>
  <div style="display:flex;gap:16px;margin-top:8px;font-size:10px;color:{MUTED}">
    <span>🔴 正收益 · 🔴 强盈利</span>
    <span>🟢 负收益 · 🟢 强亏损</span>
    <span>总月数: {len(months)}</span>
  </div>
</div>"""


def _trade_table(result: dict) -> str:
    """交易明细"""
    trades = result.get("trade_log", [])
    if not trades:
        return ""

    rows = ""
    for i, t in enumerate(trades):
        cls = "up" if t.profit_pct >= 0 else "down"
        icon = "🟢" if t.profit_pct >= 0 else "🔴"
        rows += f"""<tr>
  <td>#{i+1}</td>
  <td>{t.entry_date}</td>
  <td>{t.exit_date}</td>
  <td class="{cls}">{icon} {t.profit_pct:+.2f}%</td>
  <td>{t.hold_days}天</td>
  <td style="color:{MUTED};font-size:10px">{t.exit_reason[:50]}</td>
</tr>"""

    color_bar = f"""
<div style="display:flex;gap:8px;margin-top:8px;font-size:10px;color:{MUTED}">
  <span>🟢 盈利: {result.get('win_trades',0)}笔</span>
  <span>🔴 亏损: {result.get('lose_trades',0)}笔</span>
  <span>平均盈利: {result.get('avg_win_pct',0):+.1f}%</span>
  <span>平均亏损: {result.get('avg_loss_pct',0):+.1f}%</span>
</div>"""

    return f"""
<div class="card">
  <h2>📋 交易明细 — {result['strategy']}</h2>
  {color_bar}
  <table>
  <thead><tr>
    <th>#</th><th>买入</th><th>卖出</th><th>盈亏</th><th>持仓</th><th>原因</th>
  </tr></thead>
  <tbody>{rows}</tbody>
  </table>
</div>"""


def _strategy_comparison_chart(results: list) -> str:
    """策略对比柱状图"""
    names = [r["strategy"] for r in results]
    returns = [r.get("total_return_pct", 0) for r in results]
    sharpes = [r.get("sharpe_ratio", 0) for r in results]
    win_rates = [r.get("win_rate_pct", 0) for r in results]
    dd = [r.get("max_drawdown_pct", 0) for r in results]

    fig = make_subplots(rows=2, cols=2,
                        subplot_titles=("总收益率", "夏普比率", "胜率", "最大回撤"),
                        specs=[[{"type": "bar"}, {"type": "bar"}],
                               [{"type": "bar"}, {"type": "bar"}]],
                        vertical_spacing=0.12, horizontal_spacing=0.08)

    fig.add_trace(go.Bar(x=names, y=returns, name="收益率",
                          marker_color=[UP_COLOR if v >= 0 else DOWN_COLOR for v in returns],
                          text=[f"{v:+.1f}%" for v in returns],
                          textposition="outside"), row=1, col=1)
    fig.add_trace(go.Bar(x=names, y=sharpes, name="Sharpe",
                          marker_color=[UP_COLOR if v >= 0 else DOWN_COLOR for v in sharpes],
                          text=[f"{v:.2f}" for v in sharpes],
                          textposition="outside"), row=1, col=2)
    fig.add_trace(go.Bar(x=names, y=win_rates, name="胜率",
                          marker_color=GOLD_COLOR,
                          text=[f"{v:.0f}%" for v in win_rates],
                          textposition="outside"), row=2, col=1)
    fig.add_trace(go.Bar(x=names, y=[-v for v in dd], name="最大回撤",
                          marker_color=DOWN_COLOR,
                          text=[f"-{v:.1f}%" for v in dd],
                          textposition="outside"), row=2, col=2)

    fig.update_layout(
        title="策略对比：四维度",
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=10, r=20, t=50, b=80), height=500,
        font=dict(color=TEXT_COLOR, size=10),
        showlegend=False,
    )
    fig.update_xaxes(gridcolor="rgba(255,255,255,0.06)")
    fig.update_yaxes(gridcolor="rgba(255,255,255,0.06)")

    return f'<div class="card">{fig.to_html(full_html=False, include_plotlyjs="cdn")}</div>'


# =============================================================
# 全策略对比报告
# =============================================================

def generate_comparison_report(codes: list = None) -> Optional[str]:
    """生成多标的多策略对比报告"""
    from backtest_engine import (
        TrendFollowingStrategy, MultiFactorStrategy, MeanReversionStrategy,
        HybridStrategy, MultiFactorWithSignalsStrategy,
        run_backtest, calc_performance_metrics,
    )

    if codes is None:
        codes = ALL_CODES[:6]  # first 6 stocks

    strategies = [
        TrendFollowingStrategy(),
        MultiFactorStrategy(),
        MeanReversionStrategy(),
        HybridStrategy(),
        MultiFactorWithSignalsStrategy(use_factors=True),
    ]

    # Run all combos
    all_results = []
    for code in codes:
        name = STOCK_MAP.get(code, {}).get("name", code)
        for strat in strategies:
            r = run_backtest(code, strat)
            if "error" not in r:
                r = calc_performance_metrics(r)
                all_results.append(r)

    if not all_results:
        return None

    html_parts = [
        _head_html("全策略对比报告"),
        _comparison_overview(all_results),
        _comparison_heatmap(all_results),
        _comparison_strategy_ranking(all_results),
        _foot_html(),
    ]

    html = "\n".join(html_parts)
    date_str = datetime.now().strftime("%Y%m%d_%H%M")
    path = os.path.join(REPORTS_DIR, f"backtest_comparison_{date_str}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"✅ 对比报告生成: {path}")
    return path


def _comparison_overview(results: list) -> str:
    """对比概览统计"""
    # Per strategy aggregate
    from collections import defaultdict
    by_strat = defaultdict(list)
    for r in results:
        by_strat[r["strategy"]].append(r)

    rows = ""
    for sname, sresults in sorted(by_strat.items()):
        avg_return = np.mean([r.get("total_return_pct", 0) for r in sresults])
        avg_sharpe = np.mean([r.get("sharpe_ratio", 0) for r in sresults])
        avg_dd = np.mean([r.get("max_drawdown_pct", 0) for r in sresults])
        avg_win = np.mean([r.get("win_rate_pct", 0) for r in sresults])
        wins = sum(1 for r in sresults if r.get("total_return_pct", 0) > 0)
        total = len(sresults)
        rows += f"""<tr>
  <td style="font-weight:600">{sname}</td>
  <td class="{'up' if avg_return >= 0 else 'down'}">{avg_return:+.1f}%</td>
  <td>{avg_sharpe:.2f}</td>
  <td class="down">{avg_dd:.1f}%</td>
  <td>{avg_win:.0f}%</td>
  <td>{wins}/{total}</td>
  <td>{total}</td>
</tr>"""

    return f"""
<div class="card">
  <h2>📊 策略综合表现（{len(results)} 个回测组合）</h2>
  <table>
  <thead><tr>
    <th>策略</th><th>平均收益</th><th>平均 Sharpe</th><th>平均回撤</th>
    <th>平均胜率</th><th>盈利/总数</th><th>标的数</th>
  </tr></thead>
  <tbody>{rows}</tbody>
  </table>
</div>"""


def _comparison_heatmap(results: list) -> str:
    """策略×标的 收益率热力图"""
    from collections import defaultdict
    strat_order = ["TrendFollowingStrategy", "MultiFactorStrategy",
                   "MeanReversionStrategy", "HybridStrategy",
                   "MultiFactorWithSignalsStrategy"]

    code_order = []
    perfs = defaultdict(dict)
    for r in results:
        code_order.append(r["code"])
        perfs[r["code"]][r["strategy"]] = r.get("total_return_pct", 0)

    code_order = list(dict.fromkeys(code_order))  # unique, preserve order

    # Filter to strategies that exist
    active_strats = [s for s in strat_order if any(s in perfs[c] for c in code_order)]

    z = []
    y_labels = code_order
    x_labels = active_strats
    for code in code_order:
        row = [perfs[code].get(s, 0) for s in active_strats]
        z.append(row)

    fig = go.Figure(data=go.Heatmap(
        z=z, x=x_labels, y=[STOCK_MAP.get(c, {}).get("name", c) for c in y_labels],
        text=[[f"{v:+.1f}%" for v in row] for row in z],
        texttemplate="%{text}",
        textfont=dict(size=9, color="white"),
        colorscale=[[0, DOWN_COLOR], [0.25, "rgb(0,80,40)"],
                    [0.45, "rgb(30,30,30)"], [0.55, "rgb(60,30,30)"],
                    [1, UP_COLOR]],
        zmid=0,
        hovertemplate="%{y} | %{x}<br>%{text}<extra></extra>",
    ))

    fig.update_layout(
        title="策略 × 标的 收益率矩阵",
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=10, r=20, t=40, b=60), height=400,
        font=dict(color=TEXT_COLOR, size=11),
        xaxis=dict(tickangle=30),
    )

    return f'<div class="card">{fig.to_html(full_html=False, include_plotlyjs="cdn")}</div>'


def _comparison_strategy_ranking(results: list) -> str:
    """各策略在所有标的上的得分排名"""
    from collections import defaultdict
    strat_scores = defaultdict(list)
    for r in results:
        sharpe = r.get("sharpe_ratio", 0)
        score = sharpe * 0.3 + r.get("total_return_pct", 0) / 20 * 0.3 + \
                (100 - r.get("max_drawdown_pct", 0)) / 100 * 0.2 + r.get("win_rate_pct", 0) / 100 * 0.2
        strat_scores[r["strategy"]].append(round(score, 2))

    sorted_strats = sorted(strat_scores.items(), key=lambda x: np.mean(x[1]), reverse=True)

    # Bar chart
    strat_names = [s[0] for s in sorted_strats]
    avg_scores = [np.mean(s[1]) for s in sorted_strats]
    std_scores = [np.std(s[1]) for s in sorted_strats]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=strat_names, y=avg_scores,
        error_y=dict(type="data", array=std_scores, visible=True, color="rgba(255,255,255,0.3)"),
        marker_color=[UP_COLOR if s >= 0 else DOWN_COLOR for s in avg_scores],
        text=[f"{s:.2f}" for s in avg_scores],
        textposition="outside",
        hovertemplate="%{x}<br>综合得分: %{y:.2f} ± %{customdata:.2f}<extra></extra>",
        customdata=std_scores,
    ))

    fig.update_layout(
        title="策略综合排名（Sharpe×0.3 + 收益×0.3 + 回撤×0.2 + 胜率×0.2）",
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        xaxis=dict(gridcolor="rgba(255,255,255,0.06)"),
        yaxis=dict(title="综合得分", gridcolor="rgba(255,255,255,0.06)", range=[0, max(avg_scores) * 1.3]),
        margin=dict(l=10, r=20, t=40, b=80), height=350,
        font=dict(color=TEXT_COLOR, size=11),
    )

    # Detail table
    rows = ""
    for i, (sname, scores) in enumerate(sorted_strats):
        rows += f"""<tr>
  <td>#{i+1}</td>
  <td style="font-weight:600">{sname}</td>
  <td>{np.mean(scores):.2f}</td>
  <td>{np.std(scores):.2f}</td>
  <td>{', '.join(f'{s:+.1f}' for s in scores[:5])}{'...' if len(scores) > 5 else ''}</td>
</tr>"""

    return f"""
<div class="card">{fig.to_html(full_html=False, include_plotlyjs="cdn")}</div>
<div class="card">
  <h2>🏆 策略综合排名</h2>
  <table>
  <thead><tr>
    <th>排名</th><th>策略</th><th>均分</th><th>标准差</th><th>各标的得分</th>
  </tr></thead>
  <tbody>{rows}</tbody>
  </table>
</div>"""


# =============================================================
# CLI 入口
# =============================================================

if __name__ == "__main__":
    if "--compare" in sys.argv:
        generate_comparison_report()
    elif "--all" in sys.argv:
        for code in ALL_CODES:
            try:
                generate_report(code)
            except Exception as e:
                print(f"⚠️ {code} 报告生成失败: {e}")
    else:
        code = sys.argv[1] if len(sys.argv) > 1 else "002281"
        generate_report(code)
