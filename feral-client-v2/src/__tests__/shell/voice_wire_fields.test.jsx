/**
 * Fields the brain works to produce and the desktop threw away.
 *
 * `voice/diagnostics.py` exists for one reason: to turn a machine tag
 * like `openai_realtime_auth` into a sentence a person can act on. It
 * fills `voice_status.cause`, `.summary` and `.recommendation`, and
 * `VoiceStatusPayload` also carries `privacy_downgrade` (FERAL refused
 * to send local-only audio to a cloud vendor) and `muted` (the brain is
 * dropping ingress).
 *
 * `useVoiceMode` copied five of those ten fields into its state and
 * `VoiceOverlay` rendered a hard-coded lookup table of five `reason`
 * strings, falling back to `detail`. So the only human explanation the
 * system produces reached no screen, and a reason the table did not
 * list rendered as a bare "Voice degraded" with no cause at all.
 *
 * Also pinned here:
 *   - `voice_config_ack`, the brain's answer to the config the engine
 *     sends on every start and reconnect. Nothing handled the frame, so
 *     a rejected config looked exactly like an accepted one.
 *   - transcript `confidence` and `is_partial`. `handleTranscript`
 *     forwarded both and the hook dropped them, so a caption that was
 *     still forming looked identical to a settled one.
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
    this.handleTranscript = vi.fn(function (payload) {
      captured.callbacks.onTranscript(
        payload.text,
        payload.is_partial,
        payload.role || 'assistant',
        { confidence: payload.confidence },
      );
    });
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

import { VoiceProvider, useVoice } from '../../shell/VoiceContext';
import VoiceOverlay from '../../shell/VoiceOverlay';

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

beforeEach(() => {
  cleanup();
  fakeSocket.listeners.clear();
  fakeSocket.ws = { readyState: 1 };
});

describe('the diagnosis reaches the screen', () => {
  const DIAGNOSED = {
    type: 'voice_status',
    payload: {
      state: 'unavailable',
      reason: 'key_rejected',
      provider: 'openai',
      fallback_provider: '',
      detail: 'openai_realtime: 401',
      cause: 'key_rejected',
      summary: 'OpenAI rejected the credential, so nothing can be transcribed.',
      recommendation: 'Issue a new key and run `feral key add --provider openai --set-active`.',
      privacy_downgrade: false,
      muted: false,
    },
  };

  it('keeps the three diagnosis fields on the published state', async () => {
    await mountVoice();
    act(() => { fakeSocket._dispatch(DIAGNOSED); });
    expect(voice.voiceStatus.cause).toBe('key_rejected');
    expect(voice.voiceStatus.summary).toContain('rejected the credential');
    expect(voice.voiceStatus.recommendation).toContain('feral key add');
  });

  it('renders the summary and the recommendation', async () => {
    const { container } = await mountVoice();
    act(() => { fakeSocket._dispatch(DIAGNOSED); });
    expect(container.textContent).toContain('rejected the credential');
    expect(container.textContent).toContain('feral key add');
  });

  it('says a refused cloud fallback is a refusal, not a degradation', async () => {
    const { container } = await mountVoice();
    act(() => {
      fakeSocket._dispatch({
        type: 'voice_status',
        payload: {
          state: 'unavailable',
          reason: 'privacy_downgrade',
          cause: 'privacy_downgrade',
          summary: 'The only fallback available is a cloud service.',
          recommendation: 'Fix the local engine.',
          privacy_downgrade: true,
        },
      });
    });
    expect(voice.voiceStatus.privacyDowngrade).toBe(true);
    expect(container.textContent).toMatch(/privacy/i);
  });

  it('shows the mic as muted when the brain says it is dropping ingress', async () => {
    const { container } = await mountVoice();
    act(() => {
      fakeSocket._dispatch({
        type: 'voice_status',
        payload: { state: 'degraded', reason: 'x', muted: true },
      });
    });
    expect(voice.voiceStatus.muted).toBe(true);
    expect(container.textContent).toMatch(/muted/i);
  });
});

describe('voice_config_ack', () => {
  it('is not dropped on the floor', async () => {
    await mountVoice();
    act(() => {
      fakeSocket._dispatch({
        type: 'voice_config_ack',
        payload: { mode: 'realtime', provider: 'gemini', status: 'ok' },
      });
    });
    expect(voice.configAck).toEqual({
      mode: 'realtime', provider: 'gemini', status: 'ok',
    });
  });

  it('adopts the provider the brain actually chose', async () => {
    await mountVoice();
    expect(voice.provider).toBe('openai');
    act(() => {
      fakeSocket._dispatch({
        type: 'voice_config_ack',
        payload: { mode: 'realtime', provider: 'gemini', status: 'ok' },
      });
    });
    expect(voice.provider).toBe('gemini');
  });

  it('a rejected config is a visible failure, not silence', async () => {
    const { container } = await mountVoice();
    act(() => {
      fakeSocket._dispatch({
        type: 'voice_config_ack',
        payload: { mode: 'realtime', provider: 'openai', status: 'unsupported' },
      });
    });
    expect(voice.configAck.status).toBe('unsupported');
    expect(container.textContent).toMatch(/did not accept|rejected/i);
  });
});

describe('transcript metadata', () => {
  it('marks a partial caption as still forming', async () => {
    const { container } = await mountVoice();
    act(() => {
      fakeSocket._dispatch({
        type: 'transcript',
        payload: { text: 'turn the ligh', role: 'user', is_partial: true, confidence: 0.9 },
      });
    });
    expect(voice.transcriptPartial).toBe(true);
    const caption = container.querySelector('.v2-voice-transcript');
    // Docked mode hides the caption; the state is what the overlay reads.
    expect(caption === null || caption.getAttribute('data-partial') === 'true').toBe(true);

    act(() => {
      fakeSocket._dispatch({
        type: 'transcript',
        payload: { text: 'turn the lights off', role: 'user', is_partial: false, confidence: 0.94 },
      });
    });
    expect(voice.transcriptPartial).toBe(false);
  });

  it('keeps the provider-reported confidence', async () => {
    await mountVoice();
    act(() => {
      fakeSocket._dispatch({
        type: 'transcript',
        payload: { text: 'mumble', role: 'user', is_partial: false, confidence: 0.31 },
      });
    });
    expect(voice.transcriptConfidence).toBeCloseTo(0.31);
  });
});
