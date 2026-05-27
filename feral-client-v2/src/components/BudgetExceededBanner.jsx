/**
 * BudgetExceededBanner — inline yellow banner the chat renders when the
 * brain emits a `budget_exceeded` WS frame from Lane 08.
 *
 * Frame contract (Lane 08, locked 2026-05-22):
 *   {
 *     type: "budget_exceeded",
 *     payload: {
 *       call_site: "chat" | "vision" | "embedding" | "routing" | string,
 *       cap_dollars: number,
 *       current_dollars: number,
 *       reset_at: number (unix epoch seconds) | string (HH:MM)
 *     }
 *   }
 *
 * Banner copy (locked per parent ack):
 *   "Chat budget reached ($X.XX / hour). Resets at HH:MM. Adjust caps in Settings."
 * The "Adjust caps in Settings" portion is a router link to
 *   /settings?section=Cost
 * which the Settings page consumes to land on the Cost panel.
 */
import React from 'react';
import { Link } from 'react-router-dom';
import { AlertTriangle, X } from 'lucide-react';

function formatDollars(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return '$0.00';
  return `$${n.toFixed(2)}`;
}

function formatResetTime(reset) {
  if (reset == null) return '';
  if (typeof reset === 'string' && /^\d{1,2}:\d{2}/.test(reset)) return reset;
  const epoch = typeof reset === 'string' ? Date.parse(reset) / 1000 : Number(reset);
  if (!Number.isFinite(epoch) || epoch <= 0) return '';
  const d = new Date(epoch * 1000);
  if (Number.isNaN(d.getTime())) return '';
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function pretty(siteId) {
  if (!siteId || typeof siteId !== 'string') return 'Chat';
  // Humanize snake_case call_site ids: "screen_loop" → "Screen loop".
  const spaced = siteId.replace(/_/g, ' ');
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

/**
 * @param {object} props
 * @param {string} props.callSite - identifier from the WS frame
 * @param {number} props.capDollars
 * @param {number} [props.currentDollars]
 * @param {number|string} props.resetAt
 * @param {string} [props.subsystem] - friendly subsystem label from
 *   the broadcast `cost_cap_hit` payload (e.g. "ScreenLoop"). When
 *   present we prefer it over the snake_cased `callSite`.
 * @param {() => void} [props.onDismiss] - if provided, renders an X dismiss button
 */
export default function BudgetExceededBanner({
  callSite,
  capDollars,
  currentDollars,
  resetAt,
  subsystem,
  onDismiss,
}) {
  const label = (typeof subsystem === 'string' && subsystem.trim())
    ? subsystem
    : pretty(callSite);
  const resetStr = formatResetTime(resetAt);
  const capStr = formatDollars(capDollars);
  const currentStr = currentDollars != null ? formatDollars(currentDollars) : null;

  return (
    <div
      className="v2-budget-banner"
      role="status"
      aria-live="polite"
      data-testid="budget-exceeded-banner"
    >
      <AlertTriangle size={16} aria-hidden="true" className="v2-budget-banner__icon" />
      <div className="v2-budget-banner__body">
        <div className="v2-budget-banner__title">
          {label} budget reached
          {currentStr ? <span className="v2-budget-banner__spend"> ({currentStr} / {capStr})</span>
            : <span className="v2-budget-banner__spend"> ({capStr} cap)</span>}
        </div>
        <div className="v2-budget-banner__sub">
          {resetStr ? <>Resets at <strong>{resetStr}</strong>. </> : null}
          <Link
            to={`/settings?section=Cost&call_site=${encodeURIComponent(callSite || '')}`}
            className="v2-budget-banner__link"
          >
            Adjust caps in Settings
          </Link>
        </div>
      </div>
      {onDismiss && (
        <button
          type="button"
          className="v2-btn v2-btn--ghost v2-budget-banner__dismiss"
          onClick={onDismiss}
          aria-label="Dismiss budget banner"
        >
          <X size={13} />
        </button>
      )}
    </div>
  );
}
