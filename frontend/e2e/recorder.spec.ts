import { expect, test } from '@playwright/test';

test('record a clip with a synthetic camera, review it, and upload it', async ({ page }) => {
  // The processing/S3 side has its own backend tests; here the upload is intercepted
  // so the test covers what the browser does: camera -> countdown -> recording -> review -> upload.
  const uploads: { url: string; contentType: string; size: number }[] = [];
  await page.route('**/api/v1/teams/E2E1A/clips/bot', async (route) => {
    const request = route.request();
    uploads.push({ url: request.url(), contentType: request.headers()['content-type'] ?? '', size: request.postDataBuffer()?.length ?? 0 });
    await route.fulfill({ status: 202, contentType: 'application/json', body: JSON.stringify({ team_number: 'E2E1A', kind: 'bot', status: 'PROCESSING' }) });
  });

  await page.goto('/record');
  await expect(page.getByText(/Camera reports/)).toBeVisible({ timeout: 15_000 });
  await expect(page.getByRole('button', { name: 'Record' })).toBeDisabled(); // no team yet

  await page.getByPlaceholder('21549A').fill('e2e1a');
  await page.locator('input[type=range]').fill('5');
  await expect(page.getByRole('button', { name: 'Record' })).toBeEnabled();
  await page.getByRole('button', { name: 'Record' }).click();

  await expect(page.getByText('3', { exact: true })).toBeVisible(); // 3-2-1 countdown
  await expect(page.getByText(/\/ 5s/)).toBeVisible({ timeout: 8000 }); // recording
  const review = page.locator('video[controls]');
  await expect(review).toBeVisible({ timeout: 10_000 });
  await expect.poll(() => review.evaluate((v: HTMLVideoElement) => v.src.startsWith('blob:'))).toBe(true);

  await page.getByRole('button', { name: 'Upload clip' }).click();
  await expect(page.getByText(/E2E1A bot clip uploaded/)).toBeVisible();
  expect(uploads).toHaveLength(1);
  expect(uploads[0].contentType).toContain('multipart/form-data');
  expect(uploads[0].size).toBeGreaterThan(1000); // a real recorded blob, not an empty file
  await expect(page.getByRole('button', { name: 'Record' })).toBeVisible(); // back to a fresh take
});

test('retake discards the recording and returns to the camera', async ({ page }) => {
  await page.goto('/record');
  await expect(page.getByText(/Camera reports/)).toBeVisible({ timeout: 15_000 });
  await page.getByPlaceholder('21549A').fill('E2E1A');
  await page.locator('input[type=range]').fill('5');
  await page.getByRole('button', { name: 'Record' }).click();
  await expect(page.locator('video[controls]')).toBeVisible({ timeout: 15_000 });
  await page.getByRole('button', { name: 'Retake' }).click();
  await expect(page.getByRole('button', { name: 'Record' })).toBeVisible();
  await expect(page.locator('video[controls]')).toHaveCount(0);
});
