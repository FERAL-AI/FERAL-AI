import React from 'react';
import { Mic, MicOff, Square } from 'lucide-react';
import { useRegisterVoiceLane } from '../shell/VoiceContext';

/**
 * Voice as a lane in the composer, not a takeover.
 *
 * The approved design puts voice inside the composer row: the text field
 * is replaced in place by a pill bordered in the run colour, carrying a
 * level meter and its own controls. Its note is explicit about the
 * controls being distinct: "The mic starts voice; mute and end are
 * separate."
 *
 * What shipped instead was a fixed overlay at z-index 200 with a
 * fullscreen variant that dimmed the page to brightness(0.4) and made
 * the dock non-interactive. So starting voice took the machine away from
 * you at exactly the moment you might want to watch it, which is the
 * opposite of the design's whole argument.
 *
 * The three controls are deliberately not one button. A single toggle
 * has to mean both "stop listening for a moment" and "end the session",
 * and those are different intentions with different costs: muting is
 * recoverable, ending is not.
 */

/** Five bars, filled from a 0..1 level. Purely a readout. */
export function meterBars(level, count = 5) {
  const v = Number.isFinite(Number(level)) ? Math.min(1, Math.max(0, Number(level))) : 0;
  return Array.from({ length: count }, (_, i) => v >= (i + 1) / count);
}

/** What the pill says, which is never just "on". */
export function laneLabel({ phase, muted, state }) {
  if (state === 'degraded') return 'Voice paused';
  if (muted) return 'Muted';
  if (phase === 'processing') return 'Thinking';
  if (phase === 'speaking') return 'Speaking';
  if (phase === 'listening') return 'Listening';
  if (state === 'starting') return 'Opening';
  return 'Listening';
}

export default function VoiceLane({ voice, level = 0, muted = false, onMute, onEnd }) {
  const label = laneLabel({ phase: voice?.phase, muted, state: voice?.state });
  const bars = meterBars(level);

  // Tell the shell a lane is on screen, so the docked overlay does not
  // put a second "End voice" next to this one. Registered from here
  // rather than inferred from the route, because this component is the
  // only thing that knows whether a lane exists, and reading the URL
  // instead coupled the overlay to a Router it does not need.
  useRegisterVoiceLane(true);

  return (
    <div
      className={`v2-vlane${muted ? ' is-muted' : ''}`}
      role="status"
      aria-live="polite"
      aria-label={`Voice: ${label}`}
    >
      <span className="v2-vlane-meter" aria-hidden="true">
        {bars.map((on, i) => (
          <i key={i} data-on={on ? 'yes' : 'no'} />
        ))}
      </span>

      <span className="v2-vlane-text">
        {label}
        {voice?.transcript ? <em className="v2-vlane-caption">{voice.transcript}</em> : null}
      </span>

      {voice?.phaseError && <span className="v2-vlane-err">{voice.phaseError}</span>}

      <button
        type="button"
        className="v2-vlane-btn"
        onClick={onMute}
        aria-pressed={muted}
        title={muted ? 'Unmute the microphone' : 'Mute the microphone'}
        aria-label={muted ? 'Unmute the microphone' : 'Mute the microphone'}
      >
        {muted ? <MicOff size={14} aria-hidden="true" /> : <Mic size={14} aria-hidden="true" />}
      </button>
      <button
        type="button"
        className="v2-vlane-btn v2-vlane-btn--end"
        onClick={onEnd}
        title="End the voice session"
        aria-label="End the voice session"
      >
        <Square size={12} aria-hidden="true" />
      </button>
    </div>
  );
}
