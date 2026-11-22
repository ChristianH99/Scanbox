<script lang="ts">
  /** A scanner option that is either "scanner default" (null) or a value inside the caps range. */
  import type { RangeCaps } from '../api/types';
  import { t } from '../i18n/index.svelte';

  let { value = $bindable<number | null | undefined>(null), range, label, id, disabled = false }: {
    value?: number | null;
    range: RangeCaps;
    label: string;
    id: string;
    disabled?: boolean;
  } = $props();

  let custom = $derived(value !== null && value !== undefined);
</script>

<div>
  <div class="flex items-center justify-between gap-2">
    <label class="label mb-0" for={id}>{label}</label>
    <label class="flex items-center gap-1 text-xs">
      <input type="checkbox" class="checkbox size-4" checked={!custom} {disabled} onchange={(e) => (value = (e.target as HTMLInputElement).checked ? null : range.normal)} />
      {t('profile.scanner_default')}
    </label>
  </div>
  {#if custom}
    <div class="flex items-center gap-2">
      <input {id} type="range" class="flex-1 accent-brand-700" min={range.min} max={range.max} step={range.step || 1} bind:value {disabled} />
      <input type="number" class="input w-24" min={range.min} max={range.max} step={range.step || 1} bind:value {disabled} aria-label={label} />
    </div>
    <p class="hint">{range.min} – {range.max} ({t('profile.normal')}: {range.normal})</p>
  {:else}
    <p class="hint">{t('profile.scanner_default_hint', { v: range.normal })}</p>
  {/if}
</div>
