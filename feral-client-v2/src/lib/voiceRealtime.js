/**
 * Realtime Voice Engine — AudioWorklet-based PCM16 capture & playback
 *
 * Uses AudioWorklet (not deprecated ScriptProcessor) for mic capture.
 * PCM16 at 24kHz, base64 chunks over WebSocket.
 * Includes energy-based VAD to skip sending silence.
 * Supports live transcription captions and tool-call status display.
 *
 * Features:
 *  - Automatic reconnection with exponential backoff
 *  - Push-to-talk mode (external toggle via muteMic / unmuteMic)
 *  - Visual state callback for "reconnecting" UI
 */

const TARGET_SAMPLE_RATE = 24000;
const WORKLET_NAME = 'pcm-capture-processor';
const VAD_ENERGY_THRESHOLD = 0.005;
const VAD_SILENCE_FRAMES = 15; // ~1.5s of silence before stopping send

// Whether the energy gate above is allowed to STOP sending audio.
//
// It never should when the brain endpoints for itself. The gate was
// the client half of a two-stage endpointer: it kept streaming for 15
// quiet frames (1.5s) after the speaker stopped, went silent, and only
// then could the server's own packet-absence timer start its 0.8s
// count. Two silence timers in series, about 2.3s of dead air before
// the transcription request was even issued. Measured against the
// chained pipeline: 2218ms from end of speech to `processing`.
//
// With server-side VAD (`feral-core/voice/vad.py`) the brain decides
// where the utterance ended by reading the audio, and it needs the
// audio to keep arriving to do that. Same measurement with the gate
// off and VAD on: 309ms.
//
// The energy reading is still computed and still drives `onVADChange`,
// because the orb animation wants to know when the user is talking.
// What changes is that it no longer censors the stream.
const CLIENT_GATE_ENDPOINTING = false;

const RECONNECT_BASE_MS = 1000;
const RECONNECT_MAX_MS = 16000;
const RECONNECT_MAX_ATTEMPTS = 8;

const WORKLET_CODE = `
class PCMCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._buffer = [];
    this._bufferSize = 2400; // 100ms at 24kHz
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || !input[0]) return true;
    const samples = input[0];

    for (let i = 0; i < samples.length; i++) {
      this._buffer.push(samples[i]);
    }

    while (this._buffer.length >= this._bufferSize) {
      const chunk = this._buffer.splice(0, this._bufferSize);
      const pcm16 = new Int16Array(chunk.length);
      let energy = 0;
      for (let i = 0; i < chunk.length; i++) {
        const s = Math.max(-1, Math.min(1, chunk[i]));
        pcm16[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
        energy += s * s;
      }
      energy = Math.sqrt(energy / chunk.length);
      this.port.postMessage(
        { type: 'audio', pcm16: pcm16.buffer, energy },
        [pcm16.buffer]
      );
    }
    return true;
  }
}
registerProcessor('${WORKLET_NAME}', PCMCaptureProcessor);
`;


export class RealtimeVoiceEngine {
  /**
   * @param wsOrFactory Prefer a RESOLVER, `() => socket.ws`. A raw
   *   WebSocket is frozen at construction: the shared FeralSocket
   *   replaces `socket.ws` whenever it rebinds to another chat thread
   *   (`FeralSocket.setSession`) or reconnects, and an engine holding
   *   the old object can never find its way back to the live one. It
   *   spends its whole reconnect budget re-examining a closed socket
   *   and then reports `degraded`.
   */
  constructor(wsOrFactory, callbacks = {}) {
    this._wsFactory = typeof wsOrFactory === 'function' ? wsOrFactory : null;
    this._rawWs = typeof wsOrFactory === 'function' ? null : wsOrFactory;
    this._ws = null;
    this._audioCtx = null;
    this._stream = null;
    this._workletNode = null;
    this._source = null;
    this._playbackCtx = null;
    // Buffer sources currently scheduled on the playback timeline, so a
    // barge-in can stop them without tearing down the AudioContext.
    this._activeSources = new Set();
    this._isPlaying = false;
    this._active = false;
    this._nextPlayTime = 0;
    this._silenceCount = 0;
    this._isSpeaking = false;
    this._chunkIndex = 0;
    this._provider = 'openai';
    this._micMuted = false;
    this._reconnectAttempts = 0;
    this._reconnectTimer = null;
    this._degraded = false;
    // True between the first assistant audio frame of a reply and its
    // `is_final`. The frames always carried it; nothing read it, so no
    // surface on the desktop knew when FERAL was talking.
    this._assistantSpeaking = false;
    // Last `chunk_index` seen on the fallback TTS stream, so an
    // out-of-order or duplicated chunk is reported rather than
    // scheduled as if it were the next one.
    this._lastTtsChunkIndex = -1;

    this.onTranscript = callbacks.onTranscript || null;
    this.onToolCall = callbacks.onToolCall || null;
    this.onSpeechStarted = callbacks.onSpeechStarted || null;
    this.onError = callbacks.onError || null;
    this.onVADChange = callbacks.onVADChange || null;
    this.onAssistantSpeaking = callbacks.onAssistantSpeaking || null;
    this.onStateChange = callbacks.onStateChange || null; // 'active' | 'reconnecting' | 'degraded' | 'off'

    // Bind a raw socket through the same path a resolved one takes, so
    // the close and error listeners exist for both forms. They used to
    // be registered a second time inside `start()` for this case only.
    if (this._rawWs) this._setWs(this._rawWs);
  }

  get active() { return this._active; }
  get speaking() { return this._isSpeaking; }
  get degraded() { return this._degraded; }
  get assistantSpeaking() { return this._assistantSpeaking; }

  /** Publish an assistant-audio transition once, on change only. */
  _setAssistantSpeaking(next) {
    if (this._assistantSpeaking === next) return;
    this._assistantSpeaking = next;
    if (this.onAssistantSpeaking) this.onAssistantSpeaking(next);
  }

  _setWs(ws) {
    // A resolver can legitimately answer "no socket right now" while
    // the shared FeralSocket is between connections. Binding null here
    // used to throw out of `start()` on the addEventListener below.
    if (!ws) return;
    this._ws = ws;
    this._reconnectAttempts = 0;
    if (this._degraded) {
      this._degraded = false;
      if (this.onStateChange) this.onStateChange('active');
    }

    // Every real WebSocket has this. A caller that passes a
    // send-only stub gets a working audio path and no reconnect
    // wiring, which is the honest outcome: there is no close event to
    // listen for.
    if (typeof ws.addEventListener !== 'function') return;
    ws.addEventListener('close', () => {
      if (this._active) this._attemptReconnect();
    });
    ws.addEventListener('error', () => {
      if (this._active) this._attemptReconnect();
    });
  }

  _attemptReconnect() {
    if (!this._active || this._reconnectTimer) return;
    if (this._reconnectAttempts >= RECONNECT_MAX_ATTEMPTS) {
      this._degraded = true;
      if (this.onStateChange) this.onStateChange('degraded');
      if (this.onError) this.onError('reconnect', 'Voice connection failed — falling back to text input');
      return;
    }

    if (this.onStateChange) this.onStateChange('reconnecting');

    const delay = Math.min(
      RECONNECT_BASE_MS * Math.pow(2, this._reconnectAttempts),
      RECONNECT_MAX_MS,
    );
    this._reconnectAttempts++;

    this._reconnectTimer = setTimeout(async () => {
      this._reconnectTimer = null;
      try {
        if (this._wsFactory) {
          const ws = await this._wsFactory();
          if (ws && ws.readyState === WebSocket.OPEN) {
            this._setWs(ws);
            this._sendVoiceConfig();
            if (this.onStateChange) this.onStateChange('active');
            return;
          }
        }
        // WS still connected? Just re-send config.
        if (this._ws && this._ws.readyState === WebSocket.OPEN) {
          this._sendVoiceConfig();
          this._reconnectAttempts = 0;
          if (this.onStateChange) this.onStateChange('active');
          return;
        }
      } catch { /* ignore */ }
      this._attemptReconnect();
    }, delay);
  }

  _sendVoiceConfig() {
    if (this._ws && this._ws.readyState === WebSocket.OPEN) {
      this._ws.send(JSON.stringify({
        hop: 'client',
        type: 'voice_config',
        payload: { mode: 'realtime', provider: this._provider, supports_realtime: true },
      }));
    }
  }

  async start(provider = 'openai') {
    this._active = true;
    this._provider = provider;
    this._chunkIndex = 0;
    this._silenceCount = 0;
    this._micMuted = false;
    this._degraded = false;
    this._reconnectAttempts = 0;
    this._assistantSpeaking = false;
    this._lastTtsChunkIndex = -1;

    if (!this._ws && this._wsFactory) {
      this._setWs(await this._wsFactory());
    }

    this._sendVoiceConfig();
    if (this.onStateChange) this.onStateChange('active');

    // `_setWs` already registered the close and error listeners. Adding
    // a second pair here meant every drop called `_attemptReconnect`
    // twice, and the second call was only ever absorbed by the
    // `_reconnectTimer` guard.

    this._stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        sampleRate: { ideal: TARGET_SAMPLE_RATE },
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });

    this._audioCtx = new AudioContext({ sampleRate: TARGET_SAMPLE_RATE });
    this._source = this._audioCtx.createMediaStreamSource(this._stream);

    const blob = new Blob([WORKLET_CODE], { type: 'application/javascript' });
    const url = URL.createObjectURL(blob);
    try {
      await this._audioCtx.audioWorklet.addModule(url);
    } finally {
      URL.revokeObjectURL(url);
    }

    this._workletNode = new AudioWorkletNode(this._audioCtx, WORKLET_NAME);
    this._workletNode.port.onmessage = (e) => {
      if (!this._active || this._micMuted || e.data.type !== 'audio') return;

      const energy = e.data.energy || 0;

      if (energy < VAD_ENERGY_THRESHOLD) {
        this._silenceCount++;
        if (this._isSpeaking && this._silenceCount > VAD_SILENCE_FRAMES) {
          this._isSpeaking = false;
          if (this.onVADChange) this.onVADChange(false);
        }
        // The `return` here is what used to stop the stream. Keeping
        // it would starve the server VAD of exactly the silence it
        // needs to hear in order to call the end of the utterance.
        if (!this._isSpeaking && CLIENT_GATE_ENDPOINTING) return;
      } else {
        if (!this._isSpeaking) {
          this._isSpeaking = true;
          if (this.onVADChange) this.onVADChange(true);
        }
        this._silenceCount = 0;
      }

      const b64 = this._arrayBufferToBase64(e.data.pcm16);
      if (this._ws && this._ws.readyState === WebSocket.OPEN) {
        this._ws.send(JSON.stringify({
          hop: 'client',
          type: 'audio_chunk',
          payload: {
            encoding: 'pcm16',
            sample_rate: TARGET_SAMPLE_RATE,
            channels: 1,
            chunk_index: this._chunkIndex++,
            is_final: false,
            data_b64: b64,
          },
        }));
      }
    };

    this._source.connect(this._workletNode);

    this._playbackCtx = new AudioContext({ sampleRate: TARGET_SAMPLE_RATE });
    // Audit-r11 — Bug 3 (silent voice on WebUI desktop). Safari +
    // Chrome both create new AudioContexts in the `suspended` state
    // until the page receives a user gesture. The voice button click
    // counts but the click handler is upstream of this constructor
    // (useVoiceMode.start) so the gesture chain can be lost on rapid
    // remount. Force a resume here so the very first `audio_response`
    // chunk fires through a `running` context and the user actually
    // hears the assistant.
    if (this._playbackCtx.state === 'suspended') {
      this._playbackCtx.resume().catch(() => {});
    }
    this._nextPlayTime = 0;
  }

  /** Mute the mic (push-to-talk release). */
  muteMic() {
    this._micMuted = true;
  }

  /** Unmute the mic (push-to-talk press). */
  unmuteMic() {
    this._micMuted = false;
    this._silenceCount = 0;
  }

  stop() {
    this._active = false;
    this._isSpeaking = false;
    this._setAssistantSpeaking(false);
    this._lastTtsChunkIndex = -1;

    if (this._reconnectTimer) {
      clearTimeout(this._reconnectTimer);
      this._reconnectTimer = null;
    }

    if (this._ws && this._ws.readyState === WebSocket.OPEN) {
      this._ws.send(JSON.stringify({
        hop: 'client',
        type: 'voice_config',
        payload: { mode: 'disabled' },
      }));
    }

    if (this._workletNode) {
      this._workletNode.port.close();
      this._workletNode.disconnect();
      this._workletNode = null;
    }
    if (this._source) {
      this._source.disconnect();
      this._source = null;
    }
    if (this._stream) {
      this._stream.getTracks().forEach((t) => t.stop());
      this._stream = null;
    }
    if (this._audioCtx) {
      this._audioCtx.close().catch(() => {});
      this._audioCtx = null;
    }
    if (this._playbackCtx) {
      this._playbackCtx.close().catch(() => {});
      this._playbackCtx = null;
    }
    this._activeSources.clear();
    this._isPlaying = false;
    this._nextPlayTime = 0;
    if (this.onStateChange) this.onStateChange('off');
  }

  handleAudioResponse(payload) {
    // `is_final` marks the end of the assistant's turn and carries no
    // audio (see `GeminiRealtimeSession`, which sends `("", True)`).
    // The early return was right about the audio and wrong to throw the
    // signal away with it: on the realtime path this frame is the only
    // notice the client gets that FERAL stopped talking, which is what
    // left the orb showing one mode for a whole session.
    if (payload.is_final) {
      this._setAssistantSpeaking(false);
      return;
    }
    if (!payload.data_b64) return;
    if (!this._playbackCtx) return;
    this._setAssistantSpeaking(true);

    try {
      const pcm16 = this._base64ToPCM16(payload.data_b64);
      const float32 = this._pcm16ToFloat32(pcm16);

      const buffer = this._playbackCtx.createBuffer(1, float32.length, TARGET_SAMPLE_RATE);
      buffer.getChannelData(0).set(float32);
      this._schedule(buffer);
    } catch (e) {
      if (this.onError) this.onError('playback', e.message);
    }
  }

  handleTranscript(payload) {
    if (this.onTranscript) {
      // Fourth argument is metadata the payload carries and every
      // caller used to drop: `confidence` is the STT provider's own
      // score (unnormalised across the 16 backends the brain routes,
      // so it is passed through and never rescaled), and the ordering
      // fields let a caller replace a partial in place instead of
      // appending a second bubble.
      this.onTranscript(
        payload.text,
        payload.is_partial,
        payload.role || 'assistant',
        {
          confidence: typeof payload.confidence === 'number'
            ? payload.confidence
            : null,
          itemId: payload.item_id || null,
          seq: typeof payload.seq === 'number' ? payload.seq : null,
        },
      );
    }
  }

  handleToolCallStatus(payload) {
    if (this.onToolCall) {
      this.onToolCall(payload.name, payload.status, payload.result);
    }
  }

  handleSpeechStarted() {
    // Barge-in: drop whatever the assistant still has queued.
    //
    // This used to close the playback AudioContext and construct a new
    // one on every barge-in. Stopping the scheduled source nodes has
    // the same audible effect without tearing down the output stream.
    // The teardown mattered because the browser's echo canceller keys
    // its reference signal off the output render stream and has to
    // re-converge its delay estimate whenever that stream is
    // re-acquired — i.e. the canceller was at its weakest immediately
    // after every barge-in, which is exactly when the assistant
    // resumes speaking into an open mic. Uncancelled speaker bleed
    // gets transcribed as user speech and renders as a right-aligned
    // bubble containing the assistant's own words.
    this._nextPlayTime = 0;
    for (const source of this._activeSources) {
      try { source.stop(); } catch { /* already ended */ }
    }
    this._activeSources.clear();
    // The reply that was playing has been cut. Anything tracking who
    // is talking has to hear about it here as well as on `is_final`,
    // because a barge-in means that `is_final` is never coming.
    this._setAssistantSpeaking(false);
    if (this._playbackCtx && this._playbackCtx.state === 'suspended') {
      // Same gesture-handoff issue described in `start()`.
      this._playbackCtx.resume().catch(() => {});
    }
    if (this.onSpeechStarted) {
      this.onSpeechStarted();
    }
  }

  /** Schedule a decoded buffer on the shared playback timeline. */
  _schedule(buffer) {
    const source = this._playbackCtx.createBufferSource();
    source.buffer = buffer;
    source.connect(this._playbackCtx.destination);
    // Track the node so a barge-in can stop it; drop it again once it
    // has played out so the set does not grow across a long session.
    this._activeSources.add(source);
    source.onended = () => this._activeSources.delete(source);

    const now = this._playbackCtx.currentTime;
    const startTime = Math.max(now, this._nextPlayTime);
    source.start(startTime);
    this._nextPlayTime = startTime + buffer.duration;
  }

  /**
   * Audit-r11 — Bug 3 (silent voice on whisper fallback). When the
   * brain falls back to OpenAI `/audio/speech` it emits `tts_chunk`
   * frames carrying base64-encoded mp3 (or wav for the local Piper
   * provider). The realtime PCM path above only handles `audio_response`;
   * without this method the WebUI dropped every fallback chunk and the
   * assistant went silent whenever Realtime was unavailable (operator
   * report 2026-05-18). Decodes the audio blob through
   * `AudioContext.decodeAudioData` and schedules it on the same
   * playback timeline `audio_response` uses, so a single utterance can
   * mix realtime + fallback frames cleanly.
   */
  async handleTtsChunk(payload) {
    if (!payload) return;
    // `chunk_index` and `is_final` shipped on every one of these frames
    // and neither was read. The index is the only way to notice that
    // the stream arrived out of order or repeated a chunk, which on
    // this path is audible (a syllable played twice, or the wrong way
    // round) with nothing in the UI saying why. The final flag is the
    // end of the assistant's turn.
    const index = typeof payload.chunk_index === 'number'
      ? payload.chunk_index
      : null;
    if (index !== null) {
      if (index <= this._lastTtsChunkIndex) {
        if (this.onError) {
          this.onError(
            'playback',
            `Out-of-order TTS chunk ${index} after ${this._lastTtsChunkIndex}`,
          );
        }
      }
      this._lastTtsChunkIndex = Math.max(this._lastTtsChunkIndex, index);
    }
    if (payload.is_final) {
      this._lastTtsChunkIndex = -1;
      if (!payload.data_b64) {
        this._setAssistantSpeaking(false);
        return;
      }
    }
    if (!payload.data_b64) return;
    this._setAssistantSpeaking(true);
    if (!this._playbackCtx) {
      this._playbackCtx = new AudioContext({ sampleRate: TARGET_SAMPLE_RATE });
      if (this._playbackCtx.state === 'suspended') {
        try { await this._playbackCtx.resume(); } catch { /* ignore */ }
      }
    }
    try {
      const binStr = atob(payload.data_b64);
      const bytes = new Uint8Array(binStr.length);
      for (let i = 0; i < binStr.length; i++) bytes[i] = binStr.charCodeAt(i);

      // `decodeAudioData` handles mp3 + wav natively across modern
      // browsers. Wrap in a Promise so the await actually waits.
      const audioBuffer = await new Promise((resolve, reject) => {
        try {
          this._playbackCtx.decodeAudioData(
            bytes.buffer.slice(0),
            (buf) => resolve(buf),
            (err) => reject(err || new Error('decodeAudioData failed')),
          );
        } catch (e) { reject(e); }
      });

      this._schedule(audioBuffer);
      if (payload.is_final) {
        // The last chunk is on the timeline. It has not finished
        // sounding yet, and nothing here can know when it will without
        // tracking playback, so this is reported as "the reply is
        // complete", not "the speaker is silent". Approximating the
        // latter with a timer would be a fabricated measurement.
        this._setAssistantSpeaking(false);
      }
    } catch (e) {
      if (this.onError) this.onError('playback', e?.message || String(e));
    }
  }

  _pcm16ToFloat32(pcm16) {
    const float32 = new Float32Array(pcm16.length);
    for (let i = 0; i < pcm16.length; i++) {
      float32[i] = pcm16[i] / (pcm16[i] < 0 ? 0x8000 : 0x7FFF);
    }
    return float32;
  }

  _arrayBufferToBase64(buffer) {
    const bytes = new Uint8Array(buffer);
    let binary = '';
    const chunkSize = 8192;
    for (let i = 0; i < bytes.length; i += chunkSize) {
      const slice = bytes.subarray(i, i + chunkSize);
      binary += String.fromCharCode.apply(null, slice);
    }
    return btoa(binary);
  }

  _base64ToPCM16(b64) {
    const binary = atob(b64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) {
      bytes[i] = binary.charCodeAt(i);
    }
    return new Int16Array(bytes.buffer);
  }
}
