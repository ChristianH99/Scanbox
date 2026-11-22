/** Client-side port of backend/scanbox/filenames.py for the live filename example. */

export const FILENAME_TOKENS = ['date', 'time', 'datetime', 'qr', 'title', 'counter', 'profile', 'user', 'pages'] as const;
export type FilenameToken = (typeof FILENAME_TOKENS)[number];

export interface TemplateVars {
  created: Date;
  qr?: string | null;
  title?: string;
  counter?: number;
  profile?: string;
  user?: string;
  pages?: number;
}

const TOKEN_RE = /\{([a-z]+)(?::([^{}]{1,40}))?\}/g;
// eslint-disable-next-line no-control-regex
const ILLEGAL = /[\u0000-\u001f\u007f/\\:*?"<>|]/g;
const ALLOWED_STRFTIME = /^[%a-zA-Z0-9 ._-]+$/;
const RESERVED = new Set([
  'CON',
  'PRN',
  'AUX',
  'NUL',
  ...Array.from({ length: 9 }, (_, i) => `COM${i + 1}`),
  ...Array.from({ length: 9 }, (_, i) => `LPT${i + 1}`),
]);

const pad = (n: number, w = 2) => String(n).padStart(w, '0');

/** Minimal strftime (the directives that make sense in file names). Unknown directives stay as-is. */
export function strftime(d: Date, fmt: string): string {
  const map: Record<string, () => string> = {
    Y: () => String(d.getFullYear()),
    y: () => pad(d.getFullYear() % 100),
    m: () => pad(d.getMonth() + 1),
    d: () => pad(d.getDate()),
    H: () => pad(d.getHours()),
    M: () => pad(d.getMinutes()),
    S: () => pad(d.getSeconds()),
    j: () => pad(Math.floor((d.getTime() - new Date(d.getFullYear(), 0, 1).getTime()) / 86400000) + 1, 3),
    '%': () => '%',
  };
  return fmt.replace(/%([a-zA-Z%])/g, (m, c: string) => (map[c] ? map[c]() : m));
}

function fmtDate(d: Date, spec: string | undefined, def: string): string {
  if (spec && ALLOWED_STRFTIME.test(spec) && spec.includes('%')) return strftime(d, spec);
  return strftime(d, def);
}

/** Render tokens; unknown tokens are dropped (like the backend). Not yet sanitised. */
export function renderTemplate(template: string, v: TemplateVars): string {
  const out = template.replace(TOKEN_RE, (_m, name: string, spec: string | undefined) => {
    switch (name) {
      case 'date':
        return fmtDate(v.created, spec, '%Y-%m-%d');
      case 'time':
        return fmtDate(v.created, spec, '%H-%M-%S');
      case 'datetime':
        return fmtDate(v.created, spec, '%Y-%m-%d_%H-%M-%S');
      case 'qr':
        return v.qr ?? '';
      case 'title':
        return v.title ?? '';
      case 'counter': {
        const w = spec && /^\d+$/.test(spec) && +spec >= 1 && +spec <= 8 ? +spec : 4;
        return String(v.counter ?? 0).padStart(w, '0');
      }
      case 'profile':
        return v.profile ?? '';
      case 'user':
        return v.user ?? '';
      case 'pages':
        return String(v.pages ?? 0);
      default:
        return '';
    }
  });
  return out.replace(/([_\- ])[_\- ]+/g, '$1').replace(/^[_\- .]+|[_\- .]+$/g, '');
}

function truncateUtf8(s: string, maxBytes: number): string {
  const enc = new TextEncoder();
  if (enc.encode(s).length <= maxBytes) return s;
  let out = '';
  let used = 0;
  for (const ch of s) {
    const n = enc.encode(ch).length;
    if (used + n > maxBytes) break;
    out += ch;
    used += n;
  }
  return out;
}

export function sanitizeComponent(name: string, maxBytes = 150): string {
  let n = (name ?? '').normalize('NFC').replace(ILLEGAL, '_');
  n = n.replace(/\s+/g, ' ').trim();
  n = n.replace(/^[.-]+/, '').replace(/[. ]+$/, '');
  if (n === '' || n === '.' || n === '..') return '';
  if (RESERVED.has(n.split('.')[0].toUpperCase())) n = '_' + n;
  return truncateUtf8(n, maxBytes).replace(/[. ]+$/, '');
}

export function extensionFor(format: string): string {
  return ({ jpeg: 'jpg', png: 'png', tiff: 'tif', txt: 'txt' } as Record<string, string>)[format] ?? 'pdf';
}

/** Render + sanitise + extension. ``addQr`` prefixes ``{qr}_`` when the template lacks ``{qr}``. */
export function buildFilename(template: string, v: TemplateVars, ext: string, addQr = false): string {
  let tpl = template;
  if (addQr && v.qr && !tpl.includes('{qr')) tpl = '{qr}_' + tpl;
  const base = sanitizeComponent(renderTemplate(tpl, v)) || 'scan';
  const e = ext.toLowerCase().replace(/[^a-z0-9]/g, '').slice(0, 5);
  return e ? `${base}.${e}` : base;
}

/** Token names used in a template that the backend doesn't know (they render as empty). */
export function unknownTokens(template: string): string[] {
  const out: string[] = [];
  for (const m of template.matchAll(TOKEN_RE)) {
    if (!(FILENAME_TOKENS as readonly string[]).includes(m[1])) out.push(m[1]);
  }
  return out;
}
