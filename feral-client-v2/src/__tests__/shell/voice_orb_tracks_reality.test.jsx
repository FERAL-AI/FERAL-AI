/**
 * The orb showed one thing while something else was happening.
 *
 * Three separate holes, all of the same shape (a value is computed and
 * nothing reads it):
 *
 * 1. `RealtimeVoiceEngine` computes microphone energy every 100ms and
 *    calls `onVADChange(true|false)`, with a comment saying it exists
 *    "because the orb animation wants to know when the user is talking".
 *    `useVoiceMode` never passed an `onVADChange`, so the callback had
 *    zero consumers and the value was discarded 10 times a second.
 *
 * 2. `audio_response` / `tts_chunk` frames carry `is_final`. The engine
 *    dropped it, so nothing on the desktop knew when the assistant
 *    started or stopped speaking. On the realtime path (which sends no
 *    `voice_state`) the orb therefore sat on one mode for the whole
 *    session, whoever was talking.
 *
 * 3. `Orb.jsx` styles an `offline` mode that no call site anywhere in
 *    the app ever asked for. The one state that means "the connection
 *    to the brain is gone" rendered as the generic `alerting`.
 *
 * These drive the real hook and the real overlay.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, act, cleanup } from '@testing-library/react';
import React from 'react';

vi.mock('../../lib/voiceRealtime', () => {
  const captured = { callbacks: null };
  function RealtimeVoiceEngine(wsOrFactory, callbacks = {}) {
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
};

vi.mock('../../hooks/useFeralSocket', () => ({
  useFeralSocket: () => fakeSocket,
}));

vi.mock('../../lib/api', () => ({
  apiJson: vi.fn().mockResolvedValue({ features: { voice_provider: 'openai' } }),
}));

import { VoiceProvider, useVoice } from '../../shell/VoiceContext';
import VoiceOverlay from '../../shell/VoiceOverlay';
import * as engineModule from '../../lib/voiceRealtime';

let voice;

function Probe() {
  voice = useVoice();
  return null;
}

async function mountVoice() {
  const utils = render(
    <VoiceProvider>
      <Probe />
      <VoiceOverlay />
    </VoiceProvider>,
  );
  await act(async () => { await voice.start(); });
  return utils;
}

const orbMode = (container) =>
  container.querySelector('.v2-orb').getAttribute('data-mode');

beforeEach(() => {
  cleanup();
  fakeSocket.listeners.clear();
  fakeSocket.ws = { readyState: 1 };
  engineModule.__captured.callbacks = null;
});

describe('the orb follows who is actually talking', () => {
  it('subscribes to the engine VAD callback at all', async () => {
    await mountVoice();
    expect(
      typeof engineModule.__captured.callbacks.onVADChange,
      'onVADChange is computed for the orb and has no consumer',
    ).toBe('function');
  });

  it('shows listening only while the mic hears the user', async () => {
    const { container } = await mountVoice();
    expect(orbMode(container)).toBe('idle');

    act(() => { engineModule.__captured.callbacks.onVADChange(true); });
    expect(orbMode(container)).toBe('listening');

    act(() => { engineModule.__captured.callbacks.onVADChange(false); });
    expect(orbMode(container)).toBe('idle');
  });

  it('shows speaking while the assistant has audio in flight', async () => {
    const { container } = await mountVoice();
    act(() => {
      engineModule.__captured.callbacks.onAssistantSpeaking(true);
    });
    expect(orbMode(container)).toBe('speaking');
    act(() => {
      engineModule.__captured.callbacks.onAssistantSpeaking(false);
    });
    expect(orbMode(container)).toBe('idle');
  });

  it('lets the assistant win over an open mic', async () => {
    const { container } = await mountVoice();
    act(() => {
      engineModule.__captured.callbacks.onVADChange(true);
      engineModule.__captured.callbacks.onAssistantSpeaking(true);
    });
    expect(orbMode(container)).toBe('speaking');
  });

  it('still lets the brain phase win over both', async () => {
    const { container } = await mountVoice();
    act(() => {
      engineModule.__captured.callbacks.onAssistantSpeaking(true);
      for (const fn of fakeSocket.listeners) {
        fn({ type: 'voice_state', payload: { state: 'processing' } });
      }
    });
    expect(orbMode(container)).toBe('thinking');
  });
});

describe('every orb mode has a producer', () => {
  it('offline is reachable', async () => {
    fakeSocket.ws = { readyState: 3 };
    const { container } = await mountVoice();
    expect(orbMode(container)).toBe('offline');
  });
});
