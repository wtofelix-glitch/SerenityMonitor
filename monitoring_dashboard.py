#!/usr/bin/env python3
"""
Serenity Monitor — 移动端监控看板
极简 Flask web 看板，手机一屏看完所
function loadExecutionPlan(){
  fetch("/api/execution-plan")
  .then(r=>r.json())
  .then(d=>{
    if(!d.ok) return;
    var card=document.getElementById("execution-plan-card");
    if(!card) return;
    if(d.already_executed){
      card.style.display="block";
      document.getElementById("exec-plan-content").innerHTML='<div style="padding:12px;text-align:center;color:#FF5252">✅ 今日计划已执行</div>';
      return;
    }
    var hasActions = (d.sells&&d.sells.length>0) || (d.buys&&d.buys.length>0);
    if(!hasActions) return;
    card.style.display="block";
    var h='';
    if(d.sells&&d.sells.length>0){
      h+='<div style="font-size:12px;color:#69F0AE;margin-bottom:4px">🟢 卖出</div>';
      d.sells.forEach(function(s){
        h+='<div style="display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-bottom:1px solid rgba(255,255,255,.05)">';
        h+='<div><span style="font-weight:600">'+s.name+'</span> <span style="font-size:10px;color:rgba(255,255,255,.4)">'+s.code+'</span>';
        h+='<div style="font-size:10px;color:rgba(255,255,255,.3)">'+s.shares+'股 ~¥'+(s.estimated_proceeds||0).toFixed(0)+' | '+(s.reasons||[]).join(", ")+'</div></div>';
        h+='<span style="font-size:11px;color:'+(s.profit_pct>=0?'#FF1744':'#00C853')+'">'+(s.profit_pct>=0?'+':'')+(s.profit_pct||0).toFixed(1)+'%</span>';
        h+='</div>';
      });
    }
    if(d.buys&&d.buys.length>0){
      h+='<div style="font-size:12px;color:#FF5252;margin:8px 0 4px">🔴 买入</div>';
      d.buys.forEach(function(b){
        h+='<div style="display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-bottom:1px solid rgba(255,255,255,.05)">';
        h+='<div><span style="font-weight:600">'+b.name+'</span> <span style="font-size:10px;color:rgba(255,255,255,.4)">'+b.code+'</span>';
        h+='<div style="font-size:10px;color:rgba(255,255,255,.3)">'+b.shares+'股 @'+b.price+' ≈¥'+(b.amount||0).toFixed(0)+' | 评分'+(b.score||0).toFixed(0)+'</div></div>';
        h+='<span style="font-size:10px;padding:2px 6px;border-radius:4px;background:rgba(255,23,68,.15);color:#FF5252">'+(b.signal||'BUY')+'</span>';
        h+='</div>';
      });
    }
    h+='<button onclick="confirmExecute()" style="width:100%;padding:10px;margin-top:10px;background:#E65100;color:#fff;border:none;border-radius:8px;font-size:14px;font-weight:600">⚡ 确认执行</button>';
    document.getElementById("exec-plan-content").innerHTML=h;
  });
}
function confirmExecute(){
  if(!confirm("确认执行当前计划？\n\n这将记录交易到本地数据库。\n实际下单请在同花顺手动完成。")) return;
  var btn=event.target;
  btn.disabled=true;
  btn.textContent="执行中...";
  fetch("/api/execute",{method:"POST"})
  .then(r=>r.json())
  .then(d=>{
    if(d.ok){
      btn.textContent="✅ 已执行";
      btn.style.background="#2E7D32";
      setTimeout(function(){loadExecutionPlan();},2000);
    }else{
      btn.textContent="❌ 失败";
      btn.style.background="#C62828";
      alert(d.msg);
      setTimeout(function(){loadExecutionPlan();},2000);
    }
  })
  .catch(function(e){
    btn.textContent="❌ 网络错误";
    btn.style.background="#C62828";
    setTimeout(function(){loadExecutionPlan();},2000);
  });
}
setTimeout(loadExecutionPlan,1000);
有数据。
端口 8401，毛玻璃风格，30秒自动刷新。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 防止 Hermes cron 执行 daemon 后发 SIGTERM 误杀看板进程
import signal
def _ignore_term(signum, frame):
    """Ignore SIGTERM — daemon 脚本会管理进程生命周期"""
    pass
signal.signal(signal.SIGTERM, _ignore_term)

import json
import time
from datetime import datetime, timedelta
from flask import Flask, jsonify, render_template, request

from serenity_logger import get_logger
from db import get_conn

log = get_logger(__name__)

# --- 项目模块 ---
from config import ALL_CODES, STOCK_MAP
from data_engine import fetch_realtime, sina_fetch_raw
from scorer import score_all
from factor_engine import get_current_signals, SIGNAL_FACTORS
from market_timing import get_market_signal
from market_sense import MarketSense
from sector_rotation import SectorRotationEngine
from rating_engine import get_rating
from dividend_engine import DividendEngine
from etf_momentum import ETFMomentumStrategy
from portfolio import PortfolioManager

app = Flask(__name__)

# 模块级缓存（避免每30秒重复跑引擎）
_cache = {"etf": None, "dividend": None, "pf": None, "scores": None, "sectors": None}
_cache_time = {"etf": None, "dividend": None, "pf": None, "scores": None, "sectors": None}
# 分级 TTL：ETF数据每日收盘后更新 → 30分钟，评分 → 2分钟，行业轮动 → 5分钟
CACHE_TTL = {
    "etf": timedelta(minutes=30),
    "dividend": timedelta(minutes=5),
    "pf": timedelta(minutes=5),
    "scores": timedelta(minutes=2),
    "sectors": timedelta(minutes=5),
}

# =============================================================
# API 数据组装
# =============================================================
FACTOR_LABELS = {
    "ksft": "KSFT(形态)",
    "rank_20": "Rank20",
    "rsv_20": "RSV20",
    "beta_20": "Beta20",
    "resi_20": "Resi20",
    "macd_signal": "MACD",
    "obv_trend": "OBV",
    "mfi_signal": "MFI",
    "cci_signal": "CCI",
    "wq_alpha1": "WQα#1",
    "wq_alpha3": "WQα#3",
    "wq_alpha5": "WQα#5",
    "wq_alpha15": "WQα#15",
    "wq_alpha19": "WQα#19",
}


def _lightweight_scores():
    """轻量评分：基于实时行情 + 持仓数据计算排行，不依赖 scorer.score_all"""
    import sys
    scores = []
    try:
        from data_engine import fetch_realtime
        realtime = fetch_realtime()
        rt_map = {r["code"]: r for r in realtime}

        for code, info in STOCK_MAP.items():
            rt = rt_map.get(code, {})
            price = rt.get("price", 0)
            change_pct = 0
            if rt.get("close_yesterday", 0) > 0:
                change_pct = (price - rt["close_yesterday"]) / rt["close_yesterday"] * 100

            # 简化的评分：基于涨跌幅映射 0-100
            base = 50
            momentum = max(-25, min(25, change_pct * 5))  # ±25 based on change
            total_score = base + momentum

            signal_action = "HOLD"
            if change_pct > 3:
                signal_action = "BUY"
            elif change_pct > 1:
                signal_action = "CAUTION_BUY"
            elif change_pct < -3:
                signal_action = "SELL"
            elif change_pct < -1:
                signal_action = "WATCH"

            scores.append({
                "code": code,
                "name": info.get("name", code),
                "total_score": round(total_score, 1),
                "signal_action": signal_action,
                "signal_confidence": round(abs(change_pct) / 10, 2),
                "rank": 0,  # will be filled below
                "price": price,
                "change_pct": round(change_pct, 2),
            })
    except Exception as e:
        sys.stderr.write(f"_lightweight_scores error: {e}\n")
        return []

    # Sort by total_score descending, assign ranks
    scores.sort(key=lambda s: s["total_score"], reverse=True)
    for i, s in enumerate(scores):
        s["rank"] = i + 1

    return scores
def gather_monitor_data():
    """收集看板所需全部数据（分级缓存）"""
    now = datetime.now()
    today = datetime.now().strftime("%Y-%m-%d")

    # 1. 评分数据（2分钟缓存）
    if _cache["scores"] and _cache_time["scores"] and (now - _cache_time["scores"]) < CACHE_TTL["scores"]:
        scores = _cache["scores"]
    else:
        scores = score_all()
        _cache["scores"] = scores
        _cache_time["scores"] = now

    # 2. 14因子数据
    factor_raw = get_current_signals()
    factors = []
    for fr in factor_raw:
        signals = fr.get("factors", {}).get("signals", {})
        item = {"code": fr["code"], "name": fr["name"], "signal": fr.get("signal", 0)}
        for fn in SIGNAL_FACTORS:
            item[fn] = signals.get(fn, None)
        factors.append(item)

    # 3. 大盘择时
    market = get_market_signal()
    
    # 3.5 操作模式（均值回归/趋势跟踪）
    try:
        _ms_dash = MarketSense()
        operational_mode = _ms_dash.get_operational_mode()
    except Exception:
        operational_mode = {"mode": "neutral", "factor_invert": False, 
                           "sell_trigger_weight": 1.0, "buy_threshold_shift": 0,
                           "regime_label": "震荡市", "avg_20d_return": 0}

    # 4. 行业轮动（5分钟缓存）
    if _cache["sectors"] and _cache_time["sectors"] and (now - _cache_time["sectors"]) < CACHE_TTL["sectors"]:
        sectors = _cache["sectors"]
    else:
        sector_engine = SectorRotationEngine()
        sectors = sector_engine.get_sector_rank()
        _cache["sectors"] = sectors
        _cache_time["sectors"] = now

    # 5. 综合评级（所有标的）
    ratings = []
    for code in ALL_CODES:
        name = STOCK_MAP.get(code, {}).get("name", code)
        try:
            r = get_rating(code)
            ratings.append({
                "code": code,
                "name": name,
                "rating": r.get("rating", "N/A"),
                "rating_emoji": r.get("rating_emoji", "❓"),
                "score": r.get("score", 0),
                "signal_label": r.get("signal_label", "N/A"),
                "signal_emoji": r.get("signal_emoji", "⚪"),
            })
        except Exception:
            ratings.append({
                "code": code, "name": name, "rating": "N/A",
                "rating_emoji": "❓", "score": 0, "signal_label": "N/A", "signal_emoji": "⚪",
            })

    # 每日净值快照（异步保存，不影响响应）
    try:
        import json as _json
        pf = _get_portfolio_summary()
        conn = get_conn()
        conn.execute("""
            INSERT OR REPLACE INTO nav_history (date, total_value, cash, holdings_value, profit_pct, positions_json)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (today, pf["total_value"], pf["cash"], pf["holdings_value"], pf["total_profit_pct"], _json.dumps(pf.get("positions", []))))
        conn.commit()
        conn.close()
    except Exception:
        pass

    return {
        "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
        "date": today,
        "scores": scores,
        "factors": factors,
        "market": market,
        "sectors": sectors,
        "ratings": ratings,
        "signal_factors": SIGNAL_FACTORS,
        "factor_labels": FACTOR_LABELS,
        "etf_top5": _get_etf_top5(),
        "dividend_top5": _get_dividend_top5(),
        "portfolio_summary": _get_portfolio_summary(),
        "signal_brief": _build_signal_brief(scores, _get_portfolio_summary()),
        "target_tracker": _get_target_tracker(),
        "position_advice": _get_position_advice(scores),
        "stop_conditions": _get_stop_conditions(),
        "operational_mode": operational_mode,
    }


def _get_position_advice(scores):
    """仓位优化建议 — Kelly 仓位 + 加减仓信号"""
    try:
        from portfolio import PortfolioManager
        pm = PortfolioManager()
        pv = pm.get_portfolio_value()
        positions = pv.get("positions", [])
        score_map = {s["code"]: s for s in scores}

        # 获取持仓感知信号（解决看板直接引用原始BUY信号的问题）
        try:
            from signal_engine import get_position_signal
        except ImportError:
            get_position_signal = None

        advice = []
        for pos in positions:
            code = pos["code"]
            sig = score_map.get(code, {})
            score = sig.get("total_score", 50)
            raw_action = sig.get("signal_action", "HOLD")
            profit = pos.get("profit_pct", 0)

            # 使用持仓感知信号覆盖原始BUY信号
            action = raw_action
            if get_position_signal and raw_action in ("STRONG_BUY", "BUY", "CAUTION_BUY"):
                try:
                    final_signal = get_position_signal(score, profit, is_holding=True)
                    if final_signal in ("STRONG_HOLD", "HOLD"):
                        action = final_signal
                except Exception:
                    pass  # fallback to raw action

            # Kelly 仓位计算（跳过持仓数限制，已有持仓需要算Kelly）
            try:
                sizing = pm.calc_position_size(code, sig.get("signal_confidence", 0.5), skip_limit_check=True)
            except Exception:
                sizing = {}

            # 加减仓建议
            suggest = "HOLD"
            reason = ""
            if action in ("STRONG_BUY", "BUY") and profit < 5:
                suggest = "ADD"
                reason = f"信号强劲({score:.0f}分)+浮盈适中，可加仓"
            elif action in ("STRONG_BUY", "BUY") and profit >= 15:
                suggest = "TAKE_PARTIAL"
                reason = f"信号强劲但浮盈{profit:.1f}%，建议部分止盈"
            elif action in ("SELL", "STOP_LOSS"):
                suggest = "EXIT"
                reason = f"信号转空({score:.0f}分)，建议清仓"
            elif action in ("WATCH", "WEAK_HOLD"):
                if profit < -5:
                    suggest = "REDUCE"
                    reason = f"信号转弱+亏损{profit:.1f}%，建议减仓"
                else:
                    suggest = "WATCH"
                    reason = "信号转弱，密切关注"
            elif score < 48:
                suggest = "REDUCE"
                reason = f"评分偏低({score:.0f}分)，建议减仓"
            else:
                suggest = "HOLD"
                reason = f"评分{score:.0f}分，继续持有"

            advice.append({
                "code": code,
                "name": pos["name"],
                "score": score,
                "action": action,
                "suggest": suggest,
                "reason": reason,
                "profit_pct": round(profit, 2),
                "kelly_max_shares": sizing.get("shares", 0),
                "kelly_max_amount": sizing.get("amount", 0),
                "kelly_cash_pct": sizing.get("cash_used_pct", 0),
            })

        # 非持仓的买入候选
        held_codes = {p["code"] for p in positions}
        buy_candidates = []
        for s in scores:
            if s["code"] not in held_codes and s.get("signal_action") in ("BUY", "STRONG_BUY", "CAUTION_BUY"):
                try:
                    sizing = pm.calc_position_size(s["code"], s.get("signal_confidence", 0.5))
                    if sizing.get("shares", 0) > 0:
                        buy_candidates.append({
                            "code": s["code"],
                            "name": s.get("name", s["code"]),
                            "score": s.get("total_score", 0),
                            "action": s.get("signal_action"),
                            "suggested_shares": sizing.get("shares", 0),
                            "suggested_amount": sizing.get("amount", 0),
                        })
                except Exception:
                    pass
        buy_candidates.sort(key=lambda x: x["score"], reverse=True)

        return {
            "holdings_advice": advice,
            "buy_candidates": buy_candidates[:3],
            "cash": pv.get("cash", 0),
            "max_positions": pm.max_positions,
        }
    except Exception as e:
        return {"error": str(e)}

def _get_stop_conditions():
    """获取止盈止损触发状态"""
    try:
        from portfolio import PortfolioManager
        pm = PortfolioManager()
        actions = pm.check_stop_conditions()
        trailing = pm.get_trailing_stop_levels()
        # 合并：每个持仓的止盈止损状态
        result = []
        trail_map = {t["code"]: t for t in trailing}
        action_map = {}
        for a in actions:
            code = a["code"]
            if code not in action_map:
                action_map[code] = []
            action_map[code].append({
                "action": a.get("action", ""),
                "reason": a.get("reason", ""),
                "profit_pct": a.get("profit_pct", 0),
            })
        for t in trailing:
            code = t["code"]
            result.append({
                "code": code,
                "name": t.get("name", code),
                "current": t.get("current", 0),
                "entry": t.get("entry", 0),
                "peak": t.get("peak", 0),
                "profit_pct": round(t.get("profit_pct", 0), 1),
                "peak_profit_pct": round(t.get("peak_profit_pct", 0), 1),
                "drawdown_from_peak": round(t.get("drawdown_from_peak", 0), 1),
                "trailing_triggered": t.get("trailing_triggered", False),
                "exceeds_profit_take1": t.get("exceeds_profit_take1", False),
                "exceeds_profit_take2": t.get("exceeds_profit_take2", False),
                "actions": action_map.get(code, []),
            })
        return result
    except Exception:
        return []

def _get_target_tracker():
    """目标追踪：5.1万 → 10.2万 / 3个月"""
    try:
        from portfolio import PortfolioManager
        pm = PortfolioManager()
        return pm.get_target_tracker()
    except Exception:
        return {}

def _build_signal_brief(scores, pf_summary):
    """从评分+持仓中提取可执行信号简报"""
    held_codes = set()
    if pf_summary and "position_details" in pf_summary:
        held_codes = {p["code"] for p in pf_summary["position_details"]}

    buy_candidates = []
    risk_alerts = []

    for s in scores:
        code = s["code"]
        action = s.get("signal_action", "HOLD")
        score = s["total_score"]

        # 买入候选（非持仓 + 高分 + 买入信号）
        if code not in held_codes:
            if score >= 60 and action in ("BUY", "CAUTION_BUY", "STRONG_BUY"):
                buy_candidates.append({
                    "code": code, "name": s["name"],
                    "score": score, "action": action,
                    "confidence": s.get("signal_confidence", 0),
                })
        # 风险提醒（持仓 + 低分或卖出信号）
        else:
            if action in ("SELL", "STOP_LOSS") or score < 50:
                risk_alerts.append({
                    "code": code, "name": s["name"],
                    "score": score, "action": action,
                })

    return {
        "buy_count": len(buy_candidates),
        "buy_candidates": buy_candidates[:3],
        "risk_count": len(risk_alerts),
        "risk_alerts": risk_alerts,
    }


def _get_etf_top5():
    """ETF 动量轮动 Top 5（30分钟缓存）"""
    now = datetime.now()
    if _cache["etf"] and _cache_time["etf"] and (now - _cache_time["etf"]) < CACHE_TTL["etf"]:
        return _cache["etf"]
    try:
        ems = ETFMomentumStrategy()
        ranks = ems.rank_all()
        _cache["etf"] = ranks[:5]
        _cache_time["etf"] = now
        return _cache["etf"]
    except Exception:
        return _cache["etf"] or []


def _get_dividend_top5():
    """红利低波 Top 5（5分钟缓存）"""
    now = datetime.now()
    if _cache["dividend"] and _cache_time["dividend"] and (now - _cache_time["dividend"]) < CACHE_TTL["dividend"]:
        return _cache["dividend"]
    try:
        de = DividendEngine()
        results = de.score_all()
        _cache["dividend"] = results[:5]
        _cache_time["dividend"] = now
        return _cache["dividend"]
    except Exception:
        return _cache["dividend"] or []


def _get_portfolio_summary():
    """组合摘要 + 真实盈亏（5分钟缓存，使用 PortfolioManager）"""
    now = datetime.now()
    if _cache["pf"] and _cache_time["pf"] and (now - _cache_time["pf"]) < CACHE_TTL["pf"]:
        return _cache["pf"]
    try:
        pm = PortfolioManager()
        pf_data = pm.get_portfolio_value()
        result = {
            "positions": pf_data["position_count"],
            "total_value": round(pf_data["total_value"], 0),
            "cash": round(pf_data["cash"], 0),
            "holdings_value": round(pf_data["holdings_value"], 0),
            "total_profit_pct": pf_data["total_profit_pct"],
            "total_profit_amount": round(pf_data["total_profit_amount"], 0),
            "position_details": pf_data["positions"],  # 每只持仓的真实盈亏
        }
        _cache["pf"] = result
        _cache_time["pf"] = now
        return result
    except Exception:
        return _cache["pf"] or {"positions": 0, "total_value": 0}


@app.route("/")
def index():
    """root → ngrok 隧道入口，重定向到移动端看板"""
    from flask import redirect
    return redirect("/monitor")


@app.route("/api/monitor-data")
def api_monitor_data():
    API_CALLS.labels(source="dashboard_api").inc()
    try:
        data = gather_monitor_data()
        return jsonify({"ok": True, "data": data})
    except Exception as e:
        API_ERRORS.labels(source="dashboard_api").inc()
        SCORE_ERRORS.labels(module="dashboard").inc()
        log.error("API monitor-data failed: %s", e, exc_info=True)
        return jsonify({"ok": False, "error": str(e)})


# =============================================================
# 主页面
# =============================================================
# HTML 模板已迁移到 templates/monitor.html
# 使用 static/css/monitor.css + static/js/monitor.js
# 看板 UI 已重构为 Bloomberg/TradingView 风格
# 旧内联 HTML 保留在 git 历史中（commit 前）


# ===== 信号历史 API（仪表盘） =====
@app.route("/api/signal-history")
def api_signal_history():
    """返回近 7 天买入信号及其绩效"""
    from db import get_conn
    conn = get_conn()
    rows = conn.execute("""
        SELECT code, date, time, action, total_score, price,
               outcome_1d, outcome_3d, outcome_5d, outcome_10d
        FROM signal_log
        WHERE date >= date('now', '-7 days')
          AND action IN ('BUY','CAUTION_BUY','STRONG_BUY')
        ORDER BY date DESC, time DESC
        LIMIT 20
    """).fetchall()
    conn.close()
    result = []
    for r in rows:
        result.append({
            "code": r["code"],
            "name": STOCK_MAP.get(r["code"], {}).get("name", r["code"]),
            "date": r["date"],
            "time": r["time"],
            "action": r["action"],
            "score": r["total_score"],
            "price": r["price"],
            "outcome_1d": r["outcome_1d"],
            "outcome_3d": r["outcome_3d"],
        })
    return jsonify({"ok": True, "data": result})


@app.route("/api/signal-performance")
def api_signal_performance():
    """返回信号绩效与维度有效性分析数据"""
    try:
        from signal_performance import get_performance_report
        report = get_performance_report()
        summary = report["summary"]
        signal_by_action = report["signal_by_action"]
        dimensions = report["dimensions"]
        return jsonify({
            "ok": True,
            "updated": datetime.now().isoformat(),
            "summary": {
                "total_signals": summary["total_signals"],
                "with_outcome": summary["signals_with_outcome"],
                "overall_win_rate": summary["overall_win_rate_1d"],
                "overall_avg_return": summary["overall_avg_return_1d"],
                "best_action": summary["best_action"],
                "best_action_win_rate": summary["best_action_win_rate"],
                "best_dimension": summary["best_dimension"],
                "best_dimension_corr": summary["best_dimension_corr"],
            },
            "signal_actions": [
                {
                    "action": s["action"],
                    "total": s["total"],
                    "avg_return_1d": s["avg_return_1d"],
                    "avg_return_3d": s["avg_return_3d"],
                    "win_rate_1d": s["win_rate_1d"],
                    "win_rate_3d": s["win_rate_3d"],
                }
                for s in signal_by_action
            ],
            "dimensions": [
                {
                    "dimension": d["dimension"],
                    "samples": d["samples"],
                    "positive_pct": d["positive_pct"],
                    "rank_corr_1d": d["rank_corr_1d"],
                    "bins": d["bins"],
                }
                for d in dimensions
            ],
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ===== Prometheus 指标 =====
from metrics import metrics_endpoint, API_CALLS, API_ERRORS, SCORE_ERRORS, SIGNAL_ACTIONS

@app.route("/metrics")
def api_metrics():
    """Prometheus 指标端点"""
    return metrics_endpoint()


# ===== 净值历史 API（Canvas 图表） =====
@app.route("/api/nav-history")
def api_nav_history():
    """返回净值历史，用于前端 Canvas 绘制"""
    from db import get_conn
    conn = get_conn()
    rows = conn.execute("""
        SELECT date, total_value, profit_pct
        FROM nav_history
        ORDER BY date ASC
    """).fetchall()
    conn.close()
    result = []
    for r in rows:
        result.append({
            "date": r["date"],
            "value": round(r["total_value"], 2) if r["total_value"] else None,
            "profit_pct": round(r["profit_pct"], 2) if r["profit_pct"] is not None else None,
        })
    return jsonify({"ok": True, "data": result})
@app.route('/api/factor-ic')
def api_factor_ic():
    """因子 IC 归因 — 各评分维度的 Rank IC"""
    from factor_ic import compute_rank_ic, DIMENSION_LABELS
    try:
        result = compute_rank_ic(days=30, window=20)
        if 'error' in result:
            return jsonify({'ok': False, 'error': result['error']}), 500

        # Build summary list sorted by absolute IC
        dims = list(result.get('latest', {}).keys())
        summary = []
        for dim in dims:
            summary.append({
                'dimension': dim,
                'label': DIMENSION_LABELS.get(dim, dim),
                'latest_ic': result['latest'].get(dim, 0),
                'mean_ic': result['mean_ic'].get(dim, 0),
                'ic_ir': result['ic_ir'].get(dim, 0),
                'win_rate': result['win_rate'].get(dim, 0),
                'n_days': result['n_days'].get(dim, 0),
            })
        summary.sort(key=lambda x: abs(x['latest_ic']), reverse=True)

        # Top / weak factors
        rankings = result.get('rankings', {})
        top_factors = []
        for dim, ic_val in rankings.get('best', []):
            top_factors.append({
                'dimension': dim,
                'label': DIMENSION_LABELS.get(dim, dim),
                'ic': ic_val,
            })
        weak_factors = []
        for dim, ic_val in rankings.get('worst', []):
            weak_factors.append({
                'dimension': dim,
                'label': DIMENSION_LABELS.get(dim, dim),
                'ic': ic_val,
            })

        # Trend data (latest 20 days IC sequence for top 3 dimensions)
        all_ics = result.get('all_ics', {})
        trend = []
        for dim in dims[:3]:
            trend.append({
                'dimension': dim,
                'label': DIMENSION_LABELS.get(dim, dim),
                'values': all_ics.get(dim, [])[-20:],
            })

        return jsonify({
            'ok': True,
            'updated': datetime.now().isoformat(),
            'window': 20,
            'days': 30,
            'ic_summary': summary,
            'ic_trend': trend,
            'top_factors': top_factors[:3],
            'weak_factors': weak_factors[-3:] if weak_factors else [],
        })
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


# ===== 大师智慧 API =====
@app.route("/api/guru")
def api_guru():
    """返回大师智慧看板数据"""
    try:
        from guru_wisdom import status as guru_status
        from guru_wisdom import get_recent_quotes, get_db
        stats = guru_status()
        recent = get_recent_quotes(5)
        gurus = get_db().execute(
            "SELECT id, cn_name FROM gurus WHERE active=1 ORDER BY category, name"
        ).fetchall()

        quotes = []
        for q in recent:
            quotes.append({
                "guru": q.get("cn_name", ""),
                "guru_id": q.get("guru_id", ""),
                "content": q.get("content", "")[:120],
                "sentiment": q.get("sentiment", "neutral"),
                "topic": q.get("topic", ""),
                "collected_at": q.get("collected_at", ""),
                "source": q.get("source", ""),
            })

        sd = stats["sentiment_distribution"]
        total = max(sd["bullish"] + sd["bearish"] + sd["neutral"], 1)
        guru_list = [{"id": r["id"], "name": r["cn_name"], "influence": 5} for r in gurus]

        return jsonify({
            "ok": True,
            "stats": {
                "gurus": stats["gurus"],
                "total_quotes": stats["total_quotes"],
                "recent_7d": stats["recent_quotes_7d"],
                "last_collection": stats["last_collection"],
                "bullish_pct": round(sd["bullish"] / total * 100),
                "bearish_pct": round(sd["bearish"] / total * 100),
                "neutral_pct": round(sd["neutral"] / total * 100),
                "bullish": sd["bullish"], "bearish": sd["bearish"], "neutral": sd["neutral"],
            },
            "gurus": guru_list,
            "recent_quotes": quotes,
        })
    except Exception as e:
        log.error("guru API failed: %s", e, exc_info=True)
        return jsonify({"ok": False, "error": str(e)})


@app.route("/monitor")
def monitor_page():
    return render_template("monitor.html")


# ===== 调仓 API =====
@app.route("/api/trades", methods=["POST"])
def api_trades():
    from flask import request
    from db import add_trade, set_active, clear_active
    from datetime import datetime
    data = request.get_json()
    code = data.get("code", "")
    action = data.get("action", "buy")
    price = float(data.get("price", 0))
    qty = int(data.get("quantity", 0))
    note = data.get("note", "")
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        add_trade(code, action, price, qty, today, note)
        if action == "buy":
            set_active(code, price, today)
        elif action == "sell":
            clear_active(code)
        return jsonify({"ok": True, "msg": f"{'买入' if action=='buy' else '卖出'} {code} {price}元 × {qty}股 已记录"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 400


# ===== 设置 API =====
@app.route("/api/config", methods=["POST"])
def api_config():
    from flask import request
    from db import upsert_stock, get_stock
    data = request.get_json()
    code = data.get("code", "")
    stock = get_stock(code)
    if not stock:
        return jsonify({"ok": False, "msg": f"找不到 {code}"}), 404
    if "stop_loss" in data:
        stock["stop_loss"] = data["stop_loss"]
    if "target_high" in data:
        stock["target_high"] = data["target_high"]
    if "target_low" in data:
        stock["target_low"] = data["target_low"]
    try:
        upsert_stock(stock)
        return jsonify({"ok": True, "msg": f"{code} 设置已保存"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 400


# ===== 交易日志 API =====
@app.route("/api/journal")
def api_journal():
    """返回近期交易日志和统计"""
    try:
        from trading_journal import get_journal, get_stats
        from config import STOCK_MAP
        entries = get_journal(limit=10)
        stats = get_stats()
        # Attach name to each entry
        for e in entries:
            e["name"] = STOCK_MAP.get(e["code"], {}).get("name", e["code"])
        return jsonify({"ok": True, "entries": entries, "stats": stats})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ===== 执行计划 API =====
@app.route("/api/execution-plan")
def api_execution_plan():
    """返回当前自动执行计划"""
    try:
        from auto_execute import generate_execution_plan
        plan = generate_execution_plan(dry_run=True)
        # 移除 summary（太长），保留结构化数据
        return jsonify({
            "ok": True,
            "date": plan["date"],
            "cash": plan["cash"],
            "total_value": plan["total_value"],
            "sells": plan["sells"],
            "buys": plan["buys"],
            "swaps": plan.get("swaps", []),
            "already_executed": _plan_already_executed(plan),
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/execute", methods=["POST"])
def api_execute():
    """执行当前交易计划（仅记录到本地DB，不在券商下单）"""
    try:
        from auto_execute import generate_execution_plan
        from db import set_active, clear_active, add_trade, get_conn
        from config import STOCK_DETAILS
        from backtest_engine import recommend_atr_params
        from datetime import date

        plan = generate_execution_plan(dry_run=True)
        if _plan_already_executed(plan):
            return jsonify({"ok": True, "msg": "今日计划已执行，跳过", "already_executed": True})

        if not plan["sells"] and not plan["buys"]:
            return jsonify({"ok": True, "msg": "无待执行操作", "executed": []})

        today = date.today().isoformat()
        executed = []

        for s in plan["sells"]:
            # 安全闸：只卖出活跃持仓中的标的
            stock = get_conn().execute("SELECT is_active FROM stocks WHERE code = ?", (s["code"],)).fetchone()
            if not stock or not stock["is_active"]:
                log.warning(f"跳过卖出 {s['code']}: 非活跃持仓")
                continue
            price = s["estimated_proceeds"] / max(s["shares"], 1)
            clear_active(s["code"])
            reason = " ".join(s.get("reasons", []))[:200]
            add_trade(s["code"], "sell", price, s["shares"], today, f"auto: {reason}")
            executed.append(f"卖出 {s['name']}({s['code']}) {s['shares']}股 @{price:.2f}")

        for b in plan["buys"]:
            is_topup = b.get("action") == "TOPUP"
            price = b["price"]
            try:
                atr_rec = recommend_atr_params(b["code"])
                stop_pct = atr_rec.get("suggested_stop_pct", 8.0)
            except Exception:
                stop_pct = 8.0
            stop_price = round(price * (1 - stop_pct / 100), 2)

            target = STOCK_DETAILS.get(b["code"], {})
            target_high = target.get("target_sell", 0)
            target_low = target.get("buy_zone_low", 0)

            if is_topup:
                # 加仓：累加 trade_amount，不重置已有持仓
                try:
                    conn = get_conn()
                    existing = conn.execute("SELECT trade_amount FROM stocks WHERE code=?", (b["code"],)).fetchone()
                    old_amount = float(existing["trade_amount"] or 0) if existing else 0
                    new_amount = old_amount + (b["amount"] or 0)
                    conn.execute("UPDATE stocks SET stop_loss = ?, trade_amount = ?, notes = ? WHERE code = ?",
                               (stop_price, new_amount, f"加仓+{b['shares']}股", b["code"]))
                    conn.commit()
                    conn.close()
                except Exception:
                    pass
            else:
                set_active(b["code"], price, today, target_high, target_low)
                try:
                    conn = get_conn()
                    conn.execute("UPDATE stocks SET stop_loss = ?, trade_amount = ?, notes = ? WHERE code = ?",
                               (stop_price, b["amount"], f"auto买入{b['shares']}股", b["code"]))
                    conn.commit()
                    conn.close()
                except Exception:
                    pass

            add_trade(b["code"], "buy", price, b["shares"], today,
                      f"auto: score={b.get('score',0):.0f} {b.get('signal','')}", trade_amount=b["amount"])
            executed.append(f"买入 {b['name']}({b['code']}) {b['shares']}股 @{price:.2f} 止损{stop_price}")

        return jsonify({"ok": True, "msg": "执行完成", "executed": executed, "already_executed": False})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "msg": str(e)}), 500


def _plan_already_executed(plan):
    """检查计划是否已在今天执行过"""
    try:
        from db import get_conn
        from datetime import date
        today = date.today().isoformat()
        conn = get_conn()
        trade_codes = set()
        for r in conn.execute("SELECT code, action FROM trades WHERE date = ?", (today,)).fetchall():
            trade_codes.add(f"{r['code']}:{r['action']}")
        conn.close()

        plan_codes = set()
        for s in plan.get("sells", []):
            plan_codes.add(f"{s['code']}:sell")
        for b in plan.get("buys", []):
            plan_codes.add(f"{b['code']}:buy")

        if not plan_codes:
            return True  # 无计划 = 视为已执行
        return plan_codes.issubset(trade_codes)
    except Exception:
        return False


# ===== 异常事件 API =====
@app.route("/api/anomalies")
def api_anomalies():
    """返回未确认的异常事件（含价格异动/信号突变/因子突变）"""
    try:
        from db import get_unacknowledged_anomalies
        raw = get_unacknowledged_anomalies(limit=10)
        anomalies = []
        for r in raw:
            anomalies.append({
                "id": r["id"],
                "code": r["code"],
                "name": r.get("name") or STOCK_MAP.get(r["code"], {}).get("name", r["code"]),
                "level": r["level"],
                "type": r["alert_type"],
                "price": r["price"],
                "message": r["message"][:200],
                "created_at": r["created_at"],
            })
        emerg = [a for a in anomalies if a["level"] == "A"]
        warnings = [a for a in anomalies if a["level"] == "B"]
        info = [a for a in anomalies if a["level"] == "C"]
        return jsonify({
            "ok": True,
            "count": len(anomalies),
            "emergency_count": len(emerg),
            "warning_count": len(warnings),
            "info_count": len(info),
            "anomalies": anomalies[:5],
            "emergencies": emerg[:3],
        })
    except Exception as e:
        log.error("anomalies API failed: %s", e, exc_info=True)
        return jsonify({"ok": False, "error": str(e)})

# ===== 今日策略增强 API（聚合大师+异动+信号） =====
@app.route("/api/today-strategy")
def api_today_strategy():
    """返回今日策略增强数据：大师情绪摘要 + 最强买入信号 + 异动摘要"""
    try:
        from guru_wisdom import status as guru_status
        from guru_wisdom import get_recent_quotes
        stats = guru_status()
        sd = stats["sentiment_distribution"]
        total = max(sd["bullish"] + sd["bearish"] + sd["neutral"], 1)
        recent_quotes = get_recent_quotes(3)
        guru_summary = {
            "bullish_pct": round(sd["bullish"] / total * 100),
            "bearish_pct": round(sd["bearish"] / total * 100),
            "gurus": stats["gurus"],
            "total_quotes": stats["total_quotes"],
            "recent_7d": stats["recent_quotes_7d"],
            "latest_quotes": [
                {"guru": q.get("cn_name", ""), "content": q.get("content", "")[:80],
                 "sentiment": q.get("sentiment", "neutral")}
                for q in recent_quotes[:3]
            ],
        }
        signals = get_current_signals()
        conviction = []
        for s in sorted(signals, key=lambda x: x.get("signal", 0), reverse=True)[:5]:
            code = s["code"]
            name = STOCK_MAP.get(code, {}).get("name", code)
            conviction.append({
                "code": code,
                "name": name,
                "signal": s.get("signal", 0),
                "rank": s.get("rank", 0),
                "score": s.get("score", 0),
            })
        conviction = [c for c in conviction if c["signal"] > 55][:3]
        from db import get_unacknowledged_anomalies
        raw_anomalies = get_unacknowledged_anomalies(limit=5)
        anomaly_summary = {
            "total": len(raw_anomalies),
            "emergency": len([a for a in raw_anomalies if a["level"] == "A"]),
            "recent": [
                {"name": STOCK_MAP.get(a["code"], {}).get("name", a["code"]),
                 "code": a["code"],
                 "level": a["level"],
                 "type": a["alert_type"],
                 "message": a["message"][:100],
                 "created_at": a["created_at"]}
                for a in raw_anomalies[:3]
            ],
        }
        return jsonify({
            "ok": True,
            "data": {
                "guru": guru_summary,
                "conviction": conviction,
                "anomalies": anomaly_summary,
            }
        })
    except Exception as e:
        log.error("today-strategy API failed: %s", e, exc_info=True)
        return jsonify({"ok": False, "error": str(e), "data": {
            "guru": {"bullish_pct": 0, "bearish_pct": 0, "gurus": 0, "total_quotes": 0, "recent_7d": 0, "latest_quotes": []},
            "conviction": [],
            "anomalies": {"total": 0, "emergency": 0, "recent": []},
        }})


# ===== 快捷查询 API（手机书签一键直达） =====

@app.route("/api/nl-query")
def api_nl_query():
    """自然语言查询 — 中文意图识别，调用现有分析函数"""
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"ok": False, "error": "请提供参数 ?q=你的问题"})

    try:
        from db import get_conn, get_unacknowledged_anomalies
        from auto_execute import generate_execution_plan
        conn = get_conn()

        intent = _detect_intent(q)

        # ── 卖出/该卖/止盈止损 ──
        if intent == "sell":
            plan = generate_execution_plan(dry_run=True)
            sells = [{
                "code": s["code"], "name": s["name"],
                "reason": s.get("reasons", [])[:3],
                "pnl": s.get("pnl_pct", 0),
            } for s in plan.get("sells", [])]
            return jsonify({
                "ok": True, "intent": "sell",
                "answer": f"今日卖出候选 {len(sells)} 只" if sells else "今日无卖出计划",
                "sells": sells,
            })

        # ── 买入/该买/机会 ──
        elif intent == "buy":
            plan = generate_execution_plan(dry_run=True)
            buys = [{
                "code": b["code"], "name": b["name"],
                "score": b.get("score", 0), "reason": b.get("reason", ""),
            } for b in plan.get("buys", [])]
            return jsonify({
                "ok": True, "intent": "buy",
                "answer": f"买入候选 {len(buys)} 只" if buys else "今日无买入候选",
                "buys": sorted(buys, key=lambda x: x["score"], reverse=True),
            })

        # ── 盈亏/赚赔/收益 ──
        elif intent == "pnl":
            rows = conn.execute("""
                SELECT s.code, s.name, s.buy_price,
                       d.close as current_price,
                       ROUND((d.close - s.buy_price) / s.buy_price * 100, 2) as pnl_pct
                FROM stocks s
                LEFT JOIN (SELECT code, close FROM daily_snapshots
                           WHERE date = (SELECT MAX(date) FROM daily_snapshots)) d
                  ON s.code = d.code
                WHERE s.is_active = 1 AND s.buy_price > 0
            """).fetchall()
            positions = [{
                "code": r["code"], "name": r["name"],
                "pnl_pct": round(r["pnl_pct"], 1) if r["pnl_pct"] else 0,
            } for r in rows]
            total = sum(p["pnl_pct"] for p in positions)
            return jsonify({
                "ok": True, "intent": "pnl",
                "answer": f"总盈亏 {total:+.1f}%，持仓 {len(positions)} 只",
                "total_pnl_pct": round(total, 1),
                "positions": positions,
            })

        # ── 预警/风险/异常 ──
        elif intent == "alert":
            raw = get_unacknowledged_anomalies(limit=10)
            alerts = [{
                "code": a["code"],
                "name": STOCK_MAP.get(a["code"], {}).get("name", a["code"]),
                "level": a["level"], "msg": a["message"][:120],
            } for a in raw]
            emergency = len([a for a in raw if a["level"] == "A"])
            return jsonify({
                "ok": True, "intent": "alert",
                "answer": f"{emergency}条紧急，{len(alerts)}条预警" if emergency else f"{len(alerts)}条预警",
                "emergency": emergency, "alerts": alerts,
            })

        # ── 汇总/状态/怎么样 ──
        else:
            plan = generate_execution_plan(dry_run=True)
            raw = get_unacknowledged_anomalies(limit=3)
            return jsonify({
                "ok": True, "intent": "summary",
                "answer": f"持仓{_count_positions(conn)}只，买入候选{len(plan.get('buys',[]))}只，"
                          f"预警{len(raw)}条",
                "details": {
                    "positions": _count_positions(conn),
                    "buy_candidates": len(plan.get("buys", [])),
                    "sell_candidates": len(plan.get("sells", [])),
                    "alerts": len(raw),
                },
            })

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


def _detect_intent(q: str) -> str:
    """中文意图识别"""
    ql = q.lower()
    # 卖出/止盈/止损/该卖
    if any(w in q for w in ("卖", "止盈", "止损", "清仓", "减仓", "脱手", "该跑")):
        return "sell"
    # 买入/机会/该买/加仓
    if any(w in q for w in ("买", "机会", "加仓", "建仓", "入场", "该上")):
        return "buy"
    # 盈亏/收益/赚赔/赚了
    if any(w in q for w in ("盈亏", "收益", "赚", "亏", "赔", "盈利", "损益")):
        return "pnl"
    # 预警/风险/异常/警报
    if any(w in q for w in ("预警", "风险", "异常", "警报", "告警", "踩雷")):
        return "alert"
    return "summary"


def _count_positions(conn) -> int:
    try:
        r = conn.execute(
            "SELECT COUNT(*) as c FROM stocks WHERE is_active = 1 AND buy_price > 0"
        ).fetchone()
        return r["c"] if r else 0
    except Exception:
        return 0


def _plan_already_executed(plan: dict) -> bool:
    """检测执行计划是否已执行"""
    try:
        conn = get_conn()
        today = datetime.now().strftime("%Y-%m-%d")
        r = conn.execute(
            "SELECT COUNT(*) as c FROM trades WHERE date(created_at) = ?",
            (today,),
        ).fetchone()
        return bool(r and r["c"] > 0)
    except Exception:
        return False


_app = app  # 供外部引用


# ===== 数据源状态 API =====
@app.route("/api/data-source-status")
def api_data_source_status():
    """检测主备数据源连通性"""
    from datetime import datetime as dt
    status = {"sina": {"reachable": False, "latency_ms": 0},
              "tencent": {"reachable": False, "latency_ms": 0}}

    # Sina 测试
    try:
        t0 = time.time()
        raw = sina_fetch_raw([ALL_CODES[0]])
        if raw and "var hq_str" in raw:
            status["sina"]["reachable"] = True
            status["sina"]["latency_ms"] = round((time.time() - t0) * 1000)
    except Exception as e:
        status["sina"]["error"] = str(e)[:80]

    # 腾讯行情测试
    try:
        t0 = time.time()
        data = fetch_realtime([ALL_CODES[0]], source="tencent")
        if data and data[0].get("price"):
            status["tencent"]["reachable"] = True
            status["tencent"]["latency_ms"] = round((time.time() - t0) * 1000)
    except Exception as e:
        status["tencent"]["error"] = str(e)[:80]

    return jsonify({
        "ok": True,
        "timestamp": dt.now().isoformat(),
        "primary": "sina",
        "fallback": "tencent",
        "sources": status,
    })
@app.route("/api/quick")
def api_quick():
    """一键汇总：持仓盈亏 + 今日信号 + 未读预警"""
    try:
        from db import get_conn, get_unacknowledged_anomalies
        from auto_execute import generate_execution_plan
        conn = get_conn()

        # 1. 持仓盈亏
        positions = conn.execute("""
            SELECT s.code, s.name, s.buy_price, s.buy_date,
                   d.close as current_price,
                   d.change_pct,
                   ROUND((d.close - s.buy_price) / s.buy_price * 100, 2) as pnl_pct
            FROM stocks s
            LEFT JOIN (
                SELECT code, close, change_pct FROM daily_snapshots
                WHERE date = (SELECT MAX(date) FROM daily_snapshots)
            ) d ON s.code = d.code
            WHERE s.is_active = 1 AND s.buy_price > 0
        """).fetchall()

        pnl = []
        total_pnl = 0
        for r in positions:
            pnl.append({
                "code": r["code"],
                "name": r["name"],
                "buy": r["buy_price"],
                "now": r["current_price"] or 0,
                "pnl": round(r["pnl_pct"], 1) if r["pnl_pct"] else 0,
            })
            total_pnl += r["pnl_pct"] or 0

        # 2. 今日执行计划
        plan = generate_execution_plan(dry_run=True)
        sells = [{"code": s["code"], "name": s["name"], "reason": s["reasons"][:2]} for s in plan.get("sells", [])]
        buys = [{"code": b["code"], "name": b["name"], "score": b.get("score", 0)} for b in plan.get("buys", [])]

        # 3. 未读预警
        raw = get_unacknowledged_anomalies(limit=5)
        alerts = [{
            "code": a["code"],
            "name": STOCK_MAP.get(a["code"], {}).get("name", a["code"]),
            "level": a["level"],
            "msg": a["message"][:100],
        } for a in raw]

        return jsonify({
            "ok": True,
            "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "pnl": {
                "total_pct": round(total_pnl, 1),
                "positions": pnl,
            },
            "plan": {
                "sells": sells,
                "buys": buys,
                "already_executed": _plan_already_executed(plan),
            },
            "alerts": {
                "unread": len(raw),
                "emergency": len([a for a in raw if a["level"] == "A"]),
                "items": alerts,
            },
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/quick/pnl")
def api_quick_pnl():
    """纯盈亏数据"""
    try:
        conn = get_conn()
        positions = conn.execute("""
            SELECT s.code, s.name, s.buy_price,
                   d.close as current_price,
                   ROUND((d.close - s.buy_price) / s.buy_price * 100, 2) as pnl_pct
            FROM stocks s
            LEFT JOIN (SELECT code, close FROM daily_snapshots
                       WHERE date = (SELECT MAX(date) FROM daily_snapshots)) d ON s.code = d.code
            WHERE s.is_active = 1 AND s.buy_price > 0
        """).fetchall()

        positions_data = []
        total = 0
        for r in positions:
            p = round(r["pnl_pct"], 1) if r["pnl_pct"] else 0
            positions_data.append({"code": r["code"], "name": r["name"], "pnl_pct": p})
            total += p

        return jsonify({
            "ok": True,
            "total_pnl_pct": round(total, 1),
            "positions": positions_data,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/quick/alerts")
def api_quick_alerts():
    """纯预警数据"""
    try:
        from db import get_unacknowledged_anomalies
        raw = get_unacknowledged_anomalies(limit=20)
        items = []
        for a in raw:
            items.append({
                "code": a["code"],
                "name": STOCK_MAP.get(a["code"], {}).get("name", a["code"]),
                "level": a["level"],
                "type": a["alert_type"],
                "msg": a["message"][:150],
                "time": a["created_at"],
            })
        return jsonify({
            "ok": True,
            "total": len(raw),
            "emergency": len([a for a in raw if a["level"] == "A"]),
            "warning": len([a for a in raw if a["level"] == "B"]),
            "alerts": items,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


if __name__ == "__main__":
    log.info("🅳 Serenity Monitor 移动端看板启动 — http://localhost:8401/monitor")
    log.info("📊 Prometheus 指标: http://localhost:8401/metrics")
    log.info("⚡ 快捷: /api/quick /api/quick/pnl /api/quick/alerts")
    app.run(host="0.0.0.0", port=8401, debug=False)
