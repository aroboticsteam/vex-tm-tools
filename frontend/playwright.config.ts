import { defineConfig, devices } from '@playwright/test';

/**
 * End-to-end tests run against a real backend that serves the built SPA
 * (see docs/TESTING.md). `E2E_BASE_URL` points at it; locally you can also
 * set `E2E_CHANNEL=chrome` to drive an installed Chrome instead of the
 * Playwright-managed Chromium.
 */
const baseURL = process.env.E2E_BASE_URL ?? 'http://localhost:8765';

export default defineConfig({
  testDir: './e2e',
  // One shared database and event bus: keep tests serial and independent by cleaning up what they create.
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  timeout: 45_000,
  expect: { timeout: 10_000 },
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : [['list']],
  use: {
    baseURL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    channel: process.env.E2E_CHANNEL || undefined,
  },
  projects: [
    { name: 'setup', testMatch: /auth\.setup\.ts/ },
    {
      name: 'chromium',
      dependencies: ['setup'],
      testIgnore: /auth\.setup\.ts/,
      use: {
        ...devices['Desktop Chrome'],
        storageState: 'e2e/.auth/admin.json',
        permissions: ['camera', 'microphone'],
        launchOptions: {
          // A synthetic camera so the recorder works without hardware.
          args: ['--use-fake-device-for-media-stream=fps=60', '--use-fake-ui-for-media-stream'],
        },
      },
    },
  ],
});
