/**
 * BrowserNode handshake tests — proves the HUP envelope leaving the
 * browser is exactly what the Brain's /v1/node WebSocket handler expects.
 *
 * Uses a mock WebSocket so the test can assert on every frame sent.
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
    // Open asynchronously so connect() awaits onopen.
    setTimeout(() => {
      this.readyState = MockWebSocket.OPEN;
      this.onopen?.();
    }, 0);
  }
  send(data) { this.sent.push(data); }
  close() { this.readyState = MockWebSocket.CLOSED; this.onclose?.(); }
}
MockWebSocket.instances = [];

describe('BrowserNode', () => {
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
    // Don't stub window — jsdom's window is read-only but already matches.
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

  it('builds ws url with token + platform + declares node_type browser_node', async () => {
    const { BrowserNode } = await import('../node/BrowserNode.js');
    const node = new BrowserNode({ token: 'abc123', name: 'Test Phone' });

    expect(node.wsUrl).toBe('ws://brain.local:9090/v1/node');
    expect(node.wsProtocol).toBe('feral-token-abc123');
    expect(node.platform).toBe('ios-browser');
    expect(node.capabilities).toContain('location');
    expect(node.capabilities).toContain('display');
  });

  it('sends node_register on connect using token subprotocol auth', async () => {
    const { BrowserNode } = await import('../node/BrowserNode.js');
    const phases = [];
    const node = new BrowserNode({
      token: 'TOKEN1',
      name: 'My Phone',
      onPhase: (p) => phases.push(p),
    });

    await node.connect();

    expect(MockWebSocket.instances).toHaveLength(1);
    const ws = MockWebSocket.instances[0];
    expect(ws.protocols).toContain('feral-token-TOKEN1');
    expect(ws.sent).toHaveLength(1);
    const frame = JSON.parse(ws.sent[0]);
    expect(frame.type).toBe('node_register');
    expect(frame.hup_version).toBe('1.3.0');
    expect(frame.payload.node_type).toBe('browser_node');
    expect(frame.payload.name).toBe('My Phone');
    expect(frame.payload.capabilities).toContain('location');

    expect(phases).toContain('connected');
    expect(phases).toContain('registered');
  });

  it('rejects construction with no token', async () => {
    const { BrowserNode } = await import('../node/BrowserNode.js');
    expect(() => new BrowserNode({})).toThrow(/token/i);
  });

  it('sendVoiceConfig emits voice_config frame before any audio_chunk', async () => {
    const { BrowserNode } = await import('../node/BrowserNode.js');
    const node = new BrowserNode({ token: 'vc-1', voiceProvider: 'gemini' });
    await node.connect();
    const ws = MockWebSocket.instances[0];
    // one node_register already queued
    ws.sent = [];

    await node.sendVoiceConfig();
    expect(ws.sent).toHaveLength(1);
    const frame = JSON.parse(ws.sent[0]);
    expect(frame.type).toBe('voice_config');
    expect(frame.payload.provider).toBe('gemini');
    expect(frame.payload.supports_realtime).toBe(true);
    expect(frame.payload.sample_rate).toBe(24000);
    expect(frame.payload.encoding).toBe('pcm16');
  });

  it('_pushAudioChunk sends an audio_chunk with PCM16 base64 + chunk_index', async () => {
    const { BrowserNode } = await import('../node/BrowserNode.js');
    const node = new BrowserNode({ token: 'a-1' });
    await node.connect();
    const ws = MockWebSocket.instances[0];
    ws.sent = [];

    // Fake Float32 samples
    const samples = new Float32Array(320);
    for (let i = 0; i < samples.length; i++) samples[i] = Math.sin(i * 0.1);
    node._pushAudioChunk(samples);

    expect(ws.sent).toHaveLength(1);
    const frame = JSON.parse(ws.sent[0]);
    expect(frame.type).toBe('audio_chunk');
    expect(frame.payload.encoding).toBe('pcm16');
    expect(frame.payload.sample_rate).toBe(24000);
    expect(typeof frame.payload.data_b64).toBe('string');
    expect(frame.payload.data_b64.length).toBeGreaterThan(0);
    expect(frame.payload.chunk_index).toBe(0);

    node._pushAudioChunk(samples);
    const second = JSON.parse(ws.sent[1]);
    expect(second.payload.chunk_index).toBe(1);
  });

  it('floatToPCM16 produces a 2x-byte payload per sample', async () => {
    // Indirectly verified by _pushAudioChunk above, but re-assert shape.
    const { BrowserNode } = await import('../node/BrowserNode.js');
    const node = new BrowserNode({ token: 'pcm-1' });
    await node.connect();
    const ws = MockWebSocket.instances[0];
    ws.sent = [];

    const samples = new Float32Array(160);
    node._pushAudioChunk(samples);
    const frame = JSON.parse(ws.sent[0]);
    // 160 samples * 2 bytes = 320 raw bytes; base64 → ceil(320/3)*4 = 428 chars
    expect(frame.payload.data_b64.length).toBe(Math.ceil(320 / 3) * 4);
  });
});
