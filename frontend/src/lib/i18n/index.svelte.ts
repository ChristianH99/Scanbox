import { de, type MessageKey } from './de';
import { en } from './en';
import { storageGet, storageSet } from '../utils/misc';

export type Locale = 'de' | 'en';
export type { MessageKey };

const dicts: Record<Locale, Record<MessageKey, string>> = { de, en };

function initialLocale(): Locale {
  const stored = storageGet('scanbox.locale');
  if (stored === 'de' || stored === 'en') return stored;
  if (typeof navigator !== 'undefined' && navigator.language?.toLowerCase().startsWith('en')) return 'en';
  return 'de';
}

let current = $state<Locale>(initialLocale());

export const i18n = {
  get locale(): Locale {
    return current;
  },
  set locale(v: Locale) {
    current = v;
    storageSet('scanbox.locale', v);
    if (typeof document !== 'undefined') document.documentElement.lang = v;
  },
};

export type Params = Record<string, string | number | null | undefined>;

export function interpolate(s: string, params?: Params): string {
  if (!params) return s;
  return s.replace(/\{(\w+)\}/g, (m, k: string) => (params[k] === undefined || params[k] === null ? m : String(params[k])));
}

/** Translate a key. Reactive: reads the current locale rune. */
export function t(key: MessageKey, params?: Params): string {
  const s = dicts[current][key] ?? de[key] ?? key;
  return interpolate(s, params);
}

/** Translate a dynamic key (e.g. an error code) with a fallback when the key is unknown. */
export function tMaybe(key: string, fallback: string, params?: Params): string {
  const d = dicts[current] as Record<string, string>;
  const s = d[key] ?? (de as Record<string, string>)[key];
  return s ? interpolate(s, params) : fallback;
}

export function hasKey(key: string): key is MessageKey {
  return key in de;
}

// ------------------------------------------------------------------ formatting
const intlLocale = () => (current === 'de' ? 'de-DE' : 'en-GB');

/** Backend timestamps are UTC without a zone suffix: treat them as UTC. */
export function parseDate(v: string | Date | null | undefined): Date | null {
  if (!v) return null;
  if (v instanceof Date) return v;
  const s = /[zZ]|[+-]\d\d:?\d\d$/.test(v) || /^\d{4}-\d{2}-\d{2}$/.test(v) ? v : `${v}Z`;
  const d = new Date(s);
  return Number.isNaN(d.getTime()) ? null : d;
}

export function fmtDateTime(v: string | Date | null | undefined): string {
  const d = parseDate(v);
  return d ? new Intl.DateTimeFormat(intlLocale(), { dateStyle: 'medium', timeStyle: 'short' }).format(d) : '–';
}

export function fmtDate(v: string | Date | null | undefined): string {
  const d = parseDate(v);
  return d ? new Intl.DateTimeFormat(intlLocale(), { dateStyle: 'medium' }).format(d) : '–';
}

export function fmtTime(v: string | Date | null | undefined): string {
  const d = parseDate(v);
  return d ? new Intl.DateTimeFormat(intlLocale(), { timeStyle: 'medium' }).format(d) : '–';
}

export function fmtNumber(n: number, digits = 0): string {
  return new Intl.NumberFormat(intlLocale(), { maximumFractionDigits: digits }).format(n);
}

export function fmtBytes(n: number | null | undefined): string {
  if (n === null || n === undefined) return '–';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let v = n;
  let i = 0;
  while (v >= 1000 && i < units.length - 1) {
    v /= 1000;
    i++;
  }
  return `${fmtNumber(v, v < 10 && i > 0 ? 1 : 0)} ${units[i]}`;
}

export function fmtRelative(v: string | Date | null | undefined, now = Date.now()): string {
  const d = parseDate(v);
  if (!d) return '–';
  const diff = (d.getTime() - now) / 1000;
  const rtf = new Intl.RelativeTimeFormat(intlLocale(), { numeric: 'auto' });
  const abs = Math.abs(diff);
  if (abs < 60) return rtf.format(Math.round(diff), 'second');
  if (abs < 3600) return rtf.format(Math.round(diff / 60), 'minute');
  if (abs < 86400) return rtf.format(Math.round(diff / 3600), 'hour');
  return rtf.format(Math.round(diff / 86400), 'day');
}
