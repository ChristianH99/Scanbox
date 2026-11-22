<script lang="ts">
  import { untrack } from 'svelte';
  import Icon from './Icon.svelte';
  import StatusBadge from './StatusBadge.svelte';
  import PageThumb from './PageThumb.svelte';
  import DocumentAlerts from './DocumentAlerts.svelte';
  import { LiveDocument } from '../liveDocument.svelte';
  import { docApi, downloadUrl, orderedPages } from '../docs';
  import { notify } from '../stores/toast.svelte';
  import { confirmDialog } from '../stores/dialog.svelte';
  import { t } from '../i18n/index.svelte';
  import type { DocumentDetail } from '../api/types';

  let { id, ondismiss }: { id: number; ondismiss: () => void } = $props();

  const live = new LiveDocument(untrack(() => id));
  $effect(() => live.start());

  let busy = $state<string | null>(null);
  let doc = $derived(live.doc);
  let pages = $derived(doc ? orderedPages(doc) : []);

  async function act(name: string, fn: () => Promise<DocumentDetail>) {
    busy = name;
    try {
      live.set(await fn());
    } catch (e) {
      notify.error(e);
    } finally {
      busy = null;
    }
  }

  async function discard() {
    const ok = await confirmDialog({ title: t('doc.discard_confirm_title'), message: t('doc.discard_confirm_text'), confirmLabel: t('doc.discard'), danger: true });
    if (!ok) return;
    await act('discard', () => docApi.discard(id));
    ondismiss();
  }

  let lastBatch = $derived(doc ? [...doc.batches].sort((a, b) => b.seq - a.seq)[0] : null);
  let deliveriesDone = $derived(doc ? doc.deliveries.filter((d) => d.status === 'done').length : 0);
</script>

<section class="card overflow-hidden" data-testid="active-document" data-status={doc?.status ?? ''} data-doc-id={id}>
  {#if !doc}
    <div class="flex items-center gap-2 p-4 text-sm">
      {#if live.error}
        <Icon name="alert" class="text-red-600" />{live.error}
        <button type="button" class="btn btn-secondary btn-sm ml-auto" onclick={() => live.load()}>{t('common.retry')}</button>
        <button type="button" class="btn btn-ghost btn-sm" onclick={ondismiss}>{t('common.close')}</button>
      {:else}
        <Icon name="loader" />{t('common.loading')}
      {/if}
    </div>
  {:else}
    <header class="flex flex-wrap items-center gap-2 border-b border-slate-200 px-4 py-3 dark:border-slate-800">
      <div class="min-w-0 flex-1">
        <a href="/documents/{doc.id}" class="block truncate font-semibold hover:underline">{doc.title || doc.profile_name || `#${doc.id}`}</a>
        <p class="text-xs text-slate-500 dark:text-slate-400">{doc.profile_name} · {t('doc.pages_count', { n: doc.page_count })}</p>
      </div>
      <StatusBadge status={doc.status} />
      {#if ['done', 'discarded', 'expired'].includes(doc.status)}
        <button type="button" class="btn btn-ghost btn-icon" onclick={ondismiss} aria-label={t('common.close')}><Icon name="x" /></button>
      {/if}
    </header>

    <div class="space-y-4 p-4">
      <!-- Progress line -->
      {#if doc.status === 'queued'}
        <p class="flex items-center gap-2 text-sm" role="status">
          <Icon name="clock" />
          {doc.queue_position ? t('scan.queued_position', { n: doc.queue_position }) : t('scan.queued')}
        </p>
      {:else if doc.status === 'scanning'}
        <p class="flex items-center gap-2 text-sm font-medium text-sky-800 dark:text-sky-200" role="status">
          <Icon name="loader" />{t('scan.scanning', { n: doc.page_count })}
          {#if lastBatch && lastBatch.seq > 1}<span class="text-xs opacity-75">({t('scan.stack_n', { n: lastBatch.seq })})</span>{/if}
        </p>
      {:else if doc.status === 'processing'}
        <div role="status">
          <p class="flex items-center gap-2 text-sm font-medium"><Icon name="loader" />
            {live.progress ? t('scan.processing_progress', { done: live.progress.done, total: live.progress.total }) : t('scan.processing')}
          </p>
          {#if live.progress}
            <div class="mt-2 h-2 overflow-hidden rounded bg-slate-200 dark:bg-slate-700">
              <div class="h-full bg-brand-600 transition-all" style="width:{(100 * live.progress.done) / Math.max(1, live.progress.total)}%"></div>
            </div>
          {/if}
        </div>
      {:else if doc.status === 'delivering'}
        <p class="flex items-center gap-2 text-sm font-medium" role="status"><Icon name="send" />{t('scan.delivering')}</p>
      {:else if doc.status === 'done'}
        <div class="flex items-center gap-2 rounded-lg bg-emerald-50 p-3 text-sm text-emerald-900 dark:bg-emerald-950 dark:text-emerald-100" role="status">
          <Icon name="check-circle" />
          <span class="flex-1">{t('scan.done', { file: doc.filename ?? '' })}
            {#if doc.deliveries.length}<br /><span class="text-xs">{t('scan.delivered_to', { n: deliveriesDone, total: doc.deliveries.length })}</span>{/if}
          </span>
        </div>
      {:else if doc.status === 'stored'}
        <p class="text-sm text-slate-700 dark:text-slate-300">{t('scan.stored_hint')}</p>
      {/if}

      <DocumentAlerts {doc} onchange={(d) => live.set(d)} compact={false} />

      <!-- Live thumbnails -->
      {#if pages.length}
        <ul class="grid grid-cols-3 gap-2 sm:grid-cols-5 lg:grid-cols-6" aria-label={t('doc.pages')} data-testid="live-thumbs">
          {#each pages as p, i (p.id)}
            <li><a href="/documents/{doc.id}" aria-label={t('page.alt', { n: i + 1 })}><PageThumb page={p} index={i} fresh={live.fresh.has(p.id)} /></a></li>
          {/each}
          {#if doc.status === 'scanning'}
            <li class="flex aspect-[1/1.414] items-center justify-center rounded-md border-2 border-dashed border-sky-300 text-sky-600 dark:border-sky-800">
              <Icon name="loader" />
            </li>
          {/if}
        </ul>
      {/if}

      <!-- Actions -->
      <div class="flex flex-wrap gap-2">
        {#if doc.status === 'stored'}
          <button type="button" class="btn btn-primary btn-lg grow sm:grow-0" disabled={!!busy} onclick={() => act('finish', () => docApi.finish(id))} data-testid="finish">
            {#if busy === 'finish'}<Icon name="loader" />{:else}<Icon name="check" />{/if}{t('scan.finish')}
          </button>
          <button type="button" class="btn btn-secondary btn-lg" disabled={!!busy} onclick={() => act('batch', () => docApi.addBatch(id))} data-testid="add-stack">
            {#if busy === 'batch'}<Icon name="loader" />{:else}<Icon name="plus" />{/if}{t('scan.add_stack')}
          </button>
          <a href="/documents/{doc.id}" class="btn btn-secondary btn-lg"><Icon name="edit" />{t('scan.review')}</a>
          <button type="button" class="btn btn-ghost btn-lg" disabled={!!busy} onclick={discard}><Icon name="trash" />{t('doc.discard')}</button>
        {:else if doc.status === 'queued' || doc.status === 'scanning'}
          <button type="button" class="btn btn-secondary btn-lg" disabled={!!busy} onclick={() => act('cancel', () => docApi.cancel(id))} data-testid="stop-scan">
            {#if busy === 'cancel'}<Icon name="loader" />{:else}<Icon name="x-circle" />{/if}{t('scan.stop')}
          </button>
        {:else if doc.status === 'done'}
          <a href={downloadUrl(doc.id)} class="btn btn-primary" download data-testid="download"><Icon name="download" />{t('doc.download')}</a>
          <a href="/documents/{doc.id}" class="btn btn-secondary"><Icon name="file" />{t('scan.open_document')}</a>
          <button type="button" class="btn btn-ghost" onclick={ondismiss}><Icon name="scan" />{t('scan.new_scan')}</button>
        {/if}
      </div>
    </div>
  {/if}
</section>
