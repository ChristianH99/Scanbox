<script lang="ts">
  /**
   * Draw / move / resize a relative rectangle on top of an image (pointer events → mouse, touch, pen).
   * Used for the QR search zone and the crop box. Keyboard: arrows move, Shift+arrows resize.
   */
  import type { Snippet } from 'svelte';
  import { isValidRect, moveRect, normalizeRect, rectFromPoints, rectStyle, resizeRect, toRelative, type Handle, type Rect } from '../utils/zone';
  import { t } from '../i18n/index.svelte';

  let {
    rect = $bindable<Rect | null>(null),
    src = null,
    aspect = 1 / 1.414,
    label,
    color = 'brand',
    disabled = false,
    onchange,
    overlay,
  }: {
    rect?: Rect | null;
    src?: string | null;
    /** width / height of the placeholder frame when there is no image */
    aspect?: number;
    label: string;
    color?: 'brand' | 'amber';
    disabled?: boolean;
    onchange?: (r: Rect | null) => void;
    overlay?: Snippet;
  } = $props();

  let box: HTMLDivElement | undefined = $state();
  let op = $state<{ kind: 'draw' | 'move' | Handle; start: { x: number; y: number }; orig: Rect | null; id: number } | null>(null);

  function rel(e: PointerEvent) {
    return toRelative(e.clientX, e.clientY, box!.getBoundingClientRect());
  }

  function down(e: PointerEvent, kind: 'draw' | 'move' | Handle) {
    if (disabled || !box || e.button > 0) return;
    e.preventDefault();
    e.stopPropagation();
    box.setPointerCapture(e.pointerId);
    op = { kind, start: rel(e), orig: rect ? { ...rect } : null, id: e.pointerId };
  }

  function move(e: PointerEvent) {
    if (!op || e.pointerId !== op.id) return;
    const p = rel(e);
    if (op.kind === 'draw') rect = rectFromPoints(op.start, p);
    else if (op.kind === 'move' && op.orig) rect = moveRect(op.orig, p.x - op.start.x, p.y - op.start.y);
    else if (op.orig) rect = resizeRect(op.orig, op.kind as Handle, p);
  }

  function up() {
    if (!op) return;
    if (op.kind === 'draw' && !isValidRect(rect)) rect = op.orig; // a tap doesn't erase the zone
    op = null;
    onchange?.(rect);
  }

  function key(e: KeyboardEvent) {
    if (disabled || !rect) return;
    const step = e.altKey ? 0.002 : 0.01;
    const dx = e.key === 'ArrowLeft' ? -step : e.key === 'ArrowRight' ? step : 0;
    const dy = e.key === 'ArrowUp' ? -step : e.key === 'ArrowDown' ? step : 0;
    if (!dx && !dy) return;
    e.preventDefault();
    rect = e.shiftKey ? normalizeRect({ ...rect, w: Math.max(0.02, rect.w + dx), h: Math.max(0.02, rect.h + dy) }) : moveRect(rect, dx, dy);
    onchange?.(rect);
  }

  const handles: Handle[] = ['nw', 'ne', 'sw', 'se'];
  const handlePos: Record<Handle, string> = {
    nw: 'left-0 top-0 -translate-x-1/2 -translate-y-1/2 cursor-nwse-resize',
    ne: 'right-0 top-0 translate-x-1/2 -translate-y-1/2 cursor-nesw-resize',
    sw: 'left-0 bottom-0 -translate-x-1/2 translate-y-1/2 cursor-nesw-resize',
    se: 'right-0 bottom-0 translate-x-1/2 translate-y-1/2 cursor-nwse-resize',
  };
  let col = $derived(color === 'amber' ? 'border-amber-500 bg-amber-400/20' : 'border-brand-600 bg-brand-500/20');
</script>

<div
  bind:this={box}
  class="relative w-full touch-none overflow-hidden rounded-md border border-slate-300 bg-white select-none dark:border-slate-600 {disabled ? '' : 'cursor-crosshair'}"
  style={src ? '' : `aspect-ratio:${aspect}`}
  role="application"
  aria-label={label}
  onpointerdown={(e) => down(e, 'draw')}
  onpointermove={move}
  onpointerup={up}
  onpointercancel={up}
>
  {#if src}
    <img {src} alt="" class="pointer-events-none block w-full" draggable="false" />
  {:else}
    <div class="pointer-events-none absolute inset-0 flex flex-col gap-2 p-[8%] opacity-40">
      {#each Array(12) as _, i}<div class="h-1.5 rounded bg-slate-300 dark:bg-slate-500" style="width:{60 + ((i * 37) % 40)}%"></div>{/each}
    </div>
  {/if}
  {#if overlay}{@render overlay()}{/if}
  {#if rect}
    <div
      class="absolute border-2 {col} {disabled ? '' : 'cursor-move'} focus-visible:outline-2"
      style={rectStyle(rect)}
      role="slider"
      tabindex={disabled ? -1 : 0}
      aria-label={t('zone.rect_aria', { x: Math.round(rect.x * 100), y: Math.round(rect.y * 100), w: Math.round(rect.w * 100), h: Math.round(rect.h * 100) })}
      aria-valuenow={Math.round(rect.x * 100)}
      onpointerdown={(e) => down(e, 'move')}
      onkeydown={key}
    >
      {#if !disabled}
        {#each handles as h}
          <span
            class="absolute size-6 rounded-full border-2 border-white bg-brand-700 shadow {handlePos[h]}"
            role="presentation"
            onpointerdown={(e) => down(e, h)}
          ></span>
        {/each}
      {/if}
    </div>
  {/if}
</div>
