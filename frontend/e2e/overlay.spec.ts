import { expect, test } from '@playwright/test';
import { ok, uid } from './helpers';

test('the operator spotlights a team and the stream overlay switches to it and back', async ({ page, browser, request }) => {
  const entityId = uid('e2e-overlay');
  await ok(await request.post('/api/v1/overlays', { data: { entity_id: entityId, display_name: 'E2E overlay', field_set_id: 1, tags: [] } }));
  try {
    const obs = await browser.newContext({ storageState: { cookies: [], origins: [] } });
    const overlay = await obs.newPage();
    await overlay.goto(`/overlay/${entityId}`);
    await expect(overlay.locator('#grid')).toBeAttached();
    await overlay.waitForTimeout(1500); // let the websocket connect

    await page.goto('/overlays');
    const card = page.locator('div', { hasText: entityId }).filter({ has: page.getByLabel(`Spotlight team for ${entityId}`) }).last();
    await card.getByLabel(`Spotlight team for ${entityId}`).fill('E2E1A');
    await card.getByRole('button', { name: 'Show team' }).click();
    await expect(page.getByText('Spotlighting E2E1A')).toBeVisible();

    await expect(overlay.locator('#grid.mode-finals')).toBeVisible();
    await expect(overlay.locator('#grid')).toContainText('E2E1A');
    await expect(overlay.locator('#grid')).toContainText('Gearsquad E2E');

    await card.getByRole('button', { name: 'Back to match view' }).click();
    await expect(page.getByText('Back to the match view')).toBeVisible();
    await expect(overlay.locator('#grid.mode-finals')).toHaveCount(0);
    await obs.close();
  } finally {
    await request.delete(`/api/v1/overlays/${entityId}`);
  }
});

test('the robot name appears on the overlay only when the setting allows it', async ({ page, browser, request }) => {
  const entityId = uid('e2e-robot');
  await ok(await request.post('/api/v1/overlays', { data: { entity_id: entityId, display_name: 'E2E robot overlay', field_set_id: 1, tags: [] } }));
  await ok(await request.put('/api/v1/teams/E2E1A', { data: { pit_location: 'P1' } }));
  try {
    const feed = async () => ok<{ show_robot_name: boolean }>(await request.get('/api/v1/public/overlay/teams?teams=E2E1A'));
    expect((await feed()).show_robot_name).toBe(false);

    await page.goto('/settings');
    await page.locator('text=Show robot names on the overlay').locator('..').getByRole('switch').click();
    await page.getByRole('button', { name: /^Save/ }).first().click();
    await expect(page.getByText('Settings saved')).toBeVisible();
    expect((await feed()).show_robot_name).toBe(true);
  } finally {
    await request.put('/api/v1/settings', { data: { overlay: { value: { show_robot_name: false } } } });
    await request.delete(`/api/v1/overlays/${entityId}`);
    void browser;
  }
});
