import { describe, expect, it } from 'vitest';
import { buildFilename, extensionFor, renderTemplate, sanitizeComponent, unknownTokens } from './filename';

const created = new Date(2024, 2, 5, 9, 7, 3); // 2024-03-05 09:07:03 local

describe('filename templates (mirror backend filenames.py)', () => {
  it('renders all tokens', () => {
    const v = { created, qr: 'INV-1', title: 'Brief', counter: 7, profile: 'Dokument', user: 'anna', pages: 3 };
    expect(renderTemplate('{date}_{time}_{datetime}', v)).toBe('2024-03-05_09-07-03_2024-03-05_09-07-03');
    expect(renderTemplate('{qr}-{title}-{counter}-{profile}-{user}-{pages}', v)).toBe('INV-1-Brief-0007-Dokument-anna-3');
    expect(renderTemplate('{counter:6}', v)).toBe('000007');
  });

  it('supports strftime specs on date tokens', () => {
    expect(renderTemplate('{date:%Y%m%d}', { created })).toBe('20240305');
    expect(renderTemplate('{date:%d.%m.%y}', { created })).toBe('05.03.24');
  });

  it('collapses separators left by empty tokens and drops unknown tokens', () => {
    expect(renderTemplate('{qr}_{date}_{title}', { created, title: '' })).toBe('2024-03-05');
    expect(renderTemplate('{nope}_{date}', { created })).toBe('2024-03-05');
    expect(unknownTokens('{nope}_{date}_{bad}')).toEqual(['nope', 'bad']);
  });

  it('sanitises illegal characters and reserved names', () => {
    expect(sanitizeComponent('a/b:c*d?"e<f>g|h')).toBe('a_b_c_d__e_f_g_h');
    expect(sanitizeComponent('  ..hidden  ')).toBe('hidden');
    expect(sanitizeComponent('CON')).toBe('_CON');
    expect(sanitizeComponent('x'.repeat(300)).length).toBe(150);
  });

  it('builds the final name with extension and optional QR prefix', () => {
    expect(buildFilename('{title}', { created, title: 'Rechnung', qr: 'Q1' }, 'pdf', true)).toBe('Q1_Rechnung.pdf');
    expect(buildFilename('{qr}_{title}', { created, title: 'R', qr: 'Q1' }, 'pdf', true)).toBe('Q1_R.pdf');
    expect(buildFilename('{title}', { created, title: 'R', qr: null }, 'pdf', true)).toBe('R.pdf');
    expect(buildFilename('{title}', { created, title: '' }, 'pdf')).toBe('scan.pdf');
    expect(extensionFor('jpeg')).toBe('jpg');
    expect(extensionFor('pdfa')).toBe('pdf');
    expect(extensionFor('tiff')).toBe('tif');
  });
});
