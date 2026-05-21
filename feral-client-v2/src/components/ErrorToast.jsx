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
      role="alert"
      aria-live="polite"
      aria-atomic="false"
      data-testid="error-toast-stack"
      style={{
        position: 'fixed',
        top: 72,
        right: 20,
        maxWidth: 420,
        minWidth: 260,
        zIndex: 65,
        display: 'flex',
        flexDirection: 'column',
        gap: 8,
      }}
    >
      {errors.slice(0, 5).map((entry) => (
        <ErrorCard key={entry.id} entry={entry} onDismiss={clear} />
      ))}
    </div>
  );
}
