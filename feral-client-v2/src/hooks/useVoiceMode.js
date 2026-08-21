import { useCallback, useEffect, useRef, useState } from 'react';
import { RealtimeVoiceEngine } from '../lib/voiceRealtime';
import { apiJson } from '../lib/api';
import { useFeralSocket } from './useFeralSocket';

/**
 * useVoiceMode — single source of truth for v2's voice state.
 *
 * Shape:
 *   state: 'off' | 'starting' | 'active' | 'reconnecting' | 'degraded' | 'ended'
 *   provider: 'openai' | 'gemini' | 'local-whisper' | null
 *   transcript: string (latest user utterance snippet)
 *
 * The Menubar's voice button toggles start/stop. VoiceOverlay reads the
 * state to drive the transition + Orb takeover.
 */
const STORAGE_KEY = 'feral_v2_voice_provider';

export function useVoiceMode() {
  const socket = useFeralSocket();
  const engineRef = useRef(null);
  const [state, setState] = useState('off');
  const [provider, setProvider] = useState(null);
  const [transcript, setTranscript] = useState('');
  // Audit-r11 — Bug 3 (silent voice). The brain emits `voice_status`
  // when the realtime provider fails (e.g. OpenAI 1013
  // insufficient_quota) so clients can render a banner instead of
  // going mute. Shape mirrors `feral-core/models/protocol.py:VoiceStatusPayload`.
  const [voiceStatus, setVoiceStatus] = useState(null);
  // Per-turn phase from the brain's `voice_state` frame: idle,
  // listening, processing, speaking or error. Null while nothing has
  // reported one, which is the realtime path, where the engine drives
  // the phase locally instead.
  const [phase, setPhase] = useState(null);
  const [phaseError, setPhaseError] = useState('');

  useEffect(() => {
    (async () => {
      try {
        const config = await apiJson('/api/config');
        const cfgProvider =
          config?.features?.voice_provider ||
          config?.voice_provider ||
          (typeof localStorage !== 'undefined' && localStorage.getItem(STORAGE_KEY)) ||
          'openai';
        setProvider(cfgProvider);
      } catch {
        setProvider('openai');
      }
    })();
  }, []);

  // Audit-r11 — Bug 3 (silent voice on WebUI desktop). Pre-fix,
  // `useVoiceMode` constructed a `RealtimeVoiceEngine` but never
  // wired the shared FeralSocket to dispatch incoming brain frames
  // into the engine — `handleAudioResponse`, `handleSpeechStarted`,
  // and the new `handleTtsChunk` had ZERO callers. v1 wired this in
  // its `src/hooks/useFeralSession.js` (feral-client/ was deleted in
  // 2026.8.12); the port to v2 was
  // missed. Without this subscription the desktop voice path stayed
  // silent for both healthy realtime AND the new whisper fallback.
  // Also tracks `voice_status` so the VoiceOverlay banner renders.
  useEffect(() => {
    if (!socket || typeof socket.subscribe !== 'function') return undefined;
    return socket.subscribe((msg) => {
      if (!msg || !msg.type) return;
      const engine = engineRef.current;
      switch (msg.type) {
        case 'audio_response':
        case 'audio_delta':
          if (engine?.handleAudioResponse) engine.handleAudioResponse(msg.payload || {});
          break;
        case 'tts_chunk':
          if (engine?.handleTtsChunk) {
            engine.handleTtsChunk(msg.payload || {}).catch(() => {});
          }
          break;
        case 'speech_started':
          if (engine?.handleSpeechStarted) engine.handleSpeechStarted();
          break;
        case 'transcript':
          // `handleTranscript` had zero callers, so `voice.transcript`
          // never populated and VoiceOverlay's caption stayed empty.
          if (engine?.handleTranscript) engine.handleTranscript(msg.payload || {});
          break;
        case 'voice_state': {
          // The per-turn conversational phase, which is a different
          // question from `voice_status` (provider health) and from
          // `state` (is a session open at all).
          //
          // On the realtime path RealtimeVoiceEngine drives the phase
          // locally. On the chained path nothing does: this frame is
          // the only source, and it was falling through to `default`
          // and being dropped. The consequence was that a chained
          // session showed "Listening" for its whole duration, while
          // FERAL was thinking and then speaking, and a pipeline error
          // never reached the screen at all.
          const payload = msg.payload || {};
          const next = String(payload.state || '');
          setPhase(next || null);
          setPhaseError(next === 'error' ? (payload.error || 'Voice failed.') : '');
          break;
        }
        case 'voice_status': {
          const payload = msg.payload || {};
          if ((payload.state || 'available') === 'available') {
            setVoiceStatus(null);
          } else {
            setVoiceStatus({
              state: payload.state || 'degraded',
              reason: payload.reason || '',
              provider: payload.provider || '',
              fallbackProvider: payload.fallback_provider || '',
              detail: payload.detail || '',
            });
          }
          break;
        }
        default:
          break;
      }
    });
  }, [socket]);

  const start = useCallback(async () => {
    if (state !== 'off' && state !== 'ended') return;
    if (!socket.ws || socket.ws.readyState !== 1) {
      setState('degraded');
      return;
    }
    setState('starting');
    setTranscript('');
    // A phase left over from the last session would show the new one
    // as "speaking" before a word has been said.
    setPhase(null);
    setPhaseError('');
    try {
      const engine = new RealtimeVoiceEngine(socket.ws, {
        onStateChange: (s) => setState(s === 'active' ? 'active' : s),
        // `transcript` is documented as the latest USER utterance
        // snippet (the overlay caption); assistant transcripts belong
        // in the chat log, not the caption.
        onTranscript: (text, _isPartial, role) => {
          if (role === 'user') setTranscript(text || '');
        },
        onError: () => {},
      });
      engineRef.current = engine;
      await engine.start(provider || 'openai');
      setState('active');
    } catch (err) {
      setState('ended');
      engineRef.current = null;
      // eslint-disable-next-line no-console
      console.error('Voice start failed:', err);
    }
  }, [socket, state, provider]);

  const stop = useCallback(() => {
    if (engineRef.current) {
      try { engineRef.current.stop(); } catch {}
      engineRef.current = null;
    }
    setState('ended');
    setPhase(null);
    setPhaseError('');
    setTimeout(() => setState('off'), 220);
  }, []);

  const toggle = useCallback(() => {
    if (state === 'off' || state === 'ended') return start();
    return stop();
  }, [state, start, stop]);

  return {
    state,
    provider,
    setProvider,
    transcript,
    voiceStatus,
    phase,
    phaseError,
    active: state === 'active' || state === 'starting' || state === 'reconnecting',
    start,
    stop,
    toggle,
  };
}
