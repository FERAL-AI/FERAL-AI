/**
 * Audit-r11 — Bug 3 (silent voice on WebUI desktop).
 *
 * These tests pin the regression that left `useVoiceMode` with a
 * `RealtimeVoiceEngine` instance but no subscription to the shared
 * `FeralSocket`, so incoming `audio_response` / `tts_chunk` /
 * `speech_started` / `voice_status` frames from the brain were
 * dropped on the floor. Result: voice played in v1, was silent in v2
 * even with healthy realtime, and the new whisper fallback path was
 * inaudible.
 *
 * The contract pinned here:
 *   1. `useVoiceMode` subscribes to the shared socket and dispatches
 *      `audio_response` / `audio_delta` -> `engine.handleAudioResponse`.
 *   2. `tts_chunk` -> `engine.handleTtsChunk` (whisper / fallback path).
 *   3. `speech_started` -> `engine.handleSpeechStarted`.
 *   4. `voice_status` updates the `voiceStatus` published state so the
 *      VoiceOverlay banner can render. `state=available` clears it.
 *   5. The subscription is torn down on unmount (no socket leak).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';

// Mock the realtime engine so we can observe handler dispatches
// without booting an AudioContext / mic stream.
vi.mock('../../lib/voiceRealtime', () => {
  const handlers = {
    handleAudioResponse: vi.fn(),
    handleTtsChunk: vi.fn().mockResolvedValue(undefined),
    handleSpeechStarted: vi.fn(),
    handleTranscript: vi.fn(),
    start: vi.fn().mockResolvedValue(undefined),
    stop: vi.fn(),
  };
  function RealtimeVoiceEngine() {
    Object.assign(this, handlers);
  }
  return {
    RealtimeVoiceEngine,
    __engineHandlers: handlers,
  };
});

// Stub the shared FeralSocket so subscribe() is observable and
// dispatching messages doesn't require a real WebSocket.
const fakeSocket = {
  ws: { readyState: 1 },
  listeners: new Set(),
  subscribe: vi.fn(function (fn) {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }),
  _dispatch(msg) {
    for (const fn of this.listeners) fn(msg);
  },
};

vi.mock('../../hooks/useFeralSocket', () => ({
  useFeralSocket: () => fakeSocket,
}));

vi.mock('../../lib/api', () => ({
  apiJson: vi.fn().mockResolvedValue({ features: { voice_provider: 'openai' } }),
}));

import { useVoiceMode } from '../../hooks/useVoiceMode';
import * as voiceRealtimeModule from '../../lib/voiceRealtime';

beforeEach(() => {
  fakeSocket.listeners.clear();
  fakeSocket.subscribe.mockClear();
  const h = voiceRealtimeModule.__engineHandlers;
  h.handleAudioResponse.mockClear();
  h.handleTtsChunk.mockClear();
  h.handleSpeechStarted.mockClear();
  h.handleTranscript.mockClear();
  h.start.mockClear();
  h.stop.mockClear();
});

afterEach(() => {
  vi.clearAllTimers();
});

describe('useVoiceMode brain-frame dispatch', () => {
  it('subscribes to the shared FeralSocket on mount', () => {
    renderHook(() => useVoiceMode());
    expect(fakeSocket.subscribe).toHaveBeenCalledTimes(1);
    expect(fakeSocket.listeners.size).toBe(1);
  });

  it('dispatches audio_response frames to engine.handleAudioResponse', async () => {
    const { result } = renderHook(() => useVoiceMode());
    // Boot the engine so engineRef.current is populated.
    await act(async () => {
      await result.current.start();
    });
    act(() => {
      fakeSocket._dispatch({ type: 'audio_response', payload: { data_b64: 'abc' } });
    });
    expect(voiceRealtimeModule.__engineHandlers.handleAudioResponse)
      .toHaveBeenCalledWith({ data_b64: 'abc' });
  });

  it('also dispatches audio_delta (alt frame name) to handleAudioResponse', async () => {
    const { result } = renderHook(() => useVoiceMode());
    await act(async () => { await result.current.start(); });
    act(() => {
      fakeSocket._dispatch({ type: 'audio_delta', payload: { data_b64: 'xyz' } });
    });
    expect(voiceRealtimeModule.__engineHandlers.handleAudioResponse)
      .toHaveBeenCalledWith({ data_b64: 'xyz' });
  });

  it('dispatches tts_chunk to engine.handleTtsChunk (whisper fallback path)', async () => {
    const { result } = renderHook(() => useVoiceMode());
    await act(async () => { await result.current.start(); });
    act(() => {
      fakeSocket._dispatch({
        type: 'tts_chunk',
        payload: { data_b64: 'ZmFrZQ==', format: 'mp3', is_final: true },
      });
    });
    expect(voiceRealtimeModule.__engineHandlers.handleTtsChunk)
      .toHaveBeenCalledWith({ data_b64: 'ZmFrZQ==', format: 'mp3', is_final: true });
  });

  it('dispatches speech_started to engine.handleSpeechStarted', async () => {
    const { result } = renderHook(() => useVoiceMode());
    await act(async () => { await result.current.start(); });
    act(() => {
      fakeSocket._dispatch({ type: 'speech_started', payload: {} });
    });
    expect(voiceRealtimeModule.__engineHandlers.handleSpeechStarted).toHaveBeenCalled();
  });

  it('dispatches transcript to engine.handleTranscript', async () => {
    // `handleTranscript` had zero callers, so `voice.transcript` never
    // populated and the VoiceOverlay caption stayed permanently empty.
    const { result } = renderHook(() => useVoiceMode());
    await act(async () => { await result.current.start(); });
    act(() => {
      fakeSocket._dispatch({
        type: 'transcript',
        payload: { text: 'hello brain', role: 'user', is_partial: false },
      });
    });
    expect(voiceRealtimeModule.__engineHandlers.handleTranscript)
      .toHaveBeenCalledWith({ text: 'hello brain', role: 'user', is_partial: false });
  });

  it('updates voiceStatus when brain emits voice_status state=degraded', () => {
    const { result } = renderHook(() => useVoiceMode());
    act(() => {
      fakeSocket._dispatch({
        type: 'voice_status',
        payload: {
          state: 'degraded',
          reason: 'openai_realtime_quota',
          provider: 'openai',
          fallback_provider: 'whisper',
          detail: 'You exceeded your current quota',
        },
      });
    });
    expect(result.current.voiceStatus).toEqual({
      state: 'degraded',
      reason: 'openai_realtime_quota',
      provider: 'openai',
      fallbackProvider: 'whisper',
      detail: 'You exceeded your current quota',
    });
  });

  it('updates voiceStatus when brain emits voice_status state=unavailable', () => {
    const { result } = renderHook(() => useVoiceMode());
    act(() => {
      fakeSocket._dispatch({
        type: 'voice_status',
        payload: { state: 'unavailable', reason: 'fallback_tts_failed' },
      });
    });
    expect(result.current.voiceStatus?.state).toBe('unavailable');
    expect(result.current.voiceStatus?.reason).toBe('fallback_tts_failed');
  });

  it('clears voiceStatus when brain emits voice_status state=available', () => {
    const { result } = renderHook(() => useVoiceMode());
    act(() => {
      fakeSocket._dispatch({
        type: 'voice_status',
        payload: { state: 'degraded', reason: 'openai_realtime_quota' },
      });
    });
    expect(result.current.voiceStatus?.state).toBe('degraded');
    act(() => {
      fakeSocket._dispatch({
        type: 'voice_status',
        payload: { state: 'available' },
      });
    });
    expect(result.current.voiceStatus).toBeNull();
  });

  it('ignores unknown frame types without throwing', () => {
    renderHook(() => useVoiceMode());
    expect(() => {
      fakeSocket._dispatch({ type: 'made_up_frame', payload: {} });
    }).not.toThrow();
  });

  it('unsubscribes from the socket on unmount', () => {
    const { unmount } = renderHook(() => useVoiceMode());
    expect(fakeSocket.listeners.size).toBe(1);
    unmount();
    expect(fakeSocket.listeners.size).toBe(0);
  });
});
