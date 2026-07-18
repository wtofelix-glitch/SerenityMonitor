// SW 行为专测 — 仅在 chromium-sw project 运行（serviceWorkers: 'allow'）
// 断言 R0.3 目标行为：/api/** network-only + no-store；静态资产网络优先。
// ⚠️ Commit A 阶段整体 skip（当前 sw.js 仍是 v6 全 GET 缓存回退）；Commit B 移除 skip。
const { test, expect } = require('@playwright/test');

test.describe.skip('Service Worker v7 安全行为（Commit B 启用）', () => {
  // SW 测试不 mock API —— 打真实 Flask，验证的是 SW 网络层行为而非渲染
  async function loadWithSw(page) {
    await page.goto('/monitor');
    await page.evaluate(() => navigator.serviceWorker.ready);
    // 首访无 controller（SW 首次安装不接管已打开页面）→ reload 让 v7 接管
    await page.reload();
    await page.evaluate(() => navigator.serviceWorker.ready);
  }

  test('controller 为 v7，CacheStorage 无 v6', async ({ page }) => {
    // 预置残留 v6 缓存，验证 activate 真正清理
    await page.goto('/monitor');
    await page.evaluate(async () => {
      const c = await caches.open('serenity-v6');
      await c.put('/static/manifest.json', new Response('{}'));
    });
    await loadWithSw(page);
    const keys = await page.evaluate(() => caches.keys());
    expect(keys).toContain('serenity-v7');
    expect(keys).not.toContain('serenity-v6');
    const hasController = await page.evaluate(() => !!navigator.serviceWorker.controller);
    expect(hasController).toBe(true);
  });

  test('断网时 /api/** 失败，绝不返回缓存 200', async ({ page, context }) => {
    await loadWithSw(page);
    // 先在线访问一次 API（若 SW 有缓存倾向，此时会被污染）
    const online = await page.evaluate(() =>
      fetch('/api/monitor-data').then((r) => r.status).catch(() => 'network-error')
    );
    expect(online).toBe(200);

    await context.setOffline(true);
    const offline = await page.evaluate(() =>
      fetch('/api/monitor-data').then((r) => ({ status: r.status, fromCache: true })).catch(() => 'network-error')
    );
    // 核心断言：断网 API 必须是网络错误，不能出现任何形式的 200
    expect(offline).toBe('network-error');
    await context.setOffline(false);
  });

  test('断网时静态资产回退缓存（离线打开能力保留）', async ({ page, context }) => {
    await loadWithSw(page);
    // 在线时静态资产已进缓存
    await page.evaluate(() => fetch('/static/css/theme-legacy.css').then((r) => r.status));
    await context.setOffline(true);
    const status = await page.evaluate(() =>
      fetch('/static/css/theme-legacy.css').then((r) => r.status).catch(() => 'network-error')
    );
    expect(status).toBe(200); // 来自 CacheStorage 回退
    await context.setOffline(false);
  });

  test('/api/** 响应头 Cache-Control: no-store', async ({ page }) => {
    await loadWithSw(page);
    const cc = await page.evaluate(() =>
      fetch('/api/monitor-data').then((r) => r.headers.get('cache-control'))
    );
    expect(cc).toBe('no-store');
  });

  test('非 2xx 响应不进缓存', async ({ page, context }) => {
    await loadWithSw(page);
    // 请求一个 404 静态路径
    await page.evaluate(() => fetch('/static/css/nonexistent.css').then((r) => r.status).catch(() => {}));
    const cached = await page.evaluate(async () => {
      const c = await caches.open('serenity-v7');
      const hit = await c.match('/static/css/nonexistent.css');
      return !!hit;
    });
    expect(cached).toBe(false);
  });
});
