import React, { useState, useEffect } from 'react';
import Orb from '../ui/Orb';
import Glass from '../ui/Glass';
import { useVoice } from './VoiceContext';

const PROVIDER_LABEL = {
  openai: 'OpenAI Realtime',
  gemini: 'Gemini Live',
  'local-whisper': 'Local Whisper + Piper',
};

// The chained pipeline's VoiceState enum
// (feral-core/voice/chained_pipeline.py) mapped onto orb modes. Every
// value here is a mode Orb.jsx actually styles; an unlisted phase falls
// through to the session-state mapping rather than silently rendering
// as idle.
export const PHASE_MODE = {
  listening: 'listening',
  processing: 'thinking',
  speaking: 'speaking',
  error: 'alerting',
};

export const PHASE_TEXT = {
  listening: 'Listening. Speak naturally.',
  processing: 'Thinking…',
  speaking: 'Speaking…',
};

/**
 * VoiceOverlay — desktop voice surface.
 *
 * v2026.5.30 — voice no longer takes over the whole viewport by
 * default. Pre-fix the overlay was `position:fixed; inset:0;
 * pointer-events:auto` *and* `.v2-shell.is-voice-mode` dimmed the
 * main content to 0.4 brightness, so starting voice from the
 * menubar effectively locked the entire WebUI. The operator could
 * not keep typing in chat, switch tabs, or look at the dashboard.
 *
 * Now it renders as a compact docked strip pinned to the bottom-
 * right of the viewport with the orb, provider badge, status, mute
 * (when supported), expand, and end. An explicit Expand control
 * flips to the original full-viewport layout for screen-share /
 * presentation mode. Voice can be running and the chat / dock /
 * dashboard stay fully interactive.
 */
/**
 * Audit-r11 — Bug 3 banner. Brain emits `voice_status` (degraded /
 * unavailable + reason) when the realtime provider fails. The
 * overlay renders this above the controls so the user knows why TTS
 * is silent (and which next action — top up OpenAI credit, switch
 * provider, etc.) instead of guessing.
 */
function VoiceStatusBanner({ status }) {
  if (!status) return null;
  const headline =
    status.state === 'unavailable'
      ? 'Voice unavailable'
      : 'Voice degraded — using fallback TTS';
  const subline =
    {
      openai_realtime_quota: 'OpenAI Realtime is out of credit. Top up at platform.openai.com/usage.',
      openai_realtime_auth: 'OpenAI API key is invalid or expired.',
      openai_realtime_rate_limit: 'OpenAI Realtime is rate-limited; retrying via fallback TTS.',
      fallback_tts_failed: 'No fallback TTS provider is configured.',
      no_tts_provider: 'No TTS provider configured in settings.',
    }[status.reason] || status.detail || '';
  return (
    <div className="v2-voice-status-banner__row" role="status">
      <span className="v2-voice-status-banner__icon" aria-hidden="true">!</span>
      <div className="v2-voice-status-banner__text">
        <strong>{headline}</strong>
        {subline && <span>{subline}</span>}
      </div>
    </div>
  );
}

export default function VoiceOverlay() {
  const voice = useVoice();
  const visible = voice.active;
  const [variant, setVariant] = useState('docked'); // 'docked' | 'fullscreen'
  // Each time voice starts fresh, default back to docked so an
  // operator who expanded once doesn't keep getting the full takeover.
  useEffect(() => {
    if (!visible) setVariant('docked');
  }, [visible]);

  // The brain's per-turn phase wins when it reports one. Only the
  // chained pipeline sends `voice_state`; on the realtime path this is
  // null and the session state below drives the orb, as before.
  //
  // `active` used to map to `speaking`, so an open session animated as
  // if FERAL were talking the entire time it was waiting for the user,
  // and the `listening` orb mode was never reachable from anywhere.
  const mode =
    PHASE_MODE[voice.phase] ||
    (voice.state === 'starting' ? 'thinking' :
     voice.state === 'reconnecting' ? 'thinking' :
     voice.state === 'degraded' ? 'alerting' :
     voice.state === 'active' ? 'listening' :
     'idle');

  const providerLabel = PROVIDER_LABEL[voice.provider] || voice.provider || 'Voice';
  const statusText =
    (voice.phase === 'error' && (voice.phaseError || 'Voice failed.')) ||
    PHASE_TEXT[voice.phase] ||
    (voice.state === 'starting' ? 'Opening channel…' :
     voice.state === 'active' ? 'Listening. Speak naturally.' :
     voice.state === 'reconnecting' ? 'Reconnecting…' :
     voice.state === 'degraded' ? 'Brain socket down, voice paused.' :
     '');

  const isFullscreen = variant === 'fullscreen';

  return (
    <div
      className={
        `v2-voice-overlay v2-voice-overlay--${variant}` +
        (visible ? ' is-visible' : '')
      }
      data-variant={variant}
      aria-hidden={!visible}
      role={isFullscreen ? 'dialog' : 'region'}
      aria-modal={isFullscreen ? 'true' : undefined}
      aria-label="Voice session"
    >
      <div className="v2-voice-orb">
        <Orb
          size={isFullscreen ? 320 : 56}
          mode={mode}
          label="FERAL voice"
        />
      </div>
      <div className="v2-voice-meta">
        <Glass level={2} radius="pill" padding="sm" className="v2-voice-provider">
          <span className="v2-voice-dot" />
          {providerLabel}
        </Glass>
        {statusText && (
          <div className="v2-voice-status">{statusText}</div>
        )}
      </div>
      {voice.voiceStatus && (
        <Glass level={1} radius="md" padding="sm" className="v2-voice-status-banner">
          <VoiceStatusBanner status={voice.voiceStatus} />
        </Glass>
      )}
      {voice.transcript && isFullscreen && (
        <Glass level={1} radius="md" padding="md" className="v2-voice-transcript">
          <span>{voice.transcript}</span>
        </Glass>
      )}
      <Glass level={2} radius="pill" padding="sm" className="v2-voice-endbar">
        <button
          type="button"
          className="v2-btn"
          aria-label={isFullscreen ? 'Minimize voice' : 'Expand voice'}
          onClick={() => setVariant(isFullscreen ? 'docked' : 'fullscreen')}
        >
          {isFullscreen ? 'Minimize' : 'Expand'}
        </button>
        <button
          type="button"
          className="v2-btn v2-btn--primary"
          onClick={() => voice.stop()}
        >
          End voice
        </button>
      </Glass>
    </div>
  );
}
