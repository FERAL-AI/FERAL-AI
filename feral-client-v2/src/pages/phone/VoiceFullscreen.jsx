/**
 * VoiceFullscreen — full-screen voice agent modal for the phone UX.
 *
 * API contract (for Subagent D / ChatPanel long-press-mic):
 * @param {object} props
 * @param {boolean} props.open       — whether the fullscreen modal is visible
 * @param {() => void} props.onClose — called when the user dismisses the modal
 * @param {'idle'|'listening'} [props.initialMode='idle'] — starting state
 * @param {{ send: (type: string, payload: object) => void,
 *           onFrame: (cb: (frame: object) => void) => (() => void),
 *           node?: object,
 *           voice_config?: { mode?: string } }} [props.shell]
 *   — shell context providing WS send, frame subscription, and node reference.
 *     If omitted, the component renders in a disconnected/demo state.
 *
 * Usage:
 *   import { VoiceFullscreen } from '../phone/VoiceFullscreen';
 *   <VoiceFullscreen open={isOpen} onClose={() => setOpen(false)} initialMode="listening" shell={shell} />
 */
import { useState, useEffect, useRef, useCallback } from 'react';
import { createPortal } from 'react-dom';
import VoiceOrb from './VoiceOrb';
import { unlockSharedAudioContext } from '../../lib/audioContext';

const VOICE_STATES = ['idle', 'listening', 'processing', 'speaking', 'error'];

function triggerHaptic(pattern) {
  if (typeof navigator !== 'undefined' && typeof navigator.vibrate === 'function') {
    navigator.vibrate(pattern);
  }
}

function providerLabel(mode) {
  if (!mode) return '';
  if (mode === 'openai_realtime') return 'OpenAI Realtime';
  if (mode === 'gemini_live') return 'Gemini Live';
  if (mode.startsWith('chained')) return 'Chained (Deepgram)';
  return mode;
}

/** Audit-r11 — Bug 3 banner. Renders a single-line warning whenever
 * the brain reports a degraded or unavailable voice subsystem. Same
 * messaging copy as the iOS ChatView banner so cross-surface help
 * docs stay accurate. */
function VoiceStatusBanner({ status, layout = 'overlay' }) {
  if (!status) return null;
  const headline =
    status.state === 'unavailable'
      ? 'Voice unavailable'
      : 'Voice degraded — using fallback TTS';
  const reasonCopy = {
    openai_realtime_quota: 'OpenAI Realtime is out of credit. Top up at platform.openai.com/usage.',
    openai_realtime_auth: 'OpenAI API key is invalid or expired.',
    openai_realtime_rate_limit: 'OpenAI Realtime is rate-limited; retrying via fallback TTS.',
    fallback_tts_failed: 'No fallback TTS provider is configured.',
    no_tts_provider: 'No TTS provider configured in settings.',
  };
  const subline = reasonCopy[status.reason] || status.detail || '';
  const baseStyle = {
    background: 'var(--v2-state-error-soft)',
    border: '1px solid rgb(var(--v2-state-error-rgb) / 0.5)',
    color: 'var(--v2-on-immersive)',
    borderRadius: 12,
    padding: '8px 12px',
    fontSize: 12,
    lineHeight: 1.35,
    pointerEvents: 'auto',
  };
  const positioned = layout === 'overlay'
    ? { position: 'fixed', left: 12, right: 12, top: 12, zIndex: 1001, ...baseStyle }
    : { width: '100%', marginBottom: 12, ...baseStyle };
  return (
    <div role="status" data-testid="voice-status-banner" style={positioned}>
      <div style={{ fontWeight: 600 }}>{headline}</div>
      {subline && <div style={{ opacity: 0.85, marginTop: 2 }}>{subline}</div>}
    </div>
  );
}

function base64ToArrayBuffer(dataB64) {
  const binary = atob(dataB64 || '');
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes.buffer;
}

export function VoiceFullscreen({
  open,
  onClose,
  initialMode = 'idle',
  shell,
  // v2026.5.29 — ``docked`` renders a compact bar pinned to the
  // bottom of the viewport instead of a full-viewport modal so the
  // operator can keep typing in the chat composer or interacting
  // with the dashboard while voice is active. Default stays
  // ``fullscreen`` to preserve existing call sites (VoicePanel).
  variant: variantProp = 'fullscreen',
}) {
  const [variant, setVariant] = useState(variantProp);
  useEffect(() => { setVariant(variantProp); }, [variantProp]);
  const isDocked = variant === 'docked';
  const [voiceState, setVoiceState] = useState(initialMode);
  const [transcript, setTranscript] = useState('');
  const [partialTranscript, setPartialTranscript] = useState('');
  const [brainText, setBrainText] = useState('');
  const [errorMessage, setErrorMessage] = useState('');
  const [isMuted, setIsMuted] = useState(false);
  const [audioLevel, setAudioLevel] = useState(0);
  const [history, setHistory] = useState([]);
  // Audit-r11 — Bug 3 (silent voice on phone). Mirror of the brain's
  // `voice_status` frame so the UI can render a banner explaining the
  // realtime failure instead of going mute. Cleared when the brain
  // emits `state=available`.
  const [voiceStatus, setVoiceStatus] = useState(null);

  const containerRef = useRef(null);
  const prevStateRef = useRef(voiceState);
  const voiceStateRef = useRef(voiceState);
  const analyserRef = useRef(null);
  const animLevelRef = useRef(null);
  const playbackCtxRef = useRef(null);
  const nextPlaybackTimeRef = useRef(0);
  const playbackQueueRef = useRef(Promise.resolve());
  // Buffer sources currently scheduled on the playback timeline. Held
  // so barge-in can stop them; an AudioBufferSourceNode cannot be
  // un-scheduled any other way once `start()` has been called.
  const activeSourcesRef = useRef(new Set());

  useEffect(() => {
    if (!open) return;
    setVoiceState(initialMode);
    setTranscript('');
    setPartialTranscript('');
    setBrainText('');
    setErrorMessage('');
    setAudioLevel(0);
    // Mute is NOT reset here. It used to be, which meant reopening the
    // panel on a session the brain still considers muted painted a live
    // microphone over a muted one. Adopt whatever the node is actually
    // doing; the brain's next `voice_status` frame confirms or corrects
    // it.
    const nodeMuted = shell?.node?.isMicMuted?.();
    setIsMuted(nodeMuted === true);
  }, [open, initialMode, shell]);

  useEffect(() => {
    const prev = prevStateRef.current;
    prevStateRef.current = voiceState;
    voiceStateRef.current = voiceState;
    if (prev === voiceState) return;

    if (prev === 'listening' && voiceState === 'processing') {
      triggerHaptic(50);
    } else if (voiceState === 'error') {
      triggerHaptic([30, 30, 30]);
    }
  }, [voiceState]);

  const ensurePlaybackContext = useCallback(async () => {
    // v2026.5.28 — use the app-wide shared AudioContext that's been
    // unlocked on the first user gesture (see ../../lib/audioContext.js
    // and ../../bootstrap.js). Pre-fix, each VoiceFullscreen instance
    // created its OWN AudioContext inside this async helper and the
    // resume() call landed outside the gesture stack, so Chrome left
    // the context `suspended` and every scheduled PCM source produced
    // silence — exactly the symptom the operator reported on
    // 2026-05-15: text reply arrives in the chat, voice never plays.
    const ctx = await unlockSharedAudioContext();
    if (ctx && playbackCtxRef.current !== ctx) {
      playbackCtxRef.current = ctx;
      nextPlaybackTimeRef.current = 0;
    }
    return ctx || null;
  }, []);

  const queuePcm16Playback = useCallback(async (payload) => {
    const ctx = await ensurePlaybackContext();
    if (!ctx || !payload?.data_b64) return;
    // v2026.5.29 — one-shot diagnostic on the first chunk per
    // session so we can see in DevTools whether the shared
    // AudioContext was actually unlocked when assistant audio
    // started arriving. Silent voice with `state === 'suspended'`
    // means the user-gesture unlock missed; with `state === 'running'`
    // it means the audio is reaching the graph but inaudible for
    // another reason (volume, output device, etc.).
    if (nextPlaybackTimeRef.current === 0) {
      try {
        // eslint-disable-next-line no-console
        console.debug(
          '[voice] first audio chunk:',
          'ctx.state=', ctx.state,
          'sample_rate=', payload.sample_rate,
          'b64_len=', payload.data_b64.length,
        );
      } catch (_err) { /* ignore */ }
    }
    const pcm16 = new Int16Array(base64ToArrayBuffer(payload.data_b64));
    const float32 = new Float32Array(pcm16.length);
    for (let i = 0; i < pcm16.length; i++) {
      float32[i] = pcm16[i] / (pcm16[i] < 0 ? 0x8000 : 0x7fff);
    }
    const sampleRate = Number(payload.sample_rate) || 24000;
    const buffer = ctx.createBuffer(1, float32.length, sampleRate);
    buffer.getChannelData(0).set(float32);
    const source = ctx.createBufferSource();
    source.buffer = buffer;
    source.connect(ctx.destination);
    const now = ctx.currentTime;
    const startTime = Math.max(now, nextPlaybackTimeRef.current);
    activeSourcesRef.current.add(source);
    source.onended = () => activeSourcesRef.current.delete(source);
    source.start(startTime);
    nextPlaybackTimeRef.current = startTime + buffer.duration;
  }, [ensurePlaybackContext]);

  const queueEncodedPlayback = useCallback(async (payload) => {
    const ctx = await ensurePlaybackContext();
    if (!ctx || !payload?.data_b64) return;
    const encoded = base64ToArrayBuffer(payload.data_b64);
    const decoded = await ctx.decodeAudioData(encoded.slice(0));
    const source = ctx.createBufferSource();
    source.buffer = decoded;
    source.connect(ctx.destination);
    const now = ctx.currentTime;
    const startTime = Math.max(now, nextPlaybackTimeRef.current);
    activeSourcesRef.current.add(source);
    source.onended = () => activeSourcesRef.current.delete(source);
    source.start(startTime);
    nextPlaybackTimeRef.current = startTime + decoded.duration;
  }, [ensurePlaybackContext]);

  const queueAudioPlayback = useCallback((type, payload) => {
    if (!payload?.data_b64 || payload?.is_final) return;
    const encoding = (payload.encoding || '').toLowerCase();
    playbackQueueRef.current = playbackQueueRef.current
      .then(async () => {
        // Route on the ENCODING, never on the frame type.
        //
        // This used to read `if (encoding === 'mp3' || type ===
        // 'audio_chunk')`, and the second half of that condition ate
        // the first: the chained pipeline sends its audio as
        // `audio_chunk` whatever the codec, so raw PCM arriving on an
        // `audio_chunk` frame went to `decodeAudioData`, which cannot
        // parse headerless samples and threw `EncodingError` into a
        // .catch that swallowed it. Silent assistant, no console error.
        //
        // That mattered the moment the pipeline started emitting PCM
        // incrementally, which is what lets playback start on the
        // first 100ms rather than after the last byte of the reply.
        if (encoding === 'pcm16' || encoding === 'pcm' || encoding === 'linear16') {
          await queuePcm16Playback(payload);
          return;
        }
        if (encoding === 'mp3' || encoding === 'opus' || encoding === 'wav') {
          await queueEncodedPlayback(payload);
          return;
        }
        // No encoding declared. `tts_chunk` has always been PCM;
        // anything else is assumed to be a self-contained encoded file.
        if (type === 'tts_chunk') {
          await queuePcm16Playback(payload);
          return;
        }
        await queueEncodedPlayback(payload);
      })
      .catch(() => {
        // Keep queue healthy after transient decode/playback failures.
      });
  }, [queueEncodedPlayback, queuePcm16Playback]);

  // Barge-in. The brain cancels the turn the moment its VAD hears the
  // user start talking over the reply, and sends `voice_cancel`. The
  // audio the client has already buffered has to go too: without this,
  // the user keeps hearing the answer they interrupted for as long as
  // playback was scheduled ahead, which is the whole experience
  // barge-in exists to remove.
  const cancelPlayback = useCallback(() => {
    for (const source of activeSourcesRef.current) {
      try { source.stop(); } catch (_err) { /* already ended */ }
    }
    activeSourcesRef.current.clear();
    nextPlaybackTimeRef.current = 0;
    // Drop anything still queued behind the current decode.
    playbackQueueRef.current = Promise.resolve();
  }, []);

  useEffect(() => {
    if (!open) return;
    // PairShell exposes subscribeFrame (not onFrame — Subagent C's
    // JSDoc guessed wrong). Same silent-noop bug as the sendEnvelope
    // fix above. Prefer subscribeFrame; fall through to onFrame only
    // for test harnesses that inject it.
    const subscribe = shell?.subscribeFrame || shell?.onFrame;
    if (typeof subscribe !== 'function') return;

    const unsub = subscribe((frame) => {
      const type = frame?.type || '';
      const payload = frame?.payload || {};

      if (type === 'voice_state') {
        const s = payload.state;
        if (VOICE_STATES.includes(s)) {
          setVoiceState(s);
          if (s === 'error') {
            setErrorMessage(payload.message || 'An error occurred');
          }
        }
      } else if (type === 'transcript') {
        const isFinal = payload.is_final === true || payload.is_partial === false;
        if (isFinal) {
          setTranscript(payload.text || '');
          setPartialTranscript('');
          if (payload.text) {
            setHistory((h) => [...h, { role: 'user', text: payload.text || '' }]);
          }
        } else {
          setPartialTranscript(payload.text || '');
        }
      } else if (type === 'chat_response') {
        setBrainText(payload.text || '');
        setHistory((h) => [...h, { role: 'assistant', text: payload.text || '' }]);
      } else if (type === 'stream_delta') {
        const delta = payload.delta || '';
        if (delta) setBrainText((prev) => `${prev}${delta}`);
      } else if (type === 'tts_chunk' || type === 'audio_response' || type === 'audio_chunk') {
        if (voiceStateRef.current !== 'speaking') setVoiceState('speaking');
        queueAudioPlayback(type, payload);
      } else if (type === 'voice_vad') {
        if (payload.speaking && voiceStateRef.current !== 'listening') {
          setVoiceState('listening');
        }
      } else if (type === 'voice_cancel') {
        // Barge-in from the brain's server-side VAD.
        cancelPlayback();
        setVoiceState('listening');
      } else if (type === 'speech_started') {
        nextPlaybackTimeRef.current = 0;
      } else if (type === 'voice_error') {
        setVoiceState('error');
        setErrorMessage(payload.message || 'Voice session failed');
      } else if (type === 'voice_status') {
        // Audit-r11 — Bug 3 banner contract. Brain emits this when
        // the realtime provider died (`state=degraded`,
        // `reason=openai_realtime_quota`/`openai_realtime_auth`/etc)
        // or even the fallback failed (`state=unavailable`). Render
        // a banner above the orb so the user knows why audio is
        // muted instead of guessing at a connection issue.
        const st = (payload.state || 'available');
        // The brain is the authority on mute: it stamps the live state
        // onto every voice_status frame, which is how a mute applied
        // from another surface, or one that survived a reconnect,
        // reaches this UI. A frame that OMITS the field is an older
        // brain, not an unmute, so absence is ignored rather than
        // treated as false.
        if (typeof payload.muted === 'boolean') {
          setIsMuted(payload.muted);
        }
        if (st === 'available') {
          setVoiceStatus(null);
        } else {
          setVoiceStatus({
            state: st,
            reason: payload.reason || '',
            provider: payload.provider || '',
            fallbackProvider: payload.fallback_provider || '',
            detail: payload.detail || '',
            cause: payload.cause || '',
            summary: payload.summary || '',
            recommendation: payload.recommendation || '',
            privacyDowngrade: payload.privacy_downgrade === true,
          });
        }
      }
    });

    return unsub;
  }, [open, queueAudioPlayback, cancelPlayback, shell]);

  useEffect(() => {
    if (!open) return;

    // Critical iOS Safari fix: do NOT open a second getUserMedia here.
    // BrowserNode already owns the mic via VoicePanel.handleOpen's
    // startMic() call. Safari allows multiple getUserMedia streams
    // from the same origin, BUT the cleanup path `track.stop()` on
    // THIS stream's shared track can kill BrowserNode's track too
    // (depending on iOS version) — which silently breaks audio
    // streaming the moment VoiceFullscreen remounts.
    //
    // Instead, reuse BrowserNode's existing MediaStream if available
    // (stored at shell.node._mediaStream after startMic runs). If the
    // stream isn't ready yet (race), skip the analyser gracefully —
    // the orb still renders from voice_vad frames the brain emits.
    let ctx = null;

    const sharedStream = shell?.node?._mediaStream;
    if (!sharedStream) {
      // No BrowserNode stream yet — orb will update from voice_vad
      // frames instead. Don't open our own getUserMedia.
      return undefined;
    }

    try {
      ctx = new (window.AudioContext || window.webkitAudioContext)();
      if (ctx.state === 'suspended') {
        ctx.resume().catch(() => {});
      }
      const source = ctx.createMediaStreamSource(sharedStream);
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 256;
      source.connect(analyser);
      analyserRef.current = analyser;

      const dataArray = new Uint8Array(analyser.frequencyBinCount);
      const tick = () => {
        if (!analyserRef.current) return;
        analyser.getByteFrequencyData(dataArray);
        let sum = 0;
        for (let i = 0; i < dataArray.length; i++) sum += dataArray[i];
        const avg = sum / dataArray.length / 255;
        setAudioLevel(avg);
        animLevelRef.current = requestAnimationFrame(tick);
      };
      tick();
    } catch {
      // Analyser is best-effort for the orb visual — voice data
      // still flows through BrowserNode's mic → WS pipeline.
    }

    return () => {
      analyserRef.current = null;
      if (animLevelRef.current) cancelAnimationFrame(animLevelRef.current);
      // Do NOT stop sharedStream tracks — they belong to BrowserNode.
      if (ctx) ctx.close().catch(() => {});
    };
  }, [open, shell]);

  useEffect(() => {
    if (!open) {
      nextPlaybackTimeRef.current = 0;
      playbackQueueRef.current = Promise.resolve();
      // v2026.5.28 — do NOT close the AudioContext here. It is now
      // the app-wide shared context (see getSharedAudioContext) and
      // closing it would break any future playback site. Just drop
      // our reference; the shared singleton stays alive.
      playbackCtxRef.current = null;
      return;
    }
    ensurePlaybackContext().catch(() => {});
  }, [ensurePlaybackContext, open]);

  useEffect(() => {
    if (!open) return;
    function handleKeyDown(e) {
      if (e.key === 'Escape') {
        e.preventDefault();
        handleClose();
      }
    }
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  });

  useEffect(() => {
    if (open && containerRef.current) {
      containerRef.current.focus();
    }
  }, [open]);

  const sendEnvelope = useCallback(
    (type, payload = {}) => {
      // PairShell exposes sendFrame(type, payload). The original
      // Subagent C implementation referenced shell.send which doesn't
      // exist on the context, so interrupts + envelopes silently
      // no-opped in the live test. Prefer sendFrame; fall through
      // to shell.send only for off-shell test harnesses that mock it.
      if (typeof shell?.sendFrame === 'function') {
        shell.sendFrame(type, payload);
        return;
      }
      if (typeof shell?.send === 'function') shell.send(type, payload);
    },
    [shell],
  );

  const handleInterrupt = useCallback(() => {
    if (voiceState === 'speaking') {
      sendEnvelope('voice_interrupt', {});
      triggerHaptic(30);
    }
  }, [voiceState, sendEnvelope]);

  const handleMuteToggle = useCallback(() => {
    setIsMuted((m) => {
      const next = !m;
      // Stop capture at the source FIRST. Telling the brain and
      // leaving the microphone running is what the pre-fix button did:
      // the envelope went out, nothing handled it, and the worklet kept
      // streaming PCM to whichever cloud provider was live. The node
      // call is what makes the button's promise true even if the frame
      // never lands.
      if (typeof shell?.node?.setMicMuted === 'function') {
        shell.node.setMicMuted(next);
      }
      sendEnvelope('voice_mute', { muted: next });
      return next;
    });
  }, [sendEnvelope, shell]);

  const handleClose = useCallback(() => {
    sendEnvelope('voice_interrupt', {});
    if (onClose) onClose();
  }, [sendEnvelope, onClose]);

  const handleRetry = useCallback(() => {
    setVoiceState('idle');
    setErrorMessage('');
    sendEnvelope('voice_session_start', {});
  }, [sendEnvelope]);

  const handleKeyboard = useCallback(() => {
    if (onClose) onClose();
  }, [onClose]);

  if (!open) return null;

  const voiceMode = shell?.voice_config?.mode || '';

  // Portal to document.body so position:fixed actually covers the
  // viewport. Without this the modal is trapped inside the pair
  // shell's Glass pane — which has backdrop-filter, creating a
  // containing block that captures fixed-position descendants. That's
  // why the live test showed the voice UI "cut off" as a black
  // rectangle inside the tab area instead of a fullscreen takeover.
  if (typeof document === 'undefined') return null;

  // v2026.5.29 — docked variant: a compact bar pinned to the bottom
  // of the viewport with the orb + status + minimal controls. The
  // rest of the page stays interactive because:
  //   - no full-viewport backdrop
  //   - pointer-events live only on the bar itself
  //   - aria-modal is dropped so screen-reader focus is not trapped
  if (isDocked) {
    return createPortal((
      <>
      <VoiceStatusBanner status={voiceStatus} layout="overlay" />
      <div
        ref={containerRef}
        role="region"
        aria-label="Voice session"
        tabIndex={-1}
        data-testid="voice-fullscreen"
        data-variant="docked"
        style={{
          position: 'fixed',
          left: 12,
          right: 12,
          bottom: 12,
          zIndex: 1000,
          background: 'var(--v2-surface-immersive)',
          color: 'var(--v2-on-immersive)',
          borderRadius: 18,
          boxShadow: 'var(--v2-shadow-glass)',
          backdropFilter: 'blur(12px)',
          WebkitBackdropFilter: 'blur(12px)',
          display: 'flex',
          alignItems: 'center',
          gap: 12,
          padding: '10px 12px',
          pointerEvents: 'auto',
          fontFamily: '-apple-system, BlinkMacSystemFont, sans-serif',
        }}
      >
        <div
          data-testid="orb-container"
          onClick={handleInterrupt}
          style={{
            width: 56,
            height: 56,
            flex: '0 0 auto',
            cursor: voiceState === 'speaking' ? 'pointer' : 'default',
            opacity: isMuted ? 0.5 : 1,
          }}
        >
          <VoiceOrb state={voiceState} audioLevel={audioLevel} />
        </div>
        <div style={{ flex: '1 1 auto', minWidth: 0, fontSize: 13 }}>
          {voiceMode && (
            <div
              data-testid="provider-badge"
              style={{ opacity: 0.55, fontSize: 11, marginBottom: 2 }}
            >
              {providerLabel(voiceMode)}
            </div>
          )}
          <div
            data-testid="voice-status-line"
            style={{ opacity: 0.9, fontSize: 13, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}
          >
            {/* Mute outranks every other state here. The brain is not
                reading this microphone, so "Listening…" would be a
                claim about the system that is simply false. Speaking
                still wins, because mute is an input control and the
                assistant's reply keeps playing. */}
            {voiceState === 'speaking' ? (brainText || 'Speaking…') :
              isMuted ? 'Muted' :
              voiceState === 'processing' ? 'Thinking…' :
              voiceState === 'listening' ? (partialTranscript || transcript || 'Listening…') :
              voiceState === 'error' ? (errorMessage || 'Voice error') :
              'Tap orb to interrupt · ⤢ to expand'}
          </div>
        </div>
        <button
          data-testid="mute-button"
          onClick={handleMuteToggle}
          aria-label={isMuted ? 'Unmute microphone' : 'Mute microphone'}
          aria-pressed={isMuted}
          style={{
            background: isMuted ? 'rgb(var(--v2-state-error-rgb) / 0.3)' : 'var(--v2-fill-strong)',
            color: 'var(--v2-on-immersive)', border: 'none', borderRadius: '50%',
            width: 40, height: 40, fontSize: 16, cursor: 'pointer', flex: '0 0 auto',
          }}
        >
          {isMuted ? '🔇' : '🎤'}
        </button>
        <button
          data-testid="expand-button"
          onClick={() => setVariant('fullscreen')}
          aria-label="Expand voice session"
          style={{
            background: 'var(--v2-fill-strong)',
            color: 'var(--v2-on-immersive)', border: 'none', borderRadius: '50%',
            width: 40, height: 40, fontSize: 16, cursor: 'pointer', flex: '0 0 auto',
          }}
        >
          ⤢
        </button>
        <button
          data-testid="close-button"
          onClick={handleClose}
          aria-label="End voice session"
          style={{
            background: 'rgb(var(--v2-state-error-rgb) / 0.25)',
            color: 'var(--v2-on-immersive)', border: 'none', borderRadius: '50%',
            width: 40, height: 40, fontSize: 16, cursor: 'pointer', flex: '0 0 auto',
          }}
        >
          ✕
        </button>
      </div>
      </>
    ), document.body);
  }

  return createPortal((
    <>
    <VoiceStatusBanner status={voiceStatus} layout="overlay" />
    <div
      ref={containerRef}
      role="dialog"
      aria-label="Voice agent"
      aria-modal="true"
      tabIndex={-1}
      data-testid="voice-fullscreen"
      data-variant="fullscreen"
      style={{
        position: 'fixed',
        inset: 0,
        zIndex: 1000,
        background: 'var(--v2-bg-immersive)',
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'space-between',
        color: 'var(--v2-on-immersive)',
        fontFamily: '-apple-system, BlinkMacSystemFont, sans-serif',
        overflow: 'hidden',
      }}
    >
      {/* Provider badge */}
      {voiceMode && (
        <div
          data-testid="provider-badge"
          style={{
            position: 'absolute',
            top: 16,
            left: 16,
            fontSize: 12,
            opacity: 0.6,
            background: 'var(--v2-fill-strong)',
            padding: '4px 10px',
            borderRadius: 12,
          }}
        >
          {providerLabel(voiceMode)}
        </div>
      )}
      {/* v2026.5.29 — Minimize affordance returns to docked variant
          without ending the voice session. */}
      <button
        data-testid="minimize-button"
        onClick={() => setVariant('docked')}
        aria-label="Minimize voice session"
        style={{
          position: 'absolute',
          top: 12,
          right: 12,
          background: 'var(--v2-fill-strong)',
          color: 'var(--v2-on-immersive)',
          border: 'none',
          borderRadius: 14,
          width: 36,
          height: 28,
          fontSize: 14,
          cursor: 'pointer',
        }}
      >
        ⤡
      </button>

      {/* Transcript area */}
      <div
        style={{
          flex: '0 0 auto',
          width: '100%',
          padding: '60px 24px 16px',
          textAlign: 'center',
          minHeight: 80,
        }}
      >
        {partialTranscript && (
          <p data-testid="partial-transcript" style={{ opacity: 0.7, fontSize: 16, margin: 0 }}>
            {partialTranscript}
          </p>
        )}
        {transcript && (
          <p data-testid="final-transcript" style={{ fontSize: 18, margin: '8px 0 0' }}>
            {transcript}
          </p>
        )}
        {voiceState === 'processing' && (
          <p style={{ opacity: 0.5, fontSize: 14, margin: '8px 0 0' }}>
            Brain is thinking…
          </p>
        )}
      </div>

      {/* Orb (tap to interrupt) */}
      <div
        data-testid="orb-container"
        onClick={handleInterrupt}
        style={{
          flex: '1 1 auto',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          width: '100%',
          cursor: voiceState === 'speaking' ? 'pointer' : 'default',
          opacity: isMuted ? 0.4 : 1,
          transition: 'opacity 0.3s',
        }}
      >
        <div style={{ width: '60vmin', height: '60vmin', maxWidth: 360, maxHeight: 360 }}>
          <VoiceOrb state={voiceState} audioLevel={audioLevel} />
        </div>
      </div>

      {/* Brain response text */}
      {brainText && voiceState === 'speaking' && (
        <div
          data-testid="brain-text"
          style={{
            padding: '0 24px 8px',
            fontSize: 15,
            opacity: 0.8,
            textAlign: 'center',
            maxHeight: 120,
            overflow: 'auto',
          }}
        >
          {brainText}
        </div>
      )}

      {/* Error surface */}
      {voiceState === 'error' && (
        <div data-testid="error-surface" style={{ textAlign: 'center', padding: '0 24px 16px' }}>
          <p style={{ color: 'var(--v2-state-error)', fontSize: 15, margin: '0 0 12px' }}>
            {errorMessage || 'An error occurred'}
          </p>
          <button
            data-testid="retry-button"
            onClick={handleRetry}
            style={{
              background: 'var(--v2-fill-strong)',
              color: 'var(--v2-on-immersive)',
              border: 'none',
              borderRadius: 8,
              padding: '10px 28px',
              fontSize: 15,
              cursor: 'pointer',
            }}
          >
            Retry
          </button>
        </div>
      )}

      {/* Mute takes the status line. "Tap to speak" over a microphone
          the brain is refusing to read is an invitation to talk to
          nothing. */}
      {isMuted && voiceState !== 'speaking' && (
        <p
          data-testid="muted-hint"
          style={{ opacity: 0.75, fontSize: 14, margin: 0, paddingBottom: 8 }}
        >
          Muted (microphone off)
        </p>
      )}
      {/* Idle hint */}
      {!isMuted && voiceState === 'idle' && (
        <p style={{ opacity: 0.4, fontSize: 14, margin: 0, paddingBottom: 8 }}>Tap to speak</p>
      )}

      {/* Bottom controls */}
      <div
        style={{
          flex: '0 0 auto',
          display: 'flex',
          gap: 32,
          padding: '16px 0 40px',
          justifyContent: 'center',
          alignItems: 'center',
        }}
      >
        <button
          data-testid="mute-button"
          onClick={handleMuteToggle}
          aria-label={isMuted ? 'Unmute microphone' : 'Mute microphone'}
          aria-pressed={isMuted}
          style={{
            background: isMuted ? 'rgb(var(--v2-state-error-rgb) / 0.3)' : 'var(--v2-fill-strong)',
            color: 'var(--v2-on-immersive)',
            border: 'none',
            borderRadius: '50%',
            width: 52,
            height: 52,
            fontSize: 20,
            cursor: 'pointer',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
          }}
        >
          {isMuted ? '🔇' : '🎤'}
        </button>

        <button
          data-testid="keyboard-button"
          onClick={handleKeyboard}
          aria-label="Switch to keyboard"
          style={{
            background: 'var(--v2-fill-strong)',
            color: 'var(--v2-on-immersive)',
            border: 'none',
            borderRadius: '50%',
            width: 52,
            height: 52,
            fontSize: 20,
            cursor: 'pointer',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
          }}
        >
          ⌨
        </button>

        <button
          data-testid="close-button"
          onClick={handleClose}
          aria-label="Close voice session"
          style={{
            background: 'rgb(var(--v2-state-error-rgb) / 0.25)',
            color: 'var(--v2-on-immersive)',
            border: 'none',
            borderRadius: '50%',
            width: 52,
            height: 52,
            fontSize: 20,
            cursor: 'pointer',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
          }}
        >
          ✕
        </button>
      </div>
    </div>
    </>
  ), document.body);
}

export default VoiceFullscreen;
