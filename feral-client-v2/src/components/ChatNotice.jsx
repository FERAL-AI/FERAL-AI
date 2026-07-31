/**
 * ChatNotice is the visible failure state for a chat turn.
 *
 * The maintainer's #1 complaint was silent failure: an `error` or
 * `refusal` frame arrived, the spinner kept spinning (or the row just
 * never appeared) and the user was left staring at nothing. Chat.jsx
 * now turns every such frame into one of these rows, so a turn always
 * ends in either an answer or a legible explanation.
 *
 * Kinds:
 *   error    = ErrorPayload {code, message, recoverable}
 *   refusal  = RefusalPayload {reason, retry_hint, source}
 *   stalled  = client-side; the turn ended with nothing to render
 *
 * Never a bare spinner, never a blank bubble.
 */
import React from 'react';
import { AlertTriangle, ShieldAlert, RotateCw } from 'lucide-react';
import Glass from '../ui/Glass';

const KINDS = {
  error: {
    icon: AlertTriangle,
    title: 'That turn failed',
    className: 'v2-chat-notice--error',
  },
  refusal: {
    icon: ShieldAlert,
    title: 'FERAL declined this request',
    className: 'v2-chat-notice--refusal',
  },
  stalled: {
    icon: AlertTriangle,
    title: 'The turn ended without a reply',
    className: 'v2-chat-notice--stalled',
  },
};

export default function ChatNotice({
  kind = 'error',
  title,
  message,
  detail,
  code,
  hint,
  onRetry,
}) {
  const spec = KINDS[kind] || KINDS.error;
  const Icon = spec.icon;
  return (
    <Glass
      level={0}
      radius="md"
      padding="sm"
      className={`v2-chat-notice ${spec.className}`}
      role="alert"
      data-testid="chat-notice"
      data-kind={kind}
    >
      <Icon size={15} aria-hidden="true" className="v2-chat-notice__icon" />
      <div className="v2-chat-notice__body">
        <div className="v2-chat-notice__title">
          {title || spec.title}
          {code && <span className="v2-chat-notice__code">{code}</span>}
        </div>
        {message && <div className="v2-chat-notice__msg">{message}</div>}
        {detail && <pre className="v2-chat-notice__detail">{detail}</pre>}
        {hint && <div className="v2-chat-notice__hint">{hint}</div>}
      </div>
      {onRetry && (
        <button
          type="button"
          className="v2-btn v2-chat-notice__retry"
          onClick={onRetry}
        >
          <RotateCw size={12} aria-hidden="true" /> Retry
        </button>
      )}
    </Glass>
  );
}
