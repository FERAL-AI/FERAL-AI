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

// Minimal AudioContext stand-in so the ScriptProcessor audio path runs in
// jsdom. Captures the created processor so a test can fire onaudioprocess
// manually and assert whether PCM is transmitted.
class FakeScriptProcessor {
  constructor() {
    this.onaudioprocess = null;
    this.connected = false;
    this.disconnected = false;
  }
  connect() { this.connected = true; }
  disconnect() { this.disconnected = true; }
}

class FakeAudioContext {
  constructor(opts) {
    this.sampleRate = opts?.sampleRate || 48000;
    this.state = 'running';
    this.destination = {};
    this.suspendCalls = 0;
    this.resumeCalls = 0;
    this.closeCalls = 0;
    this.processor = null;
    FakeAudioContext.instances.push(this);
  }
  createMediaStreamSource() { return { connect: () => {} }; }
  createScriptProcessor() {
    this.processor = new FakeScriptProcessor();
    return this.processor;
  }
  suspend() { this.suspendCalls += 1; this.state = 'suspended'; return Promise.resolve(); }
  resume() { this.resumeCalls += 1; this.state = 'running'; return Promise.resolve(); }
  close() { this.closeCalls += 1; this.state = 'closed'; return Promise.resolve(); }
}
FakeAudioContext.instances = [];

function fireAudioFrame(ctx) {
  ctx.processor?.onaudioprocess?.({
    inputBuffer: { getChannelData: () => new Float32Array(4096) },
  });
}

function audioFramesSent(sock) {
  return sock.sent
    .map((s) => { try { return JSON.parse(s); } catch { return {}; } })
    .filter((m) => m.type === 'audio_frame').length;
}

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


describe('usePerceptionShare privacy (Batch 2)', () => {
  let origAudioContext;

  beforeEach(() => {
    FakeAudioContext.instances = [];
    origAudioContext = globalThis.AudioContext;
    globalThis.AudioContext = FakeAudioContext;
  });

  afterEach(() => {
    globalThis.AudioContext = origAudioContext;
  });

  it('pause() stops audio transmission and suspends the AudioContext', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({ ok: true, json: async () => ({}) });

    const { result } = renderHook(() => usePerceptionShare({ token: 'tok', audio: true }));
    await act(async () => {
      await result.current.start();
    });
    await waitFor(() => expect(FakeSocket.instances.length).toBeGreaterThan(0));
    await waitFor(() => expect(FakeAudioContext.instances.length).toBeGreaterThan(0));

    const sock = FakeSocket.instances[0];
    const ctx = FakeAudioContext.instances[0];

    // While running, audio frames flow.
    act(() => { fireAudioFrame(ctx); });
    const runningCount = audioFramesSent(sock);
    expect(runningCount).toBeGreaterThan(0);

    // Pause must stop transmission AND suspend the context.
    act(() => { result.current.pause(); });
    expect(result.current.status).toBe('paused');
    expect(ctx.suspendCalls).toBeGreaterThan(0);

    // Any worklet callback that still fires must NOT emit PCM.
    act(() => { fireAudioFrame(ctx); });
    act(() => { fireAudioFrame(ctx); });
    expect(audioFramesSent(sock)).toBe(runningCount);
  });

  it('surfaces a distinct "disconnected" state on unexpected socket close', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({ ok: true, json: async () => ({}) });

    const { result } = renderHook(() => usePerceptionShare({ token: 'tok', audio: false }));
    await act(async () => {
      await result.current.start();
    });
    await waitFor(() => expect(FakeSocket.instances.length).toBeGreaterThan(0));
    expect(result.current.status).toBe('running');

    const sock = FakeSocket.instances[0];
    // Simulate a network drop (not a user-initiated stop()).
    act(() => { sock.onclose?.({ code: 1006, reason: 'network' }); });

    // Must NOT masquerade as an active or user-paused share.
    expect(result.current.status).toBe('disconnected');
  });
});
