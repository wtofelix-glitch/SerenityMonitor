/* Serenity Dashboard v6.0 — lightweight, mobile-first */

const D = {
  data: null, tab: "overview", timer: null, staleSec: 0,
  API: "/api/dashboard",
};

const $ = (id) => document.getElementById(id);

/* ── Bootstrap ───────────────────────────────── */
document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => switchTab(btn.dataset.tab));
  });
  fetchData().then(() => renderTab(D.tab));
  startRefresh();
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) { fetchData().then(() => renderTab(D.tab)); }
  });
  window.addEventListener("online", () => {
    fetchData().then(() => renderTab(D.tab));
  });
});

/* ── Data ────────────────────────────────────── */
async function fetchData() {
  try {
    const res = await fetch(D.API, { signal: AbortSignal.timeout(8000) });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    D.data = await res.json();
    D.staleSec = 0;
    updateMeta();
    updateAlerts();
  } catch (e) {
    D.staleSec += 30;
    updateMeta();
  }
}

function startRefresh() {
  clearInterval(D.timer);
  D.timer = setInterval(() => {
    const mkt = D.data?.market_status;
    if (mkt === "trading" && !document.hidden) {
      D.staleSec += 30;
      fetchData().then(() => renderTab(D.tab));
    }
  }, 60000);
}

/* ── Meta ────────────────────────────────────── */
function updateMeta() {
  const time = D.data?.generated_at || "";
  const short = time.slice(11, 19) || "--";
  $("update-time").textContent = short;

  const dot = $("data-age");
  dot.className = "age-dot";
  const ms = D.data?.module_status || {};
  if (!D.data) { dot.classList.add("unavailable"); return; }
  const bad = Object.values(ms).filter((v) => v !== "fresh").length;
  if (bad > 2) dot.classList.add("unavailable");
  else if (bad > 0) dot.classList.add("stale");
  else dot.classList.add("fresh");
}

/* ── Alerts ──────────────────────────────────── */
function updateAlerts() {
  const banner = $("alert-banner");
  if (!D.data) { banner.hidden = true; return; }
  const r = D.data.risk || {};
  const alerts = [];
  if (r.observation_mode && r.observation_mode !== "NORMAL")
    alerts.push(`观察模式: ${r.observation_mode}`);
  if (r.kill_switch)
    alerts.push(`熔断触发: ${r.kill_switch_type || "未知"}`);
  if (r.daily_loss_locked)
    alerts.push("日亏损锁定");
  if (alerts.length) {
    banner.hidden = false;
    banner.className = "alert-banner critical";
    banner.textContent = "⚠ " + alerts.join(" · ");
  } else {
    banner.hidden = true;
  }
}

/* ── Tab switching ───────────────────────────── */
function switchTab(id) {
  D.tab = id;
  document.querySelectorAll(".tab-btn").forEach((b) => {
    b.classList.toggle("active", b.dataset.tab === id);
  });
  renderTab(id);
}

function renderTab(id) {
  if (!D.data) {
    $("tab-content").innerHTML = stateSkeleton();
    return;
  }
  if (id === "overview") renderOverview();
  else if (id === "detail") renderDetail();
  else if (id === "oos") renderOOS();
}

/* ══════════════════════════════════════════════
   Overview
   ══════════════════════════════════════════════ */
function renderOverview() {
  const d = D.data;
  let h = "";

  // ── NAV card ──
  const pf = d.portfolio || {};
  const nav = pf.nav ?? "--";
  const profit = pf.profit_pct ?? 0;
  const profitClass = profit > 0 ? "text-green" : profit < 0 ? "text-red" : "";
  h += `<div class="card nav-card">
    <div class="nav-primary">¥${num(nav)}<span class="profit ${profitClass}">${profit > 0 ? "+" : ""}${profit.toFixed(1)}%</span></div>
    <div class="nav-secondary">
      <span>💰 现金 <strong>¥${num(pf.cash)}</strong></span>
      <span>📈 市值 <strong>¥${num(pf.holdings_value)}</strong></span>
    </div>
  </div>`;

  // ── Position cards ──
  const pos = d.positions || [];
  if (pos.length) {
    h += `<div class="pos-row">`;
    for (const p of pos) {
      const pnlClass = p.profit_pct >= 0 ? "text-green" : "text-red";
      h += `<div class="pos-card">
        <div class="pos-left">
          <span class="pos-name">${esc(p.name)} <span class="text-dim">${p.code}</span></span>
          <span class="pos-detail">${p.shares}股 · ¥${p.price.toFixed(2)}</span>
        </div>
        <div class="pos-right">
          <span class="pos-value">¥${num(p.value)}</span>
          <span class="pos-pnl ${pnlClass}">${p.profit_pct >= 0 ? "+" : ""}${p.profit_pct.toFixed(1)}%</span>
        </div>
      </div>`;
    }
    h += `</div>`;
  } else {
    h += stateEmpty("暂无持仓");
  }

  // ── Signal chips ──
  const sig = d.signals || {};
  if (sig && Object.keys(sig).length) {
    h += `<div class="signal-row">`;
    if (sig.buy) h += `<span class="signal-chip buy">🟢 买入 ${sig.buy}</span>`;
    if (sig.caution_buy) h += `<span class="signal-chip" style="border-color:var(--amber);color:var(--amber)">🟡 谨慎 ${sig.caution_buy}</span>`;
    if (sig.hold) h += `<span class="signal-chip">⚪ 持有 ${sig.hold}</span>`;
    if (sig.sell) h += `<span class="signal-chip sell">🔴 卖出 ${sig.sell}</span>`;
    if (!sig.buy && !sig.caution_buy && !sig.hold && !sig.sell) h += `<span class="signal-chip">无信号</span>`;
    h += `</div>`;
  }

  // ── OOS progress ──
  const oos = d.oos || {};
  if (oos.trading_days !== undefined) {
    const pct = oos.progress_pct || 0;
    h += `<div class="oos-bar">
      <div class="label">OOS 实验进度</div>
      <div class="track"><div class="fill" style="width:${Math.min(pct,100)}%"></div></div>
      <div class="info">Day ${oos.trading_days} / ${oos.min_required} · ${pct.toFixed(1)}%</div>
    </div>`;
  }

  $("tab-content").innerHTML = h;
}

/* ══════════════════════════════════════════════
   Detail
   ══════════════════════════════════════════════ */
function renderDetail() {
  const d = D.data;
  let h = "";

  // ── Scoring top5 ──
  const sc = d.scoring || {};
  if (sc.top5 && sc.top5.length) {
    h += `<div class="card"><div class="card-header">评分 Top 5</div>`;
    h += `<table class="data-table"><thead><tr><th>标的</th><th class="num">评分</th><th>信号</th></tr></thead><tbody>`;
    for (const s of sc.top5) {
      h += `<tr><td>${esc(s.name)} <span class="text-dim">${s.code}</span></td><td class="num">${s.score.toFixed(1)}</td><td>${esc(s.signal)}</td></tr>`;
    }
    h += `</tbody></table></div>`;
  }

  // ── Holdings scoring ──
  if (sc.holdings && sc.holdings.length) {
    h += `<div class="card"><div class="card-header">持仓评分</div>`;
    h += `<table class="data-table"><tbody>`;
    for (const s of sc.holdings) {
      h += `<tr><td>${esc(s.name)}</td><td class="num">${s.score.toFixed(1)}</td><td>${esc(s.signal)}</td></tr>`;
    }
    h += `</tbody></table></div>`;
  }

  // ── Factors ──
  const f = d.factors || {};
  if (f.top3 && f.top3.length) {
    h += `<div class="card"><div class="card-header">因子 ICIR</div>`;
    h += `<div class="mb-8">`;
    for (const x of f.top3) {
      const cls = x.value > 0 ? "pos" : "neg";
      h += `<span class="factor-chip ${cls}">${x.dim}: ${x.value >= 0 ? "+" : ""}${x.value.toFixed(2)}</span>`;
    }
    h += `</div>`;
    if (f.worst2 && f.worst2.length) {
      h += `<div class="text-dim">弱势: `;
      h += f.worst2.map((x) => `${x.dim} ${x.value >= 0 ? "+" : ""}${x.value.toFixed(2)}`).join(" · ");
      h += `</div>`;
    }
    h += `</div>`;
  } else {
    h += stateEmpty("因子数据暂不可用");
  }

  // ── Risk full ──
  const r = d.risk || {};
  if (r && Object.keys(r).length) {
    h += `<div class="card"><div class="card-header">风控状态</div>`;
    const items = [
      { label: "观察模式", value: r.observation_mode || "unknown", ok: r.observation_mode === "NORMAL" },
      { label: "熔断", value: r.kill_switch ? "触发" : "正常", ok: !r.kill_switch },
      { label: "日亏损锁", value: r.daily_loss_locked ? "锁定" : "正常", ok: !r.daily_loss_locked },
    ];
    h += `<div class="status-grid">`;
    for (const it of items) {
      h += `<span class="status-item ${it.ok ? "ok" : "warn"}">${it.ok ? "✓" : "⚠"} ${it.label}: ${it.value}</span>`;
    }
    h += `</div></div>`;
  }

  $("tab-content").innerHTML = h || stateEmpty("详情数据暂不可用");
}

/* ══════════════════════════════════════════════
   OOS
   ══════════════════════════════════════════════ */
function renderOOS() {
  const d = D.data;
  let h = "";

  const oos = d.oos || {};
  if (oos.trading_days !== undefined) {
    const pct = oos.progress_pct || 0;
    h += `<div class="card">
      <div class="card-header">OOS 冻结实验</div>
      <div class="mb-8"><span class="text-dim">实验 #${oos.experiment_id || "?"} · 启动 ${(oos.started_at || "").slice(0, 10)}</span></div>
      <div class="oos-bar" style="margin:0">
        <div class="track"><div class="fill" style="width:${Math.min(pct, 100)}%"></div></div>
        <div class="info">${oos.trading_days} / ${oos.min_required} 交易日 · ${pct.toFixed(1)}%</div>
      </div>
      <div class="mt-8 text-dim">判定点约 2027-01-04 · 不显示排名 · 120天后见</div>
    </div>`;
  } else {
    h += stateEmpty("OOS 实验尚未启动");
  }

  // ── Module status table ──
  const ms = d.module_status || {};
  if (Object.keys(ms).length) {
    h += `<div class="card"><div class="card-header">模块状态</div>`;
    h += `<table class="data-table"><tbody>`;
    for (const [mod, status] of Object.entries(ms)) {
      const icon = status === "fresh" ? "✓" : "✗";
      const cls = status === "fresh" ? "text-green" : "text-amber";
      h += `<tr><td>${mod}</td><td class="num ${cls}">${icon} ${status}</td></tr>`;
    }
    h += `</tbody></table></div>`;
  }

  // ── Risk (also shown in OOS tab for completeness) ──
  const r = d.risk || {};
  if (r && r.observation_mode) {
    const mode = r.observation_mode;
    const cls = mode === "NORMAL" ? "ok" : "warn";
    h += `<div class="card"><div class="card-header">风控</div>`;
    h += `<div class="status-grid">
      <span class="status-item ${cls}">观察: ${mode}</span>
      <span class="status-item ${r.kill_switch ? 'warn' : 'ok'}">熔断: ${r.kill_switch ? '触发' : '正常'}</span>
    </div></div>`;
  }

  $("tab-content").innerHTML = h || stateEmpty("OOS 数据暂不可用");
}

/* ══════════════════════════════════════════════
   States
   ══════════════════════════════════════════════ */
function stateSkeleton() {
  return `<div class="state-skeleton"><span></span><span></span><span></span></div>`;
}
function stateEmpty(msg) {
  return `<div class="state-empty">${esc(msg)}</div>`;
}
function stateError(msg) {
  return `<div class="state-error"><div class="msg">${esc(msg)}</div><button onclick="fetchData().then(()=>renderTab(D.tab))">重试</button></div>`;
}

/* ── Helpers ─────────────────────────────────── */
function num(v) { if (v == null || isNaN(v)) return "--"; return Number(v).toLocaleString("zh-CN", { maximumFractionDigits: 0 }); }
function esc(s) { if (!s) return ""; const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }
