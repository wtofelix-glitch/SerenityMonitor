/* ============================================================
   Serenity Monitor v3.0 — 前端渲染引擎
   ───────────────────────────────────────────────────────────
   3-Tab 布局: 总览 / 持仓 / 风控
   30 秒自动刷新，Chart.js 净值曲线
   ============================================================ */

'use strict';

// ─── 全局状态 ─────────────────────────────────────────────────
const STATE = {
  data: null,
  chartInstance: null,
  activeTab: 'overview',
  refreshInterval: null,
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

// ─── 防抖 ────────────────────────────────────────────────────
function debounce(fn, delay) {
  let timer = null;
  return function(...args) { clearTimeout(timer); timer = setTimeout(() => fn.apply(this, args), delay); };
}

// ─── TAB 导航 ─────────────────────────────────────────────────
function initTabs() {
  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });
}

function switchTab(tabId) {
  STATE.activeTab = tabId;
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === tabId));
  document.querySelectorAll('.tab-content').forEach(c => c.classList.toggle('active', c.id === 'tab-' + tabId));

  if (!STATE.data) return;
  if (tabId === 'overview') renderOverview(STATE.data);
  else if (tabId === 'holdings') renderHoldingsTab(STATE.data);
  else if (tabId === 'sentinel') { renderSentinelTab(STATE.data); loadSentinelData(); }
  else if (tabId === 'risk') { renderRiskTab(STATE.data); loadNavHistory(); }
}

// ─── 初始化 ────────────────────────────────────────────────────
function init() {
  initTabs();
  refresh();
  STATE.refreshInterval = setInterval(refresh, 30000);
}

// ─── 数据刷新 ──────────────────────────────────────────────────
function refresh(force) {
  const timeEl = $('header-time');
  if (timeEl) timeEl.textContent = '⟳ 刷新中...';
  const url = force ? '/api/monitor-data?force=1' : '/api/monitor-data';
  fetch(url)
    .then(r => r.json())
    .then(d => {
      if (d.ok) { STATE.data = d.data; renderAll(); }
      else showError('数据获取失败');
    })
    .catch(err => { if (err && err.name === 'AbortError') return; showError('网络错误'); });
}

function renderAll() {
  updateHeader();
  updateMarketTape();
  switchTab(STATE.activeTab);
}

function manualRefresh() {
  const btn = $('refresh-btn');
  if (btn) btn.classList.add('spinning');
  refresh(true);
  clearInterval(STATE.refreshInterval);
  STATE.refreshInterval = setInterval(refresh, 30000);
  setTimeout(() => { if (btn) btn.classList.remove('spinning'); }, 700);
}

// ─── 头部 ──────────────────────────────────────────────────────
function updateHeader() {
  const d = STATE.data; if (!d) return;
  const el = $('header-time');
  if (el) el.textContent = d.timestamp || d.date || '—';
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
  if (!d) { $('tab-overview').innerHTML = '<div class="loading-state"><span class="loading-spinner"></span><div class="loading-text">加载中...</div></div>'; return; }

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
    <input type="text" id="nl-query-input" class="nl-search-input" placeholder="问 Serenity... 如"今天该买什么""收益怎么样""有风险吗"">
    <button class="nl-search-btn" onclick="doNLQuery()">→</button>
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

  // ── Merged Actions ──────────────────────────────────────
  html += buildMergedActions(d, session);

  // ── Position Quick View ─────────────────────────────────
  html += buildPositionQuickView(d);

  // ── Signal Brief (inline) ───────────────────────────────
  if (buyCount > 0 || riskCount > 0) {
    let chips = '';
    (sb.buy_candidates || []).slice(0, 3).forEach(b => {
      chips += `<span class="hero-pill up" style="font-size:11px">${b.name} ${b.score}分</span>`;
    });
    (sb.risk_alerts || []).slice(0, 2).forEach(r => {
      chips += `<span class="hero-pill down" style="font-size:11px">${r.name} ${r.action}</span>`;
    });
    html += `<div class="card"><div class="card-header"><span class="card-title">信号简报</span><span class="card-subtitle">${buyCount}买 / ${riskCount}险</span></div>
      <div class="card-body" style="display:flex;flex-wrap:wrap;gap:6px">${chips || '<span class="text-dim">暂无高优先级信号</span>'}</div></div>`;
  }

  $('tab-overview').innerHTML = html;
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
    items.push({ tone: 'up', title: `候选复核：${s.name}`, desc: `评分 ${fmt(s.total_score, 0)} · ${actionLabel(s.signal_action)}`, tag: s.code, tagType: 'buy' });
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
        <th>标的</th><th class="text-right">盈亏</th><th class="text-right">权重</th><th class="text-right">成本</th><th>信号</th><th></th>
      </tr></thead><tbody>`;

    details.forEach(p => {
      const isUp = (p.profit_pct || 0) >= 0;
      const sig = scores.find(s => s.code === p.code) || {};
      html += `<tr>
        <td><span class="pos-name ${isUp ? 'up' : 'down'}">${p.name || '—'}</span><br><span class="pos-code">${p.code || ''}</span></td>
        <td class="text-right ${isUp ? 'up' : 'down'}" style="font-weight:600">${(p.profit_pct >= 0 ? '+' : '') + fmt(p.profit_pct, 2)}%</td>
        <td class="text-right text-dim">${fmt(p.weight, 1)}%</td>
        <td class="text-right text-dim">¥${fmt(p.buy_price)}</td>
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
      return `<div class="score-chip">
        <div class="sc-rank">#${s.rank || '-'}</div>
        <div class="sc-name">${s.name}</div>
        <div class="sc-value" style="color:${color}">${fmt(sc, 0)}</div>
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
        borderColor: '#FFD700',
        backgroundColor: function(context) {
          const { ctx, chartArea } = context.chart;
          if (!chartArea) return 'rgba(255,215,0,0.06)';
          const gradient = ctx.createLinearGradient(0, chartArea.top, 0, chartArea.bottom);
          gradient.addColorStop(0, 'rgba(255,215,0,0.12)');
          gradient.addColorStop(1, 'rgba(255,215,0,0.0)');
          return gradient;
        },
        fill: true, borderWidth: 2, pointRadius: 0, pointHoverRadius: 4,
        pointHoverBackgroundColor: '#FFD700', pointHoverBorderColor: '#000', pointHoverBorderWidth: 2,
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
  fetch('/api/anomalies')
    .then(r => r.json())
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
    .catch(() => { el.innerHTML = '<div class="error-state">加载失败</div>'; });
}

// ─── Signal Performance ───────────────────────────────────────
function loadSignalPerformance() {
  const el = $('signal-perf-content'); if (!el) return;
  fetch('/api/signal-performance')
    .then(r => r.json())
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
    .catch(() => { el.innerHTML = '<div class="error-state">加载失败</div>'; });
}

// ─── Journal ──────────────────────────────────────────────────
function loadJournal() {
  const el = $('journal-content'); if (!el) return;
  fetch('/api/journal')
    .then(r => r.json())
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
    .catch(() => { el.innerHTML = '<div class="error-state">加载失败</div>'; });
}

// ═══════════════════════════════════════════════════════════
// SENTINEL TAB
// ═══════════════════════════════════════════════════════════
function renderSentinelTab(d) {
  $('tab-sentinel').innerHTML = '<div class="loading-state"><span class="loading-spinner"></span><div class="loading-text">加载哨兵数据...</div></div>';
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
  }).catch(() => { $('tab-sentinel').innerHTML = '<div class="error-state">加载失败</div>'; });
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
  fetch('/api/paper-portfolio')
    .then(r => r.json())
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
    .catch(() => { el.innerHTML = '<div class="error-state">加载失败</div>'; });
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

  fetch('/api/backtest/' + codes[0])
    .then(function(r){return r.json()})
    .then(function(d){
      if (!d.ok || !d.strategies) { el.innerHTML = '<div class="empty-state"><div class="text">回测数据不足</div></div>'; return; }
      var h = '<div class="data-table-wrap"><table class="data-table"><thead><tr><th>策略</th><th class="text-right">收益</th><th class="text-right">Sharpe</th><th class="text-right">回撤</th><th class="text-right">胜率</th></tr></thead><tbody>';
      d.strategies.forEach(function(s){
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
    .catch(function(){ el.innerHTML = '<div class="error-state">加载失败</div>'; });
}

// ═══════════════════════════════════════════════════════════
// FACTOR IC DASHBOARD
// ═══════════════════════════════════════════════════════════
function loadFactorIC() {
  var el = $('factor-ic-content'); if (!el) return;
  fetch('/api/factor-ic-dashboard')
    .then(function(r){return r.json()})
    .then(function(d){
      if (!d.ok || !d.bars) { el.innerHTML = '<div class="empty-state"><div class="text">IC数据暂不可用</div></div>'; return; }
      var maxAbs = 0;
      d.bars.forEach(function(b){var a=Math.abs(b.latest_ic); if(a>maxAbs)maxAbs=a;});
      var h = '';
      d.bars.forEach(function(b){
        var absIC = Math.abs(b.latest_ic);
        var barW = maxAbs > 0 ? (absIC/maxAbs*100).toFixed(0) : 0;
        var side = b.latest_ic >= 0 ? 'up' : 'down';
        var barColor = b.latest_ic >= 0 ? 'var(--up)' : 'var(--down)';
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
        d.top.forEach(function(t,i){if(i>0)h+=', ';h+=t.label + ' ' + (t.latest_ic>=0?'+':'') + t.latest_ic.toFixed(2)});
        if (d.weak && d.weak.length) { h += '<br>⚠️ 最弱: '; d.weak.forEach(function(w,i){if(i>0)h+=', ';h+=w.label + ' ' + (w.latest_ic>=0?'+':'') + w.latest_ic.toFixed(2)}); }
        h += '</div>';
      }
      el.innerHTML = h;
    })
    .catch(function(){ el.innerHTML = '<div class="error-state">加载失败</div>'; });
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
  document.querySelectorAll('.tab-content.active').forEach(tc => { tc.innerHTML = `<div class="error-state">${msg}</div>`; });
}

// ═══════════════════════════════════════════════════════════
// COMPARE — 纸面 vs 真实 vs 沪深300
// ═══════════════════════════════════════════════════════════
function loadCompare() {
  var el = $('compare-content'); if (!el) return;
  fetch('/api/compare')
    .then(function(r){return r.json()})
    .then(function(d){
      if (!d.ok) { el.innerHTML = '<div class="empty-state"><div class="text">对比数据暂不可用</div></div>'; return; }
      var diff = d.diff_paper_vs_real || 0;
      var h = '<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin-bottom:8px">';
      h += '<div style="text-align:center;padding:8px;background:var(--bg-card);border-radius:8px"><div style="font-size:10px;color:var(--text-tertiary)">真实账户</div><div style="font-size:16px;font-weight:700;font-family:var(--font-num)" class="' + (d.real.pnl>=0?'up':'down') + '">' + (d.real.pnl>=0?'+':'') + fmt(d.real.pnl,1) + '%</div><div style="font-size:10px;color:var(--text-tertiary)">¥' + fmt(d.real.total,0) + '</div></div>';
      h += '<div style="text-align:center;padding:8px;background:var(--bg-card);border-radius:8px"><div style="font-size:10px;color:var(--text-tertiary)">纸面模拟</div><div style="font-size:16px;font-weight:700;font-family:var(--font-num)" class="' + (d.paper.pnl>=0?'up':'down') + '">' + (d.paper.pnl>=0?'+':'') + fmt(d.paper.pnl,1) + '%</div><div style="font-size:10px;color:var(--text-tertiary)">¥' + fmt(d.paper.total,0) + '</div></div>';
      if (d.benchmark && d.benchmark.return !== null) {
        h += '<div style="text-align:center;padding:8px;background:var(--bg-card);border-radius:8px"><div style="font-size:10px;color:var(--text-tertiary)">' + d.benchmark.name + '</div><div style="font-size:16px;font-weight:700;font-family:var(--font-num)" class="' + (d.benchmark.return>=0?'up':'down') + '">' + (d.benchmark.return>=0?'+':'') + fmt(d.benchmark.return,1) + '%</div><div style="font-size:10px;color:var(--text-tertiary)">基准</div></div>';
      }
      h += '</div>';
      h += '<div style="font-size:10px;color:var(--text-tertiary);padding:5px 8px;background:var(--bg-card);border-radius:6px;text-align:center">纸面 vs 真实: <span class="' + (diff>=0?'up':'down') + '" style="font-weight:600">' + (diff>=0?'+':'') + fmt(diff,2) + '%</span></div>';
      el.innerHTML = h;
    })
    .catch(function(){ el.innerHTML = '<div class="error-state">加载失败</div>'; });
}

// ═══════════════════════════════════════════════════════════
// RISK MATRIX — VaR + 相关性
// ═══════════════════════════════════════════════════════════
function loadRiskMatrix() {
  var el = $('risk-matrix-content'); if (!el) return;
  fetch('/api/risk-matrix')
    .then(function(r){return r.json()})
    .then(function(d){
      if (!d.ok || d.error) { el.innerHTML = '<div class="empty-state"><div class="text">风险数据不足(需≥2只持仓≥10周历史)</div></div>'; return; }
      var r = d.risk || {};
      var m = d.matrix || {};
      var h = '';

      // VaR bar
      h += '<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:5px;margin-bottom:8px">';
      h += '<div style="text-align:center;padding:6px;background:var(--bg-card);border-radius:6px"><div style="font-size:9px;color:var(--text-tertiary)">VaR 95%</div><div style="font-size:15px;font-weight:700;font-family:var(--font-num);color:var(--accent-orange)">-' + r.var_95_pct + '%</div></div>';
      h += '<div style="text-align:center;padding:6px;background:var(--bg-card);border-radius:6px"><div style="font-size:9px;color:var(--text-tertiary)">最大回撤</div><div style="font-size:15px;font-weight:700;font-family:var(--font-num);color:var(--down)">-' + r.max_drawdown_pct + '%</div></div>';
      h += '<div style="text-align:center;padding:6px;background:var(--bg-card);border-radius:6px"><div style="font-size:9px;color:var(--text-tertiary)">Sharpe</div><div style="font-size:15px;font-weight:700;font-family:var(--font-num)" class="' + (r.sharpe>=1?'up':'gold') + '">' + fmt(r.sharpe,2) + '</div></div>';
      h += '</div>';

      // Correlation table (compact)
      if (m.codes && m.codes.length >= 2) {
        h += '<div style="font-size:10px;color:var(--text-tertiary);margin-bottom:4px">相关性矩阵</div>';
        h += '<div class="data-table-wrap"><table class="data-table"><thead><tr><th></th>';
        m.codes.forEach(function(c){h += '<th style="font-size:9px">' + (c.length==6?c.slice(-3):c) + '</th>'});
        h += '</tr></thead><tbody>';
        for (var i = 0; i < m.codes.length; i++) {
          h += '<tr><td style="font-weight:600;font-size:10px">' + (m.codes[i].length==6?m.codes[i].slice(-3):m.codes[i]) + '</td>';
          for (var j = 0; j < m.codes.length; j++) {
            var val = (m.correlation[i]||[])[j] || 0;
            var cls = Math.abs(val) < 0.3 ? 'gold' : (val > 0.7 ? 'down' : '');
            h += '<td class="text-right ' + cls + '">' + val.toFixed(2) + '</td>';
          }
          h += '</tr>';
        }
        h += '</tbody></table></div>';
      }

      // Stress tests
      var s = d.stress || {};
      if (Object.keys(s).length) {
        h += '<div style="margin-top:6px;display:flex;gap:8px;font-size:9px;color:var(--text-tertiary)">';
        h += '<span>压力测试:</span>';
        h += '<span>2008: <strong style="color:var(--up)">-' + s["2008_crisis"] + '%</strong></span>';
        h += '<span>2015: <strong style="color:var(--up)">-' + s["2015_crash"] + '%</strong></span>';
        h += '<span>COVID: <strong style="color:var(--up)">-' + s["covid_crash"] + '%</strong></span>';
        h += '</div>';
      }

      el.innerHTML = h;
    })
    .catch(function(){ el.innerHTML = '<div class="error-state">加载失败</div>'; });
}

// ─── INIT ─────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', init);
