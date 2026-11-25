<script lang="ts">
  import Icon from '../lib/components/Icon.svelte';
  import Setup from './Setup.svelte';
  import { login, session } from '../lib/stores/session.svelte';
  import { router } from '../lib/router.svelte';
  import { isApiError, errorMessage } from '../lib/api/client';
  import { i18n, t, tMaybe } from '../lib/i18n/index.svelte';

  let username = $state('');
  let password = $state('');
  let busy = $state(false);
  let error = $state<string | null>(null);
  let rateLimited = $state(false);

  let urlError = $derived(router.query.get('error'));
  let urlErrorText = $derived(urlError ? tMaybe(`login.error.${urlError}`, t('login.error.sso_generic', { code: urlError })) : null);
  let next = $derived(router.query.get('next') ?? '/');
  let ssoHref = $derived(`/api/auth/oidc/login?next=${encodeURIComponent(next.startsWith('/') && !next.startsWith('//') ? next : '/')}`);

  async function submit(e: SubmitEvent) {
    e.preventDefault();
    busy = true;
    error = null;
    rateLimited = false;
    try {
      await login(username.trim(), password);
    } catch (err) {
      rateLimited = isApiError(err, 'rate_limited');
      error = isApiError(err, 'network') ? t('login.server_unreachable') : errorMessage(err);
      password = '';
    } finally {
      busy = false;
    }
  }
</script>

<div class="flex min-h-dvh items-center justify-center p-4">
  <div class="w-full max-w-sm">
    <div class="mb-6 flex flex-col items-center gap-2">
      <img src="/favicon.svg" alt="" class="size-16" />
      <h1 class="text-2xl font-bold">Scanbox</h1>
      <p class="text-sm text-slate-500 dark:text-slate-400">{t('login.subtitle')}</p>
    </div>

    <div class="card space-y-4 p-5">
      {#if session.authConfig?.setup_required}
        <Setup />
      {:else}
      {#if urlErrorText}
        <div class="flex gap-2 rounded-lg bg-red-50 p-3 text-sm text-red-800 dark:bg-red-950 dark:text-red-200" role="alert">
          <Icon name="alert" size={18} />{urlErrorText}
        </div>
      {/if}

      {#if session.authConfig?.oidc_enabled}
        <a href={ssoHref} data-external class="btn btn-primary btn-lg w-full">
          <Icon name="key" />{t('login.sso', { label: session.authConfig.oidc_label })}
        </a>
        <div class="flex items-center gap-3 text-xs text-slate-500"><span class="h-px flex-1 bg-slate-200 dark:bg-slate-700"></span>{t('login.or_local')}<span class="h-px flex-1 bg-slate-200 dark:bg-slate-700"></span></div>
      {/if}

      <form class="space-y-3" onsubmit={submit}>
        <div>
          <label class="label" for="username">{t('login.username')}</label>
          <input id="username" name="username" class="input" autocomplete="username" autocapitalize="none" required bind:value={username} />
        </div>
        <div>
          <label class="label" for="password">{t('login.password')}</label>
          <input id="password" name="password" type="password" class="input" autocomplete="current-password" required bind:value={password} />
        </div>
        {#if error}
          <div class="flex gap-2 rounded-lg p-3 text-sm {rateLimited ? 'bg-amber-50 text-amber-900 dark:bg-amber-950 dark:text-amber-100' : 'bg-red-50 text-red-800 dark:bg-red-950 dark:text-red-200'}" role="alert" data-testid="login-error">
            <Icon name={rateLimited ? 'clock' : 'alert'} size={18} />
            <span>{error}{#if rateLimited}<br /><span class="text-xs">{t('login.rate_limited_hint')}</span>{/if}</span>
          </div>
        {/if}
        <button type="submit" class="btn {session.authConfig?.oidc_enabled ? 'btn-secondary' : 'btn-primary'} btn-lg w-full" disabled={busy}>
          {#if busy}<Icon name="loader" />{/if}{t('login.submit')}
        </button>
      </form>
      {/if}
    </div>

    <div class="mt-4 flex justify-center gap-2 text-sm">
      <button type="button" class="btn btn-ghost btn-sm" class:font-bold={i18n.locale === 'de'} onclick={() => (i18n.locale = 'de')}>Deutsch</button>
      <button type="button" class="btn btn-ghost btn-sm" class:font-bold={i18n.locale === 'en'} onclick={() => (i18n.locale = 'en')}>English</button>
    </div>
  </div>
</div>
