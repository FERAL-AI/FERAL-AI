import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { ApiError, apiFetch, apiJson, _resetToastDedupeForTesting } from '../../lib/api';
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
    _resetToastDedupeForTesting();
    vi.stubGlobal('localStorage', {
      getItem: () => null,
      setItem: () => {},
      removeItem: () => {},
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    _resetToastDedupeForTesting();
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

/**
 * Toast dedupe.
 *
 * Observed after `feral stop` / `feral start`: three toasts stacked on
 * Home, "Failed to fetch /api/conversations/save", "Failed to fetch
 * /api/conversations/new" and "Request failed (503)
 * /api/conversations/active/thread", for a boot that then worked. Those
 * are three renderings of one fact. While the brain is not answering,
 * every poller on every mounted surface fails on the same tick, so the
 * count is bounded only by how many endpoints the page reads.
 *
 * Status 0 (the fetch never completed) and 503 (up, not ready) share one
 * key because to a reader they are the same event. Everything else keeps
 * its own key, so two genuinely different failures both still surface.
 */
describe('global toast dedupe', () => {
  beforeEach(() => {
    _resetGlobalErrorsForTesting();
    _resetToastDedupeForTesting();
    vi.stubGlobal('localStorage', { getItem: () => null });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    _resetToastDedupeForTesting();
  });

  async function errorsAfter(run) {
    const { renderHook } = await import('@testing-library/react');
    const { useGlobalErrors } = await import('../../hooks/useGlobalErrors');
    const { result } = renderHook(() => useGlobalErrors());
    await run();
    return result.current.errors;
  }

  it('shows one toast for a burst of unreachable-brain failures', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));
    const errors = await errorsAfter(async () => {
      await apiFetch('/api/conversations/save', { method: 'POST' }).catch(() => {});
      await apiFetch('/api/conversations/new', { method: 'POST' }).catch(() => {});
      await apiFetch('/api/jobs').catch(() => {});
    });
    expect(errors).toHaveLength(1);
  });

  it('collapses a 503 into the same unreachable-brain toast', async () => {
    let calls = 0;
    vi.stubGlobal('fetch', vi.fn(async () => {
      calls += 1;
      if (calls === 1) throw new TypeError('Failed to fetch');
      return {
        ok: false,
        status: 503,
        statusText: 'Service Unavailable',
        text: async () => '',
      };
    }));
    const errors = await errorsAfter(async () => {
      await apiFetch('/api/conversations/new', { method: 'POST' }).catch(() => {});
      await apiFetch('/api/conversations/active/thread').catch(() => {});
    });
    expect(errors).toHaveLength(1);
  });

  it('keeps two different real failures apart', async () => {
    let calls = 0;
    vi.stubGlobal('fetch', vi.fn(async () => {
      calls += 1;
      return {
        ok: false,
        status: 400,
        statusText: 'Bad Request',
        text: async () => JSON.stringify({ error: calls === 1 ? 'first' : 'second' }),
      };
    }));
    const errors = await errorsAfter(async () => {
      await apiFetch('/api/a').catch(() => {});
      await apiFetch('/api/b').catch(() => {});
    });
    expect(errors).toHaveLength(2);
  });

  it('suppresses only the repeat, not the first report', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 500,
      statusText: 'Error',
      text: async () => JSON.stringify({ error: 'boom' }),
    }));
    const errors = await errorsAfter(async () => {
      await apiFetch('/api/x').catch(() => {});
      await apiFetch('/api/x').catch(() => {});
    });
    expect(errors).toHaveLength(1);
    expect(errors[0].message).toContain('boom');
  });
});
