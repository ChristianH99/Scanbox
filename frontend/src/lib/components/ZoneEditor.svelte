<script lang="ts">
  /** QR search zone editor: presets + free rectangle on a sample page (relative coords). */
  import RectEditor from './RectEditor.svelte';
  import { presetFor, ZONE_PRESETS, type PresetName, type Rect } from '../utils/zone';
  import { t } from '../i18n/index.svelte';

  let {
    preset = $bindable<'top_right' | 'top_left' | 'full' | 'custom'>('top_right'),
    zone = $bindable<Rect | null>(null),
    sampleUrl = null,
    disabled = false,
  }: {
    preset?: 'top_right' | 'top_left' | 'full' | 'custom';
    zone?: Rect | null;
    sampleUrl?: string | null;
    disabled?: boolean;
  } = $props();

  /** The rectangle currently in effect (what the backend will use). */
  let effective = $derived<Rect>(preset === 'custom' && zone ? zone : ZONE_PRESETS[preset === 'custom' ? 'full' : preset]);

  function choose(p: PresetName) {
    preset = p;
    zone = null;
  }

  function changed(r: Rect | null) {
    if (!r) return;
    const p = presetFor(r);
    if (p === 'custom') {
      preset = 'custom';
      zone = r;
    } else choose(p);
  }

  const presets: PresetName[] = ['top_right', 'top_left', 'full'];
</script>

<div class="space-y-2" data-testid="zone-editor">
  <div class="flex flex-wrap gap-1" role="group" aria-label={t('zone.presets')}>
    {#each presets as p}
      <button type="button" class="btn btn-sm {preset === p ? 'btn-primary' : 'btn-secondary'}" aria-pressed={preset === p} {disabled} onclick={() => choose(p)}>{t(`zone.${p}`)}</button>
    {/each}
    <span class="btn btn-sm pointer-events-none {preset === 'custom' ? 'btn-primary' : 'btn-ghost'}">{t('zone.custom')}</span>
  </div>
  <div class="max-w-xs">
    <RectEditor rect={effective} src={sampleUrl} label={t('zone.editor_label')} {disabled} onchange={changed} />
  </div>
  <p class="hint">
    {t('zone.hint')}
    <span class="font-mono">x={effective.x.toFixed(2)} y={effective.y.toFixed(2)} w={effective.w.toFixed(2)} h={effective.h.toFixed(2)}</span>
  </p>
  {#if !sampleUrl}<p class="hint">{t('zone.no_sample')}</p>{/if}
</div>
