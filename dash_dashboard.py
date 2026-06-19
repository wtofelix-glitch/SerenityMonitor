#!/usr/bin/env python3
"""
Serenity Dash 交互看板 — IC 归因 · 权重对比 · 执行历史 · 风控状态
Plotly Dash 应用，端口 8050，与 Flask 看板互补。

用法:
    python3 dash_dashboard.py
    访问 http://localhost:8050

数据来源:
    - factor_ic.compute_rank_ic() → IC 趋势
    - weight_adjuster.load_adjusted_weights() → 当前权重
    - DB: nav_history, trades, execution_log, signal_performance
    - risk_manager.get_risk_report() → 风控状态
"""
import sys
import os
import json
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dash
from dash import dcc, html, Input, Output, callback
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots
import numpy as np

from serenity_logger import get_logger
log = get_logger(__name__)

# ── 数据源 ────────────────────────────────────────────────────
from db import get_conn
from config import STOCK_MAP
from factor_ic import compute_rank_ic, DIMENSION_LABELS
from weight_adjuster import load_adjusted_weights, DEFAULT_WEIGHTS

# ── Dash 应用 ─────────────────────────────────────────────────
app = dash.Dash(__name__, title="Serenity Dash")
app.config.suppress_callback_exceptions = True

# ── 全局 CSS ────────────────────────────────────────────────
app.index_string = '''
<!DOCTYPE html>
<html>
<head>
{%metas%}
<title>{%title%}</title>
{%favicon%}
{%css%}
<style>
  body { -webkit-font-smoothing: antialiased; -moz-osx-font-smoothing: grayscale; }
  ::-webkit-scrollbar { width: 6px; height: 6px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.10); border-radius: 3px; }
  ::-webkit-scrollbar-thumb:hover { background: rgba(255,255,255,0.18); }
</style>
</head>
<body>
{%app_entry%}
<footer>
{%config%}
{%scripts%}
{%renderer%}
</footer>
</body>
</html>
'''

# ── Bloomberg/TradingView 风格设计系统 ──────────────────────
THEME = {
    "bg": "#0E1118",
    "card": "#1E2230",
    "card_border": "rgba(255,255,255,0.08)",
    "card_border_hover": "rgba(255,255,255,0.12)",
    "text": "#E8EAED",
    "text_sec": "rgba(232,234,237,0.6)",
    "text_ter": "rgba(232,234,237,0.35)",
    "gold": "#FFD700",
    "up": "#FF1744",
    "down": "#00C853",
    "neutral": "#BDBDBD",
    "bg_card_alt": "rgba(255,255,255,0.03)",
    "chart_grid": "rgba(255,255,255,0.04)",
    "chart_tick": "rgba(255,255,255,0.25)",
    "font_family": '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
    "font_mono": '"SF Mono", "Fira Code", Consolas, monospace',
}

NAV_STYLE = {
    "backgroundColor": "rgba(14,17,24,0.96)",
    "padding": "8px 16px",
    "borderBottom": "1px solid rgba(255,255,255,0.08)",
    "backdropFilter": "blur(12px)",
    "WebkitBackdropFilter": "blur(12px)",
}
TAB_STYLE = {
    "backgroundColor": "transparent",
    "color": "rgba(232,234,237,0.45)",
    "padding": "10px 20px",
    "border": "none",
    "borderBottom": "2px solid transparent",
    "fontWeight": "500",
    "fontFamily": '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
    "fontSize": "12px",
    "textTransform": "none",
    "letterSpacing": "0.3px",
}
TAB_SELECTED_STYLE = {
    "backgroundColor": "transparent",
    "color": "#FFD700",
    "padding": "10px 20px",
    "border": "none",
    "borderBottom": "2px solid #FFD700",
    "fontWeight": "600",
    "fontFamily": '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
    "fontSize": "12px",
    "letterSpacing": "0.3px",
}

# ── 图表通用布局配置 ──────────────────────────────────────
CHART_LAYOUT = {
    "paper_bgcolor": "rgba(0,0,0,0)",
    "plot_bgcolor": "rgba(0,0,0,0)",
    "font": {"color": THEME["text"], "family": THEME["font_family"]},
    "margin": {"l": 10, "r": 20, "t": 40, "b": 10},
    "hovermode": "x unified",
    "hoverlabel": {
        "bgcolor": THEME["card"],
        "bordercolor": THEME["card_border"],
        "font": {"color": THEME["text"], "size": 11, "family": THEME["font_mono"]},
    },
    "xaxis": {
        "gridcolor": THEME["chart_grid"],
        "zerolinecolor": "rgba(255,255,255,0.06)",
        "tickfont": {"color": THEME["chart_tick"], "size": 9, "family": THEME["font_mono"]},
        "showline": False,
        "showspikes": True,
        "spikethickness": 1,
        "spikedash": "dot",
        "spikecolor": "rgba(255,255,255,0.1)",
    },
    "yaxis": {
        "gridcolor": THEME["chart_grid"],
        "zerolinecolor": "rgba(255,255,255,0.06)",
        "tickfont": {"color": THEME["chart_tick"], "size": 9, "family": THEME["font_mono"]},
        "showline": False,
    },
    "legend": {
        "orientation": "h",
        "y": 1.12,
        "font": {"size": 10, "color": THEME["text_sec"]},
        "bgcolor": "rgba(0,0,0,0)",
    },
    "margin": {"l": 10, "r": 20, "t": 40, "b": 10},
}


# =============================================================
# 数据获取函数
# =============================================================

def fetch_factor_ic_data():
    """IC 归因数据"""
    try:
        result = compute_rank_ic(days=30, window=20)
        if "error" in result:
            return None, None, None, result["error"]

        ic_summary = []
        for dim in list(result.get("latest", {}).keys()):
            ic_summary.append({
                "dimension": dim,
                "label": DIMENSION_LABELS.get(dim, dim),
                "latest": result["latest"].get(dim, 0),
                "mean": result["mean_ic"].get(dim, 0),
                "ic_ir": result["ic_ir"].get(dim, 0),
                "win_rate": result["win_rate"].get(dim, 0),
                "n_days": result["n_days"].get(dim, 0),
            })
        ic_summary.sort(key=lambda x: abs(x["latest"]), reverse=True)

        # Trend data: time series of IC for each dimension
        all_ics = result.get("all_ics", {})
        ic_trend = {}
        for dim, values in all_ics.items():
            if values:
                ic_trend[dim] = values[-20:]  # last 20 observations

        return ic_summary, ic_trend, result, None
    except Exception as e:
        return None, None, None, str(e)


def fetch_weight_data():
    """权重数据 — 默认 vs 调整后"""
    adjusted = load_adjusted_weights()
    default = dict(DEFAULT_WEIGHTS)

    # Build comparison
    all_keys = sorted(set(list(default.keys()) + list(adjusted.keys())))
    comparison = []
    for k in all_keys:
        d_val = default.get(k, 0)
        a_val = adjusted.get(k, 0)
        comparison.append({
            "dimension": k,
            "default": d_val,
            "adjusted": a_val,
            "delta": a_val - d_val,
            "delta_pct": ((a_val - d_val) / d_val * 100) if d_val > 0 else 0,
        })

    return comparison, default, adjusted


def fetch_nav_history(days=60):
    """净值历史"""
    conn = get_conn()
    rows = conn.execute("""
        SELECT date, total_value, profit_pct
        FROM nav_history
        WHERE date >= date('now', ?)
        ORDER BY date ASC
    """, (f"-{days} days",)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def fetch_trade_history(days=30):
    """交易历史"""
    conn = get_conn()
    rows = conn.execute("""
        SELECT date, code, action, price, quantity, note, created_at, trade_amount
        FROM trades
        WHERE date >= date('now', ?)
        ORDER BY date ASC
    """, (f"-{days} days",)).fetchall()
    conn.close()
    trades = [dict(r) for r in rows]
    for t in trades:
        t["name"] = STOCK_MAP.get(t["code"], {}).get("name", t["code"])
    return trades


def fetch_signal_performance():
    """信号绩效汇总"""
    conn = get_conn()
    rows = conn.execute("""
        SELECT code, action, total_signals, wins_1d, wins_3d,
               avg_return_1d, avg_return_3d
        FROM signal_performance
        ORDER BY total_signals DESC
        LIMIT 20
    """).fetchall()
    conn.close()
    result = [dict(r) for r in rows]
    for r in result:
        r["name"] = STOCK_MAP.get(r["code"], {}).get("name", r["code"])
        r["win_rate_1d"] = round(r["wins_1d"] / max(r["total_signals"], 1) * 100, 1)
        r["win_rate_3d"] = round(r["wins_3d"] / max(r["total_signals"], 1) * 100, 1)
    return result


def fetch_risk_report():
    """风控状态报告"""
    try:
        from risk_manager import get_risk_manager
        rm = get_risk_manager()
        report = rm.get_risk_report()
        text = rm.format_risk_report()
        return report, text, None
    except Exception as e:
        return None, None, str(e)


def fetch_execution_log(days=7):
    """自动执行日志"""
    conn = get_conn()
    rows = conn.execute("""
        SELECT date, code, action, status, price, shares, amount,
               reason, attempt, error_msg, created_at
        FROM execution_log
        WHERE date >= date('now', ?)
        ORDER BY created_at DESC
        LIMIT 50
    """, (f"-{days} days",)).fetchall()
    conn.close()
    result = [dict(r) for r in rows]
    for r in result:
        r["name"] = STOCK_MAP.get(r["code"], {}).get("name", r["code"])
    return result


def fetch_latest_scores():
    """最近评分快照"""
    conn = get_conn()
    rows = conn.execute("""
        SELECT code, total_score, base_score, zone_score,
               momentum_score, volume_score, technical_score,
               sentiment_score, date
        FROM scoring_history
        WHERE date = (SELECT MAX(date) FROM scoring_history)
        ORDER BY total_score DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# =============================================================
# 布局
# =============================================================

app.layout = html.Div(style={
    "backgroundColor": THEME["bg"],
    "color": THEME["text"],
    "fontFamily": THEME["font_family"],
    "minHeight": "100vh",
    "padding": "0",
    "WebkitFontSmoothing": "antialiased",
}, children=[

    # ── 顶部导航 ──
    html.Div(style=NAV_STYLE, children=[
        html.Div(style={"display": "flex", "alignItems": "center", "justifyContent": "space-between",
                         "maxWidth": "1200px", "margin": "0 auto"}, children=[
            html.H1([
                html.Span("S", style={
                    "display": "inline-flex", "width": "22px", "height": "22px",
                    "background": "linear-gradient(135deg,#FFD700,#FFA500)",
                    "borderRadius": "4px", "alignItems": "center", "justifyContent": "center",
                    "fontSize": "12px", "fontWeight": "700", "color": "#0E1118",
                    "marginRight": "8px",
                }),
                html.Span("Serenity Dash", style={
                    "fontSize": "18px", "fontWeight": "700",
                    "background": "linear-gradient(90deg,#FFD700,#FFA500)",
                    "-webkitBackgroundClip": "text",
                    "-webkitTextFillColor": "transparent",
                    "backgroundClip": "text",
                }),
            ], style={"display": "flex", "alignItems": "center", "margin": "0"}),
            html.Div(id="last-update", style={
                "fontSize": "11px", "color": "rgba(232,234,237,0.35)",
                "fontFamily": THEME["font_mono"],
            }),
        ]),
    ]),

    # ── Tab 导航 ──
    dcc.Tabs(id="main-tabs", value="tab-ic", style={
        "backgroundColor": THEME["bg"],
        "maxWidth": "1200px", "margin": "0 auto",
        "borderBottom": "1px solid rgba(255,255,255,0.08)",
    }, children=[

        dcc.Tab(label="📊 IC 归因", value="tab-ic", style=TAB_STYLE, selected_style=TAB_SELECTED_STYLE),
        dcc.Tab(label="⚖️ 权重对比", value="tab-weight", style=TAB_STYLE, selected_style=TAB_SELECTED_STYLE),
        dcc.Tab(label="📈 执行历史", value="tab-exec", style=TAB_STYLE, selected_style=TAB_SELECTED_STYLE),
        dcc.Tab(label="🛡️ 风控状态", value="tab-risk", style=TAB_STYLE, selected_style=TAB_SELECTED_STYLE),

    ]),

    # ── Tab 内容 ──
    html.Div(id="tab-content", style={
        "maxWidth": "1200px", "margin": "16px auto", "padding": "0 12px",
    }),

    # ── 刷新定时器（30秒）──
    dcc.Interval(id="refresh-timer", interval=30_000, n_intervals=0),

])


# =============================================================
# Tab 内容渲染
# =============================================================

@callback(Output("tab-content", "children"), Input("main-tabs", "value"),
          Input("refresh-timer", "n_intervals"))
def render_tab(tab, _n):
    if tab == "tab-ic":
        return _render_ic_tab()
    elif tab == "tab-weight":
        return _render_weight_tab()
    elif tab == "tab-exec":
        return _render_exec_tab()
    elif tab == "tab-risk":
        return _render_risk_tab()
    return html.Div()


# =============================================================
# IC 归因 Tab
# =============================================================

def _render_ic_tab():
    ic_summary, ic_trend, raw, err = fetch_factor_ic_data()

    if err:
        return _error_card(f"⚠️ IC 数据获取失败: {err}")

    if not ic_summary:
        return _error_card("暂无 IC 数据，请先运行评分系统")

    # ── IC 柱状图（当前值） ──
    dims = [s["label"] for s in ic_summary]
    latest_ics = [s["latest"] for s in ic_summary]
    colors = [THEME["up"] if v >= 0 else THEME["down"] for v in latest_ics]

    fig_ic_bar = go.Figure()
    fig_ic_bar.add_trace(go.Bar(
        x=latest_ics, y=dims, orientation="h",
        marker_color=colors,
        text=[f"{v:+.3f}" for v in latest_ics],
        textposition="outside",
        hovertemplate="%{y}: %{x:+.3f}<extra></extra>",
    ))
    fig_ic_bar.update_layout(
        title=dict(text="📊 各维度当前 Rank IC", font=dict(color=THEME["text"], size=13)),
        **{k: v for k, v in CHART_LAYOUT.items() if k not in ("xaxis", "yaxis", "hovermode")},
        xaxis=dict(title="Rank IC", **CHART_LAYOUT["xaxis"]),
        yaxis=dict(**CHART_LAYOUT["yaxis"]),
        height=350,
        hovermode="y",
    )
    fig_ic_bar.add_hline(y=0, line_dash="dot", line_color="rgba(255,255,255,0.15)")

    # ── IC 趋势折线图 ──
    fig_trend = go.Figure()
    if ic_trend:
        # Only show dimensions with enough data
        trend_dims = sorted(ic_trend.keys(),
                           key=lambda d: abs(ic_trend[d][-1]) if ic_trend[d] else 0,
                           reverse=True)[:5]
        for dim in trend_dims:
            values = ic_trend[dim]
            label = DIMENSION_LABELS.get(dim, dim)
            fig_trend.add_trace(go.Scatter(
                y=values, name=label, mode="lines+markers",
                line=dict(width=2),
                marker=dict(size=4),
                hovertemplate=f"{label}: %{{y:+.3f}}<extra></extra>",
            ))

    fig_trend.update_layout(
        title=dict(text="📈 IC 趋势（近20期）", font=dict(color=THEME["text"], size=13)),
        **CHART_LAYOUT,
        height=350,
    )

    # ── IC-IR 指标表 ──
    ic_table_rows = []
    for s in ic_summary:
        ic_table_rows.append(html.Tr([
            html.Td(s["label"], style={"fontWeight": "600", "padding": "4px 8px"}),
            html.Td(f"{s['latest']:+.3f}", style={
                "color": THEME["up"] if s["latest"] >= 0 else THEME["down"],
                "fontWeight": "600", "padding": "4px 8px",
            }),
            html.Td(f"{s['mean']:+.3f}", style={"padding": "4px 8px"}),
            html.Td(f"{s['ic_ir']:.2f}", style={
                "color": THEME["up"] if s["ic_ir"] >= 0.5 else
                         (THEME["down"] if s["ic_ir"] <= -0.5 else THEME["neutral"]),
                "padding": "4px 8px",
            }),
            html.Td(f"{s['win_rate']:.0f}%", style={"padding": "4px 8px"}),
            html.Td(f"{s['n_days']}天", style={"padding": "4px 8px"}),
        ]))

    ic_table = html.Table([
        html.Thead(html.Tr([
            html.Th("维度", style={"padding": "6px 8px", "textAlign": "left", "color": "rgba(255,255,255,0.5)"}),
            html.Th("最新 IC", style={"padding": "6px 8px", "textAlign": "right", "color": "rgba(255,255,255,0.5)"}),
            html.Th("均值 IC", style={"padding": "6px 8px", "textAlign": "right", "color": "rgba(255,255,255,0.5)"}),
            html.Th("IC-IR", style={"padding": "6px 8px", "textAlign": "right", "color": "rgba(255,255,255,0.5)"}),
            html.Th("胜率", style={"padding": "6px 8px", "textAlign": "right", "color": "rgba(255,255,255,0.5)"}),
            html.Th("天数", style={"padding": "6px 8px", "textAlign": "right", "color": "rgba(255,255,255,0.5)"}),
        ])),
        html.Tbody(ic_table_rows),
    ], style={"width": "100%", "fontSize": "12px", "borderCollapse": "collapse"})

    # Top/Worst factors cards
    top_dims = [s for s in ic_summary if s["latest"] > 0][:3]
    worst_dims = [s for s in ic_summary if s["latest"] < 0][:3]

    top_cards = []
    for s in top_dims:
        top_cards.append(html.Div([
            html.Div(s["label"], style={"fontSize": "11px", "color": "rgba(255,255,255,0.5)"}),
            html.Div(f"{s['latest']:+.3f}", style={"fontSize": "18px", "fontWeight": "700",
                     "color": THEME["up"]}),
            html.Div(f"IC-IR: {s['ic_ir']:.2f}", style={"fontSize": "10px", "color": "rgba(255,255,255,0.3)"}),
        ], style={"flex": "1", "background": "rgba(255,23,68,0.08)",
                  "borderRadius": "10px", "padding": "10px", "textAlign": "center"}))

    worst_cards = []
    for s in worst_dims:
        worst_cards.append(html.Div([
            html.Div(s["label"], style={"fontSize": "11px", "color": "rgba(255,255,255,0.5)"}),
            html.Div(f"{s['latest']:+.3f}", style={"fontSize": "18px", "fontWeight": "700",
                     "color": THEME["down"]}),
            html.Div(f"IC-IR: {s['ic_ir']:.2f}", style={"fontSize": "10px", "color": "rgba(255,255,255,0.3)"}),
        ], style={"flex": "1", "background": "rgba(0,200,83,0.08)",
                  "borderRadius": "10px", "padding": "10px", "textAlign": "center"}))

    return html.Div([
        # Top / Worst 概览
        html.Div(style={"display": "flex", "gap": "12px", "marginBottom": "16px"}, children=[
            html.Div(style={"flex": "1"}, children=[
                html.Div("🏆 最有效因子", style={"fontSize": "12px", "fontWeight": "600",
                         "color": THEME["up"], "marginBottom": "8px"}),
                html.Div(style={"display": "flex", "gap": "8px"}, children=top_cards or [
                    html.Div("暂无", style={"color": "rgba(255,255,255,0.3)", "padding": "8px"})
                ]),
            ]),
            html.Div(style={"flex": "1"}, children=[
                html.Div("⚠️ 最无效因子", style={"fontSize": "12px", "fontWeight": "600",
                         "color": THEME["down"], "marginBottom": "8px"}),
                html.Div(style={"display": "flex", "gap": "8px"}, children=worst_cards or [
                    html.Div("暂无", style={"color": "rgba(255,255,255,0.3)", "padding": "8px"})
                ]),
            ]),
        ]),

        # IC 柱状图 + 趋势图
        html.Div(style={"display": "flex", "gap": "12px", "flexWrap": "wrap"}, children=[
            html.Div(style={"flex": "1", "minWidth": "320px", "background": THEME["card"],
                            "borderRadius": "12px", "padding": "12px",
                            "border": "1px solid rgba(255,255,255,0.08)"}, children=[
                dcc.Graph(figure=fig_ic_bar, config={"displayModeBar": False}),
            ]),
            html.Div(style={"flex": "1", "minWidth": "320px", "background": THEME["card"],
                            "borderRadius": "12px", "padding": "12px",
                            "border": "1px solid rgba(255,255,255,0.08)"}, children=[
                dcc.Graph(figure=fig_trend, config={"displayModeBar": False}),
            ]),
        ]),

        # IC 指标表
        html.Div(style={"background": THEME["card"], "borderRadius": "12px", "padding": "12px",
                        "border": "1px solid rgba(255,255,255,0.08)", "background": THEME["card"], "marginTop": "12px"}, children=[
            html.Div("📋 因子 IC 明细", style={"fontSize": "13px", "fontWeight": "600",
                     "color": THEME["text_sec"], "marginBottom": "8px"}),
            ic_table,
        ]),
    ])


# =============================================================
# 权重对比 Tab
# =============================================================

def _render_weight_tab():
    comparison, default, adjusted = fetch_weight_data()

    if not comparison:
        return _error_card("无法获取权重数据")

    # ── 默认 vs 调整后 对比柱状图 ──
    dims = [c["dimension"] for c in comparison]
    default_vals = [c["default"] for c in comparison]
    adjusted_vals = [c["adjusted"] for c in comparison]
    deltas = [c["delta"] for c in comparison]

    # Labels for Chinese dimension names
    dim_labels_cn = {
        "base": "基本面", "zone": "价格位置", "momentum": "动量",
        "volume": "成交量", "serenity": "Serenity", "factor": "因子引擎",
        "technical": "技术面", "sentiment": "情绪",
    }
    dim_labels = [dim_labels_cn.get(d, d) for d in dims]

    fig_weight = go.Figure()
    fig_weight.add_trace(go.Bar(
        name="默认权重",
        x=dim_labels, y=default_vals,
        marker_color="rgba(255,255,255,0.3)",
        text=[f"{v:.1%}" for v in default_vals],
        textposition="outside",
        textfont=dict(size=9),
    ))
    fig_weight.add_trace(go.Bar(
        name="调整后权重",
        x=dim_labels, y=adjusted_vals,
        marker_color="rgba(255,215,0,0.7)",
        text=[f"{v:.1%}" for v in adjusted_vals],
        textposition="outside",
        textfont=dict(size=9),
    ))

    fig_weight.update_layout(
        title=dict(text="⚖️ 权重对比：默认 vs IC 调整", font=dict(color=THEME["text"], size=13)),
        xaxis=dict(title="评分维度", **CHART_LAYOUT["xaxis"]),
        yaxis=dict(title="权重", **CHART_LAYOUT["yaxis"],
                   tickformat=".0%", range=[0, max(default_vals + adjusted_vals) * 1.4]),
        barmode="group",
        legend=CHART_LAYOUT["legend"],
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        height=400,
        margin=dict(l=10, r=20, t=40, b=60),
        font=dict(color=THEME["text"], family=THEME["font_family"]),
    )

    # ── 调整增量柱状图 ──
    delta_colors = [THEME["up"] if d >= 0 else THEME["down"] for d in deltas]
    fig_delta = go.Figure()
    fig_delta.add_trace(go.Bar(
        x=dim_labels, y=deltas,
        marker_color=delta_colors,
        text=[f"{d:+.1%}" for d in deltas],
        textposition="outside",
        textfont=dict(size=10),
        hovertemplate="%{x}: %{y:+.1%}<extra></extra>",
    ))

    fig_delta.update_layout(
        title=dict(text="📊 权重调整幅度", font=dict(color=THEME["text"], size=13)),
        xaxis=dict(**CHART_LAYOUT["xaxis"]),
        yaxis=dict(title="变动", **CHART_LAYOUT["yaxis"], tickformat="+.0%"),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        height=300,
        margin=dict(l=10, r=20, t=40, b=60),
        font=dict(color=THEME["text"], family=THEME["font_family"]),
    )
    fig_delta.add_hline(y=0, line_dash="dot", line_color="rgba(255,255,255,0.15)")

    # ── 详情表 ──
    detail_rows = []
    label_map_cn = {
        "base": "基本面", "zone": "价格位置", "momentum": "动量",
        "volume": "成交量", "serenity": "Serenity", "factor": "因子引擎",
        "technical": "技术面", "sentiment": "情绪",
    }
    for c in comparison:
        lbl = label_map_cn.get(c["dimension"], c["dimension"])
        detail_rows.append(html.Tr([
            html.Td(lbl, style={"fontWeight": "600", "padding": "4px 12px"}),
            html.Td(f"{c['default']:.1%}", style={"padding": "4px 12px"}),
            html.Td(f"{c['adjusted']:.1%}", style={"padding": "4px 12px",
                     "color": THEME["up"] if c["delta"] >= 0 else THEME["down"]}),
            html.Td(f"{c['delta']:+.1%}", style={"padding": "4px 12px",
                     "color": THEME["up"] if c["delta"] >= 0 else THEME["down"]}),
            html.Td(f"{c['delta_pct']:+.0f}%", style={"padding": "4px 12px",
                     "color": THEME["up"] if c["delta"] >= 0 else THEME["down"]}),
        ]))

    detail_table = html.Table([
        html.Thead(html.Tr([
            html.Th("维度", style={"padding": "6px 12px", "textAlign": "left", "color": "rgba(255,255,255,0.5)"}),
            html.Th("默认权重", style={"padding": "6px 12px", "textAlign": "right", "color": "rgba(255,255,255,0.5)"}),
            html.Th("调整后", style={"padding": "6px 12px", "textAlign": "right", "color": "rgba(255,255,255,0.5)"}),
            html.Th("绝对变动", style={"padding": "6px 12px", "textAlign": "right", "color": "rgba(255,255,255,0.5)"}),
            html.Th("相对变动", style={"padding": "6px 12px", "textAlign": "right", "color": "rgba(255,255,255,0.5)"}),
        ])),
        html.Tbody(detail_rows),
    ], style={"width": "100%", "fontSize": "12px", "borderCollapse": "collapse"})

    # Summary stats
    total_delta = sum(abs(c["delta"]) for c in comparison)
    up_count = sum(1 for c in comparison if c["delta"] > 0)
    down_count = sum(1 for c in comparison if c["delta"] < 0)

    return html.Div([
        html.Div(style={"display": "flex", "gap": "12px", "marginBottom": "16px",
                         "flexWrap": "wrap"}, children=[
            _stat_card("📊 总调整幅度", f"{total_delta:.1%}",
                      f"调整 {up_count}↑ / {down_count}↓"),
            _stat_card("📐 维度数", f"{len(comparison)}个", "默认8个评分维度"),
            _stat_card("⚡ IC 驱动", "自动调整",
                      "基于近30日 Rank IC 均值"),
        ]),

        html.Div(style={"display": "flex", "gap": "12px", "flexWrap": "wrap"}, children=[
            html.Div(style={"flex": "2", "minWidth": "400px", "background": THEME["card"],
                            "borderRadius": "12px", "padding": "12px",
                            "border": "1px solid rgba(255,255,255,0.08)"}, children=[
                dcc.Graph(figure=fig_weight, config={"displayModeBar": False}),
            ]),
            html.Div(style={"flex": "1", "minWidth": "280px", "background": THEME["card"],
                            "borderRadius": "12px", "padding": "12px",
                            "border": "1px solid rgba(255,255,255,0.08)"}, children=[
                dcc.Graph(figure=fig_delta, config={"displayModeBar": False}),
            ]),
        ]),

        html.Div(style={"background": THEME["card"], "borderRadius": "12px", "padding": "12px",
                        "border": "1px solid rgba(255,255,255,0.08)", "background": THEME["card"], "marginTop": "12px"}, children=[
            html.Div("📋 权重调整明细", style={"fontSize": "13px", "fontWeight": "600",
                     "color": THEME["text_sec"], "marginBottom": "8px"}),
            detail_table,
        ]),
    ])


# =============================================================
# 执行历史 Tab
# =============================================================

def _render_exec_tab():
    nav = fetch_nav_history(60)
    trades = fetch_trade_history(30)
    signal_perf = fetch_signal_performance()
    exec_log = fetch_execution_log(7)

    # ── NAV 曲线 + 交易标记 ──
    fig_nav = go.Figure()

    if nav:
        nav_dates = [r["date"] for r in nav]
        nav_values = [(r["total_value"] or 0) for r in nav]
        nav_profits = [r["profit_pct"] for r in nav]

        fig_nav.add_trace(go.Scatter(
            x=nav_dates, y=nav_values,
            name="净值",
            mode="lines",
            line=dict(color="#FFD700", width=2),
            fill="tozeroy",
            fillcolor="rgba(255,215,0,0.08)",
            hovertemplate="%{x}: ¥%{y:,.0f}<extra></extra>",
        ))

        # Buy markers
        buy_trades = [t for t in trades if t["action"] == "buy"]
        if buy_trades:
            buy_dates = [t["date"] for t in buy_trades]
            buy_prices = []
            for t in buy_trades:
                price = t.get("trade_amount", 0) or (t.get("price", 0) * t.get("quantity", 0))
                buy_prices.append(price)
            fig_nav.add_trace(go.Scatter(
                x=buy_dates, y=buy_prices,
                name="买入",
                mode="markers",
                marker=dict(symbol="triangle-up", size=10, color="#00C853"),
                hovertemplate="🟢 买入 %{text}<br>%{x}<extra></extra>",
                text=[f"{t['name']} ¥{t['price']:.2f}" for t in buy_trades],
            ))

        # Sell markers
        sell_trades = [t for t in trades if t["action"] == "sell"]
        if sell_trades:
            sell_dates = [t["date"] for t in sell_trades]
            sell_prices = []
            for t in sell_trades:
                price = t.get("trade_amount", 0) or (t.get("price", 0) * t.get("quantity", 0))
                sell_prices.append(price)
            fig_nav.add_trace(go.Scatter(
                x=sell_dates, y=sell_prices,
                name="卖出",
                mode="markers",
                marker=dict(symbol="triangle-down", size=10, color="#FF1744"),
                hovertemplate="🔴 卖出 %{text}<br>%{x}<extra></extra>",
                text=[f"{t['name']} ¥{t['price']:.2f}" for t in sell_trades],
            ))

        fig_nav.update_layout(
            title=dict(text="📈 净值曲线 · 交易标记", font=dict(color=THEME["text"], size=13)),
            **CHART_LAYOUT,
            height=400,
        )

    # ── 交易明细表 ──
    trade_rows = []
    for t in reversed(trades[-20:]):  # last 20 trades
        action_icon = "🟢" if t["action"] == "buy" else "🔴"
        trade_rows.append(html.Tr([
            html.Td(t["date"], style={"padding": "4px 8px", "fontSize": "11px"}),
            html.Td(action_icon, style={"padding": "4px 4px"}),
            html.Td(t["name"], style={"padding": "4px 8px", "fontWeight": "600"}),
            html.Td(t["action"], style={"padding": "4px 8px",
                     "color": THEME["up"] if t["action"] == "buy" else THEME["down"]}),
            html.Td(f"¥{t['price']:.2f}", style={"padding": "4px 8px"}),
            html.Td(f"{t['quantity']}股", style={"padding": "4px 8px", "textAlign": "right"}),
            html.Td(f"¥{(t.get('trade_amount') or 0):,.0f}", style={"padding": "4px 8px", "textAlign": "right"}),
        ]))

    trade_table = html.Table([
        html.Thead(html.Tr([
            html.Th("日期", style={"padding": "6px 8px", "textAlign": "left", "color": THEME["text_ter"], "fontSize": "11px"}),
            html.Th("", style={"padding": "6px 4px"}),
            html.Th("标的", style={"padding": "6px 8px", "textAlign": "left", "color": THEME["text_ter"], "fontSize": "11px"}),
            html.Th("方向", style={"padding": "6px 8px", "textAlign": "left", "color": THEME["text_ter"], "fontSize": "11px"}),
            html.Th("价格", style={"padding": "6px 8px", "textAlign": "right", "color": THEME["text_ter"], "fontSize": "11px"}),
            html.Th("数量", style={"padding": "6px 8px", "textAlign": "right", "color": THEME["text_ter"], "fontSize": "11px"}),
            html.Th("金额", style={"padding": "6px 8px", "textAlign": "right", "color": THEME["text_ter"], "fontSize": "11px"}),
        ])),
        html.Tbody(trade_rows),
    ], style={"width": "100%", "fontSize": "12px", "borderCollapse": "collapse"})

    # ── 信号绩效表 ──
    perf_rows = []
    for p in signal_perf[:10]:
        ar1 = p.get("avg_return_1d") or 0
        ar3 = p.get("avg_return_3d") or 0
        wr1 = p.get("win_rate_1d") or 0
        wr3 = p.get("win_rate_3d") or 0
        perf_rows.append(html.Tr([
            html.Td(p["name"], style={"padding": "4px 8px", "fontWeight": "600"}),
            html.Td(p["action"], style={"padding": "4px 8px"}),
            html.Td(f"{p['total_signals']}次", style={"padding": "4px 8px", "textAlign": "right"}),
            html.Td(f"{wr1:.0f}%", style={"padding": "4px 8px", "textAlign": "right",
                     "color": THEME["up"] if wr1 >= 50 else THEME["down"]}),
            html.Td(f"{ar1:+.2f}%", style={"padding": "4px 8px", "textAlign": "right",
                     "color": THEME["up"] if ar1 >= 0 else THEME["down"]}),
            html.Td(f"{wr3:.0f}%", style={"padding": "4px 8px", "textAlign": "right",
                     "color": THEME["up"] if wr3 >= 50 else THEME["down"]}),
            html.Td(f"{ar3:+.2f}%", style={"padding": "4px 8px", "textAlign": "right",
                     "color": THEME["up"] if ar3 >= 0 else THEME["down"]}),
        ]))

    perf_table = html.Table([
        html.Thead(html.Tr([
            html.Th("标的", style={"padding": "6px 8px", "textAlign": "left", "color": THEME["text_ter"], "fontSize": "11px"}),
            html.Th("信号", style={"padding": "6px 8px", "textAlign": "left", "color": THEME["text_ter"], "fontSize": "11px"}),
            html.Th("次数", style={"padding": "6px 8px", "textAlign": "right", "color": THEME["text_ter"], "fontSize": "11px"}),
            html.Th("1D胜率", style={"padding": "6px 8px", "textAlign": "right", "color": THEME["text_ter"], "fontSize": "11px"}),
            html.Th("1D收益", style={"padding": "6px 8px", "textAlign": "right", "color": THEME["text_ter"], "fontSize": "11px"}),
            html.Th("3D胜率", style={"padding": "6px 8px", "textAlign": "right", "color": THEME["text_ter"], "fontSize": "11px"}),
            html.Th("3D收益", style={"padding": "6px 8px", "textAlign": "right", "color": THEME["text_ter"], "fontSize": "11px"}),
        ])),
        html.Tbody(perf_rows),
    ], style={"width": "100%", "fontSize": "12px", "borderCollapse": "collapse"})

    # ── 最近执行日志 ──
    exec_rows = []
    for e in exec_log[:15]:
        status_icon = "✅" if e["status"] == "success" else ("❌" if e["status"] == "failed" else "⏳")
        exec_rows.append(html.Tr([
            html.Td(e["date"], style={"padding": "3px 8px", "fontSize": "11px"}),
            html.Td(status_icon, style={"padding": "3px 4px"}),
            html.Td(e.get("name", e["code"]), style={"padding": "3px 8px", "fontWeight": "600", "fontSize": "11px"}),
            html.Td(e["action"], style={"padding": "3px 8px", "fontSize": "11px",
                     "color": THEME["up"] if e["action"] == "buy" else THEME["down"]}),
            html.Td(e.get("reason", "")[:40], style={"padding": "3px 8px", "fontSize": "10px",
                     "color": "rgba(255,255,255,0.5)"}),
        ]))

    exec_table = html.Table([
        html.Thead(html.Tr([
            html.Th("日期", style={"padding": "4px 8px", "textAlign": "left", "color": "rgba(255,255,255,0.5)", "fontSize": "10px"}),
            html.Th("", style={"padding": "4px 4px"}),
            html.Th("标的", style={"padding": "4px 8px", "textAlign": "left", "color": "rgba(255,255,255,0.5)", "fontSize": "10px"}),
            html.Th("方向", style={"padding": "4px 8px", "textAlign": "left", "color": "rgba(255,255,255,0.5)", "fontSize": "10px"}),
            html.Th("原因", style={"padding": "4px 8px", "textAlign": "left", "color": "rgba(255,255,255,0.5)", "fontSize": "10px"}),
        ])),
        html.Tbody(exec_rows),
    ], style={"width": "100%", "fontSize": "12px", "borderCollapse": "collapse"})

    # ── 信号绩效柱状图 ──
    if signal_perf:
        perf_dims = [p["name"] for p in signal_perf[:10]]
        perf_1d = [p["avg_return_1d"] or 0 for p in signal_perf[:10]]
        perf_3d = [p["avg_return_3d"] or 0 for p in signal_perf[:10]]
        perf_wr = [p["win_rate_1d"] or 0 for p in signal_perf[:10]]

        fig_perf = make_subplots(rows=1, cols=2,
                                 subplot_titles=("1D/3D 平均收益", "1D 胜率"),
                                 specs=[[{"type": "bar"}, {"type": "bar"}]])
        fig_perf.add_trace(go.Bar(
            x=perf_dims, y=perf_1d, name="1D收益",
            marker_color=[THEME["up"] if v >= 0 else THEME["down"] for v in perf_1d],
        ), row=1, col=1)
        fig_perf.add_trace(go.Bar(
            x=perf_dims, y=perf_3d, name="3D收益",
            marker_color=[THEME["down"] if v >= 0 else THEME["up"] for v in perf_3d],
        ), row=1, col=1)
        fig_perf.add_trace(go.Bar(
            x=perf_dims, y=perf_wr, name="1D胜率",
            marker_color="rgba(255,215,0,0.7)",
            text=[f"{v:.0f}%" for v in perf_wr],
            textposition="outside",
        ), row=1, col=2)

        fig_perf.update_layout(
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            xaxis=dict(**CHART_LAYOUT["xaxis"]),
            yaxis=dict(**CHART_LAYOUT["yaxis"]),
            xaxis2=dict(**CHART_LAYOUT["xaxis"]),
            yaxis2=dict(**CHART_LAYOUT["yaxis"]),
            margin=dict(l=10, r=20, t=40, b=10),
            height=300,
            font=dict(color=THEME["text"], size=10, family=THEME["font_family"]),
            showlegend=False,
        )

    nav_stats = {
        "trades_30d": len(trades),
        "buys_30d": len([t for t in trades if t["action"] == "buy"]),
        "sells_30d": len([t for t in trades if t["action"] == "sell"]),
        "exec_logs_7d": len(exec_log),
        "success_exec": len([e for e in exec_log if e["status"] == "success"]),
    }

    return html.Div([
        html.Div(style={"display": "flex", "gap": "12px", "marginBottom": "16px",
                         "flexWrap": "wrap"}, children=[
            _stat_card("📊 交易数 (30天)", str(nav_stats["trades_30d"]),
                      f"买入 {nav_stats['buys_30d']} / 卖出 {nav_stats['sells_30d']}"),
            _stat_card("⚡ 执行次数 (7天)", str(nav_stats["exec_logs_7d"]),
                      f"成功 {nav_stats['success_exec']}"),
            _stat_card("📈 信号胜率",
                      f"{len([p for p in signal_perf if p['win_rate_1d'] >= 50])}/{len(signal_perf)}",
                      "1D胜率≥50%的标的"),
        ]),

        # NAV + 交易标记
        html.Div(style={"background": THEME["card"], "borderRadius": "12px", "padding": "12px",
                        "border": "1px solid rgba(255,255,255,0.08)", "background": THEME["card"], "marginBottom": "12px"}, children=[
            dcc.Graph(figure=fig_nav, config={"displayModeBar": False}),
        ]),

        # 信号绩效
        html.Div(style={"display": "flex", "gap": "12px", "flexWrap": "wrap"}, children=[
            html.Div(style={"flex": "1", "minWidth": "320px", "background": THEME["card"],
                            "borderRadius": "12px", "padding": "12px",
                            "border": "1px solid rgba(255,255,255,0.08)"}, children=[
                html.Div("🎯 信号绩效", style={"fontSize": "13px", "fontWeight": "600",
                         "color": THEME["text_sec"], "marginBottom": "8px"}),
                perf_table if signal_perf else html.Div("暂无数据", style={"color": "rgba(255,255,255,0.3)", "textAlign": "center", "padding": "20px"}),
            ]),
            html.Div(style={"flex": "1", "minWidth": "320px", "background": THEME["card"],
                            "borderRadius": "12px", "padding": "12px",
                            "border": "1px solid rgba(255,255,255,0.08)"}, children=[
                html.Div("📋 最近交易", style={"fontSize": "13px", "fontWeight": "600",
                         "color": THEME["text_sec"], "marginBottom": "8px"}),
                trade_table if trades else html.Div("暂无交易", style={"color": "rgba(255,255,255,0.3)", "textAlign": "center", "padding": "20px"}),
            ]),
        ]),

        # 信号绩效图
        *([html.Div(style={"background": THEME["card"], "borderRadius": "12px", "padding": "12px",
                            "border": "1px solid rgba(255,255,255,0.08)", "marginTop": "12px"}, children=[
                dcc.Graph(figure=fig_perf, config={"displayModeBar": False}),
            ])] if signal_perf else []),

        # 执行日志
        html.Div(style={"background": THEME["card"], "borderRadius": "12px", "padding": "12px",
                        "border": "1px solid rgba(255,255,255,0.08)", "background": THEME["card"], "marginTop": "12px"}, children=[
            html.Div("📝 自动执行日志（近7天）", style={"fontSize": "13px", "fontWeight": "600",
                     "color": THEME["text_sec"], "marginBottom": "8px"}),
            exec_table if exec_log else html.Div("暂无日志", style={"color": "rgba(255,255,255,0.3)", "textAlign": "center", "padding": "20px"}),
        ]),
    ])


# =============================================================
# 风控状态 Tab
# =============================================================

def _render_risk_tab():
    report, text, err = fetch_risk_report()

    if err:
        return _error_card(f"⚠️ 风控数据获取失败: {err}")

    if not report:
        return _error_card("风控未启用")

    # 连续亏损
    consec = report.get("consecutive_losses", 0)
    max_consec = report.get("max_consecutive_losses", 2)
    consec_pct = min(consec / max(consec, 1), max_consec) / max_consec * 100 if max_consec > 0 else 0

    # 冷却期
    in_cooldown = report.get("in_cooldown", False)
    cooldown_until = report.get("cooldown_until", "")

    # 黑名单
    blacklist = report.get("blacklist", {})
    blacklist_count = report.get("blacklist_count", 0)

    # 回撤
    max_dd = report.get("max_drawdown_pct", 0)

    # 止损
    hard_stop_val = abs(report.get("hard_stop_pct", 0) / 100) if isinstance(report.get("hard_stop_pct"), (int, float)) else 6
    trailing_stop_val = abs(report.get("trailing_stop_pct", 0) / 100) if isinstance(report.get("trailing_stop_pct"), (int, float)) else 8

    # 日亏损 (absolute value for gauge)
    daily_loss_val = abs(report.get("daily_loss", 0))
    daily_loss_limit_val = abs(report.get("daily_loss_limit_pct", -0.04))

    # ── 风控仪表 ──
    gauge_style = {
        "background": "rgba(255,255,255,0.05)",
        "borderRadius": "10px",
        "padding": "16px",
        "textAlign": "center",
        "flex": "1",
        "minWidth": "150px",
    }

    def _gauge(label, value, max_val, unit="", good_direction="down"):
        pct = min(value / max(max_val, 1), 1) * 100 if max_val > 0 else 0
        color = THEME["up"] if pct < 60 else ("#FFD700" if pct < 85 else THEME["down"])
        bar_color = THEME["down"] if good_direction == "down" else THEME["up"]
        if good_direction == "up":
            color = THEME["down"] if pct < 60 else ("#FFD700" if pct < 85 else THEME["up"])

        return html.Div(style=gauge_style, children=[
            html.Div(label, style={"fontSize": "11px", "color": "rgba(255,255,255,0.5)", "marginBottom": "8px"}),
            html.Div(f"{value}{unit}", style={"fontSize": "24px", "fontWeight": "700", "color": color}),
            html.Div(f"上限 {max_val}{unit}", style={"fontSize": "10px", "color": "rgba(255,255,255,0.3)", "marginTop": "4px"}),
            html.Div(style={"background": "rgba(255,255,255,0.06)", "borderRadius": "6px", "height": "6px",
                            "marginTop": "8px", "overflow": "hidden"}, children=[
                html.Div(style={"background": bar_color, "height": "100%",
                                "width": f"{pct}%", "borderRadius": "6px",
                                "transition": "width 0.5s"}),
            ]),
        ])

    # ── 黑名单详情 ──
    blacklist_rows = []
    for code, expiry in sorted(blacklist.items()):
        name = STOCK_MAP.get(code, {}).get("name", code)
        blacklist_rows.append(html.Tr([
            html.Td(name, style={"padding": "4px 8px", "fontWeight": "600"}),
            html.Td(code, style={"padding": "4px 8px", "color": "rgba(255,255,255,0.5)"}),
            html.Td(expiry, style={"padding": "4px 8px", "color": THEME["down"]}),
        ]))

    bl_table = html.Table([
        html.Thead(html.Tr([
            html.Th("标的", style={"padding": "6px 8px", "textAlign": "left", "color": THEME["text_ter"], "fontSize": "11px"}),
            html.Th("代码", style={"padding": "6px 8px", "textAlign": "left", "color": THEME["text_ter"], "fontSize": "11px"}),
            html.Th("解禁日", style={"padding": "6px 8px", "textAlign": "left", "color": THEME["text_ter"], "fontSize": "11px"}),
        ])),
        html.Tbody(blacklist_rows),
    ], style={"width": "100%", "fontSize": "12px", "borderCollapse": "collapse"})

    return html.Div([
        # 概览卡片
        html.Div(style={"display": "flex", "gap": "12px", "flexWrap": "wrap",
                         "marginBottom": "16px"}, children=[
            html.Div(style={"flex": "1", "minWidth": "200px", "background": THEME["card"],
                            "borderRadius": "12px", "padding": "12px",
                            "border": "1px solid rgba(255,255,255,0.08)"}, children=[
                html.Div("🛡️ 风控状态", style={"fontSize": "13px", "fontWeight": "600",
                         "color": "rgba(255,255,255,0.6)", "marginBottom": "12px"}),
                html.Div(style={"display": "flex", "gap": "4px", "flexWrap": "wrap"}, children=[
                    html.Span("✅ 正常" if not in_cooldown and blacklist_count == 0 else "⚠️ 有风险",
                             style={"fontSize": "16px", "fontWeight": "700",
                                    "color": THEME["down"] if not in_cooldown and blacklist_count == 0 else "#FFD700"}),
                    html.Span(f"冷却:{'是' if in_cooldown else '否'}",
                             style={"fontSize": "11px", "color": "rgba(255,255,255,0.5)", "marginLeft": "12px"}),
                    html.Span(f"黑名单:{blacklist_count}只",
                             style={"fontSize": "11px", "color": "rgba(255,255,255,0.5)", "marginLeft": "12px"}),
                    html.Span(f"连续亏损:{consec}/{max_consec}",
                             style={"fontSize": "11px", "color": "rgba(255,255,255,0.5)", "marginLeft": "12px"}),
                ]),
                html.Div(style={"marginTop": "12px", "fontSize": "11px", "whiteSpace": "pre-wrap",
                         "color": "rgba(255,255,255,0.6)", "lineHeight": "1.6"}, children=[
                    text if text else "暂无风控状态报告",
                ]),
            ]),
        ]),

        # 仪表盘
        html.Div(style={"display": "flex", "gap": "12px", "flexWrap": "wrap",
                         "marginBottom": "16px"}, children=[
            _gauge("连续亏损", consec, max_consec),
            _gauge("日亏损", round(daily_loss_val, 3), round(daily_loss_limit_val, 3), good_direction="down"),
            _gauge("最大回撤", abs(max_dd) if isinstance(max_dd, (int, float)) else 0, 12, "%", good_direction="down"),
            _gauge("硬止损", hard_stop_val, 6, "%", good_direction="down"),
            _gauge("黑名单", blacklist_count, 5, "只", good_direction="up") if blacklist_count > 0 else html.Div("暂无黑名单", style={"color": "rgba(255,255,255,0.3)", "textAlign": "center", "padding": "16px", "flex": "1"}),
        ]),

        # 黑名单详情
        *([html.Div(style={"background": THEME["card"], "borderRadius": "12px", "padding": "12px",
                            "border": "1px solid rgba(255,255,255,0.08)"}, children=[
                html.Div(f"📋 黑名单 ({blacklist_count})",
                         style={"fontSize": "13px", "fontWeight": "600",
                                "color": THEME["text_sec"], "marginBottom": "8px"}),
                bl_table,
            ])] if blacklist_rows else []),
    ])


# =============================================================
# 工具组件
# =============================================================

def _stat_card(title, value, subtitle=""):
    """统计卡片"""
    return html.Div(style={
        "flex": "1", "minWidth": "140px", "background": THEME["card"],
        "borderRadius": "12px", "padding": "12px 16px",
        "border": "1px solid rgba(255,255,255,0.08)",
    }, children=[
        html.Div(title, style={"fontSize": "11px", "color": THEME["text_ter"],
                                "marginBottom": "4px", "fontWeight": "500",
                                "letterSpacing": "0.3px", "textTransform": "uppercase"}),
        html.Div(value, style={"fontSize": "22px", "fontWeight": "700", "color": THEME["gold"],
                                "fontFamily": THEME["font_mono"]}),
        html.Div(subtitle, style={"fontSize": "10px", "color": THEME["text_ter"],
                                   "marginTop": "2px"}),
    ])


def _error_card(msg):
    """错误提示卡片"""
    return html.Div(style={
        "background": "rgba(255,23,68,0.06)",
        "border": "1px solid rgba(255,23,68,0.15)",
        "borderRadius": "12px", "padding": "20px", "textAlign": "center",
    }, children=[
        html.Div(msg, style={"color": THEME["down"], "fontSize": "13px"}),
    ])


# =============================================================
# 更新时间
# =============================================================

@callback(Output("last-update", "children"), Input("refresh-timer", "n_intervals"))
def _update_time(_n):
    return f"🔄 {datetime.now().strftime('%H:%M:%S')}"


# =============================================================
# 启动
# =============================================================

if __name__ == "__main__":
    log.info("🅳 Serenity Dash 看板启动 — http://localhost:8050")
    app.run(host="0.0.0.0", port=8050, debug=False)
