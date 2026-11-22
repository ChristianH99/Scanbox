import type { DocumentDetail } from './api/types';
import { errorMessage, isApiError } from './api/client';
import { docApi } from './docs';
import { onLive } from './events.svelte';
import { debounce } from './utils/misc';

/**
 * A document kept in sync with the server: refetches (debounced) whenever an SSE event mentions it,
 * and after the event stream reconnects.
 */
export class LiveDocument {
  doc = $state<DocumentDetail | null>(null);
  error = $state<string | null>(null);
  errorCode = $state<string | null>(null);
  loading = $state(false);
  progress = $state<{ step: string; done: number; total: number } | null>(null);
  /** Page ids that just arrived (for a short highlight). */
  fresh = $state<Set<string>>(new Set());

  readonly id: number;
  private offs: (() => void)[] = [];
  private refetch = debounce(() => void this.load(), 250);

  constructor(id: number) {
    this.id = id;
  }

  async load(): Promise<void> {
    this.loading = true;
    try {
      this.doc = await docApi.get(this.id);
      this.error = null;
      this.errorCode = null;
      if (this.doc.status !== 'processing') this.progress = null;
    } catch (e) {
      this.error = errorMessage(e);
      this.errorCode = isApiError(e) ? e.code : 'unknown';
    } finally {
      this.loading = false;
    }
  }

  /** Replace the local copy with a server response (after an action). */
  set(d: DocumentDetail): void {
    this.doc = d;
  }

  start(): () => void {
    void this.load();
    const mine = (data: { id?: number; document_id?: number }) => data?.id === this.id || data?.document_id === this.id;
    this.offs.push(
      onLive(['document.updated', 'document.created', 'delivery.updated', 'page.updated', 'batch.updated', 'inbox.changed'], (data) => {
        if (mine(data)) this.refetch();
      }),
      onLive('page.added', (data: { document_id?: number; page_id?: string }) => {
        if (data?.document_id !== this.id) return;
        if (data.page_id) {
          const s = new Set(this.fresh);
          s.add(data.page_id);
          this.fresh = s;
          setTimeout(() => {
            const n = new Set(this.fresh);
            n.delete(data.page_id!);
            this.fresh = n;
          }, 2500);
        }
        this.refetch();
      }),
      onLive('document.progress', (data: { id?: number; step: string; done: number; total: number }) => {
        if (data?.id === this.id) this.progress = { step: data.step, done: data.done, total: data.total };
      }),
      onLive('document.deleted', (data) => {
        if (mine(data)) this.refetch();
      }),
      onLive('reconnect', () => this.refetch()),
    );
    return () => this.stop();
  }

  stop(): void {
    this.refetch.cancel();
    this.offs.forEach((f) => f());
    this.offs = [];
  }
}
