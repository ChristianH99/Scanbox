<script lang="ts">
  import Icon from './Icon.svelte';
  import { t } from '../i18n/index.svelte';
  import { TONE_CLASSES, deliveryStatusView, docStatusView } from '../utils/status';

  let { status, kind = 'document' }: { status: string; kind?: 'document' | 'delivery' } = $props();
  let view = $derived(kind === 'delivery' ? deliveryStatusView(status) : docStatusView(status));
</script>

<span class="chip gap-1 {TONE_CLASSES[view.tone]}" data-status={status}>
  <Icon name={view.icon} size={14} />
  {t(view.label)}
</span>
