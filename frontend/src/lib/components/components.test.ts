import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import ScannerStatusCard from './ScannerStatusCard.svelte';
import FilenameTemplateInput from './FilenameTemplateInput.svelte';
import ZoneEditor from './ZoneEditor.svelte';
import { i18n } from '../i18n/index.svelte';

afterEach(() => cleanup());

describe('ScannerStatusCard', () => {
  it('shows a jam with recovery hint', () => {
    i18n.locale = 'de';
    render(ScannerStatusCard, { status: { configured: true, online: true, state: 'error', code: 'paper_jam' } });
    expect(screen.getByRole('heading').textContent).toBe('Papierstau');
    expect(screen.getByText(/Blatt vorsichtig entfernen/)).toBeTruthy();
    expect(screen.getByTestId('scanner-status').dataset.state).toBe('error');
  });

  it('is translated to English', () => {
    i18n.locale = 'en';
    render(ScannerStatusCard, { status: { configured: true, online: false, state: 'offline' } });
    expect(screen.getByRole('heading').textContent).toBe('Scanner unreachable');
    i18n.locale = 'de';
  });

  it('offers the setup link to admins when unconfigured', () => {
    render(ScannerStatusCard, { status: { configured: false, online: false, state: 'unconfigured' }, isAdmin: true });
    expect(screen.getByRole('link', { name: /Scanner einrichten/ }).getAttribute('href')).toBe('/admin/scanner');
  });
});

describe('FilenameTemplateInput', () => {
  it('shows a live example and inserts token chips', async () => {
    render(FilenameTemplateInput, { id: 'fn', label: 'Dateiname', value: '{title}', format: 'jpeg', addQr: true });
    const example = screen.getByTestId('filename-example');
    expect(example.textContent).toBe('INV-2024-0042_Rechnung.jpg');
    await fireEvent.click(screen.getByRole('button', { name: '{pages}' }));
    expect((screen.getByLabelText('Dateiname') as HTMLInputElement).value).toContain('{pages}');
    expect(screen.getByTestId('filename-example').textContent).toMatch(/Rechnung3\.jpg$|Rechnung.*3\.jpg$/);
  });

  it('warns about unknown tokens', () => {
    render(FilenameTemplateInput, { id: 'fn2', label: 'X', value: '{foo}_{date}' });
    expect(screen.getByText(/foo/)).toBeTruthy();
  });
});

describe('ZoneEditor', () => {
  it('switches presets and draws a custom zone with pointer events', async () => {
    const { component } = render(ZoneEditor, { preset: 'top_right', zone: null });
    void component;
    await fireEvent.click(screen.getByRole('button', { name: 'Oben links' }));
    expect(screen.getByText(/x=0\.00 y=0\.00 w=0\.40 h=0\.30/)).toBeTruthy();

    const area = screen.getByRole('application');
    area.getBoundingClientRect = () => ({ left: 0, top: 0, width: 100, height: 200, right: 100, bottom: 200, x: 0, y: 0, toJSON: () => ({}) });
    area.setPointerCapture = () => {};
    await fireEvent.pointerDown(area, { clientX: 20, clientY: 40, pointerId: 1, button: 0 });
    await fireEvent.pointerMove(area, { clientX: 70, clientY: 140, pointerId: 1 });
    await fireEvent.pointerUp(area, { pointerId: 1 });
    expect(screen.getByText(/x=0\.20 y=0\.20 w=0\.50 h=0\.50/)).toBeTruthy();
    expect(screen.getByRole('slider')).toBeTruthy();
  });
});
