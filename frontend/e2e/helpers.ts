import { expect, type APIRequestContext, type Page } from '@playwright/test';

export const ADMIN_PASSWORD = process.env.E2E_ADMIN_PASSWORD ?? 'ci-admin-password';

/** Unique-ish names so reruns never collide with leftovers. */
export function uid(prefix: string): string {
  return `${prefix}-${Math.random().toString(36).slice(2, 8)}`;
}

/** Fails loudly with the server's answer instead of a bare status code. */
export async function ok<T = unknown>(res: { ok(): boolean; status(): number; text(): Promise<string>; json(): Promise<unknown> }): Promise<T> {
  if (!res.ok()) throw new Error(`API call failed (${res.status()}): ${await res.text()}`);
  return (res.status() === 204 ? undefined : await res.json().catch(() => undefined)) as T;
}

/** Toasts appear bottom-right; assert on text only. */
export async function expectToast(page: Page, text: string | RegExp): Promise<void> {
  await expect(page.getByText(text).first()).toBeVisible();
}

export async function listPages(api: APIRequestContext): Promise<{ id: string; name: string; buttons: { id: string; label: string }[] }[]> {
  return ok(await api.get('/api/v1/button-pages'));
}
