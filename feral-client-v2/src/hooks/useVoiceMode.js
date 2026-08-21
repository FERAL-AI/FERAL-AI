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
  // Who is talking, on the realtime path. The brain sends `voice_state`
  // only from the chained pipeline; for realtime the evidence is local
  // and was being thrown away at both ends. `userSpeaking` comes from
  // the engine's microphone energy gate (`onVADChange`, which had no
  // consumer at all) and `assistantSpeaking` from the `is_final` flag
  // on `audio_response` / `tts_chunk` (read by nothing).
  const [userSpeaking, setUserSpeaking] = useState(false);
  const [assistantSpeaking, setAssistantSpeaking] = useState(false);
  // The brain's answer to the `voice_config` the engine sends on every
  // start and reconnect. Nothing handled the frame, so a config the
  // brain refused was indistinguishable from one it accepted.
  const [configAck, setConfigAck] = useState(null);
  // Transcript metadata the payload carries and the caption dropped.
  const [transcriptPartial, setTranscriptPartial] = useState(false);
  const [transcriptConfidence, setTranscriptConfidence] = useState(null);

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
            // `cause`, `summary` and `recommendation` are what
            // `feral-core/voice/diagnostics.py` exists to produce: the
            // only human explanation of a voice failure anywhere in the
            // system. They were dropped here, so the overlay was left
            // matching `reason` against a five-entry lookup table and
            // rendering nothing for anything outside it.
            //
            // `privacy_downgrade` means FERAL refused to serve a
            // local-only session from a cloud vendor. That is a
            // refusal, never a degradation, and the UI has to say so.
            // `muted` is the live ingress state, stamped on every
            // status frame precisely so a client cannot render
            // "listening" over a microphone the brain is ignoring.
            setVoiceStatus({
              state: payload.state || 'degraded',
              reason: payload.reason || '',
              provider: payload.provider || '',
              fallbackProvider: payload.fallback_provider || '',
              detail: payload.detail || '',
              cause: payload.cause || '',
              summary: payload.summary || '',
              recommendation: payload.recommendation || '',
              privacyDowngrade: !!payload.privacy_downgrade,
              muted: !!payload.muted,
            });
          }
          break;
        }
        case 'voice_config_ack': {
          // The brain's reply to `voice_config`. It reports the mode
          // and the provider it ACTUALLY selected, which is not always
          // the one asked for, and a status that can say no.
          const payload = msg.payload || {};
          const ack = {
            mode: payload.mode || '',
            provider: payload.provider || '',
            status: payload.status || '',
          };
          setConfigAck(ack);
          if (ack.provider) setProvider(ack.provider);
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
    setTranscriptPartial(false);
    setTranscriptConfidence(null);
    setUserSpeaking(false);
    setAssistantSpeaking(false);
    setConfigAck(null);
    // A phase left over from the last session would show the new one
    // as "speaking" before a word has been said.
    setPhase(null);
    setPhaseError('');
    try {
      // A RESOLVER, not `socket.ws`. The shared FeralSocket swaps its
      // `ws` for a new one whenever it rebinds to a different chat
      // thread (`FeralSocket.setSession` closes and reopens) or
      // reconnects. Handing the engine the object froze it against the
      // socket that the next thread switch closed: its reconnect logic
      // re-checked that same dead reference eight times over about 79
      // seconds of backoff, dropped every audio chunk in the meantime,
      // and then went degraded. With a resolver it re-reads the live
      // socket off the singleton and the first retry, 1s later, lands
      // on the reconnected one.
      const engine = new RealtimeVoiceEngine(() => socket.ws, {
        onStateChange: (s) => setState(s === 'active' ? 'active' : s),
        // `transcript` is documented as the latest USER utterance
        // snippet (the overlay caption); assistant transcripts belong
        // in the chat log, not the caption.
        onTranscript: (text, isPartial, role, meta) => {
          if (role !== 'user') return;
          setTranscript(text || '');
          setTranscriptPartial(!!isPartial);
          setTranscriptConfidence(
            meta && typeof meta.confidence === 'number' ? meta.confidence : null,
          );
        },
        // The engine has computed microphone energy on every 100ms
        // frame since it was written, and published it here, and
        // nothing subscribed. This is what lets the orb show that the
        // mic is hearing someone rather than only that it is open.
        onVADChange: (speaking) => setUserSpeaking(!!speaking),
        onAssistantSpeaking: (speaking) => setAssistantSpeaking(!!speaking),
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
    setUserSpeaking(false);
    setAssistantSpeaking(false);
    setTranscriptPartial(false);
    setTranscriptConfidence(null);
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
    transcriptPartial,
    transcriptConfidence,
    voiceStatus,
    configAck,
    phase,
    phaseError,
    userSpeaking,
    assistantSpeaking,
    // `degraded` belongs here. It is the state that means "voice
    // stopped and there is something to tell you", and leaving it out
    // hid the only surface that says so: `VoiceOverlay` renders on
    // `active`, so its "Brain socket down, voice paused." string was
    // unreachable, and `Menubar` disables its button on
    // `state !== 'open' && !voice.active`, which for a degraded
    // session is exactly true, so the user could not even end it.
    //
    // `active` here means "a voice session is open", not "the
    // microphone is live" - `starting` and `reconnecting` were
    // already in it on the same reading.
    active: (
      state === 'active'
      || state === 'starting'
      || state === 'reconnecting'
      || state === 'degraded'
    ),
    start,
    stop,
    toggle,
  };
}
