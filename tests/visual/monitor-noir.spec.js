// R1 Terminal Noir 全量视觉矩阵：7 tab × 3 视口 + 断点边界 + 极端状态 + CLS
// 基线按 project 分目录：chromium-noir/，通过 SERENITY_THEME=terminal-noir 注入
const { test, expect } = require('@playwright/test');
const { installClsObserver, readCls, openMonitor, gotoTab, stopTimers, TABS, VIEWPORTS } = require('./helpers');

test.describe('Noir 视觉矩阵 7×3', () => {
  for (const vp of VIEWPORTS) {
    for (const tab of TABS) {
      const isMobileCase = vp.tag === '375' ? ' @mobile' : '';
      test(`${tab}-${vp.tag}${isMobileCase}`, async ({ page }) => {
        await page.setViewportSize({ width: vp.w, height: vp.h });
        const unmocked = await openMonitor(page);
        await gotoTab(page, tab);
        await expect(page).toHaveScreenshot(`noir-${tab}-${vp.tag}.png`, { fullPage: true });
        expect(unmocked, '存在未 mock 的 API 请求').toEqual([]);
      });
    }
  }
});

test.describe('Noir 断点边界 767/768/769', () => {
  for (const w of [767, 768, 769]) {
    test(`overview-${w}`, async ({ page }) => {
      await page.setViewportSize({ width: w, height: 1024 });
      const unmocked = await openMonitor(page);
      await gotoTab(page, 'overview');
      await expect(page).toHaveScreenshot(`noir-boundary-overview-${w}.png`, { fullPage: true });
      expect(unmocked).toEqual([]);
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
    await stopTimers(page);
    await expect(page).toHaveScreenshot('noir-state-error.png', { fullPage: true });
  });

  test('empty-data @mobile', async ({ page }) => {
    const unmocked = await openMonitor(page, { overrides: { '/api/monitor-data': 'monitor-data-empty' } });
    await gotoTab(page, 'overview');
    await expect(page).toHaveScreenshot('noir-state-empty.png', { fullPage: true });
    expect(unmocked).toEqual([]);
  });

  test('stale-data @mobile', async ({ page }) => {
    const unmocked = await openMonitor(page, { overrides: { '/api/monitor-data': 'monitor-data-stale' } });
    await gotoTab(page, 'overview');
    await expect(page).toHaveScreenshot('noir-state-stale.png', { fullPage: true });
    expect(unmocked).toEqual([]);
  });

  test('long-numbers @mobile', async ({ page }) => {
    const unmocked = await openMonitor(page, { overrides: { '/api/monitor-data': 'monitor-data-extreme' } });
    await gotoTab(page, 'overview');
    await expect(page).toHaveScreenshot('noir-state-long-numbers.png', { fullPage: true });
    expect(unmocked).toEqual([]);
  });

  test('modal-config @mobile', async ({ page }) => {
    const unmocked = await openMonitor(page);
    await gotoTab(page, 'overview');
    await page.evaluate(() => showConfig('601006'));
    await page.waitForSelector('#modal-container .modal-box', { timeout: 10_000 });
    await page.waitForTimeout(250);
    await expect(page).toHaveScreenshot('noir-state-modal.png', { fullPage: true });
    expect(unmocked).toEqual([]);
  });
});

test.describe('Noir CLS', () => {
  const fs = require('fs');
  const path = require('path');
  const CLS_BASELINE = path.join(__dirname, 'cls-baseline-noir.json');

  for (const vp of VIEWPORTS) {
    const isMobileCase = vp.tag === '375' ? ' @mobile' : '';
    test(`cls-overview-${vp.tag}${isMobileCase}`, async ({ page }, testInfo) => {
      await page.setViewportSize({ width: vp.w, height: vp.h });
      await installClsObserver(page);
      await openMonitor(page);
      await gotoTab(page, 'overview');
      const cls = await readCls(page);
      await testInfo.attach('cls', { body: String(cls), contentType: 'text/plain' });

      expect(cls, 'CLS 必须 < 0.1').toBeLessThan(0.1);

      const key = `${testInfo.project.name}/overview-${vp.tag}`;
      let baseline = {};
      try { baseline = JSON.parse(fs.readFileSync(CLS_BASELINE, 'utf-8')); } catch (e) {}
      if (process.env.UPDATE_CLS === '1') {
        baseline[key] = cls;
        fs.writeFileSync(CLS_BASELINE, JSON.stringify(baseline, null, 1));
      } else if (baseline[key] !== undefined) {
        expect(cls, `CLS 不得差于基线 ${baseline[key]}`).toBeLessThanOrEqual(baseline[key] + 0.01);
      }
    });
  }
});
