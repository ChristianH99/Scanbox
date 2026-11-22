import { expect, request, type APIRequestContext, type Page } from '@playwright/test';

export const BASE = process.env.E2E_BASE_URL ?? 'http://127.0.0.1:8080';
export const SIM = process.env.E2E_SIM_URL ?? 'http://127.0.0.1:8090';
export const USER = process.env.E2E_USER ?? 'admin';
export const PASSWORD = process.env.E2E_PASSWORD ?? 'adminadmin';

/** API client logged in with cookies; sends the CSRF header for unsafe methods. */
export async function apiLogin(): Promise<{ api: APIRequestContext; csrf: () => Promise<Record<string, string>> }> {
  const api = await request.newContext({ baseURL: BASE });
  const r = await api.post('/api/auth/login', { data: { username: USER, password: PASSWORD } });
  expect(r.ok(), `login failed: ${r.status()} ${await r.text()}`).toBeTruthy();
  const csrf = async () => {
    const state = await api.storageState();
    const c = state.cookies.find((x) => x.name === 'scanbox_csrf');
    return c ? { 'X-CSRF-Token': c.value } : {};
  };
  return { api, csrf };
}

/** A profile that works without Tesseract/Ghostscript: no OCR, no auto-rotate, plain PDF, simplex. */
export async function createTestProfile(api: APIRequestContext, headers: Record<string, string>, name: string): Promise<number> {
  const list = await (await api.get('/api/profiles')).json();
  const base = list.find((p: { builtin: boolean }) => p.builtin).settings;
  const settings = {
    ...base,
    source: 'adf_simplex',
    resolution: 200,
    color_mode: 'gray',
    processing: { ...base.processing, auto_rotate: false, deskew: false, blank_removal: 'off' },
    ocr: { ...base.ocr, enabled: false, suggest_title: false },
    output: { ...base.output, format: 'pdf', compression: 'none' },
    qr: { ...base.qr, enabled: false },
    destinations: [{ type: 'download', enabled: true, label: '', config: {} }],
    review_before_processing: true,
    filename_template: '{date}_{title}',
  };
  const r = await api.post('/api/profiles', { data: { name, description: 'e2e', settings }, headers });
  expect(r.status(), await r.text()).toBe(201);
  return (await r.json()).id;
}

export async function simLoad(stack: string | null): Promise<void> {
  const ctx = await request.newContext({ baseURL: SIM });
  await ctx.post('/_sim/reset');
  await ctx.delete('/_sim/faults');
  const r = await ctx.post('/_sim/load', { data: stack ? { stack } : { empty: true } });
  expect(r.ok(), await r.text()).toBeTruthy();
  await ctx.dispose();
}

export async function uiLogin(page: Page): Promise<void> {
  await page.goto('/login');
  await page.getByLabel(/Benutzername|Username/).fill(USER);
  await page.getByLabel(/Passwort|Password/).fill(PASSWORD);
  await page.getByRole('button', { name: /^(Anmelden|Sign in)$/ }).click();
  await expect(page.getByTestId('scanner-status')).toBeVisible();
}
