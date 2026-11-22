/** Minimal history-API router (no hash). The backend serves index.html for unknown non-/api paths. */

export type RouteName =
  | 'scan'
  | 'login'
  | 'document'
  | 'inbox'
  | 'history'
  | 'profiles'
  | 'profile'
  | 'settings'
  | 'admin'
  | 'notfound';

const ROUTES: [RegExp, RouteName, string[]][] = [
  [/^\/$/, 'scan', []],
  [/^\/login\/?$/, 'login', []],
  [/^\/documents\/(\d+)\/?$/, 'document', ['id']],
  [/^\/inbox\/?$/, 'inbox', []],
  [/^\/history\/?$/, 'history', []],
  [/^\/profiles\/?$/, 'profiles', []],
  [/^\/profiles\/(new|\d+)\/?$/, 'profile', ['id']],
  [/^\/settings\/?$/, 'settings', []],
  [/^\/admin\/?$/, 'admin', []],
  [/^\/admin\/([a-z-]+)\/?$/, 'admin', ['tab']],
];

export interface Match {
  name: RouteName;
  params: Record<string, string>;
}

export function matchRoute(path: string): Match {
  for (const [re, name, keys] of ROUTES) {
    const m = re.exec(path);
    if (m) {
      const params: Record<string, string> = {};
      keys.forEach((k, i) => (params[k] = decodeURIComponent(m[i + 1])));
      return { name, params };
    }
  }
  return { name: 'notfound', params: {} };
}

const loc = $state({
  path: typeof location !== 'undefined' ? location.pathname : '/',
  search: typeof location !== 'undefined' ? location.search : '',
});

type Guard = () => boolean | Promise<boolean>;
let leaveGuard: Guard | null = null;

/** Register a guard (e.g. unsaved changes) that may veto navigation. Returns an unregister fn. */
export function setLeaveGuard(g: Guard | null): () => void {
  leaveGuard = g;
  return () => {
    if (leaveGuard === g) leaveGuard = null;
  };
}

export const router = {
  get path() {
    return loc.path;
  },
  get search() {
    return loc.search;
  },
  get query(): URLSearchParams {
    return new URLSearchParams(loc.search);
  },
  get route(): Match {
    return matchRoute(loc.path);
  },
};

function sync() {
  loc.path = location.pathname;
  loc.search = location.search;
}

export async function navigate(to: string, opts: { replace?: boolean; force?: boolean } = {}): Promise<void> {
  const url = new URL(to, location.origin);
  if (url.pathname === location.pathname && url.search === location.search) return;
  if (!opts.force && leaveGuard && url.pathname !== location.pathname) {
    const ok = await leaveGuard();
    if (!ok) return;
    leaveGuard = null;
  }
  if (opts.replace) history.replaceState({}, '', url.pathname + url.search);
  else history.pushState({}, '', url.pathname + url.search);
  sync();
  if (!opts.replace) window.scrollTo({ top: 0 });
}

/** Update query parameters in place (filters etc.). */
export function setQuery(params: Record<string, string | number | null | undefined>): void {
  const sp = new URLSearchParams(location.search);
  for (const [k, v] of Object.entries(params)) {
    if (v === null || v === undefined || v === '') sp.delete(k);
    else sp.set(k, String(v));
  }
  const qs = sp.toString();
  history.replaceState({}, '', location.pathname + (qs ? `?${qs}` : ''));
  sync();
}

export function installRouter(): () => void {
  const onPop = () => sync();
  const onClick = (e: MouseEvent) => {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    const a = (e.target as Element | null)?.closest?.('a');
    if (!a || a.target || a.hasAttribute('download') || a.dataset.external !== undefined) return;
    const href = a.getAttribute('href');
    if (!href || href.startsWith('#') || href.startsWith('/api/')) return;
    const url = new URL(href, location.href);
    if (url.origin !== location.origin) return;
    e.preventDefault();
    void navigate(url.pathname + url.search);
  };
  window.addEventListener('popstate', onPop);
  document.addEventListener('click', onClick);
  return () => {
    window.removeEventListener('popstate', onPop);
    document.removeEventListener('click', onClick);
  };
}
