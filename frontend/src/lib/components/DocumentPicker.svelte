<script lang="ts">
  import Modal from './Modal.svelte';
  import Icon from './Icon.svelte';
  import PageThumb from './PageThumb.svelte';
  import StatusBadge from './StatusBadge.svelte';
  import type { DocumentDetail, DocumentList, DocumentSummary } from '../api/types';
  import { api, errorMessage } from '../api/client';
  import { docApi, orderedPages } from '../docs';
  import { fmtDateTime, t } from '../i18n/index.svelte';
  import { debounce } from '../utils/misc';

  let {
    open = $bindable(false),
    mode,
    excludeId,
    onconfirm,
  }: {
    open?: boolean;
    mode: 'insert' | 'merge';
    excludeId: number;
    onconfirm: (r: { docIds: number[]; pageIds: string[]; copy: boolean }) => Promise<void>;
  } = $props();

  let q = $state('');
  let items = $state<DocumentSummary[]>([]);
  let loading = $state(false);
  let error = $state<string | null>(null);
  let chosen = $state<Set<number>>(new Set());
  let source = $state<DocumentDetail | null>(null);
  let pageSel = $state<Set<string>>(new Set());
  let copy = $state(true);
  let working = $state(false);

  const EDITABLE = ['stored', 'needs_attention', 'failed', 'done'];

  async function load() {
    loading = true;
    try {
      const r = await api.get<DocumentList>('/api/documents', { query: { scope: 'history', q: q || undefined, limit: 50, status: EDITABLE } });
      items = r.items.filter((d) => d.id !== excludeId && !d.raw_expired);
      error = null;
    } catch (e) {
      error = errorMessage(e);
    } finally {
      loading = false;
    }
  }
  const search = debounce(load, 300);

  $effect(() => {
    if (open) {
      chosen = new Set();
      source = null;
      pageSel = new Set();
      void load();
    }
  });

  async function pick(d: DocumentSummary) {
    if (mode === 'merge') {
      const s = new Set(chosen);
      if (s.has(d.id)) s.delete(d.id);
      else s.add(d.id);
      chosen = s;
      return;
    }
    try {
      source = await docApi.get(d.id);
      pageSel = new Set(source.pages.filter((p) => !p.deleted).map((p) => p.id));
    } catch (e) {
      error = errorMessage(e);
    }
  }

  async function confirm() {
    working = true;
    try {
      if (mode === 'merge') await onconfirm({ docIds: [...chosen], pageIds: [], copy: false });
      else if (source) await onconfirm({ docIds: [source.id], pageIds: orderedPages(source).filter((p) => pageSel.has(p.id)).map((p) => p.id), copy });
      open = false;
    } catch (e) {
      error = errorMessage(e);
    } finally {
      working = false;
    }
  }
</script>

<Modal bind:open title={mode === 'merge' ? t('picker.merge_title') : t('picker.insert_title')} size="lg">
  {#if error}<p class="mb-3 rounded bg-red-50 p-2 text-sm text-red-800 dark:bg-red-950 dark:text-red-200" role="alert">{error}</p>{/if}
  {#if mode === 'insert' && source}
    <button type="button" class="btn btn-ghost btn-sm mb-2" onclick={() => (source = null)}><Icon name="arrow-left" size={16} />{t('picker.back')}</button>
    <p class="mb-2 text-sm">{t('picker.choose_pages', { title: source.title || `#${source.id}` })}</p>
    <ul class="grid grid-cols-3 gap-2 sm:grid-cols-5">
      {#each orderedPages(source) as p, i (p.id)}
        <li>
          <label class="block cursor-pointer rounded-md p-1 {pageSel.has(p.id) ? 'bg-brand-100 ring-2 ring-brand-600 dark:bg-brand-800/40' : ''}">
            <input type="checkbox" class="sr-only" checked={pageSel.has(p.id)} onchange={() => {
              const s = new Set(pageSel);
              if (s.has(p.id)) s.delete(p.id);
              else s.add(p.id);
              pageSel = s;
            }} />
            <PageThumb page={p} index={i} />
          </label>
        </li>
      {/each}
    </ul>
    <fieldset class="mt-3 space-y-1 text-sm">
      <label class="flex items-center gap-2"><input type="radio" name="copy" checked={copy} onchange={() => (copy = true)} />{t('picker.copy')}</label>
      <label class="flex items-center gap-2"><input type="radio" name="copy" checked={!copy} onchange={() => (copy = false)} />{t('picker.move')}</label>
    </fieldset>
  {:else}
    <div class="relative mb-3">
      <Icon name="search" size={18} class="absolute top-3 left-3 text-slate-400" />
      <input class="input pl-10" type="search" placeholder={t('history.search')} bind:value={q} oninput={() => search()} aria-label={t('history.search')} />
    </div>
    {#if mode === 'merge'}<p class="mb-2 text-sm text-slate-600 dark:text-slate-400">{t('picker.merge_hint')}</p>{/if}
    {#if loading && !items.length}
      <p class="flex items-center gap-2 text-sm"><Icon name="loader" />{t('common.loading')}</p>
    {:else if !items.length}
      <p class="text-sm text-slate-500">{t('picker.empty')}</p>
    {/if}
    <ul class="divide-y divide-slate-200 dark:divide-slate-800">
      {#each items as d (d.id)}
        <li>
          <button type="button" class="flex min-h-14 w-full items-center gap-3 px-2 py-2 text-left hover:bg-slate-50 dark:hover:bg-slate-800 {chosen.has(d.id) ? 'bg-brand-50 dark:bg-brand-800/30' : ''}" onclick={() => pick(d)}>
            {#if mode === 'merge'}<Icon name={chosen.has(d.id) ? 'check-square' : 'square'} />{/if}
            <span class="min-w-0 flex-1">
              <span class="block truncate font-medium">{d.title || d.filename || `#${d.id}`}</span>
              <span class="block text-xs text-slate-500">{fmtDateTime(d.created_at)} · {t('doc.pages_count', { n: d.page_count })}</span>
            </span>
            <StatusBadge status={d.status} />
            {#if mode === 'insert'}<Icon name="chevron-right" />{/if}
          </button>
        </li>
      {/each}
    </ul>
  {/if}

  {#snippet footer()}
    <button type="button" class="btn btn-secondary" onclick={() => (open = false)}>{t('common.cancel')}</button>
    {#if mode === 'merge'}
      <button type="button" class="btn btn-primary" disabled={!chosen.size || working} onclick={confirm}>
        {#if working}<Icon name="loader" />{/if}{t('picker.merge_n', { n: chosen.size })}
      </button>
    {:else}
      <button type="button" class="btn btn-primary" disabled={!source || !pageSel.size || working} onclick={confirm}>
        {#if working}<Icon name="loader" />{/if}{t('picker.insert_n', { n: pageSel.size })}
      </button>
    {/if}
  {/snippet}
</Modal>
