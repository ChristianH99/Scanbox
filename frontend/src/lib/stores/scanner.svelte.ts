import { api } from '../api/client';
import type { Capabilities, InputCaps, ScannerStatus } from '../api/types';
import { onLive } from '../events.svelte';
import { debounce } from '../utils/misc';

export const scanner = $state<{
  status: ScannerStatus | null;
  caps: Capabilities | null;
  statusError: string | null;
  loading: boolean;
}>({ status: null, caps: null, statusError: null, loading: false });

export async function loadScannerStatus(): Promise<void> {
  try {
    scanner.status = await api.get<ScannerStatus>('/api/scanner/status');
    scanner.statusError = null;
  } catch (e) {
    scanner.statusError = (e as Error).message;
  }
}

export async function loadCapabilities(): Promise<void> {
  try {
    scanner.caps = await api.get<Capabilities>('/api/scanner/capabilities');
  } catch {
    /* status card shows the problem */
  }
}

let installed = false;

/** Keep scanner status/caps in sync with SSE. Safe to call repeatedly. */
export function initScannerStore(): void {
  if (installed) return;
  installed = true;
  const refetchCaps = debounce(() => void loadCapabilities(), 300);
  onLive('scanner.status', (data: Partial<ScannerStatus>) => {
    const wasOnline = scanner.status?.online;
    if (data && typeof data === 'object' && 'state' in data) {
      scanner.status = { ...(scanner.status ?? { configured: false, online: false, state: 'unknown' }), ...data } as ScannerStatus;
      scanner.statusError = null;
    } else void loadScannerStatus();
    if (!wasOnline && data?.online) refetchCaps();
  });
  onLive('scanner.capabilities', () => refetchCaps());
  onLive('reconnect', () => {
    void loadScannerStatus();
    refetchCaps();
  });
  scanner.loading = true;
  void Promise.all([loadScannerStatus(), loadCapabilities()]).finally(() => (scanner.loading = false));
}

export function inputCaps(caps: Capabilities | null, source: string): InputCaps | null {
  if (!caps) return null;
  if (source === 'adf_duplex') return caps.adf_duplex ?? caps.adf_simplex ?? null;
  return caps.adf_simplex ?? caps.adf_duplex ?? null;
}
