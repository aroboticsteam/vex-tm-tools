import { expect, test } from '@playwright/test';
import { ok, uid } from './helpers';

// 1x1 transparent PNG
const PNG = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==', 'base64');

test.describe('public site and its admin tools', () => {
  test.afterEach(async ({ request }) => {
    await request.put('/api/v1/settings', { data: { site: { value: {} } } });
    const recordings = await ok<{ match_key: string }[]>(await request.get('/api/v1/match-recordings'));
    for (const r of recordings) await request.delete(`/api/v1/match-recordings/${encodeURIComponent(r.match_key)}`);
    await request.put('/api/v1/teams/E2E1A', { data: { website_url: '' } });
    await request.delete('/api/v1/teams/E2E1A/logo');
  });

  test('visitors browse the event, a team page and the live leaderboards', async ({ browser }) => {
    const context = await browser.newContext({ storageState: { cookies: [], origins: [] } });
    const page = await context.newPage();
    await page.goto('/site#event');
    await page.getByRole('button', { name: 'Matches' }).click();
    await expect(page.getByRole('row', { name: /Q1.*E2E1A/ }).first()).toBeVisible();
    await page.getByRole('button', { name: 'Qualification rankings' }).click();
    await expect(page.getByRole('row', { name: /E2E1A/ }).first()).toBeVisible();

    await page.goto('/site#team/E2E1A');
    await expect(page.getByRole('heading', { name: /E2E1A — Gearsquad E2E/ })).toBeVisible();
    await expect(page.getByText('Excellence').or(page.getByText('Awards')).first()).toBeVisible();

    await page.goto('/site/leaderboard?panel=2&programme=8');
    await expect(page.locator('#prog')).toHaveText(/VIQRC|V5RC/);
    const seen = new Set<string>();
    for (let i = 0; i < 12 && seen.size < 3; i++) {
      seen.add((await page.locator('#tabs .on').textContent()) ?? '');
      await page.waitForTimeout(1000);
    }
    expect(seen.size).toBeGreaterThanOrEqual(3); // it rotates through panels on its own
    await context.close();
  });

  test('the site title, sponsors and next competition are edited in a form and appear publicly', async ({ page, request }) => {
    const title = uid('E2E Title');
    await page.goto('/settings');
    await page.getByLabel('Site title').fill(title);
    await page.getByLabel('Next competition name').fill('E2E Nationals');
    await page.getByLabel('Next competition date').fill('2999-01-02');
    await page.getByLabel('Teams attending').fill('E2E1A, E2E2B');
    await page.getByRole('button', { name: /add sponsor/i }).click();
    await page.getByLabel('Sponsor 1 name').fill('Kingston');
    await page.getByLabel('Sponsor 1 link').fill('https://kingston.example');
    await page.getByRole('button', { name: /add bus/i }).click();
    await page.getByLabel('Bus 1 name').fill('Bus 1');
    await page.getByLabel('Bus 1 status').fill('At school');
    await page.getByRole('button', { name: /^Save/ }).first().click();
    await expect(page.getByText('Settings saved')).toBeVisible();

    const overview = await ok<{ title: string; next_competition: { name: string; teams: string[] }; sponsors: { name: string }[]; bus_tracker: { buses: { name: string }[] } }>(
      await request.get('/api/v1/public/site/overview'),
    );
    expect(overview.title).toBe(title);
    expect(overview.next_competition).toMatchObject({ name: 'E2E Nationals', teams: ['E2E1A', 'E2E2B'] });
    expect(overview.sponsors.map((s) => s.name)).toEqual(['Kingston']);
    expect(overview.bus_tracker.buses[0].name).toBe('Bus 1');

    await page.reload();
    await expect(page.getByLabel('Site title')).toHaveValue(title);
    await page.goto('/site#bus');
    await expect(page.getByText('At school')).toBeVisible();
  });

  test('an unsafe link is refused before saving', async ({ page }) => {
    await page.goto('/settings');
    await page.getByLabel('Event page link').fill('javascript:alert(1)');
    await expect(page.getByTestId('site-problems')).toContainText('Next competition link');
    await page.getByRole('button', { name: /^Save/ }).first().click();
    await expect(page.getByText(/Public site settings: Next competition link/)).toBeVisible();
  });

  test('a map image is uploaded, pins are placed by clicking, and visitors see them', async ({ page, request }) => {
    await page.goto('/settings');
    await page.getByLabel('Upload map image').setInputFiles({ name: 'map.png', mimeType: 'image/png', buffer: PNG });
    await expect(page.getByTestId('map-canvas')).toBeVisible();
    await page.getByRole('button', { name: /add room pin/i }).click();
    await page.getByLabel('Room 1 name').fill('Main Hall');
    const canvas = page.getByTestId('map-canvas');
    await canvas.locator('img').click({ position: { x: 0, y: 0 }, force: true });
    await page.getByRole('button', { name: /^Save/ }).first().click();
    await expect(page.getByText('Settings saved')).toBeVisible();

    const overview = await ok<{ map: { image_url: string; rooms: { name: string }[] } }>(await request.get('/api/v1/public/site/overview'));
    expect(overview.map.image_url).toMatch(/^https?:\/\//);
    expect(overview.map.rooms.map((r) => r.name)).toEqual(['Main Hall']);
  });

  test('a match recording link and a team logo/website show up on the public site', async ({ page, browser, request }) => {
    await page.goto('/settings');
    await page.getByLabel('Recording division').fill('1');
    await page.getByLabel('Recording match').fill('Q1');
    await page.getByLabel('Recording link').fill('https://videos.example.com/q1');
    await page.getByRole('button', { name: 'Link', exact: true }).click();
    await expect(page.getByTestId('recording-row')).toContainText('Q1');

    await page.goto('/teams/E2E1A');
    await page.getByLabel('Team website').fill('https://gearsquad.example.com');
    await page.getByRole('button', { name: 'Save' }).click();
    await page.getByLabel('Upload team logo').setInputFiles({ name: 'logo.png', mimeType: 'image/png', buffer: PNG });
    await expect(page.getByText('Logo uploaded').first()).toBeVisible();

    const visitor = await browser.newContext({ storageState: { cookies: [], origins: [] } });
    const pub = await visitor.newPage();
    await pub.goto('/site#event');
    await pub.getByRole('button', { name: 'Matches' }).click();
    await expect(pub.getByRole('link', { name: '▶ Watch' })).toHaveAttribute('href', 'https://videos.example.com/q1');
    await pub.goto('/site#team/E2E1A');
    await expect(pub.getByRole('link', { name: 'gearsquad.example.com' })).toBeVisible();
    await expect(pub.locator('h2 img')).toHaveCount(1);
    await visitor.close();
    void request;
  });
});
