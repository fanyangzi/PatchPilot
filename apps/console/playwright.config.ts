import { defineConfig, devices } from '@playwright/test';

/**
 * Browser acceptance is intentionally pointed at a running PatchPilot API and
 * console. This keeps evidence grounded in the same persisted task,
 * candidate, verification and report records that a maintainer sees.
 *
 * Start the product with `./start.sh`, then run `npm run test:e2e` from this
 * directory. A different console can be selected with PATCHPILOT_UI_BASE_URL.
 */
export default defineConfig({
  testDir: './e2e',
  timeout: 30_000,
  expect: { timeout: 8_000 },
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: [['list'], ['html', { outputFolder: 'playwright-report', open: 'never' }]],
  use: {
    baseURL: process.env.PATCHPILOT_UI_BASE_URL || 'http://127.0.0.1:5174',
    ...devices['Desktop Chrome'],
    locale: 'zh-CN',
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
  },
  outputDir: 'test-results',
  webServer: {
    command: 'VITE_DEV_API_TARGET=http://127.0.0.1:8010 npm run dev -- --host 127.0.0.1 --port 5174',
    url: 'http://127.0.0.1:5174',
    reuseExistingServer: true,
    timeout: 120_000,
  },
});
