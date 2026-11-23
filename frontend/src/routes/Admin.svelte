<script lang="ts">
  import Icon from '../lib/components/Icon.svelte';
  import AdminField from '../lib/components/AdminField.svelte';
  import ScannerStatusCard from '../lib/components/ScannerStatusCard.svelte';
  import { api, errorMessage } from '../lib/api/client';
  import type { AdminSettings, AdminSettingValue, Capabilities, ClearCache, Diagnostics, Profile, TestConnection, TestResult, Usage, User } from '../lib/api/types';
  import { loadCapabilities, scanner } from '../lib/stores/scanner.svelte';
  import { session } from '../lib/stores/session.svelte';
  import { notify } from '../lib/stores/toast.svelte';
  import { confirmDialog } from '../lib/stores/dialog.svelte';
  import { onLive } from '../lib/events.svelte';
  import { fmtBytes, fmtDateTime, t, type MessageKey } from '../lib/i18n/index.svelte';
  import { copyText } from '../lib/utils/misc';

  let { tab }: { tab: string } = $props();

  const TABS: [string, MessageKey][] = [
    ['scanner', 'admin.tab.scanner'],
    ['sso', 'admin.tab.sso'],
    ['mail', 'admin.tab.mail'],
    ['paperless', 'admin.tab.paperless'],
    ['storage', 'admin.tab.storage'],
    ['walkup', 'admin.tab.walkup'],
    ['users', 'admin.tab.users'],
    ['diagnostics', 'admin.tab.diagnostics'],
  ];

  let settings = $state<Record<string, AdminSettingValue>>({});
  let draft = $state<Record<string, unknown>>({});
  let clear = $state<Set<string>>(new Set());
  let loadError = $state<string | null>(null);
  let saving = $state(false);
  let saveError = $state<string | null>(null);

  async function loadSettings() {
    try {
      settings = (await api.get<AdminSettings>('/api/admin/settings')).settings;
      loadError = null;
    } catch (e) {
      loadError = errorMessage(e);
    }
  }
  $effect(() => void loadSettings());

  let dirty = $derived(Object.keys(draft).length > 0 || clear.size > 0);

  async function save() {
    saving = true;
    saveError = null;
    try {
      settings = (await api.patch<AdminSettings>('/api/admin/settings', { values: draft, clear_secrets: [...clear] })).settings;
      draft = {};
      clear = new Set();
      notify.success(t('common.saved'));
    } catch (e) {
      saveError = errorMessage(e);
    } finally {
      saving = false;
    }
  }

  // ---------------------------------------------------------------- scanner
  let testAddr = $state('');
  let testing = $state(false);
  let testResult = $state<TestConnection | null>(null);
  let refreshing = $state(false);
  $effect(() => {
    if (!testAddr && settings['scanner.url']?.value) testAddr = String(settings['scanner.url'].value);
  });

  async function testConnection() {
    testing = true;
    testResult = null;
    try {
      testResult = await api.post<TestConnection>('/api/scanner/test', { address: testAddr.trim() });
    } catch (e) {
      testResult = { ok: false, error: errorMessage(e), sources: [], resolutions: [] } as TestConnection;
    } finally {
      testing = false;
    }
  }

  async function useAddress() {
    draft = { ...draft, 'scanner.url': testResult?.url ?? testAddr.trim() };
    await save();
  }

  async function refreshCaps() {
    refreshing = true;
    try {
      const c = await api.post<Capabilities>('/api/scanner/refresh');
      scanner.caps = c;
      notify.success(t('admin.caps_refreshed', { model: c.make_and_model ?? '' }));
    } catch (e) {
      notify.error(e);
      void loadCapabilities();
    } finally {
      refreshing = false;
    }
  }

  // ---------------------------------------------------------------- tests
  let testMsg = $state<Record<string, TestResult | null>>({});
  let testBusy = $state<string | null>(null);
  let testMailTo = $state(session.user?.email ?? '');
  async function runTest(name: 'smtp' | 'mail' | 'paperless') {
    if (dirty) {
      notify.warning(t('admin.save_first'));
      return;
    }
    testBusy = name;
    try {
      testMsg[name] = await api.post<TestResult>(`/api/admin/test/${name}`, name === 'mail' ? { to: testMailTo.trim() } : undefined);
    } catch (e) {
      testMsg[name] = { ok: false, message: errorMessage(e) };
    } finally {
      testBusy = null;
    }
  }

  // ---------------------------------------------------------------- output roots
  let roots = $state<{ name: string; path: string }[]>([]);
  let rootsLoadedFrom = '';
  $effect(() => {
    const v = settings['output.roots']?.value as Record<string, string> | undefined;
    const key = JSON.stringify(v ?? {});
    if (key !== rootsLoadedFrom) {
      rootsLoadedFrom = key;
      roots = Object.entries(v ?? {}).map(([name, path]) => ({ name, path }));
    }
  });
  function rootsChanged() {
    draft = { ...draft, 'output.roots': Object.fromEntries(roots.filter((r) => r.name.trim()).map((r) => [r.name.trim(), r.path.trim()])) };
  }

  // ---------------------------------------------------------------- usage / diagnostics / users
  let usage = $state<Usage | null>(null);
  let diag = $state<Diagnostics | null>(null);
  let users = $state<User[]>([]);
  let profiles = $state<Profile[]>([]);
  let walkup = $state<Record<string, unknown> | null>(null);

  async function loadTabData() {
    try {
      if (tab === 'storage') usage = await api.get<Usage>('/api/admin/usage');
      if (tab === 'diagnostics' || tab === 'walkup') {
        diag = await api.get<Diagnostics>('/api/admin/diagnostics');
        walkup = (diag.walkup as Record<string, unknown> | null) ?? null;
      }
      if (tab === 'users') users = await api.get<User[]>('/api/admin/users');
      if (tab === 'walkup') profiles = await api.get<Profile[]>('/api/profiles');
    } catch (e) {
      notify.error(e);
    }
  }
  $effect(() => {
    void tab;
    void loadTabData();
  });
  $effect(() => onLive('walkup.status', (d) => (walkup = d)));

  async function clearCache() {
    const ok = await confirmDialog({ title: t('admin.clear_cache_title'), message: t('admin.clear_cache_text'), confirmLabel: t('admin.clear_cache'), danger: true });
    if (!ok) return;
    try {
      const r = await api.post<ClearCache>('/api/admin/clear-cache');
      notify.success(t('admin.cache_cleared', { raw: r.raw_expired, processed: r.processed_expired, inbox: r.inbox_expired, bytes: fmtBytes(r.render_cache_bytes) }));
      usage = await api.get<Usage>('/api/admin/usage');
    } catch (e) {
      notify.error(e);
    }
  }

  async function patchUser(u: User, body: { is_admin?: boolean; disabled?: boolean }) {
    try {
      const nu = await api.patch<User>(`/api/admin/users/${u.id}`, body);
      users = users.map((x) => (x.id === u.id ? nu : x));
    } catch (e) {
      notify.error(e);
    }
  }

  async function restartWalkup() {
    try {
      walkup = await api.post<Record<string, unknown>>('/api/admin/walkup/restart');
      notify.success(t('admin.walkup_restarted'));
    } catch (e) {
      notify.error(e);
    }
  }

  let redirectUri = typeof location !== 'undefined' ? `${location.origin}/api/auth/oidc/callback` : '';
  let usageParts = $derived(
    usage
      ? ([
          ['admin.usage.raw', usage.raw_bytes, 'bg-sky-500'],
          ['admin.usage.processed', usage.processed_bytes, 'bg-emerald-500'],
          ['admin.usage.cache', usage.cache_bytes, 'bg-amber-500'],
          ['admin.usage.db', usage.db_bytes, 'bg-violet-500'],
          ['admin.usage.logs', usage.logs_bytes, 'bg-slate-500'],
        ] as [MessageKey, number, string][])
      : [],
  );
</script>

<div class="space-y-4">
  <h1 class="text-xl font-semibold">{t('admin.title')}</h1>

  <nav class="flex flex-wrap gap-1" aria-label={t('admin.title')}>
    {#each TABS as [k, label]}
      <a href="/admin/{k}" class="btn btn-sm shrink-0 {tab === k ? 'btn-primary' : 'btn-secondary'}" aria-current={tab === k ? 'page' : undefined}>{t(label)}</a>
    {/each}
  </nav>

  {#if loadError}<p class="rounded-lg bg-red-50 p-3 text-sm text-red-800 dark:bg-red-950 dark:text-red-200" role="alert">{loadError}</p>{/if}
  {#if saveError}<p class="rounded-lg bg-red-50 p-3 text-sm break-words text-red-800 dark:bg-red-950 dark:text-red-200" role="alert">{saveError}</p>{/if}

  {#if tab === 'scanner'}
    <ScannerStatusCard status={scanner.status} error={scanner.statusError} isAdmin={false} />
    <section class="card space-y-3 p-4">
      <h2 class="section-title">{t('admin.scanner_address')}</h2>
      {#if settings['scanner.url']?.locked}
        <p class="text-sm"><code>{settings['scanner.url'].value}</code></p>
        <p class="hint flex items-center gap-1"><Icon name="lock" size={12} />{t('admin.locked')}</p>
      {/if}
      <div class="flex flex-wrap gap-2">
        <input class="input min-w-0 flex-1" bind:value={testAddr} placeholder="192.168.1.50 / http://scanner.local" aria-label={t('admin.scanner_address')} />
        <button type="button" class="btn btn-secondary" disabled={testing || !testAddr.trim()} onclick={testConnection} data-testid="scanner-test">
          {#if testing}<Icon name="loader" />{:else}<Icon name="activity" />{/if}{t('admin.test_connection')}
        </button>
      </div>
      {#if testResult}
        <div class="rounded-lg p-3 text-sm {testResult.ok ? 'bg-emerald-50 text-emerald-900 dark:bg-emerald-950 dark:text-emerald-100' : 'bg-red-50 text-red-900 dark:bg-red-950 dark:text-red-100'}" role="status">
          {#if testResult.ok}
            <p class="font-semibold">{t('admin.test_ok')}</p>
            <dl class="mt-1 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5">
              <dt>{t('admin.model')}</dt><dd>{testResult.model ?? '–'}</dd>
              <dt>{t('admin.serial')}</dt><dd>{testResult.serial_number ?? '–'}</dd>
              <dt>URL</dt><dd class="break-all">{testResult.url}</dd>
              <dt>eSCL</dt><dd>{testResult.escl_version ?? '–'}</dd>
              <dt>{t('admin.sources')}</dt><dd>{testResult.sources.join(', ')}</dd>
              <dt>{t('scan.resolution')}</dt><dd>{testResult.resolutions.join(', ')}</dd>
              {#if testResult.status}<dt>Status</dt><dd>{JSON.stringify(testResult.status)}</dd>{/if}
              {#if testResult.admin_uri}<dt>Web</dt><dd><a class="underline" href={testResult.admin_uri} target="_blank" rel="noopener" data-external>{testResult.admin_uri}</a></dd>{/if}
            </dl>
            {#if !settings['scanner.url']?.locked}
              <button type="button" class="btn btn-primary mt-2" disabled={saving} onclick={useAddress}>{t('admin.use_address')}</button>
            {/if}
          {:else}
            <p class="font-semibold">{t('admin.test_failed')}</p>
            <p>{testResult.error}{testResult.error_code ? ` (${testResult.error_code})` : ''}</p>
          {/if}
        </div>
      {/if}
      <div class="flex flex-wrap items-center gap-2 border-t border-slate-200 pt-3 dark:border-slate-800">
        <button type="button" class="btn btn-secondary" disabled={refreshing} onclick={refreshCaps}>
          {#if refreshing}<Icon name="loader" />{:else}<Icon name="refresh" />{/if}{t('admin.refresh_caps')}
        </button>
        {#if scanner.caps?.fetched_at}<span class="text-xs text-slate-500">{t('admin.caps_fetched', { date: fmtDateTime(scanner.caps.fetched_at) })}</span>{/if}
      </div>
    </section>
  {:else if tab === 'sso'}
    <section class="card grid grid-cols-1 gap-3 p-4 sm:grid-cols-2">
      <h2 class="section-title sm:col-span-2">{t('admin.tab.sso')}</h2>
      <div class="sm:col-span-2">
        <span class="label">{t('admin.redirect_uri')}</span>
        <div class="flex gap-2"><input class="input font-mono" readonly value={redirectUri} aria-label={t('admin.redirect_uri')} />
          <button type="button" class="btn btn-secondary" onclick={async () => (await copyText(redirectUri)) && notify.success(t('common.copied'))}><Icon name="copy" />{t('common.copy')}</button></div>
        <p class="hint">{t('admin.redirect_hint')}</p>
      </div>
      <AdminField key="oidc.issuer" label={t('admin.oidc_issuer')} {settings} bind:draft bind:clear placeholder="https://sso.example.lan/webman/sso" />
      <AdminField key="oidc.client_id" label={t('admin.oidc_client_id')} {settings} bind:draft bind:clear />
      <AdminField key="oidc.client_secret" label={t('admin.oidc_client_secret')} kind="secret" {settings} bind:draft bind:clear />
      <AdminField key="oidc.scopes" label={t('admin.oidc_scopes')} {settings} bind:draft bind:clear />
      <AdminField key="oidc.admin_claim" label={t('admin.oidc_admin_claim')} {settings} bind:draft bind:clear />
      <AdminField key="oidc.admin_value" label={t('admin.oidc_admin_value')} {settings} bind:draft bind:clear />
      <AdminField key="oidc.button_label" label={t('admin.oidc_button')} {settings} bind:draft bind:clear />
    </section>
  {:else if tab === 'mail'}
    <section class="card grid grid-cols-1 gap-3 p-4 sm:grid-cols-2">
      <h2 class="section-title sm:col-span-2">SMTP</h2>
      <AdminField key="smtp.host" label={t('admin.smtp_host')} {settings} bind:draft bind:clear placeholder="smtp.gmail.com" />
      <AdminField key="smtp.port" label={t('admin.smtp_port')} kind="number" {settings} bind:draft bind:clear />
      <AdminField key="smtp.security" label={t('admin.smtp_security')} kind="select" options={[{ value: 'starttls', label: 'STARTTLS' }, { value: 'tls', label: 'TLS (465)' }, { value: 'none', label: t('admin.smtp_none') }]} {settings} bind:draft bind:clear />
      <AdminField key="smtp.username" label={t('admin.smtp_username')} {settings} bind:draft bind:clear />
      <AdminField key="smtp.password" label={t('admin.smtp_password')} kind="secret" {settings} bind:draft bind:clear />
      <AdminField key="smtp.from_address" label={t('admin.smtp_from')} {settings} bind:draft bind:clear placeholder="scanner@example.org" />
      <AdminField key="smtp.from_name" label={t('admin.smtp_from_name')} {settings} bind:draft bind:clear />
      <AdminField key="smtp.max_attachment_mb" label={t('admin.smtp_max_mb')} kind="number" {settings} bind:draft bind:clear />
      <AdminField key="smtp.timeout_s" label={t('admin.smtp_timeout')} kind="number" {settings} bind:draft bind:clear />
      <AdminField key="smtp.verify_tls" label={t('admin.verify_tls')} kind="bool" {settings} bind:draft bind:clear hint={t('admin.verify_tls_hint')} />
      <div class="space-y-2 border-t border-slate-200 pt-3 sm:col-span-2 dark:border-slate-800">
        <div class="flex flex-wrap gap-2">
          <button type="button" class="btn btn-secondary" disabled={!!testBusy} onclick={() => runTest('smtp')}>{#if testBusy === 'smtp'}<Icon name="loader" />{/if}{t('admin.test_smtp')}</button>
          <input class="input w-auto min-w-0 flex-1" type="email" bind:value={testMailTo} placeholder="you@example.org" aria-label={t('admin.test_mail_to')} />
          <button type="button" class="btn btn-secondary" disabled={!!testBusy || !testMailTo.trim()} onclick={() => runTest('mail')}>{#if testBusy === 'mail'}<Icon name="loader" />{/if}{t('admin.send_test_mail')}</button>
        </div>
        {#each ['smtp', 'mail'] as k}
          {#if testMsg[k]}<p class="text-sm {testMsg[k]!.ok ? 'text-emerald-700 dark:text-emerald-300' : 'text-red-700 dark:text-red-300'}" role="status">{testMsg[k]!.message}</p>{/if}
        {/each}
      </div>
    </section>
  {:else if tab === 'paperless'}
    <section class="card grid grid-cols-1 gap-3 p-4 sm:grid-cols-2">
      <h2 class="section-title sm:col-span-2">Paperless-ngx</h2>
      <AdminField key="paperless.url" label={t('admin.paperless_url')} {settings} bind:draft bind:clear placeholder="http://paperless:8000" />
      <AdminField key="paperless.consume_dir" label={t('admin.paperless_consume')} {settings} bind:draft bind:clear placeholder="/consume" hint={t('admin.paperless_consume_hint')} />
      <AdminField key="paperless.verify_tls" label={t('admin.verify_tls')} kind="bool" {settings} bind:draft bind:clear />
      <div class="space-y-2 sm:col-span-2">
        <button type="button" class="btn btn-secondary" disabled={!!testBusy} onclick={() => runTest('paperless')}>{#if testBusy === 'paperless'}<Icon name="loader" />{/if}{t('admin.test_paperless')}</button>
        <p class="hint">{t('admin.test_paperless_hint')}</p>
        {#if testMsg.paperless}<p class="text-sm {testMsg.paperless.ok ? 'text-emerald-700 dark:text-emerald-300' : 'text-red-700 dark:text-red-300'}" role="status">{testMsg.paperless.message}</p>{/if}
      </div>
    </section>
  {:else if tab === 'storage'}
    <section class="card space-y-3 p-4">
      <h2 class="section-title">{t('admin.output_roots')}</h2>
      <p class="hint">{t('admin.output_roots_hint')}</p>
      {#if settings['output.roots']?.locked}<p class="hint flex items-center gap-1"><Icon name="lock" size={12} />{t('admin.locked')}</p>{/if}
      {#each roots as r, i (i)}
        <div class="flex flex-wrap gap-2">
          <input class="input w-40" placeholder={t('admin.root_name')} aria-label={t('admin.root_name')} bind:value={r.name} oninput={rootsChanged} disabled={settings['output.roots']?.locked} />
          <input class="input min-w-0 flex-1 font-mono" placeholder="/output/scans" aria-label={t('admin.root_path')} bind:value={r.path} oninput={rootsChanged} disabled={settings['output.roots']?.locked} />
          <button type="button" class="btn btn-ghost btn-icon" aria-label={t('common.remove')} disabled={settings['output.roots']?.locked} onclick={() => { roots = roots.filter((_, j) => j !== i); rootsChanged(); }}><Icon name="trash" /></button>
        </div>
      {/each}
      <button type="button" class="btn btn-secondary btn-sm" disabled={settings['output.roots']?.locked} onclick={() => (roots = [...roots, { name: '', path: '' }])}><Icon name="plus" size={16} />{t('common.add')}</button>
    </section>
    <section class="card grid grid-cols-1 gap-3 p-4 sm:grid-cols-2">
      <h2 class="section-title sm:col-span-2">{t('admin.retention_jobs')}</h2>
      <AdminField key="retention.raw_days" label={t('admin.raw_days')} kind="number" {settings} bind:draft bind:clear />
      <AdminField key="retention.processed_days" label={t('admin.processed_days')} kind="number" {settings} bind:draft bind:clear />
      <AdminField key="jobs.ocr_concurrency" label={t('admin.ocr_concurrency')} kind="number" {settings} bind:draft bind:clear />
      <AdminField key="jobs.ocr_jobs" label={t('admin.ocr_jobs')} kind="number" {settings} bind:draft bind:clear />
      <AdminField key="disk.min_free_mb" label={t('admin.min_free_mb')} kind="number" {settings} bind:draft bind:clear />
    </section>
    <section class="card space-y-3 p-4">
      <h2 class="section-title">{t('admin.usage')}</h2>
      {#if usage}
        {@const total = Math.max(1, usage.total_bytes)}
        <div class="flex h-5 overflow-hidden rounded bg-slate-200 dark:bg-slate-700" role="img" aria-label={t('admin.usage')}>
          {#each usageParts as [, bytes, color]}<div class={color} style="width:{(100 * bytes) / total}%"></div>{/each}
          <div class="flex-1"></div>
        </div>
        <ul class="grid grid-cols-1 gap-1 text-sm sm:grid-cols-2">
          {#each usageParts as [label, bytes, color]}
            <li class="flex items-center gap-2"><span class="size-3 rounded {color}"></span><span class="flex-1">{t(label)}</span><span class="font-mono">{fmtBytes(bytes)}</span></li>
          {/each}
          <li class="flex items-center gap-2"><span class="size-3 rounded bg-slate-200 dark:bg-slate-700"></span><span class="flex-1">{t('admin.usage.free')}</span><span class="font-mono">{fmtBytes(usage.free_bytes)} / {fmtBytes(usage.total_bytes)}</span></li>
        </ul>
        {#if usage.free_bytes < usage.min_free_mb * 1_000_000}
          <p class="rounded bg-red-50 p-2 text-sm text-red-800 dark:bg-red-950 dark:text-red-200" role="alert">{t('admin.low_disk', { mb: usage.min_free_mb })}</p>
        {/if}
      {:else}
        <p class="flex items-center gap-2 text-sm"><Icon name="loader" />{t('common.loading')}</p>
      {/if}
      <button type="button" class="btn btn-danger" onclick={clearCache}><Icon name="trash" />{t('admin.clear_cache')}</button>
    </section>
  {:else if tab === 'walkup'}
    <section class="card grid grid-cols-1 gap-3 p-4 sm:grid-cols-2">
      <h2 class="section-title sm:col-span-2">{t('admin.tab.walkup')}</h2>
      <div class="sm:col-span-2"><AdminField key="walkup.enabled" label={t('admin.walkup_enabled')} kind="bool" {settings} bind:draft bind:clear hint={t('admin.walkup_hint')} /></div>
      <AdminField key="walkup.default_profile_id" label={t('admin.walkup_profile')} kind="select" options={[{ value: '', label: '–' }, ...profiles.map((p) => ({ value: String(p.id), label: p.name }))]} {settings} bind:draft bind:clear />
      <AdminField key="walkup.destination_name" label={t('admin.walkup_name')} {settings} bind:draft bind:clear hint={t('admin.walkup_name_hint')} />
      <AdminField key="walkup.hostname" label={t('admin.walkup_hostname')} {settings} bind:draft bind:clear hint={t('admin.walkup_hostname_hint')} />
    </section>
    <section class="card space-y-2 p-4">
      <div class="flex items-center gap-2"><h2 class="section-title flex-1">{t('admin.walkup_status')}</h2>
        <button type="button" class="btn btn-secondary btn-sm" onclick={restartWalkup}><Icon name="refresh" size={16} />{t('admin.walkup_restart')}</button></div>
      {#if walkup}
        <dl class="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
          {#each Object.entries(walkup) as [k, v]}<dt class="text-slate-500">{k}</dt><dd class="break-all">{typeof v === 'object' ? JSON.stringify(v) : String(v ?? '–')}</dd>{/each}
        </dl>
      {:else}
        <p class="text-sm text-slate-500">–</p>
      {/if}
    </section>
  {:else if tab === 'users'}
    <section class="card p-4">
      <h2 class="section-title mb-2">{t('admin.tab.users')}</h2>
      <ul class="divide-y divide-slate-200 dark:divide-slate-800">
        {#each users as u (u.id)}
          <li class="flex flex-wrap items-center gap-3 py-2">
            <div class="min-w-0 flex-1">
              <p class="font-medium">{u.display_name || u.username} <span class="text-xs text-slate-500">({u.username}{u.is_local ? ', lokal' : ', SSO'})</span></p>
              <p class="text-xs text-slate-500">{u.email ?? ''} · {t('admin.last_login', { date: fmtDateTime(u.last_login_at) })}</p>
            </div>
            <label class="flex items-center gap-2 text-sm"><input type="checkbox" class="checkbox" checked={u.is_admin} disabled={u.id === session.user?.id} onchange={(e) => patchUser(u, { is_admin: (e.target as HTMLInputElement).checked })} />Admin</label>
            <label class="flex items-center gap-2 text-sm"><input type="checkbox" class="checkbox" checked={u.disabled} disabled={u.id === session.user?.id} onchange={(e) => patchUser(u, { disabled: (e.target as HTMLInputElement).checked })} />{t('admin.disabled')}</label>
          </li>
        {/each}
      </ul>
      <p class="hint mt-2">{t('admin.users_hint')}</p>
    </section>
  {:else if tab === 'diagnostics'}
    {#if diag}
      <section class="card grid grid-cols-1 gap-4 p-4 sm:grid-cols-2">
        <div>
          <h2 class="section-title mb-2">{t('admin.system')}</h2>
          <dl class="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
            <dt>Version</dt><dd>{diag.version}</dd>
            <dt>{t('admin.workers')}</dt><dd class={diag.workers_alive ? 'text-emerald-700 dark:text-emerald-300' : 'text-red-700 dark:text-red-300'}>{diag.workers_alive ? t('admin.running') : t('admin.stopped')}</dd>
            <dt>Scanner</dt><dd>{diag.scanner.state}{diag.scanner.code ? ` (${diag.scanner.code})` : ''}</dd>
          </dl>
        </div>
        <div>
          <h2 class="section-title mb-2">{t('admin.tools')}</h2>
          <ul class="grid grid-cols-2 gap-1 text-sm">
            {#each Object.entries(diag.tools) as [k, ok]}<li class="flex items-center gap-1"><Icon name={ok ? 'check-circle' : 'x-circle'} size={16} class={ok ? 'text-emerald-600' : 'text-red-600'} />{k}</li>{/each}
          </ul>
        </div>
        <div class="sm:col-span-2">
          <h2 class="section-title mb-2">{t('admin.queue')}</h2>
          <table class="w-full text-left text-sm">
            <thead><tr><th class="py-1">Lane</th><th>Status</th></tr></thead>
            <tbody>
              {#each Object.entries(diag.queue) as [lane, counts]}
                <tr class="border-t border-slate-200 dark:border-slate-800"><td class="py-1 font-medium">{lane}</td><td>{Object.entries(counts).map(([k, n]) => `${k}: ${n}`).join(' · ')}</td></tr>
              {:else}
                <tr><td colspan="2" class="py-1 text-slate-500">–</td></tr>
              {/each}
            </tbody>
          </table>
        </div>
      </section>
      <section class="card p-4">
        <div class="mb-2 flex items-center gap-2"><h2 class="section-title flex-1">{t('admin.recent_errors')}</h2>
          <a class="btn btn-secondary btn-sm" href="/api/admin/logs" download><Icon name="download" size={16} />{t('admin.download_logs')}</a>
          <button type="button" class="btn btn-ghost btn-sm" onclick={loadTabData}><Icon name="refresh" size={16} /></button></div>
        <ul class="space-y-1 text-xs">
          {#each diag.recent_errors as err, i (i)}
            <li class="rounded bg-slate-50 p-2 font-mono break-all dark:bg-slate-800">{JSON.stringify(err)}</li>
          {:else}
            <li class="text-sm text-slate-500">{t('admin.no_errors')}</li>
          {/each}
        </ul>
      </section>
    {:else}
      <p class="flex items-center gap-2"><Icon name="loader" />{t('common.loading')}</p>
    {/if}
  {/if}

  {#if dirty}
    <div class="sticky bottom-16 z-10 flex items-center justify-end gap-2 md:bottom-2">
      <button type="button" class="btn btn-secondary shadow" onclick={() => { draft = {}; clear = new Set(); void loadSettings(); }}>{t('common.cancel')}</button>
      <button type="button" class="btn btn-primary btn-lg shadow-lg" disabled={saving} onclick={save} data-testid="admin-save">{#if saving}<Icon name="loader" />{/if}{t('common.save')}</button>
    </div>
  {/if}
</div>
