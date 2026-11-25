<script lang="ts">
  import Icon from '../lib/components/Icon.svelte';
  import { completeSetup, session } from '../lib/stores/session.svelte';
  import { isApiError, errorMessage } from '../lib/api/client';
  import { t, tMaybe } from '../lib/i18n/index.svelte';

  const MIN_PASSWORD = 8;

  let code = $state('');
  let username = $state(session.authConfig?.default_admin_username ?? 'admin');
  let password = $state('');
  let confirm = $state('');
  let busy = $state(false);
  let error = $state<string | null>(null);

  let mismatch = $derived(confirm.length > 0 && confirm !== password);

  async function submit(e: SubmitEvent) {
    e.preventDefault();
    if (password !== confirm) return;
    busy = true;
    error = null;
    try {
      await completeSetup(code.trim(), username.trim(), password);
    } catch (err) {
      if (isApiError(err, 'network')) error = t('login.server_unreachable');
      else if (isApiError(err, 'setup_closed')) {
        // Someone else finished setup meanwhile: back to the normal login.
        if (session.authConfig) session.authConfig.setup_required = false;
      } else error = isApiError(err) ? tMaybe(`setup.error.${err.code}`, errorMessage(err)) : errorMessage(err);
    } finally {
      busy = false;
    }
  }
</script>

<form class="space-y-3" onsubmit={submit} data-testid="setup-form">
  <div>
    <h2 class="text-lg font-semibold">{t('setup.title')}</h2>
    <p class="hint mt-1">{t('setup.intro')}</p>
  </div>
  <div>
    <label class="label" for="setup-code">{t('setup.code')}</label>
    <input id="setup-code" name="code" class="input font-mono uppercase" autocomplete="one-time-code" autocapitalize="characters"
      spellcheck="false" placeholder="XXXX-XXXX-XXXX" required maxlength="50" bind:value={code} />
    <p class="hint">{t('setup.code_hint')}</p>
  </div>
  <div>
    <label class="label" for="setup-username">{t('login.username')}</label>
    <input id="setup-username" name="username" class="input" autocomplete="username" autocapitalize="none" required
      maxlength="150" pattern="[A-Za-z0-9._@\-]+" bind:value={username} />
  </div>
  <div>
    <label class="label" for="setup-password">{t('setup.password')}</label>
    <input id="setup-password" name="password" type="password" class="input" autocomplete="new-password" required
      minlength={MIN_PASSWORD} maxlength="500" bind:value={password} />
    <p class="hint">{t('setup.password_hint', { n: MIN_PASSWORD })}</p>
  </div>
  <div>
    <label class="label" for="setup-confirm">{t('setup.password_confirm')}</label>
    <input id="setup-confirm" name="confirm" type="password" class="input" autocomplete="new-password" required
      maxlength="500" bind:value={confirm} aria-invalid={mismatch} />
    {#if mismatch}<p class="mt-1 text-xs text-red-700 dark:text-red-300">{t('setup.password_mismatch')}</p>{/if}
  </div>
  {#if error}
    <div class="flex gap-2 rounded-lg bg-red-50 p-3 text-sm text-red-800 dark:bg-red-950 dark:text-red-200" role="alert" data-testid="setup-error">
      <Icon name="alert" size={18} /><span>{error}</span>
    </div>
  {/if}
  <button type="submit" class="btn btn-primary btn-lg w-full" disabled={busy || mismatch}>
    {#if busy}<Icon name="loader" />{/if}{t('setup.submit')}
  </button>
</form>
