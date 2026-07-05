/* Serenity Monitor dashboard renderer. */

/* Serenity Monitor dashboard renderer. */

'use strict';

// ─── 全局状态 ─────────────────────────────────────────────────
const STATE = {
  data: null,
  chartInstance: null,
  activeTab: 'overview',
  refreshInterval: null,
  refreshController: null,
  renderedTabs: new Set(),
};

// ─── 工具函数 ─────────────────────────────────────────────────
const fmt = (n, d = 2) => (n == null || isNaN(n)) ? '—' : Number(n).toFixed(d);
const clsPct = v => (v == null || isNaN(v) || v === 0) ? '' : (v >= 0 ? 'up' : 'down');
const pctStr = v => (v == null || isNaN(v)) ? '—' : (v >= 0 ? '+' : '') + v.toFixed(2) + '%';
const signClass = s => s ? 'signal-label-' + s : '';

function fmtCurrency(v) {
  if (v == null || isNaN(v)) return '—';
  if (Math.abs(v) >= 10000) return '¥' + (v / 10000).toFixed(1) + '万';
  return '¥' + Number(v).toFixed(0);
}

function getWriteToken() {
  const params = new URLSearchParams(window.location.search);
  const urlToken = params.get('token');
  if (urlToken) { try { localStorage.setItem('serenity_dashboard_token', urlToken); } catch(e) {} return urlToken; }
  try { return localStorage.getItem('serenity_dashboard_token') || ''; } catch(e) { return ''; }
}
function writeHeaders(base) {
  const headers = Object.assign({}, base || {});
  const token = getWriteToken();
  if (token) headers['X-Serenity-Token'] = token;
  return headers;
}

function cssToken(name, fallback) {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || fallback;
}

function dashboardSkeleton(rows = 3) {
  return `<div class="dashboard-skeleton" aria-label="正在加载数据">
    <span class="skeleton-heading"></span>
    ${Array.from({ length: rows }, () => '<span class="skeleton-row"></span>').join('')}
  </div>`;
}

function retryDashboardLoad() {
  const target = $('tab-' + STATE.activeTab);
  if (target) target.innerHTML = dashboardSkeleton();
  refresh(true);
}

function componentError(message, retryFunction) {
  const retry = retryFunction || 'retryDashboardLoad';
  return `<div class="error-state" role="alert"><strong>${message}</strong>
    <button class="retry-btn" type="button" onclick="${retry}()">重试</button></div>`;
}

function fetchJSON(url, options) {
  return fetch(url, options).then(response => {
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  });
}

// ─── DOM 快捷引用 ─────────────────────────────────────────────
const $ = id => document.getElementById(id);
const qs = (sel, ctx) => (ctx || document).querySelector(sel);

// ─── 行情/市场逻辑 ────────────────────────────────────────────
function getMarketView(mkt) {
  const raw = ((mkt || {}).overall_signal || '').toLowerCase();
  if (raw.includes('多') || raw === 'bull' || raw === 'bullish') return { label: '多头', cls: 'up' };
  if (raw.includes('空') || raw === 'bear' || raw === 'bearish') return { label: '空头', cls: 'down' };
  return { label: '震荡', cls: 'gold' };
}

function getSession(d) {
  const now = new Date();
  const day = now.getDay(); const mins = now.getHours() * 60 + now.getMinutes();
  if (day === 0 || day === 6) return { id: 'closed', label: '休市', tone: 'gold', focus: '观察，不开新动作', window: '非交易日' };
  if (mins < 9 * 60 + 15) return { id: 'premarket', label: '盘前', tone: 'gold', focus: '筛候选，定风控线', window: '09:15前' };
  if (mins < 9 * 60 + 30) return { id: 'premarket', label: '竞价', tone: 'gold', focus: '只确认，不追价', window: '09:15-09:30' };
  if ((mins >= 9 * 60 + 30 && mins <= 11 * 60 + 30) || (mins >= 13 * 60 && mins <= 15 * 60))
    return { id: 'intraday', label: '盘中', tone: 'up', focus: '只处理高置信动作', window: mins < 12 * 60 ? '09:30-11:30' : '13:00-15:00' };
  if (mins > 11 * 60 + 30 && mins < 13 * 60) return { id: 'midday', label: '午间', tone: 'gold', focus: '复核早盘成交', window: '11:30-13:00' };
  return { id: 'postmarket', label: '盘后', tone: 'down', focus: '记录原因，更新明日队列', window: '15:00后' };
}

function actionLabel(a) {
  const map = { STRONG_BUY:'强买', BUY:'买入', CAUTION_BUY:'谨慎买入', HOLD:'持有', WATCH:'观察', WEAK_HOLD:'弱持有', SELL:'卖出', STOP_LOSS:'止损', TAKE_PROFIT:'止盈' };
  return map[a] || a || '观察';
}

function qdDecisionLabel(decision) {
  const map = { BUY: '偏进攻', WATCH: '观察', REDUCE: '降风险', NO_DATA: '无数据' };
  return map[decision] || decision || '观察';
}

function qdTone(decision) {
  if (decision === 'BUY') return 'up';
  if (decision === 'REDUCE') return 'down';
  if (decision === 'NO_DATA') return 'muted';
  return 'gold';
}

function qdName(item) {
  return item.name || item.code || '—';
}

function buildAutoGateCard(d) {
  const gate = d.auto_gate || {};
  const state = gate.state || 'PAPER';
  const passed = !!gate.gate_passed;
  const tone = state === 'SEMI_AUTO' ? 'up' : (state === 'LOCKED' ? 'down' : 'gold');
  const reasons = gate.reasons || [];
  const complianceFlow = gate.compliance_flow || [];
  const qualityWarnings = gate.data_quality_warnings || [];
  const requiredSamples = gate.required_sample_count || 50;
  const reasonHtml = reasons.length
    ? `<div class="gate-reasons">${reasons.map(r => `<span>${r}</span>`).join('')}</div>`
    : '<div class="gate-reasons"><span>等待更多可执行样本</span></div>';
  const complianceHtml = complianceFlow.length
    ? `<div class="compliance-flow">${complianceFlow.map(step => `
        <div class="compliance-step ${step.done ? 'done' : ''} ${step.active ? 'active' : ''}">
          <span class="compliance-dot"></span>
          <span>${step.label || step.id}</span>
        </div>`).join('')}</div>`
    : '';
  const qualityHtml = qualityWarnings.length
    ? `<div class="gate-quality">${qualityWarnings.map(w => `
        <span>${w.code || '—'} ${w.quality_status || 'unknown'} · ${w.warning || '数据质量 warning'}</span>`).join('')}</div>`
    : '';
  return `
  <div class="card gate-card">
    <div class="card-header">
      <span class="card-title">自动闸门</span>
      <span class="card-subtitle ${tone}">${state}</span>
    </div>
    <div class="card-body">
      <div class="gate-grid">
        <div><span>样本</span><b>${gate.sample_count || 0}/${requiredSamples}</b></div>
        <div><span>胜率</span><b class="${passed ? 'up' : 'gold'}">${fmt((gate.win_rate || 0) * 100, 1)}%</b></div>
        <div><span>Wilson</span><b class="${(gate.wilson_lower || 0) >= 0.5 ? 'up' : 'gold'}">${fmt((gate.wilson_lower || 0) * 100, 1)}%</b></div>
        <div><span>超额胜率</span><b>${fmt((gate.excess_win_rate || 0) * 100, 1)}%</b></div>
      </div>
      ${complianceHtml}
      <div class="gate-footer">
        <span>合规 ${gate.compliance_status || 'not_reported'}</span>
        <span>上限 ${gate.max_state || 'MANUAL'}</span>
        <span>${gate.consecutive_loss_ok === false ? '连续亏损触发' : '连续亏损正常'}</span>
      </div>
      ${reasonHtml}
      ${qualityHtml}
    </div>
  </div>`;
}

// ─── 防抖 ────────────────────────────────────────────────────
function debounce(fn, delay) {
  let timer = null;
  return function(...args) { clearTimeout(timer); timer = setTimeout(() => fn.apply(this, args), delay); };
}

// ─── TAB 导航 ─────────────────────────────────────────────────
function initTabs() {
  const tabs = [...document.querySelectorAll('.tab-btn')];
  tabs.forEach((btn, index) => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
    btn.addEventListener('keydown', event => {
      let nextIndex = null;
      if (event.key === 'ArrowRight' || event.key === 'ArrowDown') nextIndex = (index + 1) % tabs.length;
      if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') nextIndex = (index - 1 + tabs.length) % tabs.length;
      if (event.key === 'Home') nextIndex = 0;
      if (event.key === 'End') nextIndex = tabs.length - 1;
      if (nextIndex == null) return;
      event.preventDefault();
      tabs[nextIndex].focus();
      switchTab(tabs[nextIndex].dataset.tab);
    });
  });
}

function switchTab(tabId, force = false) {
  STATE.activeTab = tabId;
  document.querySelectorAll('.tab-btn').forEach(button => {
    const active = button.dataset.tab === tabId;
    button.classList.toggle('active', active);
    button.setAttribute('aria-selected', String(active));
    button.tabIndex = active ? 0 : -1;
  });
  document.querySelectorAll('.tab-content').forEach(panel => {
    const active = panel.id === 'tab-' + tabId;
    panel.classList.toggle('active', active);
    panel.hidden = !active;
  });

  // v5.1 总览页显示快捷操作栏
  const ab = $('action-bar');
  if (ab) ab.style.display = tabId === 'overview' ? 'flex' : 'none';

  if (!STATE.data) return;
  if (!force && STATE.renderedTabs.has(tabId)) return;
  if (tabId === 'overview') renderOverview(STATE.data);
  else if (tabId === 'holdings') renderHoldingsTab(STATE.data);
  else if (tabId === 'sentinel') { renderSentinelTab(STATE.data); loadSentinelData(); }
  else if (tabId === 'risk') { renderRiskTab(STATE.data); loadNavHistory(); }
  else if (tabId === 'operations') { renderOperationsTab(); loadOperationsData(); }
  else if (tabId === 'live') { renderLiveTab(STATE.data); startLiveRefresh(); }
  else if (tabId === 'governance') { renderGovernanceTab(); loadGovernanceData(); }
  STATE.renderedTabs.add(tabId);
}

// ─── v5.0 Toast 通知系统 ─────────────────────────────────────────
function showToast(msg, type) {
  type = type || 'info';
  let container = document.querySelector('.toast-container');
  if (!container) { container = document.createElement('div'); container.className = 'toast-container'; document.body.appendChild(container); }
  const icons = { success: '✅', error: '🚨', warning: '⚠️', info: 'ℹ️' };
  const el = document.createElement('div');
  el.className = 'toast ' + (type || 'info');
  el.innerHTML = '<span class="toast-icon">' + (icons[type] || 'ℹ️') + '</span><span class="toast-msg">' + msg + '</span>';
  container.appendChild(el);
  setTimeout(() => { el.classList.add('exiting'); setTimeout(() => el.remove(), 200); }, 3500);
}

// ─── 初始化 ────────────────────────────────────────────────────
function init() {
  initTabs();
  switchTab(STATE.activeTab);
  // v5.1 快速初始加载：独立于refresh，避免AbortController冲突
  loadInitialData();
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) scheduleRefresh();
    else refresh(true);
  });
}

function loadInitialData() {
  // v5.1 两阶段加载: 快速快照(<1s) → 立即展示 → 全量数据后台刷新
  var overviewEl = document.getElementById('tab-overview');
  if (overviewEl) overviewEl.innerHTML = '<div style="color:var(--gold);padding:20px;text-align:center">⏳ 加载中...</div>';

  // Phase 1: 快速快照(DB only, <500ms)
  fetch('/api/quick-snapshot')
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (d.ok && d.portfolio) {
        // 构造最小可用数据展示
        STATE.data = {
          portfolio_summary: d.portfolio,
          scores: d.scores || [],
          auto_gate: d.gate || {},
          signal_brief: { buy_count: 0, risk_count: 0, buy_candidates: [], risk_alerts: [] },
          market: {}, operational_mode: {}, quantdinger_consensus: {},
          position_advice: {}, uzi_chain: [], ic_analysis: {},
        };
        renderAll();
      }
    })
    .catch(function() { /* Phase 1 failed, wait for Phase 2 */ })
    .finally(function() {
      // Phase 2: 全量数据(后台静默刷新, 不阻塞展示)
      fetch('/api/monitor-data')
        .then(function(r) { return r.json(); })
        .then(function(d) {
          if (d.ok) { STATE.data = d.data; renderAll(); }
        })
        .catch(function() {})
        .finally(function() { scheduleRefresh(); });
    });
}

// ─── 数据刷新 ──────────────────────────────────────────────────
function refresh(force) {
  if (STATE.refreshController && !force) return;
  if (STATE.refreshController) STATE.refreshController.abort();
  const controller = new AbortController();
  STATE.refreshController = controller;
  const timeEl = $('header-time');
  if (timeEl) timeEl.textContent = '⟳ 刷新中...';
  const url = force ? '/api/monitor-data?force=1' : '/api/monitor-data';
  fetch(url, { signal: controller.signal })
    .then(r => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.json();
    })
    .then(d => {
      if (d.ok) { STATE.data = d.data; renderAll(); }
      else { showToast(d.error || '数据获取失败', 'error'); }
    })
    .catch(err => { if (err && err.name !== 'AbortError') showToast(`数据加载失败：${err.message || '网络错误'}`, 'error'); })
    .finally(() => {
      if (STATE.refreshController === controller) STATE.refreshController = null;
      scheduleRefresh();
    });
}

function renderAll() {
  updateHeader();
  updateMarketTape();
  STATE.renderedTabs.clear();
  switchTab(STATE.activeTab, true);
  checkGateStateChange();
  pushSignalAlerts();
}

function refreshDelay() {
  if (document.hidden) return 60000;
  const now = new Date();
  const minutes = now.getHours() * 60 + now.getMinutes();
  const day = now.getDay();
  if (day === 0 || day === 6) return 30000; // 周末
  // 盘中 09:30-11:30 + 13:00-15:00 → 5s
  if ((minutes >= 570 && minutes <= 690) || (minutes >= 780 && minutes <= 900)) return 5000;
  // 盘前/午间 → 10s
  if (minutes >= 540 || (minutes > 690 && minutes < 780)) return 10000;
  return 30000; // 盘后
}

function scheduleRefresh() {
  clearTimeout(STATE.refreshInterval);
  STATE.refreshInterval = setTimeout(() => refresh(false), refreshDelay());
}

function manualRefresh() {
  const btn = $('refresh-btn');
  if (btn) btn.classList.add('spinning');
  refresh(true);
  setTimeout(() => { if (btn) btn.classList.remove('spinning'); }, 700);
}

// ─── 头部 ──────────────────────────────────────────────────────
function updateHeader() {
  const d = STATE.data; if (!d) return;
  const el = $('header-time');
  if (el) el.textContent = d.timestamp || d.date || '—';
  const badge = $('session-badge');
  if (badge) {
    const mkt = d.market || {};
    const session = getSession(d);
    badge.textContent = session.label;
    badge.className = 'session-badge ' + session.tone;
  }
}

// ─── 行情条 ────────────────────────────────────────────────────
function updateMarketTape() {
  const d = STATE.data; const el = $('market-tape'); if (!d || !el) return;
  const pf = d.portfolio_summary || {}; const sb = d.signal_brief || {};
  const mkt = d.market || {}; const sh = mkt.sh || {}; const hs300 = mkt.hs300 || {};
  const mv = getMarketView(mkt); const session = getSession(d);
  const pnl = pf.total_profit_pct || 0;

  el.innerHTML = `
    <span class="tape-kicker">MARKET</span>
    <div class="tape-track">
      <span><b>上证</b><em>${sh.last_close ? fmt(sh.last_close, 0) : '--'} · ${mv.label}</em></span>
      <span><b>沪深300</b><em>${hs300.last_close ? fmt(hs300.last_close, 0) : '--'} · ${mv.label}</em></span>
      <span><b>阶段</b><em class="${session.tone}">${session.label}</em></span>
      <span><b>组合</b><em class="${pnl >= 0 ? 'up' : 'down'}">${(pnl >= 0 ? '+' : '') + fmt(pnl, 2)}%</em></span>
      <span><b>信号</b><em>${sb.buy_count || 0}B / ${sb.risk_count || 0}R</em></span>
    </div>
    <span class="tape-time">${d.timestamp || 'LIVE'}</span>`;
}

// ═══════════════════════════════════════════════════════════
// TAB 1 — 总览
// ═══════════════════════════════════════════════════════════
function renderOverview(d) {
  if (!d) { $('tab-overview').innerHTML = dashboardSkeleton(5); return; }

  const pf = d.portfolio_summary || {};
  const sb = d.signal_brief || {};
  const mkt = d.market || {};
  const op = d.operational_mode || {};
  const mv = getMarketView(mkt);
  const session = getSession(d);
  const pnl = pf.total_profit_pct || 0;
  const pnlAmount = pf.total_profit_amount || 0;

  const modeLabels = { mean_revert:'均值回归', trend:'趋势跟踪', neutral:'中性' };
  const modeLabel = modeLabels[op.mode] || op.mode || '中性';

  // Signal counts
  const buyCount = sb.buy_count || 0;
  const riskCount = sb.risk_count || 0;

  let html = '';

  // ── Hero ────────────────────────────────────────────────
  const yesterday = d.yesterday_summary || {};
  const dayChange = yesterday.total_value ? pf.total_value - yesterday.total_value : 0;
  html += `
  <section class="hero-compact">
    <div class="hero-equity">${fmtCurrency(pf.total_value)}</div>
    <div class="hero-pnl-row">
      <span class="hero-pnl-today ${clsPct(pnl)}">
        <span class="dot-row"><span class="dot-indicator dot-${pnl >= 0 ? 'up' : 'down'}"></span>${(pnl >= 0 ? '+' : '') + fmt(pnl, 2)}%</span>
      </span>
      <span class="hero-pnl-total">浮盈 ${fmtCurrency(pnlAmount)}</span>
    </div>
    <div class="hero-pills">
      <span class="hero-pill ${mv.cls}">市场 ${mv.label}</span>
      <span class="hero-pill ${session.tone}">${session.label}阶段</span>
      <span class="hero-pill">模式 ${modeLabel}</span>
      <span class="hero-pill">${pf.positions || 0}只持仓</span>
    </div>
  </section>`;

  // ── NL Search ───────────────────────────────────────────
  html += `
  <div class="nl-search-bar">
    <input type="text" id="nl-query-input" class="nl-search-input" placeholder="问 Serenity，例如：今天该买什么？">
    <button class="nl-search-btn" onclick="doNLQuery()" aria-label="提交自然语言查询" title="提交查询">→</button>
  </div>
  <div id="nl-result"></div>`;

  // ── KPI Row ─────────────────────────────────────────────
  const cashPct = pf.total_value ? ((pf.cash || 0) / pf.total_value * 100).toFixed(0) : 0;
  html += `
  <div class="kpi-row">
    <div class="kpi-item">
      <div class="kpi-label">总权益</div>
      <div class="kpi-value">${fmtCurrency(pf.total_value)}</div>
      <div class="kpi-sub">${pf.positions || 0} 只持仓</div>
    </div>
    <div class="kpi-item">
      <div class="kpi-label">可用资金</div>
      <div class="kpi-value">${fmtCurrency(pf.cash)}</div>
      <div class="kpi-sub">${cashPct}% 现金</div>
    </div>
    <div class="kpi-item">
      <div class="kpi-label">持仓数</div>
      <div class="kpi-value gold">${pf.positions || 0}</div>
      <div class="kpi-sub">最大 10 只</div>
    </div>
    <div class="kpi-item">
      <div class="kpi-label">活跃信号</div>
      <div class="kpi-value ${buyCount > 0 ? 'up' : (riskCount > 0 ? 'down' : '')}">${buyCount + riskCount}</div>
      <div class="kpi-sub">${buyCount}买 / ${riskCount}险</div>
    </div>
  </div>`;

  html += buildAutoGateCard(d);

  // ── Merged Actions ──────────────────────────────────────
  html += buildQuantDingerConsensus(d);
  html += buildMergedActions(d, session);

  // ── Position Quick View ─────────────────────────────────
  html += buildPositionQuickView(d);

  // ── v5.1 紧凑信号卡片 + 信号分布图 ──
  const scores = d.scores || [];
  const heldCodes = new Set((d.portfolio_summary || {}).position_details ? d.portfolio_summary.position_details.map(p => p.code) : []);
  const buys = scores.filter(s => !heldCodes.has(s.code) && ['STRONG_BUY', 'BUY','CAUTION_BUY'].includes(s.signal_action)).slice(0, 4);

  // 信号分布图 + 信号卡片 2列(桌面端)
  html += `<div class="desktop-overview-grid"><div class="card signal-group-card"><div class="card-header"><span class="card-title">📡 信号分布</span></div><div class="card-body">${renderSignalDistChart(scores)}</div></div>`;

  if (buys.length > 0) {
    let signalCards = '<div class="card signal-group-card"><div class="card-header"><span class="card-title">🎯 买入候选</span><span class="card-subtitle">' + buys.length + '个</span></div><div class="card-body" style="display:flex;flex-direction:column;gap:6px">';
    buys.forEach((b, i) => {
      const label = {STRONG_BUY:'强买',BUY:'买入',CAUTION_BUY:'谨慎'}[b.signal_action];
      const accentColor = b.signal_action==='STRONG_BUY'?'var(--up)':b.signal_action==='BUY'?'#FF453A':'var(--gold)';
      signalCards += `<div class="compact-signal">
        <span class="compact-signal-rank">#${i+1}</span>
        <div class="compact-signal-body">
          <span class="compact-signal-name">${b.name}</span>
          <span class="compact-signal-code">${b.code.slice(-3)}</span>
          <span class="compact-signal-score" style="color:${accentColor}">${fmt(b.total_score,0)}<span style="font-size:10px;opacity:.7">分</span></span>
          <span style="font-size:10px;padding:2px 6px;border-radius:4px;background:${accentColor}22;color:${accentColor}">${label}</span>
        </div>
        <div class="compact-signal-price">${b.close>0?'¥'+fmt(b.close,2):'—'}<br><span style="font-size:9px;color:var(--text-tertiary)">目标 ${b.target_sell>0?'¥'+fmt(b.target_sell,0):'—'}</span></div>
      </div>`;
    });
    signalCards += '</div></div>';
    html += signalCards;
  }
  html += '</div>'; // close desktop-overview-grid

  // ── 🆕 v3.5 Premium PnL Cards — 渐变盈亏 ──────────────────
  const posDetails = (d.portfolio_summary || {}).position_details || [];
  if (posDetails.length > 0) {
    let pnlCards = '';
    posDetails.forEach(p => {
      const pnlPct = p.profit_pct || 0;
      const isUp = pnlPct >= 0;
      // 盈利=金暖渐变, 亏损=冷灰渐变
      const bgGrad = isUp
        ? `linear-gradient(135deg, rgba(255,214,10,0.08), rgba(255,159,10,0.04))`
        : `linear-gradient(135deg, rgba(255,69,58,0.06), rgba(0,0,0,0.2))`;
      const borderClr = isUp ? 'rgba(255,214,10,0.2)' : 'rgba(255,69,58,0.15)';
      pnlCards += `
      <div class="pnl-premium-card" style="background:${bgGrad};border-color:${borderClr}">
        <div class="pnl-premium-left">
          <div class="pnl-emoji">${isUp ? '📈' : '📉'}</div>
          <div>
            <div class="pnl-name">${p.name || p.code}</div>
            <div class="pnl-meta">${p.code} · ${p.shares}股 · 成本${fmt(p.buy_price, 2)}</div>
          </div>
        </div>
        <div class="pnl-premium-right">
          <div class="pnl-pct ${isUp ? 'up' : 'down'}">${isUp ? '+' : ''}${fmt(pnlPct, 1)}%</div>
          <div class="pnl-amount ${isUp ? 'up' : 'down'}">${p.profit_amount >= 0 ? '+' : ''}¥${fmt(Math.abs(p.profit_amount || 0), 0)}</div>
          <div class="pnl-weight">仓位 ${p.weight || 0}%</div>
        </div>
      </div>`;
    });
    html += `<div class="card">
      <div class="card-header"><span class="card-title">💰 持仓盈亏</span><span class="card-subtitle">${posDetails.length}只</span></div>
      <div class="card-body">${pnlCards}</div></div>`;
  }

  // 🆕 v3.5 紧凑面板：UZI + IC 一行摘要
  if (d.uzi_chain && d.uzi_chain.length) {
    const inChain = d.uzi_chain.filter(u => u.is_ai_chain);
    const topChain = d.uzi_chain.filter(u => u.is_ai_chain).slice(0, 3);
    html += `<div class="compact-info-bar">
      <span>🔗 AI链 ${inChain.length}只</span>
      <span class="compact-dim">${topChain.map(u => u.name).join(' · ')}</span>
    </div>`;
  }
  if (d.ic_analysis && d.ic_analysis.summary) {
    html += `<div class="compact-info-bar" style="margin-top:2px">
      <span>🧬 IC</span>
      <span class="compact-dim" style="font-size:11px">${d.ic_analysis.summary}</span>
    </div>`;
  }

  $('tab-overview').innerHTML = html;
}

// ── QuantDinger Consensus ────────────────────────────────────
function buildQuantDingerConsensus(d) {
  const qd = d.quantdinger_consensus || {};
  if (!qd || !qd.latest_date) {
    return `<div class="card qd-card"><div class="card-header"><span class="card-title">QuantDinger 共识</span><span class="card-subtitle">只读闸门</span></div>
      <div class="card-body"><div class="qd-empty">暂无客观共识数据</div></div></div>`;
  }

  const tone = qdTone(qd.universe_decision);
  const top = (qd.top_opportunities || []).slice(0, 3);
  const risks = (qd.risk_flags || []).slice(0, 3);

  const row = (item, kind) => {
    const itemTone = qdTone(item.consensus_decision);
    const label = qdDecisionLabel(item.consensus_decision);
    const score = fmt(item.consensus_score, 1);
    const meta = `Q ${fmt((item.quality_multiplier || 0) * 100, 0)} · A ${fmt((item.agreement_ratio || 0) * 100, 0)}`;
    return `<div class="qd-row ${kind}">
      <div class="qd-row-main">
        <span class="qd-name">${qdName(item)}</span>
        <span class="qd-code">${item.code || ''}</span>
      </div>
      <div class="qd-row-side">
        <span class="qd-row-score ${itemTone}">${score}</span>
        <span class="qd-row-meta">${label} · ${meta}</span>
      </div>
    </div>`;
  };

  const topRows = top.length
    ? top.map(item => row(item, 'buy')).join('')
    : '<div class="qd-row empty">暂无强机会</div>';
  const riskRows = risks.length
    ? risks.map(item => row(item, 'risk')).join('')
    : '<div class="qd-row empty">暂无强风险</div>';

  return `<div class="card qd-card">
    <div class="card-header">
      <span class="card-title">QuantDinger 共识</span>
      <span class="card-subtitle">${qd.latest_date} · ${qd.coverage || '--'}</span>
    </div>
    <div class="card-body">
      <div class="qd-summary">
        <div class="qd-compass ${tone}">
          <span>全局</span>
          <strong>${fmt(qd.universe_score, 1)}</strong>
          <em>${qdDecisionLabel(qd.universe_decision)}</em>
        </div>
        <div class="qd-metrics">
          <div><span>Quality</span><b>${fmt((qd.quality_multiplier || 0) * 100, 0)}%</b></div>
          <div><span>Agree</span><b>${fmt((qd.agreement_ratio || 0) * 100, 0)}%</b></div>
          <div><span>Cover</span><b>${fmt(qd.coverage_pct || 0, 0)}%</b></div>
        </div>
      </div>
      <div class="qd-lanes">
        <div class="qd-lane"><div class="qd-lane-title up">机会</div>${topRows}</div>
        <div class="qd-lane"><div class="qd-lane-title down">风险</div>${riskRows}</div>
      </div>
    </div>
  </div>`;
}

// ── Merged Actions Builder ────────────────────────────────────
function buildMergedActions(d, session) {
  const sb = d.signal_brief || {};
  const advice = d.position_advice || {};
  const items = [];

  // Session phase
  const phaseCopy = {
    premarket: ['盘前校准', `先审 ${sb.buy_count || 0} 个候选，标记触发价`],
    intraday: ['盘中执行', '只处理高置信信号和风险项'],
    midday: ['午间校准', '复核早盘异动，下午只保留最高优先级'],
    postmarket: ['盘后复盘', '记录实际执行理由，准备明日队列'],
    closed: ['休市观察', '不做新动作，只更新观察名单'],
  }[session.id] || ['今日节奏', session.focus];

  items.push({ tone: session.tone, title: phaseCopy[0], desc: phaseCopy[1], tag: session.window, tagType: 'info' });

  // Top risks
  (sb.risk_alerts || []).slice(0, 2).forEach(r => {
    items.push({ tone: 'down', title: `处理风险：${r.name}`, desc: `${actionLabel(r.action)} · 评分 ${fmt(r.score, 0)}`, tag: r.code, tagType: 'risk' });
  });

  // Top candidates from position advice
  (advice.holdings_advice || []).filter(a => a.suggest && !['HOLD', 'WATCH'].includes(a.suggest)).slice(0, 1).forEach(a => {
    const t = ['EXIT', 'REDUCE'].includes(a.suggest) ? 'down' : 'up';
    items.push({ tone: t, title: `仓位动作：${a.name}`, desc: a.reason || `${a.suggest} · ${fmt(a.profit_pct, 1)}%`, tag: a.suggest, tagType: t === 'up' ? 'buy' : 'risk' });
  });

  // Buy candidates from scores
  const scores = d.scores || [];
  const heldCodes = new Set((d.portfolio_summary || {}).position_details ? d.portfolio_summary.position_details.map(p => p.code) : []);
  const buyCandidates = scores.filter(s => !heldCodes.has(s.code) && ['STRONG_BUY', 'BUY', 'CAUTION_BUY'].includes(s.signal_action)).slice(0, 2);
  buyCandidates.forEach(s => {
    const uziText = s.uzi_score ? ` · UZI ${fmt(s.uzi_score, 0)}/${s.uzi_rating || '-'}` : '';
    items.push({ tone: 'up', title: `候选复核：${s.name}`, desc: `评分 ${fmt(s.total_score, 0)} · ${actionLabel(s.signal_action)}${uziText}`, tag: s.code, tagType: 'buy' });
  });

  const top5 = items.slice(0, 5);
  const rows = top5.map((item, i) => `
    <div class="action-item">
      <span class="action-priority">${String(i + 1).padStart(2, '0')}</span>
      <div class="action-content">
        <div class="action-title ${item.tone}">${item.title}</div>
        <div class="action-desc">${item.desc}</div>
      </div>
      <span class="action-tag ${item.tagType}">${item.tag}</span>
    </div>`).join('');

  return `<div class="card action-merged"><div class="card-header"><span class="card-title">今日行动</span><span class="card-subtitle">${top5.length} 项</span></div><div class="card-body">${rows}</div></div>`;
}

// ── Position Quick View ───────────────────────────────────────
function buildPositionQuickView(d) {
  const details = (d.portfolio_summary || {}).position_details || [];
  const scores = d.scores || [];
  const scoreMap = {};
  scores.forEach(s => { scoreMap[s.code] = s; });

  if (!details.length) return '<div class="card"><div class="card-header"><span class="card-title">持仓速览</span></div><div class="card-body"><div class="empty-state"><div class="text">暂无持仓</div></div></div></div>';

  const cards = details.map(p => {
    const isUp = (p.profit_pct || 0) >= 0;
    const sig = scoreMap[p.code] || {};
    const weight = p.weight || 0;
    return `<div class="position-quick-card">
      <div class="pq-name ${isUp ? 'up' : 'down'}">${p.name || '—'}</div>
      <div class="pq-code">${p.code || ''}</div>
      <div class="pq-pnl ${isUp ? 'up' : 'down'}"><span class="dot-row"><span class="dot-indicator dot-${isUp ? 'up' : 'down'}"></span>${(p.profit_pct >= 0 ? '+' : '') + fmt(p.profit_pct, 2)}%</span></div>
      <div class="pq-weight">权重 ${fmt(weight, 1)}% · 成本 ¥${fmt(p.buy_price)}</div>
      <span class="pq-signal ${signClass(sig.signal_action || 'HOLD')}">${sig.signal_action || 'HOLD'}</span>
    </div>`;
  }).join('');

  return `<div class="card"><div class="card-header"><span class="card-title">持仓速览</span><span class="card-subtitle">${details.length} 只</span></div><div class="card-body"><div class="position-quick-scroll">${cards}</div></div></div>`;
}

// ═══════════════════════════════════════════════════════════
// TAB 2 — 持仓
// ═══════════════════════════════════════════════════════════
function renderHoldingsTab(d) {
  if (!d) return;
  const pf = d.portfolio_summary || {};
  const details = pf.position_details || [];
  const scores = d.scores || [];
  const advice = d.position_advice || {};
  const tt = d.target_tracker || {};
  const op = d.operational_mode || {};

  let html = '';

  // ── Position Table ──────────────────────────────────────
  if (details.length) {
    html += `<div class="card"><div class="card-header"><span class="card-title">持仓明细</span><span class="card-subtitle">${details.length} 只 · 总权益 ${fmtCurrency(pf.total_value)}</span></div><div class="card-body">
      <div class="data-table-wrap"><table class="position-table"><thead><tr>
        <th>标的</th><th class="text-right">现价</th><th class="text-right">市值</th><th class="text-right">盈亏</th><th class="text-right">盈亏%</th><th class="text-right">权重</th><th>信号</th><th></th>
      </tr></thead><tbody>`;

    details.forEach(p => {
      const isUp = (p.profit_pct || 0) >= 0;
      const sig = scores.find(s => s.code === p.code) || {};
      html += `<tr>
            <td><span class="pos-name ${isUp ? 'up' : 'down'}">${p.name || '—'}</span><br><span class="pos-code">${p.code || ''}</span></td>
            <td class="text-right text-dim" style="font-family:var(--font-num)">¥${fmt(p.current_price)}</td>
            <td class="text-right" style="font-weight:600;font-family:var(--font-num)">¥${fmt(p.current_value, 0)}</td>
            <td class="text-right ${isUp ? 'up' : 'down'}" style="font-weight:600;font-family:var(--font-num)">${p.profit_amount >= 0 ? '+' : ''}¥${fmt(Math.abs(p.profit_amount || 0), 0)}</td>
            <td class="text-right ${isUp ? 'up' : 'down'}" style="font-weight:600">${(p.profit_pct >= 0 ? '+' : '') + fmt(p.profit_pct, 2)}%</td>
            <td class="text-right text-dim">${fmt(p.weight, 1)}%</td>
            <td><span class="pq-signal ${signClass(sig.signal_action || 'HOLD')}" style="font-size:9px;font-weight:600;padding:2px 6px;border-radius:3px">${sig.signal_action || 'HOLD'}</span></td>
            <td><button onclick="showConfig('${p.code}')" style="background:none;border:1px solid var(--border-color);color:var(--text-tertiary);font-size:12px;cursor:pointer;border-radius:4px;padding:2px 6px" title="设置">⚙</button></td>
      </tr>`;
    });
    html += '</tbody></table></div></div></div>';
  } else {
    html += '<div class="card"><div class="card-header"><span class="card-title">持仓明细</span></div><div class="card-body"><div class="empty-state"><div class="text">暂无持仓</div></div></div></div>';
  }

  // ── Score Ranking ───────────────────────────────────────
  if (scores.length) {
    const chips = scores.slice(0, 12).map(s => {
      const sc = s.total_score || 0;
      const color = sc >= 65 ? 'var(--up)' : sc >= 50 ? 'var(--gold)' : 'var(--down)';
      const uzi = s.uzi_score || 0;
      const uziColor = uzi >= 65 ? 'var(--up)' : uzi >= 45 ? 'var(--gold)' : 'var(--text-tertiary)';
      const trap = s.uzi_trap_count || 0;
      return `<div class="score-chip">
        <div class="sc-rank">#${s.rank || '-'}</div>
        <div class="sc-name">${s.name}</div>
        <div class="sc-value" style="color:${color}">${fmt(sc, 0)}</div>
        <div class="sc-uzi" style="color:${uziColor}">UZI ${fmt(uzi, 0)} · ${s.uzi_rating || '-'}</div>
        <div class="sc-tier">${s.uzi_chain_tier || '未分层'}${trap ? ` · 陷阱${trap}` : ''}</div>
      </div>`;
    }).join('');
    html += `<div class="card"><div class="card-header"><span class="card-title">评分排行</span><span class="card-subtitle">${scores.length} 只标的</span></div><div class="card-body"><div class="score-hscroll">${chips}</div></div></div>`;
  }

  // ── Position Advice ─────────────────────────────────────
  if (advice.holdings_advice && advice.holdings_advice.length) {
    const rows = advice.holdings_advice.map(a => {
      const suggestMap = { ADD:{l:'加仓',c:'ADD'}, REDUCE:{l:'减仓',c:'REDUCE'}, EXIT:{l:'清仓',c:'EXIT'}, TAKE_PARTIAL:{l:'止盈',c:'TAKE_PAR'}, TAKE_PAR:{l:'止盈',c:'TAKE_PAR'}, WATCH:{l:'观察',c:'WATCH'}, HOLD:{l:'持有',c:'HOLD'} };
      const sm = suggestMap[a.suggest] || { l: a.suggest, c: 'HOLD' };
      return `<div class="advice-row">
        <div><div class="advice-name ${(a.profit_pct || 0) >= 0 ? 'up' : 'down'}">${a.name}</div><div class="advice-reason">${a.reason || ''}</div></div>
        <div class="advice-meta"><span class="advice-tag ${sm.c}">${sm.l}</span>${a.kelly_max_amount > 0 ? `<div class="advice-kelly">Kelly ¥${fmt(a.kelly_max_amount, 0)}</div>` : ''}</div>
      </div>`;
    }).join('');
    html += `<div class="card"><div class="card-header"><span class="card-title">仓位建议</span><span class="card-subtitle">Kelly + 信号强度</span></div><div class="card-body"><div class="advice-list">${rows}</div></div></div>`;
  }

  // ── Target Tracker + OpMode ─────────────────────────────
  const progress = Math.min(100, tt.progress_pct || 0);
  const monthlyReq = tt.required_monthly_return || 0;
  const modeLabels = { mean_revert:'均值回归', trend:'趋势跟踪', neutral:'中性' };

  html += `<div class="card"><div class="card-header"><span class="card-title">目标与模式</span></div><div class="card-body">
    <div class="target-compact">
      <div class="tc-header"><span>目标进度 <strong class="gold">${progress.toFixed(1)}%</strong></span><span class="text-dim">${tt.days_elapsed || 0}/${tt.days_total || 90}天</span></div>
      <div class="tc-bar"><div class="tc-fill" style="width:${progress}%"></div></div>
      <div class="tc-footer"><span>${fmtCurrency(tt.initial_capital)}</span><span>${fmtCurrency(tt.target_capital)}</span></div>
    </div>
    <div class="opmode-row">
      <div class="om-item"><span class="om-label">模式</span><span class="om-value gold">${modeLabels[op.mode] || op.mode || '中性'}</span></div>
      <div class="om-item"><span class="om-label">因子翻转</span><span class="om-value" style="color:${op.factor_invert ? 'var(--accent-orange)' : 'var(--text-tertiary)'}">${op.factor_invert ? 'ON' : 'OFF'}</span></div>
      <div class="om-item"><span class="om-label">卖出触发</span><span class="om-value gold">${((op.sell_trigger_weight || 1) * 100).toFixed(0)}%</span></div>
      <div class="om-item"><span class="om-label">阶段</span><span class="om-value">${op.regime_label || '—'}</span></div>
    </div>
  </div></div>`;

  $('tab-holdings').innerHTML = html;
}

// ═══════════════════════════════════════════════════════════
// TAB 3 — 风控
// ═══════════════════════════════════════════════════════════
function renderRiskTab(d) {
  if (!d) return;
  const pf = d.portfolio_summary || {};
  const sb = d.signal_brief || {};
  const pnl = pf.total_profit_pct || 0;
  const cashRatio = pf.total_value ? ((pf.cash || 0) / pf.total_value * 100).toFixed(0) : 0;

  let html = '';

  // ── Risk Gauges ─────────────────────────────────────────
  html += `<div class="card"><div class="card-header"><span class="card-title">风控仪表</span></div><div class="card-body">
    <div class="risk-gauges">
      <div class="risk-gauge">
        <div class="rg-label">日收益率</div>
        <div class="rg-value ${clsPct(pnl)}">${pctStr(pnl)}</div>
      </div>
      <div class="risk-gauge">
        <div class="rg-label">持仓数</div>
        <div class="rg-value gold">${pf.positions || 0}</div>
        <div class="rg-limit">最多 10 只</div>
      </div>
      <div class="risk-gauge">
        <div class="rg-label">风险信号</div>
        <div class="rg-value" style="color:${sb.risk_count > 0 ? 'var(--down)' : 'var(--up)'}">${sb.risk_count || 0}</div>
        <div class="rg-limit">需关注</div>
      </div>
      <div class="risk-gauge">
        <div class="rg-label">现金比</div>
        <div class="rg-value">${cashRatio}%</div>
        <div class="rg-limit">${fmtCurrency(pf.cash)}</div>
      </div>
    </div></div></div>`;

  // ── NAV Chart ───────────────────────────────────────────
  html += `<div class="card" id="nav-card">
    <div class="card-header"><span class="card-title">净值曲线</span></div>
    <div class="card-body">
      <div class="chart-container"><canvas id="navChart"></canvas></div>
      <div class="nav-stats" id="nav-chart-stats">
        <div class="nav-stat"><span class="nav-stat-label">起始</span><span class="nav-stat-value" id="nav-start">--</span></div>
        <div class="nav-stat"><span class="nav-stat-label">最新</span><span class="nav-stat-value" id="nav-end">--</span></div>
        <div class="nav-stat"><span class="nav-stat-label">收益率</span><span class="nav-stat-value" id="nav-return">--</span></div>
        <div class="nav-stat"><span class="nav-stat-label">最高</span><span class="nav-stat-value" id="nav-high">--</span></div>
        <div class="nav-stat"><span class="nav-stat-label">最低</span><span class="nav-stat-value" id="nav-low">--</span></div>
      </div>
    </div></div>`;

  // ── Paper Account ───────────────────────────────────────
  html += `<div class="card" id="paper-card"><div class="card-header"><span class="card-title">纸面模拟</span><span class="card-subtitle">无风险验证</span></div><div class="card-body" id="paper-content"><div class="empty-state"><div class="text">加载中...</div></div></div></div>`;

  // ── Anomaly Alerts ──────────────────────────────────────
  html += `<div class="card" id="anomaly-card"><div class="card-header"><span class="card-title">异动告警</span></div><div class="card-body" id="anomaly-content"><div class="empty-state"><div class="text">加载中...</div></div></div></div>`;

  // ── Signal Performance ──────────────────────────────────
  html += `<div class="card" id="signal-perf-card"><div class="card-header"><span class="card-title">信号绩效</span><span class="card-subtitle">全部历史</span></div><div class="card-body" id="signal-perf-content"><div class="empty-state"><div class="text">加载中...</div></div></div></div>`;

  // ── Compare ──────────────────────────────────────────
  html += `<div class="card" id="compare-card"><div class="card-header"><span class="card-title">账户对比</span><span class="card-subtitle">纸面 vs 真实 vs 沪深300</span></div><div class="card-body" id="compare-content"><div class="empty-state"><div class="text">加载中...</div></div></div></div>`;

  // ── Risk Matrix ───────────────────────────────────────
  html += `<div class="card" id="risk-matrix-card"><div class="card-header"><span class="card-title">风险矩阵</span><span class="card-subtitle">VaR · 相关性 · 压力测试</span></div><div class="card-body" id="risk-matrix-content"><div class="empty-state"><div class="text">加载中...</div></div></div></div>`;

  // ── Backtest ──────────────────────────────────────────
  html += `<div class="card" id="backtest-card"><div class="card-header"><span class="card-title">策略回测</span><span class="card-subtitle">多策略对比</span></div><div class="card-body" id="backtest-content"><div class="empty-state"><div class="text">加载中...</div></div></div></div>`;

  // ── Factor IC ──────────────────────────────────────────
  html += `<div class="card" id="factor-ic-card"><div class="card-header"><span class="card-title">因子有效性</span><span class="card-subtitle">Rank IC 归因</span></div><div class="card-body" id="factor-ic-content"><div class="empty-state"><div class="text">加载中...</div></div></div></div>`;

  // ── Trading Journal ─────────────────────────────────────
  html += `<div class="card" id="journal-card"><div class="card-header"><span class="card-title">交易日志</span></div><div class="card-body" id="journal-content"><div class="empty-state"><div class="text">加载中...</div></div></div></div>`;

  $('tab-risk').innerHTML = html;

  // Async loads
  loadNavHistory();
  setTimeout(loadPaperAccount, 50);
  setTimeout(loadAnomalyData, 100);
  setTimeout(loadSignalPerformance, 200);
  setTimeout(loadJournal, 300);
  setTimeout(loadBacktest, 50);
  setTimeout(loadFactorIC, 100);
  setTimeout(loadCompare, 30);
  setTimeout(loadRiskMatrix, 60);
}

// ═══════════════════════════════════════════════════════════
// ASYNC DATA LOADERS
// ═══════════════════════════════════════════════════════════

// ─── LTTB 降采样 ──────────────────────────────────────────────
function lttbDownsample(data, threshold) {
  const len = data.length;
  if (threshold >= len || threshold <= 2) return data;
  const sampled = [data[0]];
  const bucketSize = (len - 2) / (threshold - 2);
  let a = 0;
  for (let i = 0; i < threshold - 2; i++) {
    const bucketStart = Math.floor((i + 0) * bucketSize) + 1;
    const bucketEnd = Math.floor((i + 1) * bucketSize) + 1;
    const avgRangeEnd = Math.min(bucketEnd, len - 1);
    let avgX = 0, avgY = 0, avgCount = 0;
    for (let j = bucketStart; j < avgRangeEnd; j++) { avgX += data[j].x; avgY += data[j].y; avgCount++; }
    if (avgCount === 0) continue;
    avgX /= avgCount; avgY /= avgCount;
    let maxArea = -1, maxAreaIdx = bucketStart;
    const bucketEndActual = Math.min(bucketEnd, len - 1);
    for (let j = bucketStart; j < bucketEndActual; j++) {
      const area = Math.abs((data[a].x - data[j].x) * (avgY - data[a].y) - (data[a].x - avgX) * (data[j].y - data[a].y));
      if (area > maxArea) { maxArea = area; maxAreaIdx = j; }
    }
    sampled.push(data[maxAreaIdx]);
    a = maxAreaIdx;
  }
  sampled.push(data[len - 1]);
  return sampled;
}

function downsampleNavData(data) {
  const threshold = 500;
  if (!data || data.length <= threshold) return data;
  const mapped = data.map((d, i) => ({ x: i, y: d.value || 0, date: d.date, profit_pct: d.profit_pct || 0 }));
  const sampled = lttbDownsample(mapped, threshold);
  return sampled.map(s => ({ date: s.date, value: s.y, profit_pct: s.profit_pct }));
}

// ─── NAV History (Chart.js) ────────────────────────────────────
function loadNavHistory() {
  fetch('/api/nav-history')
    .then(r => r.json())
    .then(d => {
      if (!d.ok || !d.data || !d.data.length) return;
      const sampled = downsampleNavData(d.data);
      renderNavChart(sampled);
    })
    .catch(() => {});
}

const debouncedResize = debounce(function() {
  if (STATE.chartInstance) STATE.chartInstance.resize();
}, 250);
window.addEventListener('resize', debouncedResize);

function renderNavChart(data) {
  const canvas = $('navChart');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  if (STATE.chartInstance) { STATE.chartInstance.destroy(); STATE.chartInstance = null; }

  const dates = data.map(r => r.date);
  const values = data.map(r => r.value || 0);
  const pcts = data.map(r => r.profit_pct || 0);
  const startVal = values[0], endVal = values[values.length - 1];
  const maxVal = Math.max(...values), minVal = Math.min(...values);
  const totalReturn = pcts[pcts.length - 1] || 0;

  const setStat = (id, val, cls) => { const el = $(id); if (el) { el.textContent = val; el.className = 'nav-stat-value' + (cls ? ' ' + cls : ''); } };
  setStat('nav-start', '¥' + fmt(startVal, 0));
  setStat('nav-end', '¥' + fmt(endVal, 0));
  setStat('nav-return', (totalReturn >= 0 ? '+' : '') + fmt(totalReturn, 2) + '%', totalReturn >= 0 ? 'up' : 'down');
  setStat('nav-high', '¥' + fmt(maxVal, 0));
  setStat('nav-low', '¥' + fmt(minVal, 0));

  STATE.chartInstance = new Chart(ctx, {
    type: 'line',
    data: {
      labels: dates,
      datasets: [{
        label: '净值', data: values,
        borderColor: cssToken('--gold', '#d9b84f'),
        backgroundColor: function(context) {
          const { ctx, chartArea } = context.chart;
          if (!chartArea) return 'rgba(255,215,0,0.06)';
          const gradient = ctx.createLinearGradient(0, chartArea.top, 0, chartArea.bottom);
          gradient.addColorStop(0, 'rgba(255,215,0,0.12)');
          gradient.addColorStop(1, 'rgba(255,215,0,0.0)');
          return gradient;
        },
        fill: true, borderWidth: 2, pointRadius: 0, pointHoverRadius: 4,
        pointHoverBackgroundColor: cssToken('--gold', '#d9b84f'), pointHoverBorderColor: cssToken('--bg-root', '#000'), pointHoverBorderWidth: 2,
        tension: 0.05,
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { intersect: false, mode: 'index' },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: '#1a1d24', titleColor: '#E8EAED', bodyColor: '#E8EAED',
          borderColor: 'rgba(255,255,255,0.08)', borderWidth: 1, padding: 10, displayColors: false,
          callbacks: { title: items => items[0].label, label: item => '¥' + fmt(item.raw, 0) }
        },
      },
      scales: {
        x: {
          grid: { color: 'rgba(255,255,255,0.03)', drawBorder: false },
          ticks: { color: 'rgba(255,255,255,0.2)', maxTicksLimit: 8, font: { size: 9, family: 'SF Mono, monospace' } },
        },
        y: {
          grid: { color: 'rgba(255,255,255,0.03)', drawBorder: false },
          ticks: { color: 'rgba(255,255,255,0.2)', font: { size: 9, family: 'SF Mono, monospace' }, callback: v => '¥' + Number(v).toFixed(0) },
        },
      },
    }
  });
}

// ─── Anomaly Data ──────────────────────────────────────────────
function loadAnomalyData() {
  const el = $('anomaly-content'); if (!el) return;
  fetchJSON('/api/anomalies')
    .then(d => {
      if (!d.ok || !d.anomalies || !d.anomalies.length) {
        el.innerHTML = '<div class="empty-state"><div class="text">暂无未确认异动 ✅</div></div>'; return;
      }
      const items = d.anomalies.slice(0, 8).map(a => {
        const time = a.created_at ? a.created_at.replace('T', ' ').slice(0, 16) : '—';
        return `<div class="anomaly-compact level-${a.level}">
          <span class="anom-badge ${a.level}">${a.level}级</span>
          <div class="anom-body"><div class="anom-name">${a.code || '—'} ${a.name || ''}</div><div class="anom-msg">${a.message || ''}</div></div>
          <span class="anom-time">${time}</span>
        </div>`;
      }).join('');
      el.innerHTML = `<div style="font-size:11px;color:var(--text-tertiary);margin-bottom:6px">共 ${d.count || d.anomalies.length} 条未确认</div>${items}`;
    })
    .catch(error => { el.innerHTML = componentError(`异动数据加载失败：${error.message}`, 'loadAnomalyData'); });
}

// ─── Signal Performance ───────────────────────────────────────
function loadSignalPerformance() {
  const el = $('signal-perf-content'); if (!el) return;
  fetchJSON('/api/signal-performance')
    .then(d => {
      if (!d.ok || !d.signal_actions || !d.signal_actions.length) { el.innerHTML = '<div class="empty-state"><div class="text">暂无数据</div></div>'; return; }
      const s = d.summary || {};
      let html = `<div style="display:flex;justify-content:space-between;font-size:10px;color:var(--text-tertiary);margin-bottom:8px">
        <span>信号 ${s.total_signals || 0} | 已结算 ${s.with_outcome || 0}</span>
        <span>胜率 ${s.overall_win_rate != null ? fmt(s.overall_win_rate * 100, 1) + '%' : 'N/A'} | 均收益 ${s.overall_avg_return != null ? fmt(s.overall_avg_return, 2) + '%' : 'N/A'}</span>
      </div>`;
      html += '<div class="data-table-wrap"><table class="data-table"><thead><tr><th>信号</th><th class="text-right">次数</th><th class="text-right">1日收益</th><th class="text-right">1日胜率</th><th class="text-right">3日胜率</th></tr></thead><tbody>';
      d.signal_actions.forEach(sa => {
        const ar1 = sa.avg_return_1d != null ? fmt(sa.avg_return_1d, 2) + '%' : 'N/A';
        const wr1 = sa.win_rate_1d != null ? fmt(sa.win_rate_1d * 100, 1) + '%' : 'N/A';
        const wr3 = sa.win_rate_3d != null ? fmt(sa.win_rate_3d * 100, 1) + '%' : 'N/A';
        html += `<tr><td style="font-weight:500">${sa.action}</td><td class="text-right">${sa.total}</td><td class="text-right ${sa.avg_return_1d >= 0 ? 'up' : 'down'}">${ar1}</td><td class="text-right ${sa.win_rate_1d >= 0.4 ? 'up' : (sa.win_rate_1d >= 0.3 ? 'gold' : 'down')}">${wr1}</td><td class="text-right">${wr3}</td></tr>`;
      });
      html += '</tbody></table></div>';
      el.innerHTML = html;
    })
    .catch(error => { el.innerHTML = componentError(`信号统计加载失败：${error.message}`, 'loadSignalPerformance'); });
}

// ─── Journal ──────────────────────────────────────────────────
function loadJournal() {
  const el = $('journal-content'); if (!el) return;
  fetchJSON('/api/journal')
    .then(d => {
      if (!d.ok) { el.innerHTML = '<div class="empty-state"><div class="text">暂无数据</div></div>'; return; }
      const entries = d.entries || [];
      const stats = d.stats || {};
      let html = `<div style="display:flex;justify-content:space-between;font-size:10px;color:var(--text-tertiary);margin-bottom:8px">
        <span>总计 <strong class="gold">${stats.total || 0}</strong> 条</span>
        <span style="color:${(stats.no_reflection || 0) > 0 ? 'var(--accent-orange)' : 'var(--up)'}">${(stats.no_reflection || 0) > 0 ? '📝 ' + stats.no_reflection + ' 条未反思' : '✅ 全部已反思'}</span>
      </div>`;
      if (!entries.length) { el.innerHTML = html + '<div class="empty-state"><div class="text">暂无交易日志</div></div>'; return; }
      html += '<div class="data-table-wrap"><table class="data-table"><thead><tr><th>交易</th><th>标的</th><th class="text-right">盈亏</th><th class="text-center">反思</th></tr></thead><tbody>';
      entries.slice(0, 6).forEach(e => {
        const icon = e.action === 'buy' ? '🟢' : '🔴';
        let profitStr = '—', profitCls = '';
        if (e.profit_pct != null) { profitStr = (e.profit_pct >= 0 ? '+' : '') + fmt(e.profit_pct, 2) + '%'; profitCls = e.profit_pct >= 0 ? 'up' : 'down'; }
        const hasReflection = e.reflection && e.reflection.trim() !== '';
        html += `<tr><td>${icon}</td><td style="font-weight:500">${e.name}<div style="font-size:9px;color:var(--text-tertiary)">${e.date || ''}</div></td><td class="text-right ${profitCls}" style="font-weight:600">${profitStr}</td><td class="text-center">${hasReflection ? '✅' : '⬜'}</td></tr>`;
      });
      html += '</tbody></table></div>';
      el.innerHTML = html;
    })
    .catch(error => { el.innerHTML = componentError(`交易日志加载失败：${error.message}`, 'loadJournal'); });
}

// ═══════════════════════════════════════════════════════════
// SENTINEL TAB
// ═══════════════════════════════════════════════════════════
function renderSentinelTab(d) {
  $('tab-sentinel').innerHTML = dashboardSkeleton(4);
}

function loadSentinelData() {
  // Load fusion + status + research in parallel
  Promise.all([
    fetch('/api/sentinel/status').then(r => r.json()),
    fetch('/api/sentinel/fusion').then(r => r.json()),
    fetch('/api/research/brief').then(r => r.json()).catch(() => ({ok:false}))
  ]).then(([statusD, fusionD, researchD]) => {
    if (!statusD.ok && !fusionD.ok) { $('tab-sentinel').innerHTML = '<div class="error-state">哨兵数据暂不可用</div>'; return; }

    const sources = statusD.sources || [];
    const obs = (statusD.observations || []).filter(o => o.signal_type === 'bullish' || o.signal_type === 'bearish');
    const fusion = (fusionD.fusion || []).filter(f => f.source_count > 0);

    let html = '';

    // ═══ Research Brief (top of sentinel tab) ══════════
    if (researchD && researchD.ok && researchD.topics && researchD.topics.length > 0) {
      html += '<div class="card" style="border-color:rgba(10,132,255,0.2);background:rgba(10,132,255,0.02)">';
      html += '<div class="card-header"><span class="card-title">今日研究</span><span class="card-subtitle">' + (researchD.topics.length || 0) + '个话题·自主采集</span></div>';
      html += '<div class="card-body"><div class="research-topics">';
      researchD.topics.slice(0, 6).forEach(t => {
        const tickers = t.mapping ? t.mapping.tickers : [];
        html += '<div class="rt-row"><span class="rt-topic">' + t.topic + '</span>';
        html += '<span class="rt-count">' + t.count + '次</span>';
        html += '<span class="rt-sector">' + (t.mapping ? t.mapping.sector : '') + '</span>';
        if (tickers.length) html += '<span class="rt-tickers">' + tickers.slice(0,3).map(function(tk){return '<span class="ssig-tk">'+tk+'</span>'}).join('') + '</span>';
        html += '</div>';
      });
      html += '</div></div></div>';
    }

    // ═══ Top: Key Fusion Impact (curated) ═══════════════
    const highImpact = fusion.filter(f => Math.abs(f.bonus) >= 0.3).sort((a,b) => Math.abs(b.bonus) - Math.abs(a.bonus));
    if (highImpact.length > 0) {
      html += '<div class="card sentinel-highlight"><div class="card-header"><span class="card-title">关键影响</span><span class="card-subtitle">多源共振信号</span></div><div class="card-body">';
      html += '<div class="sentinel-fusion-list">';
      highImpact.forEach(f => {
        const impactCls = f.bonus > 0 ? 'up' : 'down';
        const arrow = f.bonus > 0 ? '↑' : '↓';
        const sources = [...new Set((f.signals || []).map(s => s.source))].join(' · ');
        const topSignals = (f.signals || []).filter(s => s.direction !== 'neutral').slice(0, 3);
        html += '<div class="sentinel-fusion-item">'
          + '<div class="sfi-main"><span class="sfi-arrow ' + impactCls + '">' + arrow + '</span>'
          + '<span class="sfi-name">' + f.name + '</span><span class="sfi-code">' + f.code + '</span></div>'
          + '<div class="sfi-impact ' + impactCls + '">' + (f.bonus >= 0 ? '+' : '') + fmt(f.bonus, 1) + '分</div>'
          + '<div class="sfi-sources">' + f.source_count + '源 · ' + sources + '</div>'
          + (topSignals.length ? '<div class="sfi-quotes">' + topSignals.map(s => '<span class="sfi-quote">"' + (s.content || '').substring(0, 50) + '"</span>').join('') + '</div>' : '')
          + '</div>';
      });
      html += '</div></div></div>';
    } else {
      html += '<div class="card"><div class="card-header"><span class="card-title">关键影响</span></div><div class="card-body"><div class="empty-state"><div class="text">暂无显著共振信号</div></div></div></div>';
    }

    // ═══ Middle: Source Quality + Recent Signals ═════════
    html += '<div class="sentinel-row">';

    // Sources (compact, accuracy-sorted)
    const rankedSources = [...sources].sort((a,b) => b.accuracy - a.accuracy);
    html += '<div class="card sentinel-col"><div class="card-header"><span class="card-title">信源质量</span><span class="card-subtitle">' + sources.length + '人·准确率排序</span></div><div class="card-body"><div class="sentinel-sources-mini">';
    rankedSources.forEach(s => {
      const hasAcc = s.total_predictions >= 3;
      const accCls = hasAcc ? (s.accuracy >= 60 ? 'up' : (s.accuracy >= 40 ? 'gold' : 'down')) : '';
      const accText = hasAcc ? s.accuracy + '%' : '—';
      html += '<div class="ssm-row">'
        + '<span class="ssm-name">' + s.name + '</span>'
        + '<span class="ssm-acc ' + accCls + '">' + accText + '</span>'
        + '<span class="ssm-count">' + (s.total_predictions || 0) + '次</span>'
        + '</div>';
    });
    html += '</div></div></div>';

    // Recent actionable signals
    html += '<div class="card sentinel-col"><div class="card-header"><span class="card-title">最新信号</span><span class="card-subtitle">' + obs.length + '条·近72h</span></div><div class="card-body">';
    if (obs.length === 0) {
      html += '<div class="empty-state"><div class="text">暂无可操作信号</div></div>';
    } else {
      html += '<div class="sentinel-signals-mini">';
      obs.slice(0, 8).forEach(o => {
        const isBull = o.signal_type === 'bullish';
        const tickers = safeJSON(o.tickers);
        const topics = safeJSON(o.topics);
        const src = sources.find(s => s.id === o.source_id);
        html += '<div class="ssig-row">'
          + '<div class="ssig-head"><span class="ssig-dot ' + (isBull ? 'dot-up' : 'dot-down') + '" style="display:inline-block;width:6px;height:6px;border-radius:50%;margin-right:4px"></span>'
          + '<span class="ssig-src">' + (src ? src.name : o.source_id) + '</span>'
          + '<span class="ssig-time">' + (o.fetched_at || '').slice(5, 16) + '</span></div>'
          + '<div class="ssig-msg">' + (o.content || '').substring(0, 80) + '</div>'
          + (tickers.length ? '<div class="ssig-tickers">' + tickers.map(t => '<span class="ssig-tk">' + t + '</span>').join('') + '</div>' : '')
          + '</div>';
      });
      html += '</div>';
    }
    html += '</div></div>';
    html += '</div>'; // sentinel-row

    // ═══ Bottom: Full Fusion Table (collapsible) ════════
    const allFusion = fusion.filter(f => f.source_count > 0);
    if (allFusion.length > highImpact.length) {
      html += '<div class="card"><div class="card-header"><span class="card-title">完整影响矩阵</span><span class="card-subtitle">' + allFusion.length + '标的</span></div><div class="card-body">';
      html += '<div class="sentinel-fusion-list">';
      allFusion.forEach(f => {
        const impactCls = f.bonus > 0 ? 'up' : (f.bonus < 0 ? 'down' : '');
        html += '<div class="sentinel-fusion-item sentinel-fusion-compact">'
          + '<span class="sfi-name">' + f.name + '</span>'
          + '<span class="sfi-code">' + f.code + '</span>'
          + '<span class="sfi-impact ' + impactCls + '">' + (f.bonus >= 0 ? '+' : '') + fmt(f.bonus, 1) + '</span>'
          + '<span class="sfi-sources">' + f.source_count + '源</span>'
          + '</div>';
      });
      html += '</div></div></div>';
    }

    $('tab-sentinel').innerHTML = html;
  }).catch(error => { $('tab-sentinel').innerHTML = componentError(`哨兵数据加载失败：${error.message}`, 'loadSentinelData'); });
}

function safeJSON(v) {
  if (!v) return [];
  try { return typeof v === 'string' ? JSON.parse(v) : v; } catch(e) { return []; }
}

function loadSentinelFusion() {}  // merged into loadSentinelData
function loadSentinelPerformance() {}  // merged into loadSentinelData

// ═══════════════════════════════════════════════════════════
// PAPER TRADING
// ═══════════════════════════════════════════════════════════
function loadPaperAccount() {
  const el = $('paper-content'); if (!el) return;
  fetchJSON('/api/paper-portfolio')
    .then(d => {
      if (!d.ok) { el.innerHTML = '<div class="empty-state"><div class="text">模拟数据暂不可用</div></div>'; return; }
      const pf = d.portfolio || {};
      const cmp = d.compare || {};
      const stats = d.stats || {};
      const diffCls = (cmp.diff_return || 0) >= 0 ? 'up' : 'down';

      el.innerHTML = `
        <div style="display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin-bottom:8px">
          <div style="text-align:center;padding:8px;background:var(--bg-card);border-radius:8px">
            <div style="font-size:10px;color:var(--text-tertiary)">模拟权益</div>
            <div style="font-size:18px;font-weight:700;font-family:var(--font-num)">¥${fmt(pf.total_value, 0)}</div>
          </div>
          <div style="text-align:center;padding:8px;background:var(--bg-card);border-radius:8px">
            <div style="font-size:10px;color:var(--text-tertiary)">模拟收益</div>
            <div class="${clsPct(pf.total_profit_pct)}" style="font-size:18px;font-weight:700;font-family:var(--font-num)">${(pf.total_profit_pct >= 0 ? '+' : '') + fmt(pf.total_profit_pct, 2)}%</div>
          </div>
        </div>
        <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--text-tertiary);margin-bottom:8px">
          <span>持仓 ${pf.position_count || 0}只 · 现金 ¥${fmt(pf.cash, 0)}</span>
          <span>交易 ${stats.total_trades || 0}笔 (${stats.buys || 0}B/${stats.sells || 0}S)</span>
        </div>
        <div style="font-size:10px;color:var(--text-tertiary);padding:6px 8px;background:var(--bg-card);border-radius:6px;display:flex;justify-content:space-between">
          <span>vs 实际</span>
          <span class="${diffCls}" style="font-weight:600">${(cmp.diff_return >= 0 ? '+' : '') + fmt(cmp.diff_return, 2)}%</span>
          <span class="${(cmp.diff_amount >= 0 ? 'up' : 'down')}" style="font-weight:600">${(cmp.diff_amount >= 0 ? '+' : '')}¥${fmt(Math.abs(cmp.diff_amount), 0)}</span>
        </div>`;
    })
    .catch(error => { el.innerHTML = componentError(`模拟账户加载失败：${error.message}`, 'loadPaperAccount'); });
}

// ═══════════════════════════════════════════════════════════
// NL QUERY
// ═══════════════════════════════════════════════════════════
function doNLQuery() {
  const input = document.getElementById('nl-query-input');
  const resultEl = document.getElementById('nl-result');
  const q = (input && input.value || '').trim();
  if (!q) { if (resultEl) resultEl.innerHTML = ''; return; }

  if (resultEl) resultEl.innerHTML = '<div style="text-align:center;padding:12px;color:var(--text-tertiary)">⟳ 查询中...</div>';

  fetch(`/api/nl-query?q=${encodeURIComponent(q)}&token=${getWriteToken()}`)
    .then(r => r.json())
    .then(d => {
      if (!d.ok) { if (resultEl) resultEl.innerHTML = `<div class="nl-result-card"><div style="color:var(--text-tertiary)">${d.error || '查询失败'}</div></div>`; return; }
      if (resultEl) resultEl.innerHTML = renderNLResult(d);
    })
    .catch(() => { if (resultEl) resultEl.innerHTML = '<div class="nl-result-card"><div style="color:var(--text-tertiary)">网络错误</div></div>'; });
}

function renderNLResult(d) {
  const intent = d.intent;
  let html = `<div class="nl-result-card"><div class="nl-result-answer">${d.answer || ''}</div>`;

  if (intent === 'sell' && d.sells && d.sells.length) {
    html += '<div class="nl-result-list">' + d.sells.map(s => `<div class="nl-result-item"><span class="nl-item-name">${s.name}</span><span class="nl-item-code">${s.code}</span><span class="nl-item-reason text-dim">${(s.reason || []).slice(0,2).join(' ')}</span><span class="nl-item-pnl ${(s.pnl||0) >= 0 ? 'up' : 'down'}">${(s.pnl >= 0 ? '+' : '') + fmt(s.pnl, 1)}%</span></div>`).join('') + '</div>';
  } else if (intent === 'buy' && d.buys && d.buys.length) {
    html += '<div class="nl-result-list">' + d.buys.map(b => `<div class="nl-result-item"><span class="nl-item-name">${b.name}</span><span class="nl-item-code">${b.code}</span><span class="nl-item-score up">${fmt(b.score, 0)}分</span><span class="text-dim" style="font-size:10px">${b.reason || ''}</span></div>`).join('') + '</div>';
  } else if (intent === 'pnl' && d.positions && d.positions.length) {
    html += `<div class="nl-result-list"><div class="nl-result-item" style="font-weight:600"><span>总盈亏</span><span class="${(d.total_pnl_pct||0) >= 0 ? 'up' : 'down'}" style="font-size:15px">${(d.total_pnl_pct >= 0 ? '+' : '') + fmt(d.total_pnl_pct, 1)}%</span></div>` +
      d.positions.map(p => `<div class="nl-result-item"><span class="nl-item-name">${p.name}</span><span class="nl-item-code">${p.code}</span><span class="${(p.pnl_pct||0) >= 0 ? 'up' : 'down'}">${(p.pnl_pct >= 0 ? '+' : '') + fmt(p.pnl_pct, 1)}%</span></div>`).join('') + '</div>';
  } else if (intent === 'alert' && d.alerts && d.alerts.length) {
    html += `<div class="nl-result-list"><div class="nl-result-item" style="color:var(--accent-orange);font-weight:600">${d.emergency || 0} 条紧急告警</div>` +
      d.alerts.map(a => `<div class="nl-result-item"><span class="nl-item-name">${a.name}</span><span class="nl-item-code">${a.code}</span><span class="text-dim" style="font-size:10px">${a.msg || ''}</span></div>`).join('') + '</div>';
  } else if (d.details) {
    const dt = d.details;
    html += `<div class="nl-result-list"><div class="nl-result-item"><span>持仓</span><span class="gold">${dt.positions || 0}只</span></div><div class="nl-result-item"><span>买入候选</span><span class="up">${dt.buy_candidates || 0}只</span></div><div class="nl-result-item"><span>卖出候选</span><span class="down">${dt.sell_candidates || 0}只</span></div><div class="nl-result-item"><span>预警</span><span style="color:var(--accent-orange)">${dt.alerts || 0}条</span></div></div>`;
  }

  html += '</div>';
  return html;
}

// ═══════════════════════════════════════════════════════════
// MODALS
// ═══════════════════════════════════════════════════════════
function showTrade() {
  fetch('/api/monitor-data').then(r => r.json()).then(d => {
    const scores = d.data.scores || [];
    const options = scores.map(s => `<option value="${s.code}">${s.name} (${s.code}) 评分:${fmt(s.total_score || 0, 1)}</option>`).join('');
    showModal(`
      <div class="modal-overlay" onclick="closeModal(event)"><div class="modal-box" onclick="event.stopPropagation()">
        <div class="modal-title">调仓操作</div>
        <form class="modal-form" onsubmit="submitTrade(event)">
          <select name="code">${options}</select>
          <select name="action"><option value="buy">买入</option><option value="sell">卖出</option></select>
          <input name="price" type="number" step="0.01" placeholder="成交价格" required>
          <input name="qty" type="number" step="1" placeholder="数量(股)" required>
          <input name="note" placeholder="备注(可选)">
          <button type="submit" class="modal-btn modal-btn-primary">确认提交</button>
        </form>
      </div></div>`);
  });
}

function submitTrade(e) {
  e.preventDefault(); const f = e.target;
  fetch('/api/trades', { method:'POST', headers:writeHeaders({'Content-Type':'application/json'}), body:JSON.stringify({ code:f.code.value, action:f.action.value, price:parseFloat(f.price.value), quantity:parseInt(f.qty.value), note:f.note.value }) })
    .then(r => r.json()).then(d => { alert(d.ok ? '✅ ' + d.msg : '❌ ' + d.msg); closeModal(); refresh(); });
}

function showConfig(code) {
  // Fetch current config for this stock and pre-fill
  fetch(`/api/config/${code}`).then(r => r.json()).then(d => {
    const cfg = (d.ok && d.data) ? d.data : {};
    showModal(`
      <div class="modal-overlay" onclick="closeModal(event)"><div class="modal-box" onclick="event.stopPropagation()">
        <div class="modal-title">⚙️ ${cfg.name || code} 设置</div>
        <form class="modal-form" onsubmit="submitConfig(event)">
          <input type="hidden" name="code" value="${code}">
          <label>成本价</label>
          <input name="buy_price" type="number" step="0.01" value="${cfg.buy_price || ''}" placeholder="成本价">
          <label>止损价</label>
          <input name="stop_loss" type="number" step="0.01" value="${cfg.stop_loss || ''}" placeholder="止损价">
          <label>止盈目标上限</label>
          <input name="target_high" type="number" step="0.01" value="${cfg.target_high || ''}" placeholder="止盈目标上限">
          <label>止盈目标下限</label>
          <input name="target_low" type="number" step="0.01" value="${cfg.target_low || ''}" placeholder="止盈目标下限">
          <button type="submit" class="modal-btn modal-btn-danger">保存设置</button>
        </form>
      </div></div>`);
  }).catch(() => {
    showModal(`
      <div class="modal-overlay" onclick="closeModal(event)"><div class="modal-box" onclick="event.stopPropagation()">
        <div class="modal-title">加载失败</div>
        <div style="color:var(--text-tertiary)">无法获取 ${code} 的配置信息</div>
      </div></div>`);
  });
}

function submitConfig(e) {
  e.preventDefault(); const f = e.target;
  const data = { code: f.code.value };
  if (f.stop_loss.value) data.stop_loss = parseFloat(f.stop_loss.value);
  if (f.target_high.value) data.target_high = parseFloat(f.target_high.value);
  if (f.target_low.value) data.target_low = parseFloat(f.target_low.value);
  fetch('/api/config', { method:'POST', headers:writeHeaders({'Content-Type':'application/json'}), body:JSON.stringify(data) })
    .then(r => r.json()).then(d => { alert(d.ok ? '✅ ' + d.msg : '❌ ' + d.msg); closeModal(); });
}

function showModal(html) { const el = document.createElement('div'); el.id = 'modal-container'; el.innerHTML = html; document.body.appendChild(el); }
function closeModal(e) { if (e && e.target !== e.currentTarget) return; const el = document.getElementById('modal-container'); if (el) el.remove(); }

// ═══════════════════════════════════════════════════════════
// BACKTEST
// ═══════════════════════════════════════════════════════════
function loadBacktest() {
  const el = $('backtest-content'); if (!el) return;
  const codes = (STATE.data && STATE.data.portfolio_summary && STATE.data.portfolio_summary.position_details)
    ? STATE.data.portfolio_summary.position_details.map(function(p){return p.code}).slice(0,3) : ['600460'];
  if (!codes.length) return;

  fetchJSON('/api/backtest/' + codes[0])
    .then(d => {
      if (!d.ok || !d.strategies) { el.innerHTML = '<div class="empty-state"><div class="text">回测数据不足</div></div>'; return; }
      let h = '<div class="data-table-wrap"><table class="data-table"><thead><tr><th>策略</th><th class="text-right">收益</th><th class="text-right">Sharpe</th><th class="text-right">回撤</th><th class="text-right">胜率</th></tr></thead><tbody>';
      d.strategies.forEach(s => {
        if (s.error) return;
        h += '<tr><td>' + s.strategy + '</td>'
          + '<td class="text-right ' + (s.total_return >= 0 ? 'up' : 'down') + '">' + (s.total_return >= 0 ? '+' : '') + fmt(s.total_return, 1) + '%</td>'
          + '<td class="text-right ' + (s.sharpe >= 1 ? 'up' : 'gold') + '">' + fmt(s.sharpe, 2) + '</td>'
          + '<td class="text-right down">' + fmt(s.max_dd, 1) + '%</td>'
          + '<td class="text-right ' + (s.win_rate >= 50 ? 'up' : 'down') + '">' + fmt(s.win_rate, 1) + '%</td></tr>';
      });
      h += '</tbody></table></div>';
      el.innerHTML = h;
    })
    .catch(error => { el.innerHTML = componentError(`回测数据加载失败：${error.message}`, 'loadBacktest'); });
}

// ═══════════════════════════════════════════════════════════
// FACTOR IC DASHBOARD
// ═══════════════════════════════════════════════════════════
function loadFactorIC() {
  const el = $('factor-ic-content'); if (!el) return;
  fetchJSON('/api/factor-ic-dashboard')
    .then(d => {
      if (!d.ok || !d.bars) { el.innerHTML = '<div class="empty-state"><div class="text">IC数据暂不可用</div></div>'; return; }
      let maxAbs = 0;
      d.bars.forEach(b => { const value = Math.abs(b.latest_ic); if (value > maxAbs) maxAbs = value; });
      let h = '';
      d.bars.forEach(b => {
        const absIC = Math.abs(b.latest_ic);
        const barW = maxAbs > 0 ? (absIC/maxAbs*100).toFixed(0) : 0;
        const side = b.latest_ic >= 0 ? 'up' : 'down';
        const barColor = b.latest_ic >= 0 ? 'var(--up)' : 'var(--down)';
        h += '<div style="display:flex;align-items:center;gap:8px;margin-bottom:5px;font-size:11px">';
        h += '<span style="width:60px;font-weight:600;text-align:right;font-size:11px">' + b.label + '</span>';
        h += '<span style="width:36px;text-align:right;font-family:var(--font-num);font-size:11px" class="' + side + '">' + (b.latest_ic>=0?'+':'') + b.latest_ic.toFixed(3) + '</span>';
        h += '<div style="flex:1;height:14px;background:rgba(255,255,255,0.04);border-radius:3px;overflow:hidden">';
        h += '<div style="width:' + barW + '%;height:100%;background:' + barColor + ';border-radius:3px;opacity:0.6"></div></div>';
        h += '<span style="width:40px;text-align:right;font-size:10px;color:var(--text-tertiary)">' + b.win_rate + '%</span>';
        h += '</div>';
      });
      // Top/weak summary
      if (d.top && d.top.length) {
        h += '<div style="margin-top:8px;padding-top:8px;border-top:1px solid var(--border-light);font-size:10px;color:var(--text-tertiary)">';
        h += '🏆 最强: ';
        d.top.forEach((t, i) => { if (i > 0) h += ', '; h += t.label + ' ' + (t.latest_ic >= 0 ? '+' : '') + t.latest_ic.toFixed(2); });
        if (d.weak && d.weak.length) { h += '<br>⚠️ 最弱: '; d.weak.forEach((w, i) => { if (i > 0) h += ', '; h += w.label + ' ' + (w.latest_ic >= 0 ? '+' : '') + w.latest_ic.toFixed(2); }); }
        h += '</div>';
      }
      el.innerHTML = h;
    })
    .catch(error => { el.innerHTML = componentError(`因子 IC 加载失败：${error.message}`, 'loadFactorIC'); });
}

// ─── Error ────────────────────────────────────────────────────
function showError(msg) {
  const timeEl = $('header-time'); if (timeEl) timeEl.textContent = '刷新失败';
  if (STATE.data) {
    const active = qs('.tab-content.active');
    if (!active || active.querySelector('.refresh-notice')) return;
    active.insertAdjacentHTML('afterbegin', `<div class="refresh-notice">${msg}，保留上次稳定数据</div>`);
    return;
  }
  document.querySelectorAll('.tab-content.active').forEach(tc => { tc.innerHTML = componentError(msg); });
}

// ═══════════════════════════════════════════════════════════
// COMPARE — 纸面 vs 真实 vs 沪深300
// ═══════════════════════════════════════════════════════════
function loadCompare() {
  const el = $('compare-content'); if (!el) return;
  fetchJSON('/api/compare')
    .then(d => {
      if (!d.ok) { el.innerHTML = '<div class="empty-state"><div class="text">对比数据暂不可用</div></div>'; return; }
      const diff = d.diff_paper_vs_real || 0;
      let h = '<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin-bottom:8px">';
      h += '<div style="text-align:center;padding:8px;background:var(--bg-card);border-radius:8px"><div style="font-size:10px;color:var(--text-tertiary)">真实账户</div><div style="font-size:16px;font-weight:700;font-family:var(--font-num)" class="' + (d.real.pnl>=0?'up':'down') + '">' + (d.real.pnl>=0?'+':'') + fmt(d.real.pnl,1) + '%</div><div style="font-size:10px;color:var(--text-tertiary)">¥' + fmt(d.real.total,0) + '</div></div>';
      h += '<div style="text-align:center;padding:8px;background:var(--bg-card);border-radius:8px"><div style="font-size:10px;color:var(--text-tertiary)">纸面模拟</div><div style="font-size:16px;font-weight:700;font-family:var(--font-num)" class="' + (d.paper.pnl>=0?'up':'down') + '">' + (d.paper.pnl>=0?'+':'') + fmt(d.paper.pnl,1) + '%</div><div style="font-size:10px;color:var(--text-tertiary)">¥' + fmt(d.paper.total,0) + '</div></div>';
      if (d.benchmark && d.benchmark.return !== null) {
        h += '<div style="text-align:center;padding:8px;background:var(--bg-card);border-radius:8px"><div style="font-size:10px;color:var(--text-tertiary)">' + d.benchmark.name + '</div><div style="font-size:16px;font-weight:700;font-family:var(--font-num)" class="' + (d.benchmark.return>=0?'up':'down') + '">' + (d.benchmark.return>=0?'+':'') + fmt(d.benchmark.return,1) + '%</div><div style="font-size:10px;color:var(--text-tertiary)">基准</div></div>';
      }
      h += '</div>';
      h += '<div style="font-size:10px;color:var(--text-tertiary);padding:5px 8px;background:var(--bg-card);border-radius:6px;text-align:center">纸面 vs 真实: <span class="' + (diff>=0?'up':'down') + '" style="font-weight:600">' + (diff>=0?'+':'') + fmt(diff,2) + '%</span></div>';
      el.innerHTML = h;
    })
    .catch(error => { el.innerHTML = componentError(`收益对比加载失败：${error.message}`, 'loadCompare'); });
}

// ═══════════════════════════════════════════════════════════
// RISK MATRIX — VaR + 相关性
// ═══════════════════════════════════════════════════════════
function loadRiskMatrix() {
  const el = $('risk-matrix-content'); if (!el) return;
  fetchJSON('/api/risk-matrix')
    .then(d => {
      if (!d.ok || d.error) { el.innerHTML = '<div class="empty-state"><div class="text">风险数据不足(需≥2只持仓≥10周历史)</div></div>'; return; }
      const risk = d.risk || {};
      const matrix = d.matrix || {};
      let h = '';

      // VaR bar
      h += '<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:5px;margin-bottom:8px">';
      h += '<div style="text-align:center;padding:6px;background:var(--bg-card);border-radius:6px"><div style="font-size:9px;color:var(--text-tertiary)">VaR 95%</div><div style="font-size:15px;font-weight:700;font-family:var(--font-num);color:var(--accent-orange)">-' + risk.var_95_pct + '%</div></div>';
      h += '<div style="text-align:center;padding:6px;background:var(--bg-card);border-radius:6px"><div style="font-size:9px;color:var(--text-tertiary)">最大回撤</div><div style="font-size:15px;font-weight:700;font-family:var(--font-num);color:var(--down)">-' + risk.max_drawdown_pct + '%</div></div>';
      h += '<div style="text-align:center;padding:6px;background:var(--bg-card);border-radius:6px"><div style="font-size:9px;color:var(--text-tertiary)">Sharpe</div><div style="font-size:15px;font-weight:700;font-family:var(--font-num)" class="' + (risk.sharpe>=1?'up':'gold') + '">' + fmt(risk.sharpe,2) + '</div></div>';
      h += '</div>';

      // Correlation table (compact)
      if (matrix.codes && matrix.codes.length >= 2) {
        h += '<div style="font-size:10px;color:var(--text-tertiary);margin-bottom:4px">相关性矩阵</div>';
        h += '<div class="data-table-wrap"><table class="data-table"><thead><tr><th></th>';
        matrix.codes.forEach(code => { h += '<th style="font-size:9px">' + (code.length === 6 ? code.slice(-3) : code) + '</th>'; });
        h += '</tr></thead><tbody>';
        for (let i = 0; i < matrix.codes.length; i++) {
          h += '<tr><td style="font-weight:600;font-size:10px">' + (matrix.codes[i].length === 6 ? matrix.codes[i].slice(-3) : matrix.codes[i]) + '</td>';
          for (let j = 0; j < matrix.codes.length; j++) {
            const value = (matrix.correlation[i] || [])[j] || 0;
            const cls = Math.abs(value) < 0.3 ? 'gold' : (value > 0.7 ? 'down' : '');
            h += '<td class="text-right ' + cls + '">' + value.toFixed(2) + '</td>';
          }
          h += '</tr>';
        }
        h += '</tbody></table></div>';
      }

      // Stress tests
      const stress = d.stress || {};
      if (Object.keys(stress).length) {
        h += '<div style="margin-top:6px;display:flex;gap:8px;font-size:9px;color:var(--text-tertiary)">';
        h += '<span>压力测试:</span>';
        h += '<span>2008: <strong style="color:var(--up)">-' + stress["2008_crisis"] + '%</strong></span>';
        h += '<span>2015: <strong style="color:var(--up)">-' + stress["2015_crash"] + '%</strong></span>';
        h += '<span>COVID: <strong style="color:var(--up)">-' + stress["covid_crash"] + '%</strong></span>';
        h += '</div>';
      }

      el.innerHTML = h;
    })
    .catch(error => { el.innerHTML = componentError(`风险矩阵加载失败：${error.message}`, 'loadRiskMatrix'); });
}

// ═══════════════════════════════════════════════════════════
// OPERATIONS — 对账 / 数据质量 / 风险任务 / PAPER
// ═══════════════════════════════════════════════════════════
function renderOperationsTab() {
  const el = $('tab-operations');
  if (el) el.innerHTML = dashboardSkeleton(5);
}

function retryOperationsData() {
  renderOperationsTab();
  loadOperationsData();
}

function operationsStatusLabel(status) {
  return ({ matched:'已对齐', warning:'需复核', blocked:'已阻断', completed:'已完成',
            blocked_invalid_baseline:'基线无效' })[status] || '未运行';
}

function loadOperationsData() {
  const el = $('tab-operations');
  if (!el) return;
  fetch('/api/operations-center')
    .then(response => {
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return response.json();
    })
    .then(payload => {
      if (!payload.ok) throw new Error(payload.error || '运营数据不可用');
      const data = payload.data || {};
      const reconciliation = data.reconciliation || {};
      const quality = data.quality || {};
      const settlement = quality.settlement_diagnostics || {};
      const recovery = data.risk_recovery || {};
      const paper = data.paper || {};
      const run = data.operations_run || {};
      const tasks = data.tasks || [];
      const nav = (data.analytics || {}).nav || {};
      const taskRows = tasks.slice(0, 8).map(task => `<div class="ops-task">
        <span class="ops-task-severity ${task.severity || 'warning'}"></span>
        <div><strong>${task.code ? task.code + ' · ' : ''}${task.title || '待复核'}</strong>
        <small>${task.summary || '需要人工确认'}</small></div>
        <em>${task.task_type || '任务'}</em></div>`).join('');
      el.innerHTML = `<div class="ops-grid">
        <section class="card ops-summary" aria-label="运营状态摘要">
          <div><span>账户对账</span><strong>${operationsStatusLabel(reconciliation.status)}</strong><small>${reconciliation.broker_snapshot_at || '无快照'}</small></div>
          <div><span>开放任务</span><strong>${tasks.length}</strong><small>${tasks.filter(task => task.severity === 'critical').length} 项关键</small></div>
          <div><span>高置信行情</span><strong>${fmt(quality.high_confidence_pct || 0, 1)}%</strong><small>${quality.high_confidence || 0}/${quality.total || 0}</small></div>
          <div><span>PAPER</span><strong>${paper.orders_filled || 0}/${paper.orders_generated || 0}</strong><small>${operationsStatusLabel(paper.status)}</small></div>
        </section>
        <section class="card ops-control"><div class="card-header"><span class="card-title">对账与可信度</span></div>
          <div class="ops-ledger"><span>资产差异 <b>${fmtCurrency(reconciliation.asset_drift || 0)}</b></span>
          <span>现金差异 <b>${fmtCurrency(reconciliation.cash_drift || 0)}</b></span>
          <span>当前仓位 <b>${fmt(recovery.current_invested_pct || 0, 1)}%</b></span>
          <span>自动池参考 <b>${fmt(recovery.reference_auto_pool_pct || 0, 0)}%</b></span>
          <span>待结算 <b>${settlement.pending || 0}</b></span>
          <span>到期阻塞 <b>${settlement.due_blocked || 0}</b></span>
          <span>处置方式 <b>${recovery.manual_confirmation_required ? '人工复核' : '无需操作'}</b></span></div></section>
        <section class="card ops-queue"><div class="card-header"><span class="card-title">待处理任务</span><span class="card-subtitle">${tasks.length} 项</span></div>
          <div>${taskRows || '<div class="empty-state">没有开放任务</div>'}</div></section>
        <section class="card ops-history"><div class="card-header"><span class="card-title">券商对账净值</span><span class="card-subtitle">${nav.point_count || 0} 个快照</span></div>
          <div class="ops-ledger"><span>当前收益 <b>${pctStr(Number(nav.current_return_pct || 0))}</b></span>
          <span>当前回撤 <b>${pctStr(Number(nav.current_drawdown_pct || 0))}</b></span>
          <span>最大回撤 <b>${fmt(nav.max_drawdown_pct || 0, 2)}%</b></span>
          <span>历史峰值 <b>${fmtCurrency(nav.peak_value || 0)}</b></span></div></section>
        <section class="card ops-run"><div class="card-header"><span class="card-title">最近运营周期</span></div>
          <div class="ops-run-state"><strong>${operationsStatusLabel(run.status)}</strong><span>${run.run_date || '尚未运行'} · ${run.mode || '—'}</span></div></section>
      </div>`;
    })
    .catch(error => { el.innerHTML = componentError(`运营数据加载失败：${error.message}`, 'retryOperationsData'); });
}

// ─── INIT ─────────────────────────────────────────────────────
// ═══════════════════════════════════════════════════════════
// v5.1 浏览器通知
// ═══════════════════════════════════════════════════════════
let _lastGateState = null;
function checkGateStateChange() {
  const d = STATE.data; if (!d) return;
  const gate = d.auto_gate || {};
  const state = gate.state || '';
  if (_lastGateState && _lastGateState !== state) {
    const msg = `闸门状态变更: ${_lastGateState} → ${state}`;
    showToast(msg, state === 'LOCKED' ? 'error' : 'warning');
    if (Notification && Notification.permission === 'granted') {
      new Notification('Serenity 闸门变化', { body: msg, icon: '/static/icon-192.png' });
    }
    // v5.2 推送闸门变化到 Mission Control → Telegram
    fetch('http://localhost:3001/api/alerts/gate-change', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({oldState:_lastGateState, newState:state, reasons:gate.reasons||[]})
    }).catch(function(){});
  }
  _lastGateState = state;
}
// 请求通知权限
if (typeof Notification !== 'undefined' && Notification.permission === 'default') {
  Notification.requestPermission();
}

// ═══════════════════════════════════════════════════════════
// v5.1 信号分布图表
// ═══════════════════════════════════════════════════════════
function renderSignalDistChart(scores) {
  if (!scores || !scores.length) return '';
  const counts = {};
  scores.forEach(s => { const a = s.signal_action || '—'; counts[a] = (counts[a] || 0) + 1; });
  const labels = Object.keys(counts);
  const max = Math.max(...Object.values(counts), 1);
  let html = '<div style="display:flex;flex-direction:column;gap:4px;min-width:120px">';
  const colors = {BUY:'#FF3B30',CAUTION_BUY:'#FF453A',STRONG_BUY:'#FF3B30',HOLD:'#FFD60A',WATCH:'#FF9F0A',WEAK_HOLD:'#FFD60A',SELL:'#34C759',STOP_LOSS:'#30D158',TAKE_PROFIT:'#FFD60A'};
  labels.forEach(l => {
    const w = Math.round((counts[l]/max)*100);
    html += `<div style="display:flex;align-items:center;gap:6px;font-size:10px">
      <span style="width:36px;text-align:right;color:var(--text-tertiary)">${l}</span>
      <div style="flex:1;height:10px;border-radius:5px;background:rgba(255,255,255,0.06)">
        <div style="width:${w}%;height:100%;border-radius:5px;background:${colors[l]||'#666'};transition:width .6s"></div>
      </div>
      <span style="width:16px;color:var(--text-secondary);font-weight:600">${counts[l]}</span>
    </div>`;
  });
  return html + '</div>';
}

// ═══════════════════════════════════════════════════════════
// v5.1 快捷操作
// ═══════════════════════════════════════════════════════════
function quickActions() {
  return `<div class="action-bar" id="action-bar">
    <button class="action-btn primary" onclick="window.open('/monitor','_self')">🔄 刷新</button>
    <button class="action-btn" onclick="navigator.clipboard.writeText(JSON.stringify(STATE.data?.portfolio_summary||{}));showToast('已复制持仓摘要','success')">📋 复制</button>
    <button class="action-btn" onclick="showToast('盘中 5s · 盘后 30s 自动刷新中','info')">ℹ️ 状态</button>
  </div>`;
}

// ═══════════════════════════════════════════════════════════
// v5.1 Δ值辅助函数
// ═══════════════════════════════════════════════════════════
function applyDelta(el, current, previous) {
  if (!previous || !current) return el;
  const diff = current - previous;
  const pct = previous ? (diff / previous * 100) : 0;
  const cls = diff > 0 ? 'up' : diff < 0 ? 'down' : 'flat';
  const sign = diff > 0 ? '+' : '';
  return el + `<span class="delta ${cls}" style="margin-left:6px">${sign}${fmt(diff,0)} (${sign}${fmt(pct,1)}%)</span>`;
}

document.addEventListener('DOMContentLoaded', init);

// ═══════════════════════════════════════════════════════════
// v5.3 LIVE TAB — 盯盘模式
// ═══════════════════════════════════════════════════════════
let _liveTimer = null;
let _liveStartTime = null;

function startLiveRefresh() {
  if (_liveTimer) clearInterval(_liveTimer);
  _liveStartTime = Date.now();
  _liveTimer = setInterval(function() {
    if (document.hidden || STATE.activeTab !== 'live') {
      clearInterval(_liveTimer); _liveTimer = null; return;
    }
    var el = document.getElementById('tab-live');
    if (!el) return;
    fetch('/api/quick-snapshot')
      .then(function(r){return r.json();})
      .then(function(d){
        if (d.ok && d.portfolio) {
          STATE.data = STATE.data || {};
          STATE.data.portfolio_summary = d.portfolio;
          STATE.data.scores = d.scores || STATE.data.scores || [];
          renderLiveTab(STATE.data);
        }
      })
      .catch(function(){});
  }, 5000);
}

function renderLiveTab(d) {
  var el = document.getElementById('tab-live');
  if (!el) return;
  var pf = d.portfolio_summary || {};
  var pnl = pf.total_profit_pct || 0;
  var positions = pf.position_details || [];
  var scores = d.scores || [];
  var uptime = _liveStartTime ? Math.floor((Date.now() - _liveStartTime) / 1000) : 0;
  var mins = Math.floor(uptime / 60), secs = uptime % 60;

  var h = '';
  // Header with timer
  h += '<div class="live-header" style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">';
  h += '<div><span style="font-size:20px;font-weight:700">👁️ 盯盘</span><span style="font-size:11px;color:var(--text-tertiary);margin-left:8px">每5秒刷新</span></div>';
  h += '<div style="font-size:11px;color:var(--text-tertiary);font-family:var(--font-mono)">⏱ ' + mins + ':' + (secs < 10 ? '0' : '') + secs + '</div>';
  h += '</div>';

  // Hero P&L bar
  h += '<div class="hero-compact" style="margin-bottom:12px">';
  h += '<div class="hero-equity">' + fmtCurrency(pf.total_value) + '</div>';
  h += '<div class="hero-pnl-row"><span class="hero-pnl-today ' + clsPct(pnl) + '">' + (pnl >= 0 ? '+' : '') + fmt(pnl, 2) + '%</span>';
  h += '<span class="hero-pnl-total">浮盈 ' + fmtCurrency(pf.total_profit_amount || 0) + '</span></div>';
  h += '</div>';

  // Position cards with live P&L
  if (positions.length) {
    h += '<div class="live-positions" style="display:flex;flex-direction:column;gap:8px">';
    positions.forEach(function(p) {
      var isUp = (p.profit_pct || 0) >= 0;
      var sig = scores.find(function(s){return s.code===p.code}) || {};
      var bgColor = isUp ? 'rgba(255,59,48,0.04)' : 'rgba(52,199,89,0.04)';
      h += '<div class="live-position-card" style="background:' + bgColor + ';border:0.5px solid var(--glass-border);border-radius:12px;padding:12px;transition:all .3s">';
      h += '<div style="display:flex;justify-content:space-between;align-items:center">';
      h += '<div><span style="font-size:16px;font-weight:600">' + (p.name || p.code) + '</span><span style="font-size:11px;color:var(--text-tertiary);margin-left:6px">' + (p.code || '') + '</span></div>';
      h += '<div style="text-align:right"><div style="font-size:11px;color:var(--text-tertiary)">⏺ 实时</div><div style="font-size:18px;font-weight:700;font-family:var(--font-num)" class="' + (isUp ? 'up' : 'down') + '">' + fmtCurrency(p.current_value) + '</div></div>';
      h += '</div>';
      h += '<div style="display:flex;justify-content:space-between;align-items:center;margin-top:8px">';
      h += '<div style="display:flex;gap:16px;font-size:11px">';
      h += '<span>成本 <b style="font-family:var(--font-num)">¥' + fmt(p.buy_price) + '</b></span>';
      h += '<span>现价 <b style="font-family:var(--font-num);color:' + (isUp ? 'var(--up)' : 'var(--down)') + '">¥' + fmt(p.current_price) + '</b></span>';
      h += '<span>股数 <b>' + fmt(p.shares, 0) + '</b></span>';
      h += '</div>';
      h += '<div style="display:flex;gap:8px;align-items:center">';
      h += '<span class="' + (isUp ? 'up' : 'down') + '" style="font-size:20px;font-weight:700;font-family:var(--font-num)">' + (isUp ? '+' : '') + fmt(p.profit_pct, 2) + '%</span>';
      if (sig.signal_action) h += '<span class="signal-label-' + sig.signal_action + '" style="font-size:9px;padding:2px 6px;border-radius:3px">' + sig.signal_action + '</span>';
      h += '</div>';
      h += '</div></div>';
    });
    h += '</div>';
  } else {
    h += '<div class="empty-state"><div class="text">暂无持仓</div></div>';
  }

  el.innerHTML = h;
}

// ═══════════════════════════════════════════════════════════
// v5.7 信号实时推送 — STRONG_BUY/SELL → Telegram
// ═══════════════════════════════════════════════════════════
function pushSignalAlerts() {
  var d = STATE.data; if (!d) return;
  var scores = d.scores || [];
  var heldCodes = new Set((d.portfolio_summary || {}).position_details ? d.portfolio_summary.position_details.map(function(p){return p.code}) : []);
  var alerts = [];
  scores.forEach(function(s) {
    if (s.signal_action === 'STRONG_BUY' && !heldCodes.has(s.code)) {
      alerts.push('🟢 ' + s.name + '(' + s.code + ') STRONG_BUY ' + s.total_score + '分');
    } else if (s.signal_action === 'SELL' && heldCodes.has(s.code)) {
      alerts.push('🔴 ' + s.name + '(' + s.code + ') SELL ' + s.total_score + '分');
    } else if (s.signal_action === 'TAKE_PROFIT' && heldCodes.has(s.code)) {
      alerts.push('💰 ' + s.name + '(' + s.code + ') TAKE_PROFIT');
    }
  });
  if (alerts.length > 0) {
    fetch('http://localhost:3001/api/alerts/gate-change', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({oldState:'signal_check', newState:'signal_alert', reasons:alerts})
    }).catch(function(){});
    alerts.forEach(function(a) { showToast(a, 'warning'); });
  }
}

// ═══════════════════════════════════════════════════════════
// v4 Governance Tab — 内核冻结 + 三系统对比 + 审计 + 观察模式
// ═══════════════════════════════════════════════════════════
function renderGovernanceTab() {
  var el = $('tab-governance');
  if (!el) return;
  el.innerHTML = dashboardSkeleton(6);
}

function loadGovernanceData() {
  var el = $('tab-governance');
  if (!el) return;
  fetchJSON('/api/v4/governance')
    .then(function(d) {
      if (!d || !d.ok) { el.innerHTML = componentError('治理数据暂不可用'); return; }
      renderGovernanceContent(d);
    })
    .catch(function(e) {
      el.innerHTML = componentError('治理数据加载失败：' + e.message, 'loadGovernanceData');
    });
}

function renderGovernanceContent(d) {
  var el = $('tab-governance');
  if (!el) return;
  var f = d.freeze || {};
  var cmp = d.comparison || {};
  var aud = d.audit || {};
  var obs = d.observation || {};

  var html = '';

  // ── 1. 内核冻结状态 ──
  var modulesHtml = '';
  var mods = f.modules || [];
  if (mods.length > 0) {
    mods.forEach(function(m) {
      var locked = m.frozen || m.locked || false;
      modulesHtml += '<tr><td>' + (m.name || m.module || '?') + '</td>'
        + '<td style="text-align:center;font-size:16px">' + (locked ? '🔒' : '🔓') + '</td>'
        + '<td class="text-dim" style="font-size:10px">' + (m.version || m.reason || '') + '</td></tr>';
    });
  } else {
    modulesHtml = '<tr><td colspan="3" class="text-dim" style="text-align:center;padding:12px">暂无模块数据</td></tr>';
  }
  html += '<div class="card"><div class="card-header"><h3 class="card-title">内核冻结状态</h3>'
    + '<span class="card-subtitle">' + (f.total_frozen || 0) + '/' + (f.total_managed || 0) + ' 冻结</span></div>'
    + '<div class="data-table-wrap"><table class="data-table"><thead><tr><th>模块</th><th style="text-align:center;width:40px">状态</th><th>版本/备注</th></tr></thead>'
    + '<tbody>' + modulesHtml + '</tbody></table></div>';
  if (f.freeze_reason) {
    html += '<div style="margin-top:8px;font-size:11px;color:var(--gold)">📋 ' + f.freeze_reason + '</div>';
  }
  html += '</div>';

  // ── 2. 三系统对比 ──
  var divergencesHtml = '';
  var divs = cmp.divergences || [];
  if (divs.length > 0) {
    divs.forEach(function(dv) {
      divergencesHtml += '<tr><td>' + dv.name + '</td><td style="font-family:var(--font-mono);font-size:10px">' + dv.code + '</td>'
        + '<td style="text-align:center">' + (dv.in_system_a ? 'A' : '—') + '</td>'
        + '<td style="text-align:center">' + (dv.in_system_b ? 'B' : '—') + '</td></tr>';
    });
  }
  html += '<div class="card"><div class="card-header"><h3 class="card-title">三系统对比</h3></div>';

  // 三系统 KPI 行
  html += '<div class="kpi-row" style="margin-bottom:8px">'
    + '<div class="kpi-item"><div class="kpi-label">System A<br>Frozen Baseline</div><div class="kpi-value">' + (cmp.system_a ? cmp.system_a.total_signals : 0) + '</div><div class="kpi-sub">今日信号</div></div>'
    + '<div class="kpi-item"><div class="kpi-label">System B<br>Adaptive</div><div class="kpi-value">' + (cmp.system_b ? cmp.system_b.total_signals : 0) + '</div><div class="kpi-sub">今日信号</div></div>'
    + '<div class="kpi-item"><div class="kpi-label">System C<br>Equal Weight</div><div class="kpi-value ' + clsPct(cmp.system_c ? cmp.system_c.daily_return : 0) + '">' + pctStr(cmp.system_c ? cmp.system_c.daily_return : 0) + '</div><div class="kpi-sub">日收益 | NAV ¥' + fmt(cmp.system_c ? cmp.system_c.nav : 0, 0) + '</div></div>'
    + '</div>';

  // 协议率
  html += '<div style="display:flex;gap:12px;margin-bottom:8px;font-size:12px">'
    + '<span>协议率: <b style="color:var(--gold)">' + (cmp.agreement_rate || 0) + '%</b></span>'
    + '<span>分歧数: <b style="color:var(--accent-orange)">' + (cmp.divergence_count || 0) + '</b></span>'
    + '</div>';

  // 分歧表
  if (divergencesHtml) {
    html += '<div class="data-table-wrap"><table class="data-table"><thead><tr><th>标的</th><th>代码</th><th style="text-align:center;width:36px">A</th><th style="text-align:center;width:36px">B</th></tr></thead>'
      + '<tbody>' + divergencesHtml + '</tbody></table></div>';
  }
  html += '</div>';

  // ── 3. 决策审计日志 ──
  html += '<div class="card"><div class="card-header"><h3 class="card-title">决策审计日志</h3></div>'
    + '<div class="gate-grid">'
    + '<div><span>总计</span><b>' + (aud.total || 0) + '</b></div>'
    + '<div><span>已执行</span><b style="color:var(--down)">' + (aud.executed || 0) + '</b></div>'
    + '<div><span>已拦截</span><b style="color:var(--up)">' + (aud.blocked || 0) + '</b></div>'
    + '<div><span>已覆写</span><b style="color:var(--accent-orange)">' + (aud.overridden || 0) + '</b></div>'
    + '<div><span>已结算</span><b>' + (aud.settled || 0) + '</b></div>'
    + '<div><span>执行率</span><b style="color:var(--gold)">' + (aud.execution_rate || 0) + '%</b></div>'
    + '<div><span>覆写率</span><b>' + (aud.override_rate || 0) + '%</b></div>'
    + '<div><span>待结算</span><b style="color:' + ((aud.pending_settlements || 0) > 0 ? 'var(--up)' : 'var(--text-secondary)') + '">' + (aud.pending_settlements || 0) + '</b></div>'
    + '</div></div>';

  // ── 4. 观察模式状态 ──
  var mode = obs.mode || 'NORMAL';
  var modeCls = mode === 'NORMAL' ? 'down' : (mode === 'OBSERVATION' ? 'gold' : 'up');
  var modeBg = mode === 'NORMAL' ? 'var(--down-bg)' : (mode === 'OBSERVATION' ? 'var(--gold-bg)' : 'var(--up-bg)');
  html += '<div class="card"><div class="card-header"><h3 class="card-title">观察模式</h3>'
    + '<span class="card-subtitle ' + modeCls + '" style="font-weight:700;font-size:13px">' + mode + '</span></div>';

  if (mode === 'OBSERVATION') {
    html += '<div style="font-size:12px;color:var(--text-secondary);line-height:1.6">'
      + '<div>触发原因: ' + (obs.trigger_reason || '—') + '</div>'
      + '<div>进入时间: ' + (obs.time_entered || '—') + '</div>'
      + '<div>剩余天数: <b style="color:var(--gold)">' + (obs.days_remaining || 0) + ' 天</b></div>'
      + '</div>';
  } else if (mode === 'EMERGENCY') {
    html += '<div style="font-size:12px;color:var(--text-secondary);line-height:1.6">'
      + '<div>触发原因: ' + (obs.trigger_reason || '—') + '</div>'
      + '<div>平仓状态: <b style="color:var(--up)">' + (obs.liquidation_status || '—') + '</b></div>'
      + '</div>';
  } else {
    html += '<div class="text-dim" style="font-size:12px;padding:8px 0">系统正常运行，无观察/紧急模式触发。</div>';
  }
  html += '</div>';

  // 时间戳
  html += '<div class="text-dim" style="text-align:center;font-size:10px;padding:16px 0">数据时间: ' + (d.timestamp || '—') + ' | 日期: ' + (d.date || '—') + '</div>';

  el.innerHTML = html;
}
