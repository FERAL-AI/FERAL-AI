/**
 * ErrorToast — global stack of API / WebSocket errors from useGlobalErrors.
 * Mounted once at Shell root; auto-dismiss after 6s; click to dismiss.
 */
import React, { useEffect } from 'react';
import { X, AlertCircle } from 'lucide-react';
import Glass from '../ui/Glass';
import { useGlobalErrors } from '../hooks/useGlobalErrors';

const DISMISS_MS = 6000;

function ErrorCard({ entry, onDismiss }) {
  const { id, message, err } = entry;
  const status = err?.status;
  const path = err?.path;

  useEffect(() => {
    const t = setTimeout(() => onDismiss(id), DISMISS_MS);
    return () => clearTimeout(t);
  }, [id, onDismiss]);

  return (
    <Glass level={2} radius="md" padding="md" className="v2-error-toast-card">
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
        <AlertCircle size={14} aria-hidden style={{ color: 'var(--v2-state-error)', flexShrink: 0 }} />
        <strong style={{ fontSize: 13, flex: 1 }}>{message}</strong>
        <button
          type="button"
          className="v2-btn v2-btn--ghost"
          onClick={() => onDismiss(id)}
          aria-label="Dismiss error"
          // The stack is pointer-transparent so it cannot cover a page
          // control. This one control turns that back on for itself,
          // so the toast is still dismissable while the button it
          // happens to be sitting over stays clickable.
          style={{ pointerEvents: 'auto' }}
        >
          <X size={13} />
        </button>
      </div>
      {(path || status) ? (
        <div style={{ fontSize: 11, opacity: 0.65, display: 'flex', gap: 8 }} aria-hidden>
          {path ? <span>{path}</span> : null}
          {status ? <span>{status}</span> : null}
        </div>
      ) : null}
    </Glass>
  );
}

export default function ErrorToast() {
  const { errors, clear } = useGlobalErrors();
  if (!errors.length) return null;

  return (
    <div
      className="v2-error-toast-stack"
      /*
       * role="alert" and aria-live="polite" were both set here, which is a
       * contradiction: `alert` carries an implicit aria-live of `assertive`,
       * so the explicit `polite` fought its own role and the announcement
       * behaviour came down to which one a given screen reader resolved
       * last. These are API and WebSocket failures, i.e. the user's action
       * did not happen, so assertive is the correct reading and it is now
       * stated rather than left to chance.
       *
       * aria-atomic stays false deliberately: this node is a stack, and an
       * atomic region would re-read every visible error each time one more
       * arrived.
       */
      role="alert"
      aria-live="assertive"
      aria-atomic="false"
      data-testid="error-toast-stack"
      style={{
        position: 'fixed',
        // Below the page action row, which ends at y=103 at this scale.
        // pointerEvents alone was not enough: it protects the card body,
        // but the dismiss button has to take clicks to be dismissable,
        // and at top:72 that one re-enabled element landed exactly on
        // "Refresh" at (1182,71). Measured after the first fix: Refresh
        // blocked by `v2-btn v2-btn--ghost`, which is the dismiss button
        // itself. So it moves clear of the row AND stays transparent.
        top: 112,
        right: 20,
        maxWidth: 420,
        minWidth: 260,
        zIndex: 65,
        display: 'flex',
        flexDirection: 'column',
        gap: 8,
        // A toast must never intercept a click. This one is pinned at
        // top:72 right:20, which is exactly where a page puts its own
        // action row: measured on /oversight at 1280x720, the stack
        // covered "Pause actions" at (1045,71) and "Refresh" at
        // (1182,71) for its full six-second life. "Pause actions" is
        // the supervisor kill switch, so the message telling you
        // something went wrong sat on top of the button that stops it.
        //
        // Moving it is not the fix: the opposite corner is already
        // taken by ProactiveToast at right:20 bottom:130, so relocating
        // trades one collision for another, and any fixed corner
        // eventually collides with something. Being transparent to the
        // pointer holds wherever it is put. Each card turns pointer
        // events back on for itself so its own dismiss button still
        // works, and the card body deliberately does not, so whatever
        // is underneath stays clickable through it.
        pointerEvents: 'none',
      }}
    >
      {errors.slice(0, 5).map((entry) => (
        <ErrorCard key={entry.id} entry={entry} onDismiss={clear} />
      ))}
    </div>
  );
}
