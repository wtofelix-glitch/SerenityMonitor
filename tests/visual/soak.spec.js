// 刷新压力测试 — 2880 次 5s 刷新等效（4h 盘中）
// clock.install + runFor 驱动真实 scheduleRefresh 循环（setFixedTime 不驱动定时器）
// 指标：渲染耗时（首/末 100 均值 + p95）、JS heap 增长（仅 Chromium）、Chart 数组长度恒定
const { test, expect } = require('@playwright/test');
const { FROZEN_TIME, mockAllApis } = require('./helpers');

const CYCLES = 2880; // 5s × 4h 等效

test('soak: 2880 次刷新无性能劣化', async ({ page }, testInfo) => {
  const unmocked = await mockAllApis(page);
  await page.clock.install({ time: FROZEN_TIME });
  await page.goto('/monitor');

  // 等首屏渲染
  await page.waitForFunction(() => {
    const t = document.getElementById('header-time');
    return t && t.textContent && !t.textContent.includes('刷新中') && t.textContent !== '--';
  }, { timeout: 30_000 });

  // 包裹 renderAll 采集耗时（renderAll 为全局函数声明）
  await page.evaluate(() => {
    window.__renderTimes = [];
    const orig = window.renderAll;
    window.renderAll = function () {
      const t0 = performance.now();
      const r = orig.apply(this, arguments);
      window.__renderTimes.push(performance.now() - t0);
      return r;
    };
  });

  const samples = []; // { cycle, heap, chartLen }
  for (let i = 0; i < CYCLES; i++) {
    await page.clock.runFor(5000);
    if (i % 96 === 0) {
      // 每 8 分钟等效采样一次
      const s = await page.evaluate(() => ({
        heap: performance.memory ? performance.memory.usedJSHeapSize : null,
        chartLen: (window.STATE && STATE.chartInstance && STATE.chartInstance.data.datasets[0])
          ? STATE.chartInstance.data.datasets[0].data.length : null,
        renders: window.__renderTimes.length,
      }));
      samples.push({ cycle: i, ...s });
    }
  }

  const times = await page.evaluate(() => window.__renderTimes);
  const renders = times.length;

  // 时钟穿越 10:30→盘中→午间→13:00 盘中的会话变速，刷新次数在合理带宽内
  expect(renders, `实际刷新次数 ${renders}`).toBeGreaterThan(800);

  const avg = (a) => a.reduce((x, y) => x + y, 0) / a.length;
  const first100 = avg(times.slice(0, 100));
  const last100 = avg(times.slice(-100));
  const sorted = [...times].sort((a, b) => a - b);
  const p95 = sorted[Math.floor(sorted.length * 0.95)];

  const heaps = samples.map((s) => s.heap).filter((h) => h != null);
  const q = Math.floor(heaps.length / 4);
  const heapGrowth = q > 0 ? (avg(heaps.slice(-q)) - avg(heaps.slice(0, q))) / avg(heaps.slice(0, q)) : 0;

  const chartLens = [...new Set(samples.map((s) => s.chartLen).filter((l) => l != null))];

  const report = {
    renders,
    first100_avg_ms: +first100.toFixed(2),
    last100_avg_ms: +last100.toFixed(2),
    growth_pct: +(((last100 - first100) / first100) * 100).toFixed(1),
    p95_ms: +p95.toFixed(2),
    heap_growth_pct: +(heapGrowth * 100).toFixed(1),
    chart_lengths: chartLens,
  };
  await testInfo.attach('soak-report', { body: JSON.stringify(report, null, 2), contentType: 'application/json' });
  console.log('SOAK REPORT', JSON.stringify(report));

  // 验收门槛
  expect(last100, '末段渲染耗时增幅须 <20%').toBeLessThan(first100 * 1.2 + 1); // +1ms 绝对余量防微秒级噪声
  expect(chartLens.length, 'Chart 数据数组长度必须恒定').toBeLessThanOrEqual(1);
  if (heaps.length >= 8) {
    expect(heapGrowth, 'JS heap 末/首四分位增幅须 <30%').toBeLessThan(0.3);
  }
  expect(unmocked, '存在未 mock 的 API 请求').toEqual([]);
});
