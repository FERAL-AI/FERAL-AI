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
 * Privacy rules (hard-coded, not a setting)
 * -----------------------------------------
 *   • Streaming only starts when start() is explicitly called.
 *   • Visibility change (tab backgrounded > 60s) auto-pauses.
 *   • stop() tears the MediaStream down AND revokes the daemon.
 *   • The dock indicator mounted by <PerceptionShare/> is always visible
 *     while streaming; there is no hidden-share mode.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
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

export function usePerceptionShare({
  fps = DEFAULT_FPS,
  jpegQuality = DEFAULT_JPEG_QUALITY,
  audio = true,
  video = true,
  token = null,
} = {}) {
  const [status, setStatus] = useState('idle'); // idle | requesting | running | paused | disconnected | error
  const [error, setError] = useState(null);
  const [stats, setStats] = useState({ framesSent: 0, audioChunksSent: 0, lastFrameAt: 0 });
  const [controls, setControls] = useState({ fps, audioMuted: !audio, videoMuted: !video });

  const streamRef = useRef(null);
  const videoElRef = useRef(null);
  const canvasRef = useRef(null);
  const socketRef = useRef(null);
  const audioCtxRef = useRef(null);
  const audioNodeRef = useRef(null);
  const frameLoopRef = useRef(null);
  const visibilityTimerRef = useRef(null);
  const nodeIdRef = useRef(pickNodeId());
  const chunkIdxRef = useRef(0);
  // Mirror `status` into a ref so callbacks captured once at start()
  // (the audio worklet's onaudioprocess, the socket onclose) can read
  // the live status without going stale. Privacy-critical: the audio
  // send path gates on this, so a pause must be visible immediately and
  // not wait for a React re-render.
  const statusRef = useRef('idle');
  const setStatusSafe = useCallback((next) => {
    statusRef.current = next;
    setStatus(next);
  }, []);

  const sendRaw = useCallback((obj) => {
    const ws = socketRef.current;
    if (!ws || ws.readyState !== 1) return false;
    try {
      ws.send(JSON.stringify(obj));
      return true;
    } catch {
      return false;
    }
  }, []);

  const buildEnvelope = useCallback((type, payload) => ({
    msg_id: (typeof crypto !== 'undefined' && crypto.randomUUID) ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`,
    session_id: '',
    timestamp_ms: Date.now(),
    hop: 'daemon',
    type,
    payload,
  }), []);

  const captureFrame = useCallback(async () => {
    if (!videoElRef.current || !canvasRef.current || controls.videoMuted) return;
    const video = videoElRef.current;
    const width = Math.min(video.videoWidth || 640, 1280);
    const height = Math.min(video.videoHeight || 480, 960);
    if (!width || !height) return;
    const canvas = canvasRef.current;
    canvas.width = width;
    canvas.height = height;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    ctx.drawImage(video, 0, 0, width, height);
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/jpeg', jpegQuality));
    if (!blob) return;
    if (blob.size > MAX_JPEG_BYTES) return; // brain rejects oversized frames; skip instead of shouting
    const data_b64 = await blobToBase64(blob);
    if (bytesFromBase64(data_b64) > MAX_JPEG_BYTES) return;
    const ok = sendRaw(buildEnvelope('video_frame', {
      node_id: nodeIdRef.current,
      encoding: 'jpeg',
      resolution: [width, height],
      data_b64,
      timestamp: Date.now() / 1000,
      metadata: { source: 'browser_camera', quality: Math.round(jpegQuality * 100) },
    }));
    if (ok) {
      setStats((s) => ({ ...s, framesSent: s.framesSent + 1, lastFrameAt: Date.now() }));
    }
  }, [buildEnvelope, controls.videoMuted, jpegQuality, sendRaw]);

  const startFrameLoop = useCallback(() => {
    if (frameLoopRef.current) clearInterval(frameLoopRef.current);
    const intervalMs = Math.max(100, Math.round(1000 / Math.max(1, controls.fps)));
    frameLoopRef.current = setInterval(() => { captureFrame().catch(() => {}); }, intervalMs);
  }, [captureFrame, controls.fps]);

  const stopFrameLoop = useCallback(() => {
    if (frameLoopRef.current) {
      clearInterval(frameLoopRef.current);
      frameLoopRef.current = null;
    }
  }, []);

  const attachAudioWorklet = useCallback(async (stream) => {
    if (!audio || typeof AudioContext === 'undefined') return;
    try {
      const ctx = new AudioContext({ sampleRate: PCM_SAMPLE_RATE });
      audioCtxRef.current = ctx;
      // ScriptProcessor is deprecated but universally available. The modern
      // AudioWorklet path would require bundling a worklet file with the v2
      // client; ScriptProcessor keeps this hook self-contained.
      const source = ctx.createMediaStreamSource(stream);
      const processor = ctx.createScriptProcessor(4096, 1, 1);
      processor.onaudioprocess = (event) => {
        // Privacy gate: never emit PCM unless the share is actively
        // running. Muting the mic OR any non-running status (paused,
        // disconnected, error, idle) hard-stops transmission. Suspending
        // the AudioContext in pause() already stops these events, but we
        // gate here too so no frame can slip out between transitions.
        if (controls.audioMuted || statusRef.current !== 'running') return;
        const input = event.inputBuffer.getChannelData(0);
        const buf = new Float32Array(input);
        const data_b64 = float32ToPcm16Base64(buf);
        const ok = sendRaw(buildEnvelope('audio_frame', {
          node_id: nodeIdRef.current,
          encoding: 'pcm16',
          sample_rate: PCM_SAMPLE_RATE,
          channels: 1,
          duration_ms: Math.round((buf.length / PCM_SAMPLE_RATE) * 1000),
          data_b64,
        }));
        if (ok) {
          chunkIdxRef.current += 1;
          setStats((s) => ({ ...s, audioChunksSent: s.audioChunksSent + 1 }));
        }
      };
      source.connect(processor);
      processor.connect(ctx.destination);
      audioNodeRef.current = processor;
    } catch (e) {
      // Audio is best-effort. Video remains primary.
      // eslint-disable-next-line no-console
      console.warn('Perception audio attach failed:', e);
    }
  }, [audio, buildEnvelope, controls.audioMuted, sendRaw]);

  const detachAudioWorklet = useCallback(() => {
    try { audioNodeRef.current?.disconnect(); } catch { /* ignore */ }
    try { audioCtxRef.current?.close(); } catch { /* ignore */ }
    audioNodeRef.current = null;
    audioCtxRef.current = null;
  }, []);

  const openSocket = useCallback(async (pairToken) => new Promise((resolve, reject) => {
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
      socketRef.current = ws;
      ws.onopen = () => {
        ws.send(JSON.stringify(buildEnvelope('node_register', {
          node_id: nodeIdRef.current,
          node_type: 'browser_camera',
          os: navigator?.userAgent || 'browser',
          platform: 'browser',
          manufacturer: 'browser',
          model: 'browser_getUserMedia',
          capabilities: [
            ...(video ? ['camera', 'browser_camera'] : []),
            ...(audio ? ['microphone', 'audio_frame'] : []),
            'video_frame',
            'browser_share',
          ],
        })));
        resolve(ws);
      };
      ws.onerror = () => reject(new Error('perception socket error'));
      ws.onclose = () => {
        // Ignore closes for a socket we've already torn down/replaced
        // (stop() nulls socketRef before the async onclose fires).
        if (socketRef.current !== ws) return;
        // A normal stop() has already moved status to 'idle'.
        if (statusRef.current === 'idle') return;
        // Unexpected drop mid-share. Surface a DISTINCT disconnected
        // state — do not masquerade as an active or user-paused share —
        // and halt capture so nothing is queued against a dead socket.
        stopFrameLoop();
        try { audioCtxRef.current?.suspend?.(); } catch { /* ignore */ }
        setStatusSafe('disconnected');
      };
    } catch (err) {
      reject(err);
    }
  }), [audio, buildEnvelope, setStatusSafe, stopFrameLoop, video]);

  const closeSocket = useCallback(() => {
    try { socketRef.current?.close(); } catch { /* ignore */ }
    socketRef.current = null;
  }, []);

  const attachVideo = useCallback(async (stream) => {
    let el = videoElRef.current;
    if (!el) {
      el = document.createElement('video');
      el.muted = true;
      el.playsInline = true;
      el.setAttribute('playsinline', 'true');
      el.style.position = 'fixed';
      el.style.width = '1px';
      el.style.height = '1px';
      el.style.left = '-9999px';
      el.style.top = '-9999px';
      document.body.appendChild(el);
      videoElRef.current = el;
    }
    if (!canvasRef.current) {
      canvasRef.current = document.createElement('canvas');
    }
    el.srcObject = stream;
    try { await el.play(); } catch { /* autoplay restrictions — video tracks will still fire frames */ }
  }, []);

  const detachVideo = useCallback(() => {
    const el = videoElRef.current;
    if (!el) return;
    try {
      const tracks = el.srcObject?.getTracks?.() || [];
      tracks.forEach((t) => t.stop());
    } catch { /* ignore */ }
    try { el.remove(); } catch { /* ignore */ }
    videoElRef.current = null;
    canvasRef.current = null;
  }, []);

  const stop = useCallback(() => {
    stopFrameLoop();
    detachAudioWorklet();
    detachVideo();
    try {
      streamRef.current?.getTracks?.().forEach((t) => t.stop());
    } catch { /* ignore */ }
    streamRef.current = null;
    closeSocket();
    setStatusSafe('idle');
  }, [closeSocket, detachAudioWorklet, detachVideo, setStatusSafe, stopFrameLoop]);

  const start = useCallback(async () => {
    if (status === 'running' || status === 'requesting') return;
    setError(null);
    setStatusSafe('requesting');
    try {
      // Lane 11 fix — auth-gate the WS before requesting camera/mic
      // so a denied auth doesn't leave the user with a permission
      // prompt and no working connection. Throws when the user isn't
      // signed in OR the brain isn't reachable.
      const pairToken = await resolvePairToken({ apiBase: API_BASE, explicit: token });
      const stream = await navigator.mediaDevices.getUserMedia({ video, audio });
      streamRef.current = stream;
      await attachVideo(stream);
      await openSocket(pairToken);
      await attachAudioWorklet(stream);
      startFrameLoop();
      setStatusSafe('running');
    } catch (e) {
      // Tear down side effects first (mic / camera tracks, partial
      // WS) — then flip to ``error`` so the caller's UI shows the
      // failure. ``stop()`` resets status to ``idle``; the error
      // state must come AFTER so the user sees what went wrong.
      stop();
      setError(e?.message || 'permission denied');
      setStatusSafe('error');
    }
  }, [attachAudioWorklet, attachVideo, audio, openSocket, setStatusSafe, startFrameLoop, status, stop, token, video]);

  const pause = useCallback(() => {
    stopFrameLoop();
    // Privacy: stop audio transmission too. Flip status first (the
    // onaudioprocess gate reads statusRef synchronously) then suspend the
    // AudioContext so the worklet stops firing entirely. No PCM leaves the
    // device while paused.
    setStatusSafe('paused');
    try { audioCtxRef.current?.suspend?.(); } catch { /* ignore */ }
  }, [setStatusSafe, stopFrameLoop]);

  const resume = useCallback(() => {
    if (!streamRef.current || !socketRef.current) {
      start();
      return;
    }
    try { audioCtxRef.current?.resume?.(); } catch { /* ignore */ }
    startFrameLoop();
    setStatusSafe('running');
  }, [setStatusSafe, start, startFrameLoop]);

  const setFps = useCallback((next) => {
    setControls((c) => ({ ...c, fps: Math.max(1, Math.min(10, Math.round(next))) }));
  }, []);

  const toggleAudio = useCallback(() => {
    setControls((c) => ({ ...c, audioMuted: !c.audioMuted }));
  }, []);

  const toggleVideo = useCallback(() => {
    setControls((c) => ({ ...c, videoMuted: !c.videoMuted }));
  }, []);

  useEffect(() => {
    if (status === 'running') startFrameLoop();
  }, [controls.fps, startFrameLoop, status]);

  // Auto-pause when the tab is hidden for > HIDDEN_PAUSE_MS.
  useEffect(() => {
    const onVis = () => {
      if (document.visibilityState === 'hidden') {
        visibilityTimerRef.current = setTimeout(() => {
          if (status === 'running') pause();
        }, HIDDEN_PAUSE_MS);
      } else {
        if (visibilityTimerRef.current) {
          clearTimeout(visibilityTimerRef.current);
          visibilityTimerRef.current = null;
        }
      }
    };
    document.addEventListener('visibilitychange', onVis);
    return () => {
      document.removeEventListener('visibilitychange', onVis);
      if (visibilityTimerRef.current) clearTimeout(visibilityTimerRef.current);
    };
  }, [pause, status]);

  useEffect(() => () => stop(), [stop]);

  return useMemo(() => ({
    status,
    error,
    stats,
    controls,
    nodeId: nodeIdRef.current,
    start,
    stop,
    pause,
    resume,
    setFps,
    toggleAudio,
    toggleVideo,
    apiBase: API_BASE,
  }), [controls, error, pause, resume, setFps, start, stats, status, stop, toggleAudio, toggleVideo]);
}

export default usePerceptionShare;
