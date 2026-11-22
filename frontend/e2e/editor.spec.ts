import { expect, test } from '@playwright/test';
import { apiLogin, createTestProfile, simLoad, uiLogin } from './helpers';

test('editor: drag-reorder, rotate + save in page editor, QR detection and manual value', async ({ page }) => {
  const { api, csrf } = await apiLogin();
  const headers = await csrf();
  const profileId = await createTestProfile(api, headers, `E2E Editor ${Date.now()}`);
  await simLoad('invoice_qr'); // QR "INV-2024-0042" top right on page 1, then 2 text pages
  const r = await api.post('/api/scans', { data: { profile_id: profileId, title: 'E2E Editor' }, headers });
  expect(r.status(), await r.text()).toBe(201);
  const doc = await r.json();
  await expect
    .poll(async () => (await (await api.get(`/api/documents/${doc.id}`)).json()).status, { timeout: 60_000 })
    .toBe('stored');

  await uiLogin(page);
  await page.goto(`/documents/${doc.id}`);
  const grid = page.getByTestId('page-grid');
  await expect(grid.locator('li')).toHaveCount(3);
  const before = await grid.locator('li').evaluateAll((els) => els.map((e) => (e as HTMLElement).dataset.pageId));

  // Drag page 1's handle behind page 3 (pointer events → works for mouse and touch).
  const handle = grid.locator('li').nth(0).locator('[data-handle]');
  const target = grid.locator('li').nth(2);
  const hb = (await handle.boundingBox())!;
  const tb = (await target.boundingBox())!;
  await page.mouse.move(hb.x + hb.width / 2, hb.y + hb.height / 2);
  await page.mouse.down();
  await page.mouse.move(tb.x + tb.width * 0.8, tb.y + tb.height / 2, { steps: 12 });
  await page.mouse.up();
  await expect
    .poll(async () => grid.locator('li').evaluateAll((els) => els.map((e) => (e as HTMLElement).dataset.pageId)))
    .toEqual([before[1], before[2], before[0]]);
  // Persisted on the server
  await expect
    .poll(async () => ((await (await api.get(`/api/documents/${doc.id}`)).json()).pages as { id: string; position: number }[])
      .sort((a, b) => a.position - b.position).map((p) => p.id))
    .toEqual([before[1], before[2], before[0]]);

  // Keyboard alternative: move it back to the front.
  const h3 = grid.locator(`[data-handle="${before[0]}"]`);
  await h3.focus();
  await page.keyboard.press('ArrowLeft');
  await page.keyboard.press('ArrowLeft');
  await expect
    .poll(async () => grid.locator('li').evaluateAll((els) => els.map((e) => (e as HTMLElement).dataset.pageId)))
    .toEqual(before);

  // Page editor: rotate right, preview renders, save → PATCH persists edits.rotate.
  await grid.locator('li').nth(1).getByRole('button', { name: /bearbeiten|Edit page/i }).click();
  await expect(page.getByTestId('editor-preview')).toBeVisible({ timeout: 30_000 });
  await page.getByRole('button', { name: /90°/ }).nth(1).click();
  await page.screenshot({ path: 'test-results/editor-desktop.png' });
  await page.getByTestId('editor-save').click();
  await expect
    .poll(async () => ((await (await api.get(`/api/documents/${doc.id}`)).json()).pages as { id: string; edits: { rotate?: number } }[])
      .find((p) => p.id === before[1])?.edits.rotate)
    .toBe(90);
  await page.getByRole('button', { name: /Schließen|Close/ }).last().click();

  // QR: detection shows the hit; manual value is saved.
  const qr = page.getByTestId('qr-panel');
  await qr.getByTestId('qr-detect').click();
  await expect(qr.locator('code', { hasText: 'INV-2024-0042' })).toBeVisible({ timeout: 30_000 });
  await qr.getByRole('textbox', { name: /^(QR-Wert|QR value)$/ }).fill('MANUAL-7');
  await qr.getByRole('button', { name: /^(Speichern|Save)$/ }).click();
  await expect(qr.getByTestId('qr-value')).toHaveText('MANUAL-7');

  await api.post(`/api/documents/${doc.id}/discard`, { headers });
  await api.dispose();
});
