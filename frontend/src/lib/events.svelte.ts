/** Server-Sent Events (GET /api/events). Components subscribe with `onLive()` and refetch on events. */

export const EVENT_TYPES = [
  'scanner.status',
  'scanner.capabilities',
  'document.created',
  'document.updated',
  'document.deleted',
  'document.progress',
  'page.added',
  'page.updated',
  'batch.updated',
  'delivery.updated',
  'inbox.changed',
  'walkup.status',
  'heartbeat',
] as const;

/** `reconnect` is synthetic: emitted when the stream comes back after an interruption → refetch everything. */
export type LiveEventType = (typeof EVENT_TYPES)[number] | 'reconnect';
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type LiveHandler = (data: any, type: LiveEventType) => void;

const handlers = new Map<LiveEventType, Set<LiveHandler>>();

export const live = $state({ connected: false, everConnected: false, lastEventAt: 0, failures: 0 });

let es: EventSource | null = null;
let retryTimer: ReturnType<typeof setTimeout> | null = null;
let wasDown = false;
let authCheck: (() => Promise<boolean>) | null = null;

export function emit(type: LiveEventType, data: unknown): void {
  live.lastEventAt = Date.now();
  for (const h of handlers.get(type) ?? []) {
    try {
      h(data, type);
    } catch (e) {
      console.error('live handler failed', type, e);
    }
  }
}

/** Subscribe to one or more event types. Returns an unsubscribe function (use inside $effect). */
export function onLive(types: LiveEventType | LiveEventType[], handler: LiveHandler): () => void {
  const list = Array.isArray(types) ? types : [types];
  for (const t of list) {
    if (!handlers.has(t)) handlers.set(t, new Set());
    handlers.get(t)!.add(handler);
  }
  return () => list.forEach((t) => handlers.get(t)?.delete(handler));
}

/** `check` is called when the stream closes for good (e.g. 401) – return false to stop reconnecting. */
export function connectLive(check?: () => Promise<boolean>): void {
  if (check) authCheck = check;
  if (es || typeof EventSource === 'undefined') return;
  const src = new EventSource('/api/events', { withCredentials: true });
  es = src;
  src.onopen = () => {
    live.connected = true;
    live.failures = 0;
    if (wasDown && live.everConnected) emit('reconnect', {});
    live.everConnected = true;
    wasDown = false;
  };
  src.onerror = () => {
    live.connected = false;
    wasDown = true;
    live.failures++;
    if (src.readyState === EventSource.CLOSED) {
      // The browser gave up (HTTP error such as 401/502). Retry ourselves with backoff.
      src.close();
      if (es === src) es = null;
      const delay = Math.min(30000, 2000 * 2 ** Math.min(live.failures, 4));
      retryTimer = setTimeout(async () => {
        retryTimer = null;
        const ok = authCheck ? await authCheck().catch(() => true) : true;
        if (ok) connectLive();
      }, delay);
    }
  };
  for (const type of EVENT_TYPES) {
    src.addEventListener(type, (ev) => {
      let data: unknown = {};
      try {
        data = JSON.parse((ev as MessageEvent).data || '{}');
      } catch {
        /* ignore */
      }
      emit(type, data);
    });
  }
}

export function disconnectLive(): void {
  if (retryTimer) clearTimeout(retryTimer);
  retryTimer = null;
  es?.close();
  es = null;
  live.connected = false;
  live.everConnected = false;
  wasDown = false;
}
