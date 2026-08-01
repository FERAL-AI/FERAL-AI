/**
 * BrowserNode — a real HUP node that runs inside a phone / laptop browser.
 *
 * The phone scans the "Web phone" QR, lands on /pair?t=<TOKEN>, taps
 * "Pair this phone". Pair.jsx then instantiates this class, which opens
 * a WebSocket to /v1/node authenticated via
 * Sec-WebSocket-Protocol: feral-token-<TOKEN>, sends a NodeRegisterPayload
 * with node_type="browser_node", and streams live sensor data back to the
 * Brain. No app install needed.
 *
 * Frames the Brain understands:
 *   • location — navigator.geolocation.watchPosition → WS `location_update`
 *   • mic      — AudioWorklet PCM16 @ 24kHz → WS `audio_chunk`
 *                 {data_b64, chunk_index, is_final, encoding, sample_rate}
 *   • camera   — canvas.toBlob('image/jpeg') → WS `frame`
 *                 {data_b64, width, height, mime}
 *   • display  — server-pushed `action` frames (notify / vibrate)
 *
 * Privacy rules (non-negotiable):
 *   • Sensor streams only start when the user taps "Allow".
 *   • Tab-hidden for >60 s ⇒ streams auto-pause.
 *   • stop() revokes getUserMedia tracks + closes the WS.
 */

const HUP_VERSION = "1.3.0";
const AUDIO_TARGET_SAMPLE_RATE = 24000;
const AUDIO_CHUNK_MS = 250; // 200-400ms per chunk — matches the VoiceRouter contract
const VIDEO_INTERVAL_MS = 750;
const VIDEO_MAX_WIDTH = 640;
const VIDEO_JPEG_QUALITY = 0.7;

function nowTs() {
  return Date.now() / 1000;
}

function makeNodeId() {
  try {
    const existing = localStorage.getItem("feral.browser_node_id");
    if (existing) return existing;
    const fresh = `browser-node-${Math.random().toString(36).slice(2, 10)}`;
    localStorage.setItem("feral.browser_node_id", fresh);
    return fresh;
  } catch {
    return `browser-node-${Math.random().toString(36).slice(2, 10)}`;
  }
}

function detectPlatform() {
  const ua = typeof navigator !== "undefined" ? navigator.userAgent || "" : "";
  if (/iPhone|iPad|iPod/.test(ua)) return "ios-browser";
  if (/Android/.test(ua)) return "android-browser";
  if (/Macintosh/.test(ua)) return "macos-browser";
  if (/Windows/.test(ua)) return "windows-browser";
  if (/Linux/.test(ua)) return "linux-browser";
  return "browser";
}

function defaultCapabilities() {
  const caps = [];
  if (typeof navigator === "undefined") return caps;
  if (navigator.geolocation) caps.push("location");
  if (navigator.mediaDevices?.getUserMedia) {
    caps.push("camera");
    caps.push("mic");
  }
  if (typeof navigator.vibrate === "function") caps.push("haptic");
  caps.push("display");
  return caps;
}

function floatToPCM16Base64(float32) {
  // Convert a Float32Array of [-1..1] samples to base64-encoded PCM16LE.
  const len = float32.length;
  const buf = new ArrayBuffer(len * 2);
  const view = new DataView(buf);
  for (let i = 0; i < len; i++) {
    let s = Math.max(-1, Math.min(1, float32[i]));
    s = s < 0 ? s * 0x8000 : s * 0x7fff;
    view.setInt16(i * 2, s, true);
  }
  const bytes = new Uint8Array(buf);
  let binary = "";
  for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
  return typeof btoa !== "undefined" ? btoa(binary) : Buffer.from(binary, "binary").toString("base64");
}

// AudioWorklet source for downsampling + buffering. Built as a Blob URL
// so callers don't need to ship a separate .js file. Runs on the audio
// thread; posts Float32 batches back to the main thread every chunk.
const WORKLET_SOURCE = `
class FeralCaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.targetRate = options.processorOptions?.targetRate || 24000;
    this.inputRate = sampleRate;
    this.ratio = this.inputRate / this.targetRate;
    this.chunkSamples = Math.round(this.targetRate * (options.processorOptions?.chunkMs || 250) / 1000);
    this.buffer = [];
    this.acc = 0;
  }
  process(inputs) {
    const input = inputs[0] && inputs[0][0];
    if (!input) return true;
    // Linear-resample to targetRate.
    for (let i = 0; i < input.length; i++) {
      this.acc += 1;
      if (this.acc >= this.ratio) {
        this.acc -= this.ratio;
        this.buffer.push(input[i]);
        if (this.buffer.length >= this.chunkSamples) {
          const out = new Float32Array(this.buffer);
          this.port.postMessage(out, [out.buffer]);
          this.buffer = [];
        }
      }
    }
    return true;
  }
}
registerProcessor("feral-capture", FeralCaptureProcessor);
`;

export class BrowserNode {
  /**
   * @param {object} opts
   * @param {string} opts.token        — runtime bearer token for /v1/node
   * @param {string} [opts.brainUrl]   — ws://… prefix for /v1/node. Inferred
   *                                      from window.location when absent.
   * @param {string} [opts.nodeId]     — stable id (persisted in localStorage).
   * @param {string} [opts.name]       — display name.
   * @param {string[]} [opts.capabilities]
   * @param {string} [opts.voiceProvider]  — "openai" | "gemini" | "whisper"
   * @param {(phase: string, detail?: any) => void} [opts.onPhase]
   * @param {(err: Error) => void} [opts.onError]
   * @param {(err: {code: string, message: string, recoverable: boolean}) => void} [opts.onMicError]
   * @param {(err: {code: string, message: string, recoverable: boolean}) => void} [opts.onVoiceError]
   */
  constructor(opts) {
    if (!opts?.token) throw new Error("BrowserNode: token is required");

    this.token = opts.token;
    this.nodeId = opts.nodeId || makeNodeId();
    this.name = opts.name || "Browser Node";
    this.platform = detectPlatform();
    this.capabilities = opts.capabilities || defaultCapabilities();
    this.voiceProvider = opts.voiceProvider || "openai";
    this.onPhase = opts.onPhase || (() => {});
    this.onError = opts.onError || ((e) => console.warn("[BrowserNode]", e));
    this.onMicError = opts.onMicError || null;
    this.onVoiceError = opts.onVoiceError || null;

    const originHttp = opts.brainUrl || (
      typeof window !== "undefined" ? window.location.origin : ""
    );
    this.wsUrl = originHttp
      .replace(/^http/, "ws")
      .replace(/\/$/, "") + "/v1/node";
    this.wsProtocol = `feral-token-${this.token}`;

    this._ws = null;
    this._stopped = false;
    this._locationWatchId = null;
    this._mediaStream = null;
    this._audioContext = null;
    this._workletNode = null;
    this._audioChunkIndex = 0;
    this._videoTimer = null;
    this._videoElement = null;
    this._pausedAt = 0;
    this._visibilityHandler = null;
    this._voiceConfigSent = false;
    // Microphone mute. Enforced at capture, not just in the UI: the
    // promise a mute button makes is that the audio does not leave the
    // device, and gating the send alone leaves the OS recording
    // indicator on and any other consumer of this MediaStream still
    // receiving samples.
    this._micMuted = false;
  }

  /** True when the microphone is muted at capture. */
  isMicMuted() {
    return this._micMuted === true;
  }

  /**
   * Mute / unmute the microphone.
   *
   * Three things happen, and all three are needed:
   *   1. The MediaStream's audio tracks are DISABLED (not stopped, so
   *      unmuting needs no fresh permission prompt and no getUserMedia
   *      round-trip). A disabled track emits silence at the source.
   *   2. `_pushAudioChunk` starts dropping, so a worklet that is still
   *      draining cannot leak the last buffers past the toggle.
   *   3. The brain is told, twice on purpose: a `voice_mute` frame (the
   *      dedicated envelope) and `muted` on `voice_config` (a wire path
   *      the brain's VoiceRouter already reads, so brain-side ingress
   *      is gated even where no `voice_mute` handler exists yet).
   *
   * Muting also flushes an `is_final` audio chunk so the brain closes
   * the utterance in flight instead of holding half a sentence until
   * some later audio flushes it.
   */
  setMicMuted(muted) {
    const next = muted === true;
    if (this._micMuted === next) return;
    this._micMuted = next;
    this._applyMicMute();
    if (next) this._flushUtterance();
    this._send("voice_mute", { node_id: this.nodeId, muted: next });
    // Merged into the node's voice config rather than replacing it:
    // `sendVoiceConfig` fills in mode/provider/sample_rate and the brain
    // merges what arrives onto what it already had.
    this.sendVoiceConfig({ muted: next }).catch(() => { /* best effort */ });
  }

  /**
   * Push the current mute state onto whatever MediaStream exists now.
   *
   * Called from `setMicMuted` and from `startMic`, because a reconnect
   * acquires a NEW stream after the toggle: without re-applying, a
   * muted session would come back with a live microphone for the gap
   * between `getUserMedia` resolving and the next user action.
   */
  _applyMicMute() {
    if (!this._mediaStream) return;
    const tracks = this._mediaStream.getAudioTracks?.() || [];
    for (const track of tracks) track.enabled = !this._micMuted;
  }

  /** Send a zero-length `is_final` chunk to close the current utterance. */
  _flushUtterance() {
    if (!this._voiceConfigSent) return;
    this._send("audio_chunk", {
      node_id: this.nodeId,
      data_b64: "",
      chunk_index: this._audioChunkIndex++,
      is_final: true,
      encoding: "pcm16",
      sample_rate: AUDIO_TARGET_SAMPLE_RATE,
    });
  }

  async connect() {
    if (typeof WebSocket === "undefined") {
      throw new Error("WebSocket not available in this runtime");
    }
    this._ws = new WebSocket(this.wsUrl, [this.wsProtocol]);

    await new Promise((resolve, reject) => {
      this._ws.onopen = () => resolve();
      this._ws.onerror = () => reject(new Error("WebSocket error"));
    });
    this.onPhase("connected");

    await this._send("node_register", {
      node_id: this.nodeId,
      node_type: "browser_node",
      name: this.name,
      platform: this.platform,
      capabilities: this.capabilities,
      sensors: this.capabilities.filter((c) =>
        ["location", "camera", "mic"].includes(c),
      ),
      actuators: this.capabilities.filter((c) =>
        ["display", "haptic"].includes(c),
      ),
    });

    this._ws.onmessage = (e) => this._onFrame(e);
    this._ws.onclose = (event) => {
      this.onPhase("closed");
      if (event.code !== 1000 && event.code !== 1005 && this.onVoiceError) {
        this.onVoiceError({
          code: 'WS_CLOSED',
          message: event.reason || `WebSocket closed unexpectedly (code ${event.code})`,
          recoverable: true,
        });
      }
    };

    this._visibilityHandler = () => {
      if (document.hidden) {
        this._pausedAt = Date.now();
      } else if (this._pausedAt && Date.now() - this._pausedAt > 60_000) {
        this.pauseStreams();
      }
    };
    if (typeof document !== "undefined") {
      document.addEventListener("visibilitychange", this._visibilityHandler);
    }

    this.onPhase("registered");
  }

  /**
   * Start sensor streams. Safe to call repeatedly — each flag is
   * idempotent. Pass `{location: false}` to not start location.
   */
  async startSensors({ location = true, camera = false, mic = false } = {}) {
    if (location && navigator.geolocation && this._locationWatchId == null) {
      this._locationWatchId = navigator.geolocation.watchPosition(
        (pos) => this._pushLocation(pos),
        (err) => this.onError(err),
        { enableHighAccuracy: true, maximumAge: 10_000, timeout: 15_000 },
      );
      this.onPhase("location_streaming");
    }

    if (mic) await this.startMic();
    if (camera) await this.startCamera();
  }

  async sendVoiceConfig(overrides = {}) {
    const payload = {
      mode: "realtime",
      provider: this.voiceProvider,
      supports_realtime: true,
      sample_rate: AUDIO_TARGET_SAMPLE_RATE,
      encoding: "pcm16",
      ...overrides,
    };
    await this._send("voice_config", payload);
    this._voiceConfigSent = true;
    this.onPhase("voice_config", payload);
  }

  async startMic() {
    if (this._workletNode) return;
    if (!navigator.mediaDevices?.getUserMedia) {
      const micErr = {
        code: 'MIC_UNAVAILABLE',
        message: 'getUserMedia not available on this device',
        recoverable: false,
      };
      if (this.onMicError) this.onMicError(micErr);
      this.onError(new Error(micErr.message));
      return;
    }
    try {
      // CRITICAL iOS Safari ordering: call getUserMedia FIRST within
      // the user-gesture context. If we await anything else before
      // getUserMedia (like sendVoiceConfig) the gesture may be lost
      // and Safari silently refuses mic access, leaving the rest of
      // the pipeline set up but no audio flowing. Send the voice
      // config AFTER the mic is acquired.
      this._mediaStream = this._mediaStream || await navigator.mediaDevices.getUserMedia({
        audio: true,
      });
      // A reconnect into a still-muted session acquires a fresh stream
      // here; it must arrive already muted rather than live for the gap
      // until the next user action.
      this._applyMicMute();
      if (!this._voiceConfigSent) {
        // Intentionally not awaited — we just want the frame on the
        // wire; sendVoiceConfig's internal WebSocket send is synchronous.
        this.sendVoiceConfig().catch((err) => {
          console.warn('[BrowserNode] sendVoiceConfig failed', err);
        });
      }
      const AudioCtx = window.AudioContext || window.webkitAudioContext;
      this._audioContext = new AudioCtx();
      // iOS Safari starts AudioContext in 'suspended' state. Without
      // an explicit resume() no audio data reaches the worklet, so
      // the brain waits forever for audio_chunk frames that never
      // arrive. resume() has to be called synchronously inside the
      // user-gesture stack that led here (the Start voice tap).
      if (this._audioContext.state === "suspended") {
        try {
          await this._audioContext.resume();
        } catch (resumeErr) {
          console.warn("[BrowserNode] AudioContext.resume failed", resumeErr);
        }
      }
      const blob = new Blob([WORKLET_SOURCE], { type: "application/javascript" });
      const workletUrl = URL.createObjectURL(blob);
      await this._audioContext.audioWorklet.addModule(workletUrl);
      URL.revokeObjectURL(workletUrl);

      const source = this._audioContext.createMediaStreamSource(this._mediaStream);
      this._workletNode = new AudioWorkletNode(this._audioContext, "feral-capture", {
        processorOptions: {
          targetRate: AUDIO_TARGET_SAMPLE_RATE,
          chunkMs: AUDIO_CHUNK_MS,
        },
      });
      this._workletNode.port.onmessage = (event) => this._pushAudioChunk(event.data);
      source.connect(this._workletNode);
      const silent = this._audioContext.createGain();
      silent.gain.value = 0;
      this._workletNode.connect(silent).connect(this._audioContext.destination);
      this.onPhase("mic_streaming");
    } catch (err) {
      const micErr = {
        code: err.name === 'NotAllowedError' ? 'MIC_PERMISSION_DENIED' : 'MIC_START_FAILED',
        message: err.message || 'Microphone failed to start',
        recoverable: err.name !== 'NotAllowedError',
      };
      if (this.onMicError) this.onMicError(micErr);
      this.onError(err);
    }
  }

  async startCamera() {
    if (this._videoTimer) return;
    if (!navigator.mediaDevices?.getUserMedia) {
      this.onError(new Error("getUserMedia not available"));
      return;
    }
    try {
      this._mediaStream = this._mediaStream || await navigator.mediaDevices.getUserMedia({
        video: { facingMode: "environment" },
      });
      // Attach to a hidden <video> element so canvas.drawImage works.
      if (typeof document !== "undefined") {
        const video = document.createElement("video");
        video.playsInline = true;
        video.muted = true;
        video.srcObject = this._mediaStream;
        await video.play().catch(() => {});
        this._videoElement = video;
      }
      this._videoTimer = setInterval(() => this._pushCameraFrame(), VIDEO_INTERVAL_MS);
      this.onPhase("camera_streaming");
    } catch (err) {
      this.onError(err);
    }
  }

  pauseStreams() {
    if (this._locationWatchId != null && navigator.geolocation) {
      navigator.geolocation.clearWatch(this._locationWatchId);
      this._locationWatchId = null;
    }
    if (this._videoTimer) {
      clearInterval(this._videoTimer);
      this._videoTimer = null;
    }
    if (this._workletNode) {
      try { this._workletNode.disconnect(); } catch { /* ignore */ }
      this._workletNode = null;
    }
    if (this._audioContext) {
      this._audioContext.close().catch(() => {});
      this._audioContext = null;
    }
    if (this._videoElement) {
      this._videoElement.pause?.();
      this._videoElement.srcObject = null;
      this._videoElement = null;
    }
    if (this._mediaStream) {
      for (const track of this._mediaStream.getTracks()) track.stop();
      this._mediaStream = null;
    }
    this._voiceConfigSent = false;
    this.onPhase("paused");
  }

  async stopMic() {
    if (this._workletNode) {
      try { this._workletNode.disconnect(); } catch { /* ignore */ }
      this._workletNode = null;
    }
    if (this._audioContext) {
      await this._audioContext.close().catch(() => {});
      this._audioContext = null;
    }
    if (this._mediaStream) {
      for (const track of this._mediaStream.getAudioTracks()) track.stop();
    }
    // Send one final is_final=true frame so the voice router can close
    // its current utterance cleanly.
    if (this._voiceConfigSent) {
      await this._send("audio_chunk", {
        node_id: this.nodeId,
        data_b64: "",
        chunk_index: this._audioChunkIndex++,
        is_final: true,
        encoding: "pcm16",
        sample_rate: AUDIO_TARGET_SAMPLE_RATE,
      });
    }
    this.onPhase("mic_stopped");
  }

  async stopCamera() {
    if (this._videoTimer) {
      clearInterval(this._videoTimer);
      this._videoTimer = null;
    }
    if (this._videoElement) {
      this._videoElement.pause?.();
      this._videoElement.srcObject = null;
      this._videoElement = null;
    }
    if (this._mediaStream) {
      for (const track of this._mediaStream.getVideoTracks()) track.stop();
    }
    this.onPhase("camera_stopped");
  }

  async stop() {
    if (this._stopped) return;
    this._stopped = true;
    this.pauseStreams();
    if (this._visibilityHandler && typeof document !== "undefined") {
      document.removeEventListener("visibilitychange", this._visibilityHandler);
    }
    try {
      this._ws?.close();
    } catch (err) {
      this.onError(err);
    }
    this.onPhase("stopped");
  }

  _pushLocation(pos) {
    if (!pos?.coords) return;
    if (!this._ws || this._ws.readyState !== WebSocket.OPEN) return;
    // HUP v1.3.1: stream location over the existing authenticated
    // WebSocket instead of HTTP POSTing to /api/location/update.
    // The HTTP path required a dashboard API key, which the phone
    // never has — it authenticates with phone_bearer over the WS
    // subprotocol. Sending over WS reuses the same auth + lifecycle.
    const c = pos.coords;
    this._send("location_update", {
      node_id: this.nodeId,
      lat: c.latitude,
      lon: c.longitude,
      accuracy_m: typeof c.accuracy === "number" ? c.accuracy : null,
      altitude_m: typeof c.altitude === "number" ? c.altitude : null,
      heading_deg: typeof c.heading === "number" ? c.heading : null,
      speed_mps: typeof c.speed === "number" ? c.speed : null,
      source: "browser_node",
      ts: typeof pos.timestamp === "number"
        ? pos.timestamp / 1000
        : Date.now() / 1000,
    });
  }

  _pushAudioChunk(float32) {
    // Second line of defence behind the disabled track: a worklet that
    // is still draining its buffer must not leak the last frames past
    // the mute toggle.
    if (this._micMuted) return;
    if (!this._ws || this._ws.readyState !== WebSocket.OPEN) return;
    const b64 = floatToPCM16Base64(float32);
    this._send("audio_chunk", {
      node_id: this.nodeId,
      data_b64: b64,
      chunk_index: this._audioChunkIndex++,
      is_final: false,
      encoding: "pcm16",
      sample_rate: AUDIO_TARGET_SAMPLE_RATE,
    });
  }

  _pushCameraFrame() {
    if (!this._videoElement || typeof document === "undefined") return;
    if (!this._ws || this._ws.readyState !== WebSocket.OPEN) return;
    const video = this._videoElement;
    if (!video.videoWidth || !video.videoHeight) return;
    const maxW = VIDEO_MAX_WIDTH;
    const scale = Math.min(1, maxW / video.videoWidth);
    const w = Math.round(video.videoWidth * scale);
    const h = Math.round(video.videoHeight * scale);
    const canvas = document.createElement("canvas");
    canvas.width = w;
    canvas.height = h;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.drawImage(video, 0, 0, w, h);
    canvas.toBlob((blob) => {
      if (!blob) return;
      const reader = new FileReader();
      reader.onloadend = () => {
        // strip the `data:image/jpeg;base64,` prefix
        const dataUrl = reader.result || "";
        const b64 = typeof dataUrl === "string" ? dataUrl.split(",")[1] || "" : "";
        if (!b64) return;
        this._send("frame", {
          node_id: this.nodeId,
          data_b64: b64,
          width: w,
          height: h,
          mime: "image/jpeg",
        });
      };
      reader.readAsDataURL(blob);
    }, "image/jpeg", VIDEO_JPEG_QUALITY);
  }

  _onFrame(event) {
    let frame;
    try {
      frame = JSON.parse(event.data);
    } catch {
      return;
    }
    const type = frame?.type || "";
    if (type === "node_ack") {
      this.onPhase("acknowledged", frame.payload);
      return;
    }
    if (type === "action") {
      this._handleAction(frame.payload || {});
      return;
    }
    if (type === "voice_error" || type === "error") {
      const p = frame.payload || {};
      if (this.onVoiceError && (type === "voice_error" || p.scope === "voice")) {
        this.onVoiceError({
          code: p.code || 'PROVIDER_ERROR',
          message: p.message || 'Voice session error',
          recoverable: p.recoverable !== false,
        });
      }
    }
    this.onPhase("frame", frame);
  }

  _handleAction(payload) {
    const kind = payload?.action_type || payload?.kind || "";
    if (kind === "vibrate" && typeof navigator.vibrate === "function") {
      navigator.vibrate(Number(payload.duration_ms || 120));
    } else if (kind === "notify" && "Notification" in window) {
      try {
        if (Notification.permission === "granted") {
          new Notification("FERAL", { body: String(payload.text || "") });
        } else if (Notification.permission !== "denied") {
          Notification.requestPermission();
        }
      } catch (err) {
        this.onError(err);
      }
    }
  }

  async _send(type_, payload) {
    if (!this._ws || this._ws.readyState !== WebSocket.OPEN) return;
    const frame = {
      hup_version: HUP_VERSION,
      type: type_,
      ts: nowTs(),
      payload,
    };
    this._ws.send(JSON.stringify(frame));
  }
}

export default BrowserNode;
