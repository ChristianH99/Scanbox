import { describe, expect, it } from 'vitest';
import { docStatusView, scannerView } from './status';
import type { ScannerStatus } from '../api/types';

const base: ScannerStatus = { configured: true, online: true, state: 'ready' };

describe('scannerView', () => {
  it('shows a checking state before the first status arrives', () => {
    const v = scannerView(null);
    expect(v.canScan).toBe(false);
    expect(v.title).toBe('scanner.checking');
  });

  it('blocks scanning when no scanner is configured', () => {
    const v = scannerView({ configured: false, online: false, state: 'unconfigured', code: 'scanner_not_configured' });
    expect(v).toMatchObject({ title: 'scanner.unconfigured', canScan: false, reason: 'scanner.unconfigured', tone: 'warn' });
  });

  it('blocks scanning when offline', () => {
    const v = scannerView({ ...base, online: false, state: 'offline', code: 'scanner_offline' });
    expect(v).toMatchObject({ title: 'scanner.offline', canScan: false, tone: 'offline', icon: 'wifi-off' });
  });

  it.each([
    ['paper_jam', 'scanner.jam'],
    ['cover_open', 'scanner.cover_open'],
    ['multipick', 'scanner.multipick'],
    ['scanner_stopped', 'scanner.stopped'],
    ['something_new', 'scanner.error'],
  ])('maps error code %s', (code, title) => {
    const v = scannerView({ ...base, state: 'error', code });
    expect(v.title).toBe(title);
    expect(v.tone).toBe('error');
    expect(v.canScan).toBe(false);
    expect(v.detail).not.toBeNull();
  });

  it('allows scanning while busy (queued) with a warning', () => {
    const v = scannerView({ ...base, state: 'busy', code: 'processing' });
    expect(v).toMatchObject({ title: 'scanner.busy', canScan: true, warning: 'scanner.busy_queue' });
  });

  it('distinguishes paper loaded / feeder empty / unknown', () => {
    expect(scannerView({ ...base, code: 'paper_loaded', paper_loaded: true }).title).toBe('scanner.ready_paper');
    const empty = scannerView({ ...base, code: 'feeder_empty', paper_loaded: false });
    expect(empty).toMatchObject({ title: 'scanner.ready_empty', canScan: true, warning: 'scanner.feeder_empty_warning', tone: 'warn' });
    expect(scannerView({ ...base }).title).toBe('scanner.ready');
  });
});

describe('docStatusView', () => {
  it('maps every backend status', () => {
    for (const s of ['queued', 'scanning', 'stored', 'processing', 'delivering', 'done', 'failed', 'needs_attention', 'discarded', 'expired']) {
      expect(docStatusView(s).label).toBe(`status.${s}`);
    }
    expect(docStatusView('weird').label).toBe('status.unknown');
    expect(docStatusView('failed').tone).toBe('error');
    expect(docStatusView('needs_attention').tone).toBe('warn');
  });
});
