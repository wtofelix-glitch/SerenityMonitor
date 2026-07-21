// R1 Terminal Noir 视觉基线 — 精简矩阵（3 视口 × overview tab）
// noir 主题通过 SERENITY_THEME=terminal-noir 环境变量注入
// 全量 7×3 矩阵在 R1 稳定运行 ≥3 交易日后扩展
const { test, expect } = require('@playwright/test');
const { openMonitor, gotoTab, VIEWPORTS } = require('./helpers');

test.describe('Noir 视觉矩阵 3×1', () => {
  for (const vp of VIEWPORTS) {
    const isMobileCase = vp.tag === '375' ? ' @mobile' : '';
    test(`overview-${vp.tag}${isMobileCase}`, async ({ page }) => {
      await page.setViewportSize({ width: vp.w, height: vp.h });
      const unmocked = await openMonitor(page);
      await gotoTab(page, 'overview');
      await expect(page).toHaveScreenshot(`noir-overview-${vp.tag}.png`, { fullPage: true });
      expect(unmocked, '存在未 mock 的 API 请求').toEqual([]);
    });
  }
});

test.describe('Noir 极端状态 @mobile', () => {
  test.use({ viewport: { width: 375, height: 812 } });

  test('loading-skeleton @mobile', async ({ page }) => {
    await openMonitor(page, {
      overrides: {
        '/api/quick-snapshot': { hang: true },
        '/api/monitor-data': { hang: true },
      },
      waitRender: false,
    });
    await page.waitForFunction(() => {
      const el = document.getElementById('tab-overview');
      return el && el.textContent.includes('加载中');
    }, { timeout: 10_000 });
    await page.waitForTimeout(300);
    await expect(page).toHaveScreenshot('noir-state-loading.png', { fullPage: true });
  });

  test('error-500 @mobile', async ({ page }) => {
    await openMonitor(page, { overrides: { '/api/monitor-data': { status: 500 } }, waitRender: false });
    await page.waitForTimeout(2500);
    await expect(page).toHaveScreenshot('noir-state-error.png', { fullPage: true });
  });

  test('empty-data @mobile', async ({ page }) => {
    const unmocked = await openMonitor(page, { overrides: { '/api/monitor-data': 'monitor-data-empty' } });
    await gotoTab(page, 'overview');
    await expect(page).toHaveScreenshot('noir-state-empty.png', { fullPage: true });
    expect(unmocked).toEqual([]);
  });

  test('long-numbers @mobile', async ({ page }) => {
    const unmocked = await openMonitor(page, { overrides: { '/api/monitor-data': 'monitor-data-extreme' } });
    await gotoTab(page, 'overview');
    await expect(page).toHaveScreenshot('noir-state-long-numbers.png', { fullPage: true });
    expect(unmocked).toEqual([]);
  });
});
