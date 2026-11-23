<script lang="ts">
  import { untrack } from 'svelte';
  import Icon from '../lib/components/Icon.svelte';
  import StatusBadge from '../lib/components/StatusBadge.svelte';
  import DocumentAlerts from '../lib/components/DocumentAlerts.svelte';
  import PageGrid from '../lib/components/PageGrid.svelte';
  import PageEditor from '../lib/components/PageEditor.svelte';
  import QrPanel from '../lib/components/QrPanel.svelte';
  import DocumentPicker from '../lib/components/DocumentPicker.svelte';
  import { LiveDocument } from '../lib/liveDocument.svelte';
  import { artifactUrl, docApi, downloadUrl, orderedPages, rotated } from '../lib/docs';
  import { api, isApiError } from '../lib/api/client';
  import type { DocumentDetail, Page, Profile } from '../lib/api/types';
  import { session } from '../lib/stores/session.svelte';
  import { notify } from '../lib/stores/toast.svelte';
  import { confirmDialog } from '../lib/stores/dialog.svelte';
  import { navigate } from '../lib/router.svelte';
  import { fmtBytes, fmtDateTime, t, tMaybe } from '../lib/i18n/index.svelte';
  import { EDITABLE_STATUSES } from '../lib/utils/status';
  import { moveItems } from '../lib/utils/misc';

  let { id }: { id: number } = $props();

  const live = new LiveDocument(untrack(() => id));
  $effect(() => live.start());

  let doc = $derived(live.doc);
  let pages = $derived(doc ? orderedPages(doc) : []);
  let isInbox = $derived(!!doc && doc.owner_id === null);
  let isOwner = $derived(!!doc && doc.owner_id === session.user?.id);
  let editable = $derived(!!doc && isOwner && EDITABLE_STATUSES.has(doc.status) && !doc.raw_expired);
  let selected = $state<Set<string>>(new Set());
  let busy = $state<string | null>(null);
  let editorIndex = $state<number | null>(null);
  let editorOpen = $state(false);
  let pickerMode = $state<'insert' | 'merge'>('insert');
  let pickerOpen = $state(false);
  let profiles = $state<Profile[]>([]);
  let title = $state('');
  let docDate = $state('');
  let splitChildren = $state<number[]>([]);
  let qrPanel: HTMLElement | null = null;

  // Keep the metadata inputs in sync with the server copy unless the user is typing.
  let lastSyncedTitle: string | null = null;
  let lastSyncedDate: string | null = null;
  $effect(() => {
    if (!doc) return;
    if (doc.title !== lastSyncedTitle) {
      lastSyncedTitle = doc.title;
      title = doc.title;
    }
    if ((doc.document_date ?? '') !== lastSyncedDate) {
      lastSyncedDate = doc.document_date ?? '';
      docDate = doc.document_date ?? '';
    }
  });

  // Drop selections of pages that no longer exist.
  $effect(() => {
    const ids = new Set(pages.map((p) => p.id));
    if ([...selected].some((x) => !ids.has(x))) selected = new Set([...selected].filter((x) => ids.has(x)));
  });

  $effect(() => {
    if (isOwner && !profiles.length) api.get<Profile[]>('/api/profiles').then((p) => (profiles = p)).catch(() => {});
  });

  async function act(name: string, fn: () => Promise<DocumentDetail | void>, ok?: string) {
    busy = name;
    try {
      const r = await fn();
      if (r) live.set(r);
      if (ok) notify.success(ok);
    } catch (e) {
      notify.error(e);
      void live.load();
    } finally {
      busy = null;
    }
  }

  async function claim() {
    busy = 'claim';
    try {
      live.set(await docApi.claim(id));
      notify.success(t('inbox.claimed'));
    } catch (e) {
      if (isApiError(e, 'already_claimed') || (isApiError(e) && e.status === 404)) {
        notify.warning(t('inbox.already_claimed'));
        void navigate('/inbox');
      } else notify.error(e);
    } finally {
      busy = null;
    }
  }

  async function discard() {
    const ok = await confirmDialog({ title: t('doc.discard_confirm_title'), message: t('doc.discard_confirm_text'), confirmLabel: t('doc.discard'), danger: true });
    if (ok) await act('discard', () => docApi.discard(id));
  }

  function reorder(ids: string[]) {
    if (!doc) return;
    // optimistic
    const byId = new Map(doc.pages.map((p) => [p.id, p]));
    live.set({ ...doc, pages: ids.map((pid, i) => ({ ...byId.get(pid)!, position: i + 1 })) });
    void act('reorder', () => docApi.reorder(id, ids));
  }

  function moveSelection(delta: -1 | 1) {
    const ids = pages.map((p) => p.id);
    const idx = ids.map((x, i) => (selected.has(x) ? i : -1)).filter((i) => i >= 0);
    if (!idx.length) return;
    const target = delta < 0 ? Math.max(0, idx[0] - 1) : Math.min(ids.length, idx[idx.length - 1] + 2);
    reorder(moveItems(ids, selected, target));
  }

  async function rotateSelection(delta: 90 | -90) {
    const sel = pages.filter((p) => selected.has(p.id));
    await act('rotate', async () => {
      await Promise.all(sel.map((p) => docApi.patchPage(p.id, { edits: rotated(p.edits ?? {}, delta, Number((p.analysis as Record<string, unknown>)?.osd_rotate ?? 0)) })));
      return docApi.get(id);
    });
  }

  async function split() {
    const starts = pages.filter((p, i) => i > 0 && selected.has(p.id)).map((p) => p.id);
    if (!starts.length) {
      notify.info(t('editor.split_need_selection'));
      return;
    }
    const ok = await confirmDialog({ title: t('editor.split_title'), message: t('editor.split_text', { n: starts.length + 1 }), confirmLabel: t('editor.split') });
    if (!ok) return;
    busy = 'split';
    try {
      const r = await docApi.split(id, starts);
      splitChildren = r.children;
      selected = new Set();
      await live.load();
      notify.success(t('editor.split_done', { n: r.children.length }));
    } catch (e) {
      notify.error(e);
    } finally {
      busy = null;
    }
  }

  async function onPicked(r: { docIds: number[]; pageIds: string[]; copy: boolean }) {
    if (pickerMode === 'merge') live.set(await docApi.merge(id, r.docIds));
    else live.set(await docApi.insertPages(id, r.docIds[0], r.pageIds, r.copy));
    notify.success(t('common.done'));
  }

  function openEditor(_p: Page, i: number) {
    editorIndex = i;
    editorOpen = true;
  }

  function onPageSaved(p: Page) {
    if (!doc) return;
    live.set({ ...doc, pages: doc.pages.map((x) => (x.id === p.id ? p : x)) });
    void live.load();
  }

  function showQr() {
    qrPanel = document.getElementById('qr');
    qrPanel?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    qrPanel?.querySelector<HTMLInputElement>('#qrval')?.focus();
  }

  let selCount = $derived(selected.size);
  let selDeleted = $derived(pages.filter((p) => selected.has(p.id) && p.deleted).length);
  let canFinish = $derived(!!doc && isOwner && ['stored', 'needs_attention', 'failed', 'done'].includes(doc.status) && doc.page_count > 0 && !doc.raw_expired);
  let titleDirty = $derived(!!doc && (title.trim() !== doc.title || (docDate || '') !== (doc.document_date ?? '')));
  let warnings = $derived(doc ? doc.events.filter((e) => e.level === 'warning' || e.level === 'warn').slice(-3) : []);
</script>

{#if !doc}
  <div class="card p-6">
    {#if live.error}
      <h1 class="text-lg font-semibold">{live.errorCode === 'not_found' ? t('doc.not_found') : t('errors.generic')}</h1>
      <p class="mt-1 text-sm">{live.error}</p>
      <div class="mt-4 flex gap-2">
        <a class="btn btn-secondary" href="/history">{t('nav.history')}</a>
        <button type="button" class="btn btn-ghost" onclick={() => live.load()}>{t('common.retry')}</button>
      </div>
    {:else}
      <p class="flex items-center gap-2"><Icon name="loader" />{t('common.loading')}</p>
    {/if}
  </div>
{:else}
  <div class="space-y-4" data-testid="document-page" data-status={doc.status}>
    <!-- Header -->
    <div class="flex flex-wrap items-start gap-3">
      <button type="button" class="btn btn-ghost btn-icon" onclick={() => history.back()} aria-label={t('common.back')}><Icon name="arrow-left" /></button>
      <div class="min-w-0 flex-1">
        <h1 class="truncate text-xl font-semibold">{doc.title || doc.filename || `#${doc.id}`}</h1>
        <p class="text-sm text-slate-500 dark:text-slate-400">
          #{doc.id} · {fmtDateTime(doc.created_at)} · {doc.profile_name} · {t('doc.pages_count', { n: doc.page_count })}
          {#if doc.origin !== 'app'}· {tMaybe(`origin.${doc.origin}`, doc.origin)}{/if}
        </p>
      </div>
      <StatusBadge status={doc.status} />
    </div>

    {#if isInbox}
      <div class="rounded-xl border-2 border-sky-300 bg-sky-50 p-4 dark:border-sky-800 dark:bg-sky-950/60" data-testid="claim-banner">
        <p class="font-semibold">{t('inbox.item_title')}</p>
        <p class="text-sm">{t('inbox.claim_hint')}</p>
        {#if doc.inbox_expires_at}<p class="text-xs text-slate-600 dark:text-slate-400">{t('inbox.expires', { date: fmtDateTime(doc.inbox_expires_at) })}</p>{/if}
        <button type="button" class="btn btn-primary btn-lg mt-3" disabled={busy === 'claim'} onclick={claim} data-testid="claim">
          {#if busy === 'claim'}<Icon name="loader" />{:else}<Icon name="check" />{/if}{t('inbox.claim')}
        </button>
      </div>
    {/if}

    {#if doc.raw_expired || doc.processed_expired}
      <p class="flex items-center gap-2 rounded-lg bg-slate-100 p-3 text-sm dark:bg-slate-800">
        <Icon name="clock" />{doc.raw_expired ? t('doc.raw_expired') : t('doc.processed_expired')}
      </p>
    {/if}

    <DocumentAlerts {doc} onchange={(d) => live.set(d)} onqr={showQr} />

    {#if doc.status === 'processing'}
      <div class="card p-4" role="status">
        <p class="flex items-center gap-2 text-sm font-medium"><Icon name="loader" />
          {live.progress ? t('scan.processing_progress', { done: live.progress.done, total: live.progress.total }) : t('scan.processing')}</p>
      </div>
    {:else if doc.status === 'scanning' || doc.status === 'queued'}
      <div class="card p-4" role="status">
        <p class="flex items-center gap-2 text-sm font-medium"><Icon name="loader" />
          {doc.status === 'queued' ? (doc.queue_position ? t('scan.queued_position', { n: doc.queue_position }) : t('scan.queued')) : t('scan.scanning', { n: doc.page_count })}</p>
      </div>
    {:else if doc.status === 'delivering'}
      <div class="card p-4" role="status"><p class="flex items-center gap-2 text-sm font-medium"><Icon name="send" />{t('scan.delivering')}</p></div>
    {/if}

    {#if splitChildren.length}
      <div class="card p-4 text-sm">
        <p class="font-semibold">{t('editor.split_children')}</p>
        <ul class="mt-1 flex flex-wrap gap-2">{#each splitChildren as c}<li><a class="btn btn-secondary btn-sm" href="/documents/{c}">#{c}</a></li>{/each}</ul>
      </div>
    {/if}

    <!-- Primary actions -->
    {#if isOwner}
      <div class="flex flex-wrap gap-2">
        {#if doc.status === 'queued' || doc.status === 'scanning'}
          <button type="button" class="btn btn-secondary btn-lg" disabled={!!busy} onclick={() => act('cancel', () => docApi.cancel(id))} data-testid="doc-stop-scan">
            {#if busy === 'cancel'}<Icon name="loader" />{:else}<Icon name="x-circle" />{/if}{t('scan.stop')}
          </button>
        {/if}
        {#if canFinish}
          <button type="button" class="btn btn-primary btn-lg" disabled={!!busy} onclick={() => act('finish', () => docApi.finish(id), t('doc.finish_started'))} data-testid="doc-finish">
            {#if busy === 'finish'}<Icon name="loader" />{:else}<Icon name={doc.status === 'done' ? 'refresh' : 'check'} />{/if}
            {doc.status === 'done' ? t('doc.reprocess') : t('scan.finish')}
          </button>
        {/if}
        {#if ['stored', 'needs_attention', 'failed'].includes(doc.status) && !doc.raw_expired}
          <button type="button" class="btn btn-secondary btn-lg" disabled={!!busy} onclick={() => act('batch', () => docApi.addBatch(id))}>
            {#if busy === 'batch'}<Icon name="loader" />{:else}<Icon name="plus" />{/if}{t('scan.add_stack')}
          </button>
        {/if}
        {#if doc.status === 'done' && doc.artifacts.length && !doc.processed_expired}
          <a class="btn btn-secondary btn-lg" href={downloadUrl(id)} download data-testid="doc-download"><Icon name="download" />{t('doc.download')}</a>
        {/if}
        {#if ['done', 'needs_attention'].includes(doc.status)}
          <button type="button" class="btn btn-secondary btn-lg" disabled={!!busy} onclick={() => act('resend', () => docApi.resend(id), t('doc.resend_started'))}>
            <Icon name="send" />{t('doc.resend')}
          </button>
        {/if}
        {#if !['scanning', 'discarded', 'expired'].includes(doc.status)}
          <button type="button" class="btn btn-ghost btn-lg" disabled={!!busy} onclick={discard}><Icon name="trash" />{t('doc.discard')}</button>
        {/if}
      </div>
    {/if}

    <div class="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)] lg:items-start">
      <div class="space-y-4">
        <!-- Pages -->
        <section class="card space-y-3 p-4">
          <div class="flex flex-wrap items-center gap-2">
            <h2 class="section-title flex-1">{t('doc.pages')}</h2>
            {#if editable && pages.length}
              <button type="button" class="btn btn-ghost btn-sm" onclick={() => (selected = selCount === pages.length ? new Set() : new Set(pages.map((p) => p.id)))}>
                {selCount === pages.length ? t('editor.select_none') : t('editor.select_all')}
              </button>
            {/if}
          </div>

          {#if editable}
            <div class="flex flex-wrap gap-1.5" role="toolbar" aria-label={t('editor.toolbar')}>
              <span class="self-center text-sm text-slate-600 dark:text-slate-400" aria-live="polite">{t('editor.selected_n', { n: selCount })}</span>
              <button type="button" class="btn btn-secondary btn-sm" disabled={!selCount || !!busy} onclick={() => rotateSelection(-90)} title={t('editor.rotate_left')}><Icon name="rotate-ccw" size={16} /><span class="sr-only sm:not-sr-only">{t('editor.rotate_left')}</span></button>
              <button type="button" class="btn btn-secondary btn-sm" disabled={!selCount || !!busy} onclick={() => rotateSelection(90)} title={t('editor.rotate_right')}><Icon name="rotate-cw" size={16} /><span class="sr-only sm:not-sr-only">{t('editor.rotate_right')}</span></button>
              <button type="button" class="btn btn-secondary btn-sm" disabled={!selCount || !!busy} onclick={() => moveSelection(-1)} title={t('editor.move_earlier')}><Icon name="chevron-left" size={16} /><span class="sr-only sm:not-sr-only">{t('editor.move_earlier')}</span></button>
              <button type="button" class="btn btn-secondary btn-sm" disabled={!selCount || !!busy} onclick={() => moveSelection(1)} title={t('editor.move_later')}><Icon name="chevron-right" size={16} /><span class="sr-only sm:not-sr-only">{t('editor.move_later')}</span></button>
              {#if selDeleted}
                <button type="button" class="btn btn-secondary btn-sm" disabled={!!busy} onclick={() => act('restore', () => docApi.restorePages(id, [...selected]))}><Icon name="undo" size={16} />{t('editor.restore')}</button>
              {/if}
              <button type="button" class="btn btn-secondary btn-sm" disabled={!selCount || selDeleted === selCount || !!busy} onclick={() => act('delete', () => docApi.deletePages(id, [...selected].filter((x) => !pages.find((p) => p.id === x)?.deleted)))} data-testid="delete-pages"><Icon name="trash" size={16} />{t('editor.delete')}</button>
              <button type="button" class="btn btn-secondary btn-sm" disabled={!selCount || !!busy} onclick={split}><Icon name="scissors" size={16} />{t('editor.split')}</button>
              <button type="button" class="btn btn-secondary btn-sm" disabled={!!busy} onclick={() => { pickerMode = 'insert'; pickerOpen = true; }}><Icon name="plus" size={16} />{t('editor.insert')}</button>
              <button type="button" class="btn btn-secondary btn-sm" disabled={!!busy} onclick={() => { pickerMode = 'merge'; pickerOpen = true; }}><Icon name="merge" size={16} />{t('editor.merge')}</button>
            </div>
            <p class="hint">{t('editor.grid_hint')}</p>
          {/if}

          {#if pages.length}
            <PageGrid {pages} bind:selected {editable} fresh={live.fresh} onreorder={reorder} onopen={openEditor} />
          {:else}
            <p class="text-sm text-slate-500">{t('doc.no_pages')}</p>
          {/if}
        </section>

        <!-- Deliveries -->
        {#if doc.deliveries.length || doc.artifacts.length}
          <section class="card space-y-3 p-4" data-testid="deliveries">
            <h2 class="section-title">{t('doc.deliveries')}</h2>
            <ul class="divide-y divide-slate-200 dark:divide-slate-800">
              {#each doc.deliveries as dl (dl.id)}
                <li class="flex flex-wrap items-center gap-2 py-2">
                  <Icon name={dl.destination_type === 'email' ? 'mail' : dl.destination_type === 'folder' ? 'folder' : dl.destination_type === 'paperless' ? 'file' : 'download'} />
                  <div class="min-w-0 flex-1">
                    <p class="font-medium">{dl.label && dl.label !== dl.destination_type ? dl.label : tMaybe(`dest.${dl.destination_type}`, dl.destination_type)}</p>
                    <p class="text-xs text-slate-500">{t('delivery.attempts', { n: dl.attempts })}{#if dl.delivered_at} · {fmtDateTime(dl.delivered_at)}{/if}</p>
                    {#if dl.last_error}<p class="text-sm break-words text-red-700 dark:text-red-300">{dl.error_code ? tMaybe(`errors.${dl.error_code}`, '') : ''} {dl.last_error}</p>{/if}
                    {#if dl.result && dl.status === 'done'}
                      {@const r = dl.result as Record<string, unknown>}
                      {#if r.message || r.dir}<p class="text-xs break-all text-slate-500">{r.message ?? r.dir}</p>{/if}
                    {/if}
                  </div>
                  <StatusBadge status={dl.status} kind="delivery" />
                  {#if isOwner && (dl.status === 'needs_attention' || dl.status === 'failed')}
                    <button type="button" class="btn btn-secondary btn-sm" disabled={!!busy || doc.processed_expired} onclick={() => act(`dl${dl.id}`, () => docApi.retryDelivery(id, dl.id))}>
                      <Icon name="refresh" size={16} />{t('common.retry')}
                    </button>
                  {/if}
                </li>
              {/each}
            </ul>
            {#if doc.artifacts.length && isOwner}
              <h3 class="text-sm font-semibold">{t('doc.files')}</h3>
              <ul class="space-y-1">
                {#each doc.artifacts as a (a.id)}
                  <li class="flex items-center gap-2 text-sm">
                    <Icon name="file" size={16} />
                    <a class="min-w-0 flex-1 truncate underline" href={artifactUrl(id, a.id, true)} target="_blank" rel="noopener">{a.filename}</a>
                    <span class="text-xs text-slate-500">{fmtBytes(a.size)}</span>
                    <a class="btn btn-ghost btn-icon" href={artifactUrl(id, a.id)} download aria-label={t('doc.download_file', { name: a.filename })}><Icon name="download" size={18} /></a>
                  </li>
                {/each}
              </ul>
              {#if doc.processed_expired}<p class="hint">{t('doc.processed_expired')}</p>{/if}
            {/if}
            {#if doc.has_ocr_text && isOwner}
              <a class="btn btn-ghost btn-sm" href="/api/documents/{id}/text" target="_blank" rel="noopener"><Icon name="file" size={16} />{t('doc.ocr_text')}</a>
            {/if}
          </section>
        {/if}
      </div>

      <div class="space-y-4">
        <!-- Metadata -->
        {#if isOwner}
          <section class="card space-y-3 p-4">
            <h2 class="section-title">{t('doc.details')}</h2>
            <form class="space-y-3" onsubmit={(e) => {
              e.preventDefault();
              void act('meta', () => docApi.patch(id, { title: title.trim(), document_date: docDate || '' }), t('common.saved'));
            }}>
              <div>
                <label class="label" for="doctitle">{t('doc.title')}</label>
                <input id="doctitle" class="input" bind:value={title} maxlength="300" disabled={!editable && doc.status !== 'queued'} />
                {#if doc.title_suggestion && doc.title_suggestion !== title}
                  <button type="button" class="mt-1 text-left text-xs text-brand-700 underline dark:text-brand-500" onclick={() => (title = doc!.title_suggestion ?? title)}>
                    {t('doc.use_suggestion', { s: doc.title_suggestion })}
                  </button>
                {/if}
              </div>
              <div>
                <label class="label" for="docdate">{t('doc.date')}</label>
                <input id="docdate" type="date" class="input" bind:value={docDate} disabled={!editable} />
              </div>
              {#if titleDirty}<button type="submit" class="btn btn-primary btn-sm" disabled={busy === 'meta'}>{t('common.save')}</button>{/if}
            </form>
            {#if doc.filename}<p class="text-sm break-all"><span class="text-slate-500">{t('doc.filename')}:</span> {doc.filename}</p>{/if}
            {#if editable && profiles.length}
              <div>
                <label class="label" for="docprofile">{t('scan.profile')}</label>
                <select id="docprofile" class="input" value={doc.profile_id} onchange={(e) => act('profile', () => docApi.patch(id, { profile_id: Number((e.target as HTMLSelectElement).value) }), t('doc.profile_changed'))}>
                  {#if !profiles.some((p) => p.id === doc!.profile_id)}<option value={doc.profile_id}>{doc.profile_name}</option>{/if}
                  {#each profiles as p (p.id)}<option value={p.id}>{p.name}</option>{/each}
                </select>
                <p class="hint">{t('doc.profile_hint')}</p>
              </div>
            {/if}
            {#if warnings.length}
              <ul class="space-y-1 text-sm text-amber-800 dark:text-amber-200">
                {#each warnings as w (w.id)}<li class="flex gap-1"><Icon name="alert" size={16} />{w.message}</li>{/each}
              </ul>
            {/if}
          </section>

          <QrPanel {doc} {editable} onchange={(d) => live.set(d)} />
        {/if}

        <!-- Timeline -->
        <section class="card p-4">
          <h2 class="section-title mb-2">{t('doc.timeline')}</h2>
          <ol class="space-y-2 border-l-2 border-slate-200 pl-4 dark:border-slate-700">
            {#each [...doc.events].reverse() as ev (ev.id)}
              <li class="relative text-sm">
                <span class="absolute top-1.5 -left-[21px] size-2.5 rounded-full {ev.level === 'error' ? 'bg-red-500' : ev.level.startsWith('warn') ? 'bg-amber-500' : 'bg-slate-400'}"></span>
                <p class="break-words">{ev.message || ev.code}</p>
                <p class="text-xs text-slate-500">{fmtDateTime(ev.created_at)} · <code>{ev.code}</code></p>
              </li>
            {:else}
              <li class="text-sm text-slate-500">–</li>
            {/each}
          </ol>
        </section>
      </div>
    </div>
  </div>

  {#if editorOpen && editorIndex !== null && pages[editorIndex]}
    <PageEditor
      bind:open={editorOpen}
      page={pages[editorIndex]}
      index={editorIndex}
      total={pages.length}
      profile={doc.profile_snapshot}
      {editable}
      onsaved={onPageSaved}
      onnavigate={(d) => (editorIndex = Math.min(pages.length - 1, Math.max(0, (editorIndex ?? 0) + d)))}
    />
  {/if}

  <DocumentPicker bind:open={pickerOpen} mode={pickerMode} excludeId={id} onconfirm={onPicked} />
{/if}
