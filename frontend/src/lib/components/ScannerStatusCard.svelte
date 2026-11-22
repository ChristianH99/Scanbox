<script lang="ts">
  import Icon from './Icon.svelte';
  import type { ScannerStatus } from '../api/types';
  import { fmtTime, t } from '../i18n/index.svelte';
  import { TONE_BORDER, TONE_CLASSES, scannerView } from '../utils/status';

  let {
    status,
    error = null,
    isAdmin = false,
    compact = false,
  }: { status: ScannerStatus | null; error?: string | null; isAdmin?: boolean; compact?: boolean } = $props();

  let view = $derived(scannerView(status));
</script>

<section
  class="card flex items-start gap-3 border-2 p-4 {TONE_BORDER[view.tone]}"
  aria-live="polite"
  data-testid="scanner-status"
  data-state={status?.state ?? 'unknown'}
>
  <div class="flex size-12 shrink-0 items-center justify-center rounded-full {TONE_CLASSES[view.tone]}">
    <Icon name={view.icon} size={26} />
  </div>
  <div class="min-w-0 flex-1">
    <h2 class="text-lg leading-tight font-semibold">{t(view.title)}</h2>
    {#if view.detail && !compact}
      <p class="mt-0.5 text-sm text-slate-600 dark:text-slate-300">{t(view.detail)}</p>
    {/if}
    {#if status?.message && view.tone !== 'ok'}
      <p class="mt-1 text-xs break-words text-slate-500 dark:text-slate-400">{status.message}</p>
    {/if}
    {#if error}
      <p class="mt-1 text-xs text-red-700 dark:text-red-300">{t('scanner.status_unavailable')}: {error}</p>
    {/if}
    <p class="mt-1 flex flex-wrap gap-x-3 text-xs text-slate-500 dark:text-slate-400">
      {#if status?.model}<span>{status.model}</span>{/if}
      {#if status?.checked_at}<span>{t('scanner.checked_at', { time: fmtTime(status.checked_at) })}</span>{/if}
      {#if status?.active_document_id}
        <a class="underline" href="/documents/{status.active_document_id}">{t('scanner.active_job')}</a>
      {/if}
    </p>
    {#if view.title === 'scanner.unconfigured' && isAdmin}
      <a href="/admin/scanner" class="btn btn-secondary btn-sm mt-2"><Icon name="settings" size={16} />{t('scanner.configure')}</a>
    {/if}
  </div>
</section>
