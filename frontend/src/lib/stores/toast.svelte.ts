import { errorMessage } from '../api/client';

export type ToastKind = 'success' | 'error' | 'info' | 'warning';
export interface Toast {
  id: number;
  kind: ToastKind;
  message: string;
  title?: string;
}

let nextId = 1;
export const toasts = $state<Toast[]>([]);

export function dismissToast(id: number): void {
  const i = toasts.findIndex((t) => t.id === id);
  if (i >= 0) toasts.splice(i, 1);
}

/** Errors stay until dismissed (never a silent failure); others disappear after a few seconds. */
export function toast(kind: ToastKind, message: string, title?: string, timeoutMs?: number): number {
  const id = nextId++;
  toasts.push({ id, kind, message, title });
  if (toasts.length > 5) toasts.shift();
  const ms = timeoutMs ?? (kind === 'error' ? 0 : kind === 'warning' ? 8000 : 4000);
  if (ms > 0) setTimeout(() => dismissToast(id), ms);
  return id;
}

export const notify = {
  success: (m: string) => toast('success', m),
  info: (m: string) => toast('info', m),
  warning: (m: string) => toast('warning', m),
  error: (e: unknown, title?: string) => toast('error', errorMessage(e), title),
};
