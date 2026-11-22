import type { DocStatus, ScannerStatus } from '../api/types';
import type { MessageKey } from '../i18n/de';
import type { IconName } from '../components/icons';

export type Tone = 'ok' | 'busy' | 'warn' | 'error' | 'offline' | 'neutral';

export interface ScannerView {
  tone: Tone;
  icon: IconName;
  title: MessageKey;
  detail: MessageKey | null;
  /** Can a scan be started right now (busy → it will be queued)? */
  canScan: boolean;
  /** Why the start button is disabled (or a warning shown next to it). */
  reason: MessageKey | null;
  /** Scan allowed but with a caveat (e.g. feeder looks empty). */
  warning: MessageKey | null;
}

/** Map the backend scanner snapshot to what the status card shows. */
export function scannerView(s: ScannerStatus | null | undefined): ScannerView {
  if (!s) {
    return { tone: 'neutral', icon: 'loader', title: 'scanner.checking', detail: null, canScan: false, reason: 'scanner.checking', warning: null };
  }
  if (!s.configured || s.state === 'unconfigured') {
    return {
      tone: 'warn',
      icon: 'settings',
      title: 'scanner.unconfigured',
      detail: 'scanner.unconfigured_hint',
      canScan: false,
      reason: 'scanner.unconfigured',
      warning: null,
    };
  }
  if (!s.online || s.state === 'offline') {
    return {
      tone: 'offline',
      icon: 'wifi-off',
      title: 'scanner.offline',
      detail: 'scanner.offline_hint',
      canScan: false,
      reason: 'scanner.offline',
      warning: null,
    };
  }
  const code = s.code ?? '';
  if (s.state === 'error') {
    const map: Record<string, [MessageKey, MessageKey]> = {
      paper_jam: ['scanner.jam', 'scanner.jam_hint'],
      cover_open: ['scanner.cover_open', 'scanner.cover_open_hint'],
      multipick: ['scanner.multipick', 'scanner.multipick_hint'],
      scanner_stopped: ['scanner.stopped', 'scanner.stopped_hint'],
    };
    const [title, detail] = map[code] ?? ['scanner.error', 'scanner.error_hint'];
    return { tone: 'error', icon: 'alert', title, detail, canScan: false, reason: title, warning: null };
  }
  if (s.state === 'busy') {
    return {
      tone: 'busy',
      icon: 'loader',
      title: 'scanner.busy',
      detail: 'scanner.busy_hint',
      canScan: true,
      reason: null,
      warning: 'scanner.busy_queue',
    };
  }
  if (code === 'feeder_empty' || s.paper_loaded === false) {
    return {
      tone: 'warn',
      icon: 'tray',
      title: 'scanner.ready_empty',
      detail: 'scanner.ready_empty_hint',
      canScan: true,
      reason: null,
      warning: 'scanner.feeder_empty_warning',
    };
  }
  if (code === 'paper_loaded' || s.paper_loaded === true) {
    return { tone: 'ok', icon: 'check', title: 'scanner.ready_paper', detail: 'scanner.ready_paper_hint', canScan: true, reason: null, warning: null };
  }
  return { tone: 'ok', icon: 'check', title: 'scanner.ready', detail: null, canScan: true, reason: null, warning: null };
}

export interface DocStatusView {
  tone: Tone;
  label: MessageKey;
  icon: IconName;
}

export function docStatusView(status: DocStatus | string): DocStatusView {
  const m: Record<string, DocStatusView> = {
    queued: { tone: 'neutral', label: 'status.queued', icon: 'clock' },
    scanning: { tone: 'busy', label: 'status.scanning', icon: 'loader' },
    stored: { tone: 'busy', label: 'status.stored', icon: 'edit' },
    processing: { tone: 'busy', label: 'status.processing', icon: 'loader' },
    delivering: { tone: 'busy', label: 'status.delivering', icon: 'send' },
    done: { tone: 'ok', label: 'status.done', icon: 'check' },
    failed: { tone: 'error', label: 'status.failed', icon: 'x-circle' },
    needs_attention: { tone: 'warn', label: 'status.needs_attention', icon: 'alert' },
    discarded: { tone: 'neutral', label: 'status.discarded', icon: 'trash' },
    expired: { tone: 'neutral', label: 'status.expired', icon: 'clock' },
  };
  return m[status] ?? { tone: 'neutral', label: 'status.unknown', icon: 'info' };
}

export function deliveryStatusView(status: string): DocStatusView {
  const m: Record<string, DocStatusView> = {
    pending: { tone: 'neutral', label: 'delivery.pending', icon: 'clock' },
    running: { tone: 'busy', label: 'delivery.running', icon: 'loader' },
    retrying: { tone: 'warn', label: 'delivery.retrying', icon: 'refresh' },
    done: { tone: 'ok', label: 'delivery.done', icon: 'check' },
    failed: { tone: 'error', label: 'delivery.failed', icon: 'x-circle' },
    needs_attention: { tone: 'warn', label: 'delivery.needs_attention', icon: 'alert' },
    cancelled: { tone: 'neutral', label: 'delivery.cancelled', icon: 'x-circle' },
  };
  return m[status] ?? { tone: 'neutral', label: 'status.unknown', icon: 'info' };
}

export const TONE_CLASSES: Record<Tone, string> = {
  ok: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/50 dark:text-emerald-200',
  busy: 'bg-sky-100 text-sky-800 dark:bg-sky-900/50 dark:text-sky-200',
  warn: 'bg-amber-100 text-amber-900 dark:bg-amber-900/50 dark:text-amber-200',
  error: 'bg-red-100 text-red-800 dark:bg-red-900/50 dark:text-red-200',
  offline: 'bg-slate-200 text-slate-800 dark:bg-slate-700 dark:text-slate-100',
  neutral: 'bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300',
};

export const TONE_BORDER: Record<Tone, string> = {
  ok: 'border-emerald-300 dark:border-emerald-800',
  busy: 'border-sky-300 dark:border-sky-800',
  warn: 'border-amber-300 dark:border-amber-700',
  error: 'border-red-300 dark:border-red-800',
  offline: 'border-slate-300 dark:border-slate-600',
  neutral: 'border-slate-200 dark:border-slate-700',
};

/** Scanner-side error codes where the right recovery is "fix the device, then scan the rest". */
export const SCAN_ERROR_CODES = new Set([
  'feeder_empty',
  'paper_jam',
  'cover_open',
  'multipick',
  'scanner_timeout',
  'scanner_offline',
  'scanner_error',
  'scanner_busy',
  'interrupted',
  'insufficient_disk',
]);

export const ACTIVE_STATUSES = new Set(['queued', 'scanning', 'processing', 'delivering']);
export const EDITABLE_STATUSES = new Set(['stored', 'needs_attention', 'failed', 'done']);
