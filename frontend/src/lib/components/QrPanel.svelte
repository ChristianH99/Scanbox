<script lang="ts">
  import { untrack } from 'svelte';
  import Icon from './Icon.svelte';
  import RectEditor from './RectEditor.svelte';
  import type { DocumentDetail, QrDetect, QrHit } from '../api/types';
  import { api, errorMessage } from '../api/client';
  import { docApi, orderedPages } from '../docs';
  import { notify } from '../stores/toast.svelte';
  import { t } from '../i18n/index.svelte';
  import { rectStyle, ZONE_PRESETS, type Rect } from '../utils/zone';

  let { doc, editable, onchange }: { doc: DocumentDetail; editable: boolean; onchange: (d: DocumentDetail) => void } = $props();

  let value = $state(untrack(() => doc.qr_value ?? ''));
  let lastDocValue = untrack(() => doc.qr_value);
  $effect(() => {
    if (doc.qr_value !== lastDocValue) {
      lastDocValue = doc.qr_value;
      value = doc.qr_value ?? '';
    }
  });

  let result = $state<QrDetect | null>(null);
  let detecting = $state(false);
  let detectError = $state<string | null>(null);
  let imageUrl = $state<string | null>(null);
  let testZone = $state<Rect | null>(null);
  let saving = $state(false);

  let qr = $derived(doc.profile_snapshot.qr);
  let profileZone = $derived<Rect>(qr.zone_preset === 'custom' && qr.zone ? qr.zone : ZONE_PRESETS[qr.zone_preset === 'custom' ? 'full' : qr.zone_preset]);
  let hits = $derived((result?.hits ?? []) as unknown as QrHit[]);

  async function detect(zone?: Rect | null) {
    detecting = true;
    detectError = null;
    try {
      result = await api.post<QrDetect>(`/api/documents/${doc.id}/qr/detect`, { zone: zone ?? null, page_id: doc.qr_page_id ?? null });
      const page = doc.pages.find((p) => p.id === result!.page_id) ?? orderedPages(doc)[0];
      if (page) {
        // Hits are relative to the oriented + deskewed, uncropped page → render exactly that.
        const blob = await docApi.preview(page.id, { ...(page.edits ?? {}), crop: null, auto_crop: false }, 'preview');
        if (imageUrl) URL.revokeObjectURL(imageUrl);
        imageUrl = URL.createObjectURL(blob);
      }
      if (!testZone) testZone = { ...result.zone };
    } catch (e) {
      detectError = errorMessage(e);
    } finally {
      detecting = false;
    }
  }

  $effect(() => () => {
    if (imageUrl) URL.revokeObjectURL(imageUrl);
  });

  async function patch(body: Record<string, unknown>, ok?: string) {
    saving = true;
    try {
      onchange(await docApi.patch(doc.id, body));
      if (ok) notify.success(ok);
    } catch (e) {
      notify.error(e);
    } finally {
      saving = false;
    }
  }

  const sourceLabel = (s: string | null) =>
    s === 'manual' ? t('qr.source_manual') : s === 'none' ? t('qr.source_none') : s ? t('qr.source_auto') : t('qr.source_unset');
</script>

<section class="card space-y-3 p-4" id="qr" data-testid="qr-panel">
  <div class="flex items-center gap-2">
    <Icon name="qr" />
    <h2 class="section-title flex-1">{t('qr.title')}</h2>
    {#if !qr.enabled}<span class="chip bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300">{t('qr.profile_off')}</span>{/if}
  </div>

  <p class="text-sm">
    {#if doc.qr_value}
      {t('qr.current')}: <strong class="font-mono" data-testid="qr-value">{doc.qr_value}</strong>
    {:else}
      {t('qr.no_value')}
    {/if}
    <span class="text-xs text-slate-500">({sourceLabel(doc.qr_source)})</span>
  </p>

  {#if editable}
    <form
      class="flex flex-wrap gap-2"
      onsubmit={(e) => {
        e.preventDefault();
        void patch({ qr_value: value.trim() }, t('qr.saved'));
      }}
    >
      <label class="sr-only" for="qrval">{t('qr.value')}</label>
      <input id="qrval" class="input min-w-0 flex-1 font-mono" bind:value maxlength="500" placeholder={t('qr.value_placeholder')} />
      <button type="submit" class="btn btn-primary" disabled={saving}>{t('common.save')}</button>
    </form>
    <div class="flex flex-wrap gap-2 text-sm">
      <button type="button" class="btn btn-ghost btn-sm" disabled={saving} onclick={() => patch({ qr_value: '' }, t('qr.saved'))}>{t('qr.set_none')}</button>
      {#if doc.qr_source === 'manual' || doc.qr_source === 'none'}
        <button type="button" class="btn btn-ghost btn-sm" disabled={saving} onclick={() => patch({ clear_manual_qr: true })}>{t('qr.reset_auto')}</button>
      {/if}
    </div>
    <label class="flex min-h-11 items-center gap-3">
      <input type="checkbox" class="checkbox" checked={doc.add_qr_to_filename} disabled={saving} onchange={(e) => patch({ add_qr_to_filename: (e.target as HTMLInputElement).checked })} />
      <span class="text-sm">{t('scan.add_qr')}</span>
    </label>
    {#if doc.status === 'done'}<p class="hint">{t('qr.refinish_hint')}</p>{/if}
  {/if}

  <div class="border-t border-slate-200 pt-3 dark:border-slate-800">
    <button type="button" class="btn btn-secondary btn-sm" disabled={detecting || doc.raw_expired || !doc.page_count} onclick={() => detect(null)} data-testid="qr-detect">
      {#if detecting}<Icon name="loader" size={16} />{:else}<Icon name="search" size={16} />{/if}{t('qr.detect')}
    </button>
    {#if detectError}<p class="mt-2 text-sm text-red-700 dark:text-red-300" role="alert">{detectError}</p>{/if}

    {#if result}
      <div class="mt-3 grid gap-3">
        <div class="mx-auto w-full max-w-sm">
          <RectEditor bind:rect={testZone} src={imageUrl} label={t('qr.zone_test')} color="amber" disabled={!imageUrl}>
            {#snippet overlay()}
              <div class="pointer-events-none absolute border-2 border-dashed border-sky-500" style={rectStyle(result!.zone)} title={t('qr.zone')}></div>
              {#each hits as h, i (i)}
                <div class="pointer-events-none absolute border-[3px] {h.value === result!.selected ? 'border-emerald-500 bg-emerald-400/25' : 'border-red-500 bg-red-400/15'}" style={rectStyle(h)}></div>
              {/each}
            {/snippet}
          </RectEditor>
          <p class="hint">{t('qr.overlay_legend')}</p>
        </div>
        <div class="space-y-2 text-sm">
          {#if hits.length === 0}
            <p class="text-amber-800 dark:text-amber-200">{t('qr.no_hits')}</p>
          {:else}
            <p>{t('qr.hits_n', { n: hits.length })}</p>
            <ul class="space-y-1">
              {#each hits as h, i (i)}
                <li class="flex items-center gap-2 rounded border border-slate-200 p-2 dark:border-slate-700">
                  <span class="size-3 rounded-full {h.value === result.selected ? 'bg-emerald-500' : 'bg-red-500'}"></span>
                  <code class="min-w-0 flex-1 break-words font-semibold">{h.value}</code>
                  {#if editable}<button type="button" class="btn btn-ghost btn-sm" onclick={() => (value = h.value)}>{t('qr.use')}</button>{/if}
                </li>
              {/each}
            </ul>
          {/if}
          <p class="text-xs text-slate-500">{t('qr.zone_profile', { zone: qr.zone_preset })}</p>
          <div class="flex flex-wrap gap-2">
            <button type="button" class="btn btn-secondary btn-sm" disabled={detecting || !testZone} onclick={() => detect(testZone)}>{t('qr.detect_in_zone')}</button>
            <button type="button" class="btn btn-ghost btn-sm" onclick={() => (testZone = { ...profileZone })}>{t('qr.zone_reset')}</button>
          </div>
          <p class="hint">{t('qr.zone_test_hint')}</p>
        </div>
      </div>
    {/if}
  </div>
</section>
