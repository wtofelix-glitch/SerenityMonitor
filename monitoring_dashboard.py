#!/usr/bin/env python3
"""
Serenity Monitor — 移动端监控看板
极简 Flask web 看板，手机一屏看完所有数据。
端口 8401，毛玻璃风格，30秒自动刷新。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import json
from datetime import datetime, timedelta
from flask import Flask, jsonify

# --- 项目模块 ---
from config import ALL_CODES, STOCK_MAP
from scorer import score_all
from factor_engine import get_current_signals, SIGNAL_FACTORS
from market_timing import get_market_signal
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
    }


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


@app.route("/api/monitor-data")
def api_monitor_data():
    try:
        data = gather_monitor_data()
        return jsonify({"ok": True, "data": data})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


# =============================================================
# 主页面
# =============================================================
HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>Serenity Monitor</title>
<style>
*{margin:0;padding:0;box-sizing:border-box;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
body{background:linear-gradient(135deg,#1a1a2e 0%,#16213e 100%);color:#e0e0e0;min-height:100vh;padding:12px 10px 80px}
.card{background:rgba(255,255,255,0.05);backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);border:1px solid rgba(255,255,255,0.1);border-radius:14px;padding:12px;margin-bottom:12px}
.card-title{font-size:13px;font-weight:600;color:rgba(255,255,255,0.6);margin-bottom:8px;letter-spacing:0.5px}
.top-bar{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
.top-bar h1{font-size:20px;font-weight:700;background:linear-gradient(90deg,#FFD700,#FFA500);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.top-bar .meta{text-align:right;font-size:11px;color:rgba(255,255,255,0.4);line-height:1.4}
.up{color:#00C853}
.down{color:#FF1744}
.neutral{color:#FFD700}
.text-muted{color:rgba(255,255,255,0.4)}
/* --- 持仓卡 --- */
.holding-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px}
.holding-item{background:rgba(255,255,255,0.03);border-radius:10px;padding:8px;text-align:center}
.holding-name{font-size:12px;font-weight:600;margin-bottom:2px}
.holding-code{font-size:10px;color:rgba(255,255,255,0.3);margin-bottom:4px}
.holding-pnl{font-size:16px;font-weight:700;margin-bottom:2px}
.holding-price{font-size:11px;color:rgba(255,255,255,0.5)}
.holding-signal{font-size:9px;margin-top:4px;padding:2px 6px;border-radius:4px;display:inline-block;font-weight:600}
.signal-STRONG_BUY{background:rgba(0,200,83,0.2);color:#00C853}
.signal-BUY{background:rgba(0,200,83,0.15);color:#69F0AE}
.signal-CAUTION_BUY{background:rgba(0,200,83,0.1);color:#B9F6CA}
.signal-HOLD{background:rgba(255,215,0,0.15);color:#FFD700}
.signal-WATCH{background:rgba(255,215,0,0.1);color:#FFE082}
.signal-SELL{background:rgba(255,23,68,0.15);color:#FF1744}
.signal-STOP_LOSS{background:rgba(255,23,68,0.2);color:#FF5252}
/* --- 因子表 --- */
.factor-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch;margin:0 -12px;padding:0 12px}
.factor-wrap table{font-size:10px;border-collapse:collapse;white-space:nowrap;width:100%}
.factor-wrap th{position:sticky;top:0;background:rgba(26,26,46,0.95);padding:5px 4px;text-align:center;font-weight:600;color:rgba(255,255,255,0.6);border-bottom:1px solid rgba(255,255,255,0.1);font-size:9px}
.factor-wrap td{padding:4px;text-align:center;border-bottom:1px solid rgba(255,255,255,0.04)}
.factor-wrap .stock-name{text-align:left;font-weight:600;font-size:10px;padding-left:4px;white-space:nowrap;position:sticky;left:0;background:rgba(26,26,46,0.95);z-index:1}
.factor-val{font-variant-numeric:tabular-nums}
/* --- 大盘择时 --- */
.market-row{display:flex;gap:8px;flex-wrap:wrap}
.market-item{flex:1;min-width:80px;background:rgba(255,255,255,0.03);border-radius:8px;padding:6px 8px;text-align:center;font-size:11px}
.market-item .label{color:rgba(255,255,255,0.4);font-size:9px}
.market-item .value{font-weight:600;font-size:13px;margin-top:2px}
.advice-box{background:rgba(255,215,0,0.1);border:1px solid rgba(255,215,0,0.2);border-radius:8px;padding:8px 10px;margin-top:8px;text-align:center;font-size:13px;font-weight:600;color:#FFD700}
/* --- 行业轮动 --- */
.sector-grid{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.sector-item{display:flex;justify-content:space-between;align-items:center;background:rgba(255,255,255,0.03);border-radius:8px;padding:6px 8px;font-size:12px}
.sector-name{font-weight:500}
.sector-change{font-weight:700;font-size:13px}
.sector-signal{border-radius:4px;padding:2px 6px;font-size:9px;font-weight:600}
/* --- 评级圆点 --- */
.rating-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:6px}
.rating-item{background:rgba(255,255,255,0.03);border-radius:8px;padding:6px;text-align:center}
.rating-dot{width:24px;height:24px;border-radius:12px;display:inline-flex;align-items:center;justify-content:center;font-size:10px;font-weight:700;margin-bottom:2px}
.rating-A{background:rgba(0,200,83,0.3);color:#00C853}
.rating-B{background:rgba(105,240,174,0.2);color:#69F0AE}
.rating-C{background:rgba(255,215,0,0.2);color:#FFD700}
.rating-D{background:rgba(255,152,0,0.2);color:#FF9800}
.rating-E{background:rgba(255,23,68,0.2);color:#FF1744}
.rating-N\A{background:rgba(255,255,255,0.05);color:rgba(255,255,255,0.3)}
.rating-name{font-size:10px;color:rgba(255,255,255,0.5)}
/* --- 底部导航 --- */
.bottom-nav{position:fixed;bottom:0;left:0;right:0;background:rgba(26,26,46,0.95);backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);border-top:1px solid rgba(255,255,255,0.1);display:flex;justify-content:space-around;padding:10px 0;padding-bottom:calc(10px + env(safe-area-inset-bottom,0))}
.nav-btn{background:0 0;border:none;color:rgba(255,255,255,0.5);font-size:11px;padding:6px 16px;border-radius:8px;cursor:pointer;transition:all 0.2s;display:flex;flex-direction:column;align-items:center;gap:3px}
.nav-btn .icon{font-size:18px}
.nav-btn.active,.nav-btn:active{color:#FFD700;background:rgba(255,215,0,0.1)}
.loading{text-align:center;padding:60px 0;color:rgba(255,255,255,0.3);font-size:14px}
.loading .spinner{display:inline-block;width:24px;height:24px;border:3px solid rgba(255,255,255,0.1);border-top-color:#FFD700;border-radius:50%;animation:spin 0.8s linear infinite;margin-bottom:8px}
@keyframes spin{to{transform:rotate(360deg)}}
</style>
</head>
<body>

<div id="root">
  <div class="loading">
    <div class="spinner"></div>
    <div>加载中...</div>
  </div>
</div>

<div class="bottom-nav">
  <button class="nav-btn" onclick="refresh()"><span class="icon">🔄</span>刷新</button>
  <button class="nav-btn" onclick="showTrade()"><span class="icon">📊</span>调仓</button>
  <button class="nav-btn" onclick="showConfig()"><span class="icon">⚙️</span>设置</button>
</div>

<script>
function fmt(n,d){if(n==null||isNaN(n))return'-';return Number(n).toFixed(d==null?2:d)}
function clsPct(v){if(v==null)return'';return v>=0?'up':'down'}
function pctStr(v){if(v==null||isNaN(v))return'-';let s=v>=0?'+':'';return s+v.toFixed(2)+'%'}
function sigCls(s){return'signal-'+s}

function render(d){
  const data=d.data;if(!data)return;
  const scores=data.scores||[];
  const factors=data.factors||[];
  const market=data.market||{};
  const sectors=data.sectors||[];
  const ratings=data.ratings||[];
  const sf=data.signal_factors||[];
  const fl=data.factor_labels||{};
  const etfTop5=data.etf_top5||[];
  const divTop5=data.dividend_top5||[];
  const pf=data.portfolio_summary||{};
  const pfDetails=pf.position_details||[];
  const sb=data.signal_brief||{};

  // 持仓盈亏卡（真实 P&L）
  let posHtml='';
  if(pfDetails.length>0){
    posHtml=pfDetails.map(p=>{
      const profitCls=p.profit_pct>=0?'up':'down';
      return `
        <div class="holding-item">
          <div class="holding-name ${profitCls}">${p.name||'--'}</div>
          <div class="holding-code">${p.code||''}</div>
          <div class="holding-pnl ${profitCls}">${(p.profit_pct>=0?'+':'')+p.profit_pct.toFixed(2)}%</div>
          <div class="holding-price">成本 ¥${fmt(p.buy_price)} · 现价 ¥${fmt(p.current_price)}</div>
        </div>`}).join('');
  } else {
    posHtml='<div class="text-muted" style="padding:8px;text-align:center">暂无持仓</div>';
  }

  let html=`
    <div class="top-bar">
      <h1>Serenity Monitor</h1>
      <div class="meta">
        <div>${data.date}</div>
        <div>${data.timestamp}</div>
      </div>
    </div>

    <!-- 信号简报 -->
    ${(sb.buy_count>0||sb.risk_count>0)?`
    <div class="card" style="background:rgba(255,215,0,0.08);border-color:rgba(255,215,0,0.25)">
      <div class="card-title">📡 今日信号</div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;font-size:12px">
        ${sb.buy_candidates&&sb.buy_candidates.map(b=>`
          <div style="flex:1;min-width:120px;background:rgba(0,200,83,0.08);border-radius:8px;padding:6px 8px">
            <span style="color:#00C853;font-weight:700">🟢 关注买入</span>
            <span style="font-weight:600">${b.name}</span>
            <span style="color:rgba(255,255,255,0.5);font-size:10px">${b.score}分 · ${b.action}</span>
          </div>
        `).join('')}
        ${sb.risk_alerts&&sb.risk_alerts.map(r=>`
          <div style="flex:1;min-width:120px;background:rgba(255,23,68,0.08);border-radius:8px;padding:6px 8px">
            <span style="color:#FF1744;font-weight:700">🔴 ${r.action}</span>
            <span style="font-weight:600">${r.name}</span>
            <span style="color:rgba(255,255,255,0.5);font-size:10px">${r.score}分</span>
          </div>
        `).join('')}
      </div>
    </div>`:''}

    <!-- 持仓盈亏卡 -->
    <div class="card">
      <div class="card-title">📈 持仓盈亏 · <span style="color:#FFD700">${pf.positions||0}只</span> · 总权益 ¥${fmt(pf.total_value,0)} | 浮盈 <span class="${(pf.total_profit_pct||0)>=0?'up':'down'}">${(pf.total_profit_pct||0)>=0?'+':''}${fmt(pf.total_profit_pct,2)}%</span></div>
      <div class="holding-grid">
        ${posHtml}
      </div>
    </div>

    <!-- 评分排行条 -->
    <div class="card">
      <div class="card-title">🏆 评分排行</div>
      <div style="display:flex;gap:6px;overflow-x:auto;-webkit-overflow-scrolling:touch;padding:2px 0">
        ${scores.map((s,i)=>`
          <div style="flex:0 0 auto;min-width:72px;background:rgba(255,255,255,0.03);border-radius:8px;padding:6px 8px;text-align:center">
            <div style="font-size:9px;color:rgba(255,255,255,0.4)">#${s.rank}</div>
            <div style="font-size:12px;font-weight:600;white-space:nowrap">${s.name}</div>
            <div style="font-size:15px;font-weight:700;color:${s.total_score>=65?'#00C853':s.total_score>=50?'#FFD700':'#FF1744'}">${fmt(s.total_score,0)}</div>
            <div style="font-size:9px;color:rgba(255,255,255,0.35)">${s.signal_action||'HOLD'}</div>
          </div>
        `).join('')}
      </div>
    </div>

    <!-- 14因子矩阵 -->
    <div class="card">
      <div class="card-title">🧮 14因子信号矩阵</div>
      <div class="factor-wrap">
        <table>
          <thead><tr>
            <th>标的</th>
            ${sf.map(f=>'<th>'+ (fl[f]||f) +'</th>').join('')}
          </tr></thead>
          <tbody>
            ${factors.map(stk=>`
              <tr>
                <td class="stock-name">${stk.name}</td>
                ${sf.map(f=>{
                  let v=stk[f];
                  let c='';let disp='-';
                  if(v!=null){disp=fmt(v,3);c=v>=0?'up':'down';}
                  return '<td class="factor-val '+c+'">'+disp+'</td>';
                }).join('')}
              </tr>
            `).join('')}
          </tbody>
        </table>
      </div>
    </div>

    <!-- 大盘择时 -->
    <div class="card">
      <div class="card-title">📊 大盘择时</div>
      <div class="market-row">
        <div class="market-item">
          <div class="label">上证</div>
          <div class="value">${market.sh?fmt(market.sh.last_close):'--'}</div>
          <div class="text-muted" style="font-size:9px">${market.sh?market.sh.trend:'--'}</div>
        </div>
        <div class="market-item">
          <div class="label">沪深300</div>
          <div class="value">${market.hs300?fmt(market.hs300.last_close):'--'}</div>
          <div class="text-muted" style="font-size:9px">${market.hs300?market.hs300.trend:'--'}</div>
        </div>
        <div class="market-item">
          <div class="label">RSI</div>
          <div class="value ${market.avg_rsi>=70?'down':market.avg_rsi<=30?'up':'neutral'}">${market.avg_rsi!=null?fmt(market.avg_rsi,1):'--'}</div>
          <div class="text-muted" style="font-size:9px">${market.overall_trend||'--'}</div>
        </div>
      </div>
      <div class="advice-box">
        💡 ${market.overall_advice||'等待数据...'}
      </div>
    </div>

    <!-- 行业轮动 -->
    <div class="card">
      <div class="card-title">🔄 行业轮动</div>
      <div class="sector-grid">
        ${sectors.map(s=>`
          <div class="sector-item">
            <span class="sector-name">${s.sector}</span>
            <span class="sector-change ${clsPct(s.change)}">${pctStr(s.change)}</span>
          </div>
        `).join('')}
      </div>
    </div>

    <!-- 综合评级 -->
    <div class="card">
      <div class="card-title">⭐ 综合评级</div>
      <div class="rating-grid">
        ${ratings.map(r=>`
          <div class="rating-item">
            <div class="rating-dot rating-${r.rating.replace('/','\\/')}">${r.rating}</div>
            <div style="font-size:12px;font-weight:600">${r.name}</div>
            <div class="rating-name">${r.signal_label||''}</div>
          </div>
        `).join('')}
      </div>
    </div>

    <!-- ETF 动量轮动 -->
    <div class="card">
      <div class="card-title">📈 ETF 动量轮动 Top 5</div>
      <div class="sector-grid">
        ${etfTop5.map((e,i)=>`
          <div class="sector-item">
            <span class="sector-name">#${e.rank||i+1} ${e.name||e.etf_code}</span>
            <span class="sector-change ${e.total_score>=70?'up':e.total_score>=50?'neutral':'down'}">${fmt(e.total_score,0)}分</span>
          </div>
        `).join('')||'<div class="text-muted" style="padding:8px">暂无数据</div>'}
      </div>
    </div>

    <!-- 红利低波 -->
    <div class="card">
      <div class="card-title">💰 红利低波 Top 5</div>
      <div class="sector-grid">
        ${divTop5.map(r=>`
          <div class="sector-item">
            <span class="sector-name">${r.name||r.code}</span>
            <span class="sector-change ${r.total_score>=70?'up':r.total_score>=50?'neutral':'down'}">${fmt(r.total_score,0)}分</span>
          </div>
        `).join('')||'<div class="text-muted" style="padding:8px">暂无数据</div>'}
      </div>
    </div>

    <!-- 因子 IC 归因 -->
    <div class="card" id="factor-ic-card">
      <div class="card-title">📊 因子 IC 归因 <span style="font-size:9px;color:rgba(255,255,255,0.3);font-weight:400">（近30天·Rank IC）</span></div>
      <div id="factor-ic-content" style="font-size:11px;color:rgba(255,255,255,0.5);text-align:center;padding:10px">加载中...</div>
    </div>

    <!-- 信号绩效回顾 -->
    <div class="card" id="signal-history-card">
      <div class="card-title">📊 近7天买入信号绩效</div>
      <div id="signal-history-content" style="font-size:11px;color:rgba(255,255,255,0.5);text-align:center;padding:10px">加载中...</div>
    </div>
  `;

  document.getElementById('root').innerHTML=html;
  loadSignalHistory();
  loadFactorIC();
}

function loadSignalHistory(){
  fetch('/api/signal-history').then(r=>r.json()).then(d=>{
    if(!d.ok || !d.data.length){
      document.getElementById('signal-history-content').innerHTML='<div style="padding:8px;text-align:center;color:rgba(255,255,255,0.4)">暂无买入信号</div>';
      return;
    }
    var h=d.data.map(s=>{
      var icon={'STRONG_BUY':'🟢🟢🟢','BUY':'🟢🟢','CAUTION_BUY':'🟢'}[s.action]||'⚪';
      var o1d=s.outcome_1d!=null?(s.outcome_1d>=0?'+':'')+s.outcome_1d.toFixed(1)+'%':'—';
      var o3d=s.outcome_3d!=null?(s.outcome_3d>=0?'+':'')+s.outcome_3d.toFixed(1)+'%':'—';
      var oCls=s.outcome_1d!=null?(s.outcome_1d>=0?'up':'down'):'';
      return `<div style="display:flex;justify-content:space-between;align-items:center;padding:4px 0;border-bottom:1px solid rgba(255,255,255,0.05)">
        <div style="display:flex;align-items:center;gap:6px">
          <span>${icon}</span>
          <span style="font-weight:600">${s.name}</span>
          <span style="font-size:10px;color:rgba(255,255,255,0.4)">${s.date} ${s.time}</span>
        </div>
        <div style="display:flex;gap:12px;font-size:10px">
          <span>${fmt(s.score,0)}分</span>
          <span class="${oCls}">1D:${o1d}</span>
          <span>3D:${o3d}</span>
          <span style="color:rgba(255,255,255,0.5)">¥${fmt(s.price)}</span>
        </div>
      </div>`;
    }).join('');
    document.getElementById('signal-history-content').innerHTML=h;
  }).catch(()=>{
    document.getElementById('signal-history-content').innerHTML='<div style="color:#FF1744;text-align:center">加载失败</div>';
  });
}

function loadFactorIC(){
  var el=document.getElementById('factor-ic-content');
  fetch('/api/factor-ic').then(r=>r.json()).then(d=>{
    if(!d.ok || !d.ic_summary||!d.ic_summary.length){
      el.innerHTML='<div style="padding:8px;text-align:center;color:rgba(255,255,255,0.4)">暂无数据</div>';
      return;
    }
    var top=d.top_factors||[];
    var weak=d.weak_factors||[];
    var rows='';
    // Top factors
    if(top.length){
      rows+='<div style="margin-bottom:6px"><span style="color:#00C853;font-size:10px;font-weight:600">🏆 最有效</span></div>';
      top.forEach(function(f){
        var cls=f.ic>=0?'up':'down';
        rows+='<div style="display:flex;justify-content:space-between;align-items:center;padding:3px 0;border-bottom:1px solid rgba(255,255,255,0.04)">'
          +'<span style="font-weight:500">'+f.label+'</span>'
          +'<span class="'+cls+'" style="font-weight:600">'+(f.ic>=0?'+':'')+fmt(f.ic,3)+'</span>'
          +'</div>';
      });
    }
    // Weak factors
    if(weak.length){
      rows+='<div style="margin-top:8px;margin-bottom:6px"><span style="color:#FF1744;font-size:10px;font-weight:600">⚠️ 最无效</span></div>';
      weak.forEach(function(f){
        var cls=f.ic>=0?'up':'down';
        rows+='<div style="display:flex;justify-content:space-between;align-items:center;padding:3px 0;border-bottom:1px solid rgba(255,255,255,0.04)">'
          +'<span style="font-weight:500">'+f.label+'</span>'
          +'<span class="'+cls+'" style="font-weight:600">'+(f.ic>=0?'+':'')+fmt(f.ic,3)+'</span>'
          +'</div>';
      });
    }
    el.innerHTML=rows;
  }).catch(function(){
    el.innerHTML='<div style="color:#FF1744;text-align:center">加载失败</div>';
  });
}

function refresh(){
  document.getElementById('root').innerHTML='<div class="loading"><div class="spinner"></div><div>刷新中...</div></div>';
  fetch('/api/monitor-data').then(r=>r.json()).then(d=>{
    if(d.ok)render(d);else document.getElementById('root').innerHTML='<div class="loading" style="color:#FF1744">❌ 数据获取失败</div>';
  }).catch(()=>{
    document.getElementById('root').innerHTML='<div class="loading" style="color:#FF1744">❌ 网络错误</div>';
  });
}

// 首次加载 + 30秒自动刷新
refresh();
setInterval(refresh,30000);
// 因子 IC 卡片独立刷新（60秒）
setInterval(loadFactorIC,60000);

// ===== 调仓弹窗 =====
function showTrade(){
  fetch('/api/monitor-data').then(r=>r.json()).then(d=>{
    var scores=d.data.scores||[];
    var h='<div class="modal-overlay" onclick="closeModal()"><div class="modal-box" onclick="event.stopPropagation()">'
    +'<div class="modal-title">📊 调仓操作</div>'
    +'<form onsubmit="submitTrade(event)">'
    +'<select name="code" style="width:100%;padding:8px;margin:5px 0">'
    +scores.map(s=>'<option value="'+s.code+'">'+s.name+' ('+s.code+') 评分:'+fmt(s.total||s.score,1)+'</option>').join('')
    +'</select>'
    +'<select name="action" style="width:100%;padding:8px;margin:5px 0"><option value="buy">买入</option><option value="sell">卖出</option></select>'
    +'<input name="price" type="number" step="0.01" placeholder="成交价格" style="width:100%;padding:8px;margin:5px 0" required>'
    +'<input name="qty" type="number" step="1" placeholder="数量(股)" style="width:100%;padding:8px;margin:5px 0" required>'
    +'<input name="note" placeholder="备注(可选)" style="width:100%;padding:8px;margin:5px 0">'
    +'<button type="submit" style="width:100%;padding:10px;background:#1565C0;color:#fff;border:none;border-radius:8px;margin-top:10px">确认提交</button>'
    +'</form></div></div>';
    var el=document.createElement('div');el.id='modal';el.innerHTML=h;document.body.appendChild(el);
  });
}
function submitTrade(e){
  e.preventDefault();
  var f=e.target;
  var data={code:f.code.value,action:f.action.value,price:parseFloat(f.price.value),quantity:parseInt(f.qty.value),note:f.note.value};
  fetch('/api/trades',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)})
  .then(r=>r.json()).then(d=>{alert(d.ok?'✅ '+d.msg:'❌ '+d.msg);closeModal();refresh();});
}

// ===== 设置弹窗 =====
function showConfig(){
  fetch('/api/monitor-data').then(r=>r.json()).then(d=>{
    var scores=d.data.scores||[];
    var h='<div class="modal-overlay" onclick="closeModal()"><div class="modal-box" onclick="event.stopPropagation()">'
    +'<div class="modal-title">⚙️ 持仓设置</div>'
    +'<form onsubmit="submitConfig(event)">'
    +'<select name="code" style="width:100%;padding:8px;margin:5px 0">'
    +scores.map(s=>'<option value="'+s.code+'">'+s.name+' ('+s.code+')</option>').join('')
    +'</select>'
    +'<input name="stop_loss" type="number" step="0.01" placeholder="止损价" style="width:100%;padding:8px;margin:5px 0">'
    +'<input name="target_high" type="number" step="0.01" placeholder="止盈目标上限" style="width:100%;padding:8px;margin:5px 0">'
    +'<input name="target_low" type="number" step="0.01" placeholder="止盈目标下限" style="width:100%;padding:8px;margin:5px 0">'
    +'<button type="submit" style="width:100%;padding:10px;background:#E65100;color:#fff;border:none;border-radius:8px;margin-top:10px">保存设置</button>'
    +'</form></div></div>';
    var el=document.createElement('div');el.id='modal';el.innerHTML=h;document.body.appendChild(el);
  });
}
function submitConfig(e){
  e.preventDefault();
  var f=e.target;
  var data={code:f.code.value};
  if(f.stop_loss.value)data.stop_loss=parseFloat(f.stop_loss.value);
  if(f.target_high.value)data.target_high=parseFloat(f.target_high.value);
  if(f.target_low.value)data.target_low=parseFloat(f.target_low.value);
  fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)})
  .then(r=>r.json()).then(d=>{alert(d.ok?'✅ '+d.msg:'❌ '+d.msg);closeModal();});
}
function closeModal(){var m=document.getElementById('modal');if(m)m.remove();}
</script>
<style>
.modal-overlay{position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.6);z-index:999;display:flex;align-items:center;justify-content:center}
.modal-box{background:#1a1a2e;border-radius:16px;padding:20px;width:90%;max-width:360px;max-height:80vh;overflow-y:auto}
.modal-title{font-size:16px;font-weight:700;margin-bottom:12px;color:#fff}
</style>
</body>
</html>"""


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


@app.route("/monitor")
def monitor_page():
    return HTML


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


if __name__ == "__main__":
    print("🅳 Serenity Monitor 移动端看板启动")
    print(f"   地址: http://localhost:8401/monitor")
    app.run(host="0.0.0.0", port=8401, debug=False)
