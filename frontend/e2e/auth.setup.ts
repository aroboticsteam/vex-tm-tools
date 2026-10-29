import { expect, test as setup } from '@playwright/test';
import { ADMIN_PASSWORD } from './helpers';

/** Logs in once through the real emergency-admin form and saves the session.
 * (Served by the backend, `/admin_login` is a plain HTML form that answers
 * with JSON; the session cookie it sets is what the SPA then uses.) */
setup('sign in as admin_local', async ({ page }) => {
  await page.goto('/admin_login');
  await page.getByLabel('Password').fill(ADMIN_PASSWORD);
  await page.getByRole('button', { name: 'Log in' }).click();
  await expect(page.locator('body')).toContainText('"ok":true');
  await page.goto('/');
  await expect(page.getByRole('link', { name: 'Button Pages' })).toBeVisible();
  await page.context().storageState({ path: 'e2e/.auth/admin.json' });
});
