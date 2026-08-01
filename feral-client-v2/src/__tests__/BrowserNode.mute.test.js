/**
 * BrowserNode microphone mute.
 *
 * The defect this closes: VoiceFullscreen's mute button flipped a local
 * boolean, dimmed the orb, and sent a `voice_mute` envelope that nothing
 * on the brain handled. BrowserNode itself was never told, so the
 * AudioWorklet kept running and kept posting `audio_chunk` frames for
 * the whole "muted" period. Every syllable still reached the brain and
 * whichever cloud realtime provider was live.
 *
 * A mute button's promise is that the audio does not leave the device,
 * so enforcement has to be here, at capture, not only at the brain.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';

class MockWebSocket {
  static OPEN = 1;
  static CLOSED = 3;
  constructor(url, protocols = []) {
    this.url = url;
    this.protocols = Array.isArray(protocols) ? protocols : [protocols];
    this.readyState = 0;
    this.sent = [];
    MockWebSocket.instances.push(this);
    setTimeout(() => {
      this.readyState = MockWebSocket.OPEN;
      this.onopen?.();
    }, 0);
  }
  send(data) { this.sent.push(data); }
  close() { this.readyState = MockWebSocket.CLOSED; this.onclose?.(); }
}
MockWebSocket.instances = [];

function framesOfType(ws, type) {
  return ws.sent
    .map((raw) => JSON.parse(raw))
    .filter((frame) => frame.type === type);
}

describe('BrowserNode mute', () => {
  beforeEach(() => {
    MockWebSocket.instances = [];
    vi.stubGlobal('WebSocket', MockWebSocket);
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({ ok: true, json: () => ({}) })));

    const storage = {};
    vi.stubGlobal('localStorage', {
      getItem: (k) => (k in storage ? storage[k] : null),
      setItem: (k, v) => { storage[k] = v; },
    });

    vi.stubGlobal('navigator', {
      userAgent: 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Safari/605',
      geolocation: { watchPosition: () => 1, clearWatch: () => {} },
      mediaDevices: {},
      vibrate: () => true,
    });
    if (typeof window !== 'undefined') {
      Object.defineProperty(window, 'location', {
        value: { origin: 'http://brain.local:9090' },
        writable: true,
        configurable: true,
      });
    }
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  async function connected() {
    const { BrowserNode } = await import('../node/BrowserNode.js');
    const node = new BrowserNode({ token: 'TOK' });
    await node.connect();
    const ws = MockWebSocket.instances[0];
    ws.sent.length = 0;
    node._voiceConfigSent = true;
    return { node, ws };
  }

  function fakeStream() {
    const track = { kind: 'audio', enabled: true, stop: vi.fn() };
    return {
      track,
      getTracks: () => [track],
      getAudioTracks: () => [track],
      getVideoTracks: () => [],
    };
  }

  it('drops captured audio while muted', async () => {
    const { node, ws } = await connected();
    node.setMicMuted(true);
    node._pushAudioChunk(new Float32Array(64));
    expect(framesOfType(ws, 'audio_chunk').filter((f) => !f.payload.is_final))
      .toHaveLength(0);
  });

  it('resumes sending captured audio after unmute', async () => {
    const { node, ws } = await connected();
    node.setMicMuted(true);
    node._pushAudioChunk(new Float32Array(64));
    node.setMicMuted(false);
    node._pushAudioChunk(new Float32Array(64));
    expect(framesOfType(ws, 'audio_chunk').filter((f) => !f.payload.is_final))
      .toHaveLength(1);
  });

  it('disables the microphone track so capture really stops', async () => {
    // Gating _pushAudioChunk alone still leaves a live mic: the OS
    // recording indicator stays on and any other consumer of the same
    // MediaStream keeps receiving samples.
    const { node } = await connected();
    const stream = fakeStream();
    node._mediaStream = stream;
    node.setMicMuted(true);
    expect(stream.track.enabled).toBe(false);
    node.setMicMuted(false);
    expect(stream.track.enabled).toBe(true);
  });

  it('does not stop the track outright, so unmute needs no new permission prompt', async () => {
    const { node } = await connected();
    const stream = fakeStream();
    node._mediaStream = stream;
    node.setMicMuted(true);
    expect(stream.track.stop).not.toHaveBeenCalled();
  });

  it('closes the current utterance when muting mid-speech', async () => {
    // Without an is_final frame the brain's STT buffer sits holding a
    // half-utterance until some later audio flushes it, and the words
    // spoken just before the mute land in the transcript afterwards.
    const { node, ws } = await connected();
    node.setMicMuted(true);
    const finals = framesOfType(ws, 'audio_chunk').filter((f) => f.payload.is_final);
    expect(finals).toHaveLength(1);
  });

  it('tells the brain about the mute change', async () => {
    const { node, ws } = await connected();
    node.setMicMuted(true);
    const frames = framesOfType(ws, 'voice_mute');
    expect(frames).toHaveLength(1);
    expect(frames[0].payload.muted).toBe(true);
  });

  it('also carries mute on voice_config, which the brain already handles', async () => {
    // api/server.py has no `voice_mute` branch yet, but it does route
    // `voice_config` into VoiceRouter.register_voice_config, and the
    // router's ingress gate reads `muted` from there. Sending both means
    // brain-side enforcement works today rather than after a server
    // change lands.
    const { node, ws } = await connected();
    node.setMicMuted(true);
    const cfg = framesOfType(ws, 'voice_config');
    expect(cfg).toHaveLength(1);
    expect(cfg[0].payload.muted).toBe(true);
  });

  it('exposes the mute state', async () => {
    const { node } = await connected();
    expect(node.isMicMuted()).toBe(false);
    node.setMicMuted(true);
    expect(node.isMicMuted()).toBe(true);
  });

  it('re-applies mute to a stream acquired after the toggle', async () => {
    // Reconnect ordering: the user mutes, the socket drops, startMic
    // runs again and hands back a fresh MediaStream. That stream must
    // arrive already muted rather than live for one gap.
    const { node } = await connected();
    node.setMicMuted(true);
    const stream = fakeStream();
    node._mediaStream = stream;
    node._applyMicMute();
    expect(stream.track.enabled).toBe(false);
  });
});
