/**
 * The whole voice UI used to vanish at the moment it had something to say.
 *
 * `useVoiceMode` published
 *
 *     active: state === 'active' || state === 'starting' || state === 'reconnecting'
 *
 * and `VoiceOverlay` keys its visibility off `active`. `degraded` is the
 * one state that means "voice stopped and here is why", so the overlay's
 * "Brain socket down, voice paused." string, the orb's alert and the
 * "End voice" button were all unreachable in production: the exact
 * moment the user needed an explanation, the surface disappeared.
 *
 * It was worse than a missing message. `Menubar` disables its voice
 * button with `state !== 'open' && !voice.active`, and a degraded voice
 * session is precisely a session whose brain socket is not open, so the
 * button went disabled too. The user could neither see the failure nor
 * end the session.
 *
 * The existing suites did not catch it because every one of them mocks
 * `useVoice` and hands the overlay `{ active: true, state: 'degraded' }`,
 * a pair the real hook cannot produce. They were green over dead code.
 * These tests drive the real hook.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act, render, cleanup } from '@testing-library/react';
import React from 'react';

vi.mock('../../lib/voiceRealtime', () => {
  const captured = { callbacks: null, wsArg: null };
  function RealtimeVoiceEngine(wsOrFactory, callbacks = {}) {
    captured.wsArg = wsOrFactory;
    captured.callbacks = callbacks;
    this.start = vi.fn().mockResolvedValue(undefined);
    this.stop = vi.fn();
    this.handleAudioResponse = vi.fn();
    this.handleTtsChunk = vi.fn().mockResolvedValue(undefined);
    this.handleSpeechStarted = vi.fn();
    this.handleTranscript = vi.fn();
  }
  return { RealtimeVoiceEngine, __captured: captured };
});

const fakeSocket = {
  ws: { readyState: 1 },
  listeners: new Set(),
  subscribe(fn) {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  },
  _dispatch(msg) { for (const fn of this.listeners) fn(msg); },
};

vi.mock('../../hooks/useFeralSocket', () => ({
  useFeralSocket: () => fakeSocket,
}));

vi.mock('../../lib/api', () => ({
  apiJson: vi.fn().mockResolvedValue({ features: { voice_provider: 'openai' } }),
}));

import { useVoiceMode } from '../../hooks/useVoiceMode';
import { VoiceProvider, useVoice } from '../../shell/VoiceContext';
import VoiceOverlay from '../../shell/VoiceOverlay';
import * as engineModule from '../../lib/voiceRealtime';

beforeEach(() => {
  cleanup();
  fakeSocket.listeners.clear();
  fakeSocket.ws = { readyState: 1 };
});

describe('degraded is a state the voice UI can actually be in', () => {
  it('start() over a closed socket produces degraded AND stays active', async () => {
    fakeSocket.ws = { readyState: 3 };
    const { result } = renderHook(() => useVoiceMode());
    await act(async () => { await result.current.start(); });
    expect(result.current.state).toBe('degraded');
    expect(
      result.current.active,
      'degraded is excluded from active, so the overlay that explains the '
      + 'failure never renders and the menubar button goes disabled',
    ).toBe(true);
  });

  it('the engine giving up after its reconnect budget keeps the UI up', async () => {
    const { result } = renderHook(() => useVoiceMode());
    await act(async () => { await result.current.start(); });
    expect(result.current.state).toBe('active');

    // What RealtimeVoiceEngine._attemptReconnect does once
    // RECONNECT_MAX_ATTEMPTS is spent.
    act(() => { engineModule.__captured.callbacks.onStateChange('degraded'); });

    expect(result.current.state).toBe('degraded');
    expect(result.current.active).toBe(true);
  });

  it('renders the explanation instead of hiding it', async () => {
    fakeSocket.ws = { readyState: 3 };
    let voice;
    function Probe() {
      voice = useVoice();
      return null;
    }
    const { container } = render(
      <VoiceProvider>
        <Probe />
        <VoiceOverlay />
      </VoiceProvider>,
    );
    await act(async () => { await voice.start(); });

    const overlay = container.querySelector('.v2-voice-overlay');
    expect(overlay.classList.contains('is-visible')).toBe(true);
    expect(overlay.getAttribute('aria-hidden')).toBe('false');
    expect(container.textContent).toContain('Brain socket down');
  });

  it('the degraded orb is the offline one, not a generic alert', async () => {
    fakeSocket.ws = { readyState: 3 };
    let voice;
    function Probe() {
      voice = useVoice();
      return null;
    }
    const { container } = render(
      <VoiceProvider>
        <Probe />
        <VoiceOverlay />
      </VoiceProvider>,
    );
    await act(async () => { await voice.start(); });
    expect(container.querySelector('.v2-orb').getAttribute('data-mode'))
      .toBe('offline');
  });
});
