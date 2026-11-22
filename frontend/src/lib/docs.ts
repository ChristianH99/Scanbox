/** Document/page API helpers shared by the scan screen, editor, inbox and history. */
import { api } from './api/client';
import type { DocumentDetail, Page, PageEdits } from './api/types';
import { hashString } from './utils/misc';

export type ImgSize = 'thumb' | 'preview' | 'full';

/** Image URL with a cache-buster derived from the page edits (the browser caches images for 5 min). */
export function pageImageUrl(page: Pick<Page, 'id' | 'edits'>, size: ImgSize = 'thumb', variant: 'raw' | 'processed' = 'processed', extra = ''): string {
  const v = hashString(JSON.stringify(page.edits ?? {}) + extra);
  return `/api/pages/${encodeURIComponent(page.id)}/image?variant=${variant}&size=${size}&v=${v}`;
}

export const docApi = {
  get: (id: number) => api.get<DocumentDetail>(`/api/documents/${id}`),
  patch: (id: number, body: Record<string, unknown>) => api.patch<DocumentDetail>(`/api/documents/${id}`, body),
  claim: (id: number) => api.post<DocumentDetail>(`/api/documents/${id}/claim`),
  addBatch: (id: number) => api.post<DocumentDetail>(`/api/documents/${id}/batches`),
  finish: (id: number) => api.post<DocumentDetail>(`/api/documents/${id}/finish`),
  cancel: (id: number) => api.post<DocumentDetail>(`/api/documents/${id}/cancel`),
  discard: (id: number) => api.post<DocumentDetail>(`/api/documents/${id}/discard`),
  resend: (id: number) => api.post<DocumentDetail>(`/api/documents/${id}/resend`),
  retryDelivery: (id: number, deliveryId: number) => api.post<DocumentDetail>(`/api/documents/${id}/deliveries/${deliveryId}/retry`),
  reorder: (id: number, pageIds: string[]) => api.post<DocumentDetail>(`/api/documents/${id}/reorder`, { page_ids: pageIds }),
  deletePages: (id: number, pageIds: string[]) => api.post<DocumentDetail>(`/api/documents/${id}/pages/delete`, { page_ids: pageIds }),
  restorePages: (id: number, pageIds: string[]) => api.post<DocumentDetail>(`/api/documents/${id}/pages/restore`, { page_ids: pageIds }),
  insertPages: (id: number, sourceId: number, pageIds: string[], copy = true) =>
    api.post<DocumentDetail>(`/api/documents/${id}/pages/insert`, { source_document_id: sourceId, page_ids: pageIds, copy }),
  split: (id: number, beforePageIds: string[]) => api.post<{ children: number[] }>(`/api/documents/${id}/split`, { before_page_ids: beforePageIds }),
  merge: (id: number, sourceIds: number[]) => api.post<DocumentDetail>(`/api/documents/${id}/merge`, { source_document_ids: sourceIds }),
  patchPage: (pageId: string, body: { edits?: PageEdits; keep_blank?: boolean; deleted?: boolean }) =>
    api.patch<Page>(`/api/pages/${encodeURIComponent(pageId)}`, body),
  preview: (pageId: string, edits: PageEdits, size: 'thumb' | 'preview' = 'preview') =>
    api.postBlob(`/api/pages/${encodeURIComponent(pageId)}/preview`, edits, { query: { size } }),
};

export const downloadUrl = (id: number) => `/api/documents/${id}/download`;
export const artifactUrl = (id: number, artifactId: number, inline = false) =>
  `/api/documents/${id}/artifacts/${artifactId}${inline ? '?inline=true' : ''}`;

/** Pages in display order (backend already sorts by position, but be safe). */
export function orderedPages(doc: Pick<DocumentDetail, 'pages'>): Page[] {
  return [...doc.pages].sort((a, b) => a.position - b.position);
}

/** Pages that will end up in the output: not deleted and not auto-blank (unless kept). */
export function isPageExcluded(p: Page): boolean {
  return p.deleted || (p.is_blank && !p.keep_blank);
}

/** Rotate a page's absolute orientation by ±90°. */
export function rotated(edits: PageEdits, delta: 90 | -90, autoRotate = 0): PageEdits {
  const base = edits.rotate ?? autoRotate;
  const r = (((base + delta) % 360) + 360) % 360;
  return { ...edits, rotate: r as 0 | 90 | 180 | 270 };
}

export function lastBatchError(doc: DocumentDetail) {
  const failed = [...doc.batches].sort((a, b) => b.seq - a.seq).find((b) => b.error_code);
  return failed ?? null;
}
