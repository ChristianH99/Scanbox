<script lang="ts">
  import type { Snippet } from 'svelte';
  import Icon from './Icon.svelte';
  import { t } from '../i18n/index.svelte';

  let {
    open = $bindable(false),
    title,
    size = 'md',
    onclose,
    children,
    footer,
  }: {
    open?: boolean;
    title: string;
    size?: 'sm' | 'md' | 'lg' | 'full';
    onclose?: () => void;
    children: Snippet;
    footer?: Snippet;
  } = $props();

  let dlg: HTMLDialogElement | undefined = $state();

  $effect(() => {
    if (!dlg) return;
    if (open && !dlg.open) dlg.showModal();
    else if (!open && dlg.open) dlg.close();
  });

  function close() {
    open = false;
    onclose?.();
  }

  const sizes = {
    sm: 'max-w-md',
    md: 'max-w-2xl',
    lg: 'max-w-5xl',
    full: 'max-w-none sm:max-w-[min(1400px,96vw)] h-[100dvh] sm:h-[94dvh]',
  };
</script>

<dialog
  bind:this={dlg}
  class="m-auto w-full {sizes[size]} max-h-[100dvh] rounded-none bg-transparent p-0 backdrop:bg-slate-950/60 sm:rounded-xl"
  oncancel={(e) => {
    e.preventDefault();
    close();
  }}
  aria-labelledby="modal-title"
>
  {#if open}
    <div class="flex max-h-[100dvh] flex-col bg-white text-slate-900 shadow-xl sm:max-h-[94dvh] sm:rounded-xl dark:bg-slate-900 dark:text-slate-100 {size === 'full' ? 'h-full' : ''}">
      <header class="flex items-center gap-2 border-b border-slate-200 px-4 py-2 dark:border-slate-800">
        <h2 id="modal-title" class="min-w-0 flex-1 truncate text-base font-semibold">{title}</h2>
        <button type="button" class="btn btn-ghost btn-icon" onclick={close} aria-label={t('common.close')}>
          <Icon name="x" />
        </button>
      </header>
      <div class="min-h-0 flex-1 overflow-auto p-4">
        {@render children()}
      </div>
      {#if footer}
        <footer class="flex flex-wrap items-center justify-end gap-2 border-t border-slate-200 px-4 py-3 dark:border-slate-800">
          {@render footer()}
        </footer>
      {/if}
    </div>
  {/if}
</dialog>
