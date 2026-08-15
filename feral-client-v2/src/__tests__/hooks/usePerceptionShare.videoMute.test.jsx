/**
 * Perception share camera mute: the track, not just the send call.
 *
 * The defect this file pins is the video mirror of the microphone bug
 * that `components/PerceptionShare.privacy.test.jsx` already covers.
 * `videoMuted` gated only the emit path: `captureFrame()` returned early,
 * so no JPEG left the browser, but the MediaStreamTrack stayed
 * `enabled === true` and the camera stayed open at the hardware. The UI
 * said CameraOff and the camera light stayed lit, which is a worse
 * failure than the reverse: a user who sees the light on does not believe
 * the indicator, and an indicator nobody believes protects nobody.
 *
 * The assertions are therefore on the track state and on what actually
 * reaches WebSocket.send, never on `controls.videoMuted`. That flag was
 * already correct while the camera was still running.
 *
 * jsdom has no canvas rasteriser and no real <video>, so the capture path
 * is stubbed at exactly three points (2d context, toBlob, intrinsic video
 * size). Everything downstream of those, including the JPEG-to-base64
 * step and the socket envelope, is the hook's real code.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';

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

/** Wall-clock wait, so the real setInterval frame loop gets to run. */
function tick(ms) {
  return new Promise((resolve) => { setTimeout(resolve, ms); });
}

let stream;
let videoWidthDescriptor;
let videoHeightDescriptor;

beforeEach(() => {
  _resetPerceptionShareForTesting();
  FakeSocket.instances = [];
  stream = makeStream();

  vi.stubGlobal('WebSocket', FakeSocket);
  Object.defineProperty(navigator, 'mediaDevices', {
    value: { getUserMedia: vi.fn().mockResolvedValue(stream) },
    configurable: true,
  });
  window.sessionStorage.setItem('feral.session.pair_token', 'tok-video');

  // jsdom's <video> reports 0x0 and its canvas has no rasteriser, so
  // captureFrame() would bail before it ever reached the send. These
  // three stubs are the minimum needed for the real capture path to run.
  videoWidthDescriptor = Object.getOwnPropertyDescriptor(HTMLVideoElement.prototype, 'videoWidth');
  videoHeightDescriptor = Object.getOwnPropertyDescriptor(HTMLVideoElement.prototype, 'videoHeight');
  Object.defineProperty(HTMLVideoElement.prototype, 'videoWidth', { configurable: true, get: () => 640 });
  Object.defineProperty(HTMLVideoElement.prototype, 'videoHeight', { configurable: true, get: () => 480 });
  vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue(undefined);
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({ drawImage: () => {} });
  vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation(function toBlob(cb) {
    cb(new Blob([new Uint8Array([0xff, 0xd8, 0xff, 0xd9])], { type: 'image/jpeg' }));
  });
});

afterEach(() => {
  _resetPerceptionShareForTesting();
  if (videoWidthDescriptor) {
    Object.defineProperty(HTMLVideoElement.prototype, 'videoWidth', videoWidthDescriptor);
  }
  if (videoHeightDescriptor) {
    Object.defineProperty(HTMLVideoElement.prototype, 'videoHeight', videoHeightDescriptor);
  }
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

async function startRunningShare() {
  const rendered = renderHook(() => usePerceptionShare({
    fps: 10, audio: false, video: true, token: 'tok-video',
  }));
  await act(async () => { await rendered.result.current.start(); });
  await waitFor(() => expect(rendered.result.current.status).toBe('running'));
  return rendered;
}

describe('perception share camera mute', () => {
  it('disables the camera track when video is muted, and re-enables it on unmute', async () => {
    const { result } = await startRunningShare();
    const socket = FakeSocket.instances[0];

    // Baseline: the camera is on and frames are actually going out.
    expect(stream.videoTrack.enabled).toBe(true);
    await waitFor(
      () => expect(socket.framesOfType('video_frame').length).toBeGreaterThan(0),
      { timeout: 3000 },
    );

    await act(async () => { result.current.toggleVideo(); });

    // The assertion that matters: the camera is off at the source, so the
    // hardware light goes out. Asserting controls.videoMuted here would
    // have passed against the broken hook.
    expect(stream.videoTrack.enabled).toBe(false);

    const sentWhenMuted = socket.framesOfType('video_frame').length;
    await act(async () => { await tick(400); });
    expect(socket.framesOfType('video_frame')).toHaveLength(sentWhenMuted);

    // Mute is a gate, not a teardown: unmuting must bring both the camera
    // and the stream back without re-prompting for permission.
    await act(async () => { result.current.toggleVideo(); });
    expect(stream.videoTrack.enabled).toBe(true);
    expect(stream.videoTrack.stop).not.toHaveBeenCalled();
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1);

    await waitFor(
      () => expect(socket.framesOfType('video_frame').length).toBeGreaterThan(sentWhenMuted),
      { timeout: 3000 },
    );
  });

  it('disables the camera track while paused and re-enables it on resume', async () => {
    const { result } = await startRunningShare();
    const socket = FakeSocket.instances[0];

    await waitFor(
      () => expect(socket.framesOfType('video_frame').length).toBeGreaterThan(0),
      { timeout: 3000 },
    );

    await act(async () => { result.current.pause(); });

    // Paused is rendered as "camera paused, nothing sent". The camera
    // light has to agree with that.
    expect(stream.videoTrack.enabled).toBe(false);
    const sentWhenPaused = socket.framesOfType('video_frame').length;
    await act(async () => { await tick(400); });
    expect(socket.framesOfType('video_frame')).toHaveLength(sentWhenPaused);

    await act(async () => { result.current.resume(); });
    expect(stream.videoTrack.enabled).toBe(true);
    await waitFor(
      () => expect(socket.framesOfType('video_frame').length).toBeGreaterThan(sentWhenPaused),
      { timeout: 3000 },
    );
  });

  it('leaves the camera off after unmuting while paused', async () => {
    // Two gates, one track. Unmuting while paused must not turn the
    // camera back on: nothing is being sent, so nothing may be captured.
    const { result } = await startRunningShare();

    await act(async () => { result.current.toggleVideo(); });
    await act(async () => { result.current.pause(); });
    expect(stream.videoTrack.enabled).toBe(false);

    await act(async () => { result.current.toggleVideo(); });
    expect(result.current.controls.videoMuted).toBe(false);
    expect(stream.videoTrack.enabled).toBe(false);

    await act(async () => { result.current.resume(); });
    expect(stream.videoTrack.enabled).toBe(true);
  });
});
