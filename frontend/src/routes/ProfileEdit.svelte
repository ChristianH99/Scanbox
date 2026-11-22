<script lang="ts">
  import { untrack } from 'svelte';
  import Icon from '../lib/components/Icon.svelte';
  import FilenameTemplateInput from '../lib/components/FilenameTemplateInput.svelte';
  import ZoneEditor from '../lib/components/ZoneEditor.svelte';
  import DestinationsEditor from '../lib/components/DestinationsEditor.svelte';
  import OptionalRange from '../lib/components/OptionalRange.svelte';
  import { api, errorMessage } from '../lib/api/client';
  import type { Contact, DocumentDetail, DocumentList, Profile, ProfileSettings, UserSettings } from '../lib/api/types';
  import { inputCaps, scanner } from '../lib/stores/scanner.svelte';
  import { session } from '../lib/stores/session.svelte';
  import { notify } from '../lib/stores/toast.svelte';
  import { confirmDialog } from '../lib/stores/dialog.svelte';
  import { navigate, setLeaveGuard } from '../lib/router.svelte';
  import { docApi, orderedPages } from '../lib/docs';
  import { t, tMaybe } from '../lib/i18n/index.svelte';
  import { clone, sameJson } from '../lib/utils/misc';

  let { id }: { id: string } = $props();
  const isNew = untrack(() => id) === 'new';

  let profile = $state<Profile | null>(null);
  let name = $state('');
  let description = $state('');
  let shared = $state(false);
  let s = $state<ProfileSettings | null>(null);
  let original = '';
  let error = $state<string | null>(null);
  let saveError = $state<string | null>(null);
  let saving = $state(false);
  let userSettings = $state<UserSettings | null>(null);
  let contacts = $state<Contact[]>([]);
  let sampleUrl = $state<string | null>(null);

  let readonly = $derived(!!profile?.shared && !session.user?.is_admin);
  let caps = $derived(scanner.caps);
  let ic = $derived(s ? inputCaps(caps, s.source) : null);
  let ranges = $derived(caps?.ranges ?? {});
  let dirty = $derived(!!s && JSON.stringify({ name, description, s }) !== original);

  const FALLBACK: ProfileSettings = {
    source: 'adf_duplex', resolution: 300, color_mode: 'color', bw_strategy: 'software', page_size: 'a4', intent: 'Document', content_type: null,
    scanner: { multipick_detection: true, multipick_action: 'StopJob' },
    processing: { auto_rotate: true, deskew: true, auto_crop: false, blank_removal: 'software', blank_threshold: 0.004, color: 'keep', bw_threshold: null, denoise: false, sharpen: false, brightness: 0, contrast: 0, split: { mode: 'off', pattern: null, remove_separator: true } },
    qr: { enabled: false, zone_preset: 'top_right', zone: null, pages: 'first', selection: 'reading_order', regex: null, on_missing: 'warn', add_to_filename: true },
    ocr: { enabled: true, languages: ['deu', 'eng'], suggest_title: true },
    output: { format: 'pdfa', compression: 'medium', jpeg_quality: 85, target_size_mb: null },
    filename_template: '{datetime}_{title}', title_template: '{profile}',
    destinations: [{ type: 'download', enabled: true, label: '', config: {} }],
    review_before_processing: true,
  } as ProfileSettings;

  async function load() {
    try {
      const [list, us, cts] = await Promise.all([
        api.get<Profile[]>('/api/profiles'),
        api.get<UserSettings>('/api/me/settings'),
        api.get<Contact[]>('/api/me/contacts'),
      ]);
      userSettings = us;
      contacts = cts;
      if (isNew) {
        const base = list.find((p) => p.builtin) ?? list.find((p) => p.shared) ?? null;
        s = clone(base?.settings ?? FALLBACK);
        name = '';
        description = '';
      } else {
        profile = await api.get<Profile>(`/api/profiles/${id}`);
        s = clone(profile.settings);
        name = profile.name;
        description = profile.description;
      }
      original = JSON.stringify({ name, description, s });
      void loadSample();
    } catch (e) {
      error = errorMessage(e);
    }
  }

  /** First page of the most recent scan, oriented + deskewed + uncropped, as QR zone background. */
  async function loadSample() {
    try {
      const r = await api.get<DocumentList>('/api/documents', { query: { scope: 'history', limit: 5 } });
      const cand = r.items.find((d) => !d.raw_expired && d.page_count > 0);
      if (!cand) return;
      const d = await api.get<DocumentDetail>(`/api/documents/${cand.id}`);
      const p = orderedPages(d).find((x) => !x.deleted);
      if (!p) return;
      const blob = await docApi.preview(p.id, { ...(p.edits ?? {}), crop: null, auto_crop: false }, 'preview');
      sampleUrl = URL.createObjectURL(blob);
    } catch {
      /* placeholder frame is fine */
    }
  }

  $effect(() => {
    void load();
    return () => {
      if (sampleUrl) URL.revokeObjectURL(sampleUrl);
    };
  });

  $effect(() =>
    setLeaveGuard(async () => {
      if (!dirty) return true;
      return confirmDialog({ title: t('editor.unsaved_title'), message: t('editor.unsaved_text'), confirmLabel: t('editor.discard_changes'), danger: true });
    }),
  );

  async function save(e: SubmitEvent) {
    e.preventDefault();
    if (!s) return;
    saving = true;
    saveError = null;
    try {
      const body = { name: name.trim(), description: description.trim(), settings: s };
      const out = isNew
        ? await api.post<Profile>('/api/profiles', body, { query: { shared: shared || undefined } })
        : await api.put<Profile>(`/api/profiles/${id}`, body);
      profile = out;
      s = clone(out.settings);
      original = JSON.stringify({ name, description, s });
      notify.success(t('common.saved'));
      if (isNew) await navigate(`/profiles/${out.id}`, { replace: true, force: true });
    } catch (err) {
      saveError = errorMessage(err);
    } finally {
      saving = false;
    }
  }

  const PAGE_SIZES = ['a4', 'a5', 'letter', 'legal', 'auto', 'custom'] as const;
  const FORMATS = ['pdfa', 'pdf', 'pdf_image', 'jpeg', 'png', 'tiff', 'txt'] as const;
  const RANGE_FIELDS = [
    ['Brightness', 'brightness'],
    ['Contrast', 'contrast'],
    ['Gamma', 'gamma'],
    ['Highlight', 'highlight'],
    ['Shadow', 'shadow'],
    ['Sharpen', 'sharpen'],
    ['NoiseRemoval', 'noise_removal'],
    ['Threshold', 'threshold'],
    ['CompressionFactor', 'compression_factor'],
  ] as const;
  const resolutions = $derived(ic?.resolutions?.length ? ic.resolutions : s ? [s.resolution] : []);
  const isImageFormat = $derived(!!s && ['jpeg', 'png', 'tiff'].includes(s.output.format));
  void sameJson;
</script>

{#if error}
  <div class="card p-6"><p role="alert">{error}</p><a href="/profiles" class="btn btn-secondary mt-3">{t('profiles.title')}</a></div>
{:else if !s}
  <p class="flex items-center gap-2"><Icon name="loader" />{t('common.loading')}</p>
{:else}
  <form class="space-y-4" onsubmit={save} data-testid="profile-form">
    <div class="flex flex-wrap items-center gap-2">
      <a href="/profiles" class="btn btn-ghost btn-icon" aria-label={t('common.back')}><Icon name="arrow-left" /></a>
      <h1 class="min-w-0 flex-1 truncate text-xl font-semibold">{isNew ? t('profiles.new') : name || t('profiles.title')}</h1>
      {#if !readonly}
        <button type="submit" class="btn btn-primary" disabled={saving || !name.trim()} data-testid="profile-save">
          {#if saving}<Icon name="loader" />{/if}{t('common.save')}
        </button>
      {/if}
    </div>

    {#if readonly}
      <p class="rounded-lg bg-slate-100 p-3 text-sm dark:bg-slate-800">{t('profiles.readonly_builtin')}</p>
    {/if}
    {#if saveError}
      <p class="rounded-lg bg-red-50 p-3 text-sm break-words text-red-800 dark:bg-red-950 dark:text-red-200" role="alert">{saveError}</p>
    {/if}
    {#if profile?.problems.length}
      <div class="rounded-lg bg-amber-50 p-3 text-sm text-amber-900 dark:bg-amber-950 dark:text-amber-100" role="alert">
        <p class="font-semibold">{t('profiles.problems')}</p>
        <ul class="list-disc pl-5">{#each profile.problems as pr}<li>{pr}</li>{/each}</ul>
      </div>
    {/if}
    {#if !caps?.available}
      <p class="rounded-lg bg-slate-100 p-3 text-sm dark:bg-slate-800">{t('profile.no_caps')}</p>
    {/if}

    <fieldset disabled={readonly} class="space-y-4">
      <!-- General -->
      <section class="card grid gap-3 p-4 sm:grid-cols-2">
        <h2 class="section-title sm:col-span-2">{t('profile.general')}</h2>
        <div><label class="label" for="pname">{t('profile.name')}</label><input id="pname" class="input" bind:value={name} required maxlength="100" /></div>
        <div><label class="label" for="pdesc">{t('profile.description')}</label><input id="pdesc" class="input" bind:value={description} maxlength="300" /></div>
        {#if isNew && session.user?.is_admin}
          <label class="flex items-center gap-2 text-sm sm:col-span-2"><input type="checkbox" class="checkbox" bind:checked={shared} />{t('profile.shared')}</label>
        {/if}
        <label class="flex items-center gap-2 text-sm sm:col-span-2"><input type="checkbox" class="checkbox" bind:checked={s.review_before_processing} />{t('profile.review')}</label>
      </section>

      <!-- Scanning -->
      <section class="card grid gap-3 p-4 sm:grid-cols-2 lg:grid-cols-3">
        <h2 class="section-title sm:col-span-2 lg:col-span-3">{t('profile.scanning')}</h2>
        <div><label class="label" for="src">{t('scan.sides')}</label>
          <select id="src" class="input" bind:value={s.source}>
            {#each caps?.sources?.length ? caps.sources : ['adf_duplex', 'adf_simplex'] as v}<option value={v}>{tMaybe(`source.${v}`, v)}</option>{/each}
          </select></div>
        <div><label class="label" for="resn">{t('scan.resolution')}</label>
          <select id="resn" class="input" bind:value={s.resolution}>
            {#each resolutions as r}<option value={r}>{r} dpi</option>{/each}
          </select></div>
        <div><label class="label" for="cm">{t('scan.color')}</label>
          <select id="cm" class="input" bind:value={s.color_mode}>
            {#each caps?.color_modes?.length ? caps.color_modes : ['color', 'gray', 'bw'] as m}<option value={m}>{tMaybe(`color.${m}`, m)}</option>{/each}
          </select></div>
        {#if s.color_mode === 'bw'}
          <div class="sm:col-span-2 lg:col-span-3"><label class="label" for="bws">{t('profile.bw_strategy')}</label>
            <select id="bws" class="input" bind:value={s.bw_strategy}>
              <option value="software">{t('profile.bw_software')}</option>
              <option value="scanner">{t('profile.bw_scanner')}</option>
            </select>
            <p class="hint">{t('profile.bw_hint')}</p></div>
        {/if}
        <div><label class="label" for="ps">{t('profile.page_size')}</label>
          <select id="ps" class="input" bind:value={s.page_size}>
            {#each PAGE_SIZES as v}<option value={v}>{t(`pagesize.${v}`)}</option>{/each}
          </select></div>
        {#if s.page_size === 'custom'}
          <div><label class="label" for="cw">{t('profile.width_mm')}</label><input id="cw" type="number" class="input" min="11" max="216" step="0.1" bind:value={s.custom_width_mm} required /></div>
          <div><label class="label" for="ch">{t('profile.height_mm')}</label><input id="ch" type="number" class="input" min="11" max="3098" step="0.1" bind:value={s.custom_height_mm} required /></div>
        {/if}
        {#if ic?.intents?.length}
          <div><label class="label" for="intent">{t('profile.intent')}</label>
            <select id="intent" class="input" bind:value={s.intent}>
              <option value={null}>{t('profile.scanner_default')}</option>
              {#each ic.intents as v}<option value={v}>{tMaybe(`intent.${v}`, v)}</option>{/each}
            </select></div>
        {/if}
        {#if ic?.content_types?.length}
          <div><label class="label" for="ct">{t('profile.content_type')}</label>
            <select id="ct" class="input" bind:value={s.content_type}>
              <option value={null}>{t('profile.scanner_default')}</option>
              {#each ic.content_types as v}<option value={v}>{tMaybe(`content.${v}`, v)}</option>{/each}
            </select></div>
        {/if}
      </section>

      <!-- Scanner options -->
      <details class="card p-4">
        <summary class="section-title cursor-pointer">{t('profile.scanner_options')}</summary>
        <div class="mt-3 grid gap-4 sm:grid-cols-2">
          {#if caps?.blank_page_detection}
            <div>
              <label class="flex items-center gap-2 text-sm"><input type="checkbox" class="checkbox" checked={!!s.scanner.blank_page_removal} onchange={(e) => (s!.scanner.blank_page_removal = (e.target as HTMLInputElement).checked || null)} />{t('profile.scanner_blank')}</label>
              {#if s.scanner.blank_page_removal && ranges.BlankPageSensitivity}
                <div class="mt-2"><OptionalRange id="bps" label={t('profile.blank_sensitivity')} range={ranges.BlankPageSensitivity} bind:value={s.scanner.blank_page_sensitivity} /></div>
              {/if}
            </div>
          {/if}
          {#if caps?.multipick_actions?.length}
            <div>
              <label class="flex items-center gap-2 text-sm"><input type="checkbox" class="checkbox" checked={s.scanner.multipick_detection !== false} onchange={(e) => (s!.scanner.multipick_detection = (e.target as HTMLInputElement).checked)} />{t('profile.multipick')}</label>
              {#if s.scanner.multipick_detection !== false}
                <label class="label mt-2" for="mpa">{t('profile.multipick_action')}</label>
                <select id="mpa" class="input" bind:value={s.scanner.multipick_action}>
                  {#each caps.multipick_actions as a}<option value={a}>{tMaybe(`multipick.${a}`, a)}</option>{/each}
                </select>
              {/if}
            </div>
          {/if}
          {#if caps?.auto_crop}
            <label class="flex items-center gap-2 text-sm"><input type="checkbox" class="checkbox" checked={!!s.scanner.auto_crop} onchange={(e) => (s!.scanner.auto_crop = (e.target as HTMLInputElement).checked || null)} />{t('profile.scanner_autocrop')}</label>
          {/if}
          {#if caps?.carrier_sheet}
            <label class="flex items-center gap-2 text-sm"><input type="checkbox" class="checkbox" checked={!!s.scanner.carrier_sheet} onchange={(e) => (s!.scanner.carrier_sheet = (e.target as HTMLInputElement).checked || null)} />{t('profile.carrier_sheet')}</label>
          {/if}
          {#each RANGE_FIELDS as [capName, key]}
            {#if ranges[capName]}
              <OptionalRange id="r-{key}" label={tMaybe(`range.${capName}`, capName)} range={ranges[capName]} bind:value={s.scanner[key]} />
            {/if}
          {/each}
        </div>
      </details>

      <!-- Processing -->
      <details class="card p-4" open>
        <summary class="section-title cursor-pointer">{t('profile.processing')}</summary>
        <div class="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          <label class="flex items-center gap-2 text-sm"><input type="checkbox" class="checkbox" bind:checked={s.processing.auto_rotate} />{t('profile.auto_rotate')}</label>
          <label class="flex items-center gap-2 text-sm"><input type="checkbox" class="checkbox" bind:checked={s.processing.deskew} />{t('profile.deskew')}</label>
          <label class="flex items-center gap-2 text-sm"><input type="checkbox" class="checkbox" bind:checked={s.processing.auto_crop} />{t('profile.auto_crop')}</label>
          <div><label class="label" for="blank">{t('profile.blank_removal')}</label>
            <select id="blank" class="input" bind:value={s.processing.blank_removal}>
              {#each ['off', 'software', 'scanner', 'both'] as v}
                {#if caps?.blank_page_detection || (v !== 'scanner' && v !== 'both')}<option value={v}>{t(`blank.${v}` as 'blank.off')}</option>{/if}
              {/each}
            </select></div>
          {#if s.processing.blank_removal === 'software' || s.processing.blank_removal === 'both'}
            <div><label class="label" for="bt">{t('profile.blank_threshold')}</label>
              <input id="bt" type="number" class="input" min="0" max="20" step="0.05" value={+(s.processing.blank_threshold * 100).toFixed(3)} oninput={(e) => (s!.processing.blank_threshold = Number((e.target as HTMLInputElement).value) / 100)} />
              <p class="hint">{t('profile.blank_threshold_hint')}</p></div>
          {/if}
          <div><label class="label" for="pcol">{t('editor.color')}</label>
            <select id="pcol" class="input" bind:value={s.processing.color}>
              {#each ['keep', 'color', 'gray', 'bw'] as v}<option value={v}>{t(`pcolor.${v}` as 'pcolor.keep')}</option>{/each}
            </select></div>
          {#if s.processing.color === 'bw' || s.color_mode === 'bw'}
            <div><label class="label" for="bwt">{t('editor.threshold')}</label>
              <input id="bwt" type="number" class="input" min="0" max="255" placeholder={t('editor.threshold_auto')} value={s.processing.bw_threshold ?? ''} oninput={(e) => {
                const v = (e.target as HTMLInputElement).value;
                s!.processing.bw_threshold = v === '' ? null : Number(v);
              }} />
              <p class="hint">{t('profile.bw_threshold_hint')}</p></div>
          {/if}
          <label class="flex items-center gap-2 text-sm"><input type="checkbox" class="checkbox" bind:checked={s.processing.denoise} />{t('editor.denoise')}</label>
          <label class="flex items-center gap-2 text-sm"><input type="checkbox" class="checkbox" bind:checked={s.processing.sharpen} />{t('editor.sharpen')}</label>
          <div><label class="label" for="pb">{t('editor.brightness')} ({s.processing.brightness})</label><input id="pb" type="range" min="-1" max="1" step="0.05" class="w-full accent-brand-700" bind:value={s.processing.brightness} /></div>
          <div><label class="label" for="pc">{t('editor.contrast')} ({s.processing.contrast})</label><input id="pc" type="range" min="-1" max="1" step="0.05" class="w-full accent-brand-700" bind:value={s.processing.contrast} /></div>
          <div class="sm:col-span-2 lg:col-span-3 grid gap-3 sm:grid-cols-3">
            <div><label class="label" for="split">{t('profile.split')}</label>
              <select id="split" class="input" bind:value={s.processing.split.mode}>
                {#each ['off', 'qr_change', 'qr_pattern'] as v}<option value={v}>{t(`split.${v}` as 'split.off')}</option>{/each}
              </select></div>
            {#if s.processing.split.mode === 'qr_pattern'}
              <div><label class="label" for="spat">{t('profile.split_pattern')}</label><input id="spat" class="input font-mono" value={s.processing.split.pattern ?? ''} oninput={(e) => (s!.processing.split.pattern = (e.target as HTMLInputElement).value || null)} /></div>
              <label class="flex items-center gap-2 text-sm"><input type="checkbox" class="checkbox" bind:checked={s.processing.split.remove_separator} />{t('profile.split_remove')}</label>
            {/if}
            {#if s.processing.split.mode !== 'off' && !s.qr.enabled}<p class="hint sm:col-span-3">{t('profile.split_needs_qr')}</p>{/if}
          </div>
        </div>
      </details>

      <!-- OCR + output -->
      <section class="card grid gap-3 p-4 sm:grid-cols-2 lg:grid-cols-3">
        <h2 class="section-title sm:col-span-2 lg:col-span-3">{t('profile.ocr_output')}</h2>
        <label class="flex items-center gap-2 text-sm"><input type="checkbox" class="checkbox" bind:checked={s.ocr.enabled} data-testid="ocr-enabled" />{t('profile.ocr')}</label>
        {#if s.ocr.enabled}
          <fieldset class="flex flex-wrap gap-3 text-sm">
            <legend class="sr-only">{t('profile.ocr_languages')}</legend>
            {#each [['deu', 'Deutsch'], ['eng', 'English']] as [code, lbl]}
              <label class="flex items-center gap-2"><input type="checkbox" class="checkbox" checked={s.ocr.languages.includes(code as 'deu')} onchange={(e) => {
                const on = (e.target as HTMLInputElement).checked;
                const cur = s!.ocr.languages.filter((x) => x !== code);
                s!.ocr.languages = (on ? [...cur, code] : cur.length ? cur : ['deu']) as ('deu' | 'eng')[];
              }} />{lbl}</label>
            {/each}
          </fieldset>
          <label class="flex items-center gap-2 text-sm"><input type="checkbox" class="checkbox" bind:checked={s.ocr.suggest_title} />{t('profile.suggest_title')}</label>
        {/if}
        <div><label class="label" for="fmt">{t('profile.format')}</label>
          <select id="fmt" class="input" bind:value={s.output.format}>
            {#each FORMATS as f}<option value={f}>{t(`format.${f}`)}</option>{/each}
          </select></div>
        <div><label class="label" for="cmp">{t('profile.compression')}</label>
          <select id="cmp" class="input" bind:value={s.output.compression}>
            {#each ['none', 'low', 'medium', 'high'] as c}<option value={c}>{t(`compression.${c}` as 'compression.none')}</option>{/each}
          </select></div>
        <div><label class="label" for="jq">{t('profile.jpeg_quality')} ({s.output.jpeg_quality})</label>
          <input id="jq" type="range" min="30" max="100" class="w-full accent-brand-700" bind:value={s.output.jpeg_quality} /></div>
        {#if !isImageFormat}
          <div><label class="label" for="ts">{t('profile.target_size')}</label>
            <input id="ts" type="number" class="input" min="0.1" max="500" step="0.1" placeholder={t('common.none')} value={s.output.target_size_mb ?? ''} oninput={(e) => {
              const v = (e.target as HTMLInputElement).value;
              s!.output.target_size_mb = v === '' ? null : Number(v);
            }} /></div>
        {/if}
      </section>

      <!-- Naming -->
      <section class="card space-y-4 p-4">
        <h2 class="section-title">{t('profile.naming')}</h2>
        <FilenameTemplateInput id="fnt" label={t('profile.filename_template')} bind:value={s.filename_template} format={s.output.format} addQr={s.qr.enabled && s.qr.add_to_filename} profileName={name} userName={session.user?.username ?? ''} disabled={readonly} />
        <FilenameTemplateInput id="tt" label={t('profile.title_template')} bind:value={s.title_template} showExample={false} tokens={['profile', 'date', 'time', 'qr', 'user', 'pages']} disabled={readonly} />
      </section>

      <!-- QR -->
      <section class="card space-y-3 p-4">
        <label class="flex items-center gap-2"><input type="checkbox" class="checkbox" bind:checked={s.qr.enabled} /><span class="section-title">{t('profile.qr')}</span></label>
        {#if s.qr.enabled}
          <div class="grid gap-4 md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
            <ZoneEditor bind:preset={s.qr.zone_preset} bind:zone={s.qr.zone} {sampleUrl} disabled={readonly} />
            <div class="space-y-3">
              <div><label class="label" for="qpages">{t('profile.qr_pages')}</label>
                <select id="qpages" class="input" bind:value={s.qr.pages}>
                  {#each ['first', 'all', 'each'] as v}<option value={v}>{t(`qrpages.${v}` as 'qrpages.first')}</option>{/each}
                </select></div>
              <div><label class="label" for="qsel">{t('profile.qr_selection')}</label>
                <select id="qsel" class="input" bind:value={s.qr.selection}>
                  {#each ['reading_order', 'largest', 'regex'] as v}<option value={v}>{t(`qrsel.${v}` as 'qrsel.largest')}</option>{/each}
                </select></div>
              <div><label class="label" for="qre">{t('profile.qr_regex')}</label>
                <input id="qre" class="input font-mono" value={s.qr.regex ?? ''} oninput={(e) => (s!.qr.regex = (e.target as HTMLInputElement).value || null)} placeholder="^INV-\d+$" maxlength="300" />
                <p class="hint">{t('profile.qr_regex_hint')}</p></div>
              <div><label class="label" for="qmiss">{t('profile.qr_missing')}</label>
                <select id="qmiss" class="input" bind:value={s.qr.on_missing}>
                  {#each ['continue', 'warn', 'block'] as v}<option value={v}>{t(`qrmiss.${v}` as 'qrmiss.warn')}</option>{/each}
                </select></div>
              <label class="flex items-center gap-2 text-sm"><input type="checkbox" class="checkbox" bind:checked={s.qr.add_to_filename} />{t('scan.add_qr')}</label>
            </div>
          </div>
        {/if}
      </section>

      <!-- Destinations -->
      <section class="card space-y-3 p-4">
        <h2 class="section-title">{t('profile.destinations')}</h2>
        <DestinationsEditor bind:destinations={s.destinations} settings={userSettings} {contacts} disabled={readonly} />
      </section>
    </fieldset>

    {#if !readonly}
      <div class="sticky bottom-16 z-10 flex justify-end md:bottom-2">
        <button type="submit" class="btn btn-primary btn-lg shadow-lg" disabled={saving || !name.trim()}>
          {#if saving}<Icon name="loader" />{/if}{t('common.save')}{#if dirty}&nbsp;•{/if}
        </button>
      </div>
    {/if}
  </form>
{/if}
