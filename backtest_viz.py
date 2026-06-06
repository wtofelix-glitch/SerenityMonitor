#!/usr/bin/env python3
"""
14因子回测可视化报告 — Plotly HTML
生成: 资金曲线+回撤图+因子热力图+交易明细表
配色: 红 #FF4444(上涨/正信号) / 绿 #00AA44(下跌/负信号)
输出: reports/backtest_factors_{code}_{date}.html
"""

import os
import uuid
from datetime import datetime
from typing import Optional

import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from backtest_engine import MultiFactorWithSignalsStrategy, BacktestTrade
from db import get_price_history
from config import STOCK_MAP

# ── 14因子名称 & 展示名 ──
FACTOR_KEYS = [
    "ksft", "rank_20", "rsv_20", "beta_20", "resi_20",
    "macd_signal", "obv_trend", "mfi_signal", "cci_signal",
    "wq_alpha1", "wq_alpha3", "wq_alpha5", "wq_alpha15", "wq_alpha19",
]
FACTOR_LABELS = {
    "ksft": "K线形态", "rank_20": "Rank", "rsv_20": "RSV",
    "beta_20": "Beta", "resi_20": "残差", "macd_signal": "MACD",
    "obv_trend": "OBV", "mfi_signal": "MFI", "cci_signal": "CCI",
    "wq_alpha1": "A1日内", "wq_alpha3": "A3均价", "wq_alpha5": "A5价偏",
    "wq_alpha15": "A15波幅", "wq_alpha19": "A19动量",
}

REPORTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")


# ═══════════════════════════════════════════════════════════════
# 回测运行器（含因子信号采集）
# ═══════════════════════════════════════════════════════════════

def run_factor_backtest_viz(code: str, initial_capital: float = 50000.0) -> Optional[dict]:
    """
    对指定标的运行14因子回测，返回结构化结果
    包含: equity_curve, trades, factor_history, closes, dates
    """
    rows = get_price_history(code, 500)
    if len(rows) < 30:
        return None

    rows.sort(key=lambda r: r["date"])
    closes = np.array([r["close"] for r in rows], dtype=float)
    highs = np.array([r["high"] for r in rows], dtype=float)
    lows = np.array([r["low"] for r in rows], dtype=float)
    volumes = np.array([r["volume"] for r in rows], dtype=float)
    dates = [r["date"] for r in rows]
    n = len(rows)

    strategy = MultiFactorWithSignalsStrategy(use_factors=True)
    strategy.prepare(code, closes, highs, lows, volumes, dates)

    # 回测状态
    capital = initial_capital
    position = 0
    entry_price = 0.0
    entry_date = ""
    in_position = False

    trades: list[BacktestTrade] = []
    equity_curve = [(dates[0], capital)]

    commission_rate = 0.00025
    stamp_tax = 0.001
    position_pct = 0.30

    for i in range(n):
        date_str = dates[i]
        close = closes[i]

        # 生成信号 + 记录因子（generate_signals 在 idx>=30 时写入 factor_history）
        signal, reason = strategy.generate_signals(i)

        # idx<30 时 generate_signals 不记录因子，需手动采集
        if i < 30:
            _, sigs = strategy._compute_14factor_signals(i)
            strategy.factor_history.append((date_str, signal, dict(sigs)))

        if not in_position and signal > 0.5:
            cost = capital * position_pct * min(1.0, signal)
            fee = cost * commission_rate
            available = cost - fee
            shares = int(available / close / 100) * 100
            if shares >= 100 and available >= shares * close:
                position = shares
                entry_price = close
                entry_date = date_str
                capital -= shares * close * (1 + commission_rate)
                in_position = True

        elif in_position and signal < -0.3:
            sell_value = position * close
            fee = sell_value * (commission_rate + stamp_tax)
            capital += sell_value - fee
            profit_pct = (close - entry_price) / entry_price * 100
            hold_days = (
                datetime.strptime(date_str, "%Y-%m-%d")
                - datetime.strptime(entry_date, "%Y-%m-%d")
            ).days if entry_date else 0
            trades.append(BacktestTrade(
                entry_date=entry_date, entry_price=entry_price,
                exit_date=date_str, exit_price=close,
                profit_pct=round(profit_pct, 2),
                hold_days=hold_days,
                exit_reason="",
            ))
            position = 0
            entry_price = 0.0
            in_position = False

        total_value = capital + position * close
        equity_curve.append((date_str, round(total_value, 2)))

    # 强制平仓
    if in_position:
        close = closes[-1]
        sell_value = position * close
        fee = sell_value * (commission_rate + stamp_tax)
        capital += sell_value - fee
        profit_pct = (close - entry_price) / entry_price * 100
        trades.append(BacktestTrade(
            entry_date=entry_date, entry_price=entry_price,
            exit_date=dates[-1], exit_price=close,
            profit_pct=round(profit_pct, 2),
            hold_days=0,
            exit_reason="回测结束强平",
        ))

    # 使用 strategy 记录的 factor_history
    factor_history = strategy.factor_history

    return {
        "code": code,
        "initial_capital": initial_capital,
        "final_capital": capital,
        "equity_curve": equity_curve,
        "trades": trades,
        "factor_history": factor_history,
        "dates": dates,
        "closes": closes.tolist(),
    }


# ═══════════════════════════════════════════════════════════════
# 图表生成
# ═══════════════════════════════════════════════════════════════

def _calc_drawdown(equity_values: list[float]) -> list[float]:
    """计算每日回撤百分比"""
    dd = []
    peak = equity_values[0]
    for v in equity_values:
        if v > peak:
            peak = v
        dd.append((peak - v) / peak * -100 if peak > 0 else 0)
    return dd


def build_equity_curve_chart(data: dict) -> str:
    """图表1: 资金曲线图 — 累计净值 + 买卖点标注"""
    eq = data["equity_curve"]
    dates = [e[0] for e in eq]
    values = [e[1] for e in eq]

    fig = go.Figure()

    # 净值曲线
    fig.add_trace(go.Scatter(
        x=dates, y=values,
        mode="lines",
        name="累计净值",
        line=dict(color="#FF4444", width=2),
        hovertemplate="%{x}<br>净值: ¥%{y:,.2f}<extra></extra>",
    ))

    # 买入点 (从 equity_curve 推断: 资产突然下降 => 买入)
    buy_x, buy_y = [], []
    sell_x, sell_y = [], []
    for t in data["trades"]:
        buy_x.append(t.entry_date)
        buy_y.append(t.entry_price * (data.get("shares_at_entry", {}).get(t.entry_date, 1) or 1))
        sell_x.append(t.exit_date)
        sell_y.append(t.exit_price * (data.get("shares_at_exit", {}).get(t.exit_date, 1) or 1))

    # 更好的方法: 从 equity_curve 找买入卖出后的值
    eq_map = dict(eq)
    for t in data["trades"]:
        buy_v = eq_map.get(t.entry_date)
        sell_v = eq_map.get(t.exit_date)
        if buy_v:
            buy_x.append(t.entry_date)
            buy_y.append(buy_v)
        if sell_v:
            sell_x.append(t.exit_date)
            sell_y.append(sell_v)

    if buy_x:
        fig.add_trace(go.Scatter(
            x=buy_x, y=buy_y,
            mode="markers",
            name="买入",
            marker=dict(symbol="triangle-up", size=14, color="#00AA44",
                        line=dict(width=1, color="white")),
            hovertemplate="买入<br>%{x}<br>¥%{y:,.2f}<extra></extra>",
        ))
    if sell_x:
        fig.add_trace(go.Scatter(
            x=sell_x, y=sell_y,
            mode="markers",
            name="卖出",
            marker=dict(symbol="triangle-down", size=14, color="#FF4444",
                        line=dict(width=1, color="white")),
            hovertemplate="卖出<br>%{x}<br>¥%{y:,.2f}<extra></extra>",
        ))

    fig.update_layout(
        title=dict(text="📈 资金曲线", font=dict(size=18), x=0.5),
        xaxis=dict(title="日期", tickangle=-30, tickfont=dict(size=10)),
        yaxis=dict(title="净值 (¥)", tickprefix="¥", tickfont=dict(size=11)),
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(l=60, r=30, t=50, b=60),
        paper_bgcolor="white", plot_bgcolor="#FAFAFA",
        height=400,
    )
    fig.update_xaxes(gridcolor="#E8E8E8")
    fig.update_yaxes(gridcolor="#E8E8E8")
    return fig.to_html(full_html=False, include_plotlyjs=False)


def build_drawdown_chart(data: dict) -> str:
    """图表2: 回撤曲线图 — 标注最大回撤"""
    eq = data["equity_curve"]
    dates = [e[0] for e in eq]
    values = [e[1] for e in eq]
    drawdowns = _calc_drawdown(values)

    fig = go.Figure()

    fig.add_trace(go.Scatter(
        x=dates, y=drawdowns,
        mode="lines",
        name="回撤",
        fill="tozeroy",
        fillcolor="rgba(0,170,68,0.10)",
        line=dict(color="#00AA44", width=1.5),
        hovertemplate="%{x}<br>回撤: %{y:.2f}%<extra></extra>",
    ))

    # 标注最大回撤
    min_dd = min(drawdowns)
    min_idx = drawdowns.index(min_dd)
    fig.add_annotation(
        x=dates[min_idx], y=min_dd,
        text=f"最大回撤 {min_dd:.2f}%",
        showarrow=True,
        arrowhead=2, arrowsize=1.2, arrowwidth=2, arrowcolor="#FF4444",
        ax=60, ay=-50,
        font=dict(size=12, color="#FF4444", weight="bold"),
        bgcolor="white", bordercolor="#FF4444", borderwidth=1, borderpad=4,
    )

    fig.add_hline(y=0, line_dash="dot", line_color="#888", line_width=0.8)

    fig.update_layout(
        title=dict(text="📉 回撤曲线", font=dict(size=18), x=0.5),
        xaxis=dict(title="日期", tickangle=-30, tickfont=dict(size=10)),
        yaxis=dict(title="回撤 %", ticksuffix="%", tickfont=dict(size=11),
                   autorange="reversed"),
        hovermode="x unified",
        margin=dict(l=60, r=30, t=50, b=60),
        paper_bgcolor="white", plot_bgcolor="#FAFAFA",
        height=350,
    )
    fig.update_xaxes(gridcolor="#E8E8E8")
    fig.update_yaxes(gridcolor="#E8E8E8")
    return fig.to_html(full_html=False, include_plotlyjs=False)


def build_factor_heatmap(data: dict) -> str:
    """图表3: 因子信号热力图 — 14因子信号强度 (绿→白→红)"""
    fh = data["factor_history"]
    if not fh:
        return ""

    fh_dates = [f[0] for f in fh]
    fh_signals = [f[2] for f in fh]

    # 构建矩阵: 行=日期, 列=因子
    z = []
    for sig in fh_signals:
        row = [sig.get(k, 0) for k in FACTOR_KEYS]
        z.append(row)

    # 如果数据太多，采样显示（最多显示200个交易日）
    max_points = 200
    if len(fh_dates) > max_points:
        step = len(fh_dates) // max_points
        idxs = list(range(0, len(fh_dates), step))
        if idxs[-1] != len(fh_dates) - 1:
            idxs.append(len(fh_dates) - 1)
        fh_dates = [fh_dates[i] for i in idxs]
        z = [z[i] for i in idxs]

    # 显示日期标签采样：最多显示20个
    date_labels = []
    label_step = max(1, len(fh_dates) // 20)
    for i, d in enumerate(fh_dates):
        if i % label_step == 0 or i == len(fh_dates) - 1:
            date_labels.append(d)
        else:
            date_labels.append("")

    fig = go.Figure(data=go.Heatmap(
        z=list(zip(*z)),  # 转置: 行=因子, 列=日期
        x=fh_dates,
        y=[FACTOR_LABELS.get(k, k) for k in FACTOR_KEYS],
        colorscale=[
            [0.0, "#00AA44"],    # 绿色 (强负信号)
            [0.25, "#66CC88"],
            [0.5, "#EEEEEE"],     # 白色 (中性)
            [0.75, "#FF8888"],
            [1.0, "#FF4444"],     # 红色 (强正信号)
        ],
        zmin=-1.0, zmax=1.0,
        colorbar=dict(title=dict(text="信号强度", side="right"),
                      tickvals=[-1, -0.5, 0, 0.5, 1],
                      ticktext=["-1.0", "-0.5", "0", "+0.5", "+1.0"]),
        hovertemplate="因子: %{y}<br>日期: %{x}<br>信号: %{z:+.3f}<extra></extra>",
    ))

    fig.update_layout(
        title=dict(text="🔥 14因子信号热力图", font=dict(size=18), x=0.5),
        xaxis=dict(title="交易日期", tickangle=-45,
                   tickvals=[d for i, d in enumerate(fh_dates)
                             if i % max(1, len(fh_dates)//20) == 0 or i == len(fh_dates)-1],
                   tickfont=dict(size=9)),
        yaxis=dict(title="因子", tickfont=dict(size=10)),
        margin=dict(l=80, r=80, t=50, b=120),
        paper_bgcolor="white", plot_bgcolor="#FAFAFA",
        height=max(350, len(FACTOR_KEYS) * 22 + 80),
    )
    return fig.to_html(full_html=False, include_plotlyjs=False)


def build_trade_table_html(data: dict) -> str:
    """图表4: 交易明细表 — HTML表格"""
    trades = data["trades"]
    if not trades:
        return "<div style='text-align:center;padding:30px;color:#888'>暂无交易</div>"

    rows_html = ""
    for t in trades:
        pct = t.profit_pct
        cls = "up" if pct >= 0 else "down"
        sign = "+" if pct >= 0 else ""
        rows_html += f"""
        <tr>
            <td>{t.entry_date}</td>
            <td>{t.exit_date}</td>
            <td class="{cls}">{sign}{pct:.2f}%</td>
            <td>{t.hold_days}天</td>
            <td>{t.exit_reason[:40] if t.exit_reason else "-"}</td>
        </tr>"""

    return f"""
    <div class="cb" style="overflow-x:auto">
      <h3 style="text-align:center;margin:0 0 16px 0;font-size:18px">📋 交易明细</h3>
      <table class="st">
        <thead><tr>
          <th>买入日期</th><th>卖出日期</th><th>收益率</th><th>持有天数</th><th>退出原因</th>
        </tr></thead>
        <tbody>{rows_html}</tbody>
      </table>
    </div>"""


# ═══════════════════════════════════════════════════════════════
# 完整HTML报告
# ═══════════════════════════════════════════════════════════════

def _calc_metrics(data: dict) -> dict:
    """计算回测指标"""
    eq = data["equity_curve"]
    values = [e[1] for e in eq]
    init_cap = data["initial_capital"]
    final_cap = data["final_capital"]
    trades = data["trades"]

    total_return = (final_cap - init_cap) / init_cap * 100

    dates = [e[0] for e in eq]
    if len(dates) >= 2:
        total_days = (
            datetime.strptime(dates[-1], "%Y-%m-%d")
            - datetime.strptime(dates[0], "%Y-%m-%d")
        ).days
    else:
        total_days = 1
    years = max(0.1, total_days / 365)
    annual_return = ((final_cap / init_cap) ** (1 / years) - 1) * 100 if init_cap > 0 else 0

    winning = [t for t in trades if t.profit_pct > 0]
    losing = [t for t in trades if t.profit_pct <= 0]
    win_rate = len(winning) / max(1, len(trades)) * 100

    peak = values[0]
    max_dd = 0
    for v in values:
        if v > peak:
            peak = v
        dd = (peak - v) / peak * 100
        if dd > max_dd:
            max_dd = dd

    if len(values) > 1:
        daily_returns = np.diff(values) / np.array(values[:-1])
        sr = (daily_returns.mean() / max(1e-12, daily_returns.std())) * np.sqrt(252)
    else:
        sr = 0

    avg_hold = np.mean([t.hold_days for t in trades]) if trades else 0
    avg_win = np.mean([t.profit_pct for t in winning]) if winning else 0
    avg_loss = np.mean([t.profit_pct for t in losing]) if losing else 0
    total_win = sum(t.profit_pct for t in winning) if winning else 0
    total_loss = abs(sum(t.profit_pct for t in losing)) if losing else 0
    pf = total_win / max(1, total_loss)

    return {
        "total_return": total_return,
        "annual_return": annual_return,
        "win_rate": win_rate,
        "total_trades": len(trades),
        "winning_trades": len(winning),
        "losing_trades": len(losing),
        "max_drawdown": max_dd,
        "sharpe": sr,
        "avg_hold_days": avg_hold,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_factor": pf,
    }


def generate_report_html(code: str, data: dict) -> str:
    """生成完整Plotly HTML报告"""
    m = _calc_metrics(data)
    name = STOCK_MAP.get(code, {}).get("name", code)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    eq_chart = build_equity_curve_chart(data)
    dd_chart = build_drawdown_chart(data)
    heatmap = build_factor_heatmap(data)
    trade_table = build_trade_table_html(data)

    ret_color = "#FF4444" if m["total_return"] >= 0 else "#00AA44"
    ret_sign = "+" if m["total_return"] >= 0 else ""

    # 复用一个Plotly figure来加载js，避免重复
    fig_id = uuid.uuid4().hex[:12]

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>14因子回测报告 — {name}({code})</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Microsoft YaHei",sans-serif;background:#F0F2F5;color:#333;padding:20px}}
.container{{max-width:1200px;margin:0 auto}}
.header{{background:linear-gradient(135deg,#1a1a2e,#16213e);color:#fff;padding:30px 40px;border-radius:12px;margin-bottom:24px;box-shadow:0 4px 20px rgba(0,0,0,.15)}}
.header h1{{font-size:26px;margin-bottom:6px}}
.header p{{color:#aab;font-size:14px}}
.sg{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:24px}}
.card{{background:#fff;padding:18px 14px;border-radius:10px;text-align:center;box-shadow:0 2px 8px rgba(0,0,0,.06)}}
.card .v{{font-size:24px;font-weight:700;margin:6px 0 2px}}
.card .l{{font-size:12px;color:#888}}
.card .s{{font-size:11px;color:#aaa}}
.cb{{background:#fff;border-radius:10px;padding:16px;margin-bottom:24px;box-shadow:0 2px 8px rgba(0,0,0,.06)}}
.st{{width:100%;border-collapse:collapse;font-size:14px}}
.st th{{background:#2C3E50;color:#fff;padding:10px 8px;text-align:center;white-space:nowrap}}
.st td{{padding:8px;text-align:center;border-bottom:1px solid #eee;white-space:nowrap}}
.st tr:hover td{{background:#F5F5F5}}
.st .up{{color:#FF4444;font-weight:700}}
.st .down{{color:#00AA44;font-weight:700}}
.ft{{text-align:center;color:#999;font-size:12px;padding:20px}}
</style>
</head>
<body>
<div class="container">

<div class="header">
  <h1>🧘 14因子回测报告 — {name}({code})</h1>
  <p>生成时间: {now} &nbsp;|&nbsp; 策略: 14因子信号策略 &nbsp;|&nbsp; 阈值: &gt;0.2买入 / &lt;-0.2卖出</p>
</div>

<div class="sg">
  <div class="card">
    <div class="l">总收益率</div>
    <div class="v" style="color:{ret_color}">{ret_sign}{m["total_return"]:.2f}%</div>
    <div class="s">¥{data["initial_capital"]:,.0f} → ¥{data["final_capital"]:,.0f}</div>
  </div>
  <div class="card">
    <div class="l">年化收益</div>
    <div class="v" style="color:{ret_color}">{ret_sign}{m["annual_return"]:.2f}%</div>
    <div class="s">夏普 {m["sharpe"]:.2f}</div>
  </div>
  <div class="card">
    <div class="l">交易次数</div>
    <div class="v">{m["total_trades"]}</div>
    <div class="s">胜 {m["winning_trades"]} / 负 {m["losing_trades"]}</div>
  </div>
  <div class="card">
    <div class="l">胜率</div>
    <div class="v">{m["win_rate"]:.1f}%</div>
    <div class="s">平均收益 {m["avg_win"]:+.2f}%</div>
  </div>
  <div class="card">
    <div class="l">最大回撤</div>
    <div class="v">{m["max_drawdown"]:.2f}%</div>
    <div class="s">盈亏比 {m["profit_factor"]:.2f}</div>
  </div>
  <div class="card">
    <div class="l">平均持仓</div>
    <div class="v">{m["avg_hold_days"]:.0f}天</div>
    <div class="s">平均亏损 {m["avg_loss"]:.2f}%</div>
  </div>
</div>

<div class="cb">{eq_chart}</div>
<div class="cb">{dd_chart}</div>
<div class="cb">{heatmap}</div>
{trade_table}

<div class="ft">SerenityMonitor — 14因子回测报告自动生成 | 报告仅供参考，不构成投资建议</div>

</div>
</body>
</html>"""

    return html


def generate_viz_report(code: str) -> str:
    """
    完整流程: 回测 → 生成HTML → 保存 → 返回路径
    """
    os.makedirs(REPORTS_DIR, exist_ok=True)

    data = run_factor_backtest_viz(code)
    if data is None:
        return ""

    today_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"backtest_factors_{code}_{today_str}.html"
    filepath = os.path.join(REPORTS_DIR, filename)

    html = generate_report_html(code, data)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html)

    return filepath


if __name__ == "__main__":
    import sys
    code = sys.argv[1] if len(sys.argv) > 1 else "002281"
    path = generate_viz_report(code)
    if path:
        print(f"✅ 回测可视化报告已生成: {path}")
    else:
        print(f"❌ 数据不足，无法生成报告（{code}）")
