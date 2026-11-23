/**
 * Layout audit: visits every page, menu and dialog at several viewport sizes (German = longest
 * labels; English + dark as a second pass) and fails on
 *   - overlapping controls (buttons, links, inputs, selects …) that are not nested in each other,
 *   - controls whose content overflows their box (text sticking out of a button),
 *   - controls or the page sticking out of the viewport horizontally,
 *   - the last control of a page being hidden behind the fixed mobile navigation.
 * Intentional overlays (e.g. the selection checkbox on a page thumbnail) carry
 * `data-layout-overlay` and may overlap controls of their `data-layout-group`.
 *
 * Screenshots of every state land in test-results/layout/<combo>/ (LAYOUT_SCREENSHOTS=1).
 */
import { expect, test, type APIRequestContext, type Page } from '@playwright/test';
import { apiLogin, SIM, simLoad, uiLogin } from './helpers';
import { request } from '@playwright/test';

type Combo = { name: string; width: number; height: number; locale: 'de' | 'en'; theme: 'light' | 'dark' };

const COMBOS: Combo[] = [
  { name: 'phone-de', width: 360, height: 740, locale: 'de', theme: 'light' },
  { name: 'tablet-de', width: 768, height: 1024, locale: 'de', theme: 'light' },
  { name: 'laptop-de', width: 1024, height: 768, locale: 'de', theme: 'light' },
  { name: 'desktop-de', width: 1440, height: 900, locale: 'de', theme: 'light' },
  { name: 'phone-en-dark', width: 390, height: 844, locale: 'en', theme: 'dark' },
  { name: 'desktop-en-dark', width: 1280, height: 800, locale: 'en', theme: 'dark' },
];

const SHOTS = !!process.env.LAYOUT_SCREENSHOTS;
const FAST = { ocr: { enabled: false, suggest_title: false }, processing: { auto_rotate: false, deskew: false } };

interface Fixture { review: number; done: number; attention: number }
let fx: Fixture;

async function waitStatus(api: APIRequestContext, id: number, states: string[]): Promise<void> {
  for (let i = 0; i < 300; i++) {
    const d = await (await api.get(`/api/documents/${id}`)).json();
    if (states.includes(d.status)) return;
    await new Promise((r) => setTimeout(r, 200));
  }
  throw new Error(`document ${id} never reached ${states.join('/')}`);
}

async function scanDoc(api: APIRequestContext, headers: Record<string, string>, profileId: number, stack: string,
                       overrides: Record<string, unknown> = {}): Promise<number> {
  await simLoad(stack);
  const r = await api.post('/api/scans', { data: { profile_id: profileId, overrides: { ...FAST, source: 'adf_duplex', ...overrides } }, headers });
  expect(r.status(), await r.text()).toBe(201);
  return (await r.json()).id;
}

test.beforeAll(async () => {
  test.setTimeout(240_000);
  const { api, csrf } = await apiLogin();
  const h = await csrf();
  const profiles = await (await api.get('/api/profiles')).json();
  const pid = profiles.find((p: { builtin: boolean; name: string }) => p.builtin && p.name.startsWith('Dokument')).id;
  // 1) waiting for review, with blank pages flagged
  const review = await scanDoc(api, h, pid, 'with_blanks');
  await waitStatus(api, review, ['stored']);
  // 2) finished and delivered
  const done = await scanDoc(api, h, pid, 'letter_de', { source: 'adf_simplex' });
  await waitStatus(api, done, ['stored']);
  await api.post(`/api/documents/${done}/finish`, { headers: h });
  await waitStatus(api, done, ['done', 'needs_attention', 'failed']);
  // 3) paper jam mid-stack → needs attention with recovery actions
  await simLoad('letter_de');
  const sim = await request.newContext({ baseURL: SIM });
  await sim.post('/_sim/faults', { data: { jam_after_pages: 2 } });
  await sim.dispose();
  const r = await api.post('/api/scans', { data: { profile_id: pid, overrides: { ...FAST, source: 'adf_simplex' } }, headers: h });
  const attention = (await r.json()).id;
  await waitStatus(api, attention, ['needs_attention', 'failed', 'stored']);
  await simLoad('with_blanks');
  // an API token and a contact so the settings lists are not empty
  await api.post('/api/me/tokens', { data: { name: 'Home Assistant Automatisierung' }, headers: h });
  fx = { review, done, attention };
  await api.dispose();
});

// The combos change the shared e2e user's language/theme – restore them for the other specs.
test.afterAll(async () => {
  const { api, csrf } = await apiLogin();
  await api.patch('/api/auth/me', { data: { locale: 'de', theme: 'system' }, headers: await csrf() });
  await api.dispose();
});

// ------------------------------------------------------------------------------------------ checks
interface Violation { kind: string; a: string; b?: string; detail?: string }

async function audit(page: Page): Promise<Violation[]> {
  return page.evaluate(() => {
    const CTRL = 'button, a[href], input:not([type=hidden]), select, textarea, summary, [role=button], label.btn';
    // controls, chips and absolutely positioned badges that carry text
    const SEL = `${CTRL}, .chip, [class~=absolute]`;
    const out: { kind: string; a: string; b?: string; detail?: string }[] = [];
    const vw = document.documentElement.clientWidth;
    const dialog = [...document.querySelectorAll('dialog[open], [role=dialog]')].filter((d) => (d as HTMLElement).offsetParent !== null || d.tagName === 'DIALOG').pop() as HTMLElement | undefined;
    const root: ParentNode = dialog ?? document;
    const desc = (el: Element): string => {
      const e = el as HTMLElement;
      const label = (e.getAttribute('aria-label') || e.innerText || (e as HTMLInputElement).value || e.getAttribute('title') || e.id || '').trim().replace(/\s+/g, ' ').slice(0, 40);
      const tid = e.dataset.testid ? `[${e.dataset.testid}]` : '';
      return `${e.tagName.toLowerCase()}${tid} "${label}"`;
    };
    const visible = (el: Element): boolean => {
      const e = el as HTMLElement;
      const r = e.getBoundingClientRect();
      if (r.width < 2 || r.height < 2) return false; // sr-only / hidden
      const cs = getComputedStyle(e);
      if (cs.visibility === 'hidden' || cs.display === 'none' || Number(cs.opacity) === 0) return false;
      if (e.closest('[aria-hidden=true]')) return false;
      if (!e.matches(CTRL) && !e.matches('.chip') && !e.innerText.trim()) return false; // decorative layer
      // clipped away by an ancestor (sr-only containers, collapsed details)
      for (let p = e.parentElement; p; p = p.parentElement) {
        const pr = p.getBoundingClientRect();
        if ((pr.width < 2 || pr.height < 2) && getComputedStyle(p).overflow !== 'visible') return false;
      }
      return true;
    };
    const fixedAncestor = (el: Element): HTMLElement | null => {
      for (let p: HTMLElement | null = el as HTMLElement; p; p = p.parentElement) {
        const pos = getComputedStyle(p).position;
        if (pos === 'fixed' || pos === 'sticky') return p;
      }
      return null;
    };
    const scroller = (el: Element): HTMLElement | null => {
      for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
        const ox = getComputedStyle(p).overflowX;
        if ((ox === 'auto' || ox === 'scroll') && p.scrollWidth > p.clientWidth) return p;
      }
      return null;
    };
    /** Visible part of an element: its box clipped by every ancestor that clips overflow
     *  (scrollable dialog bodies, overflow-hidden cards). */
    const clipped = (e: Element) => {
      const r = e.getBoundingClientRect();
      let x0 = r.left, y0 = r.top, x1 = r.right, y1 = r.bottom;
      for (let p = e.parentElement; p && p !== document.body; p = p.parentElement) {
        const cs = getComputedStyle(p);
        if (cs.overflowX === 'visible' && cs.overflowY === 'visible') continue;
        const pr = p.getBoundingClientRect();
        x0 = Math.max(x0, pr.left); y0 = Math.max(y0, pr.top); x1 = Math.min(x1, pr.right); y1 = Math.min(y1, pr.bottom);
      }
      return { x: x0 + scrollX, y: y0 + scrollY, w: Math.max(0, x1 - x0), h: Math.max(0, y1 - y0) };
    };
    const els = [...root.querySelectorAll(SEL)].filter(visible);
    const rects = els.map((e) => ({ ...clipped(e), fixed: fixedAncestor(e) }));
    // 1) pairwise overlap (same layer only: both in normal flow, or both in the same fixed container)
    for (let i = 0; i < els.length; i++) {
      for (let j = i + 1; j < els.length; j++) {
        const a = els[i], b = els[j];
        if (a.contains(b) || b.contains(a)) continue;
        if (rects[i].fixed !== rects[j].fixed) continue;
        if (rects[i].w < 2 || rects[i].h < 2 || rects[j].w < 2 || rects[j].h < 2) continue; // scrolled out of view
        // an intentional overlay may sit on a much larger surface of its group (e.g. the thumbnail)
        const area = (k: number) => rects[k].w * rects[k].h;
        const ov = (el: Element, other: Element, ke: number, ko: number) => {
          const o = el.closest('[data-layout-overlay]');
          if (!o) return false;
          const g = o.closest('[data-layout-group]');
          return !!g && g.contains(other) && area(ko) > 4 * area(ke);
        };
        if (ov(a, b, i, j) || ov(b, a, j, i)) continue;
        // a label and its own control
        const la = a.tagName === 'LABEL' ? (a as HTMLLabelElement).control : null;
        const lb = b.tagName === 'LABEL' ? (b as HTMLLabelElement).control : null;
        if (la === b || lb === a) continue;
        const ra = rects[i], rb = rects[j];
        const ix = Math.min(ra.x + ra.w, rb.x + rb.w) - Math.max(ra.x, rb.x);
        const iy = Math.min(ra.y + ra.h, rb.y + rb.h) - Math.max(ra.y, rb.y);
        if (ix > 1.5 && iy > 1.5) out.push({ kind: 'overlap', a: desc(a), b: desc(b), detail: `${Math.round(ix)}x${Math.round(iy)}px` });
      }
    }
    // 2) content overflowing its control (text sticking out of a button / chip)
    for (const el of els) {
      const e = el as HTMLElement;
      if (['INPUT', 'SELECT', 'TEXTAREA'].includes(e.tagName)) continue;
      if (e.classList.contains('truncate') || e.querySelector('.truncate')) continue;
      if (e.scrollWidth > e.clientWidth + 2 && getComputedStyle(e).overflowX === 'visible') {
        out.push({ kind: 'content-overflow', a: desc(e), detail: `${e.scrollWidth} > ${e.clientWidth}` });
      }
    }
    // 2b) navigation and tab labels must not be cut off ("Einstellu…")
    for (const el of root.querySelectorAll('nav .truncate, [role=tablist] .truncate')) {
      const e = el as HTMLElement;
      if (visible(e) && e.scrollWidth > e.clientWidth + 1) out.push({ kind: 'truncated-label', a: desc(e), detail: `${e.scrollWidth} > ${e.clientWidth}` });
    }
    // 3) out of the viewport horizontally (unless inside a horizontal scroller)
    for (let i = 0; i < els.length; i++) {
      const r = els[i].getBoundingClientRect();
      if ((r.right > vw + 1 || r.left < -1) && !scroller(els[i])) out.push({ kind: 'offscreen', a: desc(els[i]), detail: `${Math.round(r.left)}..${Math.round(r.right)} of ${vw}` });
    }
    if (document.documentElement.scrollWidth > vw + 1) out.push({ kind: 'page-hscroll', a: 'document', detail: `${document.documentElement.scrollWidth} > ${vw}` });
    return out;
  });
}

/** The last control of the main content must be reachable (not under the fixed bottom nav). */
async function auditBottom(page: Page): Promise<Violation[]> {
  return page.evaluate(async () => {
    window.scrollTo(0, document.documentElement.scrollHeight);
    await new Promise((r) => setTimeout(r, 150));
    const main = document.querySelector('main');
    if (!main) return [];
    const ctrls = [...main.querySelectorAll('button, a[href], input:not([type=hidden]), select, textarea')]
      .filter((e) => { const r = e.getBoundingClientRect(); return r.width > 2 && r.height > 2; });
    const last = ctrls.pop();
    if (!last) return [];
    const r = last.getBoundingClientRect();
    const hit = document.elementFromPoint(r.left + r.width / 2, Math.min(r.bottom - 2, innerHeight - 1));
    const ok = !!hit && (last.contains(hit) || hit.contains(last) || !!hit.closest('label')?.contains(last));
    window.scrollTo(0, 0);
    return ok ? [] : [{ kind: 'covered', a: (last as HTMLElement).innerText?.trim().slice(0, 40) || last.tagName, b: hit?.tagName ?? 'nothing' }];
  });
}

// ------------------------------------------------------------------------------------------ states
type State = { name: string; run: (page: Page) => Promise<void>; bottom?: boolean };

const settle = (page: Page, ms = 500) => page.waitForTimeout(ms);
const openAllDetails = (page: Page) => page.$$eval('details', (ds) => ds.forEach((d) => ((d as HTMLDetailsElement).open = true)));
const go = async (page: Page, url: string) => {
  await page.goto(url);
  await page.locator('main').waitFor();
  await settle(page);
};

const STATES: State[] = [
  { name: 'scan', bottom: true, run: async (p) => {
    await go(p, '/');
    await p.getByRole('button', { name: /Schnelleinstellungen|Quick options/ }).click();
    await settle(p, 300);
  } },
  { name: 'scan-active', bottom: true, run: async (p) => {
    await simLoad('with_blanks');
    await go(p, '/');
    await p.getByTestId('start-scan').click();
    await p.getByTestId('active-document').first().waitFor();
    await p.waitForFunction(() => !document.querySelector('[data-testid=active-document] [data-testid=stop-scan]'), null, { timeout: 60_000 });
    await settle(p, 800);
  } },
  { name: 'document-review', bottom: true, run: async (p) => { await go(p, `/documents/${fx.review}`); await settle(p, 800); } },
  { name: 'document-selection', run: async (p) => {
    await go(p, `/documents/${fx.review}`);
    await p.getByRole('checkbox', { name: /1/ }).first().check();
    await settle(p, 300);
  } },
  { name: 'page-editor', run: async (p) => {
    await go(p, `/documents/${fx.review}`);
    await p.getByRole('button', { name: /Seite 2|page 2/i }).first().click();
    await p.locator('dialog[open]').waitFor();
    await settle(p, 800);
  } },
  { name: 'merge-picker', run: async (p) => {
    await go(p, `/documents/${fx.review}`);
    await p.getByRole('button', { name: /Zusammenführen|Merge/ }).first().click();
    await p.locator('dialog[open]').waitFor();
    await settle(p);
  } },
  { name: 'confirm-discard', run: async (p) => {
    await go(p, `/documents/${fx.review}`);
    await p.getByRole('button', { name: /Verwerfen|Discard/ }).first().click();
    await p.getByTestId('confirm-ok').waitFor();
    await settle(p, 300);
  } },
  { name: 'document-done', bottom: true, run: async (p) => { await go(p, `/documents/${fx.done}`); } },
  { name: 'document-attention', bottom: true, run: async (p) => { await go(p, `/documents/${fx.attention}`); } },
  { name: 'inbox', bottom: true, run: async (p) => { await go(p, '/inbox'); } },
  { name: 'history', bottom: true, run: async (p) => { await go(p, '/history'); await settle(p, 800); } },
  { name: 'profiles', bottom: true, run: async (p) => { await go(p, '/profiles'); } },
  { name: 'profile-new', bottom: true, run: async (p) => { await go(p, '/profiles/new'); await openAllDetails(p); await settle(p, 300); } },
  { name: 'profile-builtin', bottom: true, run: async (p) => { await go(p, '/profiles/1'); await openAllDetails(p); await settle(p, 300); } },
  { name: 'settings', bottom: true, run: async (p) => { await go(p, '/settings'); } },
  ...['scanner', 'sso', 'mail', 'paperless', 'storage', 'walkup', 'users', 'diagnostics'].map((tab): State => ({
    name: `admin-${tab}`, bottom: true, run: async (p) => { await go(p, `/admin/${tab}`); await settle(p, 500); },
  })),
  { name: 'not-found', run: async (p) => { await go(p, '/does-not-exist'); } },
];

// ------------------------------------------------------------------------------------------ tests
for (const combo of COMBOS) {
  test(`layout ${combo.name}`, async ({ browser }) => {
    test.setTimeout(600_000);
    const ctx = await browser.newContext({ viewport: { width: combo.width, height: combo.height }, locale: combo.locale === 'de' ? 'de-DE' : 'en-US', colorScheme: combo.theme, serviceWorkers: 'block' });
    const page = await ctx.newPage();
    page.setDefaultTimeout(20_000); // a covered control must fail the state, not hang the run
    const problems: string[] = [];
    const record = (state: string, vs: Violation[]) => {
      for (const v of vs) problems.push(`[${combo.name}] ${state}: ${v.kind} ${v.a}${v.b ? ` × ${v.b}` : ''}${v.detail ? ` (${v.detail})` : ''}`);
    };

    // login page (logged out)
    await page.goto('/login');
    await page.getByLabel(/Benutzername|Username/).waitFor();
    record('login', await audit(page));
    if (SHOTS) await page.screenshot({ path: `test-results/layout/${combo.name}/login.png`, fullPage: true });

    await uiLogin(page);
    const csrf = (await ctx.cookies()).find((c) => c.name === 'scanbox_csrf')?.value ?? '';
    await page.request.patch('/api/auth/me', { data: { locale: combo.locale, theme: combo.theme }, headers: { 'X-CSRF-Token': csrf } });

    for (const st of STATES) {
      const t0 = Date.now();
      try {
        await st.run(page);
      } catch (e) {
        problems.push(`[${combo.name}] ${st.name}: could not open state – ${(e as Error).message.split('\n')[0]}`);
        continue;
      }
      record(st.name, await audit(page));
      if (st.bottom && combo.width < 768) record(`${st.name} (bottom)`, await auditBottom(page));
      if (SHOTS) await page.screenshot({ path: `test-results/layout/${combo.name}/${st.name}.png`, fullPage: true });
      await page.keyboard.press('Escape');
      console.log(`[${combo.name}] ${st.name}: ${Date.now() - t0} ms`);
    }
    await ctx.close();
    expect(problems, problems.join('\n')).toEqual([]);
  });
}
