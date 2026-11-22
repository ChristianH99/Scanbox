<script lang="ts">
  import Icon from '../lib/components/Icon.svelte';
  import { api, errorMessage } from '../lib/api/client';
  import type { Profile } from '../lib/api/types';
  import { session } from '../lib/stores/session.svelte';
  import { notify } from '../lib/stores/toast.svelte';
  import { confirmDialog } from '../lib/stores/dialog.svelte';
  import { navigate } from '../lib/router.svelte';
  import { t, tMaybe } from '../lib/i18n/index.svelte';

  let profiles = $state<Profile[]>([]);
  let loading = $state(true);
  let error = $state<string | null>(null);

  async function load() {
    try {
      profiles = await api.get<Profile[]>('/api/profiles');
      error = null;
    } catch (e) {
      error = errorMessage(e);
    } finally {
      loading = false;
    }
  }
  $effect(() => void load());

  async function duplicate(p: Profile) {
    try {
      const c = await api.post<Profile>('/api/profiles', { name: `${p.name} (${t('profiles.copy_suffix')})`.slice(0, 100), description: p.description, settings: p.settings });
      notify.success(t('profiles.duplicated'));
      await navigate(`/profiles/${c.id}`);
    } catch (e) {
      notify.error(e);
    }
  }

  async function remove(p: Profile) {
    const ok = await confirmDialog({ title: t('profiles.delete_title'), message: t('profiles.delete_text', { name: p.name }), confirmLabel: t('common.delete'), danger: true });
    if (!ok) return;
    try {
      await api.del(`/api/profiles/${p.id}`);
      notify.success(t('profiles.deleted'));
      await load();
    } catch (e) {
      notify.error(e);
    }
  }

  let canEdit = (p: Profile) => !p.shared || !!session.user?.is_admin;
  let canDelete = (p: Profile) => canEdit(p) && !p.builtin;
</script>

<div class="space-y-4">
  <div class="flex flex-wrap items-center gap-2">
    <h1 class="flex-1 text-xl font-semibold">{t('profiles.title')}</h1>
    <a href="/profiles/new" class="btn btn-primary"><Icon name="plus" />{t('profiles.new')}</a>
  </div>
  {#if error}<p class="rounded-lg bg-red-50 p-3 text-sm text-red-800 dark:bg-red-950 dark:text-red-200" role="alert">{error}</p>{/if}
  {#if loading}<p class="flex items-center gap-2"><Icon name="loader" />{t('common.loading')}</p>{/if}

  <ul class="grid gap-3 md:grid-cols-2">
    {#each profiles as p (p.id)}
      <li class="card flex flex-col gap-2 p-4">
        <div class="flex items-start gap-2">
          <div class="min-w-0 flex-1">
            <a href="/profiles/{p.id}" class="font-semibold hover:underline">{p.name}</a>
            {#if p.description}<p class="text-sm text-slate-600 dark:text-slate-400">{p.description}</p>{/if}
          </div>
          {#if p.shared}<span class="chip bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300">{t('profiles.shared')}</span>{/if}
        </div>
        <p class="flex flex-wrap gap-1 text-xs">
          <span class="chip bg-slate-100 dark:bg-slate-800">{tMaybe(`source.${p.settings.source}`, p.settings.source)}</span>
          <span class="chip bg-slate-100 dark:bg-slate-800">{p.settings.resolution} dpi</span>
          <span class="chip bg-slate-100 dark:bg-slate-800">{tMaybe(`color.${p.settings.color_mode}`, p.settings.color_mode)}</span>
          <span class="chip bg-slate-100 dark:bg-slate-800">{tMaybe(`format.${p.settings.output.format}`, p.settings.output.format)}</span>
          {#if p.settings.ocr.enabled}<span class="chip bg-slate-100 dark:bg-slate-800">OCR</span>{/if}
          {#if p.settings.qr.enabled}<span class="chip bg-slate-100 dark:bg-slate-800">QR</span>{/if}
          {#each p.settings.destinations.filter((d) => d.enabled) as d}<span class="chip bg-brand-50 text-brand-800 dark:bg-brand-800/30 dark:text-brand-100">{d.label || tMaybe(`dest.${d.type}`, d.type)}</span>{/each}
        </p>
        {#if p.problems.length}
          <div class="rounded bg-amber-50 p-2 text-xs text-amber-900 dark:bg-amber-950 dark:text-amber-100" role="alert">
            <p class="font-semibold">{t('profiles.problems')}</p>
            <ul class="list-disc pl-4">{#each p.problems as pr}<li>{pr}</li>{/each}</ul>
          </div>
        {/if}
        <div class="mt-auto flex flex-wrap gap-2 pt-1">
          <a href="/profiles/{p.id}" class="btn btn-secondary btn-sm"><Icon name={canEdit(p) ? 'edit' : 'eye'} size={16} />{canEdit(p) ? t('common.edit') : t('common.view')}</a>
          <button type="button" class="btn btn-ghost btn-sm" onclick={() => duplicate(p)}><Icon name="copy" size={16} />{t('profiles.duplicate')}</button>
          {#if canDelete(p)}
            <button type="button" class="btn btn-ghost btn-sm" onclick={() => remove(p)}><Icon name="trash" size={16} />{t('common.delete')}</button>
          {/if}
        </div>
      </li>
    {/each}
  </ul>
</div>
