/**
 * Switching chat threads used to kill a live voice session.
 *
 * `FeralSocket.setSession` (lib/ws.js) rebinds the shared socket to a
 * different orchestrator session by closing the current WebSocket and
 * opening a new one against `?session_id=<token>`. `this.ws` is a fresh
 * object afterwards.
 *
 * `useVoiceMode.start` handed `RealtimeVoiceEngine` the socket BY VALUE:
 *
 *     new RealtimeVoiceEngine(socket.ws, { ... })
 *
 * so the engine kept a reference to the WebSocket that the thread switch
 * had just closed. Its close listener fired, `_attemptReconnect` found
 * no `_wsFactory`, re-checked the same dead reference eight times over
 * roughly 79 seconds of exponential backoff, and then reported
 * `degraded`. Every audio chunk in between was dropped, because the send
 * path is gated on `this._ws.readyState === OPEN`.
 *
 * The engine has supported a factory since it was written; nothing
 * passed one. Passing `() => socket.ws` is the whole fix: the engine
 * re-resolves the live socket off the shared singleton on every
 * reconnect attempt, and the first attempt lands after 1s, by which time
 * the intentional rebind (`_scheduleReconnect` uses a 0ms delay for one)
 * has already reconnected.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';

vi.mock('../../lib/voiceRealtime', () => {
  const captured = { wsArg: null, callbacks: null };
  function RealtimeVoiceEngine(wsOrFactory, callbacks = {}) {
    captured.wsArg = wsOrFactory;
    captured.callbacks = callbacks;
    this.start = vi.fn().mockResolvedValue(undefined);
    this.stop = vi.fn();
  }
  return { RealtimeVoiceEngine, __captured: captured };
});

const fakeSocket = {
  ws: { readyState: 1, id: 'first' },
  listeners: new Set(),
  subscribe(fn) {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  },
};

vi.mock('../../hooks/useFeralSocket', () => ({
  useFeralSocket: () => fakeSocket,
}));

vi.mock('../../lib/api', () => ({
  apiJson: vi.fn().mockResolvedValue({ features: { voice_provider: 'openai' } }),
}));

import { useVoiceMode } from '../../hooks/useVoiceMode';
import * as engineModule from '../../lib/voiceRealtime';

beforeEach(() => {
  fakeSocket.listeners.clear();
  fakeSocket.ws = { readyState: 1, id: 'first' };
  engineModule.__captured.wsArg = null;
});

describe('voice survives a thread switch', () => {
  it('hands the engine a live socket resolver, not one frozen socket', async () => {
    const { result } = renderHook(() => useVoiceMode());
    await act(async () => { await result.current.start(); });

    const arg = engineModule.__captured.wsArg;
    expect(
      typeof arg,
      'the engine was handed the socket by value, so a thread switch '
      + 'leaves it holding a closed WebSocket forever',
    ).toBe('function');

    // What FeralSocket.setSession does on a thread switch.
    fakeSocket.ws.readyState = 3;
    fakeSocket.ws = { readyState: 1, id: 'second' };

    expect(arg().id).toBe('second');
    expect(arg().readyState).toBe(1);
  });
});

describe('the engine reconnects through the resolver', () => {
  let RealEngine;

  beforeEach(async () => {
    vi.useFakeTimers();
    const actual = await vi.importActual('../../lib/voiceRealtime');
    RealEngine = actual.RealtimeVoiceEngine;
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  function fakeWs(readyState = 1) {
    return {
      readyState,
      sent: [],
      listeners: {},
      addEventListener(type, fn) {
        (this.listeners[type] ||= []).push(fn);
      },
      fire(type) { (this.listeners[type] || []).forEach((fn) => fn()); },
      send(data) { this.sent.push(data); },
    };
  }

  it('rebinds to the socket the resolver returns after a close', async () => {
    const first = fakeWs();
    const second = fakeWs();
    let current = first;
    const states = [];
    const engine = new RealEngine(() => current, {
      onStateChange: (s) => states.push(s),
    });
    // Drive the socket wiring only; `start()` needs a mic and an
    // AudioContext, neither of which exists in jsdom.
    engine._active = true;
    engine._setWs(first);

    current = second;
    first.readyState = 3;
    first.fire('close');

    await vi.advanceTimersByTimeAsync(1200);

    expect(engine._ws).toBe(second);
    expect(engine.degraded).toBe(false);
    expect(states).toContain('active');
  });

  it('a frozen socket reference is what produced the 8-attempt collapse', async () => {
    // Characterisation of the pre-fix wiring, kept so the reason the
    // resolver exists stays legible. No factory: the engine can only
    // ever re-examine the one socket it was given.
    const only = fakeWs();
    const states = [];
    const engine = new RealEngine(only, {
      onStateChange: (s) => states.push(s),
      onError: () => {},
    });
    engine._active = true;
    engine._setWs(only);

    only.readyState = 3;
    only.fire('close');

    // 1+2+4+8+16+16+16+16 seconds of backoff, then it gives up.
    await vi.advanceTimersByTimeAsync(120000);

    expect(engine.degraded).toBe(true);
    expect(states).toContain('degraded');
  });
});
