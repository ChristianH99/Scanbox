import { describe, expect, it } from 'vitest';
import { centreInside, isValidRect, moveRect, normalizeRect, presetFor, rectFromPoints, resizeRect, toRelative, ZONE_PRESETS } from './zone';

const box = { left: 100, top: 50, width: 200, height: 400 };

describe('QR zone coordinate math', () => {
  it('converts client pixels to relative page coordinates (clamped)', () => {
    expect(toRelative(100, 50, box)).toEqual({ x: 0, y: 0 });
    expect(toRelative(200, 250, box)).toEqual({ x: 0.5, y: 0.5 });
    expect(toRelative(400, 500, box)).toEqual({ x: 1, y: 1 });
    expect(toRelative(0, 0, box)).toEqual({ x: 0, y: 0 });
    expect(toRelative(5, 5, { left: 0, top: 0, width: 0, height: 0 })).toEqual({ x: 0, y: 0 });
  });

  it('builds a rectangle from a drag in any direction', () => {
    expect(rectFromPoints({ x: 1.2, y: 0.3 }, { x: 0.6, y: -0.1 })).toEqual(ZONE_PRESETS.top_right);
    expect(rectFromPoints({ x: 0.2, y: 0.2 }, { x: 0.5, y: 0.6 })).toEqual({ x: 0.2, y: 0.2, w: 0.3, h: 0.4 });
  });

  it('clips into the unit square like the backend Zone validator', () => {
    expect(normalizeRect({ x: 0.8, y: 0.9, w: 0.5, h: 0.5 })).toEqual({ x: 0.8, y: 0.9, w: 0.2, h: 0.1 });
    expect(normalizeRect({ x: -0.1, y: 0, w: 0.5, h: 2 })).toEqual({ x: 0, y: 0, w: 0.5, h: 1 });
  });

  it('rejects zones that are too small', () => {
    expect(isValidRect({ x: 0, y: 0, w: 0.005, h: 0.5 })).toBe(false);
    expect(isValidRect({ x: 0.1, y: 0.1, w: 0.3, h: 0.3 })).toBe(true);
    expect(isValidRect(null)).toBe(false);
  });

  it('moves without leaving the page', () => {
    expect(moveRect({ x: 0.6, y: 0, w: 0.4, h: 0.3 }, 0.2, -0.1)).toEqual({ x: 0.6, y: 0, w: 0.4, h: 0.3 });
    expect(moveRect({ x: 0.1, y: 0.1, w: 0.2, h: 0.2 }, 0.1, 0.1)).toEqual({ x: 0.2, y: 0.2, w: 0.2, h: 0.2 });
  });

  it('resizes by corner handles, also when dragged past the opposite edge', () => {
    const r = { x: 0.2, y: 0.2, w: 0.4, h: 0.4 };
    expect(resizeRect(r, 'se', { x: 0.8, y: 0.9 })).toEqual({ x: 0.2, y: 0.2, w: 0.6, h: 0.7 });
    expect(resizeRect(r, 'nw', { x: 0.1, y: 0 })).toEqual({ x: 0.1, y: 0, w: 0.5, h: 0.6 });
    expect(resizeRect(r, 'nw', { x: 0.9, y: 0.9 })).toEqual({ x: 0.6, y: 0.6, w: 0.3, h: 0.3 });
  });

  it('recognises presets and the centre-in-zone rule', () => {
    expect(presetFor({ x: 0, y: 0, w: 0.4, h: 0.3 })).toBe('top_left');
    expect(presetFor({ x: 0.1, y: 0, w: 0.4, h: 0.3 })).toBe('custom');
    expect(centreInside({ x: 0.7, y: 0.05, w: 0.1, h: 0.1 }, ZONE_PRESETS.top_right)).toBe(true);
    expect(centreInside({ x: 0.1, y: 0.8, w: 0.1, h: 0.1 }, ZONE_PRESETS.top_right)).toBe(false);
  });
});
