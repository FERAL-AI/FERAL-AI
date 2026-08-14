/**
 * useResource: the contract every converted surface leans on.
 *
 * The one rule that matters: a failure must never produce a value. If
 * the hook ever substituted `[]` or `{}` for an answer it did not get,
 * every page downstream would go back to rendering an empty state off
 * a dead request.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { renderHook, waitFor, act } from '@testing-library/react';
import { useResource } from '../../hooks/useResource';
import { ApiError } from '../../lib/api';

function okOnce(body) {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
    headers: new Map(),
    clone() { return this; },
  };
}

function failOnce(status, body = { detail: 'nope' }) {
  return {
    ok: false,
    status,
    statusText: 'Error',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
    headers: new Map(),
    clone() { return this; },
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('useResource', () => {
  it('returns the parsed body on success', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(okOnce({ skills: [1, 2] }))));
    const { result } = renderHook(() => useResource('/skills'));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data).toEqual({ skills: [1, 2] });
    expect(result.current.error).toBeNull();
  });

  it('applies `select` only on success', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(okOnce({ skills: [1, 2] }))));
    const select = vi.fn((d) => d.skills);
    const { result } = renderHook(() => useResource('/skills', { select }));
    await waitFor(() => expect(result.current.data).toEqual([1, 2]));
    expect(select).toHaveBeenCalledTimes(1);
  });

  it('leaves data null on failure, never fabricating an empty value', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(failOnce(500))));
    const select = vi.fn((d) => d.skills || []);
    const { result } = renderHook(() => useResource('/skills', { select, silent: true }));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data).toBeNull();
    expect(select).not.toHaveBeenCalled();
    expect(result.current.error).toBeInstanceOf(ApiError);
    expect(result.current.error.status).toBe(500);
  });

  it('keeps the last good data when a later poll fails', async () => {
    let call = 0;
    vi.stubGlobal('fetch', vi.fn(() => {
      call += 1;
      return Promise.resolve(call === 1 ? okOnce({ n: 7 }) : failOnce(503));
    }));
    const { result } = renderHook(() => useResource('/x', { silent: true }));
    await waitFor(() => expect(result.current.data).toEqual({ n: 7 }));
    await act(async () => { await result.current.refresh(); });
    expect(result.current.data).toEqual({ n: 7 });
    expect(result.current.error.status).toBe(503);
  });

  it('surfaces status / code / reason off ApiError so ErrorState can branch', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(
      failOnce(401, { error: { code: 'no_key', reason: 'unauthenticated', detail: 'no api key' } }),
    )));
    const { result } = renderHook(() => useResource('/secret', { silent: true }));
    await waitFor(() => expect(result.current.error).not.toBeNull());
    expect(result.current.error.status).toBe(401);
    expect(result.current.error.code).toBe('no_key');
    expect(result.current.error.reason).toBe('unauthenticated');
  });

  it('wraps a network drop as status 0 / code network', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new TypeError('Failed to fetch'))));
    const { result } = renderHook(() => useResource('/x', { silent: true }));
    await waitFor(() => expect(result.current.error).not.toBeNull());
    expect(result.current.error.status).toBe(0);
    expect(result.current.error.code).toBe('network');
  });

  it('does not fetch while disabled, and fetches when enabled flips true', async () => {
    const fetchMock = vi.fn(() => Promise.resolve(okOnce({ n: 1 })));
    vi.stubGlobal('fetch', fetchMock);
    const { result, rerender } = renderHook(
      ({ on }) => useResource('/x', { enabled: on }),
      { initialProps: { on: false } },
    );
    expect(fetchMock).not.toHaveBeenCalled();
    expect(result.current.loading).toBe(false);
    rerender({ on: true });
    await waitFor(() => expect(result.current.data).toEqual({ n: 1 }));
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('loading does not flip back to true for a refresh, so pollers never flash', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(okOnce({ n: 1 }))));
    const { result } = renderHook(() => useResource('/x'));
    await waitFor(() => expect(result.current.loading).toBe(false));
    await act(async () => { await result.current.refresh(); });
    expect(result.current.loading).toBe(false);
  });
});
