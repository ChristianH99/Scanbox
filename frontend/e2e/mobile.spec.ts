import { expect, test } from '@playwright/test';
import { apiLogin, uiLogin } from './helpers';

test.use({ viewport: { width: 360, height: 740 } });

const ROUTES = ['/', '/inbox', '/history', '/profiles', '/profiles/new', '/settings', '/admin/scanner', '/admin/storage', '/admin/diagnostics'];

test('every screen works at 360 px without horizontal scrolling', async ({ page }) => {
  await uiLogin(page);
  const errors: string[] = [];
  page.on('console', (m) => m.type() === 'error' && errors.push(`${page.url()}: ${m.text()}`));
  page.on('pageerror', (e) => errors.push(`${page.url()}: ${e.message}`));

  // Include the newest document (if any) to cover the editor.
  const { api } = await apiLogin();
  const docs = await (await api.get('/api/documents', { params: { scope: 'history', limit: '1' } })).json();
  await api.dispose();
  const routes = docs.items?.length ? [...ROUTES, `/documents/${docs.items[0].id}`] : ROUTES;

  for (const r of routes) {
    await page.goto(r);
    await page.waitForLoadState('networkidle');
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow, `horizontal overflow on ${r}`).toBeLessThanOrEqual(1);
    await page.screenshot({ path: `test-results/mobile${r.replace(/\//g, '_') || '_root'}.png`, fullPage: true });
  }
  expect(errors).toEqual([]);
});
