/**
 * UI functions end to end (what a user clicks), complementing the API-level system test:
 * profiles, document details + split/merge/discard, history search, settings (language, contacts,
 * API tokens), admin (scanner test, storage, diagnostics), logout.
 */
import { expect, request, test, type Page } from '@playwright/test';
import { apiLogin, BASE, createTestProfile, simLoad, uiLogin } from './helpers';

async function storedDoc(stack = 'letter_de', title = 'E2E Funktion'): Promise<number> {
  const { api, csrf } = await apiLogin();
  const headers = await csrf();
  const profileId = await createTestProfile(api, headers, `E2E F ${Date.now()}`);
  await simLoad(stack);
  const r = await api.post('/api/scans', { data: { profile_id: profileId, title }, headers });
  expect(r.status(), await r.text()).toBe(201);
  const id = (await r.json()).id;
  await expect.poll(async () => (await (await api.get(`/api/documents/${id}`)).json()).status, { timeout: 60_000 }).toBe('stored');
  return id;
}

async function confirm(page: Page): Promise<void> {
  await page.getByTestId('confirm-ok').click();
}

test('profiles: create in the UI, change options, edit, delete', async ({ page }) => {
  await uiLogin(page);
  await page.goto('/profiles');
  await page.getByRole('link', { name: /Neues Profil|New profile/ }).first().click();
  const name = `UI Profil ${Date.now()}`;
  await page.locator('#pname').fill(name);
  await page.locator('details').evaluateAll((ds) => ds.forEach((d) => ((d as HTMLDetailsElement).open = true)));
  await page.locator('#pclean').selectOption('off');
  await page.getByTestId('profile-save').click();
  await expect(page).toHaveURL(/\/profiles(\/\d+)?$/);
  const { api } = await apiLogin();
  const created = (await (await api.get('/api/profiles')).json()).find((p: { name: string }) => p.name === name);
  expect(created, 'profile saved').toBeTruthy();
  expect(created.settings.processing.clean_background).toBe('off');
  // edit
  await page.goto(`/profiles/${created.id}`);
  await page.locator('#pname').fill(`${name} geändert`);
  await page.getByTestId('profile-save').click();
  await expect.poll(async () => (await (await api.get(`/api/profiles/${created.id}`)).json()).name).toBe(`${name} geändert`);
  // delete from the list, with confirmation
  await page.goto('/profiles');
  await page.locator('li', { hasText: `${name} geändert` }).getByRole('button', { name: /Löschen|Delete/ }).click();
  await confirm(page);
  await expect.poll(async () => (await api.get(`/api/profiles/${created.id}`)).status()).toBe(404);
});

test('document: details, split, merge back, finish, download, history search', async ({ page }) => {
  const id = await storedDoc('letter_de', 'E2E Split');
  await uiLogin(page);
  await page.goto(`/documents/${id}`);
  const grid = page.getByTestId('page-grid');
  await expect(grid.locator('li')).toHaveCount(3);
  // details
  await page.getByLabel(/^Titel|^Title/).fill('E2E Splitdokument');
  await page.getByLabel(/^Titel|^Title/).blur();
  // split before page 3
  await grid.locator('li').nth(2).getByRole('checkbox').check();
  await page.getByRole('button', { name: /^Teilen$|^Split$/ }).click();
  await confirm(page).catch(() => undefined);
  await expect(grid.locator('li')).toHaveCount(2, { timeout: 30_000 });
  const { api } = await apiLogin();
  const docs = (await (await api.get('/api/documents?limit=50')).json()).items as { id: number; parent_id: number | null; page_count: number; status: string }[];
  const child = docs.find((d) => d.parent_id === id || (d.id > id && d.page_count === 1 && d.status === 'stored'));
  expect(child, 'split created a new document').toBeTruthy();
  // merge it back via the picker
  await page.getByRole('button', { name: /Zusammenführen|Merge/ }).click();
  const dialog = page.locator('dialog[open]');
  await dialog.getByText(`#${child!.id}`).first().click();
  await dialog.getByRole('button', { name: /anhängen|attach|append/i }).click();
  await expect(grid.locator('li')).toHaveCount(3, { timeout: 30_000 });
  // finish → done → download
  await page.getByTestId('doc-finish').click();
  await expect(page.getByTestId('doc-download')).toBeVisible({ timeout: 120_000 });
  const dl = await api.get(`/api/documents/${id}/download`);
  expect(dl.status()).toBe(200);
  expect((await (await api.get(`/api/documents/${id}`)).json()).title).toBe('E2E Splitdokument');
  // history search
  await page.goto('/history');
  await page.getByTestId('history-search').fill('Splitdokument');
  await expect(page.getByTestId('history-list').getByText('E2E Splitdokument').first()).toBeVisible({ timeout: 15_000 });
});

test('document: discard needs confirmation', async ({ page }) => {
  const id = await storedDoc();
  await uiLogin(page);
  await page.goto(`/documents/${id}`);
  await page.getByRole('button', { name: /^Verwerfen$|^Discard$/ }).first().click();
  await confirm(page);
  const { api } = await apiLogin();
  await expect.poll(async () => (await (await api.get(`/api/documents/${id}`)).json()).status).toBe('discarded');
});

test('settings: language switch, contact, API token create + revoke', async ({ page }) => {
  await uiLogin(page);
  await page.goto('/settings');
  await page.locator('#lang').selectOption('en');
  await expect(page.getByRole('heading', { name: 'Settings', exact: true })).toBeVisible();
  await page.locator('#lang').selectOption('de');
  await expect(page.getByRole('heading', { name: 'Einstellungen', exact: true })).toBeVisible();
  // contact
  const mail = `ui${Date.now()}@example.test`;
  await page.getByPlaceholder(/Name/).first().fill('UI Kontakt');
  await page.getByPlaceholder(/name@example/).fill(mail);
  await page.getByRole('button', { name: /Hinzufügen|Add/ }).click();
  await expect(page.getByText(mail)).toBeVisible();
  // token
  const tname = `UI Token ${Date.now()}`;
  await page.getByPlaceholder(/Home Assistant/).fill(tname);
  await page.getByRole('button', { name: /Erstellen|Create/ }).click();
  const token = await page.getByTestId('new-token').inputValue();
  expect(token).toMatch(/^sbx_/);
  await page.locator('dialog[open]').getByRole('button', { name: /Schließen|Close|Fertig|Done/ }).first().click();
  const row = page.locator('li', { hasText: tname });
  await row.getByRole('button', { name: /Widerrufen|Revoke/ }).click();
  await confirm(page);
  await expect(row).toContainText(/widerrufen|revoked/i); // stays listed, marked revoked
  await expect(row.getByRole('button', { name: /Widerrufen|Revoke/ })).toHaveCount(0);
  const { api } = await apiLogin();
  const listed = (await (await api.get('/api/me/tokens')).json()).find((x: { name: string }) => x.name === tname);
  expect(listed.revoked_at ?? listed.revoked).toBeTruthy();
  const anon = await request.newContext({ baseURL: BASE }); // no session cookie: only the token counts
  expect((await anon.get('/api/documents', { headers: { Authorization: `Bearer ${token}` } })).status()).toBe(401);
  await anon.dispose();
});

test('admin: scanner test, storage, diagnostics, users', async ({ page }) => {
  await uiLogin(page);
  await page.goto('/admin/scanner');
  await page.getByTestId('scanner-test').click();
  await expect(page.getByText(/eSCL|Simulator|ScanJet/).first()).toBeVisible({ timeout: 20_000 });
  await page.goto('/admin/storage');
  await expect(page.getByText(/Cache/).first()).toBeVisible();
  await page.goto('/admin/diagnostics');
  await expect(page.getByText(/tesseract/i).first()).toBeVisible({ timeout: 20_000 });
  await page.goto('/admin/users');
  await expect(page.getByText(/admin/).first()).toBeVisible();
});

test('logout returns to the login page', async ({ page }) => {
  await uiLogin(page);
  await page.getByRole('button', { name: /Abmelden|Log out|Sign out/ }).first().click();
  await expect(page.getByLabel(/Benutzername|Username/)).toBeVisible();
  await page.goto('/history');
  await expect(page.getByLabel(/Benutzername|Username/)).toBeVisible();
});
