<script lang="ts">
  import { buildFilename, extensionFor, FILENAME_TOKENS, unknownTokens } from '../utils/filename';
  import { t } from '../i18n/index.svelte';

  let {
    value = $bindable(''),
    id,
    label,
    format = 'pdf',
    addQr = false,
    profileName = '',
    userName = '',
    showExample = true,
    tokens = FILENAME_TOKENS,
    disabled = false,
  }: {
    value?: string;
    id: string;
    label: string;
    format?: string;
    addQr?: boolean;
    profileName?: string;
    userName?: string;
    showExample?: boolean;
    tokens?: readonly string[];
    disabled?: boolean;
  } = $props();

  let input: HTMLInputElement | undefined = $state();

  function insert(token: string) {
    const tok = `{${token}}`;
    if (!input) {
      value += tok;
      return;
    }
    const s = input.selectionStart ?? value.length;
    const e = input.selectionEnd ?? value.length;
    value = value.slice(0, s) + tok + value.slice(e);
    requestAnimationFrame(() => {
      input?.focus();
      input?.setSelectionRange(s + tok.length, s + tok.length);
    });
  }

  let example = $derived(
    buildFilename(
      value,
      { created: new Date(), qr: 'INV-2024-0042', title: t('filename.example_title'), counter: 42, profile: profileName, user: userName, pages: 3 },
      extensionFor(format),
      addQr,
    ),
  );
  let unknown = $derived(unknownTokens(value));
</script>

<div>
  <label class="label" for={id}>{label}</label>
  <input bind:this={input} {id} class="input font-mono" bind:value maxlength="200" {disabled} spellcheck="false" autocomplete="off" />
  <div class="mt-2 flex flex-wrap gap-1" role="group" aria-label={t('filename.tokens')}>
    {#each tokens as tok}
      <button type="button" class="chip min-h-8 cursor-pointer bg-slate-100 font-mono hover:bg-brand-100 dark:bg-slate-800 dark:hover:bg-brand-800/40" {disabled} onclick={() => insert(tok)} title={t(`filename.token.${tok}` as 'filename.token.date')}>
        {`{${tok}}`}
      </button>
    {/each}
  </div>
  {#if showExample}
    <p class="mt-2 text-sm">{t('filename.example')}: <code class="rounded bg-slate-100 px-1.5 py-0.5 break-all dark:bg-slate-800" data-testid="filename-example">{example}</code></p>
  {/if}
  {#if unknown.length}
    <p class="mt-1 text-xs text-amber-800 dark:text-amber-200">{t('filename.unknown_tokens', { tokens: unknown.join(', ') })}</p>
  {/if}
  <p class="hint">{t('filename.date_format_hint')}</p>
</div>
