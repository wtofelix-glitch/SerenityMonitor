/* ============================================================
   Serenity Monitor — 前端交互引擎
   ───────────────────────────────────────────────────────────
   依赖: Chart.js 4.x (CDN)
   架构:
     - Tab 导航系统
     - 数据加载器 (统一 fetch → render)
     - Chart.js 净值曲线
     - 信号/绩效/IC/日志卡片渲染
     - 调仓 & 设置模态框
     - 30秒自动刷新
   ============================================================ */

'use strict';

// ─── 全局状态 ─────────────────────────────────────────────────
const STATE = {
  data: null,
  navHistory: [],
  chartInstance: null,
    activeTab: 'overview',
  refreshInterval: null,
  prevSignals: null,  // for change tracking
};

// ─── 工具函数 ─────────────────────────────────────────────────
const fmt = (n, d = 2) => (n == null || isNaN(n)) ? '—' : Number(n).toFixed(d);
const clsPct = v => (v == null || isNaN(v) || v === 0) ? '' : (v >= 0 ? 'up' : 'down');
const pctStr = v => (v == null || isNaN(v)) ? '—' : (v >= 0 ? '+' : '') + v.toFixed(2) + '%';
const signClass = s => s ? 'signal-label-' + s : '';

function getWriteToken() {
  const params = new URLSearchParams(window.location.search);
  const urlToken = params.get('token');
  if (urlToken) {
    try { localStorage.setItem('serenity_dashboard_token', urlToken); } catch(e) {}
    return urlToken;
  }
  try { return localStorage.getItem('serenity_dashboard_token') || ''; } catch(e) { return ''; }
}

function writeHeaders(base) {
  const headers = Object.assign({}, base || {});
  const token = getWriteToken();
  if (token) headers['X-Serenity-Token'] = token;
  return headers;
}

// ─── 格式化货币 ────────────────────────────────────────────────
function fmtCurrency(v) {
  if (v == null || isNaN(v)) return '—';
  if (Math.abs(v) >= 10000) return '¥' + (v / 10000).toFixed(1) + 'k';
  return '¥' + Number(v).toFixed(0);
}

// ─── DOM 快捷引用 ─────────────────────────────────────────────
const $ = id => document.getElementById(id);
const qs = (sel, ctx) => (ctx || document).querySelector(sel);
const qsa = (sel, ctx) => (ctx || document).querySelectorAll(sel);

// ─── TAB 导航系统 ─────────────────────────────────────────────
function initTabs() {
  const tabs = qsa('.tab-btn');
  tabs.forEach(btn => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });
}

function switchTab(tabId) {
  STATE.activeTab = tabId;
  // Update tab buttons
  qsa('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === tabId));
  // Update tab content
  qsa('.tab-content').forEach(c => c.classList.toggle('active', c.id === 'tab-' + tabId));

  // 切换到 risk tab 时，延迟触发 chart resize
  // （因为 chart 可能在 display:none 状态下创建，需要重绘）
  if (tabId === 'risk') {
    setTimeout(() => {
      if (STATE.chartInstance) STATE.chartInstance.resize();
    }, 300);
  }

  // Lazy load tab-specific data
  if (tabId === 'overview') {
    renderOverview(STATE.data);
  } else if (tabId === 'holdings') {
    renderHoldingsTab(STATE.data);
  } else if (tabId === 'factors') {
    renderFactorsTab(STATE.data);
  } else if (tabId === 'risk') {
    renderRiskTab(STATE.data);
  } else if (tabId === 'analysis') {
    renderAnalysisTab(STATE.data);
  }
}

// ─── 初始化 ────────────────────────────────────────────────────
function init() {
  initTabs();
  refresh();
  STATE.refreshInterval = setInterval(refresh, 30000);
}

// ─── 数据刷新 ──────────────────────────────────────────────────
function refresh(force) {
  // 显示刷新指示器（非阻塞，不擦除内容）
  const timeEl = $('header-time');
  if (timeEl) timeEl.textContent = '⟳ 刷新中...';

  const url = force ? '/api/monitor-data?force=1' : '/api/monitor-data';

  fetch(url)
    .then(r => r.json())
    .then(d => {
      if (d.ok) {
        // Save previous signals for change tracking
        if (!STATE.prevSignals && STATE.data && STATE.data.signal_brief) {
          STATE.prevSignals = STATE.data.signal_brief;
        }
        STATE.data = d.data;
        renderAll();
        // Initialize lucide icons after DOM update
        const iconLib = window.lucide;
        if (iconLib && iconLib.createIcons) {
          try { iconLib.createIcons(); } catch(e) {}
        }
      } else {
        showError('数据获取失败');
      }
    })
    .catch(err => {
      if (err && err.name === 'AbortError') return;
      showError('网络错误');
    });

  // Load auxiliary data in background
  loadNavHistory();
  loadSignalHistory();
  loadSignalPerformance();
  loadDimensionEffectiveness();
  loadFactorIC();
  loadJournal();
}

function renderAll() {
  updateHeader();
  updateMarketTape();
  // Update previous signals after render for next comparison
  if (STATE.data && STATE.data.signal_brief) {
    STATE.prevSignals = STATE.data.signal_brief;
  }
  // Force current tab render
  switchTab(STATE.activeTab);
}

// ─── 手动刷新（跳过缓存） ───────────────────────────────────
function manualRefresh() {
  const btn = $('refresh-btn');
  if (btn) btn.classList.add('spinning');
  refresh(true);
  // 重置定时器：从现在起再过30秒自动刷新
  clearInterval(STATE.refreshInterval);
  STATE.refreshInterval = setInterval(refresh, 30000);
  setTimeout(() => { if (btn) btn.classList.remove('spinning'); }, 700);
}

// ─── 头部更新 ──────────────────────────────────────────────────
function updateHeader() {
  const d = STATE.data;
  if (!d) return;
  const timeEl = $('header-time');
  if (timeEl) timeEl.textContent = d.timestamp || d.date || '—';
}

function getMarketView(mkt) {
  const rawSignal = ((mkt || {}).overall_signal || '').toLowerCase();
  if (rawSignal.includes('多') || rawSignal === 'bull' || rawSignal === 'bullish') {
    return { label: '多头', cls: 'up', posture: '进攻' };
  }
  if (rawSignal.includes('空') || rawSignal === 'bear' || rawSignal === 'bearish') {
    return { label: '空头', cls: 'down', posture: '防守' };
  }
  return { label: '震荡', cls: 'gold', posture: '等待' };
}

function getExecutionView(d) {
  const sb = (d || {}).signal_brief || {};
  const pf = (d || {}).portfolio_summary || {};
  const buyCount = sb.buy_count || 0;
  const riskCount = sb.risk_count || 0;
  const pnl = pf.total_profit_pct || 0;
  const confidence = Math.max(8, Math.min(96, 42 + buyCount * 9 - riskCount * 7 + (pnl >= 0 ? 6 : -5)));
  let label = '等待确认';
  let tone = 'gold';
  if (buyCount > 0 && buyCount >= riskCount) {
    label = `${buyCount} 个买入候选`;
    tone = 'up';
  } else if (riskCount > buyCount) {
    label = `${riskCount} 个风险提示`;
    tone = 'down';
  }
  return { label, tone, confidence, buyCount, riskCount };
}

function parseDashboardTime(d) {
  const raw = (d && (d.timestamp || d.date)) || '';
  if (raw && /^\d{4}-\d{2}-\d{2}/.test(raw)) {
    const normalized = raw.length <= 10 ? raw + 'T00:00:00' : raw.replace(' ', 'T');
    const parsed = new Date(normalized);
    if (!isNaN(parsed.getTime())) return parsed;
  }
  return new Date();
}

function getSessionView(d) {
  const now = parseDashboardTime(d);
  const day = now.getDay();
  const mins = now.getHours() * 60 + now.getMinutes();
  const steps = [
    { id: 'premarket', label: '盘前', short: '盘前' },
    { id: 'intraday', label: '盘中', short: '盘中' },
    { id: 'midday', label: '午间', short: '午间' },
    { id: 'postmarket', label: '盘后', short: '盘后' },
  ];

  if (day === 0 || day === 6) {
    return {
      id: 'closed', label: '休市', tone: 'gold', focus: '观察，不开新动作',
      window: '非交易日', next: '下一交易日 09:15 盘前校准', steps,
    };
  }
  if (mins < 9 * 60 + 15) {
    return {
      id: 'premarket', label: '盘前准备', tone: 'gold', focus: '筛候选，定风控线',
      window: '09:15 前', next: '09:15 集合竞价观察', steps,
    };
  }
  if (mins < 9 * 60 + 30) {
    return {
      id: 'premarket', label: '集合竞价', tone: 'gold', focus: '只确认，不追价',
      window: '09:15-09:30', next: '09:30 开盘执行窗口', steps,
    };
  }
  if ((mins >= 9 * 60 + 30 && mins <= 11 * 60 + 30) || (mins >= 13 * 60 && mins <= 15 * 60)) {
    return {
      id: 'intraday', label: '盘中执行', tone: 'up', focus: '只处理高置信动作',
      window: mins < 12 * 60 ? '09:30-11:30' : '13:00-15:00', next: '收盘后复盘信号质量', steps,
    };
  }
  if (mins > 11 * 60 + 30 && mins < 13 * 60) {
    return {
      id: 'midday', label: '午间校准', tone: 'gold', focus: '复核早盘成交与异动',
      window: '11:30-13:00', next: '13:00 下午盘执行', steps,
    };
  }
  return {
    id: 'postmarket', label: '盘后复盘', tone: 'down', focus: '记录原因，更新明日队列',
    window: '15:00 后', next: '明日 09:15 重新校准', steps,
  };
}

function actionLabel(action) {
  const map = {
    STRONG_BUY: '强买', BUY: '买入', CAUTION_BUY: '谨慎买入',
    STRONG_HOLD: '强持有', HOLD: '持有', WATCH: '观察',
    WEAK_HOLD: '弱持有', SELL: '卖出', STOP_LOSS: '止损',
    TAKE_PROFIT: '止盈',
  };
  return map[action] || action || '观察';
}

function getFactorHighlights(d, code) {
  const row = (d.factors || []).find(f => f.code === code);
  if (!row) return [];
  const labels = d.factor_labels || {};
  return Object.keys(row)
    .filter(k => !['code', 'name', 'signal'].includes(k) && row[k] != null && !isNaN(row[k]))
    .map(k => ({ key: k, label: labels[k] || k, value: Number(row[k]) }))
    .sort((a, b) => b.value - a.value)
    .slice(0, 3);
}

function getBuyQueue(d) {
  const queue = new Map();
  const scores = d.scores || [];
  const scoreMap = {};
  scores.forEach(s => { scoreMap[s.code] = s; });

  function mergeCandidate(raw, source) {
    if (!raw || !raw.code) return;
    const score = scoreMap[raw.code] || {};
    const current = queue.get(raw.code) || {};
    queue.set(raw.code, {
      code: raw.code,
      name: raw.name || score.name || raw.code,
      score: raw.score || raw.total_score || score.total_score || current.score || 0,
      action: raw.action || raw.signal_action || score.signal_action || current.action || 'WATCH',
      confidence: raw.confidence || raw.signal_confidence || score.signal_confidence || current.confidence || 0,
      suggested_amount: raw.suggested_amount || current.suggested_amount || 0,
      suggested_shares: raw.suggested_shares || current.suggested_shares || 0,
      source,
    });
  }

  ((d.signal_brief || {}).buy_candidates || []).forEach(c => mergeCandidate(c, 'signal'));
  (((d.position_advice || {}).buy_candidates) || []).forEach(c => mergeCandidate(c, 'kelly'));
  scores
    .filter(s => ['STRONG_BUY', 'BUY', 'CAUTION_BUY'].includes(s.signal_action))
    .slice(0, 8)
    .forEach(s => mergeCandidate(s, 'score'));

  return Array.from(queue.values()).sort((a, b) => (b.score || 0) - (a.score || 0)).slice(0, 6);
}

function generateActionItems(d, session) {
  const sb = d.signal_brief || {};
  const advice = d.position_advice || {};
  const tt = d.target_tracker || {};
  const items = [];

  const phaseCopy = {
    premarket: ['盘前校准', `先审 ${sb.buy_count || 0} 个候选，标记触发价和无效条件。`],
    intraday: ['盘中执行', '只处理高置信信号和风险项，避免临盘追涨。'],
    midday: ['午间校准', '复核早盘异动，下午只保留最高优先级动作。'],
    postmarket: ['盘后复盘', '记录实际执行理由，准备明日候选队列。'],
    closed: ['休市观察', '不做新动作，只更新观察名单和复盘备注。'],
  }[session.id] || ['今日节奏', session.focus];
  items.push({ tone: session.tone, title: phaseCopy[0], body: phaseCopy[1], meta: session.window });

  (sb.risk_alerts || []).slice(0, 2).forEach(r => {
    items.push({
      tone: 'down',
      title: `处理风险：${r.name}`,
      body: `${actionLabel(r.action)} · 评分 ${fmt(r.score, 0)}，先确认是否触发减仓或退出。`,
      meta: r.code,
    });
  });

  (advice.holdings_advice || [])
    .filter(a => a.suggest && !['HOLD', 'WATCH'].includes(a.suggest))
    .slice(0, 2)
    .forEach(a => {
      const tone = ['EXIT', 'REDUCE'].includes(a.suggest) ? 'down' : 'up';
      items.push({
        tone,
        title: `仓位动作：${a.name}`,
        body: a.reason || `${a.suggest} · 当前收益 ${fmt(a.profit_pct, 1)}%。`,
        meta: `${a.code} · ${a.suggest}`,
      });
    });

  getBuyQueue(d).slice(0, 3).forEach(c => {
    const amount = c.suggested_amount ? `建议金额 ${fmtCurrency(c.suggested_amount)}` : '等待仓位确认';
    items.push({
      tone: 'up',
      title: `候选复核：${c.name}`,
      body: `评分 ${fmt(c.score, 0)} · ${actionLabel(c.action)}，${amount}。`,
      meta: c.code,
    });
  });

  if ((tt.required_monthly_return || 0) > 15) {
    items.push({
      tone: 'gold',
      title: '目标压力检查',
      body: `目标需月收益 ${fmt(tt.required_monthly_return, 1)}%，控制单笔失误成本。`,
      meta: 'Target',
    });
  }

  return items.slice(0, 5);
}

function updateMarketTape() {
  const d = STATE.data;
  const el = $('market-tape');
  if (!d || !el) return;

  const pf = d.portfolio_summary || {};
  const sb = d.signal_brief || {};
  const mkt = d.market || {};
  const sh = mkt.sh || {};
  const hs300 = mkt.hs300 || {};
  const marketView = getMarketView(mkt);
  const session = getSessionView(d);
  const pnl = pf.total_profit_pct || 0;

  el.className = `market-tape market-tape-${marketView.cls}`;
  el.innerHTML = `
    <span class="tape-kicker">MARKET TAPE</span>
    <div class="tape-track">
      <span><b>上证</b><em>${sh.last_close ? fmt(sh.last_close, 0) : '--'} · ${sh.trend || marketView.label}</em></span>
      <span><b>沪深300</b><em>${hs300.last_close ? fmt(hs300.last_close, 0) : '--'} · ${hs300.trend || marketView.label}</em></span>
      <span><b>阶段</b><em class="${session.tone}">${session.label}</em></span>
      <span><b>组合</b><em class="${pnl >= 0 ? 'up' : 'down'}">${(pnl >= 0 ? '+' : '') + fmt(pnl, 2)}%</em></span>
      <span><b>信号</b><em>${sb.buy_count || 0} 买入 / ${sb.risk_count || 0} 风险</em></span>
      <span><b>仓位</b><em>${pf.positions || 0} 只 · ${fmtCurrency(pf.cash || 0)} 现金</em></span>
    </div>
    <span class="tape-time">${d.timestamp || d.date || 'LIVE'}</span>`;
}

// ─── KPI 栏 ───────────────────────────────────────────────────
function updateKPI() {
  const d = STATE.data;
  if (!d) return;
  const pf = d.portfolio_summary || {};
  const sb = d.signal_brief || {};
  const tt = d.target_tracker || {};

  setKPI('kpi-pnl', fmt(pf.total_profit_pct, 2) + '%', clsPct(pf.total_profit_pct), pf.total_profit_amount ? fmtCurrency(pf.total_profit_amount) : '');
  setKPI('kpi-value', fmtCurrency(pf.total_value), '', pf.positions + ' 只持仓');
  setKPI('kpi-cash', fmtCurrency(pf.cash), '', '可用资金');
  setKPI('kpi-positions', String(pf.positions || 0), '', '当前持仓');
  setKPI('kpi-signals', String((sb.buy_count || 0) + (sb.risk_count || 0)), sb.buy_count > 0 ? 'up' : '', (sb.buy_count || 0) + ' 买入 / ' + (sb.risk_count || 0) + ' 风险');
  setKPI('kpi-target', (tt.progress_pct || 0).toFixed(1) + '%', 'gold', '目标进度');
}

function setKPI(id, value, cls, sub) {
  const el = $(id);
  if (!el) return;
  const vEl = el.querySelector('.kpi-value');
  const sEl = el.querySelector('.kpi-sub');
  if (vEl) {
    if (cls && (cls === 'up' || cls === 'down' || cls === 'gold')) {
      vEl.innerHTML = '<span class="dot-row"><span class="dot-indicator dot-' + cls + '"></span>' + value + '</span>';
    } else {
      vEl.textContent = value;
    }
    vEl.className = 'kpi-value' + (cls ? ' ' + cls : '');
  }
  if (sEl) sEl.textContent = sub || '';
}

// ─── 目标追踪 ──────────────────────────────────────────────────
function updateTargetTracker() {
  const d = STATE.data;
  if (!d) return;
  const tt = d.target_tracker || {};
  const el = $('target-tracker');
  if (!el) return;

  const progress = Math.min(100, tt.progress_pct || 0);
  const monthlyReq = tt.required_monthly_return || 0;
  let adviceText = '';
  if (monthlyReq > 25) adviceText = '月需收益偏高，需更积极策略';
  else if (monthlyReq > 15) adviceText = '⚡ 中等难度，精选标的';
  else if (monthlyReq > 0) adviceText = '节奏正常，按计划执行';
  else adviceText = '🎉 已达成或接近目标！';

  el.innerHTML = `
    <div class="card-header">
      <span class="card-title">翻倍目标追踪</span>
      <span class="card-subtitle">
        ${tt.initial_capital ? fmtCurrency(tt.initial_capital) + ' → ' : ''}${tt.target_capital ? fmtCurrency(tt.target_capital) : '10.2万'} / ${tt.days_total || 90}天
      </span>
    </div>
    <div style="padding:8px 0">
      <div class="target-stats" style="display:flex;justify-content:space-between;margin-bottom:8px">
        <span>进度 <strong class="gold">${progress.toFixed(1)}%</strong></span>
        <span style="color:var(--text-tertiary)">${tt.days_elapsed || 0}/${tt.days_total || 90}天</span>
        <span>需月收益 <strong class="${monthlyReq >= 0 ? 'up' : 'down'}">${monthlyReq >= 0 ? '+' : ''}${monthlyReq.toFixed(1)}%</strong></span>
      </div>
      <div class="target-progress-bar">
        <div class="target-progress-fill" style="width:${progress}%"></div>
      </div>
      <div class="target-labels">
        <span>${fmtCurrency(tt.initial_capital)}</span>
        <span>${fmtCurrency(tt.target_capital)}</span>
      </div>
      <div class="target-status">
        还差 <strong>${fmtCurrency(tt.remaining)}</strong> · ${adviceText}
      </div>
    </div>`;
}

// ─── OVERVIEW TAB ──────────────────────────────────────────────
function renderOverview(d) {
  if (!d) {
    $('tab-overview').innerHTML = '<div class="loading-state"><span class="loading-spinner"></span><div class="loading-text">等待 Serenity 数据...</div></div>';
    return;
  }

  // Build the overview content
  let html = `
    ${buildCommandHero(d)}
    ${buildKPIBar()}
    <div class="mission-grid">
      ${buildSessionModeCard(d)}
      ${buildActionChecklistCard(d)}
    </div>
    ${buildCandidateQueueCard(d)}
    <div class="overview-grid">
      <section class="overview-main">
        ${buildDailyStrategyCard(d)}
        ${buildHoldingsCard(d)}
      </section>
      <aside class="overview-side">
        ${buildSignalBrief(d)}
        <div class="card compact-card" id="position-advice-card">
          <div class="card-header">
            <span class="card-title">仓位优化</span>
            <span class="card-subtitle">Kelly + 信号强度</span>
          </div>
          <div class="card-body" id="position-advice-content">
            <div class="empty-state"><div class="text">加载中...</div></div>
          </div>
        </div>
        ${buildMarketCard(d)}
      </aside>
    </div>
    <div class="overview-grid overview-grid-secondary">
      ${buildScoreCard(d)}
      ${buildOpModeCard(d)}
    </div>
    <div class="overview-grid overview-grid-secondary">
      ${buildGuruCard(d)}
      ${buildAnomalyCard(d)}
    </div>`;

  $('tab-overview').innerHTML = html;
  updateKPI();

  // Load position advice async
  if (d.position_advice) renderPositionAdvice(d.position_advice);

  // Load strategy enhancement (guru sentiment + conviction + anomaly summary)
  loadStrategyEnhancement();
  // Load anomaly alerts
  loadAnomalyData();
}

function buildKPIBar() {
  return `
    <div class="kpi-bar">
      <div class="kpi-card" id="kpi-pnl">
        <div class="kpi-label">总收益</div>
        <div class="kpi-value">--</div>
        <div class="kpi-sub">--</div>
      </div>
      <div class="kpi-card" id="kpi-value">
        <div class="kpi-label">总权益</div>
        <div class="kpi-value">--</div>
        <div class="kpi-sub">--</div>
      </div>
      <div class="kpi-card" id="kpi-cash">
        <div class="kpi-label">可用资金</div>
        <div class="kpi-value">--</div>
        <div class="kpi-sub">--</div>
      </div>
      <div class="kpi-card" id="kpi-positions">
        <div class="kpi-label">持仓</div>
        <div class="kpi-value">--</div>
        <div class="kpi-sub">--</div>
      </div>
      <div class="kpi-card" id="kpi-signals">
        <div class="kpi-label">信号</div>
        <div class="kpi-value">--</div>
        <div class="kpi-sub">--</div>
      </div>
      <div class="kpi-card" id="kpi-target">
        <div class="kpi-label">目标进度</div>
        <div class="kpi-value">--</div>
        <div class="kpi-sub">--</div>
      </div>
    </div>`;
}

function buildSessionModeCard(d) {
  const session = getSessionView(d);
  const steps = session.steps.map(step => {
    const active = step.id === session.id || (session.id === 'closed' && step.id === 'postmarket');
    return `<span class="session-step${active ? ' active' : ''}">${step.short}</span>`;
  }).join('');

  return `
    <div class="mission-card session-mode-card">
      <div class="mission-kicker">Trading Session</div>
      <div class="session-head">
        <div>
          <div class="session-label ${session.tone}">${session.label}</div>
          <div class="session-focus">${session.focus}</div>
        </div>
        <span class="session-window">${session.window}</span>
      </div>
      <div class="session-timeline">${steps}</div>
      <div class="session-next">下一检查点：${session.next}</div>
    </div>`;
}

function buildActionChecklistCard(d) {
  const session = getSessionView(d);
  const items = generateActionItems(d, session);
  const html = items.map((item, idx) => `
    <div class="action-item action-${item.tone}">
      <div class="action-index">${String(idx + 1).padStart(2, '0')}</div>
      <div class="action-copy">
        <div class="action-title">${item.title}</div>
        <div class="action-body">${item.body}</div>
      </div>
      <div class="action-meta">${item.meta || '--'}</div>
    </div>`).join('');

  return `
    <div class="mission-card action-checklist-card">
      <div class="mission-header">
        <div>
          <div class="mission-kicker">Today Actions</div>
          <div class="mission-title">今日行动清单</div>
        </div>
        <span class="mission-count">${items.length} 项</span>
      </div>
      <div class="action-list">${html}</div>
    </div>`;
}

function buildCandidateQueueCard(d) {
  const candidates = getBuyQueue(d);
  const session = getSessionView(d);
  const marketView = getMarketView(d.market || {});

  if (!candidates.length) {
    return `
      <div class="candidate-queue candidate-queue-empty">
        <div class="candidate-head">
          <div>
            <div class="mission-kicker">Explainable Queue</div>
            <div class="mission-title">候选解释队列</div>
          </div>
          <span class="mission-count">0 项</span>
        </div>
        <div class="empty-state"><div class="text">暂无高置信买入候选，当前更适合观察和复盘。</div></div>
      </div>`;
  }

  const rows = candidates.slice(0, 5).map((c, idx) => {
    const factors = getFactorHighlights(d, c.code);
    const factorChips = factors.length
      ? factors.map(f => `<span>${f.label} ${fmt(f.value, 2)}</span>`).join('')
      : '<span>评分与信号共振</span>';
    const amount = c.suggested_amount ? fmtCurrency(c.suggested_amount) : '等仓位';
    const trigger = session.id === 'intraday'
      ? '盘中放量确认'
      : (session.id === 'premarket' ? '开盘后回踩确认' : '明日重新确认');
    const riskNote = marketView.label === '震荡'
      ? '震荡市降低频率'
      : `${marketView.label}环境跟随纪律`;

    return `
      <div class="candidate-row">
        <div class="candidate-rank">#${idx + 1}</div>
        <div class="candidate-main">
          <div class="candidate-title">
            <strong>${c.name}</strong>
            <span>${c.code}</span>
            <em>${actionLabel(c.action)}</em>
          </div>
          <div class="candidate-reason">
            评分 ${fmt(c.score, 0)} · 置信 ${fmt((c.confidence || 0) * 100, 0)}% · ${trigger}
          </div>
          <div class="candidate-factors">${factorChips}</div>
        </div>
        <div class="candidate-side">
          <div class="candidate-score up">${fmt(c.score, 0)}</div>
          <div class="candidate-amount">${amount}</div>
          <div class="candidate-risk">${riskNote}</div>
        </div>
      </div>`;
  }).join('');

  return `
    <div class="candidate-queue">
      <div class="candidate-head">
        <div>
          <div class="mission-kicker">Explainable Queue</div>
          <div class="mission-title">候选解释队列</div>
        </div>
        <span class="mission-count">${candidates.length} 项</span>
      </div>
      <div class="candidate-list">${rows}</div>
    </div>`;
}

function buildCommandHero(d) {
  const pf = d.portfolio_summary || {};
  const mkt = d.market || {};
  const op = d.operational_mode || {};
  const tt = d.target_tracker || {};

  const pnl = pf.total_profit_pct || 0;
  const pnlCls = pnl >= 0 ? 'up' : 'down';
  const marketView = getMarketView(mkt);
  const session = getSessionView(d);
  const execution = getExecutionView(d);
  const modeLabels = { mean_revert: '均值回归', trend: '趋势跟踪', neutral: '中性' };
  const modeLabel = modeLabels[op.mode] || op.mode || '中性';
  const target = tt.progress_pct == null ? '--' : fmt(tt.progress_pct, 1) + '%';
  const actionLabel = execution.label;
  const dailyAdvisory = mkt.overall_advice || op.regime_label || '按评分、风险和反思闭环更新操作节奏';
  const cashRatio = pf.total_value ? Math.max(0, Math.min(100, (pf.cash || 0) / pf.total_value * 100)) : 0;

  return `
    <section class="command-hero command-hero-${execution.tone}">
      <div class="hero-primary">
        <div class="hero-eyebrow">
          <span class="deck-code">SM-ALPHA</span>
          <span>Serenity Command Deck</span>
        </div>
        <h1 class="hero-title">${actionLabel}</h1>
        <div class="hero-subline">${dailyAdvisory}</div>
        <div class="hero-command-row">
          <span class="command-pill command-pill-${marketView.cls}">市场 ${marketView.label}</span>
          <span class="command-pill command-pill-${session.tone}">阶段 ${session.label}</span>
          <span class="command-pill">模式 ${modeLabel}</span>
          <span class="command-pill">主板池</span>
          <span class="command-pill">现金 ${fmt(cashRatio, 0)}%</span>
        </div>
        <div class="hero-confidence">
          <div class="confidence-head">
            <span>执行置信度</span>
            <strong>${fmt(execution.confidence, 0)}%</strong>
          </div>
          <div class="confidence-rail">
            <span class="confidence-fill ${execution.tone}" style="width:${execution.confidence}%"></span>
          </div>
        </div>
      </div>
      <div class="hero-metric">
        <span class="hero-label">组合收益</span>
        <strong class="${pnlCls}">${(pnl >= 0 ? '+' : '') + fmt(pnl, 2)}%</strong>
        <small>${fmtCurrency(pf.total_profit_amount || 0)}</small>
      </div>
      <div class="hero-metric">
        <span class="hero-label">市场状态</span>
        <strong class="${marketView.cls}">${marketView.label}</strong>
        <small>${mkt.overall_trend || '--'}</small>
      </div>
      <div class="hero-metric">
        <span class="hero-label">执行模式</span>
        <strong>${modeLabel}</strong>
        <small>目标 ${target}</small>
      </div>
    </section>`;
}

// ─── SIGNAL BRIEF ─────────────────────────────────────────────
function buildSignalBrief(d) {
  const sb = d.signal_brief || {};
  if (!sb.buy_count && !sb.risk_count) {
    return `
      <div class="signal-brief signal-brief-quiet">
        <div class="signal-brief-title">今日信号 · 无高优先级动作</div>
        <div class="signal-brief-items">
          <div class="signal-chip quiet"><span class="signal-chip-label">继续观察，等待评分共振</span></div>
        </div>
      </div>`;
  }

  let chips = '';
  // Compare with previous signals to detect new signals
  const prev = STATE.prevSignals || {};
  const prevBuyCodes = new Set((prev.buy_candidates || []).map(b => b.code));
  const prevRiskCodes = new Set((prev.risk_alerts || []).map(r => r.code));

  if (sb.buy_candidates) {
    sb.buy_candidates.forEach(b => {
      const isNew = !prevBuyCodes.has(b.code);
      chips += `<div class="signal-chip buy${isNew ? ' new' : ''}"><span class="signal-chip-label"><span class="dot-row"><span class="dot-indicator dot-up"></span>${b.name}</span></span><span class="signal-chip-score">${b.score}分${isNew ? '<span class="signal-new-badge">NEW</span>' : ''}</span></div>`;
    });
  }
  if (sb.risk_alerts) {
    sb.risk_alerts.forEach(r => {
      const isNew = !prevRiskCodes.has(r.code);
      chips += `<div class="signal-chip risk${isNew ? ' new' : ''}"><span class="signal-chip-label"><span class="dot-row"><span class="dot-indicator dot-down"></span>${r.name} (${r.action})</span></span><span class="signal-chip-score">${r.score}分${isNew ? '<span class="signal-new-badge">NEW</span>' : ''}</span></div>`;
    });
  }

  return `
    <div class="signal-brief">
      <div class="signal-brief-title">今日信号 · ${sb.buy_count || 0} 买入 / ${sb.risk_count || 0} 风险</div>
      <div class="signal-brief-items">${chips}</div>
    </div>`;
}

// ─── HOLDINGS CARD ────────────────────────────────────────────
function buildHoldingsCard(d) {
  const pf = d.portfolio_summary || {};
  const details = pf.position_details || [];
  const scores = d.scores || [];

  const scoreMap = {};
  scores.forEach(s => { scoreMap[s.code] = s; });

  let items;
  if (details.length === 0) {
    items = '<div class="empty-state"><div class="text">暂无持仓</div></div>';
  } else {
    items = '<div class="holding-grid">' +
      details.map(p => {
        const isUp = (p.profit_pct || 0) >= 0;
        const sig = scoreMap[p.code] || {};
        const action = sig.signal_action || 'HOLD';
        return `
          <div class="holding-item">
            <div class="holding-name ${isUp ? 'up' : 'down'}">${p.name || '--'}</div>
            <div class="holding-code">${p.code || ''}</div>
            <div class="holding-pnl ${isUp ? 'up' : 'down'}"><span class="dot-row"><span class="dot-indicator dot-${isUp ? 'up' : 'down'}"></span>${(p.profit_pct >= 0 ? '+' : '') + fmt(p.profit_pct, 2)}%</span></div>
            <div class="holding-price">成本 ¥${fmt(p.buy_price)} · 现价 ¥${fmt(p.current_price)}</div>
            <span class="holding-signal ${signClass(action)}">${action}</span>
          </div>`;
      }).join('') + '</div>';
  }

  const totalReturn = pf.total_profit_pct;
  const returnCls = (totalReturn || 0) >= 0 ? 'up' : 'down';

  return `
    <div class="card">
      <div class="card-header">
        <span class="card-title">持仓盈亏</span>
        <span class="card-subtitle">
          ${pf.positions || 0}只 ·
          总权益 <strong class="gold">${fmtCurrency(pf.total_value)}</strong> ·
          浮盈 <strong class="${returnCls}">${(totalReturn >= 0 ? '+' : '') + fmt(totalReturn, 2)}%</strong>
        </span>
      </div>
      <div class="card-body">${items}</div>
    </div>`;
}

// ─── SCORE CARD ───────────────────────────────────────────────
function buildScoreCard(d) {
  const scores = d.scores || [];
  if (!scores.length) return '';

  const items = scores.map(s => {
    const sc = s.total_score || s.score || 0;
    const color = sc >= 65 ? 'var(--up)' : sc >= 50 ? 'var(--gold)' : 'var(--down)';
    return `
      <div class="score-item">
        <div class="score-rank">#${s.rank || '-'}</div>
        <div class="score-name">${s.name}</div>
        <div class="score-value" style="color:${color}"><span class="dot-row"><span class="dot-indicator" style="background:${color};box-shadow:0 0 6px ${color}"></span>${fmt(sc, 0)}</span></div>
        <div class="score-signal">${s.signal_action || 'HOLD'}</div>
      </div>`;
  }).join('');

  return `
    <div class="card">
      <div class="card-header">
        <span class="card-title">评分排行</span>
        <span class="card-subtitle">综合评分 · ${scores.length} 只标的</span>
      </div>
      <div class="card-body">
        <div class="score-strip">${items}</div>
      </div>
    </div>`;
}

// ─── MARKET CARD ──────────────────────────────────────────────
function buildMarketCard(d) {
  const mkt = d.market || {};
  const sh = mkt.sh || {};
  const hs300 = mkt.hs300 || {};

  const rsiVal = mkt.avg_rsi;
  let rsiCls = '';
  let rsiLabel = '';
  if (rsiVal != null) {
    rsiCls = rsiVal >= 70 ? 'down' : (rsiVal <= 30 ? 'up' : 'gold');
    rsiLabel = rsiVal >= 70 ? '过热' : (rsiVal <= 30 ? '过冷' : '');
  }

  const rawSignal = (mkt.overall_signal || '').toLowerCase();
  let sigLabel = '震荡', sigCls = 'gold';
  if (rawSignal.includes('多') || rawSignal === 'bull' || rawSignal === 'bullish') { sigLabel = '多头'; sigCls = 'up'; }
  else if (rawSignal.includes('空') || rawSignal === 'bear' || rawSignal === 'bearish') { sigLabel = '空头'; sigCls = 'down'; }

  return `
    <div class="card">
      <div class="card-header">
        <span class="card-title">大盘择时</span>
        <span class="m-signal-badge ${sigCls}" style="font-size:11px;font-weight:700;padding:2px 8px;border-radius:4px;background:${sigCls === 'up' ? 'rgba(255,70,70,0.15)' : (sigCls === 'down' ? 'rgba(80,200,120,0.15)' : 'rgba(255,200,0,0.15)')};color:var(--${sigCls})">${sigLabel}</span>
      </div>
      <div class="card-body">
        <div class="market-grid">
          <div class="market-item">
            <div class="m-label">上证</div>
            <div class="m-value">${sh.last_close ? fmt(sh.last_close, 0) : '--'}</div>
            <div class="m-trend">${sh.trend || '--'}</div>
          </div>
          <div class="market-item">
            <div class="m-label">沪深300</div>
            <div class="m-value">${hs300.last_close ? fmt(hs300.last_close, 0) : '--'}</div>
            <div class="m-trend">${hs300.trend || '--'}</div>
          </div>
          <div class="market-item">
            <div class="m-label">RSI${rsiLabel ? ` <span style="font-size:10px;font-weight:600;color:var(--${rsiCls})">${rsiLabel}</span>` : ''}</div>
            <div class="m-value ${rsiCls}">${rsiVal != null ? fmt(rsiVal, 1) : '--'}</div>
            <div class="m-trend">${mkt.overall_trend || '--'}</div>
          </div>
        </div>
        ${mkt.overall_advice ? `<div class="advice-banner" style="font-weight:700;text-align:center;padding:10px 12px;margin-top:8px;border:1px solid var(--accent-orange);border-radius:6px;background:rgba(255,160,0,0.08)">${mkt.overall_advice}</div>` : '<div class="advice-banner" style="text-align:center">等待数据...</div>'}
      </div>
    </div>`;
}

// ─── OPMODE CARD ──────────────────────────────────────────────
function buildOpModeCard(d) {
  const op = d.operational_mode || {};
  const mode = op.mode || 'neutral';
  const modeLabels = { mean_revert: '均值回归', trend: '趋势跟踪', neutral: '中性' };
  const modeColors = { mean_revert: 'var(--accent-orange)', trend: 'var(--up)', neutral: 'var(--text-secondary)' };

  return `
    <div class="card">
      <div class="card-header">
        <span class="card-title">操作模式</span>
      </div>
      <div class="card-body">
        <div class="opmode-grid">
          <div class="opmode-item">
            <div class="opmode-label">模式</div>
            <div class="opmode-value" style="color:${modeColors[mode] || '#fff'}">${modeLabels[mode] || mode}</div>
          </div>
          <div class="opmode-item">
            <div class="opmode-label">因子翻转</div>
            <div class="opmode-value" style="color:${op.factor_invert ? 'var(--accent-orange)' : 'var(--text-tertiary)'}">${op.factor_invert ? 'ON' : 'OFF'}</div>
          </div>
          <div class="opmode-item">
            <div class="opmode-label">卖出触发</div>
            <div class="opmode-value gold">${(op.sell_trigger_weight || 1) * 100}%</div>
          </div>
          <div class="opmode-item">
            <div class="opmode-label">市场阶段</div>
            <div class="opmode-value">${op.regime_label || '--'}</div>
          </div>
        </div>
      </div>
    </div>`;
}

// ─── GURU WISDOM CARD ─────────────────────────────────────────
function buildGuruCard(d) {
  // Load guru data from API (async)
  const cardId = 'guru-wisdom-card';
  const placeholderId = 'guru-wisdom-content';

  // Return placeholder card — data loads asynchronously
  setTimeout(() => loadGuruData(cardId, placeholderId), 100);

  return `
    <div class="card" id="${cardId}">
      <div class="card-header">
        <span class="card-title">大师智慧</span>
        <span class="card-subtitle">投资大佬言论 · 市场情绪风向</span>
      </div>
      <div class="card-body" id="${placeholderId}">
        <div class="empty-state"><div class="text">加载中...</div></div>
      </div>
    </div>`;
}

function loadGuruData(cardId, contentId) {
  const el = document.getElementById(contentId);
  if (!el) return;

  fetch('/api/guru')
    .then(r => r.json())
    .then(d => {
      if (!d.ok || !d.stats) {
        el.innerHTML = '<div class="empty-state"><div class="text">暂无数据</div></div>';
        return;
      }

      const s = d.stats;
      const quotes = d.recent_quotes || [];

      // Sentiment bar
      const total = s.bullish + s.bearish + s.neutral;
      const bullW = s.bullish_pct || 0;
      const bearW = s.bearish_pct || 0;
      const neutralW = s.neutral_pct || 0;

      let sentimentBar = '';
      if (total > 0) {
        sentimentBar = `
          <div style="display:flex;height:6px;border-radius:3px;overflow:hidden;margin:8px 0">
            <div style="flex:${bullW};background:#2ECC71;min-width:${bullW > 0 ? '4px' : '0'}"></div>
            <div style="flex:${neutralW};background:#95a5a6;min-width:${neutralW > 0 ? '4px' : '0'}"></div>
            <div style="flex:${bearW};background:#E74C3C;min-width:${bearW > 0 ? '4px' : '0'}"></div>
          </div>`;
      }

      // Stats row
      const statsRow = `
        <div style="display:flex;gap:8px;margin:8px 0;text-align:center;font-size:11px">
          <div style="flex:1;background:rgba(255,255,255,0.04);border-radius:6px;padding:6px 4px">
            <div style="font-size:18px;font-weight:700;color:#E74C3C">${s.total_quotes}</div>
            <div style="color:var(--text-tertiary)">语录</div>
          </div>
          <div style="flex:1;background:rgba(255,255,255,0.04);border-radius:6px;padding:6px 4px">
            <div style="font-size:18px;font-weight:700;color:#2ECC71">${s.bullish_pct}%</div>
            <div style="color:var(--text-tertiary)">看多</div>
          </div>
          <div style="flex:1;background:rgba(255,255,255,0.04);border-radius:6px;padding:6px 4px">
            <div style="font-size:18px;font-weight:700;color:#E74C3C">${s.bearish_pct}%</div>
            <div style="color:var(--text-tertiary)">看空</div>
          </div>
          <div style="flex:1;background:rgba(255,255,255,0.04);border-radius:6px;padding:6px 4px">
            <div style="font-size:18px;font-weight:700;color:#F39C12">${s.gurus}</div>
            <div style="color:var(--text-tertiary)">大师</div>
          </div>
        </div>`;

      // Recent quotes
      let quotesHtml = '';
      if (quotes.length === 0) {
        quotesHtml = '<div style="font-size:12px;color:var(--text-tertiary);text-align:center;padding:8px">暂无语录</div>';
      } else {
        quotesHtml = quotes.map(q => {
          const sentimentLabel = q.sentiment === 'bullish' ? '看多' : (q.sentiment === 'bearish' ? '看空' : '中性');
          const borderColor = q.sentiment === 'bullish' ? '#2ECC71' : (q.sentiment === 'bearish' ? '#E74C3C' : '#95a5a6');
          return `
            <div style="padding:8px 10px;margin:4px 0;background:rgba(255,255,255,0.03);border-left:3px solid ${borderColor};border-radius:0 6px 6px 0">
              <div style="font-size:11px;color:var(--text-secondary);font-weight:600">${sentimentLabel} · ${q.guru}${q.topic ? ' · ' + q.topic : ''}</div>
              <div style="font-size:13px;margin:2px 0;color:var(--text-primary);line-height:1.4">${escapeHtml(q.content)}</div>
              ${q.source ? '<div style="font-size:10px;color:var(--text-tertiary)">来源: ' + escapeHtml(q.source) + '</div>' : ''}
            </div>`;
        }).join('');
      }

      const lastCol = s.last_collection ? s.last_collection.replace('T', ' ').slice(0, 16) : '—';

      el.innerHTML = `
        ${statsRow}
        ${sentimentBar}
        <div style="margin-top:6px">
          <div style="font-size:11px;font-weight:600;color:var(--text-secondary);margin-bottom:4px">最新语录</div>
          ${quotesHtml}
        </div>
        <div style="font-size:10px;color:var(--text-tertiary);text-align:right;margin-top:6px">
          上次采集: ${lastCol}
        </div>`;
    })
    .catch(() => {
      el.innerHTML = '<div class="error-state">加载失败</div>';
    });
}

function escapeHtml(str) {
  if (!str) return '';
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

function cleanDisplayText(str) {
  return (str || '')
    .replace(/[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}\uFE0F]/gu, '')
    .replace(/\*\*/g, '')
    .trim();
}

// ─── DAILY STRATEGY CARD ─────────────────────────────────────
function buildDailyStrategyCard(d) {
  const mkt = d.market || {};
  const op = d.operational_mode || {};
  const sb = d.signal_brief || {};

  // Determine market signal
  const rawSignal = (mkt.overall_signal || '').toLowerCase();
  let sigLabel = '震荡', sigCls = 'neutral';
  if (rawSignal.includes('多') || rawSignal === 'bull' || rawSignal === 'bullish') {
    sigLabel = '多头'; sigCls = 'bull';
  } else if (rawSignal.includes('空') || rawSignal === 'bear' || rawSignal === 'bearish') {
    sigLabel = '空头'; sigCls = 'bear';
  }

  // Determine operational mode
  const mode = op.mode || 'neutral';
  const modeLabels = { mean_revert: '均值回归', trend: '趋势跟踪', neutral: '中性' };
  const modeColors = { mean_revert: 'var(--accent-orange)', trend: 'var(--up)', neutral: 'var(--text-secondary)' };
  const modeLabel = modeLabels[mode] || mode;

  // Signal counts
  const buyCount = sb.buy_count || 0;
  const riskCount = sb.risk_count || 0;

  // Generate comprehensive strategy advice
  const advice = generateStrategyAdvice(sigLabel, mode, buyCount, riskCount, op);

  return `
    <div class="strategy-card">
      <div class="strategy-banner ${sigCls}">
        <div class="strategy-banner-kicker">今日策略</div>
        <div class="strategy-banner-title ${sigCls}">${sigLabel}市场</div>
        <div class="strategy-banner-sub">综合信号 · 操作模式 · 风险提示</div>
      </div>
      <div class="strategy-body">
        <div class="strategy-grid">
          <div class="strategy-stat">
            <div class="strategy-stat-label">大盘状态</div>
            <div class="strategy-stat-value" style="color:${sigCls === 'bull' ? 'var(--up)' : (sigCls === 'bear' ? 'var(--down)' : 'var(--gold)')}">${sigLabel}</div>
            <div style="font-size:10px;color:var(--text-tertiary);margin-top:4px">${mkt.overall_trend || '--'}</div>
          </div>
          <div class="strategy-stat">
            <div class="strategy-stat-label">操作模式</div>
            <div class="strategy-stat-value" style="color:${modeColors[mode] || '#fff'}">${modeLabel}</div>
            <div style="font-size:10px;color:var(--text-tertiary);margin-top:4px">${op.regime_label || '--'}</div>
          </div>
        </div>

        <div class="strategy-signal-row">
          <div class="strategy-count-box buy-box">
            <div class="strategy-count-num"><span class="dot-row"><span class="dot-indicator dot-up"></span>${buyCount}</span></div>
            <div class="strategy-count-label">买入信号</div>
          </div>
          <div class="strategy-count-box risk-box">
            <div class="strategy-count-num"><span class="dot-row"><span class="dot-indicator dot-down"></span>${riskCount}</span></div>
            <div class="strategy-count-label">风险警示</div>
          </div>
        </div>

        <div class="strategy-advice">
          <span class="strategy-advice-icon">提示</span>
          <div class="strategy-advice-text">${advice}</div>
        </div>

        <!-- 策略增强数据（大师情绪+异动摘要，异步加载） -->
        <div id="strategy-enhancement" style="margin-top:12px">
          <div class="empty-state" style="padding:8px"><div class="text" style="font-size:11px">加载大师情绪 & 异动摘要...</div></div>
        </div>
      </div>
    </div>`;
}

function generateStrategyAdvice(marketState, mode, buyCount, riskCount, op) {
  const regime = op.regime_label || '';
  const factorInvert = op.factor_invert || false;
  const sellWeight = (op.sell_trigger_weight || 1) * 100;

  // Build advice based on market_state + mode combination
  let parts = [];

  // Market condition advice
  if (marketState === '多头') {
    parts.push('大盘处于 <strong>多头趋势</strong>，积极做多为主，顺势参与。');
    if (mode === 'trend') {
      parts.push('当前适合 <strong>趋势跟踪</strong>，持仓为主、回调加仓，避免逆势做空。');
    } else if (mode === 'mean_revert') {
      parts.push('多头格局下采用 <strong>均值回归</strong> 需谨慎，建议降低回归仓位权重，等待回调企稳后再介入。');
    } else {
      parts.push('建议 <strong>中性仓位</strong> 参与，观望为主，等待趋势明朗。');
    }
  } else if (marketState === '空头') {
    parts.push('大盘处于 <strong>空头趋势</strong>，以风险控制为主，降低仓位。');
    if (mode === 'mean_revert') {
      parts.push('空头格局适合 <strong>均值回归</strong> 策略，关注超跌反弹机会，轻仓快进快出。');
    } else if (mode === 'trend') {
      parts.push('趋势跟踪在空头市场需 <strong>严格止损</strong>，仅保留最强标的，缩短持仓周期。');
    } else {
      parts.push('建议 <strong>轻仓观望</strong>，等待市场企稳信号出现。');
    }
  } else {
    parts.push('大盘处于 <strong>震荡格局</strong>，高抛低吸为主，控制仓位。');
    if (mode === 'mean_revert') {
      parts.push('震荡市适合 <strong>均值回归</strong> 策略，关注支撑位低吸、阻力位高抛。');
    } else if (mode === 'trend') {
      parts.push('趋势跟踪在震荡行情中容易被反复止损，建议 <strong>降低频率</strong>，等待突破信号确认。');
    } else {
      parts.push('建议 <strong>中性仓位</strong> 操作，利用震荡区间做波段。');
    }
  }

  // Factor invert warning
  if (factorInvert) {
    parts.push('<strong>因子翻转已触发</strong>，多空逻辑反转，注意调整方向判断。');
  }

  // Signal-based advice
  if (buyCount > 3) {
    parts.push(`买入信号较密集(${buyCount}个)，可适度增仓，但需精选标的。`);
  } else if (buyCount > 0) {
    parts.push(`有 ${buyCount} 个买入信号，逢低关注。`);
  } else {
    parts.push('暂无买入信号，耐心等待。');
  }

  if (riskCount > 2) {
    parts.push(`<strong>风险警示较多(${riskCount}个)</strong>，建议收缩仓位，降低风险敞口。卖出触发阈值为 ${sellWeight}%。`);
  } else if (riskCount > 0) {
    parts.push(`有 ${riskCount} 个风险提示，注意持仓防守。`);
  }

  if (regime) {
    parts.push(`市场阶段：${regime}。`);
  }

  return parts.join(' ');
}

// ─── HOLDINGS TAB ─────────────────────────────────────────────
function renderHoldingsTab(d) {
  if (!d) return;

  let html = '';

  // Target tracker
  html += `<div class="target-tracker" id="target-tracker"></div>`;

  // Holdings card (same as overview but larger)
  html += buildHoldingsCard(d);

  // Position advice
  html += `
    <div class="card">
      <div class="card-header">
        <span class="card-title">📐 仓位优化建议</span>
        <span class="card-subtitle">Kelly公式 + 信号强度</span>
      </div>
      <div class="card-body" id="advice-full">加载中...</div>
    </div>`;

  // Sector rotation
  const sectors = d.sectors || [];
  if (sectors.length) {
    const sectorItems = sectors.map(s => `
      <div class="sector-item">
        <span class="sector-name">${s.sector}</span>
        <span class="sector-change ${clsPct(s.change)}">${pctStr(s.change)}</span>
      </div>`).join('');
    html += `
      <div class="card">
        <div class="card-header">
          <span class="card-title">🔄 行业轮动</span>
        </div>
        <div class="card-body"><div class="sector-grid">${sectorItems}</div></div>
      </div>`;
  }

  // Ratings
  const ratings = d.ratings || [];
  if (ratings.length) {
    const ratingItems = ratings.map(r => `
      <div class="rating-item">
        <div class="rating-dot rating-${(r.rating || 'N/A').replace('/', '\\/')}">${r.rating || '?'}</div>
        <div class="rating-name">${r.name}</div>
        <div class="rating-sub">${r.signal_label || ''}</div>
      </div>`).join('');
    html += `
      <div class="card">
        <div class="card-header">
          <span class="card-title">⭐ 综合评级</span>
        </div>
        <div class="card-body"><div class="rating-grid">${ratingItems}</div></div>
      </div>`;
  }

  $('tab-holdings').innerHTML = html;
  updateTargetTracker();

  // Load position advice
  if (d.position_advice) renderFullAdvice(d.position_advice);

  // Load ETF & dividend
  renderETF(d);
  renderDividend(d);
}

function renderETF(d) {
  const etf = d.etf_top5 || [];
  if (!etf.length) return;
  const items = etf.map((e, i) => {
    const sc = e.total_score || 0;
    return `<div class="sector-item">
      <span class="sector-name">#${e.rank || i + 1} ${e.name || e.etf_code}</span>
      <span class="sector-change ${sc >= 70 ? 'up' : sc >= 50 ? 'gold' : 'down'}">${fmt(sc, 0)}分</span>
    </div>`;
  }).join('');
  const el = $('tab-holdings');
  if (el) el.insertAdjacentHTML('beforeend', `
    <div class="card">
      <div class="card-header"><span class="card-title">📈 ETF 动量轮动 Top 5</span></div>
      <div class="card-body"><div class="sector-grid">${items || '<div class="text-faint" style="padding:8px">暂无数据</div>'}</div></div>
    </div>`);
}

function renderDividend(d) {
  const div = d.dividend_top5 || [];
  if (!div.length) return;
  const items = div.map(r => {
    const sc = r.total_score || 0;
    return `<div class="sector-item">
      <span class="sector-name">${r.name || r.code}</span>
      <span class="sector-change ${sc >= 70 ? 'up' : sc >= 50 ? 'gold' : 'down'}">${fmt(sc, 0)}分</span>
    </div>`;
  }).join('');
  const el = $('tab-holdings');
  if (el) el.insertAdjacentHTML('beforeend', `
    <div class="card">
      <div class="card-header"><span class="card-title">💰 红利低波 Top 5</span></div>
      <div class="card-body"><div class="sector-grid">${items || '<div class="text-faint" style="padding:8px">暂无数据</div>'}</div></div>
    </div>`);
}

// ─── FACTORS TAB ─────────────────────────────────────────────
function renderFactorsTab(d) {
  if (!d) return;
  const factors = d.factors || [];
  const sf = d.signal_factors || [];
  const fl = d.factor_labels || {};
  if (!factors.length) {
    $('tab-factors').innerHTML = '<div class="empty-state"><div class="icon">🧮</div><div class="text">暂无因子数据</div></div>';
    return;
  }

  let table = `<div class="card">
    <div class="card-header"><span class="card-title">🧮 14因子信号矩阵</span></div>
    <div class="card-body"><div class="factor-table-wrap">
    <table class="factor-table"><thead><tr>
      <th style="min-width:48px">标的</th>
      ${sf.map(f => '<th>' + (fl[f] || f) + '</th>').join('')}
    </tr></thead><tbody>`;

  factors.forEach(stk => {
    table += '<tr><td class="stock-col">' + stk.name + '</td>';
    sf.forEach(f => {
      let v = stk[f];
      let cls = '';
      let disp = '—';
      if (v != null) { disp = fmt(v, 3); cls = v >= 0 ? 'high' : 'low'; }
      table += '<td class="factor-val' + (cls ? ' ' + cls : '') + '">' + disp + '</td>';
    });
    table += '</tr>';
  });

  table += '</tbody></table></div></div></div>';

  // Market timing (also in factors tab)
  table += buildMarketCard(d);
  table += buildOpModeCard(d);

  $('tab-factors').innerHTML = table;
}

// ─── RISK TAB ─────────────────────────────────────────────────
function renderRiskTab(d) {
  if (!d) return;

  const pf = d.portfolio_summary || {};
  const sb = d.signal_brief || {};

  let html = `
    <div class="card">
      <div class="card-header">
        <span class="card-title">🛡️ 风控概览</span>
      </div>
      <div class="card-body">
        <div class="gauge-grid" id="risk-gauges">
          <div class="gauge-card">
            <div class="gauge-label">日收益率</div>
            <div class="gauge-value">${pctStr(pf.total_profit_pct || 0)}</div>
          </div>
          <div class="gauge-card">
            <div class="gauge-label">持仓数</div>
            <div class="gauge-value gold">${pf.positions || 0}</div>
            <div class="gauge-limit">最大 10 只</div>
          </div>
          <div class="gauge-card">
            <div class="gauge-label">风险信号</div>
            <div class="gauge-value" style="color:${sb.risk_count > 0 ? 'var(--down)' : 'var(--up)'}">${sb.risk_count || 0}</div>
            <div class="gauge-limit">需关注</div>
          </div>
          <div class="gauge-card">
            <div class="gauge-label">现金比例</div>
            <div class="gauge-value">${pf.cash && pf.total_value ? ((pf.cash / pf.total_value) * 100).toFixed(0) : '--'}%</div>
          </div>
        </div>
      </div>
    </div>

    <!-- 净值曲线 -->
    <div class="card" id="nav-card">
      <div class="card-header">
        <span class="card-title">📈 净值曲线</span>
      </div>
      <div class="card-body">
        <div class="chart-container">
          <canvas id="navChart"></canvas>
        </div>
        <div class="nav-stats" id="nav-chart-stats">
          <div class="nav-stat"><span class="nav-stat-label">起始</span><span class="nav-stat-value" id="nav-start">--</span></div>
          <div class="nav-stat"><span class="nav-stat-label">最新</span><span class="nav-stat-value" id="nav-end">--</span></div>
          <div class="nav-stat"><span class="nav-stat-label">收益率</span><span class="nav-stat-value" id="nav-return">--</span></div>
          <div class="nav-stat"><span class="nav-stat-label">最高</span><span class="nav-stat-value" id="nav-high">--</span></div>
          <div class="nav-stat"><span class="nav-stat-label">最低</span><span class="nav-stat-value" id="nav-low">--</span></div>
        </div>
      </div>
    </div>

    <!-- 信号绩效 -->
    <div class="card" id="signal-perf-card">
      <div class="card-header">
        <span class="card-title">📊 信号类型绩效</span>
        <span class="card-subtitle">全部历史</span>
      </div>
      <div id="signal-perf-content" class="card-body">
        <div class="empty-state"><div class="icon">📊</div><div class="text">加载中...</div></div>
      </div>
    </div>

    <!-- 维度有效性 -->
    <div class="card" id="dimension-perf-card">
      <div class="card-header">
        <span class="card-title">🔬 评分维度预测力</span>
        <span class="card-subtitle">corr vs 1日收益</span>
      </div>
      <div id="dimension-perf-content" class="card-body">
        <div class="empty-state"><div class="icon">🔬</div><div class="text">加载中...</div></div>
      </div>
    </div>`;

  $('tab-risk').innerHTML = html;

  // Load auxiliary data
  loadNavHistory();
  setTimeout(loadSignalPerformance, 200);
  setTimeout(loadDimensionEffectiveness, 400);
}

// ─── ANALYSIS TAB ─────────────────────────────────────────────
function renderAnalysisTab(d) {
  if (!d) return;

  let html = `
    <!-- 因子 IC -->
    <div class="card" id="factor-ic-card">
      <div class="card-header">
        <span class="card-title">📊 因子 IC 归因</span>
        <span class="card-subtitle">近30天 · Rank IC</span>
      </div>
      <div id="factor-ic-content" class="card-body">
        <div class="empty-state"><div class="icon">📊</div><div class="text">加载中...</div></div>
      </div>
    </div>
    <!-- 信号历史 -->
    <div class="card" id="signal-history-card">
      <div class="card-header">
        <span class="card-title">📊 近7天买入信号绩效</span>
      </div>
      <div id="signal-history-content" class="card-body">
        <div class="empty-state"><div class="icon">📊</div><div class="text">加载中...</div></div>
      </div>
    </div>
    <!-- 交易日志 -->
    <div class="card" id="journal-card">
      <div class="card-header">
        <span class="card-title">📝 交易日志</span>
        <span class="card-subtitle">最近交易与反思状态</span>
      </div>
      <div id="journal-content" class="card-body">
        <div class="empty-state"><div class="icon">📝</div><div class="text">加载中...</div></div>
      </div>
    </div>`;

  $('tab-analysis').innerHTML = html;

  setTimeout(loadFactorIC, 100);
  setTimeout(loadSignalHistory, 300);
  setTimeout(loadJournal, 500);
}

// ─── POSITION ADVICE ──────────────────────────────────────────
function renderPositionAdvice(pa) {
  const el = $('position-advice-content');
  if (!el) return;
  if (!pa || pa.error) {
    el.innerHTML = '<div class="text-faint" style="text-align:center;padding:12px">暂无建议</div>';
    return;
  }
  const ha = pa.holdings_advice || [];
  const bc = pa.buy_candidates || [];

  const suggestMap = {
    ADD: { label: '➕ 加仓', cls: 'ADD' },
    REDUCE: { label: '➖ 减仓', cls: 'REDUCE' },
    EXIT: { label: '✕ 清仓', cls: 'EXIT' },
    TAKE_PAR: { label: '💰 止盈', cls: 'TAKE_PAR' },
    TAKE_PARTIAL: { label: '💰 止盈', cls: 'TAKE_PAR' },
    WATCH: { label: '👁 观察', cls: 'WATCH' },
    HOLD: { label: '持有', cls: 'HOLD' },
  };

  let html = '<div class="advice-list">';
  if (ha.length) {
    ha.forEach(a => {
      const sm = suggestMap[a.suggest] || { label: a.suggest, cls: 'HOLD' };
      html += `
        <div class="advice-row">
          <div class="advice-info">
            <span class="advice-name ${a.profit_pct >= 0 ? 'up' : 'down'}">${a.name}</span>
            <span class="advice-reason">${a.reason || ''}</span>
          </div>
          <div>
            <span class="advice-tag ${sm.cls}"><span class="dot-indicator advice-dot-${sm.cls}"></span>${sm.label}</span>
            ${a.kelly_max_amount > 0 ? `<span class="text-faint" style="font-size:9px;margin-left:6px">Kelly ¥${fmt(a.kelly_max_amount, 0)}</span>` : ''}
          </div>
        </div>`;
    });
  }
  html += '</div>';

  if (bc.length) {
    html += '<div style="margin-top:12px;padding-top:12px;border-top:1px solid var(--border-light)">';
    html += '<div style="font-size:10px;font-weight:600;color:var(--up);margin-bottom:8px">🟢 买入候选</div>';
    bc.forEach(b => {
      html += `<div class="flex items-center justify-between" style="padding:4px 0;border-bottom:1px solid var(--border-light)">
        <span style="font-size:12px"><strong>${b.name}</strong> <span class="text-muted" style="font-size:10px">${b.score}分</span></span>
        <span style="font-size:10px;color:var(--up)">${b.suggested_shares}股 ¥${fmt(b.suggested_amount, 0)}</span>
      </div>`;
    });
    html += '</div>';
  }

  html += `<div class="text-faint text-right" style="font-size:9px;margin-top:8px">可用现金: ${fmtCurrency(pa.cash)}</div>`;
  el.innerHTML = html;
}

function renderFullAdvice(pa) {
  const el = $('advice-full');
  if (el) renderPositionAdvice(Object.assign({}, pa)); // Uses same render logic
}

// ─── 性能工具 ──────────────────────────────────────────────────

/** 防抖 — 在 delay ms 内连续调用只执行最后一次 */
function debounce(fn, delay) {
  let timer = null;
  return function (...args) {
    clearTimeout(timer);
    timer = setTimeout(() => fn.apply(this, args), delay);
  };
}

/**
 * LTTB (Largest Triangle Three Buckets) 降采样
 * 在保留视觉形态的前提下大幅减少数据点
 * 参考: https://github.com/sveinn-steinarsson/flot-downsample
 *
 * @param {Array<{x: number, y: number}>} data  原始数据
 * @param {number} threshold  目标点数上限
 * @returns {Array<{x: number, y: number}>}  降采样后的数据
 */
function lttbDownsample(data, threshold) {
  const len = data.length;
  if (threshold >= len || threshold <= 2) return data;

  // 始终保留首尾
  const sampled = [data[0]];
  const bucketSize = (len - 2) / (threshold - 2);

  let a = 0; // 上一个选中的点索引
  for (let i = 0; i < threshold - 2; i++) {
    const bucketStart = Math.floor((i + 0) * bucketSize) + 1;
    const bucketEnd   = Math.floor((i + 1) * bucketSize) + 1;
    const avgRangeEnd = Math.min(bucketEnd, len - 1);

    // 计算当前 bucket 的平均点（用于三角形面积计算）
    let avgX = 0, avgY = 0, avgCount = 0;
    for (let j = bucketStart; j < avgRangeEnd; j++) {
      avgX += data[j].x;
      avgY += data[j].y;
      avgCount++;
    }
    if (avgCount === 0) continue;
    avgX /= avgCount;
    avgY /= avgCount;

    // 在 bucket 中找到与 (data[a], avg) 构成最大三角形的点
    let maxArea = -1, maxAreaIdx = bucketStart;
    const bucketEndActual = Math.min(bucketEnd, len - 1);
    for (let j = bucketStart; j < bucketEndActual; j++) {
      // 三角形面积 = abs((x_a - x_j)*(y_avg - y_a) - (x_a - x_avg)*(y_j - y_a))
      const area = Math.abs(
        (data[a].x - data[j].x) * (avgY - data[a].y) -
        (data[a].x - avgX) * (data[j].y - data[a].y)
      );
      if (area > maxArea) {
        maxArea = area;
        maxAreaIdx = j;
      }
    }
    sampled.push(data[maxAreaIdx]);
    a = maxAreaIdx;
  }

  // 始终保留最后一个点
  sampled.push(data[len - 1]);
  return sampled;
}

/**
 * 对净值数据进行降采样（如果数据量超过阈值）
 * 返回处理后的数据
 */
function downsampleNavData(data) {
  const threshold = 500; // 超过此点数开始降采样
  if (!data || data.length <= threshold) return data;

  const start = performance.now();
  const mapped = data.map((d, i) => ({
    x: i,
    y: d.value || 0,
    date: d.date,
    profit_pct: d.profit_pct || 0,
  }));
  const sampled = lttbDownsample(mapped, threshold);

  // 恢复原始结构
  const result = sampled.map(s => ({
    date: s.date,
    value: s.y,
    profit_pct: s.profit_pct,
  }));

  const ms = (performance.now() - start).toFixed(1);
  console.log(`[perf] LTTB: ${data.length} → ${result.length} points (${ms}ms)`);
  return result;
}

// ─── NAV HISTORY (Chart.js) ────────────────────────────────────
function loadNavHistory() {
  fetch('/api/nav-history')
    .then(r => r.json())
    .then(d => {
      if (!d.ok || !d.data || !d.data.length) return;
      STATE.navHistory = d.data;
      // 降采样后再渲染
      const sampled = downsampleNavData(d.data);
      renderNavChart(sampled);
    })
    .catch(() => { /* silent fail */ });
}

// ─── 防抖调窗 ──────────────────────────────────────────────────
const debouncedResize = debounce(function () {
  const canvas = $('navChart');
  if (!canvas) return;
  // Chart.js 自动处理 resize（responsive: true）
  // 但如果图表被创建时处于 display:none 状态，需要手动 redraw
  if (STATE.chartInstance) {
    STATE.chartInstance.resize();
  }
}, 250);

window.addEventListener('resize', debouncedResize);

function renderNavChart(data) {
  const canvas = $('navChart');
  if (!canvas) return;

  const ctx = canvas.getContext('2d');

  // Destroy previous chart
  if (STATE.chartInstance) {
    STATE.chartInstance.destroy();
    STATE.chartInstance = null;
  }

  const dates = data.map(r => r.date);
  const values = data.map(r => r.value || 0);
  const pcts = data.map(r => r.profit_pct || 0);

  // Calculate stats
  const startVal = values[0];
  const endVal = values[values.length - 1];
  const maxVal = Math.max(...values);
  const minVal = Math.min(...values);
  const totalReturn = pcts[pcts.length - 1] || 0;

  // Update stats display
  const setStat = (id, val, cls) => {
    const el = $(id);
    if (el) { el.textContent = val; el.className = 'nav-stat-value' + (cls ? ' ' + cls : ''); }
  };
  setStat('nav-start', '¥' + fmt(startVal, 0));
  setStat('nav-end', '¥' + fmt(endVal, 0));
  setStat('nav-return', (totalReturn >= 0 ? '+' : '') + fmt(totalReturn, 2) + '%', totalReturn >= 0 ? 'up' : 'down');
  setStat('nav-high', '¥' + fmt(maxVal, 0));
  setStat('nav-low', '¥' + fmt(minVal, 0));

  // Detect container size
  const container = canvas.parentElement;
  const w = container.clientWidth || 600;

  // Chart.js configuration - Bloomberg style
  STATE.chartInstance = new Chart(ctx, {
    type: 'line',
    data: {
      labels: dates,
      datasets: [{
        label: '净值',
        data: values,
        borderColor: '#FFD700',
        backgroundColor: function(context) {
          const chart = context.chart;
          const { ctx, chartArea } = chart;
          if (!chartArea) return 'rgba(255,215,0,0.08)';
          const gradient = ctx.createLinearGradient(0, chartArea.top, 0, chartArea.bottom);
          gradient.addColorStop(0, 'rgba(255,215,0,0.15)');
          gradient.addColorStop(1, 'rgba(255,215,0,0.01)');
          return gradient;
        },
        fill: true,
        borderWidth: 2,
        pointRadius: 0,
        pointHoverRadius: 5,
        pointHoverBackgroundColor: '#FFD700',
        pointHoverBorderColor: '#000',
        pointHoverBorderWidth: 2,
        tension: 0.1,
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: {
        intersect: false,
        mode: 'index',
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: '#1E2230',
          titleColor: '#E8EAED',
          bodyColor: '#E8EAED',
          borderColor: 'rgba(255,255,255,0.1)',
          borderWidth: 1,
          padding: 10,
          displayColors: false,
          callbacks: {
            title: items => items[0].label,
            label: item => '¥' + fmt(item.raw, 0),
          }
        },
      },
      scales: {
        x: {
          grid: { color: 'rgba(255,255,255,0.04)', drawBorder: false },
          ticks: {
            color: 'rgba(255,255,255,0.25)',
            maxTicksLimit: 8,
            font: { size: 9, family: 'SF Mono, monospace' },
          },
        },
        y: {
          grid: { color: 'rgba(255,255,255,0.04)', drawBorder: false },
          ticks: {
            color: 'rgba(255,255,255,0.25)',
            font: { size: 9, family: 'SF Mono, monospace' },
            callback: v => '¥' + Number(v).toFixed(0),
          },
        },
      },
    }
  });
}

// ─── SIGNAL HISTORY ───────────────────────────────────────────
function loadSignalHistory() {
  const el = $('signal-history-content');
  if (!el) return;

  fetch('/api/signal-history')
    .then(r => r.json())
    .then(d => {
      if (!d.ok || !d.data || !d.data.length) {
        el.innerHTML = '<div class="text-faint" style="text-align:center;padding:12px">暂无买入信号</div>';
        return;
      }

      let html = '<div class="data-table-wrap"><table class="data-table"><thead><tr>';
      html += '<th>信号</th><th>标的</th><th>日期</th><th class="text-right">评分</th><th class="text-right">1日收益</th><th class="text-right">3日收益</th><th class="text-right">价格</th>';
      html += '</tr></thead><tbody>';

      d.data.forEach(s => {
        const iconMap = { 'STRONG_BUY': '🟢🟢🟢', 'BUY': '🟢🟢', 'CAUTION_BUY': '🟢' };
        const icon = iconMap[s.action] || '⚪';
        const o1d = s.outcome_1d != null ? (s.outcome_1d >= 0 ? '+' : '') + fmt(s.outcome_1d, 1) + '%' : '—';
        const o3d = s.outcome_3d != null ? (s.outcome_3d >= 0 ? '+' : '') + fmt(s.outcome_3d, 1) + '%' : '—';
        const o1dCls = s.outcome_1d != null ? (s.outcome_1d >= 0 ? 'up' : 'down') : '';
        const o3dCls = s.outcome_3d != null ? (s.outcome_3d >= 0 ? 'up' : 'down') : '';

        html += `<tr>
          <td>${icon}</td>
          <td style="font-weight:600">${s.name}</td>
          <td class="text-faint">${s.date} ${s.time || ''}</td>
          <td class="text-right">${fmt(s.score, 0)}</td>
          <td class="text-right ${o1dCls}">${o1d}</td>
          <td class="text-right ${o3dCls}">${o3d}</td>
          <td class="text-right text-faint">¥${fmt(s.price)}</td>
        </tr>`;
      });

      html += '</tbody></table></div>';
      el.innerHTML = html;
    })
    .catch(() => {
      el.innerHTML = '<div class="error-state" style="color:var(--down)">加载失败</div>';
    });
}

// ─── SIGNAL PERFORMANCE ───────────────────────────────────────
function loadSignalPerformance() {
  const el = $('signal-perf-content');
  if (!el) return;

  fetch('/api/signal-performance')
    .then(r => r.json())
    .then(d => {
      if (!d.ok || !d.signal_actions || !d.signal_actions.length) {
        el.innerHTML = '<div class="text-faint" style="text-align:center;padding:12px">暂无数据</div>';
        return;
      }

      const s = d.summary || {};
      let html = '';

      // Summary line
      html += `<div class="flex justify-between" style="font-size:10px;color:var(--text-tertiary);margin-bottom:8px">
        <span>信号 ${s.total_signals || 0} | 已结算 ${s.with_outcome || 0}</span>
        <span>胜率 ${s.overall_win_rate != null ? fmt(s.overall_win_rate * 100, 1) + '%' : 'N/A'}</span>
        <span>均收益 ${s.overall_avg_return != null ? fmt(s.overall_avg_return, 2) + '%' : 'N/A'}</span>
      </div>`;

      html += '<div class="data-table-wrap"><table class="data-table"><thead><tr>';
      html += '<th>信号</th><th class="text-right">次数</th><th class="text-right">1日收益</th><th class="text-right">1日胜率</th><th class="text-right">3日胜率</th>';
      html += '</tr></thead><tbody>';

      d.signal_actions.forEach(sa => {
        const ar1 = sa.avg_return_1d != null ? fmt(sa.avg_return_1d, 2) + '%' : 'N/A';
        const wr1 = sa.win_rate_1d != null ? fmt(sa.win_rate_1d * 100, 1) + '%' : 'N/A';
        const wr3 = sa.win_rate_3d != null ? fmt(sa.win_rate_3d * 100, 1) + '%' : 'N/A';
        const ar1Cls = sa.avg_return_1d != null ? (sa.avg_return_1d >= 0 ? 'up' : 'down') : '';
        const wr1Cls = sa.win_rate_1d != null ? (sa.win_rate_1d >= 0.4 ? 'up' : (sa.win_rate_1d >= 0.3 ? 'gold' : 'down')) : '';

        html += `<tr>
          <td style="font-weight:500">${sa.action}</td>
          <td class="text-right">${sa.total}</td>
          <td class="text-right ${ar1Cls}">${ar1}</td>
          <td class="text-right ${wr1Cls}" style="font-weight:600">${wr1}</td>
          <td class="text-right ${wr1Cls}">${wr3}</td>
        </tr>`;
      });

      html += '</tbody></table></div>';
      el.innerHTML = html;
    })
    .catch(() => {
      el.innerHTML = '<div class="error-state">加载失败</div>';
    });
}

// ─── DIMENSION EFFECTIVENESS ──────────────────────────────────
function loadDimensionEffectiveness() {
  const el = $('dimension-perf-content');
  if (!el) return;

  fetch('/api/signal-performance')
    .then(r => r.json())
    .then(d => {
      if (!d.ok || !d.dimensions || !d.dimensions.length) {
        el.innerHTML = '<div class="text-faint" style="text-align:center;padding:12px">暂无数据</div>';
        return;
      }

      let html = '<div class="data-table-wrap"><table class="data-table"><thead><tr>';
      html += '<th>维度</th><th class="text-right">样本</th><th class="text-right">corr_1d</th><th class="text-right">正收益%</th><th class="text-right">强区间</th>';
      html += '</tr></thead><tbody>';

      d.dimensions.forEach(dim => {
        const bestBin = dim.bins.reduce((a, b) => a.avg_return > b.avg_return ? a : b, dim.bins[0]);
        const bbLabel = bestBin ? bestBin.range : '';
        const corrCls = Math.abs(dim.rank_corr_1d) >= 0.1 ? 'up' : 'gold';
        const ppCls = dim.positive_pct >= 0.35 ? 'up' : (dim.positive_pct >= 0.3 ? 'gold' : 'down');

        html += `<tr>
          <td style="font-weight:500">${dim.dimension.replace('_score', '')}</td>
          <td class="text-right text-faint">${dim.samples}</td>
          <td class="text-right ${corrCls}" style="font-weight:600">${(dim.rank_corr_1d >= 0 ? '+' : '') + fmt(dim.rank_corr_1d, 3)}</td>
          <td class="text-right ${ppCls}">${fmt(dim.positive_pct * 100, 1)}%</td>
          <td class="text-right text-faint">${bbLabel}</td>
        </tr>`;
      });

      html += '</tbody></table></div>';
      el.innerHTML = html;
    })
    .catch(() => {
      el.innerHTML = '<div class="error-state">加载失败</div>';
    });
}

// ─── FACTOR IC ────────────────────────────────────────────────
function loadFactorIC() {
  const el = $('factor-ic-content');
  if (!el) return;

  fetch('/api/factor-ic')
    .then(r => r.json())
    .then(d => {
      if (!d.ok || !d.ic_summary || !d.ic_summary.length) {
        el.innerHTML = '<div class="text-faint" style="text-align:center;padding:12px">暂无数据</div>';
        return;
      }

      let html = '';
      const top = d.top_factors || [];
      const weak = d.weak_factors || [];

      // Top factors
      if (top.length) {
        html += '<div style="margin-bottom:12px"><span style="font-size:10px;font-weight:600;color:var(--up)">🏆 最有效因子</span></div>';
        top.forEach(f => {
          html += `<div class="flex items-center justify-between" style="padding:4px 0;border-bottom:1px solid var(--border-light)">
            <span style="font-weight:500;font-size:12px"><span class="dot-row"><span class="dot-indicator dot-${f.ic >= 0 ? 'up' : 'down'}"></span>${f.label}</span></span>
            <span class="font-num ${f.ic >= 0 ? 'up' : 'down'}" style="font-weight:600">${(f.ic >= 0 ? '+' : '') + fmt(f.ic, 3)}</span>
          </div>`;
        });
      }

      // Weak factors
      if (weak.length) {
        html += '<div style="margin-top:12px;margin-bottom:8px"><span style="font-size:10px;font-weight:600;color:var(--down)">⚠️ 最无效因子</span></div>';
        weak.forEach(f => {
          html += `<div class="flex items-center justify-between" style="padding:4px 0;border-bottom:1px solid var(--border-light)">
            <span style="font-weight:500;font-size:12px"><span class="dot-row"><span class="dot-indicator dot-${f.ic >= 0 ? 'up' : 'down'}"></span>${f.label}</span></span>
            <span class="font-num ${f.ic >= 0 ? 'up' : 'down'}" style="font-weight:600">${(f.ic >= 0 ? '+' : '') + fmt(f.ic, 3)}</span>
          </div>`;
        });
      }

      // Full IC table
      html += '<div style="margin-top:16px;padding-top:12px;border-top:1px solid var(--border-light)">';
      html += '<span style="font-size:10px;font-weight:600;color:var(--text-secondary);margin-bottom:8px;display:block">📋 因子 IC 明细</span>';
      html += '<div class="data-table-wrap"><table class="data-table"><thead><tr>';
      html += '<th>维度</th><th class="text-right">最新 IC</th><th class="text-right">均值 IC</th><th class="text-right">IC-IR</th><th class="text-right">胜率</th>';
      html += '</tr></thead><tbody>';

      d.ic_summary.forEach(s => {
        html += `<tr>
          <td style="font-weight:500">${s.label}</td>
          <td class="text-right ${s.latest_ic >= 0 ? 'up' : 'down'}" style="font-weight:600">${(s.latest_ic >= 0 ? '+' : '') + fmt(s.latest_ic, 3)}</td>
          <td class="text-right">${(s.mean_ic >= 0 ? '+' : '') + fmt(s.mean_ic, 3)}</td>
          <td class="text-right ${s.ic_ir >= 0.5 ? 'up' : (s.ic_ir <= -0.5 ? 'down' : 'gold')}">${fmt(s.ic_ir, 2)}</td>
          <td class="text-right">${fmt(s.win_rate, 0)}%</td>
        </tr>`;
      });

      html += '</tbody></table></div></div>';
      el.innerHTML = html;
    })
    .catch(() => {
      el.innerHTML = '<div class="error-state">加载失败</div>';
    });
}

// ─── JOURNAL ──────────────────────────────────────────────────
function loadJournal() {
  const el = $('journal-content');
  if (!el) return;

  fetch('/api/journal')
    .then(r => r.json())
    .then(d => {
      if (!d.ok) {
        el.innerHTML = '<div class="text-faint" style="text-align:center;padding:12px">暂无数据</div>';
        return;
      }

      const entries = d.entries || [];
      const stats = d.stats || {};
      let html = '';

      // Stats line
      html += `<div class="flex justify-between" style="font-size:10px;color:var(--text-tertiary);margin-bottom:8px">
        <span>总计 <strong class="gold">${stats.total || 0}</strong> 条</span>
        <span style="color:${(stats.no_reflection || 0) > 0 ? 'var(--accent-orange)' : 'var(--up)'}">
          ${(stats.no_reflection || 0) > 0 ? '📝 ' + stats.no_reflection + ' 条未反思' : '✅ 全部已反思'}
        </span>
      </div>`;

      if (!entries.length) {
        html += '<div class="text-faint" style="text-align:center;padding:12px">暂无交易日志</div>';
        el.innerHTML = html;
        return;
      }

      html += '<div class="data-table-wrap"><table class="data-table"><thead><tr>';
      html += '<th>交易</th><th>标的</th><th class="text-right">盈亏</th><th class="text-center">反思</th>';
      html += '</tr></thead><tbody>';

      entries.slice(0, 5).forEach(e => {
        const actionIcon = e.action === 'buy' ? '🟢' : '🔴';
        let profitStr = '—';
        let profitCls = '';
        if (e.profit_pct != null) {
          profitStr = (e.profit_pct >= 0 ? '+' : '') + fmt(e.profit_pct, 2) + '%';
          profitCls = e.profit_pct >= 0 ? 'up' : 'down';
        }
        const hasReflection = e.reflection && e.reflection.trim() !== '';
        const reasonStr = e.reason && e.reason.trim() ? e.reason : '—';

        html += `<tr>
          <td>${actionIcon}</td>
          <td style="font-weight:500">${e.name}<div class="text-faint" style="font-size:9px">${e.date} · ${reasonStr.substring(0, 24)}${reasonStr.length > 24 ? '...' : ''}</div></td>
          <td class="text-right ${profitCls}" style="font-weight:600">${profitStr}</td>
          <td class="text-center" style="color:${hasReflection ? 'var(--up)' : 'var(--text-tertiary)'}">${hasReflection ? '✅' : '⬜'}</td>
        </tr>`;
      });

      html += '</tbody></table></div>';
      el.innerHTML = html;
    })
    .catch(() => {
      el.innerHTML = '<div class="error-state">加载失败</div>';
    });
}

// ─── MODALS ───────────────────────────────────────────────────
function showTrade() {
  fetch('/api/monitor-data')
    .then(r => r.json())
    .then(d => {
      const scores = d.data.scores || [];
      const options = scores.map(s =>
        `<option value="${s.code}">${s.name} (${s.code}) 评分:${fmt(s.total_score || s.score || 0, 1)}</option>`
      ).join('');

      const html = `
        <div class="modal-overlay" onclick="closeModal(event)">
          <div class="modal-box" onclick="event.stopPropagation()">
            <div class="modal-title">📊 调仓操作</div>
            <form class="modal-form" onsubmit="submitTrade(event)">
              <select name="code">${options}</select>
              <select name="action"><option value="buy">买入</option><option value="sell">卖出</option></select>
              <input name="price" type="number" step="0.01" placeholder="成交价格" required>
              <input name="qty" type="number" step="1" placeholder="数量(股)" required>
              <input name="note" placeholder="备注(可选)">
              <button type="submit" class="modal-btn modal-btn-primary">确认提交</button>
            </form>
          </div>
        </div>`;
      showModal(html);
    });
}

function submitTrade(e) {
  e.preventDefault();
  const f = e.target;
  const data = {
    code: f.code.value, action: f.action.value,
    price: parseFloat(f.price.value), quantity: parseInt(f.qty.value),
    note: f.note.value,
  };
  fetch('/api/trades', {
    method: 'POST',
    headers: writeHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(data),
  })
    .then(r => r.json())
    .then(d => {
      alert(d.ok ? '✅ ' + d.msg : '❌ ' + d.msg);
      closeModal();
      refresh();
    });
}

function showConfig() {
  fetch('/api/monitor-data')
    .then(r => r.json())
    .then(d => {
      const scores = d.data.scores || [];
      const options = scores.map(s =>
        `<option value="${s.code}">${s.name} (${s.code})</option>`
      ).join('');

      const html = `
        <div class="modal-overlay" onclick="closeModal(event)">
          <div class="modal-box" onclick="event.stopPropagation()">
            <div class="modal-title">⚙️ 持仓设置</div>
            <form class="modal-form" onsubmit="submitConfig(event)">
              <select name="code">${options}</select>
              <input name="stop_loss" type="number" step="0.01" placeholder="止损价">
              <input name="target_high" type="number" step="0.01" placeholder="止盈目标上限">
              <input name="target_low" type="number" step="0.01" placeholder="止盈目标下限">
              <button type="submit" class="modal-btn modal-btn-danger">保存设置</button>
            </form>
          </div>
        </div>`;
      showModal(html);
    });
}

function submitConfig(e) {
  e.preventDefault();
  const f = e.target;
  const data = { code: f.code.value };
  if (f.stop_loss.value) data.stop_loss = parseFloat(f.stop_loss.value);
  if (f.target_high.value) data.target_high = parseFloat(f.target_high.value);
  if (f.target_low.value) data.target_low = parseFloat(f.target_low.value);
  fetch('/api/config', {
    method: 'POST',
    headers: writeHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(data),
  })
    .then(r => r.json())
    .then(d => {
      alert(d.ok ? '✅ ' + d.msg : '❌ ' + d.msg);
      closeModal();
    });
}

function showModal(html) {
  const el = document.createElement('div');
  el.id = 'modal-container';
  el.innerHTML = html;
  document.body.appendChild(el);
}

function closeModal(e) {
  if (e && e.target !== e.currentTarget) return;
  const el = document.getElementById('modal-container');
  if (el) el.remove();
}

// ─── STRATEGY ENHANCEMENT — 大师情绪 + 最强买入 + 异动摘要 ────
function loadStrategyEnhancement() {
  const el = document.getElementById('strategy-enhancement');
  if (!el) return;

  fetch('/api/today-strategy')
    .then(r => r.json())
    .then(d => {
      if (!d.ok) {
        el.innerHTML = '<div style="font-size:11px;color:var(--text-tertiary);text-align:center;padding:6px">增强数据暂不可用</div>';
        return;
      }

      let parts = [];

      // Guru sentiment summary
      const gs = d.guru_summary;
      if (gs && gs.gurus) {
        parts.push(`<div class="enh-guru-bar">
          <div class="enh-section-title">大师情绪 · ${gs.gurus}位大佬</div>
          <div class="enh-sentiment-row">
            <span class="enh-sent-bull">看多 ${gs.bullish_pct}%</span>
            <span class="enh-sent-neutral">中性 ${gs.neutral_pct}%</span>
            <span class="enh-sent-bear">看空 ${gs.bearish_pct}%</span>
          </div>
          <div class="enh-quote">“${gs.latest_quote || ''}”</div>
        </div>`);
      }

      // Conviction picks
      const cv = d.conviction;
      if (cv && cv.length > 0) {
        let picks = cv.slice(0, 3).map(p =>
          `<span class="enh-conviction-chip">${p.code} ${p.name} ${p.signal}·${p.score}分</span>`
        ).join('');
        parts.push(`<div class="enh-conviction-bar">
          <div class="enh-section-title">最强买入信号</div>
          <div class="enh-chip-row">${picks}</div>
        </div>`);
      }

      // Anomaly summary
      const anom = d.anomaly_summary;
      if (anom) {
        let alerts = [];
        if (anom.emergency > 0) alerts.push(`<span class="enh-alert enh-alert-a">${anom.emergency} 紧急</span>`);
        if (anom.warning > 0) alerts.push(`<span class="enh-alert enh-alert-b">${anom.warning} 警告</span>`);
        if (anom.info > 0) alerts.push(`<span class="enh-alert enh-alert-c">${anom.info} 提示</span>`);
        if (alerts.length > 0) {
          parts.push(`<div class="enh-anomaly-bar">
            <div class="enh-section-title">盘中异动</div>
            <div>${alerts.join(' ')}</div>
          </div>`);
        }
      }

      if (parts.length === 0) {
        el.innerHTML = '<div style="font-size:11px;color:var(--text-tertiary);text-align:center;padding:6px">暂无增强数据</div>';
      } else {
        el.innerHTML = parts.join('');
      }
    })
    .catch(() => {
      el.innerHTML = '<div style="font-size:11px;color:var(--text-tertiary);text-align:center;padding:6px">增强数据加载失败</div>';
    });
}

// ─── ANOMALY CARD — 盘中异动告警 ─────────────────────────────
function buildAnomalyCard(d) {
  return `
    <div class="card" id="anomaly-card">
      <div class="card-header">
        <span class="card-title">盘中异动</span>
        <span class="card-subtitle">价格异动 · 信号突变 · 实时告警</span>
      </div>
      <div class="card-body" id="anomaly-card-content">
        <div class="empty-state"><div class="text">加载中...</div></div>
      </div>
    </div>`;
}

function loadAnomalyData() {
  const el = document.getElementById('anomaly-card-content');
  if (!el) return;

  fetch('/api/anomalies')
    .then(r => r.json())
    .then(d => {
      if (!d.ok || !d.anomalies) {
        el.innerHTML = '<div class="empty-state"><div class="text">暂无未确认异动</div></div>';
        return;
      }

      const anomalies = d.anomalies || [];
      const stats = d.stats || {};
      const total = anomalies.length;

      if (total === 0) {
        el.innerHTML = '<div class="empty-state"><div class="text">暂无未确认异动</div></div>';
        return;
      }

      // Stats chips
      let statChips = '';
      if (stats.emergency > 0) statChips += `<span class="enh-alert enh-alert-a">紧急 ${stats.emergency}</span> `;
      if (stats.warning > 0) statChips += `<span class="enh-alert enh-alert-b">警告 ${stats.warning}</span> `;
      if (stats.info > 0) statChips += `<span class="enh-alert enh-alert-c">提示 ${stats.info}</span> `;

      // Anomaly items
      const items = anomalies.slice(0, 10).map(a => {
        const levelClass = a.level === 'A' ? 'enh-alert-a' : (a.level === 'B' ? 'enh-alert-b' : 'enh-alert-c');
        const levelLabel = a.level === 'A' ? '紧急' : (a.level === 'B' ? '警告' : '提示');
        const time = a.created_at ? a.created_at.replace('T', ' ').slice(0, 16) : '--';
        return `<div class="anomaly-item ${levelClass}">
          <div class="anomaly-item-header">
            <span class="anomaly-level-badge ${levelClass}">${levelLabel} · ${a.level}级</span>
            <span class="anomaly-code">${a.code || '--'}</span>
            <span class="anomaly-type">${a.alert_type || '--'}</span>
            <span class="anomaly-time">${time}</span>
          </div>
          ${a.message ? `<div class="anomaly-msg">${escapeHtml(cleanDisplayText(a.message))}</div>` : ''}
        </div>`;
      }).join('');

      el.innerHTML = `
        <div style="margin-bottom:8px;font-size:11px;color:var(--text-tertiary)">${statChips}共 ${total} 条未确认</div>
        <div class="anomaly-list">${items}</div>`;
    })
    .catch(() => {
      el.innerHTML = '<div class="error-state">异动数据加载失败</div>';
    });
}
function showError(msg) {
  const timeEl = $('header-time');
  if (timeEl) timeEl.textContent = '刷新失败';

  if (STATE.data) {
    const active = qs('.tab-content.active');
    if (!active || active.querySelector('.refresh-notice')) return;
    active.insertAdjacentHTML('afterbegin', `<div class="refresh-notice">${msg}，保留上次稳定数据</div>`);
    return;
  }

  qsa('.tab-content.active').forEach(tc => {
    const target = qs('#overview-content', tc) || tc;
    target.innerHTML = `<div class="error-state">${msg}</div>`;
  });
}

// ─── INIT ─────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', init);
