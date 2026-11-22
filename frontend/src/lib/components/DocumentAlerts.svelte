<script lang="ts">
  import Icon from './Icon.svelte';
  import type { DocumentDetail } from '../api/types';
  import { docApi, lastBatchError } from '../docs';
  import { notify } from '../stores/toast.svelte';
  import { confirmDialog } from '../stores/dialog.svelte';
  import { t, tMaybe } from '../i18n/index.svelte';
  import { SCAN_ERROR_CODES } from '../utils/status';

  let {
    doc,
    onchange,
    onqr,
    compact = false,
  }: {
    doc: DocumentDetail;
    onchange: (d: DocumentDetail) => void;
    /** Open the QR panel (document page) – otherwise a link to the document is shown. */
    onqr?: () => void;
    compact?: boolean;
  } = $props();

  let busy = $state<string | null>(null);

  async function run(name: string, fn: () => Promise<DocumentDetail>, success?: string) {
    busy = name;
    try {
      onchange(await fn());
      if (success) notify.success(success);
    } catch (e) {
      notify.error(e);
    } finally {
      busy = null;
    }
  }

  async function discard() {
    const ok = await confirmDialog({
      title: t('doc.discard_confirm_title'),
      message: t('doc.discard_confirm_text'),
      confirmLabel: t('doc.discard'),
      danger: true,
    });
    if (ok) await run('discard', () => docApi.discard(doc.id));
  }

  async function continueWithoutQr() {
    await run('noqr', async () => {
      await docApi.patch(doc.id, { qr_value: '' });
      return docApi.finish(doc.id);
    });
  }

  async function retryFailedDeliveries() {
    busy = 'retry';
    try {
      let d = doc;
      for (const dl of doc.deliveries.filter((x) => x.status === 'needs_attention' || x.status === 'failed')) {
        d = await docApi.retryDelivery(doc.id, dl.id);
      }
      onchange(d);
    } catch (e) {
      notify.error(e);
    } finally {
      busy = null;
    }
  }

  let batchErr = $derived(lastBatchError(doc));
  let code = $derived(doc.error_code ?? (doc.status === 'needs_attention' || doc.status === 'failed' ? batchErr?.error_code ?? null : null));
  let message = $derived(doc.error_message ?? batchErr?.error_message ?? '');
  let isScanError = $derived(!!code && SCAN_ERROR_CODES.has(code));
  let failedDeliveries = $derived(doc.deliveries.filter((x) => x.status === 'needs_attention' || x.status === 'failed'));
  let show = $derived(doc.status === 'needs_attention' || doc.status === 'failed');
  let tone = $derived(doc.status === 'failed' ? 'error' : 'warn');
</script>

{#if show}
  <div
    class="rounded-xl border-2 p-4 {tone === 'error'
      ? 'border-red-300 bg-red-50 text-red-900 dark:border-red-800 dark:bg-red-950/60 dark:text-red-100'
      : 'border-amber-300 bg-amber-50 text-amber-950 dark:border-amber-700 dark:bg-amber-950/60 dark:text-amber-100'}"
    role="alert"
    data-testid="document-alert"
    data-code={code ?? ''}
  >
    <div class="flex gap-3">
      <Icon name={tone === 'error' ? 'x-circle' : 'alert'} size={24} class="mt-0.5" />
      <div class="min-w-0 flex-1 space-y-1">
        <p class="font-semibold">
          {code ? tMaybe(`errors.${code}`, t('errors.generic')) : t('errors.generic')}
          {#if isScanError && doc.page_count > 0}
            – {t('recovery.pages_saved', { n: doc.page_count })}
          {/if}
        </p>
        {#if message}<p class="text-sm break-words opacity-90">{message}</p>{/if}
        {#if code && !compact}
          {@const hint = tMaybe(`recovery.${code}`, '')}
          {#if hint}<p class="text-sm">{hint}</p>{/if}
        {/if}
        {#if failedDeliveries.length && !isScanError && code !== 'qr_missing'}
          <ul class="mt-1 list-disc pl-5 text-sm">
            {#each failedDeliveries as dl (dl.id)}
              <li><strong>{dl.label || dl.destination_type}</strong>: {dl.last_error ?? t('delivery.failed')}</li>
            {/each}
          </ul>
        {/if}
      </div>
    </div>

    <div class="mt-3 flex flex-wrap gap-2">
      {#if isScanError}
        <button type="button" class="btn btn-primary" disabled={!!busy || doc.raw_expired} onclick={() => run('batch', () => docApi.addBatch(doc.id))} data-testid="recovery-rescan">
          {#if busy === 'batch'}<Icon name="loader" />{:else}<Icon name="scan" />{/if}
          {doc.page_count > 0 ? t('recovery.scan_remaining') : t('recovery.try_again')}
        </button>
        {#if doc.page_count > 0}
          <button type="button" class="btn btn-secondary" disabled={!!busy} onclick={() => run('finish', () => docApi.finish(doc.id))} data-testid="recovery-finish">
            {#if busy === 'finish'}<Icon name="loader" />{:else}<Icon name="check" />{/if}
            {t('recovery.finish_with_pages')}
          </button>
        {/if}
      {:else if code === 'qr_missing'}
        {#if onqr}
          <button type="button" class="btn btn-primary" onclick={onqr}><Icon name="qr" />{t('recovery.enter_qr')}</button>
        {:else}
          <a class="btn btn-primary" href="/documents/{doc.id}#qr"><Icon name="qr" />{t('recovery.enter_qr')}</a>
        {/if}
        <button type="button" class="btn btn-secondary" disabled={!!busy} onclick={continueWithoutQr}>
          {#if busy === 'noqr'}<Icon name="loader" />{/if}{t('recovery.continue_without_qr')}
        </button>
      {:else if failedDeliveries.length}
        <button type="button" class="btn btn-primary" disabled={!!busy || doc.processed_expired} onclick={retryFailedDeliveries}>
          {#if busy === 'retry'}<Icon name="loader" />{:else}<Icon name="refresh" />{/if}{t('recovery.retry_deliveries')}
        </button>
        {#if doc.processed_expired}
          <button type="button" class="btn btn-secondary" disabled={!!busy} onclick={() => run('resend', () => docApi.resend(doc.id))}>
            <Icon name="refresh" />{t('doc.reprocess')}
          </button>
        {/if}
      {:else if doc.page_count > 0}
        <button type="button" class="btn btn-primary" disabled={!!busy} onclick={() => run('finish', () => docApi.finish(doc.id))}>
          {#if busy === 'finish'}<Icon name="loader" />{:else}<Icon name="refresh" />{/if}{t('recovery.process_again')}
        </button>
      {:else}
        <button type="button" class="btn btn-primary" disabled={!!busy || doc.raw_expired} onclick={() => run('batch', () => docApi.addBatch(doc.id))}>
          <Icon name="scan" />{t('recovery.try_again')}
        </button>
      {/if}
      <button type="button" class="btn btn-ghost" disabled={!!busy} onclick={discard}><Icon name="trash" />{t('doc.discard')}</button>
    </div>
  </div>
{/if}
