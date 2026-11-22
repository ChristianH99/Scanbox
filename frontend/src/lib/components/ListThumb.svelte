<script lang="ts">
  /** First-page thumbnail for list rows (Inbox, History). Falls back to an icon when originals expired. */
  import Icon from './Icon.svelte';
  import { t } from '../i18n/index.svelte';

  let { pageId, updatedAt = '' }: { pageId: string | null | undefined; updatedAt?: string } = $props();
  let failed = $state(false);
</script>

<div class="flex h-16 w-12 shrink-0 items-center justify-center overflow-hidden rounded border border-slate-200 bg-slate-100 dark:border-slate-700 dark:bg-slate-800">
  {#if pageId && !failed}
    <img
      src="/api/pages/{encodeURIComponent(pageId)}/image?variant=processed&size=thumb&v={encodeURIComponent(updatedAt)}"
      alt={t('page.alt', { n: 1 })}
      loading="lazy"
      decoding="async"
      class="h-full w-full object-contain"
      onerror={() => (failed = true)}
    />
  {:else}
    <Icon name="file" class="text-slate-400" />
  {/if}
</div>
