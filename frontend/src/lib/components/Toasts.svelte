<script lang="ts">
  import Icon from './Icon.svelte';
  import { dismissToast, toasts } from '../stores/toast.svelte';
  import { t } from '../i18n/index.svelte';

  const styles = {
    success: ['check-circle', 'border-emerald-300 bg-emerald-50 text-emerald-900 dark:border-emerald-800 dark:bg-emerald-950 dark:text-emerald-100'],
    error: ['x-circle', 'border-red-300 bg-red-50 text-red-900 dark:border-red-800 dark:bg-red-950 dark:text-red-100'],
    warning: ['alert', 'border-amber-300 bg-amber-50 text-amber-900 dark:border-amber-700 dark:bg-amber-950 dark:text-amber-100'],
    info: ['info', 'border-sky-300 bg-sky-50 text-sky-900 dark:border-sky-800 dark:bg-sky-950 dark:text-sky-100'],
  } as const;
</script>

<div class="pointer-events-none fixed inset-x-0 bottom-20 z-50 flex flex-col items-center gap-2 px-4 sm:bottom-4 sm:items-end" aria-live="polite">
  {#each toasts as toast (toast.id)}
    <div
      class="pointer-events-auto flex w-full max-w-md items-start gap-3 rounded-lg border p-3 shadow-lg {styles[toast.kind][1]}"
      role={toast.kind === 'error' ? 'alert' : 'status'}
    >
      <Icon name={styles[toast.kind][0]} class="mt-0.5" />
      <div class="min-w-0 flex-1 text-sm">
        {#if toast.title}<p class="font-semibold">{toast.title}</p>{/if}
        <p class="break-words">{toast.message}</p>
      </div>
      <button type="button" class="-m-1 rounded p-1 hover:bg-black/10" onclick={() => dismissToast(toast.id)} aria-label={t('common.close')}>
        <Icon name="x" size={16} />
      </button>
    </div>
  {/each}
</div>
