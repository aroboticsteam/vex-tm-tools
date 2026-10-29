import { expect, test } from '@playwright/test';
import { ADMIN_PASSWORD } from './helpers';

// These run signed out, whatever the project's default session is.
test.use({ storageState: { cookies: [], origins: [] } });

test('a wrong password is rejected and no session is created', async ({ page, context }) => {
  await page.goto('/admin_login');
  await page.getByLabel('Password').fill('definitely-not-it');
  await page.getByRole('button', { name: 'Log in' }).click();
  await expect(page.locator('body')).toContainText('Invalid credentials');
  expect((await context.cookies()).filter((c) => /session/i.test(c.name))).toHaveLength(0);
  await page.goto('/');
  await expect(page.getByRole('link', { name: 'Button Pages' })).toHaveCount(0);
});

test('a signed-out visitor is not shown the console', async ({ page }) => {
  await page.goto('/buttons');
  await expect(page.getByRole('link', { name: 'Button Pages' })).toHaveCount(0);
});

test('signing in with the right password opens the console', async ({ page }) => {
  await page.goto('/admin_login');
  await page.getByLabel('Password').fill(ADMIN_PASSWORD);
  await page.getByRole('button', { name: 'Log in' }).click();
  await expect(page.locator('body')).toContainText('"ok":true');
  await page.goto('/');
  await expect(page.getByRole('link', { name: 'Comms' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Dashboard' })).toBeVisible();
});
