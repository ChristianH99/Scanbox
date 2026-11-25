import { api, isApiError, setUnauthorizedHandler } from '../api/client';
import type { AuthConfig, Me, User } from '../api/types';
import { connectLive, disconnectLive } from '../events.svelte';
import { i18n } from '../i18n/index.svelte';
import { navigate, router } from '../router.svelte';
import { storageGet, storageSet } from '../utils/misc';

export type Theme = 'system' | 'light' | 'dark';

export const session = $state<{
  user: User | null;
  ready: boolean;
  authConfig: AuthConfig | null;
  /** Local account whose password may be changed in the app (not SSO, not pinned by the environment). */
  canChangePassword: boolean;
}>({
  user: null,
  ready: false,
  authConfig: null,
  canChangePassword: false,
});

// ------------------------------------------------------------------ theme
let media: MediaQueryList | null = null;

export function applyTheme(theme: Theme): void {
  storageSet('scanbox.theme', theme);
  if (typeof document === 'undefined') return;
  media ??= window.matchMedia?.('(prefers-color-scheme: dark)') ?? null;
  const dark = theme === 'dark' || (theme === 'system' && !!media?.matches);
  document.documentElement.classList.toggle('dark', dark);
  document.querySelector('meta[name="theme-color"]')?.setAttribute('content', dark ? '#020617' : '#0f766e');
}

export function initTheme(): void {
  const t = (storageGet('scanbox.theme') as Theme | null) ?? 'system';
  applyTheme(t);
  media = window.matchMedia?.('(prefers-color-scheme: dark)') ?? null;
  media?.addEventListener?.('change', () => applyTheme((storageGet('scanbox.theme') as Theme | null) ?? 'system'));
}

// ------------------------------------------------------------------ session
function adopt(me: Me): void {
  const user = me.user;
  session.user = user;
  session.canChangePassword = !!me.can_change_password;
  if (user.locale === 'de' || user.locale === 'en') i18n.locale = user.locale;
  applyTheme((user.theme as Theme) || 'system');
  connectLive(checkSession);
}

/** Used by the SSE reconnect logic: false = signed out (stop reconnecting). */
async function checkSession(): Promise<boolean> {
  try {
    await api.get<Me>('/api/auth/me', { allow401: true });
    return true;
  } catch (e) {
    if (isApiError(e) && e.status === 401) {
      signedOut();
      return false;
    }
    return true; // server down: keep trying
  }
}

function signedOut(): void {
  const wasSignedIn = session.user !== null;
  session.user = null;
  disconnectLive();
  if (router.route.name !== 'login') {
    const next = router.path + router.search;
    void navigate(`/login${wasSignedIn || next !== '/' ? `?next=${encodeURIComponent(next)}` : ''}`, {
      replace: true,
      force: true,
    });
  }
}

export async function loadSession(): Promise<void> {
  setUnauthorizedHandler((path) => {
    if (!path.startsWith('/api/auth/login') && !path.startsWith('/api/auth/setup')) signedOut();
  });
  try {
    session.authConfig = await api.get<AuthConfig>('/api/auth/config');
  } catch {
    session.authConfig = { oidc_enabled: false, oidc_label: 'SSO', local_login: true, setup_required: false, default_admin_username: 'admin' };
  }
  try {
    const me = await api.get<Me>('/api/auth/me', { allow401: true });
    adopt(me);
  } catch (e) {
    session.user = null;
    if (!(isApiError(e) && e.status === 401)) {
      // Server unreachable: the login page shows the error; keep going.
      console.warn('session check failed', e);
    }
  } finally {
    session.ready = true;
  }
}

export async function login(username: string, password: string): Promise<void> {
  const me = await api.post<Me>('/api/auth/login', { username, password });
  adopt(me);
}

/** First-start setup: create the admin with the one-time code from the container log, then sign in. */
export async function completeSetup(code: string, username: string, password: string): Promise<void> {
  const me = await api.post<Me>('/api/auth/setup', { code, username, password });
  if (session.authConfig) session.authConfig.setup_required = false;
  adopt(me);
}

export async function changePassword(currentPassword: string, newPassword: string): Promise<void> {
  await api.post('/api/auth/password', { current_password: currentPassword, new_password: newPassword });
}

export async function logout(): Promise<void> {
  try {
    await api.post('/api/auth/logout');
  } finally {
    session.user = null;
    disconnectLive();
    await navigate('/login', { replace: true, force: true });
  }
}

export async function updatePrefs(p: { locale?: 'de' | 'en'; theme?: Theme; display_name?: string }): Promise<void> {
  const u = await api.patch<User>('/api/auth/me', p);
  session.user = u;
  if (p.locale) i18n.locale = p.locale;
  if (p.theme) applyTheme(p.theme);
}
