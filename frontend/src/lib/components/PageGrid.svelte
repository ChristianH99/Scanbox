<script lang="ts">
  import Icon from './Icon.svelte';
  import PageThumb from './PageThumb.svelte';
  import type { Page } from '../api/types';
  import { t } from '../i18n/index.svelte';
  import { moveItems } from '../utils/misc';

  let {
    pages,
    selected = $bindable(new Set<string>()),
    editable = true,
    fresh = new Set<string>(),
    onreorder,
    onopen,
  }: {
    pages: Page[];
    selected?: Set<string>;
    editable?: boolean;
    fresh?: Set<string>;
    onreorder: (ids: string[]) => void;
    onopen: (page: Page, index: number) => void;
  } = $props();

  let gridEl: HTMLUListElement | undefined = $state();
  let drag = $state<{ ids: string[]; x: number; y: number; pointerId: number; moved: boolean } | null>(null);
  let dropIndex = $state<number | null>(null);
  let scrollTimer: ReturnType<typeof setInterval> | null = null;
  let liveMsg = $state('');

  function toggle(id: string) {
    const s = new Set(selected);
    if (s.has(id)) s.delete(id);
    else s.add(id);
    selected = s;
  }

  function onHandleDown(e: PointerEvent, page: Page) {
    if (!editable || e.button > 0) return;
    e.preventDefault();
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    const ids = selected.has(page.id) ? pages.filter((p) => selected.has(p.id)).map((p) => p.id) : [page.id];
    drag = { ids, x: e.clientX, y: e.clientY, pointerId: e.pointerId, moved: false };
    dropIndex = null;
  }

  function indexAt(x: number, y: number): number | null {
    if (!gridEl) return null;
    const items = Array.from(gridEl.querySelectorAll<HTMLElement>('[data-page-index]'));
    let best: { i: number; d: number; after: boolean } | null = null;
    for (const el of items) {
      const r = el.getBoundingClientRect();
      const cx = r.left + r.width / 2;
      const cy = r.top + r.height / 2;
      const d = Math.hypot(x - cx, (y - cy) * 1.5);
      if (!best || d < best.d) best = { i: Number(el.dataset.pageIndex), d, after: x > cx };
    }
    return best ? best.i + (best.after ? 1 : 0) : null;
  }

  function onHandleMove(e: PointerEvent) {
    if (!drag || e.pointerId !== drag.pointerId) return;
    const moved = drag.moved || Math.hypot(e.clientX - drag.x, e.clientY - drag.y) > 6;
    drag = { ...drag, x: e.clientX, y: e.clientY, moved };
    if (moved) dropIndex = indexAt(e.clientX, e.clientY);
    // auto-scroll near the viewport edges
    const edge = 60;
    const dir = e.clientY < edge ? -1 : e.clientY > window.innerHeight - edge ? 1 : 0;
    if (dir && !scrollTimer) scrollTimer = setInterval(() => window.scrollBy(0, dir * 12), 16);
    if (!dir && scrollTimer) {
      clearInterval(scrollTimer);
      scrollTimer = null;
    }
  }

  function endDrag(commit: boolean) {
    if (scrollTimer) clearInterval(scrollTimer);
    scrollTimer = null;
    if (drag && commit && drag.moved && dropIndex !== null) {
      const ids = pages.map((p) => p.id);
      const next = moveItems(ids, drag.ids, dropIndex);
      if (next.join() !== ids.join()) {
        onreorder(next);
        liveMsg = t('editor.moved_n', { n: drag.ids.length });
      }
    }
    drag = null;
    dropIndex = null;
  }

  /** Keyboard alternative: arrow keys on the handle move the page (or the selection). */
  function onHandleKey(e: KeyboardEvent, page: Page, index: number) {
    if (!editable) return;
    const delta = e.key === 'ArrowLeft' || e.key === 'ArrowUp' ? -1 : e.key === 'ArrowRight' || e.key === 'ArrowDown' ? 1 : 0;
    if (!delta) return;
    e.preventDefault();
    const ids = pages.map((p) => p.id);
    const target = delta < 0 ? Math.max(0, index - 1) : Math.min(ids.length, index + 2);
    const next = moveItems(ids, [page.id], target);
    if (next.join() !== ids.join()) {
      onreorder(next);
      liveMsg = t('editor.moved_to', { n: next.indexOf(page.id) + 1 });
      requestAnimationFrame(() => gridEl?.querySelector<HTMLElement>(`[data-handle="${page.id}"]`)?.focus());
    }
  }
</script>

<p class="sr-only" aria-live="assertive">{liveMsg}</p>
<ul bind:this={gridEl} class="grid grid-cols-2 gap-3 min-[480px]:grid-cols-3 sm:grid-cols-4 lg:grid-cols-6" aria-label={t('doc.pages')} data-testid="page-grid">
  {#each pages as page, i (page.id)}
    {@const isSel = selected.has(page.id)}
    {@const isDragging = !!drag?.moved && drag.ids.includes(page.id)}
    <li
      class="relative rounded-lg p-1 {isSel ? 'bg-brand-100 ring-2 ring-brand-600 dark:bg-brand-800/40' : ''} {isDragging ? 'opacity-40' : ''}"
      data-page-index={i}
      data-page-id={page.id}
    >
      {#if dropIndex === i}
        <span class="absolute inset-y-0 -left-2 w-1 rounded bg-brand-600" aria-hidden="true"></span>
      {/if}
      {#if dropIndex === pages.length && i === pages.length - 1}
        <span class="absolute inset-y-0 -right-2 w-1 rounded bg-brand-600" aria-hidden="true"></span>
      {/if}
      <button type="button" class="block w-full cursor-zoom-in" onclick={() => onopen(page, i)} aria-label={t('editor.open_page', { n: i + 1 })}>
        <PageThumb {page} index={i} fresh={fresh.has(page.id)} />
      </button>
      {#if editable}
        <label class="absolute top-1 right-1 flex size-11 cursor-pointer items-center justify-center" title={t('editor.select')}>
          <input type="checkbox" class="checkbox size-6 bg-white" checked={isSel} onchange={() => toggle(page.id)} aria-label={t('editor.select_page', { n: i + 1 })} />
        </label>
        <button
          type="button"
          class="absolute right-1 bottom-1 flex size-11 cursor-grab touch-none items-center justify-center rounded-md bg-white/90 text-slate-700 shadow active:cursor-grabbing dark:bg-slate-800/90 dark:text-slate-200"
          data-handle={page.id}
          aria-label={t('editor.drag_handle', { n: i + 1 })}
          title={t('editor.drag_hint')}
          onpointerdown={(e) => onHandleDown(e, page)}
          onpointermove={onHandleMove}
          onpointerup={() => endDrag(true)}
          onpointercancel={() => endDrag(false)}
          onkeydown={(e) => onHandleKey(e, page, i)}
        >
          <Icon name="grip" />
        </button>
      {/if}
    </li>
  {/each}
</ul>

{#if drag?.moved}
  <div
    class="pointer-events-none fixed z-50 -translate-x-1/2 -translate-y-1/2 rounded-lg bg-brand-700 px-3 py-2 text-sm font-semibold text-white shadow-xl"
    style="left:{drag.x}px;top:{drag.y}px"
  >
    {t('editor.dragging_n', { n: drag.ids.length })}
  </div>
{/if}
