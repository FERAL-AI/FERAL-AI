import React, { useState, useEffect, useRef, useCallback } from 'react';
import Orb from '../ui/Orb';
import useFocusTrap from '../ui/useFocusTrap';
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
const REASON_TEXT = {
  openai_realtime_quota: 'OpenAI Realtime is out of credit. Top up at platform.openai.com/usage.',
  openai_realtime_auth: 'OpenAI API key is invalid or expired.',
  openai_realtime_rate_limit: 'OpenAI Realtime is rate-limited; retrying via fallback TTS.',
  fallback_tts_failed: 'No fallback TTS provider is configured.',
  no_tts_provider: 'No TTS provider configured in settings.',
};

function VoiceStatusBanner({ status }) {
  if (!status) return null;
  // `summary` and `recommendation` come from
  // `feral-core/voice/diagnostics.py`, which exists solely to turn a
  // machine tag into something a person can act on, and neither ever
  // reached a screen. The table below is the older, narrower path: it
  // covers five `reason` values, so any other failure rendered as a
  // bare headline with no cause at all. The brain's own words win when
  // it sent any; the table is the fallback, and `detail` after that.
  const headline =
    status.privacyDowngrade
      ? 'Voice stopped to protect your privacy'
      : status.state === 'unavailable'
        ? 'Voice unavailable'
        : 'Voice degraded, using fallback TTS';
  const subline =
    status.summary || REASON_TEXT[status.reason] || status.detail || '';
  return (
    <div className="v2-voice-status-banner__row" role="status">
      <span className="v2-voice-status-banner__icon" aria-hidden="true">!</span>
      <div className="v2-voice-status-banner__text">
        <strong>{headline}</strong>
        {subline && <span>{subline}</span>}
        {status.recommendation && (
          <span className="v2-voice-status-banner__fix">
            {status.recommendation}
          </span>
        )}
        {status.muted && (
          <span className="v2-voice-status-banner__muted">
            Your microphone is muted, so nothing is reaching the brain.
          </span>
        )}
        {status.cause && (
          <span className="v2-voice-status-banner__cause" data-cause={status.cause}>
            Cause: {status.cause}
            {status.provider ? ` (${status.provider})` : ''}
          </span>
        )}
      </div>
    </div>
  );
}

/**
 * The brain's answer to the `voice_config` the engine sends on every
 * start and every reconnect. Nothing consumed `voice_config_ack`, so a
 * config the brain refused looked exactly like one it accepted: the
 * session showed as live and no audio ever came back.
 */
function ConfigAckNotice({ ack }) {
  if (!ack || !ack.status || ack.status === 'ok') return null;
  return (
    <div className="v2-voice-status-banner__row" role="status">
      <span className="v2-voice-status-banner__icon" aria-hidden="true">!</span>
      <div className="v2-voice-status-banner__text">
        <strong>The brain did not accept this voice config</strong>
        <span>
          {`Reported "${ack.status}"`}
          {ack.mode ? ` for mode ${ack.mode}` : ''}
          {ack.provider ? ` on ${ack.provider}` : ''}
          {'. Pick a different provider in Settings > Voice.'}
        </span>
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
  //
  // On the realtime path there is no `voice_state` frame, so the orb
  // reads the two local signals that were already being computed and
  // discarded: `assistantSpeaking` (from the `is_final` flag on the
  // audio frames) and `userSpeaking` (from the engine's microphone
  // energy gate). Without them an open session sat on one mode
  // whoever was actually talking. `listening` now means the mic is
  // hearing someone, and `idle` means the session is open and quiet,
  // which is a distinction the user can hear and could not see.
  //
  // `degraded` is `offline`, not `alerting`: the brain socket is gone,
  // which is what that greyed-out mode was drawn for and the only
  // reason it had no producer anywhere in the app.
  const realtimeMode =
    voice.assistantSpeaking ? 'speaking' :
    voice.userSpeaking ? 'listening' :
    'idle';
  const mode =
    PHASE_MODE[voice.phase] ||
    (voice.state === 'starting' ? 'thinking' :
     voice.state === 'reconnecting' ? 'thinking' :
     voice.state === 'degraded' ? 'offline' :
     voice.state === 'active' ? realtimeMode :
     'idle');

  const providerLabel = PROVIDER_LABEL[voice.provider] || voice.provider || 'Voice';
  const statusText =
    (voice.phase === 'error' && (voice.phaseError || 'Voice failed.')) ||
    PHASE_TEXT[voice.phase] ||
    (voice.state === 'starting' ? 'Opening channel…' :
     voice.state === 'active' ? (
       voice.assistantSpeaking ? 'Speaking…' :
       voice.userSpeaking ? 'Hearing you…' :
       'Listening. Speak naturally.'
     ) :
     voice.state === 'reconnecting' ? 'Reconnecting…' :
     voice.state === 'degraded' ? 'Brain socket down, voice paused.' :
     '');

  // Provider-reported and NOT normalised: the brain routes 16 STT
  // backends and only some of them scale to [0, 1] (see
  // `TranscriptPayload.confidence`). So this flags a low score as the
  // provider's own opinion and never renders it as a percentage, which
  // would be a precision the number does not have.
  const lowConfidence =
    typeof voice.transcriptConfidence === 'number'
    && voice.transcriptConfidence > 0
    && voice.transcriptConfidence < 0.6;

  const isFullscreen = variant === 'fullscreen';
  const dialogRef = useRef(null);
  const minimize = useCallback(() => setVariant('docked'), []);

  /*
   * Expand is a real feature: it is the presentation and screen-share
   * layout, and it is only ever reached by clicking Expand. What it was
   * not allowed to be is inescapable.
   *
   * Fullscreen is `inset: 0` at z-index 200 over a scrim, so it covers
   * the dock completely. The dock still computes `pointer-events: auto`
   * underneath, which is why a check of the computed style says the
   * dock is fine while a real click on a tile times out. And the
   * element declared `role="dialog" aria-modal="true"` with no Escape
   * handler and no focus containment, so the two things that attribute
   * promises were both untrue: the page behind was not inert, and there
   * was no keyboard way out of a surface covering the whole viewport.
   *
   * Escape now minimizes back to the docked pill, which is what returns
   * the dock, and the shared trap from ui/useFocusTrap keeps Tab inside
   * the dialog for as long as it claims to be modal. Scroll stays
   * unlocked: the page behind is covered, not replaced, and locking it
   * makes minimizing jump the reader's position.
   */
  useFocusTrap(isFullscreen && visible, () => dialogRef.current, {
    lockScroll: false,
    focusOnOpen: 'container',
  });

  useEffect(() => {
    if (!isFullscreen || !visible) return undefined;
    const onKey = (e) => {
      if (e.key === 'Escape') { e.preventDefault(); minimize(); }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [isFullscreen, visible, minimize]);

  return (
    <div
      ref={dialogRef}
      // -1 so the trap can put focus on the container to announce it,
      // without adding a Tab stop when the overlay is only a pill.
      tabIndex={isFullscreen ? -1 : undefined}
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
      {voice.configAck && voice.configAck.status
        && voice.configAck.status !== 'ok' && (
        <Glass level={1} radius="md" padding="sm" className="v2-voice-status-banner">
          <ConfigAckNotice ack={voice.configAck} />
        </Glass>
      )}
      {voice.transcript && isFullscreen && (
        <Glass level={1} radius="md" padding="md" className="v2-voice-transcript">
          <span
            data-partial={voice.transcriptPartial ? 'true' : 'false'}
            className={
              voice.transcriptPartial ? 'v2-voice-caption is-partial' : 'v2-voice-caption'
            }
          >
            {voice.transcript}
          </span>
          {lowConfidence && (
            <span className="v2-voice-caption-note">
              Low confidence, as reported by the transcriber.
            </span>
          )}
        </Glass>
      )}
      <Glass level={2} radius="pill" padding="sm" className="v2-voice-endbar">
        <button
          type="button"
          className="v2-btn"
          aria-label={isFullscreen ? 'Minimize voice' : 'Expand voice'}
          onClick={() => (isFullscreen ? minimize() : setVariant('fullscreen'))}
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
