<script lang="ts">
  import Modal from './Modal.svelte';
  import { closeDialog, dialogState } from '../stores/dialog.svelte';
  import { t } from '../i18n/index.svelte';

  let open = $derived(dialogState.current !== null);
</script>

{#if dialogState.current}
  {@const d = dialogState.current}
  <Modal open={open} title={d.title} size="sm" onclose={() => closeDialog(false)}>
    {#if d.message}<p class="text-sm whitespace-pre-line text-slate-700 dark:text-slate-300">{d.message}</p>{/if}
    {#snippet footer()}
      <button type="button" class="btn btn-secondary" onclick={() => closeDialog(false)}>{d.cancelLabel ?? t('common.cancel')}</button>
      <button
        type="button"
        class="btn {d.danger ? 'btn-danger' : 'btn-primary'}"
        data-testid="confirm-ok"
        onclick={() => closeDialog(true)}>{d.confirmLabel ?? t('common.ok')}</button
      >
    {/snippet}
  </Modal>
{/if}
