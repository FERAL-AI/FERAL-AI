/**
 * Client half of the chained-pipeline latency work.
 *
 * Two behaviours, both of which were silently wrong before:
 *
 * 1. Playback routed on the FRAME TYPE, not the encoding. The old
 *    condition read `if (encoding === 'mp3' || type === 'audio_chunk')`
 *    and the second half ate the first: the chained pipeline sends all
 *    its audio on `audio_chunk` whatever the codec, so raw PCM went to
 *    `decodeAudioData`, which cannot parse headerless samples and threw
 *    `EncodingError` into a `.catch` that swallowed it. Silent
 *    assistant, nothing in the console. That became load-bearing the
 *    moment the pipeline started streaming PCM incrementally, which is
 *    what lets playback start on the first 100ms instead of after the
 *    last byte of the reply.
 *
 * 2. There was no barge-in. The brain now cancels the turn when its
 *    VAD hears the user talk over the reply and sends `voice_cancel`,
 *    but audio already scheduled on the AudioContext keeps playing
 *    unless the client stops the sources by hand.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, act } from '@testing-library/react';
import { VoiceFullscreen } from '../../../pages/phone/VoiceFullscreen';
import { __resetAudioContextForTests } from '../../../lib/audioContext';

let frameListeners = [];
let decodeCalls = [];
let createdSources = [];

function makeShell(overrides = {}) {
  return {
    sendFrame: vi.fn(),
    subscribeFrame: vi.fn((cb) => {
      frameListeners.push(cb);
      return () => {
        frameListeners = frameListeners.filter((l) => l !== cb);
      };
    }),
    voice_config: { mode: 'chained' },
    node: null,
    ...overrides,
  };
}

function pushFrame(type, payload = {}) {
  act(() => {
    frameListeners.forEach((cb) => cb({ type, payload }));
  });
}

// A short run of PCM16 samples, base64'd the way the brain sends them.
const PCM_B64 = btoa(String.fromCharCode(...new Uint8Array(480).fill(1)));

async function flushPlaybackQueue() {
  // The playback queue is a promise chain; give it a few microtask
  // turns to drain before asserting on what reached the graph.
  await act(async () => {
    for (let i = 0; i < 10; i += 1) {
      await Promise.resolve();
    }
  });
}

beforeEach(() => {
  frameListeners = [];
  decodeCalls = [];
  createdSources = [];
  __resetAudioContextForTests();

  vi.stubGlobal('requestAnimationFrame', vi.fn((cb) => setTimeout(cb, 0)));
  vi.stubGlobal('cancelAnimationFrame', vi.fn((id) => clearTimeout(id)));
  vi.stubGlobal('navigator', {
    ...navigator,
    vibrate: vi.fn(() => true),
    mediaDevices: {
      getUserMedia: vi.fn(() =>
        Promise.resolve({ getTracks: () => [{ stop: vi.fn() }] }),
      ),
    },
  });

  // A REGULAR function, not an arrow. `lib/audioContext.js` builds the
  // shared context with `new AudioContext()`, and an arrow function is
  // not a constructor, so an arrow stub throws
  // "() => ({...}) is not a constructor" inside the helper's try/catch
  // and every playback assertion silently sees zero audio.
  vi.stubGlobal(
    'AudioContext',
    vi.fn(function FakeAudioContext() {
      return {
        state: 'running',
        resume: vi.fn(() => Promise.resolve()),
        destination: {},
        currentTime: 0,
        createMediaStreamSource: vi.fn(() => ({ connect: vi.fn() })),
        createAnalyser: vi.fn(() => ({
          fftSize: 256,
          frequencyBinCount: 128,
          getByteFrequencyData: vi.fn((arr) => arr.fill(0)),
          connect: vi.fn(),
        })),
        createBuffer: vi.fn((channels, length) => {
          const ch = new Float32Array(length);
          return { duration: length / 24000, getChannelData: vi.fn(() => ch) };
        }),
        createBufferSource: vi.fn(() => {
          const source = {
            connect: vi.fn(),
            start: vi.fn(),
            stop: vi.fn(),
            buffer: null,
            onended: null,
          };
          createdSources.push(source);
          return source;
        }),
        decodeAudioData: vi.fn(async (buf) => {
          decodeCalls.push(buf);
          return { duration: 0.1 };
        }),
        close: vi.fn(() => Promise.resolve()),
      };
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  frameListeners = [];
  __resetAudioContextForTests();
});

describe('VoiceFullscreen PCM playback', () => {
  it('plays pcm16 on an audio_chunk frame without calling decodeAudioData', async () => {
    render(<VoiceFullscreen open onClose={vi.fn()} shell={makeShell()} />);

    pushFrame('audio_chunk', {
      data_b64: PCM_B64,
      encoding: 'pcm16',
      sample_rate: 24000,
      chunk_index: 1,
      is_final: false,
    });
    await flushPlaybackQueue();

    expect(decodeCalls).toHaveLength(0);
    expect(createdSources.length).toBeGreaterThan(0);
    expect(createdSources[0].start).toHaveBeenCalled();
  });

  it('still decodes mp3 on an audio_chunk frame', async () => {
    render(<VoiceFullscreen open onClose={vi.fn()} shell={makeShell()} />);

    pushFrame('audio_chunk', {
      data_b64: PCM_B64,
      encoding: 'mp3',
      chunk_index: 1,
      is_final: false,
    });
    await flushPlaybackQueue();

    expect(decodeCalls).toHaveLength(1);
  });

  it('schedules successive pcm frames back to back rather than overlapping', async () => {
    render(<VoiceFullscreen open onClose={vi.fn()} shell={makeShell()} />);

    for (let i = 1; i <= 3; i += 1) {
      pushFrame('audio_chunk', {
        data_b64: PCM_B64,
        encoding: 'pcm16',
        sample_rate: 24000,
        chunk_index: i,
        is_final: false,
      });
    }
    await flushPlaybackQueue();

    expect(createdSources).toHaveLength(3);
    const startTimes = createdSources.map((s) => s.start.mock.calls[0][0]);
    for (let i = 1; i < startTimes.length; i += 1) {
      expect(startTimes[i]).toBeGreaterThan(startTimes[i - 1]);
    }
  });

  it('ignores the empty is_final sentinel frame', async () => {
    render(<VoiceFullscreen open onClose={vi.fn()} shell={makeShell()} />);

    pushFrame('audio_chunk', {
      data_b64: '',
      encoding: 'pcm16',
      chunk_index: 9,
      is_final: true,
    });
    await flushPlaybackQueue();

    expect(createdSources).toHaveLength(0);
    expect(decodeCalls).toHaveLength(0);
  });
});

describe('VoiceFullscreen barge-in', () => {
  it('stops audio already scheduled when voice_cancel arrives', async () => {
    render(<VoiceFullscreen open onClose={vi.fn()} shell={makeShell()} />);

    for (let i = 1; i <= 3; i += 1) {
      pushFrame('audio_chunk', {
        data_b64: PCM_B64,
        encoding: 'pcm16',
        sample_rate: 24000,
        chunk_index: i,
        is_final: false,
      });
    }
    await flushPlaybackQueue();
    expect(createdSources).toHaveLength(3);

    pushFrame('voice_cancel', {
      reason: 'barge_in',
      mode: 'chained',
      drop_pending_audio: true,
    });
    await flushPlaybackQueue();

    // Every scheduled source is stopped, not just the current one:
    // the queue runs ahead of the speaker, so leaving the tail
    // scheduled means the user keeps hearing the answer they
    // interrupted.
    createdSources.forEach((source) => {
      expect(source.stop).toHaveBeenCalled();
    });
  });

  it('returns the orb to listening on voice_cancel', async () => {
    const { getByTestId } = render(
      <VoiceFullscreen open onClose={vi.fn()} shell={makeShell()} />,
    );

    pushFrame('voice_state', { state: 'speaking', mode: 'chained' });
    pushFrame('voice_cancel', { reason: 'barge_in', mode: 'chained' });
    await flushPlaybackQueue();

    expect(getByTestId('voice-fullscreen')).toBeInTheDocument();
  });

  it('plays audio again after a cancel', async () => {
    render(<VoiceFullscreen open onClose={vi.fn()} shell={makeShell()} />);

    pushFrame('audio_chunk', {
      data_b64: PCM_B64, encoding: 'pcm16', chunk_index: 1, is_final: false,
    });
    await flushPlaybackQueue();
    pushFrame('voice_cancel', { reason: 'barge_in' });
    await flushPlaybackQueue();

    const before = createdSources.length;
    pushFrame('audio_chunk', {
      data_b64: PCM_B64, encoding: 'pcm16', chunk_index: 1, is_final: false,
    });
    await flushPlaybackQueue();

    expect(createdSources.length).toBeGreaterThan(before);
  });
});
