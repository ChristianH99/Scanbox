<script lang="ts">
  import Icon from '../lib/components/Icon.svelte';
  import StatusBadge from '../lib/components/StatusBadge.svelte';
  import { api, errorMessage } from '../lib/api/client';
  import type { DocumentList, DocumentSummary, Profile } from '../lib/api/types';
  import { docApi, downloadUrl } from '../lib/docs';
  import { onLive } from '../lib/events.svelte';
  import { router, setQuery } from '../lib/router.svelte';
  import { notify } from '../lib/stores/toast.svelte';
  import { fmtDateTime, t, tMaybe } from '../lib/i18n/index.svelte';
  import { debounce } from '../lib/utils/misc';

  const PAGE = 30;
  const STATUSES = ['done', 'needs_attention', 'failed', 'stored', 'processing', 'delivering', 'scanning', 'queued', 'expired'];
  const DESTS = ['download', 'folder', 'email', 'paperless'];

  let items = $state<DocumentSummary[]>([]);
  let total = $state(0);
  let loading = $state(false);
  let error = $state<string | null>(null);
  let profiles = $state<Profile[]>([]);
  let showFilters = $state(false);
  let sentinel: HTMLDivElement | undefined = $state();

  // Filters live in the URL so they survive reloads and back navigation.
  const qp = router.query;
  let q = $state(qp.get('q') ?? '');
  let status = $state(qp.get('status') ?? '');
  let profileId = $state(qp.get('profile_id') ?? '');
  let destination = $state(qp.get('destination') ?? '');
  let dateFrom = $state(qp.get('date_from') ?? '');
  let dateTo = $state(qp.get('date_to') ?? '');
  let activeFilters = $derived([status, profileId, destination, dateFrom, dateTo].filter(Boolean).length);

  let seq = 0;
  async function load(reset: boolean) {
    const my = ++seq;
    loading = true;
    try {
      const r = await api.get<DocumentList>('/api/documents', {
        query: {
          scope: 'history',
          q: q.trim() || undefined,
          status: status || undefined,
          profile_id: profileId || undefined,
          destination: destination || undefined,
          date_from: dateFrom || undefined,
          date_to: dateTo || undefined,
          limit: PAGE,
          offset: reset ? 0 : items.length,
        },
      });
      if (my !== seq) return;
      items = reset ? r.items : [...items, ...r.items.filter((x) => !items.some((y) => y.id === x.id))];
      total = r.total;
      error = null;
    } catch (e) {
      if (my === seq) error = errorMessage(e);
    } finally {
      if (my === seq) loading = false;
    }
  }

  function applyFilters() {
    setQuery({ q, status, profile_id: profileId, destination, date_from: dateFrom, date_to: dateTo });
    void load(true);
  }
  const searchDebounced = debounce(applyFilters, 350);

  function resetFilters() {
    status = profileId = destination = dateFrom = dateTo = '';
    applyFilters();
  }

  $effect(() => {
    void load(true);
    api.get<Profile[]>('/api/profiles').then((p) => (profiles = p)).catch(() => {});
    // Refresh the visible page on document changes (debounced).
    const refresh = debounce(async () => {
      try {
        const r = await api.get<DocumentList>('/api/documents', {
          query: { scope: 'history', q: q.trim() || undefined, status: status || undefined, profile_id: profileId || undefined, destination: destination || undefined, date_from: dateFrom || undefined, date_to: dateTo || undefined, limit: Math.max(PAGE, items.length), offset: 0 },
        });
        items = r.items;
        total = r.total;
      } catch {
        /* keep current list */
      }
    }, 800);
    return onLive(['document.updated', 'document.created', 'reconnect'], refresh);
  });

  // Infinite scroll
  $effect(() => {
    if (!sentinel || typeof IntersectionObserver === 'undefined') return;
    const io = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting) && !loading && items.length < total) void load(false);
    }, { rootMargin: '300px' });
    io.observe(sentinel);
    return () => io.disconnect();
  });

  async function resend(d: DocumentSummary) {
    try {
      await docApi.resend(d.id);
      notify.success(t('doc.resend_started'));
    } catch (e) {
      notify.error(e);
    }
  }
</script>

<div class="space-y-4">
  <h1 class="text-xl font-semibold">{t('history.title')}</h1>

  <div class="card space-y-3 p-3">
    <div class="flex gap-2">
      <div class="relative flex-1">
        <Icon name="search" size={18} class="absolute top-3 left-3 text-slate-400" />
        <input class="input pl-10" type="search" placeholder={t('history.search')} aria-label={t('history.search')} bind:value={q} oninput={() => searchDebounced()} data-testid="history-search" />
      </div>
      <button type="button" class="btn btn-secondary" aria-expanded={showFilters} onclick={() => (showFilters = !showFilters)}>
        <Icon name="filter" size={18} /><span class="hidden sm:inline">{t('history.filters')}</span>
        {#if activeFilters}<span class="chip bg-brand-700 text-white">{activeFilters}</span>{/if}
      </button>
    </div>
    {#if showFilters}
      <div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
        <div><label class="label" for="f-status">{t('history.status')}</label>
          <select id="f-status" class="input" bind:value={status} onchange={applyFilters}>
            <option value="">{t('common.all')}</option>
            {#each STATUSES as s}<option value={s}>{t(`status.${s}` as 'status.done')}</option>{/each}
          </select></div>
        <div><label class="label" for="f-profile">{t('scan.profile')}</label>
          <select id="f-profile" class="input" bind:value={profileId} onchange={applyFilters}>
            <option value="">{t('common.all')}</option>
            {#each profiles as p (p.id)}<option value={String(p.id)}>{p.name}</option>{/each}
          </select></div>
        <div><label class="label" for="f-dest">{t('history.destination')}</label>
          <select id="f-dest" class="input" bind:value={destination} onchange={applyFilters}>
            <option value="">{t('common.all')}</option>
            {#each DESTS as d}<option value={d}>{tMaybe(`dest.${d}`, d)}</option>{/each}
          </select></div>
        <div><label class="label" for="f-from">{t('history.from')}</label><input id="f-from" type="date" class="input" bind:value={dateFrom} onchange={applyFilters} /></div>
        <div><label class="label" for="f-to">{t('history.to')}</label><input id="f-to" type="date" class="input" bind:value={dateTo} onchange={applyFilters} /></div>
      </div>
      {#if activeFilters}<button type="button" class="btn btn-ghost btn-sm" onclick={resetFilters}>{t('history.reset_filters')}</button>{/if}
    {/if}
  </div>

  {#if error}
    <p class="rounded-lg bg-red-50 p-3 text-sm text-red-800 dark:bg-red-950 dark:text-red-200" role="alert">{error} <button type="button" class="underline" onclick={() => load(true)}>{t('common.retry')}</button></p>
  {/if}

  <p class="text-sm text-slate-500" aria-live="polite">{t('history.count', { n: total })}</p>

  <ul class="space-y-2" data-testid="history-list">
    {#each items as d (d.id)}
      <li class="card flex flex-wrap items-center gap-3 p-3">
        <div class="min-w-0 flex-1 basis-60">
          <a href="/documents/{d.id}" class="block truncate font-medium hover:underline">{d.title || d.filename || `#${d.id}`}</a>
          <p class="text-xs text-slate-500">{fmtDateTime(d.created_at)} · {d.profile_name} · {t('doc.pages_count', { n: d.page_count })}</p>
          {#if d.filename && d.title}<p class="truncate text-xs text-slate-500">{d.filename}</p>{/if}
          <div class="mt-1 flex flex-wrap gap-1">
            {#if d.qr_value}<span class="chip bg-slate-100 font-mono dark:bg-slate-800">QR {d.qr_value}</span>{/if}
            {#each d.destinations as ds}<span class="chip bg-slate-100 dark:bg-slate-800">{tMaybe(`dest.${ds}`, ds)}</span>{/each}
            {#if d.processed_expired}<span class="chip bg-slate-200 text-slate-700 dark:bg-slate-700 dark:text-slate-200" title={t('doc.processed_expired')}>{t('history.files_expired')}</span>{/if}
            {#if d.raw_expired}<span class="chip bg-slate-200 text-slate-700 dark:bg-slate-700 dark:text-slate-200" title={t('doc.raw_expired')}>{t('history.originals_expired')}</span>{/if}
          </div>
          {#if d.error_message && (d.status === 'failed' || d.status === 'needs_attention')}
            <p class="mt-1 text-xs text-red-700 dark:text-red-300">{d.error_message}</p>
          {/if}
        </div>
        <StatusBadge status={d.status} />
        <div class="flex gap-1">
          {#if d.status === 'done' && !d.processed_expired}
            <a class="btn btn-ghost btn-icon" href={downloadUrl(d.id)} download aria-label={t('doc.download')} title={t('doc.download')}><Icon name="download" /></a>
          {/if}
          {#if (d.status === 'done' || d.status === 'needs_attention') && !d.raw_expired}
            <button type="button" class="btn btn-ghost btn-icon" onclick={() => resend(d)} aria-label={t('doc.resend')} title={d.processed_expired ? t('doc.reprocess') : t('doc.resend')}><Icon name="send" /></button>
          {/if}
        </div>
      </li>
    {/each}
  </ul>

  {#if !loading && !items.length && !error}
    <div class="card p-8 text-center text-slate-500">{q || activeFilters ? t('history.no_results') : t('history.empty')}</div>
  {/if}
  <div bind:this={sentinel}></div>
  {#if loading}<p class="flex items-center justify-center gap-2 py-4"><Icon name="loader" />{t('common.loading')}</p>{/if}
  {#if !loading && items.length < total}
    <button type="button" class="btn btn-secondary w-full" onclick={() => load(false)}>{t('history.load_more')}</button>
  {/if}
</div>
