import React from 'react';
import { AlertTriangle, CloudOff, Lock, Server, ShieldAlert } from 'lucide-react';

/**
 * ErrorState: the sibling of EmptyState for "we could not ask".
 *
 * EmptyState says: we asked the brain and it has nothing for you.
 * ErrorState says: we never got an answer, so we do not know.
 *
 * Those two claims used to render identically across this client,
 * which turned a dropped request into an affirmative "no anomalies
 * detected". They must never look the same again, so this component
 * deliberately carries a fixed note line saying so.
 *
 * Same API shape as EmptyState (icon / title / hint / action) plus an
 * `error` it can describe. `ApiError` already carries `status`, `code`
 * and `reason` (lib/api.js) and nothing in the client read any of them
 * before this; they drive both the wording and the meta row.
 *
 * Props:
 *   error     the ApiError from useResource. Anything with
 *             status/code/reason/detail works.
 *   what      what we failed to load, as a noun phrase that reads after
 *             "could not load ___". Keep it concrete: "health alerts",
 *             not "data".
 *   title     override the derived headline.
 *   hint      an ADDITIONAL context line. It does not replace the
 *             derived diagnosis, which is where the 401-vs-502
 *             difference lives.
 *   onRetry   renders a Retry button (pass useResource's `refresh`).
 *   compact   inline row instead of a full centred block.
 */

const NOTE = 'This is a failed request, not an empty result.';

/**
 * Classify an ApiError into something a human can act on.
 * Exported so tests and other surfaces can assert the mapping without
 * rendering.
 */
export function describeApiError(error, what = 'this') {
  const status = Number(error?.status ?? 0) || 0;
  const code = error?.code || '';
  const reason = error?.reason || '';
  const detail = error?.detail || error?.message || '';
  const path = error?.path || '';

  if (status === 401) {
    return {
      kind: 'auth',
      tone: 'warn',
      title: `Not signed in, so ${what} could not be loaded`,
      hint: 'The brain answered 401 unauthorized. Add or refresh the API key in Settings, then retry.',
      status, code, reason, detail, path,
    };
  }
  if (status === 403) {
    return {
      kind: 'forbidden',
      tone: 'warn',
      title: `Not permitted to load ${what}`,
      hint: 'The brain answered 403 forbidden. This key is authenticated but is not allowed to read that endpoint.',
      status, code, reason, detail, path,
    };
  }
  if (status === 404) {
    return {
      kind: 'missing',
      tone: 'warn',
      title: `This brain build does not serve ${what}`,
      hint: `The client asked for a route the brain answered 404 for${path ? ` (${path})` : ''}. Nothing is wrong with your data; the client and brain versions disagree.`,
      status, code, reason, detail, path,
    };
  }
  if (status === 0) {
    return {
      kind: 'offline',
      tone: 'error',
      title: `Could not reach the brain to load ${what}`,
      hint: `The request never got a response${detail ? `: ${detail}` : '.'} Check that the brain is running and reachable, then retry.`,
      status, code, reason, detail, path,
    };
  }
  if (status === 429) {
    return {
      kind: 'busy',
      tone: 'warn',
      title: `The brain is rate limiting ${what}`,
      hint: 'The brain answered 429 too many requests. Wait a moment, then retry.',
      status, code, reason, detail, path,
    };
  }
  if (status >= 500) {
    return {
      kind: 'server',
      tone: 'error',
      title: `The brain failed while loading ${what}`,
      hint: `The brain answered ${status}${detail ? `: ${detail}` : '.'} This is a fault inside the brain, not a change in your data. Check the brain log, then retry.`,
      status, code, reason, detail, path,
    };
  }
  return {
    kind: 'request',
    tone: 'error',
    title: `Could not load ${what}`,
    hint: `The brain answered ${status}${detail ? `: ${detail}` : '.'} Retry, and check the brain log if it persists.`,
    status, code, reason, detail, path,
  };
}

const KIND_ICON = {
  auth: Lock,
  forbidden: ShieldAlert,
  missing: AlertTriangle,
  offline: CloudOff,
  busy: AlertTriangle,
  server: Server,
  request: AlertTriangle,
};

export default function ErrorState({
  error,
  what = 'this',
  title,
  hint,
  icon,
  action,
  onRetry,
  compact = false,
  testId = 'v2-error-state',
}) {
  const info = describeApiError(error, what);
  const Icon = KIND_ICON[info.kind] || AlertTriangle;
  const meta = [
    info.status ? `HTTP ${info.status}` : null,
    info.code || null,
    info.reason || null,
  ].filter(Boolean);

  return (
    <div
      className={`v2-error-state${compact ? ' v2-error-state--compact' : ''}`}
      data-tone={info.tone}
      data-kind={info.kind}
      data-testid={testId}
      role="alert"
    >
      <div className="v2-error-state-icon" aria-hidden="true">
        {icon || <Icon size={compact ? 16 : 22} />}
      </div>
      <div className="v2-error-state-title">{title || info.title}</div>
      {/* `hint` is the caller's context line and is ADDITIVE. It never
          replaces the derived diagnosis, because the diagnosis is what
          carries the actionable difference between a 401 and a 502 and
          a page-level hint would otherwise swallow it. */}
      {hint && <div className="v2-error-state-hint">{hint}</div>}
      <div className="v2-error-state-hint v2-error-state-diagnosis">{info.hint}</div>
      <div className="v2-error-state-note">{NOTE}</div>
      {meta.length > 0 && (
        <div className="v2-error-state-meta">
          {meta.map((m) => (
            <span key={m} className="v2-chip v2-chip--muted">{m}</span>
          ))}
        </div>
      )}
      {(action || onRetry) && (
        <div className="v2-error-state-action">
          {action}
          {onRetry && (
            <button type="button" className="v2-btn" onClick={() => onRetry()}>
              Retry
            </button>
          )}
        </div>
      )}
    </div>
  );
}
