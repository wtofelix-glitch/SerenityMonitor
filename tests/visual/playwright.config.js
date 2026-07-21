// Serenity 视觉回归配置 — R0 基线
// 版本纪律：@playwright/test 精确锁定于 package-lock.json（浏览器二进制随包版本绑定）
const { defineConfig, devices } = require('@playwright/test');

// 三星 S24 Ultra 手动画像（不依赖 Playwright devices 注册表的版本差异）
const S24_ULTRA = {
  viewport: { width: 384, height: 832 },
  deviceScaleFactor: 3.5,
  isMobile: true,
  hasTouch: true,
  userAgent:
    'Mozilla/5.0 (Linux; Android 14; SM-S928B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36',
};

module.exports = defineConfig({
  testDir: '.',
  timeout: 60_000,
  expect: {
    // 逐像素基线：不同引擎/平台分目录，互不可比
    toHaveScreenshot: { maxDiffPixels: 0, animations: 'disabled', caret: 'hide' },
  },
  fullyParallel: false,
  workers: 1, // 共享同一 Flask 实例，串行避免互相干扰
  retries: 0, // 确定性测试不允许 retry 掩盖 flake
  reporter: [['list'], ['html', { open: 'never' }]],
  snapshotPathTemplate: '{testDir}/__screenshots__/{projectName}/{testFilePath}/{arg}{ext}',
  use: {
    baseURL: 'http://localhost:8401',
    serviceWorkers: 'block', // 视觉用例默认隔离 SW；SW 行为在 chromium-sw project 单独测
    locale: 'zh-CN',
    timezoneId: 'Asia/Shanghai',
    colorScheme: 'dark',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
      testMatch: 'monitor.spec.js',
    },
    {
      name: 'webkit',
      use: { ...devices['Desktop Safari'] },
      testMatch: 'monitor.spec.js',
    },
    {
      name: 'chromium-s24',
      use: { ...devices['Desktop Chrome'], ...S24_ULTRA },
      testMatch: 'monitor.spec.js',
      grep: /@mobile/, // S24 只跑移动端标记用例，桌面矩阵无意义
    },
    {
      name: 'chromium-sw',
      use: { ...devices['Desktop Chrome'], serviceWorkers: 'allow' },
      testMatch: 'sw.spec.js',
    },
    {
      name: 'chromium-soak',
      use: { ...devices['Desktop Chrome'] },
      testMatch: 'soak.spec.js',
      timeout: 15 * 60_000, // 2880 次刷新等效模拟
    },
    {
      name: 'chromium-noir',
      use: { ...devices['Desktop Chrome'] },
      testMatch: 'monitor-noir.spec.js',
    },
  ],
});
