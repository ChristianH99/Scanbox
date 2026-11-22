/** Small fetch wrapper: JSON, CSRF double-submit header, uniform errors, 401 handling. */

export const CSRF_COOKIE = 'scanbox_csrf';
export const CSRF_HEADER = 'X-CSRF-Token';

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: unknown;
  constructor(status: number, code: string, message: string, details?: unknown) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

export function readCookie(name: string, cookieString: string = typeof document !== 'undefined' ? document.cookie : ''): string | null {
  for (const part of cookieString.split(';')) {
    const i = part.indexOf('=');
    if (i < 0) continue;
    if (part.slice(0, i).trim() === name) return decodeURIComponent(part.slice(i + 1).trim());
  }
  return null;
}

type UnauthorizedHandler = (path: string) => void;
let onUnauthorized: UnauthorizedHandler | null = null;
export function setUnauthorizedHandler(fn: UnauthorizedHandler | null): void {
  onUnauthorized = fn;
}

export interface RequestOptions {
  method?: string;
  body?: unknown;
  query?: Record<string, string | number | boolean | null | undefined | (string | number)[]>;
  headers?: Record<string, string>;
  signal?: AbortSignal;
  /** Don't trigger the global 401 → login redirect (e.g. for /auth/me probing). */
  allow401?: boolean;
  /** Return the raw Response (images, downloads). */
  raw?: boolean;
}

export function buildUrl(path: string, query?: RequestOptions['query']): string {
  const url = path.startsWith('/') ? path : `/api/${path}`;
  if (!query) return url;
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) {
    if (v === undefined || v === null || v === '') continue;
    if (Array.isArray(v)) v.forEach((x) => sp.append(k, String(x)));
    else sp.append(k, String(v));
  }
  const qs = sp.toString();
  return qs ? `${url}?${qs}` : url;
}

/** Build fetch init: JSON body + CSRF header for every non-GET request. */
export function buildInit(opts: RequestOptions, cookieString?: string): RequestInit {
  const method = (opts.method ?? 'GET').toUpperCase();
  const headers: Record<string, string> = { Accept: 'application/json', ...(opts.headers ?? {}) };
  let body: BodyInit | undefined;
  if (opts.body !== undefined) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(opts.body);
  }
  if (method !== 'GET' && method !== 'HEAD') {
    const token = readCookie(CSRF_COOKIE, cookieString);
    if (token) headers[CSRF_HEADER] = token;
  }
  return { method, headers, body, credentials: 'same-origin', signal: opts.signal };
}

export async function errorFromResponse(res: Response): Promise<ApiError> {
  let code = `http_${res.status}`;
  let message = res.statusText || `HTTP ${res.status}`;
  let details: unknown;
  try {
    const data = await res.json();
    const d = data?.detail;
    if (d && typeof d === 'object' && !Array.isArray(d) && 'message' in d) {
      code = String(d.code ?? code);
      message = String(d.message);
      details = d;
    } else if (Array.isArray(d)) {
      // FastAPI validation error list
      code = 'validation_error';
      message = d
        .map((e: { loc?: unknown[]; msg?: string }) => `${(e.loc ?? []).slice(1).join('.')}: ${e.msg ?? ''}`)
        .join('; ');
      details = d;
    } else if (typeof d === 'string') {
      message = d;
    }
  } catch {
    /* not JSON */
  }
  return new ApiError(res.status, code, message, details);
}

export async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const url = buildUrl(path, opts.query);
  let res: Response;
  try {
    res = await fetch(url, buildInit(opts));
  } catch (e) {
    if ((e as Error)?.name === 'AbortError') throw e;
    throw new ApiError(0, 'network', 'Server nicht erreichbar / server unreachable');
  }
  if (res.status === 401 && !opts.allow401) {
    onUnauthorized?.(path);
  }
  if (!res.ok) throw await errorFromResponse(res);
  if (opts.raw) return res as unknown as T;
  if (res.status === 204) return undefined as T;
  const ct = res.headers.get('content-type') ?? '';
  if (ct.includes('application/json')) return (await res.json()) as T;
  return (await res.text()) as unknown as T;
}

export const api = {
  get: <T>(path: string, opts: Omit<RequestOptions, 'method' | 'body'> = {}) => request<T>(path, opts),
  post: <T>(path: string, body?: unknown, opts: Omit<RequestOptions, 'method' | 'body'> = {}) =>
    request<T>(path, { ...opts, method: 'POST', body }),
  put: <T>(path: string, body?: unknown, opts: Omit<RequestOptions, 'method' | 'body'> = {}) =>
    request<T>(path, { ...opts, method: 'PUT', body }),
  patch: <T>(path: string, body?: unknown, opts: Omit<RequestOptions, 'method' | 'body'> = {}) =>
    request<T>(path, { ...opts, method: 'PATCH', body }),
  del: <T = void>(path: string, opts: Omit<RequestOptions, 'method' | 'body'> = {}) =>
    request<T>(path, { ...opts, method: 'DELETE' }),
  /** POST returning a blob (e.g. JPEG preview). */
  postBlob: async (path: string, body: unknown, opts: Omit<RequestOptions, 'method' | 'body'> = {}) => {
    const res = await request<Response>(path, { ...opts, method: 'POST', body, raw: true });
    return res.blob();
  },
};

export function isApiError(e: unknown, code?: string): e is ApiError {
  return e instanceof ApiError && (code === undefined || e.code === code);
}

export function errorMessage(e: unknown): string {
  if (e instanceof ApiError) return e.message;
  if (e instanceof Error) return e.message;
  return String(e);
}
