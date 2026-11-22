<script lang="ts">
  import { untrack } from 'svelte';
  import Modal from './Modal.svelte';
  import Icon from './Icon.svelte';
  import RectEditor from './RectEditor.svelte';
  import type { Page, PageEdits, ProfileSettings } from '../api/types';
  import { docApi, pageImageUrl, rotated } from '../docs';
  import { notify } from '../stores/toast.svelte';
  import { confirmDialog } from '../stores/dialog.svelte';
  import { errorMessage } from '../api/client';
  import { t } from '../i18n/index.svelte';
  import { clone, debounce, sameJson } from '../utils/misc';
  import type { Rect } from '../utils/zone';

  let {
    open = $bindable(false),
    page,
    index,
    total,
    profile,
    editable,
    onsaved,
    onnavigate,
  }: {
    open?: boolean;
    page: Page;
    index: number;
    total: number;
    profile: ProfileSettings;
    editable: boolean;
    onsaved: (p: Page) => void;
    onnavigate: (delta: -1 | 1) => void;
  } = $props();

  let edits = $state<PageEdits>(untrack(() => clone(page.edits ?? {})));
  let saved = $state<PageEdits>(untrack(() => clone(page.edits ?? {})));
  let mode = $state<'after' | 'before' | 'split'>('after');
  let cropMode = $state(false);
  let previewUrl = $state<string | null>(null);
  let cropBaseUrl = $state<string | null>(null);
  let previewLoading = $state(false);
  let previewError = $state<string | null>(null);
  let saving = $state(false);
  let abort: AbortController | null = null;

  // Reset when another page is shown.
  let lastId = '';
  $effect(() => {
    if (page.id !== lastId) {
      lastId = page.id;
      edits = clone(page.edits ?? {});
      saved = clone(page.edits ?? {});
      cropMode = false;
    }
  });

  let dirty = $derived(!sameJson(stripNull(edits), stripNull(saved)));
  function stripNull(e: PageEdits) {
    return Object.fromEntries(Object.entries(e).filter(([, v]) => v !== null && v !== undefined));
  }

  const autoRotate = $derived(Number((page.analysis as Record<string, unknown>)?.osd_rotate ?? 0));
  const autoAngle = $derived(() => {
    const a = page.analysis as Record<string, unknown>;
    return Number(a?.[`skew_${edits.rotate ?? autoRotate}`] ?? 0);
  });

  async function renderPreview(e: PageEdits, target: 'main' | 'crop') {
    abort?.abort();
    const ctl = new AbortController();
    abort = ctl;
    previewLoading = true;
    previewError = null;
    try {
      const blob = await docApi.preview(page.id, stripNull(e) as PageEdits, 'preview');
      if (ctl.signal.aborted) return;
      const url = URL.createObjectURL(blob);
      if (target === 'main') {
        if (previewUrl) URL.revokeObjectURL(previewUrl);
        previewUrl = url;
      } else {
        if (cropBaseUrl) URL.revokeObjectURL(cropBaseUrl);
        cropBaseUrl = url;
      }
    } catch (err) {
      if (!ctl.signal.aborted) previewError = errorMessage(err);
    } finally {
      if (abort === ctl) previewLoading = false;
    }
  }
  const schedule = debounce((e: PageEdits, target: 'main' | 'crop') => void renderPreview(e, target), 350);

  $effect(() => {
    if (!open) return;
    const snapshot = clone(edits);
    if (cropMode) {
      // Crop is drawn on the oriented + deskewed, uncropped page.
      schedule({ ...snapshot, crop: null, auto_crop: false }, 'crop');
    } else schedule(snapshot, 'main');
  });

  $effect(() => () => {
    schedule.cancel();
    abort?.abort();
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    if (cropBaseUrl) URL.revokeObjectURL(cropBaseUrl);
  });

  async function save(): Promise<boolean> {
    saving = true;
    try {
      const p = await docApi.patchPage(page.id, { edits: stripNull(edits) as PageEdits });
      saved = clone(edits);
      onsaved(p);
      notify.success(t('editor.saved'));
      return true;
    } catch (e) {
      notify.error(e);
      return false;
    } finally {
      saving = false;
    }
  }

  async function guard(): Promise<boolean> {
    if (!dirty) return true;
    return confirmDialog({ title: t('editor.unsaved_title'), message: t('editor.unsaved_text'), confirmLabel: t('editor.discard_changes'), danger: true });
  }

  async function close() {
    if (await guard()) open = false;
    else open = true;
  }

  async function nav(delta: -1 | 1) {
    if (await guard()) onnavigate(delta);
  }

  async function toggleFlag(flag: 'deleted' | 'keep_blank') {
    try {
      const p = await docApi.patchPage(page.id, { [flag]: !page[flag] });
      onsaved(p);
    } catch (e) {
      notify.error(e);
    }
  }

  function setCrop(r: Rect | null) {
    edits = { ...edits, crop: r };
  }

  // Tri-state helpers: null = profile default
  function triValue(v: boolean | null | undefined): string {
    return v === null || v === undefined ? 'default' : v ? 'on' : 'off';
  }
  function fromTri(v: string): boolean | null {
    return v === 'default' ? null : v === 'on';
  }

  let effColor = $derived(edits.color ?? (profile.processing.color !== 'keep' ? profile.processing.color : profile.color_mode === 'bw' ? 'bw' : 'keep'));
  let rawUrl = $derived(pageImageUrl(page, 'preview', 'raw'));
</script>

<Modal bind:open title={t('editor.page_title', { n: index + 1, total })} size="full" onclose={() => void close()}>
  <div class="grid h-full gap-4 lg:grid-cols-[minmax(0,1fr)_320px]">
    <!-- Image area -->
    <div class="flex min-h-0 flex-col gap-2">
      <div class="flex flex-wrap items-center gap-2">
        <div class="inline-flex rounded-lg border border-slate-300 p-0.5 dark:border-slate-600" role="group" aria-label={t('editor.view')}>
          {#each ['after', 'before', 'split'] as const as m}
            <button type="button" class="btn btn-sm {mode === m && !cropMode ? 'btn-primary' : 'btn-ghost'}" aria-pressed={mode === m} onclick={() => {
              mode = m;
              cropMode = false;
            }}>{t(`editor.view_${m}`)}</button>
          {/each}
        </div>
        {#if editable}
          <button type="button" class="btn btn-sm {cropMode ? 'btn-primary' : 'btn-secondary'}" aria-pressed={cropMode} onclick={() => (cropMode = !cropMode)}>
            <Icon name="crop" size={16} />{t('editor.crop_draw')}
          </button>
        {/if}
        {#if previewLoading}<Icon name="loader" size={18} label={t('common.loading')} />{/if}
        <span class="flex-1"></span>
        <button type="button" class="btn btn-ghost btn-icon" disabled={index === 0} onclick={() => nav(-1)} aria-label={t('editor.prev_page')}><Icon name="chevron-left" /></button>
        <button type="button" class="btn btn-ghost btn-icon" disabled={index >= total - 1} onclick={() => nav(1)} aria-label={t('editor.next_page')}><Icon name="chevron-right" /></button>
      </div>
      {#if previewError}
        <p class="rounded bg-red-50 p-2 text-sm text-red-800 dark:bg-red-950 dark:text-red-200" role="alert">{t('editor.preview_failed')}: {previewError}</p>
      {/if}

      <div class="min-h-0 flex-1 overflow-auto rounded-lg bg-slate-200 p-2 dark:bg-slate-800">
        {#if cropMode}
          <div class="mx-auto max-w-2xl">
            <p class="mb-2 text-sm">{t('editor.crop_hint')}</p>
            {#if cropBaseUrl}
              <RectEditor rect={edits.crop ?? null} src={cropBaseUrl} label={t('editor.crop')} onchange={setCrop} />
            {:else}
              <div class="flex h-64 items-center justify-center"><Icon name="loader" /></div>
            {/if}
          </div>
        {:else if mode === 'split'}
          <div class="grid grid-cols-2 gap-2">
            <figure><figcaption class="mb-1 text-xs font-medium">{t('editor.view_before')}</figcaption><img src={rawUrl} alt={t('editor.view_before')} class="w-full bg-white" /></figure>
            <figure><figcaption class="mb-1 text-xs font-medium">{t('editor.view_after')}</figcaption>
              {#if previewUrl}<img src={previewUrl} alt={t('editor.view_after')} class="w-full bg-white" />{/if}
            </figure>
          </div>
        {:else if mode === 'before'}
          <img src={rawUrl} alt={t('editor.view_before')} class="mx-auto max-h-[75dvh] bg-white object-contain" />
        {:else if previewUrl}
          <img src={previewUrl} alt={t('editor.view_after')} class="mx-auto max-h-[75dvh] bg-white object-contain" data-testid="editor-preview" />
        {:else}
          <div class="flex h-64 items-center justify-center"><Icon name="loader" /></div>
        {/if}
      </div>
    </div>

    <!-- Controls -->
    <div class="space-y-4 overflow-auto">
      {#if !editable}
        <p class="rounded bg-slate-100 p-2 text-sm dark:bg-slate-800">{t('editor.readonly')}</p>
      {/if}
      <fieldset disabled={!editable} class="space-y-4">
        <div>
          <span class="label">{t('editor.rotation')}</span>
          <div class="flex gap-2">
            <button type="button" class="btn btn-secondary flex-1" onclick={() => (edits = rotated(edits, -90, autoRotate))}><Icon name="rotate-ccw" />90°</button>
            <button type="button" class="btn btn-secondary flex-1" onclick={() => (edits = rotated(edits, 90, autoRotate))}><Icon name="rotate-cw" />90°</button>
          </div>
          <p class="hint">{edits.rotate === null || edits.rotate === undefined ? t('editor.rotation_auto', { deg: autoRotate }) : t('editor.rotation_manual', { deg: edits.rotate })}
            {#if edits.rotate !== null && edits.rotate !== undefined}<button type="button" class="underline" onclick={() => (edits = { ...edits, rotate: null })}>{t('editor.use_auto')}</button>{/if}
          </p>
        </div>

        <div>
          <label class="label flex items-center justify-between" for="angle">
            <span>{t('editor.fine_rotation')}</span>
            <span class="text-xs font-normal">{edits.angle === null || edits.angle === undefined ? t('editor.deskew_auto', { deg: autoAngle().toFixed(1) }) : `${edits.angle.toFixed(1)}°`}</span>
          </label>
          <input id="angle" type="range" min="-15" max="15" step="0.1" class="w-full accent-brand-700" value={edits.angle ?? autoAngle()} oninput={(e) => (edits = { ...edits, angle: Number((e.target as HTMLInputElement).value) })} />
          <div class="flex gap-2 text-xs">
            <button type="button" class="underline" onclick={() => (edits = { ...edits, angle: null })}>{t('editor.deskew_use_auto')}</button>
            <button type="button" class="underline" onclick={() => (edits = { ...edits, angle: 0 })}>{t('editor.deskew_off')}</button>
          </div>
        </div>

        <div>
          <span class="label">{t('editor.crop')}</span>
          {#if edits.crop}
            <p class="text-sm">{t('editor.crop_manual')} <button type="button" class="underline" onclick={() => setCrop(null)}>{t('editor.crop_remove')}</button></p>
          {:else}
            <label class="label text-xs font-normal" for="autocrop">{t('editor.auto_crop')}</label>
            <select id="autocrop" class="input" value={triValue(edits.auto_crop)} onchange={(e) => (edits = { ...edits, auto_crop: fromTri((e.target as HTMLSelectElement).value) })}>
              <option value="default">{t('editor.profile_default', { v: profile.processing.auto_crop ? t('common.on') : t('common.off') })}</option>
              <option value="on">{t('common.on')}</option>
              <option value="off">{t('common.off')}</option>
            </select>
          {/if}
        </div>

        <div>
          <label class="label" for="color">{t('editor.color')}</label>
          <select id="color" class="input" value={edits.color ?? 'default'} onchange={(e) => {
            const v = (e.target as HTMLSelectElement).value;
            edits = { ...edits, color: v === 'default' ? null : (v as PageEdits['color']) };
          }}>
            <option value="default">{t('editor.profile_default', { v: t(`pcolor.${effColor}` as 'pcolor.keep') })}</option>
            <option value="keep">{t('pcolor.keep')}</option>
            <option value="color">{t('pcolor.color')}</option>
            <option value="gray">{t('pcolor.gray')}</option>
            <option value="bw">{t('pcolor.bw')}</option>
          </select>
        </div>

        {#each [['brightness', 'editor.brightness'], ['contrast', 'editor.contrast']] as const as [k, lbl]}
          <div>
            <label class="label flex justify-between" for={k}><span>{t(lbl)}</span><span class="text-xs font-normal">{edits[k] ?? t('editor.default_short')}</span></label>
            <input id={k} type="range" min="-1" max="1" step="0.05" class="w-full accent-brand-700" value={edits[k] ?? profile.processing[k]} oninput={(e) => (edits = { ...edits, [k]: Number((e.target as HTMLInputElement).value) })} />
            {#if edits[k] !== null && edits[k] !== undefined}<button type="button" class="text-xs underline" onclick={() => (edits = { ...edits, [k]: null })}>{t('editor.reset')}</button>{/if}
          </div>
        {/each}

        {#if effColor === 'bw'}
          <div>
            <label class="label flex justify-between" for="thr"><span>{t('editor.threshold')}</span><span class="text-xs font-normal">{edits.bw_threshold ?? t('editor.threshold_auto')}</span></label>
            <input id="thr" type="range" min="0" max="255" step="1" class="w-full accent-brand-700" value={edits.bw_threshold ?? profile.processing.bw_threshold ?? 128} oninput={(e) => (edits = { ...edits, bw_threshold: Number((e.target as HTMLInputElement).value) })} />
            {#if edits.bw_threshold !== null && edits.bw_threshold !== undefined}<button type="button" class="text-xs underline" onclick={() => (edits = { ...edits, bw_threshold: null })}>{t('editor.threshold_auto')}</button>{/if}
          </div>
        {/if}

        {#each [['denoise', 'editor.denoise'], ['sharpen', 'editor.sharpen']] as const as [k, lbl]}
          <div>
            <label class="label" for={k}>{t(lbl)}</label>
            <select id={k} class="input" value={triValue(edits[k])} onchange={(e) => (edits = { ...edits, [k]: fromTri((e.target as HTMLSelectElement).value) })}>
              <option value="default">{t('editor.profile_default', { v: profile.processing[k] ? t('common.on') : t('common.off') })}</option>
              <option value="on">{t('common.on')}</option>
              <option value="off">{t('common.off')}</option>
            </select>
          </div>
        {/each}
      </fieldset>

      {#if editable}
        <div class="space-y-2 border-t border-slate-200 pt-3 dark:border-slate-800">
          {#if page.is_blank}
            <label class="flex min-h-11 items-center gap-3">
              <input type="checkbox" class="checkbox" checked={page.keep_blank} onchange={() => toggleFlag('keep_blank')} />
              <span class="text-sm">{t('editor.keep_blank')}</span>
            </label>
          {/if}
          <button type="button" class="btn btn-secondary w-full" onclick={() => toggleFlag('deleted')}>
            <Icon name={page.deleted ? 'undo' : 'trash'} />{page.deleted ? t('editor.restore_page') : t('editor.delete_page')}
          </button>
        </div>
      {/if}
    </div>
  </div>

  {#snippet footer()}
    {#if editable}
      <button type="button" class="btn btn-ghost" onclick={() => (edits = {})}>{t('editor.reset_all')}</button>
      <span class="flex-1"></span>
      {#if dirty}<span class="text-sm text-amber-700 dark:text-amber-300">{t('editor.unsaved')}</span>{/if}
      <button type="button" class="btn btn-secondary" onclick={() => void close()}>{t('common.close')}</button>
      <button type="button" class="btn btn-primary" disabled={!dirty || saving} onclick={save} data-testid="editor-save">
        {#if saving}<Icon name="loader" />{/if}{t('common.save')}
      </button>
    {:else}
      <button type="button" class="btn btn-secondary" onclick={() => (open = false)}>{t('common.close')}</button>
    {/if}
  {/snippet}
</Modal>
