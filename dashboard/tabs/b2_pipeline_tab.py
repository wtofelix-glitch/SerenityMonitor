"""
UI-P0: B2 影子管线 Tab — 离线历史报告展示。

依赖: dashboard.b2_data_provider (纯数据, 不依赖 Dash)
禁止: SQLite, B2Runner, 生产DB, 网络, push/trade adapter
"""

from __future__ import annotations

import os
from datetime import datetime

from dash import html, dcc
import plotly.graph_objects as go

from dashboard.b2_data_provider import (
    B2ReportProvider, B2DashboardViewModel, ReportStatus,
    DEFAULT_STALE_SECONDS,
)

# ══════════════════════════════════════════════════════════════════════════
# 主题 (继承 Dash 看板暗色主题)
# ══════════════════════════════════════════════════════════════════════════

THEME = {
    "bg": "#0d1117",
    "card_bg": "rgba(22,27,34,0.85)",
    "border": "rgba(255,255,255,0.06)",
    "text": "#c9d1d9",
    "text_dim": "rgba(232,234,237,0.5)",
    "green": "#3fb950",
    "red": "#f85149",
    "yellow": "#d2991d",
    "blue": "#58a6ff",
    "font_mono": "'SF Mono', 'Fira Code', monospace",
}

CARD_STYLE = {
    "background": THEME["card_bg"],
    "border": f"1px solid {THEME['border']}",
    "borderRadius": "8px",
    "padding": "16px",
    "marginBottom": "12px",
}

TAG_STYLE = {
    "display": "inline-block",
    "padding": "2px 8px",
    "borderRadius": "4px",
    "fontSize": "10px",
    "fontFamily": THEME["font_mono"],
    "marginRight": "6px",
    "marginBottom": "4px",
}

BANNER_STYLE = {
    "textAlign": "center",
    "padding": "6px 12px",
    "fontSize": "10px",
    "fontFamily": THEME["font_mono"],
    "letterSpacing": "0.5px",
    "borderBottom": f"1px solid {THEME['border']}",
}

FAILURE_NAMES = [
    ("scheduler", "调度"), ("session_check", "时段检查"), ("fetch", "请求"),
    ("http", "HTTP"), ("parse", "解析"), ("validation", "校验"),
    ("normalization", "标准化"), ("quarantine", "隔离"), ("event", "事件"),
    ("signal", "信号"), ("ledger", "账本"), ("report", "报告"),
    ("safety_guard", "安全守卫"),
]


# ══════════════════════════════════════════════════════════════════════════
# 公开入口
# ══════════════════════════════════════════════════════════════════════════

def render_b2_tab(report_path: str = "", report_root: str = "",
                  stale_seconds: int = DEFAULT_STALE_SECONDS) -> html.Div:
    """渲染 B2 管线 Tab。

    Args:
        report_path: B2 报告 JSON 路径。空字符串 → 显示 NO_DATA 占位。
        report_root: 报告根目录, 用于路径安全。空字符串 → 禁用加载。
        stale_seconds: 报告过期阈值 (秒)。默认 300。
    """
    # 数据加载
    vm = _load_report(report_path, report_root, stale_seconds)

    # 错误状态 → 占位页
    if vm.report_status != ReportStatus.OK:
        return _render_error_page(vm)

    return html.Div([
        _render_banner(vm),
        _render_run_identity(vm),
        _render_signal_storm_alert(vm),
        _render_four_cards(vm),
        _render_lineage_status(vm),
        _render_audit_equations(vm),
        _render_latency_failures(vm),
        _render_cycle_table(vm),
    ], style={"maxWidth": "1400px", "margin": "0 auto", "padding": "12px"})


# ══════════════════════════════════════════════════════════════════════════
# 数据加载 (feature-flag safe)
# ══════════════════════════════════════════════════════════════════════════

def _load_report(path: str, root: str,
                 stale_seconds: int = DEFAULT_STALE_SECONDS) -> B2DashboardViewModel:
    if not path or not root:
        vm = B2DashboardViewModel()
        vm.report_status = ReportStatus.NO_DATA
        vm.warnings = ["未配置报告路径 (report_path/report_root 为空)"]
        return vm
    provider = B2ReportProvider(report_root=root, stale_seconds=stale_seconds)
    return provider.load(path)


# ══════════════════════════════════════════════════════════════════════════
# 顶部横幅
# ══════════════════════════════════════════════════════════════════════════

def _render_banner(vm: B2DashboardViewModel) -> html.Div:
    schema_badge = _tag(f"schema:{vm.source_schema}",
                        THEME["green"] if vm.source_schema == "b2-1.1" else THEME["yellow"])
    compat_note = ""
    if vm.compatibility_mode:
        compat_note = " · COMPAT MODE (b2-1.0 → canonical)"
    return html.Div([
        html.Span("SHADOW OBSERVABILITY  ·  NOT FOR EXECUTION  ·  READ ONLY"),
        html.Span(f"  {compat_note}", style={"color": THEME["text_dim"], "fontSize": "9px"}),
        html.Span(schema_badge, style={"marginLeft": "12px"}),
    ], style={**BANNER_STYLE, "color": THEME["yellow"],
              "background": "rgba(210,153,29,0.08)"})


# ══════════════════════════════════════════════════════════════════════════
# 运行身份
# ══════════════════════════════════════════════════════════════════════════

def _render_run_identity(vm: B2DashboardViewModel) -> html.Div:
    status_color = {"RUNNING": THEME["green"], "COMPLETED": THEME["blue"],
                    "ERROR": THEME["red"], "STOPPED": THEME["yellow"],
                    "AUTO_STOPPED": THEME["yellow"]}.get(vm.run_status, THEME["text_dim"])

    tags = [
        _tag(vm.run_id, THEME["blue"]),
        _tag(f"commit:{vm.source_commit[:8]}", THEME["text_dim"]),
        _tag(f"tag:{vm.source_tag}", THEME["text_dim"]),
        _tag(vm.environment, THEME["green"]),
        _tag(vm.market_session, THEME["yellow"]),
    ]

    return html.Div([
        html.Div([
            html.Span("● ", style={"color": status_color, "fontSize": "14px"}),
            html.Span(vm.run_status, style={"color": status_color, "fontWeight": "600",
                                             "fontSize": "16px", "marginRight": "16px"}),
            html.Span(f"{vm.started_at} → {vm.ended_at}" if vm.ended_at else vm.started_at,
                      style={"color": THEME["text_dim"], "fontSize": "12px",
                             "fontFamily": THEME["font_mono"]}),
        ]),
        html.Div(tags, style={"marginTop": "8px"}),
        html.Div([
            html.Span(f"报告: {vm.report_path}", style={"fontSize": "10px", "color": THEME["text_dim"]}),
            html.Span(f" | hash: {vm.raw_json_hash}", style={"fontSize": "10px", "color": THEME["text_dim"],
                                                              "fontFamily": THEME["font_mono"]}),
        ], style={"marginTop": "4px"}),
    ], style={**CARD_STYLE, "padding": "14px 16px"})


# ══════════════════════════════════════════════════════════════════════════
# 四张总览卡
# ══════════════════════════════════════════════════════════════════════════

def _render_four_cards(vm: B2DashboardViewModel) -> html.Div:
    return html.Div([
        html.Div(_render_card("⏱ 调度", [
            _kv("planned", vm.cycles_planned),
            _kv("started", vm.cycles_started),
            _kv("completed", vm.cycles_completed),
            _kv("aborted", vm.cycles_aborted, vm.cycles_aborted > 0),
            _kv("skipped", vm.cycles_skipped),
            _kv("not_due", vm.not_due_cycles),
        ]), style={"flex": "1", "minWidth": "220px"}),

        html.Div(_render_card("📊 数据质量", [
            _kv("raw_received", vm.raw_received),
            _kv("accepted", vm.normalized_accepted),
            _kv("rejected", vm.normalized_rejected, vm.normalized_rejected > 0),
            _kv("quarantined", vm.quarantined, vm.quarantined > 0),
            _kv("events_created", vm.events_created),
            _kv("dedup", vm.events_deduplicated),
        ]), style={"flex": "1", "minWidth": "220px"}),

        html.Div(_render_card("📡 事件·信号·账本", [
            _kv("signals_total", vm.signals_total),
            _kv("claimed", vm.ledger_claimed),
            _kv("with_signal", vm.ledger_completed_with_signal),
            _kv("no_signal", vm.ledger_completed_no_signal),
            _kv("failed", vm.ledger_failed, vm.ledger_failed > 0),
            _kv("in_progress", vm.ledger_in_progress, vm.ledger_in_progress > 0),
        ]), style={"flex": "1", "minWidth": "220px"}),

        html.Div(_render_card("🛡️ 安全隔离", [
            _kv("real_push", vm.real_push_count, vm.real_push_count > 0,
                force_color=THEME["green"] if vm.real_push_count == 0 else THEME["red"]),
            _kv("real_trade", vm.real_trade_count, vm.real_trade_count > 0,
                force_color=THEME["green"] if vm.real_trade_count == 0 else THEME["red"]),
            _kv("acct_mods", vm.account_modifications, vm.account_modifications > 0,
                force_color=THEME["green"] if vm.account_modifications == 0 else THEME["red"]),
            _kv("prod_changed", "YES" if vm.production_file_changes else "NO",
                vm.production_file_changes,
                force_color=THEME["green"] if not vm.production_file_changes else THEME["red"]),
            _kv("tag_miss", vm.missing_safety_tags, vm.missing_safety_tags > 0,
                force_color=THEME["green"] if vm.missing_safety_tags == 0 else THEME["red"]),
        ]), style={"flex": "1", "minWidth": "220px"}),
    ], style={"display": "flex", "gap": "12px", "flexWrap": "wrap"})


def _render_card(title: str, rows: list) -> html.Div:
    return html.Div([
        html.Div(title, style={"fontWeight": "600", "fontSize": "13px",
                               "color": THEME["text"], "marginBottom": "10px",
                               "paddingBottom": "8px",
                               "borderBottom": f"1px solid {THEME['border']}"}),
        html.Div(rows, style={"fontSize": "12px", "fontFamily": THEME["font_mono"]}),
    ], style=CARD_STYLE)


def _kv(label: str, value, alert: bool = False, force_color: str = "") -> html.Div:
    color = force_color or (THEME["red"] if alert else THEME["text_dim"])
    return html.Div([
        html.Span(label, style={"color": THEME["text_dim"], "marginRight": "8px"}),
        html.Span(str(value), style={"color": color, "fontWeight": "500" if alert else "400"}),
    ], style={"marginBottom": "4px"})


# ══════════════════════════════════════════════════════════════════════════
# 审计方程
# ══════════════════════════════════════════════════════════════════════════

def _render_audit_equations(vm: B2DashboardViewModel) -> html.Div:
    if not vm.audit_equations:
        return html.Div()

    rows = []
    for eq in vm.audit_equations:
        # Primary status = recalculated result (ground truth)
        if eq.primary_passed:
            primary_icon = "✅"
            primary_text = "PASS"
            primary_color = THEME["green"]
        else:
            primary_icon = "❌"
            primary_text = "FAIL"
            primary_color = THEME["red"]

        # Comparison status = how reported compares to recalculated
        comp = eq.comparison
        if comp == "UNKNOWN":
            comp_text = "UNKNOWN"
            comp_color = THEME["yellow"]
        elif comp == "MATCH":
            comp_text = "MATCH"
            comp_color = THEME["text_dim"]
        else:  # MISMATCH
            comp_text = "⚠ MISMATCH"
            comp_color = THEME["red"]

        ops_str = "  ".join(f"{k}={v}" for k, v in eq.operands.items())
        rows.append(html.Tr([
            html.Td(primary_icon, style={"width": "30px", "textAlign": "center"}),
            html.Td(eq.name, style={"color": THEME["text"], "fontWeight": "500",
                                     "fontFamily": THEME["font_mono"], "fontSize": "11px"}),
            html.Td(eq.expression, style={"color": THEME["text_dim"], "fontSize": "11px",
                                          "fontFamily": THEME["font_mono"]}),
            html.Td(ops_str, style={"color": primary_color, "fontSize": "11px",
                                     "fontFamily": THEME["font_mono"]}),
            html.Td(primary_text, style={"color": primary_color, "fontSize": "9px",
                                         "fontFamily": THEME["font_mono"],
                                         "textAlign": "center", "fontWeight": "600"}),
            html.Td(comp_text, style={"color": comp_color, "fontSize": "9px",
                                      "fontFamily": THEME["font_mono"],
                                      "textAlign": "center",
                                      "fontWeight": "600" if comp == "MISMATCH" else "400"}),
        ]))

    # Timing invariant row
    ti = vm.timing_invariant
    ti_icon = "✅" if ti.status == "PASS" else "❌"
    ti_color = THEME["green"] if ti.status == "PASS" else THEME["red"]
    ti_ops = f"checked={ti.checked_cycles} violations={ti.violations}"
    ti_detail = ""
    if ti.violating_sequences:
        ti_detail = f"seq: {','.join(str(s) for s in ti.violating_sequences[:10])}"

    timing_row = html.Tr([
        html.Td(ti_icon, style={"width": "30px", "textAlign": "center"}),
        html.Td("TIMING_INVARIANT", style={"color": THEME["text"], "fontWeight": "500",
                                           "fontFamily": THEME["font_mono"], "fontSize": "11px"}),
        html.Td("cycle_ms >= http_ms ∀ cycles", style={"color": THEME["text_dim"], "fontSize": "11px",
                                                        "fontFamily": THEME["font_mono"]}),
        html.Td(ti_ops, style={"color": ti_color, "fontSize": "11px",
                               "fontFamily": THEME["font_mono"]}),
        html.Td(ti.status, style={"color": ti_color, "fontSize": "9px",
                                  "fontFamily": THEME["font_mono"], "textAlign": "center",
                                  "fontWeight": "600"}),
        html.Td(ti_detail, style={"color": THEME["red"] if ti.violations else THEME["text_dim"],
                                  "fontSize": "9px", "fontFamily": THEME["font_mono"]}),
    ])

    return html.Div([
        html.Div("📐 审计方程 (独立复算) + 计时不变量", style={
            "fontWeight": "600", "fontSize": "13px", "color": THEME["text"],
            "marginBottom": "10px", "paddingBottom": "8px",
            "borderBottom": f"1px solid {THEME['border']}"}),
        html.Table([
            html.Thead(html.Tr([
                html.Th("", style={"width": "30px"}),
                html.Th("名称", style={"textAlign": "left", "color": THEME["text_dim"], "fontSize": "10px"}),
                html.Th("方程", style={"textAlign": "left", "color": THEME["text_dim"], "fontSize": "10px"}),
                html.Th("操作数", style={"textAlign": "left", "color": THEME["text_dim"], "fontSize": "10px"}),
                html.Th("主状态", style={"textAlign": "center", "color": THEME["text_dim"], "fontSize": "10px", "width": "45px"}),
                html.Th("比较", style={"textAlign": "center", "color": THEME["text_dim"], "fontSize": "10px", "width": "75px"}),
            ])),
            html.Tbody(rows + [timing_row]),
        ], style={"width": "100%", "borderCollapse": "collapse"}),
    ], style=CARD_STYLE)


# ══════════════════════════════════════════════════════════════════════════
# 延迟与失败
# ══════════════════════════════════════════════════════════════════════════

def _render_latency_failures(vm: B2DashboardViewModel) -> html.Div:
    # Latency bar chart
    latency_fig = go.Figure(data=[
        go.Bar(name="P50", x=["HTTP", "Cycle", "Sched.Delay"],
               y=[vm.http_p50_ms, vm.cycle_p50_ms, vm.schedule_delay_p50_ms],
               marker_color=THEME["blue"]),
        go.Bar(name="P95", x=["HTTP", "Cycle", "Sched.Delay"],
               y=[vm.http_p95_ms, vm.cycle_p95_ms, 0],
               marker_color=THEME["yellow"]),
        go.Bar(name="MAX", x=["HTTP", "Cycle", "Sched.Delay"],
               y=[vm.http_max_ms, vm.cycle_max_ms, 0],
               marker_color=THEME["red"]),
    ])
    latency_fig.update_layout(
        barmode="group",
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=20, r=20, t=30, b=10),
        height=200,
        font=dict(color=THEME["text_dim"], size=10),
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
    )

    # Failure type breakdown
    failure_rows = []
    failure_attrs = [
        ("fetch_failed", vm.fetch_failed), ("http_failed", vm.http_failed),
        ("parse_failed", vm.parse_failed), ("validation_failed", vm.validation_failed),
        ("normalization_failed", vm.normalization_failed),
        ("quarantine_failed", vm.quarantine_failed),
        ("event_failed", vm.event_failed), ("signal_failed", vm.signal_failed),
        ("ledger_failed", vm.ledger_failed_count), ("report_failed", vm.report_failed),
        ("scheduler_failed", vm.scheduler_failed),
        ("session_check_failed", vm.session_check_failed),
        ("safety_guard_failed", vm.safety_guard_failed),
    ]
    for name, count in failure_attrs:
        if hasattr(vm, name):
            color = THEME["red"] if getattr(vm, name, 0) > 0 else THEME["text_dim"]
            failure_rows.append(html.Tr([
                html.Td(name.replace("_failed", "").replace("_", " "),
                        style={"color": THEME["text_dim"], "fontSize": "11px",
                               "fontFamily": THEME["font_mono"]}),
                html.Td(str(count), style={"color": color, "fontSize": "11px",
                                           "fontWeight": "600" if count > 0 else "400",
                                           "fontFamily": THEME["font_mono"],
                                           "textAlign": "right"}),
            ]))

    return html.Div([
        html.Div([
            html.Div([
                html.Div("⏱ 延迟 (ms)", style={"fontWeight": "600", "fontSize": "13px",
                                               "color": THEME["text"], "marginBottom": "8px"}),
                dcc.Graph(figure=latency_fig, config={"displayModeBar": False}),
            ], style={"flex": "2", "minWidth": "350px"}),
            html.Div([
                html.Div(f"⚠️ 失败明细 (total={vm.total_failures})", style={
                    "fontWeight": "600", "fontSize": "13px", "color": THEME["text"],
                    "marginBottom": "8px"}),
                html.Table([
                    html.Tbody(failure_rows),
                ], style={"width": "100%", "borderCollapse": "collapse"}),
            ], style={"flex": "1", "minWidth": "250px"}),
        ], style={"display": "flex", "gap": "12px", "flexWrap": "wrap"}),
    ], style=CARD_STYLE)


# ══════════════════════════════════════════════════════════════════════════
# 周期明细表
# ══════════════════════════════════════════════════════════════════════════

def _render_cycle_table(vm: B2DashboardViewModel) -> html.Div:
    if not vm.cycle_records_summary:
        return html.Div()

    rows = []
    for cr in vm.cycle_records_summary:
        status_color = THEME["green"] if cr["status"] == "COMPLETED" else THEME["red"]
        failure_cell = cr.get("failure", "") or "-"
        rows.append(html.Tr([
            html.Td(str(cr["seq"]), style={"color": THEME["text_dim"], "fontFamily": THEME["font_mono"],
                                           "fontSize": "11px", "width": "40px"}),
            html.Td("●", style={"color": status_color, "width": "20px", "textAlign": "center"}),
            html.Td(f"{cr['http_ms']}ms", style={"color": THEME["text_dim"], "fontFamily": THEME["font_mono"],
                                                  "fontSize": "11px", "width": "70px"}),
            html.Td(f"{cr['cycle_ms']}ms", style={"color": THEME["text_dim"], "fontFamily": THEME["font_mono"],
                                                   "fontSize": "11px", "width": "70px"}),
            html.Td(str(cr["signals"]), style={"color": THEME["text_dim"], "fontFamily": THEME["font_mono"],
                                               "fontSize": "11px", "width": "50px", "textAlign": "right"}),
            html.Td(failure_cell, style={"color": THEME["red"] if failure_cell != "-" else THEME["text_dim"],
                                          "fontSize": "11px", "fontFamily": THEME["font_mono"]}),
        ]))

    return html.Div([
        html.Div(f"📋 周期明细 (显示前 {len(vm.cycle_records_summary)}/{vm.cycle_count} 条)", style={
            "fontWeight": "600", "fontSize": "13px", "color": THEME["text"],
            "marginBottom": "10px", "paddingBottom": "8px",
            "borderBottom": f"1px solid {THEME['border']}"}),
        html.Table([
            html.Thead(html.Tr([
                html.Th("#", style={"textAlign": "left", "color": THEME["text_dim"], "fontSize": "10px", "width": "40px"}),
                html.Th("", style={"width": "20px"}),
                html.Th("HTTP", style={"textAlign": "left", "color": THEME["text_dim"], "fontSize": "10px", "width": "70px"}),
                html.Th("Cycle", style={"textAlign": "left", "color": THEME["text_dim"], "fontSize": "10px", "width": "70px"}),
                html.Th("信号", style={"textAlign": "right", "color": THEME["text_dim"], "fontSize": "10px", "width": "50px"}),
                html.Th("失败", style={"textAlign": "left", "color": THEME["text_dim"], "fontSize": "10px"}),
            ])),
            html.Tbody(rows),
        ], style={"width": "100%", "borderCollapse": "collapse", "maxHeight": "400px", "overflowY": "auto"}),
    ], style=CARD_STYLE)


# ══════════════════════════════════════════════════════════════════════════
# v14: Signal Storm Alert
# ══════════════════════════════════════════════════════════════════════════

def _render_signal_storm_alert(vm: B2DashboardViewModel) -> html.Div:
    if not vm.signal_storm_detected:
        return html.Div()

    storm_rows = []
    for label, count in vm.signal_storm_per_symbol.items():
        storm_rows.append(html.Tr([
            html.Td(label, style={"color": THEME["text"], "fontFamily": THEME["font_mono"],
                                  "fontSize": "11px"}),
            html.Td(f"×{count}", style={"color": THEME["red"], "fontWeight": "600",
                                         "fontFamily": THEME["font_mono"], "fontSize": "11px",
                                         "textAlign": "right"}),
        ]))

    cooldown_label = "ENABLED" if vm.cooldown_enabled else "DISABLED"
    cooldown_color = THEME["green"] if vm.cooldown_enabled else THEME["red"]

    return html.Div([
        html.Div([
            html.Span("⚠️ ", style={"fontSize": "16px"}),
            html.Span("SIGNAL STORM DETECTED", style={
                "color": THEME["red"], "fontWeight": "700", "fontSize": "14px"}),
            html.Span(f"  ({vm.cross_event_repeated_recommendations} cross-event repeated)",
                      style={"color": THEME["text_dim"], "fontSize": "11px",
                             "fontFamily": THEME["font_mono"]}),
        ]),
        html.Div([
            html.Span(f"cooldown: {cooldown_label}", style={
                "color": cooldown_color, "fontSize": "11px",
                "fontFamily": THEME["font_mono"], "fontWeight": "600"}),
            html.Span(f"  skipped={vm.signals_skipped_cooldown}  policy={vm.cooldown_policy_version or 'none'}",
                      style={"color": THEME["text_dim"], "fontSize": "10px",
                             "fontFamily": THEME["font_mono"]}),
        ], style={"marginTop": "4px"}),
        html.Table([
            html.Tbody(storm_rows),
        ], style={"width": "100%", "maxWidth": "400px", "marginTop": "8px",
                  "borderCollapse": "collapse"}),
        html.Div([
            html.Span(f"historical reprocessing: {vm.historical_reprocessing_attempts}",
                      style={"color": THEME["yellow"], "fontSize": "10px",
                             "fontFamily": THEME["font_mono"]}),
            html.Span(" — lifecycle efficiency warning" if vm.historical_reprocessing_attempts > 1000 else "",
                      style={"color": THEME["text_dim"], "fontSize": "10px"}),
        ], style={"marginTop": "4px"}),
    ], style={**CARD_STYLE, "borderLeft": f"3px solid {THEME['red']}"})


# ══════════════════════════════════════════════════════════════════════════
# v14: Lineage Status
# ══════════════════════════════════════════════════════════════════════════

def _render_lineage_status(vm: B2DashboardViewModel) -> html.Div:
    status_color = {"FULL": THEME["green"], "PARTIAL": THEME["yellow"],
                    "MISSING": THEME["red"], "UNKNOWN": THEME["text_dim"]}.get(
        vm.lineage_status, THEME["text_dim"])

    field_rows = []
    for key, val in vm.lineage_fields_present.items():
        parts = val.split("/")
        has = int(parts[0]) if len(parts) == 2 else 0
        total = int(parts[1]) if len(parts) == 2 else 0
        color = THEME["green"] if has == total else THEME["red"] if has == 0 else THEME["yellow"]
        field_rows.append(html.Tr([
            html.Td(key, style={"color": THEME["text_dim"], "fontFamily": THEME["font_mono"],
                               "fontSize": "10px"}),
            html.Td(val, style={"color": color, "fontFamily": THEME["font_mono"],
                               "fontSize": "10px", "textAlign": "right", "fontWeight": "600"}),
        ]))

    missing_note = ""
    if vm.missing_lineage_fields:
        missing_note = f"missing: {', '.join(vm.missing_lineage_fields)}"

    return html.Div([
        html.Div([
            html.Span("🔗 Signal Lineage: ", style={"color": THEME["text"], "fontWeight": "600",
                                                     "fontSize": "12px"}),
            html.Span(vm.lineage_status, style={"color": status_color, "fontWeight": "700",
                                                 "fontSize": "12px"}),
            html.Span(f"  {missing_note}" if missing_note else "",
                      style={"color": THEME["yellow"], "fontSize": "10px",
                             "fontFamily": THEME["font_mono"]}),
            html.Span("  compat_mode=True" if vm.compatibility_mode else "",
                      style={"color": THEME["yellow"], "fontSize": "10px",
                             "fontFamily": THEME["font_mono"]}),
        ]),
        html.Table([html.Tbody(field_rows)],
                   style={"width": "100%", "maxWidth": "500px", "marginTop": "6px",
                          "borderCollapse": "collapse"}),
    ], style={**CARD_STYLE, "padding": "10px 16px"}) if field_rows else html.Div()


# ══════════════════════════════════════════════════════════════════════════
# 错误占位页
# ══════════════════════════════════════════════════════════════════════════

def _render_error_page(vm: B2DashboardViewModel) -> html.Div:
    status_map = {
        ReportStatus.NO_DATA: ("NO DATA", "报告文件不存在或未配置路径", THEME["yellow"]),
        ReportStatus.INVALID_JSON: ("INVALID JSON", "JSON 解析失败", THEME["red"]),
        ReportStatus.UNSUPPORTED_SCHEMA: ("UNSUPPORTED SCHEMA", "报告 schema 版本不支持", THEME["red"]),
        ReportStatus.STALE: ("STALE", "报告数据已过期", THEME["yellow"]),
        ReportStatus.ACCESS_DENIED: ("ACCESS DENIED", "路径访问被拒绝", THEME["red"]),
        ReportStatus.FILE_TOO_LARGE: ("FILE TOO LARGE", "报告文件超出大小限制", THEME["red"]),
    }
    label, detail, color = status_map.get(vm.report_status,
                                          ("UNKNOWN", str(vm.report_status), THEME["red"]))

    return html.Div([
        _render_banner(vm),
        html.Div([
            html.Div(label, style={
                "fontSize": "48px", "fontWeight": "700", "color": color,
                "textAlign": "center", "marginTop": "60px",
                "fontFamily": THEME["font_mono"],
            }),
            html.Div(detail, style={
                "fontSize": "14px", "color": THEME["text_dim"],
                "textAlign": "center", "marginTop": "12px",
            }),
            html.Div(vm.report_path, style={
                "fontSize": "11px", "color": THEME["text_dim"],
                "textAlign": "center", "marginTop": "8px",
                "fontFamily": THEME["font_mono"],
                "wordBreak": "break-all",
            }),
            html.Div([html.Div(w, style={"fontSize": "11px", "color": THEME["red"],
                                          "marginTop": "4px", "textAlign": "center"})
                      for w in vm.warnings], style={"marginTop": "16px"}),
        ]),
    ], style={"maxWidth": "1400px", "margin": "0 auto", "padding": "12px"})


# ══════════════════════════════════════════════════════════════════════════
# 工具
# ══════════════════════════════════════════════════════════════════════════

def _tag(text: str, color: str) -> html.Span:
    return html.Span(text, style={
        **TAG_STYLE,
        "background": f"{color}22",
        "color": color,
        "border": f"1px solid {color}33",
    })
