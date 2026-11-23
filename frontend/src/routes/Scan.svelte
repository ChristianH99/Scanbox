<script lang="ts">
  import Icon from '../lib/components/Icon.svelte';
  import ScannerStatusCard from '../lib/components/ScannerStatusCard.svelte';
  import ActiveDocumentPanel from '../lib/components/ActiveDocumentPanel.svelte';
  import { api, errorMessage, isApiError } from '../lib/api/client';
  import type { DocumentDetail, DocumentList, Profile, ScanIn, UserSettings } from '../lib/api/types';
  import { inputCaps, scanner } from '../lib/stores/scanner.svelte';
  import { session } from '../lib/stores/session.svelte';
  import { notify } from '../lib/stores/toast.svelte';
  import { onLive } from '../lib/events.svelte';
  import { t, tMaybe } from '../lib/i18n/index.svelte';
  import { scannerView } from '../lib/utils/status';
  import { debounce } from '../lib/utils/misc';

  const ACTIVE_KEY = 'scanbox.activeDocs';

  let profiles = $state<Profile[]>([]);
  let settings = $state<UserSettings | null>(null);
  let loadError = $state<string | null>(null);
  let profileId = $state<number | null>(null);
  let title = $state('');
  let addQr = $state(false);
  let source = $state<string>('adf_duplex');
  let resolution = $state<number>(300);
  let colorMode = $state<string>('color');
  let showOptions = $state(false);
  let starting = $state(false);
  let startError = $state<{ code: string; message: string } | null>(null);
  let activeIds = $state<number[]>(readActive());

  function readActive(): number[] {
    try {
      return JSON.parse(sessionStorage.getItem(ACTIVE_KEY) ?? '[]').filter((x: unknown) => typeof x === 'number');
    } catch {
      return [];
    }
  }
  $effect(() => {
    try {
      sessionStorage.setItem(ACTIVE_KEY, JSON.stringify(activeIds));
    } catch {
      /* ignore */
    }
  });

  let profile = $derived(profiles.find((p) => p.id === profileId) ?? null);
  let caps = $derived(scanner.caps);
  let ic = $derived(inputCaps(caps, source));
  let view = $derived(scannerView(scanner.status));

  async function load() {
    try {
      const [p, s, active] = await Promise.all([
        api.get<Profile[]>('/api/profiles'),
        api.get<UserSettings>('/api/me/settings'),
        api.get<DocumentList>('/api/documents', { query: { scope: 'active', limit: 10 } }),
      ]);
      profiles = p;
      settings = s;
      loadError = null;
      if (profileId === null || !p.some((x) => x.id === profileId)) {
        profileId = (s.default_profile_id && p.some((x) => x.id === s.default_profile_id) ? s.default_profile_id : p[0]?.id) ?? null;
      }
      // Restore documents still in progress (e.g. after a reload or from another device).
      const ids = new Set(activeIds);
      for (const d of active.items) ids.add(d.id);
      activeIds = [...ids].sort((a, b) => b - a);
    } catch (e) {
      loadError = errorMessage(e);
    }
  }

  $effect(() => {
    void load();
    const refetch = debounce(() => void load(), 500);
    return onLive(['reconnect', 'document.created'], refetch);
  });

  // Reset quick options when the profile changes.
  $effect(() => {
    const p = profile;
    if (!p) return;
    source = p.settings.source;
    resolution = p.settings.resolution;
    colorMode = p.settings.color_mode;
    addQr = !!(p.settings.qr?.enabled && p.settings.qr?.add_to_filename);
  });

  let sources = $derived(caps?.sources?.length ? caps.sources : ['adf_duplex', 'adf_simplex']);
  let resolutions = $derived(ic?.resolutions?.length ? ic.resolutions : [resolution]);
  let colorModes = $derived(caps?.color_modes?.length ? caps.color_modes : [colorMode]);

  let overrides = $derived.by(() => {
    if (!profile) return {};
    const o: Record<string, unknown> = {};
    if (source !== profile.settings.source) o.source = source;
    if (resolution !== profile.settings.resolution) o.resolution = resolution;
    if (colorMode !== profile.settings.color_mode) o.color_mode = colorMode;
    return o;
  });
  let overrideCount = $derived(Object.keys(overrides).length);

  let disabledReason = $derived.by(() => {
    if (!profile) return t('scan.no_profile');
    if (!view.canScan) return view.reason ? t(view.reason) : null;
    return null;
  });

  async function start() {
    if (!profile || disabledReason) return;
    starting = true;
    startError = null;
    try {
      const body: ScanIn = {
        profile_id: profile.id,
        overrides: overrideCount ? overrides : null,
        title: title.trim() || null,
        add_qr_to_filename: addQr,
      };
      const doc = await api.post<DocumentDetail>('/api/scans', body);
      activeIds = [doc.id, ...activeIds.filter((x) => x !== doc.id)];
      title = '';
      if (settings && settings.default_profile_id !== profile.id) {
        // Remember the last used profile as the user's default.
        api.patch<UserSettings>('/api/me/settings', { default_profile_id: profile.id }).then((s) => (settings = s)).catch(() => {});
      }
    } catch (e) {
      startError = { code: isApiError(e) ? e.code : 'unknown', message: errorMessage(e) };
    } finally {
      starting = false;
    }
  }

  function dismiss(id: number) {
    activeIds = activeIds.filter((x) => x !== id);
  }

  const colorLabel = (m: string) => tMaybe(`color.${m}`, m);
  const sourceLabel = (s: string) => tMaybe(`source.${s}`, s);
</script>

<div class="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,5fr)_minmax(0,7fr)] lg:items-start">
  <div class="space-y-4">
    <ScannerStatusCard status={scanner.status} error={scanner.statusError} isAdmin={session.user?.is_admin} />

    <form
      class="card space-y-4 p-4"
      onsubmit={(e) => {
        e.preventDefault();
        void start();
      }}
    >
      <h1 class="section-title">{t('scan.title')}</h1>
      {#if loadError}
        <p class="rounded-lg bg-red-50 p-3 text-sm text-red-800 dark:bg-red-950 dark:text-red-200" role="alert">
          {loadError} <button type="button" class="underline" onclick={load}>{t('common.retry')}</button>
        </p>
      {/if}

      <div>
        <label class="label" for="profile">{t('scan.profile')}</label>
        <select id="profile" class="input" bind:value={profileId} data-testid="profile-select">
          {#each profiles as p (p.id)}
            <option value={p.id}>{p.name}{p.problems.length ? ' ⚠' : ''}</option>
          {/each}
        </select>
        {#if profile?.description}<p class="hint">{profile.description}</p>{/if}
        {#if profile?.problems.length}
          <div class="mt-2 rounded-lg bg-amber-50 p-2 text-xs text-amber-900 dark:bg-amber-950 dark:text-amber-100" role="alert">
            <p class="font-semibold">{t('profiles.problems')}</p>
            <ul class="list-disc pl-4">{#each profile.problems as pr}<li>{pr}</li>{/each}</ul>
          </div>
        {/if}
      </div>

      <div>
        <label class="label" for="title">{t('scan.doc_title')} <span class="font-normal text-slate-500">({t('common.optional')})</span></label>
        <input id="title" class="input" bind:value={title} maxlength="300" placeholder={t('scan.doc_title_placeholder')} />
      </div>

      <label class="flex min-h-11 items-center gap-3">
        <input type="checkbox" class="checkbox" bind:checked={addQr} />
        <span class="text-sm">{t('scan.add_qr')}</span>
      </label>

      <div>
        <button type="button" class="btn btn-ghost btn-sm -ml-2" aria-expanded={showOptions} onclick={() => (showOptions = !showOptions)}>
          <Icon name={showOptions ? 'chevron-up' : 'chevron-down'} size={16} />{t('scan.quick_options')}
          {#if overrideCount}<span class="chip bg-brand-100 text-brand-800">{t('scan.changed_n', { n: overrideCount })}</span>{/if}
        </button>
        {#if showOptions}
          <div class="mt-2 grid grid-cols-1 gap-3 sm:grid-cols-2">
            <fieldset class="min-w-0 sm:col-span-2">
              <legend class="label">{t('scan.sides')}</legend>
              <div class="flex flex-wrap gap-1">
                {#each sources as s}
                  <label class="btn btn-sm min-w-0 flex-1 basis-28 text-center whitespace-normal {source === s ? 'btn-primary' : 'btn-secondary'}">
                    <input type="radio" class="sr-only" name="source" value={s} bind:group={source} />{sourceLabel(s)}
                  </label>
                {/each}
              </div>
            </fieldset>
            <div>
              <label class="label" for="res">{t('scan.resolution')}</label>
              <select id="res" class="input" bind:value={resolution}>
                {#each resolutions as r}<option value={r}>{r} dpi</option>{/each}
              </select>
            </div>
            <div>
              <label class="label" for="color">{t('scan.color')}</label>
              <select id="color" class="input" bind:value={colorMode}>
                {#each colorModes as m}<option value={m}>{colorLabel(m)}</option>{/each}
              </select>
            </div>
          </div>
        {/if}
      </div>

      {#if startError}
        <div class="rounded-lg bg-red-50 p-3 text-sm text-red-800 dark:bg-red-950 dark:text-red-200" role="alert" data-testid="start-error">
          <p class="font-semibold">{tMaybe(`errors.${startError.code}`, t('scan.start_failed'))}</p>
          <p>{startError.message}</p>
        </div>
      {/if}

      {#if view.warning && !disabledReason}
        <p class="flex items-center gap-2 text-sm text-amber-800 dark:text-amber-200"><Icon name="info" size={16} />{t(view.warning)}</p>
      {/if}

      <button type="submit" class="btn btn-primary btn-lg w-full text-lg" disabled={starting || !!disabledReason} data-testid="start-scan" aria-describedby={disabledReason ? 'start-reason' : undefined}>
        {#if starting}<Icon name="loader" />{:else}<Icon name="play" />{/if}
        {t('scan.start')}
      </button>
      {#if disabledReason}
        <p id="start-reason" class="-mt-2 text-center text-sm text-slate-600 dark:text-slate-400">{disabledReason}</p>
      {/if}
    </form>
  </div>

  <div class="space-y-4">
    {#each activeIds as id (id)}
      <ActiveDocumentPanel {id} ondismiss={() => dismiss(id)} />
    {:else}
      <div class="card flex flex-col items-center gap-2 p-8 text-center text-slate-500 dark:text-slate-400">
        <Icon name="file" size={40} />
        <p>{t('scan.no_active')}</p>
        <p class="text-sm">{t('scan.no_active_hint')}</p>
      </div>
    {/each}
  </div>
</div>
