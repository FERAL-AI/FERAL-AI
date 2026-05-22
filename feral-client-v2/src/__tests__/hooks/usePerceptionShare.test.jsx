/**
 * usePerceptionShare auth-gate (Lane 11).
 *
 * Pins the behaviour change in this lane: opening the perception
 * WebSocket requires a pair token, surfaced via the
 * ``feral-token-<TOKEN>`` Sec-WebSocket-Protocol subprotocol.
 *
 * The hook used to open ``/v1/node`` with no credential — any visitor
 * to the WebUI could attach a browser camera daemon to the brain. The
 * fix:
 *   1. Resolve a token via the ``token`` prop, sessionStorage cache,
 *      or POST /api/devices/pair (cookie-auth'd to the dashboard).
 *   2. Fail loud + closed when no token is available — the WS is never
 *      opened anonymously.
 *
 * These tests stub ``WebSocket``, ``fetch``, and ``navigator.mediaDevices``
 * so they run in jsdom without hitting the network.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';

import { usePerceptionShare } from '../../hooks/usePerceptionShare';

class FakeSocket {
  constructor(url, protocols) {
    this.url = url;
    this.protocols = protocols;
    this.readyState = 0;
    this.sent = [];
    // Capture every socket instance for inspection.
    FakeSocket.instances.push(this);
    queueMicrotask(() => {
      this.readyState = 1;
      this.onopen?.();
    });
  }
  send(data) { this.sent.push(data); }
  close() {
    this.readyState = 3;
    this.onclose?.({ code: 1000, reason: 'test' });
  }
}
FakeSocket.instances = [];

const fakeStream = {
  getTracks: () => [{ stop: () => {} }],
  getVideoTracks: () => [],
  getAudioTracks: () => [],
};

beforeEach(() => {
  FakeSocket.instances = [];
  globalThis.WebSocket = FakeSocket;
  globalThis.navigator.mediaDevices = {
    getUserMedia: vi.fn().mockResolvedValue(fakeStream),
  };
  // sessionStorage starts clean.
  try { window.sessionStorage.clear(); } catch { /* ignore */ }
});

afterEach(() => {
  vi.restoreAllMocks();
});


describe('usePerceptionShare auth gate', () => {
  it('uses explicit token via Sec-WebSocket-Protocol', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue({ ok: true, json: async () => ({}) });

    const { result } = renderHook(() => usePerceptionShare({
      token: 'tok-explicit',
      audio: false,  // skip AudioWorklet path in jsdom
    }));

    await act(async () => {
      await result.current.start();
    });

    await waitFor(() => {
      expect(FakeSocket.instances.length).toBeGreaterThan(0);
    });
    const sock = FakeSocket.instances[0];
    expect(sock.url).toMatch(/\/v1\/node$/);
    expect(sock.protocols).toEqual(['feral-token-tok-explicit']);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('falls back to POST /api/devices/pair when no token cached', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      json: async () => ({ token: 'tok-fresh' }),
    });

    const { result } = renderHook(() => usePerceptionShare({ audio: false }));
    await act(async () => {
      await result.current.start();
    });

    await waitFor(() => {
      expect(FakeSocket.instances.length).toBeGreaterThan(0);
    });
    const sock = FakeSocket.instances[0];
    expect(sock.protocols).toEqual(['feral-token-tok-fresh']);
    expect(fetchSpy).toHaveBeenCalledWith(
      expect.stringMatching(/\/api\/devices\/pair$/),
      expect.objectContaining({
        method: 'POST',
        credentials: 'include',
      })
    );
    // Token cached for the next session.
    expect(window.sessionStorage.getItem('feral.session.pair_token')).toBe('tok-fresh');
  });

  it('refuses to open WS when pair endpoint returns 401', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({ ok: false, status: 401, json: async () => ({}) });

    const { result } = renderHook(() => usePerceptionShare({ audio: false }));
    await act(async () => {
      await result.current.start();
    });

    expect(FakeSocket.instances.length).toBe(0);
    expect(result.current.status).toBe('error');
    expect(result.current.error).toMatch(/sign in/i);
  });

  it('reuses sessionStorage-cached token without hitting the pair endpoint', async () => {
    window.sessionStorage.setItem('feral.session.pair_token', 'tok-cached');
    const fetchSpy = vi.spyOn(globalThis, 'fetch');

    const { result } = renderHook(() => usePerceptionShare({ audio: false }));
    await act(async () => {
      await result.current.start();
    });

    await waitFor(() => {
      expect(FakeSocket.instances.length).toBeGreaterThan(0);
    });
    const sock = FakeSocket.instances[0];
    expect(sock.protocols).toEqual(['feral-token-tok-cached']);
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});
