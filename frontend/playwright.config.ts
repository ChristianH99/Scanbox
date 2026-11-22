import { defineConfig, devices } from '@playwright/test';

/**
 * E2E tests run against a running Scanbox stack (backend serving frontend/dist + eSCL simulator).
 *   E2E_BASE_URL  (default http://127.0.0.1:8080)
 *   E2E_USER / E2E_PASSWORD (default admin / adminadmin)
 *   E2E_SIM_URL   (default http://127.0.0.1:8090)
 */
export default defineConfig({
  testDir: './e2e',
  timeout: 120_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : 'list',
  use: {
    baseURL: process.env.E2E_BASE_URL ?? 'http://127.0.0.1:8080',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    locale: 'de-DE',
    serviceWorkers: 'block',
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
  ],
});
