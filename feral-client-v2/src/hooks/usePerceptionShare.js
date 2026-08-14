/**
 * usePerceptionShare — browser-based perception share.
 *
 * The user grants camera + mic permission from any browser (iPhone Safari,
 * Android Chrome, desktop) and FERAL treats them as a HUP v1.1 daemon.
 *
 * Architecture
 * ------------
 *   getUserMedia()
 *     → <video> offscreen element (bound to the MediaStream)
 *     → OffscreenCanvas for JPEG capture at fps (default 2)
 *     → AudioContext + AudioWorkletNode for 16kHz PCM16 downsample chunking
 *     → secondary WebSocket to /v1/node (brain's HUP endpoint)
 *         • node_register ({node_type: "browser_camera", capabilities: ["camera", "microphone", "browser_share"]})
 *         • video_frame {data_b64, encoding: "jpeg", ...}
 *         • audio_frame {data_b64, encoding: "pcm16", ...}
 *
 * Kept deliberately independent from the shared FeralSocket so:
 *   1. The chat connection stays clean (no accidental media pings).
 *   2. /api/devices/connected picks up the real daemon without new routes.
 *   3. Revocation just closes the second socket — no teardown race with chat.
 *
 * One share, one store
 * --------------------
 * The share state lives in a MODULE-LEVEL store, not in per-component
 * useState, and every mount reads it through useSyncExternalStore. This
 * is the same pattern `useSystemHealth.js` uses for the dashboard poll.
 *
 * It is load-bearing for the privacy guarantee below, not a tidiness
 * choice. `<PerceptionShare />` (Devices pane) and
 * `<PerceptionShare.FloatingChip />` (Shell dock) are two separate
 * mounts. While the state was per-component they each held their own
 * `status`, so starting the share from the pane left the chip's copy on
 * `idle` and the "you are being recorded" indicator never rendered while
 * the camera was live. There is exactly one MediaStream, one socket and
 * one status, so there is exactly one store.
 *
 * Privacy rules (hard-coded, not a setting)
 * -----------------------------------------
 *   • Streaming only starts when start() is explicitly called.
 *   • Nothing is emitted unless status === 'running'. Pausing, muting
 *     and stopping all stop bytes leaving the browser, and the emit
 *     paths re-check the live flags on every frame/chunk rather than
 *     trusting a value captured when the share started.
 *   • Muting the mic also disables the audio tracks, so capture stops at
 *     the source and not just at the send call.
 *   • Visibility change (tab backgrounded > 60s) auto-pauses.
 *   • stop() tears the MediaStream down AND revokes the daemon.
 *   • The dock indicator is visible the whole time the camera is held
 *     (running AND paused), because the store is shared by every mount.
 *     There is no hidden-share mode.
 *   • When the last consumer unmounts the share is torn down. No UI
 *     mounted means no indicator, and no indicator means no streaming.
 */

import { useEffect, useMemo, useSyncExternalStore } from 'react';
import { API_BASE, WS_BASE } from '../lib/config';

const DEFAULT_FPS = 2;
const DEFAULT_JPEG_QUALITY = 0.6;
const PCM_SAMPLE_RATE = 16000;
const MAX_JPEG_BYTES = 512 * 1024;
const HIDDEN_PAUSE_MS = 60 * 1000;

function bytesFromBase64(b64) {
  try {
    // Strip the base64 padding characters; the brain also enforces a
    // decoded-size cap so we match the same arithmetic.
    return Math.floor((b64.length * 3) / 4);
  } catch {
    return 0;
  }
}

function blobToBase64(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error || new Error('blob read failed'));
    reader.onload = () => {
      const result = String(reader.result || '');
      const idx = result.indexOf('base64,');
      resolve(idx >= 0 ? result.slice(idx + 7) : result);
    };
    reader.readAsDataURL(blob);
  });
}

function float32ToPcm16Base64(float32) {
  const pcm16 = new Int16Array(float32.length);
  for (let i = 0; i < float32.length; i += 1) {
    const clamped = Math.max(-1, Math.min(1, float32[i]));
    pcm16[i] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7FFF;
  }
  const bytes = new Uint8Array(pcm16.buffer);
  let binary = '';
  for (let i = 0; i < bytes.length; i += 1) binary += String.fromCharCode(bytes[i]);
  return btoa(binary);
}

function pickNodeId() {
  // Stable-within-session id so the brain's device list doesn't flicker
  // as the user pauses/resumes sharing.
  try {
    const existing = window.sessionStorage.getItem('feral_browser_camera_node_id');
    if (existing) return existing;
  } catch { /* sessionStorage may be disabled */ }
  const suffix = Math.random().toString(36).slice(2, 8);
  const nodeId = `browser-camera-${suffix}`;
  try { window.sessionStorage.setItem('feral_browser_camera_node_id', nodeId); } catch { /* ignore */ }
  return nodeId;
}

/**
 * Resolve a pair token for the perception-share WebSocket.
 *
 * The brain's ``/v1/node`` endpoint requires authentication
 * (``feral-core/api/server.py`` ``daemon_session``). Browsers can't
 * set ``Authorization`` headers on WebSockets, so we follow the
 * ``BrowserNode`` convention and pass the token via the
 * ``Sec-WebSocket-Protocol`` subprotocol as ``feral-token-<TOKEN>``.
 *
 * Token source priority (Lane 11 R2 fix — was previously a zero-auth
 * open socket):
 *   1. explicit ``token`` arg to ``usePerceptionShare`` — useful when
 *      the parent already pair-minted a token via Devices → Pair.
 *   2. ``window.sessionStorage`` (``feral.session.pair_token``) so a
 *      previously-paired browser session reuses its token.
 *   3. ``POST /api/devices/pair`` (kind=browser_camera) — same path
 *      ``BrainPairFlow.swift`` and ``Pair.jsx`` use. Requires the user
 *      to be authenticated in the web UI (cookie session) — anonymous
 *      requests fail-closed and the hook surfaces ``status="error"``
 *      with a clear ``error`` message rather than opening the WS.
 */
async function resolvePairToken({ apiBase, explicit }) {
  if (explicit && typeof explicit === 'string') return explicit;
  try {
    const cached = window.sessionStorage.getItem('feral.session.pair_token');
    if (cached) return cached;
  } catch { /* sessionStorage may be disabled */ }
  try {
    const resp = await fetch(`${apiBase}/api/devices/pair`, {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ kind: 'browser_camera', name: 'Web Perception Share' }),
    });
    if (!resp.ok) {
      throw new Error(`pair endpoint returned HTTP ${resp.status}`);
    }
    const data = await resp.json();
    const token = data?.token;
    if (!token) throw new Error('pair response missing token');
    try { window.sessionStorage.setItem('feral.session.pair_token', token); } catch { /* ignore */ }
    return token;
  } catch (err) {
    const msg = err?.message || String(err);
    throw new Error(
      `Perception share requires authentication; ` +
      `${msg}. Sign in to FERAL and try again.`
    );
  }
}

/* ────────────────────────────────────────────────────────────────
 * Module-level store
 * ──────────────────────────────────────────────────────────────── */

function clampFps(n) {
  return Math.max(1, Math.min(10, Math.round(Number(n) || DEFAULT_FPS)));
}

function initialSnapshot() {
  return {
    status: 'idle', // idle | requesting | running | paused | error
    error: null,
    stats: { framesSent: 0, audioChunksSent: 0, lastFrameAt: 0 },
    controls: { fps: DEFAULT_FPS, audioMuted: false, videoMuted: false },
    nodeId: pickNodeId(),
  };
}

function initialConfig() {
  return {
    fps: DEFAULT_FPS,
    jpegQuality: DEFAULT_JPEG_QUALITY,
    audio: true,
    video: true,
    token: null,
  };
}

let snapshot = initialSnapshot();
let config = initialConfig();

/**
 * Live mirror of `snapshot.controls`, read at CALL time by the
 * long-lived media callbacks.
 *
 * `processor.onaudioprocess` is assigned exactly once, inside start(),
 * and is never reassigned. A `controls` value read from the enclosing
 * closure therefore freezes at its start-time value: that was the mute
 * bug, where toggling the mic after the share started flipped the icon
 * to MicOff and aria-pressed to true while audio kept streaming. The
 * handler reads `controlsRef.current` so it always sees the current
 * value, the same way the frame loop is kept current.
 */
const controlsRef = { current: snapshot.controls };

const listeners = new Set();
let refCount = 0;

// Media handles. Exactly one of each exists per browser tab.
let mediaStream = null;
let videoEl = null;
let canvasEl = null;
let socket = null;
let audioCtx = null;
let audioNode = null;
let frameTimer = null;
let visibilityTimer = null;
let visibilityBound = false;

function emit() {
  for (const l of listeners) {
    try { l(); } catch { /* a bad subscriber must not stall the others */ }
  }
}

function setState(patch) {
  snapshot = { ...snapshot, ...patch };
  if (patch.controls) controlsRef.current = snapshot.controls;
  emit();
}

function getSnapshot() {
  return snapshot;
}

function sendRaw(obj) {
  const ws = socket;
  if (!ws || ws.readyState !== 1) return false;
  try {
    ws.send(JSON.stringify(obj));
    return true;
  } catch {
    return false;
  }
}

function buildEnvelope(type, payload) {
  return {
    msg_id: (typeof crypto !== 'undefined' && crypto.randomUUID) ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`,
    session_id: '',
    timestamp_ms: Date.now(),
    hop: 'daemon',
    type,
    payload,
  };
}

/**
 * Single gate for "may bytes leave this browser right now".
 *
 * Both emit paths call it on every frame/chunk. `status === 'running'`
 * is exactly the condition under which the dock indicator renders, so
 * anything that is streaming is also visibly streaming.
 */
function mayEmit() {
  return snapshot.status === 'running';
}

async function captureFrame() {
  if (!mayEmit()) return;
  if (!videoEl || !canvasEl || controlsRef.current.videoMuted) return;
  const width = Math.min(videoEl.videoWidth || 640, 1280);
  const height = Math.min(videoEl.videoHeight || 480, 960);
  if (!width || !height) return;
  canvasEl.width = width;
  canvasEl.height = height;
  const ctx = canvasEl.getContext('2d');
  if (!ctx) return;
  ctx.drawImage(videoEl, 0, 0, width, height);
  const blob = await new Promise((resolve) => canvasEl.toBlob(resolve, 'image/jpeg', config.jpegQuality));
  if (!blob) return;
  if (blob.size > MAX_JPEG_BYTES) return; // brain rejects oversized frames; skip instead of shouting
  const data_b64 = await blobToBase64(blob);
  if (bytesFromBase64(data_b64) > MAX_JPEG_BYTES) return;
  // Re-check after the awaits: the user may have muted or stopped while
  // the JPEG was encoding.
  if (!mayEmit() || controlsRef.current.videoMuted) return;
  const ok = sendRaw(buildEnvelope('video_frame', {
    node_id: snapshot.nodeId,
    encoding: 'jpeg',
    resolution: [width, height],
    data_b64,
    timestamp: Date.now() / 1000,
    metadata: { source: 'browser_camera', quality: Math.round(config.jpegQuality * 100) },
  }));
  if (ok) {
    setState({ stats: { ...snapshot.stats, framesSent: snapshot.stats.framesSent + 1, lastFrameAt: Date.now() } });
  }
}

function startFrameLoop() {
  if (frameTimer) clearInterval(frameTimer);
  const intervalMs = Math.max(100, Math.round(1000 / clampFps(controlsRef.current.fps)));
  frameTimer = setInterval(() => { captureFrame().catch(() => {}); }, intervalMs);
}

function stopFrameLoop() {
  if (frameTimer) {
    clearInterval(frameTimer);
    frameTimer = null;
  }
}

/**
 * Gate the microphone at the source.
 *
 * A disabled MediaStreamTrack still fires `onaudioprocess`, it just
 * delivers silence, so this is belt-and-braces with the guard inside the
 * handler. It matters anyway: while muted the browser's own mic
 * indicator reflects a disabled track, and no captured audio ever
 * reaches JS.
 */
function applyAudioGate() {
  const enabled = !controlsRef.current.audioMuted && snapshot.status === 'running';
  try {
    (mediaStream?.getAudioTracks?.() || []).forEach((t) => { t.enabled = enabled; });
  } catch { /* fake/di stream without track control */ }
}

async function attachAudioWorklet(stream) {
  if (!config.audio || typeof AudioContext === 'undefined') return;
  try {
    const ctx = new AudioContext({ sampleRate: PCM_SAMPLE_RATE });
    audioCtx = ctx;
    // ScriptProcessor is deprecated but universally available. The modern
    // AudioWorklet path would require bundling a worklet file with the v2
    // client; ScriptProcessor keeps this hook self-contained.
    const source = ctx.createMediaStreamSource(stream);
    const processor = ctx.createScriptProcessor(4096, 1, 1);
    processor.onaudioprocess = (event) => {
      // Assigned once, lives for the whole share: every flag it reads
      // must be read from live state, never captured. See controlsRef.
      if (!mayEmit()) return;
      if (controlsRef.current.audioMuted) return;
      const input = event.inputBuffer.getChannelData(0);
      const buf = new Float32Array(input);
      const data_b64 = float32ToPcm16Base64(buf);
      const ok = sendRaw(buildEnvelope('audio_frame', {
        node_id: snapshot.nodeId,
        encoding: 'pcm16',
        sample_rate: PCM_SAMPLE_RATE,
        channels: 1,
        duration_ms: Math.round((buf.length / PCM_SAMPLE_RATE) * 1000),
        data_b64,
      }));
      if (ok) {
        setState({ stats: { ...snapshot.stats, audioChunksSent: snapshot.stats.audioChunksSent + 1 } });
      }
    };
    source.connect(processor);
    processor.connect(ctx.destination);
    audioNode = processor;
  } catch (e) {
    // Audio is best-effort. Video remains primary.
    // eslint-disable-next-line no-console
    console.warn('Perception audio attach failed:', e);
  }
}

function detachAudioWorklet() {
  try { audioNode?.disconnect(); } catch { /* ignore */ }
  try { audioCtx?.close(); } catch { /* ignore */ }
  if (audioNode) audioNode.onaudioprocess = null;
  audioNode = null;
  audioCtx = null;
}

function openSocket(pairToken) {
  return new Promise((resolve, reject) => {
    if (!pairToken) {
      reject(new Error(
        'Perception share requires a pair token; ' +
        'caller must authenticate via /api/devices/pair before opening /v1/node.'
      ));
      return;
    }
    try {
      // Follow the BrowserNode.js convention: pass the token via the
      // Sec-WebSocket-Protocol subprotocol (browsers cannot set
      // Authorization headers on WebSocket constructors).
      // Brain side reads this in feral-core/api/server.py
      // daemon_session via _extract_protocol_bearer.
      const ws = new WebSocket(
        `${WS_BASE}/v1/node`,
        [`feral-token-${pairToken}`]
      );
      socket = ws;
      ws.onopen = () => {
        ws.send(JSON.stringify(buildEnvelope('node_register', {
          node_id: snapshot.nodeId,
          node_type: 'browser_camera',
          os: navigator?.userAgent || 'browser',
          platform: 'browser',
          manufacturer: 'browser',
          model: 'browser_getUserMedia',
          capabilities: [
            ...(config.video ? ['camera', 'browser_camera'] : []),
            ...(config.audio ? ['microphone', 'audio_frame'] : []),
            'video_frame',
            'browser_share',
          ],
        })));
        resolve(ws);
      };
      ws.onerror = () => reject(new Error('perception socket error'));
      ws.onclose = () => {
        if (snapshot.status !== 'idle') {
          setState({ status: 'paused' });
          applyAudioGate();
        }
      };
    } catch (err) {
      reject(err);
    }
  });
}

function closeSocket() {
  try { socket?.close(); } catch { /* ignore */ }
  socket = null;
}

async function attachVideo(stream) {
  if (!videoEl) {
    const el = document.createElement('video');
    el.muted = true;
    el.playsInline = true;
    el.setAttribute('playsinline', 'true');
    el.style.position = 'fixed';
    el.style.width = '1px';
    el.style.height = '1px';
    el.style.left = '-9999px';
    el.style.top = '-9999px';
    document.body.appendChild(el);
    videoEl = el;
  }
  if (!canvasEl) {
    canvasEl = document.createElement('canvas');
  }
  videoEl.srcObject = stream;
  try { await videoEl.play(); } catch { /* autoplay restrictions; video tracks will still fire frames */ }
}

function detachVideo() {
  const el = videoEl;
  if (!el) return;
  try {
    const tracks = el.srcObject?.getTracks?.() || [];
    tracks.forEach((t) => t.stop());
  } catch { /* ignore */ }
  try { el.remove(); } catch { /* ignore */ }
  videoEl = null;
  canvasEl = null;
}

export function stopShare() {
  stopFrameLoop();
  detachAudioWorklet();
  detachVideo();
  try {
    mediaStream?.getTracks?.().forEach((t) => t.stop());
  } catch { /* ignore */ }
  mediaStream = null;
  closeSocket();
  setState({ status: 'idle' });
}

export async function startShare() {
  if (snapshot.status === 'running' || snapshot.status === 'requesting') return;
  setState({ error: null, status: 'requesting' });
  try {
    // Lane 11 fix: auth-gate the WS before requesting camera/mic
    // so a denied auth doesn't leave the user with a permission
    // prompt and no working connection. Throws when the user isn't
    // signed in OR the brain isn't reachable.
    const pairToken = await resolvePairToken({ apiBase: API_BASE, explicit: config.token });
    const stream = await navigator.mediaDevices.getUserMedia({ video: config.video, audio: config.audio });
    mediaStream = stream;
    await attachVideo(stream);
    await openSocket(pairToken);
    await attachAudioWorklet(stream);
    setState({ status: 'running' });
    applyAudioGate();
    startFrameLoop();
  } catch (e) {
    // Tear down side effects first (mic / camera tracks, partial
    // WS), then flip to ``error`` so the caller's UI shows the
    // failure. ``stopShare()`` resets status to ``idle``; the error
    // state must come AFTER so the user sees what went wrong.
    stopShare();
    setState({ error: e?.message || 'permission denied', status: 'error' });
  }
}

export function pauseShare() {
  stopFrameLoop();
  setState({ status: 'paused' });
  // Paused means nothing is sent, audio included. mayEmit() already
  // blocks the audio path; disabling the tracks stops capture too.
  applyAudioGate();
}

export function resumeShare() {
  if (!mediaStream || !socket) {
    startShare();
    return;
  }
  setState({ status: 'running' });
  applyAudioGate();
  startFrameLoop();
}

export function setFps(next) {
  const fps = clampFps(next);
  if (fps === snapshot.controls.fps) return;
  setState({ controls: { ...snapshot.controls, fps } });
  if (snapshot.status === 'running') startFrameLoop();
}

export function toggleAudio() {
  setState({ controls: { ...snapshot.controls, audioMuted: !snapshot.controls.audioMuted } });
  applyAudioGate();
}

export function toggleVideo() {
  setState({ controls: { ...snapshot.controls, videoMuted: !snapshot.controls.videoMuted } });
}

function onVisibilityChange() {
  if (document.visibilityState === 'hidden') {
    if (visibilityTimer) clearTimeout(visibilityTimer);
    visibilityTimer = setTimeout(() => {
      visibilityTimer = null;
      if (snapshot.status === 'running') pauseShare();
    }, HIDDEN_PAUSE_MS);
  } else if (visibilityTimer) {
    clearTimeout(visibilityTimer);
    visibilityTimer = null;
  }
}

function subscribe(listener) {
  listeners.add(listener);
  refCount += 1;
  if (refCount === 1 && !visibilityBound) {
    document.addEventListener('visibilitychange', onVisibilityChange);
    visibilityBound = true;
  }
  return () => {
    listeners.delete(listener);
    refCount -= 1;
    if (refCount <= 0) {
      refCount = 0;
      if (visibilityBound) {
        document.removeEventListener('visibilitychange', onVisibilityChange);
        visibilityBound = false;
      }
      if (visibilityTimer) {
        clearTimeout(visibilityTimer);
        visibilityTimer = null;
      }
      // Nothing is mounted, so no indicator can be shown. Streaming
      // without an indicator is the one thing this module must never
      // do, so the share dies with its last consumer.
      stopShare();
    }
  };
}

/**
 * Apply caller options to the single shared config.
 *
 * There is one share, so there is one config: an omitted option means
 * "the default", not "leave whatever a previous mount set". Anything
 * else makes the effective config depend on mount order, which is how
 * a stale pair token would survive from one caller to the next.
 *
 * While idle, the controls mirror the config. Once the share is live the
 * controls belong to the user, so a pane re-mounting mid-share can never
 * silently unmute a muted mic.
 */
function configure(opts) {
  const next = initialConfig();
  for (const key of ['fps', 'jpegQuality', 'audio', 'video', 'token']) {
    if (opts[key] !== undefined) next[key] = opts[key];
  }
  config = next;
  if (snapshot.status !== 'idle') return;
  const controls = {
    fps: clampFps(config.fps),
    audioMuted: !config.audio,
    videoMuted: !config.video,
  };
  const changed = controls.fps !== snapshot.controls.fps
    || controls.audioMuted !== snapshot.controls.audioMuted
    || controls.videoMuted !== snapshot.controls.videoMuted;
  if (changed) setState({ controls });
}

/** Test-only: drop every share and reset the store between vitest cases. */
export function _resetPerceptionShareForTesting() {
  stopFrameLoop();
  detachAudioWorklet();
  detachVideo();
  closeSocket();
  mediaStream = null;
  if (visibilityTimer) {
    clearTimeout(visibilityTimer);
    visibilityTimer = null;
  }
  if (visibilityBound) {
    document.removeEventListener('visibilitychange', onVisibilityChange);
    visibilityBound = false;
  }
  listeners.clear();
  refCount = 0;
  config = initialConfig();
  snapshot = initialSnapshot();
  controlsRef.current = snapshot.controls;
}

export function usePerceptionShare({
  fps,
  jpegQuality,
  audio,
  video,
  token,
} = {}) {
  const snap = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);

  useEffect(() => {
    configure({ fps, jpegQuality, audio, video, token });
  }, [fps, jpegQuality, audio, video, token]);

  return useMemo(() => ({
    status: snap.status,
    error: snap.error,
    stats: snap.stats,
    controls: snap.controls,
    nodeId: snap.nodeId,
    start: startShare,
    stop: stopShare,
    pause: pauseShare,
    resume: resumeShare,
    setFps,
    toggleAudio,
    toggleVideo,
    apiBase: API_BASE,
  }), [snap]);
}

export default usePerceptionShare;
