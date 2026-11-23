<script lang="ts">
  import Icon from '../lib/components/Icon.svelte';
  import Modal from '../lib/components/Modal.svelte';
  import { api, errorMessage } from '../lib/api/client';
  import type { ApiToken, ApiTokenCreated, Contact, UserSettings } from '../lib/api/types';
  import { session, updatePrefs, type Theme } from '../lib/stores/session.svelte';
  import { notify } from '../lib/stores/toast.svelte';
  import { confirmDialog } from '../lib/stores/dialog.svelte';
  import { fmtDateTime, i18n, t } from '../lib/i18n/index.svelte';
  import { copyText } from '../lib/utils/misc';

  let settings = $state<UserSettings | null>(null);
  let contacts = $state<Contact[]>([]);
  let tokens = $state<ApiToken[]>([]);
  let loadError = $state<string | null>(null);

  let displayName = $state(session.user?.display_name ?? '');
  let subject = $state('');
  let body = $state('');
  let paperlessToken = $state('');

  let cName = $state('');
  let cEmail = $state('');
  let cDefault = $state(false);
  let editingContact = $state<number | null>(null);

  let tokenName = $state('');
  let tokenDays = $state<number | ''>('');
  let created = $state<ApiTokenCreated | null>(null);
  let createdOpen = $state(false);

  async function load() {
    try {
      const [s, c, tk] = await Promise.all([
        api.get<UserSettings>('/api/me/settings'),
        api.get<Contact[]>('/api/me/contacts'),
        api.get<ApiToken[]>('/api/me/tokens'),
      ]);
      settings = s;
      contacts = c;
      tokens = tk;
      subject = s.email_subject_template ?? '';
      body = s.email_body_template ?? '';
      loadError = null;
    } catch (e) {
      loadError = errorMessage(e);
    }
  }
  $effect(() => void load());

  async function pref(p: Parameters<typeof updatePrefs>[0]) {
    try {
      await updatePrefs(p);
      notify.success(t('common.saved'));
    } catch (e) {
      notify.error(e);
    }
  }

  async function saveTemplates(e: SubmitEvent) {
    e.preventDefault();
    try {
      settings = await api.patch<UserSettings>('/api/me/settings', { email_subject_template: subject, email_body_template: body });
      notify.success(t('common.saved'));
    } catch (err) {
      notify.error(err);
    }
  }

  async function savePaperless(e: SubmitEvent, clear = false) {
    e.preventDefault();
    try {
      settings = await api.patch<UserSettings>('/api/me/settings', { paperless_token: clear ? '' : paperlessToken.trim() });
      paperlessToken = '';
      notify.success(clear ? t('settings.token_removed') : t('common.saved'));
    } catch (err) {
      notify.error(err);
    }
  }

  async function saveContact(e: SubmitEvent) {
    e.preventDefault();
    try {
      const payload = { name: cName.trim(), email: cEmail.trim(), is_default: cDefault };
      if (editingContact) await api.put(`/api/me/contacts/${editingContact}`, payload);
      else await api.post('/api/me/contacts', payload);
      cName = cEmail = '';
      cDefault = false;
      editingContact = null;
      contacts = await api.get<Contact[]>('/api/me/contacts');
    } catch (err) {
      notify.error(err);
    }
  }

  function editContact(c: Contact) {
    editingContact = c.id;
    cName = c.name;
    cEmail = c.email;
    cDefault = c.is_default;
  }

  async function deleteContact(c: Contact) {
    if (!(await confirmDialog({ title: t('settings.contact_delete'), message: c.email, confirmLabel: t('common.delete'), danger: true }))) return;
    try {
      await api.del(`/api/me/contacts/${c.id}`);
      contacts = contacts.filter((x) => x.id !== c.id);
    } catch (err) {
      notify.error(err);
    }
  }

  async function toggleDefault(c: Contact) {
    try {
      await api.put(`/api/me/contacts/${c.id}`, { name: c.name, email: c.email, is_default: !c.is_default });
      contacts = await api.get<Contact[]>('/api/me/contacts');
    } catch (err) {
      notify.error(err);
    }
  }

  async function createToken(e: SubmitEvent) {
    e.preventDefault();
    try {
      created = await api.post<ApiTokenCreated>('/api/me/tokens', { name: tokenName.trim(), expires_days: tokenDays === '' ? null : Number(tokenDays) });
      createdOpen = true;
      tokenName = '';
      tokenDays = '';
      tokens = await api.get<ApiToken[]>('/api/me/tokens');
    } catch (err) {
      notify.error(err);
    }
  }

  async function revoke(tk: ApiToken) {
    if (!(await confirmDialog({ title: t('settings.token_revoke_title'), message: t('settings.token_revoke_text', { name: tk.name }), confirmLabel: t('settings.token_revoke'), danger: true }))) return;
    try {
      await api.del(`/api/me/tokens/${tk.id}`);
      tokens = await api.get<ApiToken[]>('/api/me/tokens');
    } catch (err) {
      notify.error(err);
    }
  }

  async function copy(text: string) {
    if (await copyText(text)) notify.success(t('common.copied'));
    else notify.warning(t('common.copy_failed'));
  }

  let origin = typeof location !== 'undefined' ? location.origin : '';
  let curl = $derived(
    created
      ? `curl -X POST ${origin}/api/scans \\\n  -H "Authorization: Bearer ${created.token}" \\\n  -H "Content-Type: application/json" \\\n  -H "Idempotency-Key: $(date +%s)" \\\n  -d '{"profile_id": ${settings?.default_profile_id ?? 1}}'`
      : '',
  );
  let haYaml = $derived(
    created
      ? `# configuration.yaml\nrest_command:\n  scanbox_scan:\n    url: "${origin}/api/scans"\n    method: POST\n    headers:\n      Authorization: "Bearer ${created.token}"\n      Content-Type: "application/json"\n    payload: '{"profile_id": ${settings?.default_profile_id ?? 1}}'`
      : '',
  );
</script>

<div class="space-y-4">
  <h1 class="text-xl font-semibold">{t('settings.title')}</h1>
  {#if loadError}<p class="rounded-lg bg-red-50 p-3 text-sm text-red-800 dark:bg-red-950 dark:text-red-200" role="alert">{loadError}</p>{/if}

  <section class="card grid grid-cols-1 gap-3 p-4 sm:grid-cols-3">
    <h2 class="section-title sm:col-span-3">{t('settings.appearance')}</h2>
    <div><label class="label" for="lang">{t('settings.language')}</label>
      <select id="lang" class="input" value={i18n.locale} onchange={(e) => pref({ locale: (e.target as HTMLSelectElement).value as 'de' | 'en' })}>
        <option value="de">Deutsch</option><option value="en">English</option>
      </select></div>
    <div><label class="label" for="theme">{t('settings.theme')}</label>
      <select id="theme" class="input" value={session.user?.theme ?? 'system'} onchange={(e) => pref({ theme: (e.target as HTMLSelectElement).value as Theme })}>
        <option value="system">{t('settings.theme_system')}</option><option value="light">{t('settings.theme_light')}</option><option value="dark">{t('settings.theme_dark')}</option>
      </select></div>
    <form onsubmit={(e) => { e.preventDefault(); void pref({ display_name: displayName }); }}>
      <label class="label" for="dn">{t('settings.display_name')}</label>
      <div class="flex gap-2"><input id="dn" class="input" bind:value={displayName} maxlength="200" /><button class="btn btn-secondary" type="submit">{t('common.save')}</button></div>
    </form>
    <p class="text-xs text-slate-500 sm:col-span-3">{t('settings.signed_in_as', { user: session.user?.username ?? '' })}{session.user?.email ? ` · ${session.user.email}` : ''}</p>
  </section>

  <section class="card space-y-3 p-4">
    <h2 class="section-title">{t('settings.contacts')}</h2>
    <p class="hint">{t('settings.contacts_hint')}</p>
    <ul class="divide-y divide-slate-200 dark:divide-slate-800">
      {#each contacts as c (c.id)}
        <li class="flex flex-wrap items-center gap-2 py-2">
          <div class="min-w-0 flex-1"><p class="font-medium">{c.name || c.email}</p>{#if c.name}<p class="text-xs text-slate-500">{c.email}</p>{/if}</div>
          <label class="flex items-center gap-1 text-sm"><input type="checkbox" class="checkbox" checked={c.is_default} onchange={() => toggleDefault(c)} />{t('settings.contact_default')}</label>
          <button type="button" class="btn btn-ghost btn-icon" onclick={() => editContact(c)} aria-label={t('common.edit')}><Icon name="edit" /></button>
          <button type="button" class="btn btn-ghost btn-icon" onclick={() => deleteContact(c)} aria-label={t('common.delete')}><Icon name="trash" /></button>
        </li>
      {:else}
        <li class="py-2 text-sm text-slate-500">{t('settings.no_contacts')}</li>
      {/each}
    </ul>
    <form class="grid grid-cols-1 gap-2 sm:grid-cols-[1fr_1fr_auto_auto]" onsubmit={saveContact}>
      <input class="input" placeholder={t('settings.contact_name')} aria-label={t('settings.contact_name')} bind:value={cName} maxlength="200" />
      <input class="input" type="email" required placeholder="name@example.org" aria-label={t('settings.contact_email')} bind:value={cEmail} />
      <label class="flex items-center gap-2 text-sm"><input type="checkbox" class="checkbox" bind:checked={cDefault} />{t('settings.contact_default')}</label>
      <div class="flex gap-1">
        <button type="submit" class="btn btn-primary"><Icon name={editingContact ? 'check' : 'plus'} />{editingContact ? t('common.save') : t('common.add')}</button>
        {#if editingContact}<button type="button" class="btn btn-ghost" onclick={() => { editingContact = null; cName = cEmail = ''; cDefault = false; }}>{t('common.cancel')}</button>{/if}
      </div>
    </form>
  </section>

  <section class="card p-4">
    <form class="space-y-3" onsubmit={saveTemplates}>
      <h2 class="section-title">{t('settings.email_templates')}</h2>
      <div><label class="label" for="subj">{t('dest.email.subject')}</label><input id="subj" class="input" bind:value={subject} maxlength="300" placeholder="Scan: {'{title}'}" /></div>
      <div><label class="label" for="body">{t('dest.email.body')}</label><textarea id="body" class="input min-h-28" bind:value={body} maxlength="5000"></textarea>
        <p class="hint">{t('dest.template_vars')}</p></div>
      <button type="submit" class="btn btn-secondary">{t('common.save')}</button>
    </form>
  </section>

  <section class="card p-4">
    <form class="space-y-3" onsubmit={(e) => savePaperless(e)}>
      <h2 class="section-title">Paperless-ngx</h2>
      <p class="text-sm">{settings?.paperless_url ? t('settings.paperless_url', { url: settings.paperless_url }) : t('settings.paperless_no_url')}</p>
      <p class="flex items-center gap-2 text-sm">
        <Icon name={settings?.paperless_token_set ? 'check-circle' : 'x-circle'} size={16} />
        {settings?.paperless_token_set ? t('settings.token_set') : t('settings.token_not_set')}
      </p>
      <div><label class="label" for="pt">{t('settings.paperless_token')}</label>
        <input id="pt" class="input font-mono" type="password" autocomplete="off" bind:value={paperlessToken} placeholder={settings?.paperless_token_set ? '••••••••' : ''} maxlength="200" />
        <p class="hint">{t('settings.paperless_token_hint')}</p></div>
      <div class="flex gap-2">
        <button type="submit" class="btn btn-secondary" disabled={!paperlessToken.trim()}>{t('common.save')}</button>
        {#if settings?.paperless_token_set}<button type="button" class="btn btn-ghost" onclick={(e) => savePaperless(e as unknown as SubmitEvent, true)}>{t('settings.token_remove')}</button>{/if}
      </div>
    </form>
  </section>

  <section class="card space-y-3 p-4">
    <h2 class="section-title">{t('settings.api_tokens')}</h2>
    <p class="hint">{t('settings.api_tokens_hint')}</p>
    <ul class="divide-y divide-slate-200 dark:divide-slate-800">
      {#each tokens as tk (tk.id)}
        <li class="flex flex-wrap items-center gap-2 py-2 {tk.revoked ? 'opacity-50' : ''}">
          <Icon name="key" />
          <div class="min-w-0 flex-1">
            <p class="font-medium">{tk.name} <code class="text-xs">sbx_{tk.prefix}_…</code></p>
            <p class="text-xs text-slate-500">
              {t('settings.token_created', { date: fmtDateTime(tk.created_at) })} · {tk.last_used_at ? t('settings.token_used', { date: fmtDateTime(tk.last_used_at) }) : t('settings.token_never_used')}
              {#if tk.expires_at} · {t('settings.token_expires', { date: fmtDateTime(tk.expires_at) })}{/if}
              {#if tk.revoked} · {t('settings.token_revoked')}{/if}
            </p>
          </div>
          {#if !tk.revoked}<button type="button" class="btn btn-ghost btn-sm" onclick={() => revoke(tk)}>{t('settings.token_revoke')}</button>{/if}
        </li>
      {:else}
        <li class="py-2 text-sm text-slate-500">{t('settings.no_tokens')}</li>
      {/each}
    </ul>
    <form class="grid grid-cols-1 gap-2 sm:grid-cols-[1fr_10rem_auto]" onsubmit={createToken}>
      <input class="input" required maxlength="100" placeholder={t('settings.token_name')} aria-label={t('settings.token_name')} bind:value={tokenName} />
      <input class="input" type="number" min="1" max="3650" placeholder={t('settings.token_days')} aria-label={t('settings.token_days')} bind:value={tokenDays} />
      <button type="submit" class="btn btn-primary"><Icon name="plus" />{t('settings.token_create')}</button>
    </form>
  </section>
</div>

{#if created}
  <Modal bind:open={createdOpen} title={t('settings.token_created_title')} onclose={() => (created = null)}>
    <div class="space-y-3 text-sm">
      <p class="rounded bg-amber-50 p-2 text-amber-900 dark:bg-amber-950 dark:text-amber-100">{t('settings.token_once')}</p>
      <div class="flex gap-2">
        <input class="input font-mono" readonly value={created.token} aria-label={t('settings.api_tokens')} data-testid="new-token" />
        <button type="button" class="btn btn-secondary" onclick={() => copy(created!.token)}><Icon name="copy" />{t('common.copy')}</button>
      </div>
      <p class="font-semibold">{t('settings.curl_example')}</p>
      <pre class="overflow-x-auto rounded bg-slate-900 p-3 text-xs text-slate-100">{curl}</pre>
      <button type="button" class="btn btn-ghost btn-sm" onclick={() => copy(curl)}><Icon name="copy" size={16} />{t('common.copy')}</button>
      <p class="font-semibold">Home Assistant</p>
      <pre class="overflow-x-auto rounded bg-slate-900 p-3 text-xs text-slate-100">{haYaml}</pre>
      <button type="button" class="btn btn-ghost btn-sm" onclick={() => copy(haYaml)}><Icon name="copy" size={16} />{t('common.copy')}</button>
    </div>
    {#snippet footer()}<button type="button" class="btn btn-primary" onclick={() => { createdOpen = false; created = null; }}>{t('common.done')}</button>{/snippet}
  </Modal>
{/if}
