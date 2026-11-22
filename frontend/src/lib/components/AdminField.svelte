<script lang="ts">
  /** One admin setting: shows locked (env) values read-only, secrets write-only ("set"/"not set"). */
  import Icon from './Icon.svelte';
  import type { AdminSettingValue } from '../api/types';
  import { t } from '../i18n/index.svelte';

  let {
    key,
    label,
    settings,
    draft = $bindable(),
    clear = $bindable(),
    kind = 'text',
    options = [],
    hint,
    placeholder,
  }: {
    key: string;
    label: string;
    settings: Record<string, AdminSettingValue>;
    draft: Record<string, unknown>;
    clear: Set<string>;
    kind?: 'text' | 'number' | 'bool' | 'select' | 'secret';
    options?: { value: string; label: string }[];
    hint?: string;
    placeholder?: string;
  } = $props();

  let s = $derived(settings[key]);
  let locked = $derived(!!s?.locked);
  let id = $derived(`adm-${key.replace(/\./g, '-')}`);
  let val = $derived(draft[key] ?? (kind === 'secret' ? '' : (s?.value ?? '')));

  function set(v: unknown) {
    draft = { ...draft, [key]: v };
  }
</script>

<div class={kind === 'bool' ? 'flex items-start gap-3' : ''}>
  {#if kind === 'bool'}
    <input {id} type="checkbox" class="checkbox mt-0.5" checked={Boolean(draft[key] ?? s?.value)} disabled={locked} onchange={(e) => set((e.target as HTMLInputElement).checked)} />
    <div>
      <label class="text-sm font-medium" for={id}>{label}</label>
      {#if hint}<p class="hint">{hint}</p>{/if}
      {#if locked}<p class="hint flex items-center gap-1"><Icon name="lock" size={12} />{t('admin.locked')}</p>{/if}
    </div>
  {:else}
    <label class="label flex items-center gap-2" for={id}>
      {label}
      {#if locked}<span class="chip gap-1 bg-slate-200 text-slate-700 dark:bg-slate-700 dark:text-slate-200" title={t('admin.locked')}><Icon name="lock" size={12} />ENV</span>{/if}
      {#if kind === 'secret'}
        <span class="chip {s?.is_set && !clear.has(key) ? 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/50 dark:text-emerald-200' : 'bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300'}">
          {s?.is_set && !clear.has(key) ? t('admin.secret_set') : t('admin.secret_not_set')}
        </span>
      {/if}
    </label>
    {#if kind === 'select'}
      <select {id} class="input" value={String(val)} disabled={locked} onchange={(e) => { const v = (e.target as HTMLSelectElement).value; set(v === '' ? null : v); }}>
        {#each options as o}<option value={o.value}>{o.label}</option>{/each}
      </select>
    {:else if kind === 'secret'}
      <div class="flex gap-2">
        <input {id} class="input" type="password" autocomplete="new-password" value={String(val)} disabled={locked} placeholder={s?.is_set ? '••••••••' : placeholder} oninput={(e) => set((e.target as HTMLInputElement).value)} />
        {#if s?.is_set && !locked}
          <button type="button" class="btn btn-ghost" onclick={() => {
            const c = new Set(clear);
            if (c.has(key)) c.delete(key);
            else c.add(key);
            clear = c;
          }}>{clear.has(key) ? t('common.undo') : t('admin.secret_clear')}</button>
        {/if}
      </div>
    {:else}
      <input {id} class="input" type={kind === 'number' ? 'number' : 'text'} value={val === null ? '' : String(val)} disabled={locked} {placeholder}
        oninput={(e) => {
          const v = (e.target as HTMLInputElement).value;
          set(kind === 'number' ? (v === '' ? null : Number(v)) : v === '' ? null : v);
        }} />
    {/if}
    {#if hint}<p class="hint">{hint}</p>{/if}
    {#if locked}<p class="hint">{t('admin.locked')}</p>{/if}
  {/if}
</div>
