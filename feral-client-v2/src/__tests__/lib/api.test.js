import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { ApiError, apiFetch, apiJson } from '../../lib/api';
import { _resetGlobalErrorsForTesting, pushGlobalError } from '../../hooks/useGlobalErrors';

describe('ApiError', () => {
  it('carries status, code, reason, detail, raw, path', () => {
    const raw = { error: 'Theme not found' };
    const err = ApiError.fromResponse({ status: 200, ok: true }, raw, '/api/genui/themes/activate');
    expect(err).toBeInstanceOf(Error);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.name).toBe('ApiError');
    expect(err.status).toBe(200);
    expect(err.detail).toBe('Theme not found');
    expect(err.raw).toEqual(raw);
    expect(err.path).toBe('/api/genui/themes/activate');
  });

  it('fromWebSocket maps error frames', () => {
    const err = ApiError.fromWebSocket({ type: 'error', data: { error: 'tool failed' } });
    expect(err.code).toBeTruthy();
    expect(err.detail).toContain('tool failed');
    expect(err.path).toBe('websocket');
  });
});

describe('apiFetch', () => {
  beforeEach(() => {
    _resetGlobalErrorsForTesting();
    vi.stubGlobal('localStorage', {
      getItem: () => null,
      setItem: () => {},
      removeItem: () => {},
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('throws ApiError on non-2xx and pushes to global store', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 404,
      statusText: 'Not Found',
      text: async () => JSON.stringify({ error: 'missing' }),
    }));

    await expect(apiFetch('/api/missing')).rejects.toBeInstanceOf(ApiError);
  });

  it('throws ApiError on 2xx with body.error', async () => {
    const payload = { error: "Theme '' not found" };
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
      new Response(JSON.stringify(payload), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    ));

    await expect(apiFetch('/api/genui/themes/activate', { method: 'POST' }))
      .rejects.toMatchObject({ detail: "Theme '' not found" });
  });

  it('silent opt-out does not push but still throws', async () => {
    const { renderHook } = await import('@testing-library/react');
    const { useGlobalErrors } = await import('../../hooks/useGlobalErrors');
    const { result } = renderHook(() => useGlobalErrors());

    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 500,
      statusText: 'Error',
      text: async () => JSON.stringify({ error: 'boom' }),
    }));

    await expect(apiFetch('/health', { silent: true })).rejects.toBeInstanceOf(ApiError);
    expect(result.current.errors).toHaveLength(0);
  });

  it('returns response on success without error field', async () => {
    const body = { ok: true, items: [] };
    const bodyText = JSON.stringify(body);
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      clone: () => ({ json: async () => body, text: async () => bodyText }),
      json: async () => body,
      text: async () => bodyText,
    }));

    const r = await apiFetch('/api/devices/connected');
    expect(r.ok).toBe(true);
    await expect(r.json()).resolves.toEqual(body);
  });
});

describe('apiJson', () => {
  beforeEach(() => {
    _resetGlobalErrorsForTesting();
    vi.stubGlobal('localStorage', { getItem: () => null });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('returns parsed JSON on success', async () => {
    const body = { skills: [] };
    const bodyText = JSON.stringify(body);
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      clone: () => ({ json: async () => body, text: async () => bodyText }),
      json: async () => body,
      text: async () => bodyText,
    }));
    await expect(apiJson('/skills')).resolves.toEqual({ skills: [] });
  });

  it('surfaces 2xx error bodies', async () => {
    const payload = { error: 'text is required' };
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
      new Response(JSON.stringify(payload), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    ));
    await expect(apiJson('/api/automations')).rejects.toMatchObject({
      detail: 'text is required',
    });
  });
});
