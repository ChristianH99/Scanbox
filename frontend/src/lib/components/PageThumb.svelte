<script lang="ts">
  import Icon from './Icon.svelte';
  import type { Page } from '../api/types';
  import { pageImageUrl } from '../docs';
  import { t } from '../i18n/index.svelte';

  let {
    page,
    index,
    fresh = false,
    variant = 'processed',
  }: { page: Page; index: number; fresh?: boolean; variant?: 'raw' | 'processed' } = $props();

  let failed = $state(false);
  let src = $derived(pageImageUrl(page, 'thumb', variant));
  $effect(() => {
    void src;
    failed = false;
  });
  let dim = $derived(page.deleted || (page.is_blank && !page.keep_blank));
</script>

<div
  class="relative aspect-[1/1.414] overflow-hidden rounded-md border bg-slate-100 dark:bg-slate-800 {fresh
    ? 'border-brand-500 ring-2 ring-brand-500'
    : 'border-slate-300 dark:border-slate-700'}"
>
  {#if failed}
    <div class="flex h-full flex-col items-center justify-center gap-1 p-2 text-center text-xs text-slate-500">
      <Icon name="image" />{t('page.image_unavailable')}
    </div>
  {:else}
    <img
      {src}
      alt={t('page.alt', { n: index + 1 })}
      loading="lazy"
      decoding="async"
      draggable="false"
      class="h-full w-full object-contain transition-opacity {dim ? 'opacity-35 grayscale' : ''}"
      onerror={() => (failed = true)}
    />
  {/if}
  <span class="absolute bottom-1 left-1 rounded bg-slate-900/75 px-1.5 text-xs font-medium text-white">{index + 1}</span>
  {#if page.deleted}
    <span class="absolute top-1 left-1 chip bg-red-600 text-white">{t('page.deleted')}</span>
  {:else if page.is_blank && !page.keep_blank}
    <span class="absolute top-1 left-1 chip bg-slate-700 text-white">{t('page.blank')}</span>
  {:else if page.is_blank && page.keep_blank}
    <span class="absolute top-1 left-1 chip bg-emerald-700 text-white">{t('page.blank_kept')}</span>
  {/if}
  {#if page.side === 'back'}
    <span class="absolute right-1 bottom-1 rounded bg-slate-900/60 px-1 text-[10px] text-white">{t('page.back')}</span>
  {/if}
</div>
