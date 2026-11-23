<script lang="ts">
  import { onMount } from 'svelte';
  import Icon from './lib/components/Icon.svelte';
  import Toasts from './lib/components/Toasts.svelte';
  import ConfirmHost from './lib/components/ConfirmHost.svelte';
  import type { IconName } from './lib/components/icons';
  import { installRouter, navigate, router } from './lib/router.svelte';
  import { loadSession, logout, session } from './lib/stores/session.svelte';
  import { initScannerStore } from './lib/stores/scanner.svelte';
  import { live, onLive } from './lib/events.svelte';
  import { api } from './lib/api/client';
  import type { DocumentList } from './lib/api/types';
  import { t, type MessageKey } from './lib/i18n/index.svelte';
  import { debounce } from './lib/utils/misc';

  import Login from './routes/Login.svelte';
  import Scan from './routes/Scan.svelte';
  import DocumentPage from './routes/Document.svelte';
  import Inbox from './routes/Inbox.svelte';
  import History from './routes/History.svelte';
  import Profiles from './routes/Profiles.svelte';
  import ProfileEdit from './routes/ProfileEdit.svelte';
  import Settings from './routes/Settings.svelte';
  import Admin from './routes/Admin.svelte';

  let inboxCount = $state(0);

  onMount(() => {
    const off = installRouter();
    void loadSession();
    return off;
  });

  // Auth guard
  $effect(() => {
    if (!session.ready) return;
    const name = router.route.name;
    if (!session.user && name !== 'login') {
      const next = router.path + router.search;
      void navigate(`/login${next !== '/' ? `?next=${encodeURIComponent(next)}` : ''}`, { replace: true, force: true });
    } else if (session.user && name === 'login') {
      const next = router.query.get('next');
      void navigate(next && next.startsWith('/') && !next.startsWith('//') ? next : '/', { replace: true, force: true });
    }
  });

  async function loadInboxCount() {
    try {
      const r = await api.get<DocumentList>('/api/documents', { query: { scope: 'inbox', limit: 1 } });
      inboxCount = r.total;
    } catch {
      /* ignore */
    }
  }

  $effect(() => {
    if (!session.user) return;
    initScannerStore();
    void loadInboxCount();
    const refetch = debounce(loadInboxCount, 500);
    return onLive(['inbox.changed', 'reconnect'], refetch);
  });

  interface NavItem {
    href: string;
    label: MessageKey;
    short?: MessageKey; // bottom navigation on phones
    icon: IconName;
    match: string[];
    badge?: number;
  }
  let nav = $derived<NavItem[]>([
    { href: '/', label: 'nav.scan', icon: 'scan', match: ['scan', 'document'] },
    { href: '/inbox', label: 'nav.inbox', icon: 'inbox', match: ['inbox'], badge: inboxCount },
    { href: '/history', label: 'nav.history', icon: 'history', match: ['history'] },
    { href: '/profiles', label: 'nav.profiles', icon: 'layers', match: ['profiles', 'profile'] },
    { href: '/settings', label: 'nav.settings', short: 'nav.settings_short', icon: 'user', match: ['settings'] },
    ...(session.user?.is_admin ? [{ href: '/admin', label: 'nav.admin' as MessageKey, icon: 'shield' as IconName, match: ['admin'] }] : []),
  ]);
  let active = $derived(router.route.name);
  let liveDown = $derived(!!session.user && live.everConnected && !live.connected);
</script>

{#if !session.ready}
  <div class="flex min-h-dvh items-center justify-center text-slate-500"><Icon name="loader" size={32} label={t('common.loading')} /></div>
{:else if router.route.name === 'login' || !session.user}
  <Login />
{:else}
  <div class="min-h-dvh pb-20 md:pb-0 md:pl-56">
    <!-- Desktop / tablet sidebar -->
    <aside class="fixed inset-y-0 left-0 z-30 hidden w-56 flex-col border-r border-slate-200 bg-white md:flex dark:border-slate-800 dark:bg-slate-900">
      <a href="/" class="flex items-center gap-2 px-4 py-4 text-lg font-bold">
        <img src="/favicon.svg" alt="" class="size-8" /> Scanbox
      </a>
      <nav class="flex-1 space-y-1 px-2" aria-label={t('nav.main')}>
        {#each nav as item (item.href)}
          <a
            href={item.href}
            class="flex min-h-11 items-center gap-3 rounded-lg px-3 text-sm font-medium {item.match.includes(active)
              ? 'bg-brand-50 text-brand-800 dark:bg-brand-800/30 dark:text-brand-100'
              : 'text-slate-700 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800'}"
            aria-current={item.match.includes(active) ? 'page' : undefined}
          >
            <Icon name={item.icon} />
            <span class="flex-1">{t(item.label)}</span>
            {#if item.badge}<span class="chip bg-amber-500 text-white" data-testid="inbox-badge">{item.badge}</span>{/if}
          </a>
        {/each}
      </nav>
      <div class="border-t border-slate-200 p-3 text-sm dark:border-slate-800">
        <p class="truncate px-1 text-slate-600 dark:text-slate-400" title={session.user.username}>{session.user.display_name || session.user.username}</p>
        <button type="button" class="btn btn-ghost mt-1 w-full justify-start" onclick={() => logout()}>
          <Icon name="logout" />{t('nav.logout')}
        </button>
      </div>
    </aside>

    <!-- Mobile top bar -->
    <header class="sticky top-0 z-20 flex items-center gap-2 border-b border-slate-200 bg-white/90 px-4 py-2 backdrop-blur md:hidden dark:border-slate-800 dark:bg-slate-900/90">
      <a href="/" class="flex items-center gap-2 font-bold"><img src="/favicon.svg" alt="" class="size-7" />Scanbox</a>
      <span class="flex-1"></span>
      <button type="button" class="btn btn-ghost btn-icon" onclick={() => logout()} aria-label={t('nav.logout')}><Icon name="logout" /></button>
    </header>

    {#if liveDown}
      <div class="sticky top-0 z-10 flex items-center gap-2 bg-amber-100 px-4 py-2 text-sm text-amber-900 dark:bg-amber-900/60 dark:text-amber-100" role="status">
        <Icon name="wifi-off" size={16} />{t('live.reconnecting')}
      </div>
    {/if}

    <main class="mx-auto w-full max-w-6xl px-3 py-4 sm:px-6 sm:py-6">
      {#if router.route.name === 'scan'}
        <Scan />
      {:else if router.route.name === 'document'}
        {#key router.route.params.id}
          <DocumentPage id={Number(router.route.params.id)} />
        {/key}
      {:else if router.route.name === 'inbox'}
        <Inbox />
      {:else if router.route.name === 'history'}
        <History />
      {:else if router.route.name === 'profiles'}
        <Profiles />
      {:else if router.route.name === 'profile'}
        {#key router.route.params.id}
          <ProfileEdit id={router.route.params.id} />
        {/key}
      {:else if router.route.name === 'settings'}
        <Settings />
      {:else if router.route.name === 'admin'}
        {#if session.user.is_admin}
          <Admin tab={router.route.params.tab ?? 'scanner'} />
        {:else}
          <p class="card p-6">{t('common.forbidden')}</p>
        {/if}
      {:else}
        <div class="card p-6">
          <h1 class="text-xl font-semibold">{t('common.not_found')}</h1>
          <a href="/" class="btn btn-primary mt-4">{t('nav.scan')}</a>
        </div>
      {/if}
    </main>

    <!-- Mobile bottom navigation -->
    <nav
      class="fixed inset-x-0 bottom-0 z-30 grid border-t border-slate-200 bg-white pb-[env(safe-area-inset-bottom)] md:hidden dark:border-slate-800 dark:bg-slate-900"
      style="grid-template-columns: repeat({nav.length}, minmax(0, 1fr))"
      aria-label={t('nav.main')}
    >
      {#each nav as item (item.href)}
        <a
          href={item.href}
          class="relative flex min-h-14 flex-col items-center justify-center gap-0.5 text-[11px] font-medium {item.match.includes(active)
            ? 'text-brand-700 dark:text-brand-500'
            : 'text-slate-600 dark:text-slate-400'}"
          aria-current={item.match.includes(active) ? 'page' : undefined}
        >
          <Icon name={item.icon} size={22} />
          <span class="max-w-full truncate px-0.5">{t(item.short ?? item.label)}</span>
          {#if item.badge}<span class="absolute top-1 right-[calc(50%-20px)] chip bg-amber-500 px-1.5 text-[10px] text-white">{item.badge}</span>{/if}
        </a>
      {/each}
    </nav>
  </div>
{/if}

<Toasts />
<ConfirmHost />
