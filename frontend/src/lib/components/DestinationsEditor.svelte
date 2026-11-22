<script lang="ts">
  import Icon from './Icon.svelte';
  import type { Contact, DestinationConfig, UserSettings } from '../api/types';
  import { t } from '../i18n/index.svelte';

  type Dest = DestinationConfig;
  let {
    destinations = $bindable<Dest[]>([]),
    settings,
    contacts,
    disabled = false,
  }: { destinations?: Dest[]; settings: UserSettings | null; contacts: Contact[]; disabled?: boolean } = $props();

  const TYPES = ['download', 'folder', 'email', 'paperless'] as const;
  const icons = { download: 'download', folder: 'folder', email: 'mail', paperless: 'file' } as const;
  let addType = $state<(typeof TYPES)[number]>('folder');
  let newRecipient = $state<Record<number, string>>({});

  function defaults(type: Dest['type']): Record<string, unknown> {
    switch (type) {
      case 'folder':
        return { root: settings?.output_roots?.[0] ?? '', subfolder: '', include_text: false };
      case 'email':
        return { recipients: [], use_default_recipients: true, subject_template: null, body_template: null, attach_text: false, oversize: 'compress_then_split' };
      case 'paperless':
        return { mode: 'api', title_template: '{title}', tags: [], create_missing_tags: false, correspondent: null, document_type: null, set_created_date: true, qr_mapping: 'asn', qr_custom_field: null, asn_fallback: 'none', skip_ocr: false, wait_for_task: true, consume_subdir: null };
      default:
        return {};
    }
  }

  function add() {
    destinations = [...destinations, { type: addType, enabled: true, label: '', config: defaults(addType) }];
  }
  function remove(i: number) {
    destinations = destinations.filter((_, j) => j !== i);
  }
  function move(i: number, d: -1 | 1) {
    const j = i + d;
    if (j < 0 || j >= destinations.length) return;
    const a = [...destinations];
    [a[i], a[j]] = [a[j], a[i]];
    destinations = a;
  }
  function cfg(i: number): Record<string, unknown> {
    return destinations[i].config as Record<string, unknown>;
  }
  function setCfg(i: number, key: string, value: unknown) {
    destinations[i] = { ...destinations[i], config: { ...cfg(i), [key]: value } };
  }
  function str(v: unknown): string {
    return v === null || v === undefined ? '' : String(v);
  }
  function nullable(v: string): string | null {
    return v.trim() === '' ? null : v;
  }
  function recipients(i: number): string[] {
    return (cfg(i).recipients as string[] | undefined) ?? [];
  }
  function toggleRecipient(i: number, email: string) {
    const r = recipients(i);
    setCfg(i, 'recipients', r.includes(email) ? r.filter((x) => x !== email) : [...r, email]);
  }
  function addRecipient(i: number) {
    const v = (newRecipient[i] ?? '').trim();
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(v)) return;
    if (!recipients(i).includes(v)) setCfg(i, 'recipients', [...recipients(i), v]);
    newRecipient[i] = '';
  }
</script>

<div class="space-y-3" data-testid="destinations-editor">
  {#each destinations as d, i (i)}
    {@const c = cfg(i)}
    <fieldset class="rounded-lg border border-slate-200 p-3 dark:border-slate-700" {disabled}>
      <legend class="sr-only">{t(`dest.${d.type}`)}</legend>
      <div class="flex flex-wrap items-center gap-2">
        <Icon name={icons[d.type]} />
        <span class="font-medium">{t(`dest.${d.type}`)}</span>
        <label class="ml-2 flex items-center gap-2 text-sm"><input type="checkbox" class="checkbox" bind:checked={destinations[i].enabled} />{t('common.enabled')}</label>
        <span class="flex-1"></span>
        <button type="button" class="btn btn-ghost btn-icon" onclick={() => move(i, -1)} disabled={i === 0} aria-label={t('common.move_up')}><Icon name="chevron-up" /></button>
        <button type="button" class="btn btn-ghost btn-icon" onclick={() => move(i, 1)} disabled={i === destinations.length - 1} aria-label={t('common.move_down')}><Icon name="chevron-down" /></button>
        <button type="button" class="btn btn-ghost btn-icon" onclick={() => remove(i)} aria-label={t('common.remove')}><Icon name="trash" /></button>
      </div>

      <div class="mt-3 grid gap-3 sm:grid-cols-2">
        <div>
          <label class="label" for="dl-{i}">{t('dest.label')}</label>
          <input id="dl-{i}" class="input" bind:value={destinations[i].label} placeholder={t(`dest.${d.type}`)} maxlength="100" />
        </div>

        {#if d.type === 'folder'}
          <div>
            <label class="label" for="root-{i}">{t('dest.folder.root')}</label>
            {#if settings?.output_roots?.length}
              <select id="root-{i}" class="input" value={str(c.root)} onchange={(e) => setCfg(i, 'root', (e.target as HTMLSelectElement).value)}>
                {#if !settings.output_roots.includes(str(c.root))}<option value={str(c.root)}>{str(c.root) || '–'}</option>{/if}
                {#each settings.output_roots as r}<option value={r}>{r}</option>{/each}
              </select>
            {:else}
              <p class="rounded bg-amber-50 p-2 text-sm text-amber-900 dark:bg-amber-950 dark:text-amber-100">{t('dest.folder.no_roots')}</p>
            {/if}
          </div>
          <div class="sm:col-span-2">
            <label class="label" for="sub-{i}">{t('dest.folder.subfolder')}</label>
            <input id="sub-{i}" class="input font-mono" value={str(c.subfolder)} oninput={(e) => setCfg(i, 'subfolder', (e.target as HTMLInputElement).value)} placeholder={'{user}/{date:%Y}'} maxlength="300" />
            <p class="hint">{t('dest.folder.subfolder_hint')}</p>
          </div>
          <label class="flex items-center gap-2 text-sm"><input type="checkbox" class="checkbox" checked={!!c.include_text} onchange={(e) => setCfg(i, 'include_text', (e.target as HTMLInputElement).checked)} />{t('dest.include_text')}</label>
        {:else if d.type === 'email'}
          {#if settings && !settings.smtp_configured}
            <p class="rounded bg-amber-50 p-2 text-sm text-amber-900 sm:col-span-2 dark:bg-amber-950 dark:text-amber-100">{t('dest.email.no_smtp')}</p>
          {/if}
          <div class="sm:col-span-2">
            <span class="label">{t('dest.email.recipients')}</span>
            {#if contacts.length}
              <div class="flex flex-wrap gap-1">
                {#each contacts as ct (ct.id)}
                  <button type="button" class="chip min-h-8 cursor-pointer {recipients(i).includes(ct.email) ? 'bg-brand-700 text-white' : 'bg-slate-100 dark:bg-slate-800'}" aria-pressed={recipients(i).includes(ct.email)} onclick={() => toggleRecipient(i, ct.email)}>
                    {ct.name || ct.email}{#if ct.is_default}&nbsp;★{/if}
                  </button>
                {/each}
              </div>
            {:else}
              <p class="hint">{t('dest.email.no_contacts')} <a href="/settings" class="underline">{t('nav.settings')}</a></p>
            {/if}
            <ul class="mt-2 flex flex-wrap gap-1">
              {#each recipients(i).filter((r) => !contacts.some((ct) => ct.email === r)) as r}
                <li class="chip gap-1 bg-slate-100 dark:bg-slate-800">{r}<button type="button" aria-label={t('common.remove')} onclick={() => toggleRecipient(i, r)}><Icon name="x" size={12} /></button></li>
              {/each}
            </ul>
            <div class="mt-2 flex gap-2">
              <input class="input" type="email" placeholder="name@example.org" aria-label={t('dest.email.add_recipient')} bind:value={newRecipient[i]} onkeydown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault();
                  addRecipient(i);
                }
              }} />
              <button type="button" class="btn btn-secondary" onclick={() => addRecipient(i)}><Icon name="plus" />{t('common.add')}</button>
            </div>
            <label class="mt-2 flex items-center gap-2 text-sm"><input type="checkbox" class="checkbox" checked={c.use_default_recipients !== false} onchange={(e) => setCfg(i, 'use_default_recipients', (e.target as HTMLInputElement).checked)} />{t('dest.email.use_defaults')}</label>
          </div>
          <div class="sm:col-span-2">
            <label class="label" for="subj-{i}">{t('dest.email.subject')}</label>
            <input id="subj-{i}" class="input" value={str(c.subject_template)} oninput={(e) => setCfg(i, 'subject_template', nullable((e.target as HTMLInputElement).value))} placeholder={t('dest.email.subject_placeholder')} maxlength="300" />
          </div>
          <div class="sm:col-span-2">
            <label class="label" for="body-{i}">{t('dest.email.body')}</label>
            <textarea id="body-{i}" class="input min-h-24" value={str(c.body_template)} oninput={(e) => setCfg(i, 'body_template', nullable((e.target as HTMLTextAreaElement).value))} placeholder={t('dest.email.body_placeholder')} maxlength="5000"></textarea>
            <p class="hint">{t('dest.template_vars')}</p>
          </div>
          <div>
            <label class="label" for="ovr-{i}">{t('dest.email.oversize')}</label>
            <select id="ovr-{i}" class="input" value={str(c.oversize) || 'compress_then_split'} onchange={(e) => setCfg(i, 'oversize', (e.target as HTMLSelectElement).value)}>
              {#each ['compress_then_split', 'compress', 'split', 'fail'] as o}<option value={o}>{t(`dest.email.oversize.${o}` as 'dest.email.oversize.fail')}</option>{/each}
            </select>
          </div>
          <label class="flex items-center gap-2 text-sm"><input type="checkbox" class="checkbox" checked={!!c.attach_text} onchange={(e) => setCfg(i, 'attach_text', (e.target as HTMLInputElement).checked)} />{t('dest.include_text')}</label>
        {:else if d.type === 'paperless'}
          {#if settings && (!settings.paperless_url || !settings.paperless_token_set)}
            <p class="rounded bg-amber-50 p-2 text-sm text-amber-900 sm:col-span-2 dark:bg-amber-950 dark:text-amber-100">
              {!settings.paperless_url ? t('dest.paperless.no_url') : t('dest.paperless.no_token')}
            </p>
          {/if}
          <div>
            <label class="label" for="pm-{i}">{t('dest.paperless.mode')}</label>
            <select id="pm-{i}" class="input" value={str(c.mode) || 'api'} onchange={(e) => setCfg(i, 'mode', (e.target as HTMLSelectElement).value)}>
              <option value="api">{t('dest.paperless.mode_api')}</option>
              <option value="consume">{t('dest.paperless.mode_consume')}</option>
            </select>
          </div>
          {#if c.mode === 'consume'}
            <div>
              <label class="label" for="cs-{i}">{t('dest.paperless.consume_subdir')}</label>
              <input id="cs-{i}" class="input" value={str(c.consume_subdir)} oninput={(e) => setCfg(i, 'consume_subdir', nullable((e.target as HTMLInputElement).value))} />
            </div>
            <p class="hint sm:col-span-2">{t('dest.paperless.consume_hint')}</p>
          {:else}
            <div>
              <label class="label" for="pt-{i}">{t('dest.paperless.title')}</label>
              <input id="pt-{i}" class="input font-mono" value={str(c.title_template) || '{title}'} oninput={(e) => setCfg(i, 'title_template', (e.target as HTMLInputElement).value)} />
            </div>
            <div>
              <label class="label" for="tags-{i}">{t('dest.paperless.tags')}</label>
              <input id="tags-{i}" class="input" value={((c.tags as string[]) ?? []).join(', ')} onchange={(e) => setCfg(i, 'tags', (e.target as HTMLInputElement).value.split(',').map((x) => x.trim()).filter(Boolean))} placeholder="Rechnung, Scan" />
              <label class="mt-1 flex items-center gap-2 text-sm"><input type="checkbox" class="checkbox" checked={!!c.create_missing_tags} onchange={(e) => setCfg(i, 'create_missing_tags', (e.target as HTMLInputElement).checked)} />{t('dest.paperless.create_tags')}</label>
            </div>
            <div>
              <label class="label" for="cor-{i}">{t('dest.paperless.correspondent')}</label>
              <input id="cor-{i}" class="input" value={str(c.correspondent)} oninput={(e) => setCfg(i, 'correspondent', nullable((e.target as HTMLInputElement).value))} />
            </div>
            <div>
              <label class="label" for="dt-{i}">{t('dest.paperless.document_type')}</label>
              <input id="dt-{i}" class="input" value={str(c.document_type)} oninput={(e) => setCfg(i, 'document_type', nullable((e.target as HTMLInputElement).value))} />
            </div>
            <div>
              <label class="label" for="qm-{i}">{t('dest.paperless.qr_mapping')}</label>
              <select id="qm-{i}" class="input" value={str(c.qr_mapping) || 'asn'} onchange={(e) => setCfg(i, 'qr_mapping', (e.target as HTMLSelectElement).value)}>
                {#each ['none', 'asn', 'custom_field', 'tag'] as o}<option value={o}>{t(`dest.paperless.qr.${o}` as 'dest.paperless.qr.none')}</option>{/each}
              </select>
            </div>
            {#if c.qr_mapping === 'asn'}
              <div>
                <label class="label" for="af-{i}">{t('dest.paperless.asn_fallback')}</label>
                <select id="af-{i}" class="input" value={str(c.asn_fallback) || 'none'} onchange={(e) => setCfg(i, 'asn_fallback', (e.target as HTMLSelectElement).value)}>
                  {#each ['none', 'custom_field', 'tag'] as o}<option value={o}>{t(`dest.paperless.qr.${o}` as 'dest.paperless.qr.none')}</option>{/each}
                </select>
              </div>
            {/if}
            {#if c.qr_mapping === 'custom_field' || c.asn_fallback === 'custom_field'}
              <div>
                <label class="label" for="cf-{i}">{t('dest.paperless.custom_field')}</label>
                <input id="cf-{i}" class="input" value={str(c.qr_custom_field)} oninput={(e) => setCfg(i, 'qr_custom_field', nullable((e.target as HTMLInputElement).value))} />
              </div>
            {/if}
            <label class="flex items-center gap-2 text-sm"><input type="checkbox" class="checkbox" checked={c.set_created_date !== false} onchange={(e) => setCfg(i, 'set_created_date', (e.target as HTMLInputElement).checked)} />{t('dest.paperless.set_created')}</label>
            <label class="flex items-center gap-2 text-sm"><input type="checkbox" class="checkbox" checked={c.wait_for_task !== false} onchange={(e) => setCfg(i, 'wait_for_task', (e.target as HTMLInputElement).checked)} />{t('dest.paperless.wait')}</label>
          {/if}
          <label class="flex items-center gap-2 text-sm"><input type="checkbox" class="checkbox" checked={!!c.skip_ocr} onchange={(e) => setCfg(i, 'skip_ocr', (e.target as HTMLInputElement).checked)} />{t('dest.paperless.skip_ocr')}</label>
        {:else}
          <p class="hint self-end">{t('dest.download_hint')}</p>
        {/if}
      </div>
    </fieldset>
  {/each}

  <div class="flex flex-wrap gap-2">
    <label class="sr-only" for="add-dest">{t('dest.add')}</label>
    <select id="add-dest" class="input w-auto" bind:value={addType} {disabled}>
      {#each TYPES as ty}<option value={ty}>{t(`dest.${ty}`)}</option>{/each}
    </select>
    <button type="button" class="btn btn-secondary" onclick={add} {disabled}><Icon name="plus" />{t('dest.add')}</button>
  </div>
</div>
