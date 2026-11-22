import { afterEach, describe, expect, it, vi } from 'vitest';
import { api, ApiError, buildInit, buildUrl, readCookie, setUnauthorizedHandler } from './client';

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

afterEach(() => {
  vi.restoreAllMocks();
  setUnauthorizedHandler(null);
  document.cookie = 'scanbox_csrf=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/';
});

describe('API client', () => {
  it('reads cookies', () => {
    expect(readCookie('scanbox_csrf', 'a=1; scanbox_csrf=tok%2Bx; b=2')).toBe('tok+x');
    expect(readCookie('missing', 'a=1')).toBeNull();
  });

  it('adds the CSRF header to every non-GET request, not to GET', () => {
    const cookies = 'scanbox_session=s; scanbox_csrf=abc123';
    for (const m of ['POST', 'PUT', 'PATCH', 'DELETE']) {
      const h = buildInit({ method: m, body: {} }, cookies).headers as Record<string, string>;
      expect(h['X-CSRF-Token']).toBe('abc123');
    }
    const g = buildInit({ method: 'GET' }, cookies).headers as Record<string, string>;
    expect(g['X-CSRF-Token']).toBeUndefined();
  });

  it('sends the header through fetch from document.cookie', async () => {
    document.cookie = 'scanbox_csrf=live-token; path=/';
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse(200, { ok: true }));
    await api.post('/api/documents/1/finish');
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect((init.headers as Record<string, string>)['X-CSRF-Token']).toBe('live-token');
    expect(init.credentials).toBe('same-origin');
  });

  it('builds query strings, skipping empty values and repeating arrays', () => {
    expect(buildUrl('/api/documents', { scope: 'history', q: '', status: ['done', 'failed'], x: null })).toBe(
      '/api/documents?scope=history&status=done&status=failed',
    );
  });

  it('turns {detail:{code,message}} into ApiError', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse(409, { detail: { code: 'already_claimed', message: 'Schon weg' } }));
    const err = await api.post('/api/documents/1/claim').catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ status: 409, code: 'already_claimed', message: 'Schon weg' });
  });

  it('formats FastAPI validation errors', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse(422, { detail: [{ loc: ['body', 'name'], msg: 'field required' }] }));
    const err = (await api.post('/api/profiles', {}).catch((e) => e)) as ApiError;
    expect(err.code).toBe('validation_error');
    expect(err.message).toContain('name: field required');
  });

  it('calls the unauthorized handler on 401 unless allowed', async () => {
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () => jsonResponse(401, { detail: { code: 'not_authenticated', message: 'x' } }));
    await api.get('/api/profiles').catch(() => {});
    expect(handler).toHaveBeenCalledWith('/api/profiles');
    handler.mockClear();
    await api.get('/api/auth/me', { allow401: true }).catch(() => {});
    expect(handler).not.toHaveBeenCalled();
  });

  it('reports network failures as code "network"', async () => {
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new TypeError('Failed to fetch'));
    const err = (await api.get('/api/scanner/status').catch((e) => e)) as ApiError;
    expect(err.code).toBe('network');
  });
});
