import { expect, test } from '@playwright/test';
import { apiLogin, createTestProfile, simLoad, uiLogin } from './helpers';

let profileName = '';

test.beforeAll(async () => {
  const { api, csrf } = await apiLogin();
  profileName = `E2E ohne OCR ${Date.now()}`;
  await createTestProfile(api, await csrf(), profileName);
  await api.dispose();
});

test('login → scan → live thumbnails → finish → done → download', async ({ page }) => {
  await simLoad('letter_de'); // 3 sheets

  await uiLogin(page);
  // (The pre-login session probe legitimately returns 401; collect errors only from here on.)
  const consoleErrors: string[] = [];
  page.on('console', (m) => m.type() === 'error' && consoleErrors.push(m.text()));
  await expect(page.getByTestId('scanner-status')).toHaveAttribute('data-state', /ready|busy/);

  await page.getByTestId('profile-select').selectOption({ label: profileName });
  await page.getByLabel(/^Titel/).fill('E2E Brief');
  await page.getByTestId('start-scan').click();

  const panel = page.getByTestId('active-document').first();
  await expect(panel).toBeVisible();
  // Pages appear live while scanning.
  await expect(panel.getByTestId('live-thumbs').locator('img')).toHaveCount(3, { timeout: 60_000 });
  await expect(panel).toHaveAttribute('data-status', 'stored', { timeout: 60_000 });

  await panel.getByTestId('finish').click();
  await expect(panel).toHaveAttribute('data-status', 'done', { timeout: 90_000 });

  const downloadPromise = page.waitForEvent('download');
  await panel.getByTestId('download').click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toMatch(/E2E Brief\.pdf$/);
  const path = await download.path();
  expect(path).toBeTruthy();

  // The document page shows the finished document with its delivery.
  await panel.getByRole('link', { name: /Dokument öffnen|Open document/ }).click();
  await expect(page.getByTestId('document-page')).toHaveAttribute('data-status', 'done');
  await expect(page.getByTestId('deliveries')).toContainText(/Zugestellt|Delivered/);
  expect(consoleErrors.filter((e) => !/favicon|sw\.js/.test(e))).toEqual([]);
});

test('empty feeder → visible error → load paper → scan remaining pages', async ({ page }) => {
  await simLoad(null);
  await uiLogin(page);
  await page.getByTestId('profile-select').selectOption({ label: profileName });
  await page.getByTestId('start-scan').click();

  const panel = page.getByTestId('active-document').first();
  const alert = panel.getByTestId('document-alert');
  await expect(alert).toBeVisible({ timeout: 60_000 });
  await expect(alert).toHaveAttribute('data-code', 'feeder_empty');
  await expect(alert).toContainText(/Kein Papier|Feeder empty/);

  await simLoad('letter_en');
  await alert.getByTestId('recovery-rescan').click();
  await expect(panel.getByTestId('live-thumbs').locator('img')).toHaveCount(3, { timeout: 60_000 });
  await expect(panel).toHaveAttribute('data-status', 'stored', { timeout: 60_000 });

  // Clean up: discard via the in-app confirmation dialog (no window.confirm).
  await panel.getByRole('button', { name: /Verwerfen|Discard/ }).click();
  await page.getByTestId('confirm-ok').click();
});
