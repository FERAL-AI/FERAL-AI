import React from 'react';

/**
 * StatusDot: small state indicator. Tone maps to one of the semantic state
 * tokens; never invents a color.
 *
 * Two accessibility rules this component now enforces, both of which it used
 * to break:
 *
 * 1. Hue is never the only channel. `.v2-dot--live/--warn/--error/--neutral`
 *    used to be four identical 8px filled circles that differed only in hue,
 *    so green-vs-red was indistinguishable to a deuteranope (~1 in 12 men).
 *    ui.css now gives every tone a distinct silhouette as well (circle /
 *    triangle / diamond / rounded square / ring) inside the same 8x8 box, so
 *    no layout moves and the state survives a monochrome render.
 *
 * 2. The dot always has an accessible name. It used to fall back to
 *    `role="presentation"` when no `label` was passed, which silently deleted
 *    the state from assistive tech at roughly 20 of ~35 call sites. There is
 *    now a tone-derived fallback name so a missing label degrades to a coarse
 *    name rather than to nothing, plus a dev-only console warning so the
 *    omission is visible to whoever added it. It is a warning and not a throw
 *    because a missing caption must never blank a page at runtime.
 *
 * `role="img"` rather than `role="status"`: `status` is an ARIA live region,
 * and ~35 live regions on one screen produce announcement storms while
 * conveying nothing extra (a live region announces on content change, and
 * this element's content is always empty, so only its aria-label changes, and
 * that is not reliably announced). `img` + `aria-label` is the standard for
 * a meaningful icon and is announced on focus/traversal.
 */
const TONE_CLASS = {
  live: 'v2-dot--live',
  warn: 'v2-dot--warn',
  error: 'v2-dot--error',
  neutral: 'v2-dot--neutral',
  off: 'v2-dot--off',
};

/**
 * Coarse fallback names. Deliberately generic: they are a floor, not a
 * substitute for a call-site label that names *what* is live.
 */
const TONE_FALLBACK_LABEL = {
  live: 'Live',
  warn: 'Warning',
  error: 'Error',
  neutral: 'Status unknown',
  off: 'Offline',
};

/** One warning per tone per session, so a list of 40 rows logs once. */
const warnedTones = new Set();

function warnMissingLabel(tone) {
  const isDev =
    typeof import.meta !== 'undefined' && import.meta.env
      ? !!import.meta.env.DEV
      : false;
  if (!isDev || warnedTones.has(tone)) return;
  warnedTones.add(tone);
  // eslint-disable-next-line no-console
  console.warn(
    `[StatusDot] rendered tone="${tone}" without a \`label\`. The dot carries ` +
      'real state, so it needs an accessible name describing what is in that ' +
      `state (e.g. label="Brain connected"). Falling back to ` +
      `"${TONE_FALLBACK_LABEL[tone]}", which is better than silence but is ` +
      'not a caption.',
  );
}

/** Test seam: lets a test assert the warning fires more than once. */
export function __resetStatusDotWarnings() {
  warnedTones.clear();
}

export default function StatusDot({ tone = 'neutral', pulse = false, label, className = '' }) {
  const key = TONE_CLASS[tone] ? tone : 'neutral';
  if (!label) warnMissingLabel(key);
  const name = label || TONE_FALLBACK_LABEL[key];
  const cls = [
    'v2-dot',
    TONE_CLASS[key],
    pulse ? 'is-pulse' : '',
    className,
  ].filter(Boolean).join(' ');
  return (
    <span
      className={cls}
      role="img"
      aria-label={name}
      data-tone={key}
    />
  );
}
