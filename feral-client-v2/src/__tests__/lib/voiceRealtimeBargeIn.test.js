/**
 * Barge-in must not tear down the playback AudioContext.
 *
 * `handleSpeechStarted` used to `close()` the playback AudioContext and
 * construct a fresh one on every barge-in. Stopping the scheduled
 * source nodes achieves the same audible result without re-acquiring
 * the output stream. The teardown matters because the browser's echo
 * canceller keys its reference signal off the output render stream and
 * must re-converge its delay estimate whenever that stream is
 * re-acquired — so the canceller was at its weakest immediately after
 * every barge-in, exactly when the assistant resumes speaking into an
 * open mic. Speaker bleed that escapes cancellation is transcribed as
 * user speech and renders as a right-aligned bubble containing the
 * assistant's own words.
 *
 * NOTE: this pins the code path only. Whether echo actually occurred in
 * the operator's session is not established here — that needs a real
 * browser with real speakers.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { RealtimeVoiceEngine } from '../../lib/voiceRealtime';

class FakeBufferSource {
  constructor(ctx) {
    this._ctx = ctx;
    this.buffer = null;
    this.onended = null;
    this.started = false;
    this.stopped = false;
  }

  connect() {}

  start() { this.started = true; }

  stop() {
    if (this.stopped) throw new Error('already stopped');
    this.stopped = true;
  }
}

class FakeAudioContext {
  static instances = [];

  constructor() {
    this.state = 'running';
    this.currentTime = 0;
    this.destination = {};
    this.closed = false;
    this.sources = [];
    FakeAudioContext.instances.push(this);
  }

  createBuffer(channels, length) {
    return { duration: 0.1, getChannelData: () => new Float32Array(length) };
  }

  createBufferSource() {
    const s = new FakeBufferSource(this);
    this.sources.push(s);
    return s;
  }

  close() {
    this.closed = true;
    return Promise.resolve();
  }

  resume() { return Promise.resolve(); }
}

function engineWithPlayback() {
  const engine = new RealtimeVoiceEngine({ readyState: 1, send: vi.fn() }, {});
  engine._playbackCtx = new FakeAudioContext();
  return engine;
}

// One PCM16 sample, base64 encoded — enough for handleAudioResponse.
const PCM_B64 = btoa('\x00\x01\x00\x01');

beforeEach(() => {
  FakeAudioContext.instances = [];
  vi.stubGlobal('AudioContext', FakeAudioContext);
});

describe('RealtimeVoiceEngine barge-in', () => {
  it('does not close or replace the playback AudioContext', () => {
    const engine = engineWithPlayback();
    const ctxBefore = engine._playbackCtx;

    engine.handleSpeechStarted();

    expect(engine._playbackCtx).toBe(ctxBefore);
    expect(ctxBefore.closed).toBe(false);
    // No replacement context was constructed.
    expect(FakeAudioContext.instances).toHaveLength(1);
  });

  it('stops audio already scheduled on the timeline', () => {
    const engine = engineWithPlayback();
    engine.handleAudioResponse({ data_b64: PCM_B64, is_final: false });
    const scheduled = engine._playbackCtx.sources[0];
    expect(scheduled.started).toBe(true);

    engine.handleSpeechStarted();

    expect(scheduled.stopped).toBe(true);
    expect(engine._nextPlayTime).toBe(0);
  });

  it('drops finished sources so the tracking set does not grow', () => {
    const engine = engineWithPlayback();
    engine.handleAudioResponse({ data_b64: PCM_B64, is_final: false });
    expect(engine._activeSources.size).toBe(1);

    // Playback finishing fires onended.
    engine._playbackCtx.sources[0].onended();
    expect(engine._activeSources.size).toBe(0);
  });

  it('survives a barge-in when a source has already ended', () => {
    const engine = engineWithPlayback();
    engine.handleAudioResponse({ data_b64: PCM_B64, is_final: false });
    const scheduled = engine._playbackCtx.sources[0];
    scheduled.stop();

    // A double-stop throws in real WebAudio too; it must not escape.
    expect(() => engine.handleSpeechStarted()).not.toThrow();
  });

  it('invokes the onSpeechStarted callback', () => {
    const onSpeechStarted = vi.fn();
    const engine = new RealtimeVoiceEngine(
      { readyState: 1, send: vi.fn() }, { onSpeechStarted },
    );
    engine._playbackCtx = new FakeAudioContext();

    engine.handleSpeechStarted();
    expect(onSpeechStarted).toHaveBeenCalled();
  });
});
