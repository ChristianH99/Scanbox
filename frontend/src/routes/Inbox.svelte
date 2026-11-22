<script lang="ts">
  import Icon from '../lib/components/Icon.svelte';
  import StatusBadge from '../lib/components/StatusBadge.svelte';
  import ListThumb from '../lib/components/ListThumb.svelte';
  import { api, errorMessage, isApiError } from '../lib/api/client';
  import type { DocumentList, DocumentSummary } from '../lib/api/types';
  import { docApi } from '../lib/docs';
  import { onLive } from '../lib/events.svelte';
  import { navigate } from '../lib/router.svelte';
  import { notify } from '../lib/stores/toast.svelte';
  import { fmtDateTime, fmtRelative, t } from '../lib/i18n/index.svelte';
  import { debounce } from '../lib/utils/misc';

  let items = $state<DocumentSummary[]>([]);
  let loading = $state(true);
  let error = $state<string | null>(null);
  let claiming = $state<number | null>(null);

  async function load() {
    try {
      const r = await api.get<DocumentList>('/api/documents', { query: { scope: 'inbox', limit: 100 } });
      items = r.items;
      error = null;
    } catch (e) {
      error = errorMessage(e);
    } finally {
      loading = false;
    }
  }

  $effect(() => {
    void load();
    return onLive(['inbox.changed', 'reconnect'], debounce(load, 400));
  });

  async function claim(d: DocumentSummary) {
    claiming = d.id;
    try {
      await docApi.claim(d.id);
      notify.success(t('inbox.claimed'));
      await navigate(`/documents/${d.id}`);
    } catch (e) {
      if (isApiError(e, 'already_claimed') || (isApiError(e) && e.status === 404)) {
        notify.warning(t('inbox.already_claimed'));
        void load();
      } else notify.error(e);
    } finally {
      claiming = null;
    }
  }
</script>

<div class="space-y-4">
  <div>
    <h1 class="text-xl font-semibold">{t('inbox.title')}</h1>
    <p class="text-sm text-slate-600 dark:text-slate-400">{t('inbox.subtitle')}</p>
  </div>

  {#if error}
    <p class="rounded-lg bg-red-50 p-3 text-sm text-red-800 dark:bg-red-950 dark:text-red-200" role="alert">{error} <button type="button" class="underline" onclick={load}>{t('common.retry')}</button></p>
  {/if}

  {#if loading}
    <p class="flex items-center gap-2"><Icon name="loader" />{t('common.loading')}</p>
  {:else if !items.length}
    <div class="card flex flex-col items-center gap-2 p-8 text-center text-slate-500">
      <Icon name="inbox" size={40} />
      <p>{t('inbox.empty')}</p>
    </div>
  {:else}
    <ul class="space-y-2" data-testid="inbox-list">
      {#each items as d (d.id)}
        <li class="card flex flex-wrap items-center gap-3 p-3">
          <a href="/documents/{d.id}" aria-hidden="true" tabindex="-1"><ListThumb pageId={d.first_page_id} updatedAt={d.updated_at} /></a>
          <div class="min-w-0 flex-1">
            <a href="/documents/{d.id}" class="block truncate font-medium hover:underline">{d.title || t('inbox.walkup_scan')} #{d.id}</a>
            <p class="text-xs text-slate-500">
              {fmtDateTime(d.created_at)} ({fmtRelative(d.created_at)}) · {t('doc.pages_count', { n: d.page_count })}
              {#if d.inbox_expires_at} · {t('inbox.expires', { date: fmtDateTime(d.inbox_expires_at) })}{/if}
            </p>
          </div>
          <StatusBadge status={d.status} />
          <button type="button" class="btn btn-primary" disabled={claiming !== null} onclick={() => claim(d)}>
            {#if claiming === d.id}<Icon name="loader" />{:else}<Icon name="check" />{/if}{t('inbox.claim')}
          </button>
        </li>
      {/each}
    </ul>
  {/if}
</div>
