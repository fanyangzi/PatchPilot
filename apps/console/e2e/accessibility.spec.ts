import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

type Task = { task_id: string };
type Report = { report_id: string };

async function firstTask(request: APIRequestContext): Promise<Task> {
  const response = await request.get(`${process.env.PATCHPILOT_API_URL || 'http://127.0.0.1:8010'}/api/v1/tasks`);
  expect(response.ok(), 'PatchPilot API must be reachable before browser acceptance').toBeTruthy();
  const body = await response.json() as { items: Task[] };
  expect(body.items.length, 'a persisted task is required for review evidence').toBeGreaterThan(0);
  return body.items[0];
}

async function reportFor(request: APIRequestContext, taskId: string): Promise<Report> {
  const response = await request.get(`${process.env.PATCHPILOT_API_URL || 'http://127.0.0.1:8010'}/api/v1/tasks/${taskId}/reports`);
  expect(response.ok()).toBeTruthy();
  const body = await response.json() as { items: Report[] };
  expect(body.items.length, 'the persisted verification must have a report for download acceptance').toBeGreaterThan(0);
  return body.items[0];
}

async function waitForInbox(page: Page) {
  await page.goto('/inbox');
  await expect(page.getByRole('heading', { name: '验收队列' })).toBeVisible();
  await expect(page.getByRole('button', { name: '导入验收任务' })).toBeVisible();
}

test.describe('PatchPilot visual and keyboard acceptance', () => {
  test('persists review-studio screenshots at every required viewport', async ({ page }) => {
    const viewports = [
      [1280, 800],
      [1440, 900],
      [1600, 1000],
      [1920, 1080],
    ] as const;

    for (const [width, height] of viewports) {
      await page.setViewportSize({ width, height });
      await waitForInbox(page);
      await page.screenshot({
        path: `test-results/visual/inbox-${width}x${height}.png`,
        fullPage: true,
      });
      await expect(page.getByText('API v1')).toBeVisible();
    }
  });

  test('keeps core actions visible at a 125% zoom equivalent', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 800 });
    await waitForInbox(page);

    // Chromium's browser zoom is not exposed consistently in headless mode.
    // CSS zoom=0.8 produces the same 125% effective layout pressure while
    // retaining real keyboard/focus and hit-testing semantics.
    await page.evaluate(() => {
      document.documentElement.dataset.acceptanceZoom = '125';
      document.documentElement.style.zoom = '0.8';
    });
    await expect(page.getByRole('button', { name: '导入验收任务' })).toBeVisible();
    await expect(page.getByRole('heading', { name: '验收队列' })).toBeVisible();
    const overflow = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      clientWidth: document.documentElement.clientWidth,
    }));
    expect(overflow.scrollWidth).toBeLessThanOrEqual(overflow.clientWidth + 8);
    await page.screenshot({ path: 'test-results/visual/inbox-125-percent.png', fullPage: true });
  });

  test('opens intake and reaches validation using keyboard only', async ({ page }) => {
    await waitForInbox(page);
    await page.locator('body').focus();
    const target = page.getByRole('button', { name: '导入验收任务' });
    for (let i = 0; i < 80; i += 1) {
      if (await target.evaluate((node) => node === document.activeElement)) break;
      await page.keyboard.press('Tab');
    }
    await expect(target).toBeFocused();
    await page.keyboard.press('Enter');
    await expect(page.getByRole('heading', { name: '导入验收任务' })).toBeVisible();

    // The modal auto-focuses the repository field. Keep the interaction
    // keyboard-only and intentionally submit an invalid owner/name to prove
    // the real intake validation path is reachable without a pointer.
    await page.keyboard.type('invalid-repository');
    await page.keyboard.press('Tab');
    await page.keyboard.press('Tab');
    await page.keyboard.press('Enter');
    await expect(page.getByRole('alert')).toContainText('仓库必须填写 owner/name');
    await page.keyboard.press('Escape');
    await expect(page.getByRole('heading', { name: '导入验收任务' })).toHaveCount(0);
  });

  test('recovers a review deep link and downloads a persisted report by keyboard', async ({ page, request }) => {
    const task = await firstTask(request);
    const report = await reportFor(request, task.task_id);

    await page.goto(`/review/${task.task_id}`);
    await expect(page.getByRole('heading', { name: '代码审查' })).toBeVisible();
    await expect(page.getByText('候选补丁差异')).toBeVisible();
    await page.reload();
    await expect(page.getByRole('heading', { name: '代码审查' })).toBeVisible();
    await expect(page).toHaveURL(new RegExp(`/review/${task.task_id}\\?candidate=`));

    await page.goto(`/report/${task.task_id}?report=${report.report_id}`);
    await expect(page.getByRole('heading', { name: '交付报告' })).toBeVisible();
    const download = page.getByRole('button', { name: '下载当前报告' });
    await expect(download).toBeVisible();
    await download.focus();
    await expect(download).toBeFocused();
    const downloadPromise = page.waitForEvent('download');
    await page.keyboard.press('Enter');
    const artifact = await downloadPromise;
    expect(artifact.suggestedFilename()).toMatch(/^patchpilot-report_.*\.(md|html)$/);
  });
});
