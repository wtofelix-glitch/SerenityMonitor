// 共享辅助：确定性三要素（冻结时间 / 全量 API mock / 漏网断言）
const fs = require('fs');
const path = require('path');

// 冻结时刻：2026-07-15（周三）10:30:00 → 盘中（session=盘中，刷新周期 5s）
const FROZEN_TIME = new Date('2026-07-15T10:30:00+08:00');

const FIX_DIR = path.join(__dirname, 'fixtures');
const fixture = (name) => JSON.parse(fs.readFileSync(path.join(FIX_DIR, name + '.json'), 'utf-8'));

// pathname 前缀 → fixture 名。route 时按最长前缀匹配。
const ROUTE_TABLE = [
  ['/api/monitor-data', 'monitor-data'],
  ['/api/indices', 'indices'],
  ['/api/nav-history', 'nav-history'],
  ['/api/quick-snapshot', 'quick-snapshot'],
  ['/api/operations-center', 'operations-center'],
  ['/api/research/brief', 'research-brief'],
  ['/api/sentinel/status', 'sentinel-status'],
  ['/api/sentinel/fusion', 'sentinel-fusion'],
  ['/api/nl-query', 'nl-query'],
  ['/api/config/', 'config-code'],     // 动态 /api/config/<code>
  ['/api/alerts/gate-change', 'gate-change'], // 外部 3001 Telegram 桥 — 必须拦截防真实推送
  // 风控/运营/治理 tab 的 fetchJSON 端点
  ['/api/compare', 'compare'],
  ['/api/paper-portfolio', 'paper-portfolio'],
  ['/api/risk-matrix', 'risk-matrix'],
  ['/api/anomalies', 'anomalies'],
  ['/api/factor-ic-dashboard', 'factor-ic-dashboard'],
  ['/api/signal-performance', 'signal-performance'],
  ['/api/execution-plan', 'execution-plan'],
  ['/api/backtest/', 'backtest'],      // 动态 /api/backtest/<code>
  ['/api/journal', 'journal'],
  ['/api/v4/governance', 'governance'],
];

/**
 * 拦截所有 /api/**（含跨域 3001），返回确定性 fixture。
 * overrides: { '/api/monitor-data': 'monitor-data-empty' } 或 { '/api/monitor-data': { status: 500 } }
 * 返回 unmocked 数组 — 用例结束时断言为空（漏网请求 = 用例失败）。
 */
async function mockAllApis(page, overrides = {}) {
  const unmocked = [];
  await page.route('**/api/**', (route) => {
    const url = new URL(route.request().url());
    const p = url.pathname;

    const ovKey = Object.keys(overrides).find((k) => p === k || p.startsWith(k));
    if (ovKey) {
      const ov = overrides[ovKey];
      if (typeof ov === 'string') return route.fulfill({ json: fixture(ov) });
      if (ov && ov.abort) return route.abort('failed');
      if (ov && ov.hang) return; // 永不响应 → loading 骨架屏
      return route.fulfill({ status: ov.status || 500, json: ov.body || { ok: false, error: 'test' } });
    }

    const hit = ROUTE_TABLE.find(([prefix]) => p === prefix || p.startsWith(prefix));
    if (hit) return route.fulfill({ json: fixture(hit[1]) });

    unmocked.push(url.href);
    return route.fulfill({ status: 500, json: { ok: false, error: 'unmocked endpoint' } });
  });
  return unmocked;
}

// CLS 采集：必须在导航前注册（addInitScript），否则错过加载期的 layout-shift
async function installClsObserver(page) {
  await page.addInitScript(() => {
    window.__cls = 0;
    new PerformanceObserver((list) => {
      for (const e of list.getEntries()) {
        if (!e.hadRecentInput) window.__cls += e.value;
      }
    }).observe({ type: 'layout-shift', buffered: true });
  });
}

const readCls = (page) => page.evaluate(() => window.__cls || 0);

// 打开看板：冻结时钟 → mock → goto → 等首屏渲染完成 → 停掉刷新定时器（防截图时序抖动）
async function openMonitor(page, { overrides = {}, waitRender = true } = {}) {
  await page.clock.setFixedTime(FROZEN_TIME);
  const unmocked = await mockAllApis(page, overrides);
  await page.goto('/monitor');
  if (waitRender) {
    // header-time 从 "⟳ 刷新中..." 变为数据时间戳 = renderAll 完成
    await page.waitForFunction(
      () => {
        const t = document.getElementById('header-time');
        return t && t.textContent && !t.textContent.includes('刷新中') && t.textContent !== '--';
      },
      { timeout: 15_000 }
    );
    await stopTimers(page);
  }
  return unmocked;
}

// 停掉自动刷新与盯盘间隔器，让页面进入静止状态
async function stopTimers(page) {
  await page.evaluate(() => {
    try { clearTimeout(STATE.refreshInterval); } catch (e) {}
    try { if (typeof _liveTimer !== 'undefined' && _liveTimer) clearInterval(_liveTimer); } catch (e) {}
  });
}

// 切 tab 并等待渲染（switchTab 是全局函数；renderedTabs 缓存命中即视为完成）
async function gotoTab(page, tab) {
  await page.evaluate((t) => switchTab(t), tab);
  await page.waitForFunction(
    (t) => {
      const el = document.getElementById('tab-' + t);
      return el && !el.hidden && el.innerHTML.length > 0 && !el.querySelector('.dashboard-skeleton');
    },
    tab,
    { timeout: 15_000 }
  );
  await page.waitForTimeout(250); // 图表/字体最后一帧稳定
}

const TABS = ['overview', 'holdings', 'risk', 'sentinel', 'operations', 'live', 'governance'];
const VIEWPORTS = [
  { w: 375, h: 812, tag: '375' },
  { w: 768, h: 1024, tag: '768' },
  { w: 1920, h: 1080, tag: '1920' },
];

module.exports = { FROZEN_TIME, fixture, mockAllApis, installClsObserver, readCls, openMonitor, stopTimers, gotoTab, TABS, VIEWPORTS };
