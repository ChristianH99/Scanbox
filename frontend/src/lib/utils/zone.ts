/** Geometry helpers for relative rectangles (QR zones, crop boxes). All values in 0..1. */

export interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

export type PresetName = 'top_right' | 'top_left' | 'full';

export const ZONE_PRESETS: Record<PresetName, Rect> = {
  top_right: { x: 0.6, y: 0, w: 0.4, h: 0.3 },
  top_left: { x: 0, y: 0, w: 0.4, h: 0.3 },
  full: { x: 0, y: 0, w: 1, h: 1 },
};

/** Backend rejects zones with w or h <= 0.01. */
export const MIN_SIZE = 0.02;

export const clamp01 = (v: number) => Math.min(1, Math.max(0, v));
const round4 = (v: number) => Math.round(v * 10000) / 10000;

export interface Box {
  left: number;
  top: number;
  width: number;
  height: number;
}

/** Convert a client (pixel) point into coordinates relative to an element box (clamped to 0..1). */
export function toRelative(clientX: number, clientY: number, box: Box): { x: number; y: number } {
  if (box.width <= 0 || box.height <= 0) return { x: 0, y: 0 };
  return { x: clamp01((clientX - box.left) / box.width), y: clamp01((clientY - box.top) / box.height) };
}

/** Rectangle spanned by two relative points (any drag direction), clamped to the page. */
export function rectFromPoints(a: { x: number; y: number }, b: { x: number; y: number }): Rect {
  const x1 = clamp01(Math.min(a.x, b.x));
  const y1 = clamp01(Math.min(a.y, b.y));
  const x2 = clamp01(Math.max(a.x, b.x));
  const y2 = clamp01(Math.max(a.y, b.y));
  return { x: round4(x1), y: round4(y1), w: round4(x2 - x1), h: round4(y2 - y1) };
}

/** Clip a rectangle into the unit square (mirrors the backend Zone validator). */
export function normalizeRect(r: Rect): Rect {
  const x = clamp01(r.x);
  const y = clamp01(r.y);
  const w = Math.min(Math.max(r.w, 0), 1 - x);
  const h = Math.min(Math.max(r.h, 0), 1 - y);
  return { x: round4(x), y: round4(y), w: round4(w), h: round4(h) };
}

export function isValidRect(r: Rect | null | undefined): r is Rect {
  return !!r && r.w >= MIN_SIZE && r.h >= MIN_SIZE && r.x >= 0 && r.y >= 0 && r.x + r.w <= 1.0001 && r.y + r.h <= 1.0001;
}

/** Move a rectangle by a relative delta, keeping it inside the page. */
export function moveRect(r: Rect, dx: number, dy: number): Rect {
  return normalizeRect({
    ...r,
    x: Math.min(Math.max(r.x + dx, 0), 1 - r.w),
    y: Math.min(Math.max(r.y + dy, 0), 1 - r.h),
  });
}

export type Handle = 'nw' | 'ne' | 'sw' | 'se';

/** Resize by dragging a corner handle to a relative point. */
export function resizeRect(r: Rect, handle: Handle, p: { x: number; y: number }): Rect {
  const x1 = handle === 'nw' || handle === 'sw' ? p.x : r.x;
  const x2 = handle === 'ne' || handle === 'se' ? p.x : r.x + r.w;
  const y1 = handle === 'nw' || handle === 'ne' ? p.y : r.y;
  const y2 = handle === 'sw' || handle === 'se' ? p.y : r.y + r.h;
  return rectFromPoints({ x: x1, y: y1 }, { x: x2, y: y2 });
}

/** Which preset (if any) equals a rect. */
export function presetFor(r: Rect): PresetName | 'custom' {
  for (const [k, p] of Object.entries(ZONE_PRESETS)) {
    if (Math.abs(p.x - r.x) < 1e-3 && Math.abs(p.y - r.y) < 1e-3 && Math.abs(p.w - r.w) < 1e-3 && Math.abs(p.h - r.h) < 1e-3)
      return k as PresetName;
  }
  return 'custom';
}

/** CSS percentage style for an overlay box. */
export function rectStyle(r: Rect): string {
  return `left:${r.x * 100}%;top:${r.y * 100}%;width:${r.w * 100}%;height:${r.h * 100}%`;
}

/** Centre of a rect lies inside a zone (backend rule for "hit in zone"). */
export function centreInside(hit: Rect, zone: Rect): boolean {
  const cx = hit.x + hit.w / 2;
  const cy = hit.y + hit.h / 2;
  return cx >= zone.x && cx <= zone.x + zone.w && cy >= zone.y && cy <= zone.y + zone.h;
}
