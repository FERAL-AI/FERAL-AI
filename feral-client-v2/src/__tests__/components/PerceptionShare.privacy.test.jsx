/**
 * Perception share privacy regressions.
 *
 * Two defects this file pins, both of which shipped green:
 *
 *   1. The "you are being recorded" chip never appeared. The hook kept
 *      its state in per-component useState, so <PerceptionShare /> (the
 *      Devices pane) and <PerceptionShare.FloatingChip /> (the shell
 *      dock) held two independent copies of `status`. Starting the share
 *      from the pane left the chip's copy on 'idle' and the chip's
 *      `status !== 'running'` early return hid the indicator for the
 *      whole session.
 *
 *   2. Muting the mic did not mute. `processor.onaudioprocess` is
 *      assigned once inside start() and never reassigned, so it captured
 *      `controls.audioMuted` as it was at attach time. Toggling the mic
 *      afterwards flipped the icon to MicOff and aria-pressed to true
 *      while PCM chunks kept going out over the socket.
 *
 * Both assertions are deliberately made on observable behaviour (what
 * renders, what reaches WebSocket.send) rather than on the flags, since
 * the flags were correct in both bugs.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { act, fireEvent, waitFor, renderHook } from '@testing-library/react';

import { renderV2 } from '../_helpers/renderV2';
import PerceptionShare from '../../components/PerceptionShare';
import {
  usePerceptionShare,
  _resetPerceptionShareForTesting,
} from '../../hooks/usePerceptionShare';

class FakeSocket {
  constructor(url, protocols) {
    this.url = url;
    this.protocols = protocols;
    this.readyState = 0;
    this.sent = [];
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

  /** Every envelope of `type` this socket has been asked to send. */
  framesOfType(type) {
    return this.sent
      .map((raw) => { try { return JSON.parse(raw); } catch { return null; } })
      .filter((msg) => msg?.type === type);
  }
}
FakeSocket.instances = [];

function makeStream() {
  const audioTrack = { kind: 'audio', enabled: true, stop: vi.fn() };
  const videoTrack = { kind: 'video', enabled: true, stop: vi.fn() };
  return {
    audioTrack,
    videoTrack,
    getTracks: () => [audioTrack, videoTrack],
    getAudioTracks: () => [audioTrack],
    getVideoTracks: () => [videoTrack],
  };
}

/**
 * Minimal AudioContext double. jsdom has no Web Audio, so the real
 * ScriptProcessor never exists; this exposes the processor node so the
 * test can drive `onaudioprocess` by hand, which is the only way to
 * observe what the audio path actually emits.
 */
class FakeAudioContext {
  constructor(options) {
    this.options = options;
    this.destination = { id: 'destination' };
    this.processor = null;
    this.closed = false;
    FakeAudioContext.instances.push(this);
  }

  createMediaStreamSource(stream) {
    this.sourceStream = stream;
    return { connect: vi.fn() };
  }

  createScriptProcessor() {
    this.processor = { onaudioprocess: null, connect: vi.fn(), disconnect: vi.fn() };
    return this.processor;
  }

  close() { this.closed = true; }
}
FakeAudioContext.instances = [];

/** One buffer of non-silent mono audio, as the ScriptProcessor delivers it. */
function audioEvent() {
  const samples = new Float32Array(512);
  for (let i = 0; i < samples.length; i += 1) samples[i] = Math.sin(i / 8) * 0.5;
  return { inputBuffer: { getChannelData: () => samples } };
}

let stream;

beforeEach(() => {
  _resetPerceptionShareForTesting();
  FakeSocket.instances = [];
  FakeAudioContext.instances = [];
  stream = makeStream();
  Object.defineProperty(navigator, 'mediaDevices', {
    value: { getUserMedia: vi.fn().mockResolvedValue(stream) },
    configurable: true,
  });
  // Pre-seed the pair token so the auth path (covered in
  // hooks/usePerceptionShare.test.jsx) stays out of these cases.
  window.sessionStorage.setItem('feral.session.pair_token', 'tok-test');
});

afterEach(() => {
  _resetPerceptionShareForTesting();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('perception share indicator', () => {
  it('shows the floating chip when the share is started from the other mount', async () => {
    // The two mounts the real app has: the Devices pane and the shell dock.
    const { getByTestId, queryByTestId } = renderV2(
      <>
        <PerceptionShare />
        <PerceptionShare.FloatingChip />
      </>,
    );

    expect(queryByTestId('perception-floating-chip')).toBeNull();

    await act(async () => {
      fireEvent.click(getByTestId('perception-start'));
    });

    // The chip mount never called start() itself. If the two mounts do not
    // share one store, this never appears and the camera streams with no
    // on-screen indicator.
    await waitFor(() => {
      expect(getByTestId('perception-floating-chip')).toBeInTheDocument();
    });
    expect(getByTestId('perception-floating-chip')).toHaveTextContent(/Sharing camera/);
  });

  it('keeps the indicator up while paused, because the camera is still held', async () => {
    const { getByTestId } = renderV2(
      <>
        <PerceptionShare />
        <PerceptionShare.FloatingChip />
      </>,
    );

    await act(async () => {
      fireEvent.click(getByTestId('perception-start'));
    });
    await waitFor(() => expect(getByTestId('perception-floating-chip')).toBeInTheDocument());

    await act(async () => {
      fireEvent.click(getByTestId('perception-pause'));
    });

    expect(getByTestId('perception-floating-chip')).toHaveTextContent(/paused/i);
  });

  it('renders the recording dot with no inline animation', async () => {
    const { getByTestId } = renderV2(
      <>
        <PerceptionShare />
        <PerceptionShare.FloatingChip />
      </>,
    );

    await act(async () => {
      fireEvent.click(getByTestId('perception-start'));
    });
    await waitFor(() => expect(getByTestId('perception-floating-chip')).toBeInTheDocument());

    const dot = getByTestId('perception-chip-dot');
    expect(dot).toHaveClass('v2-perception-chip-dot');
    // An inline animation outranks the stylesheet, so prefers-reduced-motion
    // could not switch it off. The keyframe lives in styles/ui.css.
    expect(dot.style.animation).toBe('');
  });
});

describe('perception share microphone mute', () => {
  beforeEach(() => {
    vi.stubGlobal('WebSocket', FakeSocket);
    vi.stubGlobal('AudioContext', FakeAudioContext);
  });

  it('stops emitting audio frames when the mic is muted after start()', async () => {
    const { result } = renderHook(() => usePerceptionShare({ audio: true, video: false, token: 'tok-mute' }));

    await act(async () => {
      await result.current.start();
    });
    await waitFor(() => expect(result.current.status).toBe('running'));

    const ctx = FakeAudioContext.instances[0];
    const socket = FakeSocket.instances[0];
    expect(ctx?.processor?.onaudioprocess).toBeTypeOf('function');

    // Unmuted: audio reaches the socket.
    await act(async () => { ctx.processor.onaudioprocess(audioEvent()); });
    expect(socket.framesOfType('audio_frame')).toHaveLength(1);

    // Mute AFTER the share is running, the case the old closure could
    // not see.
    await act(async () => { result.current.toggleAudio(); });
    expect(result.current.controls.audioMuted).toBe(true);

    await act(async () => {
      ctx.processor.onaudioprocess(audioEvent());
      ctx.processor.onaudioprocess(audioEvent());
    });

    // Nothing new left the browser, and the mic track itself is off.
    expect(socket.framesOfType('audio_frame')).toHaveLength(1);
    expect(result.current.stats.audioChunksSent).toBe(1);
    expect(stream.audioTrack.enabled).toBe(false);

    // Unmuting resumes, so mute is a gate and not a teardown.
    await act(async () => { result.current.toggleAudio(); });
    expect(stream.audioTrack.enabled).toBe(true);
    await act(async () => { ctx.processor.onaudioprocess(audioEvent()); });
    expect(socket.framesOfType('audio_frame')).toHaveLength(2);
  });

  it('stops emitting audio while paused', async () => {
    const { result } = renderHook(() => usePerceptionShare({ audio: true, video: false, token: 'tok-pause' }));

    await act(async () => {
      await result.current.start();
    });
    await waitFor(() => expect(result.current.status).toBe('running'));

    const ctx = FakeAudioContext.instances[0];
    const socket = FakeSocket.instances[0];

    await act(async () => { ctx.processor.onaudioprocess(audioEvent()); });
    expect(socket.framesOfType('audio_frame')).toHaveLength(1);

    await act(async () => { result.current.pause(); });
    await act(async () => { ctx.processor.onaudioprocess(audioEvent()); });

    // Paused is shown as "not sending", so it must not send.
    expect(socket.framesOfType('audio_frame')).toHaveLength(1);
    expect(stream.audioTrack.enabled).toBe(false);
  });
});
