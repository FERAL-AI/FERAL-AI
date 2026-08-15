import React, { useCallback, useEffect, useRef, useState } from 'react';
import { AlertTriangle, Check, Copy } from 'lucide-react';

/**
 * CopyButton is the single copy affordance in this client: chat surface
 * (message body, fenced code block, tool args/result pane), the pairing
 * modal, and the publish wizard's CLI blocks.
 *
 * Icon-only by default so it adds no noise to the reading column and
 * no stray text to `textContent` assertions; the accessible name comes
 * from `aria-label`. Confirmation is a 1.6s icon swap plus a polite
 * live region, so screen readers hear "Copied" without a toast.
 *
 * WHY THIS COMPONENT EXISTS RATHER THAN A LOCAL `useCopy` HOOK:
 * `navigator.clipboard.writeText` returns a Promise and REJECTS on an
 * insecure origin (plain http on a LAN address, which is exactly how
 * this brain is reached from another machine), when the document is
 * not focused, and when the permission is denied. Two files each had
 * their own copy:
 *
 *     try { navigator.clipboard.writeText(text); setCopied(id); }
 *     catch { }
 *
 * The `try` is synchronous, so the rejection lands outside it: the
 * catch never runs, the checkmark always appears, and the user is told
 * a pairing token is on their clipboard when nothing was written. That
 * token is shown exactly once. Awaiting the promise, and only then
 * confirming, is the whole fix.
 *
 * A failed copy is reported, not swallowed. Silently doing nothing on
 * click is its own small lie: the user cannot tell a dead button from
 * a successful copy that did not redraw.
 *
 * `navigator.clipboard` is absent in jsdom and on non-secure origins,
 * so we fall back to a hidden `<textarea>` + `document.execCommand`
 * before giving up.
 */

const RESET_MS = 1600;

// Announcement region: present for assistive tech, invisible and
// zero-size for everyone else. Inline because src/styles/ is not this
// component's to extend.
const SR_ONLY = {
  position: 'absolute',
  width: 1,
  height: 1,
  padding: 0,
  margin: -1,
  overflow: 'hidden',
  clip: 'rect(0 0 0 0)',
  whiteSpace: 'nowrap',
  border: 0,
};

/**
 * Write `payload` to the clipboard. Resolves true only when a write
 * actually happened. Exported so a caller that needs the outcome
 * without a button (or a test) can reuse the same logic.
 */
export async function writeToClipboard(payload) {
  if (!payload) return false;
  try {
    if (navigator?.clipboard?.writeText) {
      // The await is load-bearing: without it a rejected promise
      // escapes this try block entirely.
      await navigator.clipboard.writeText(payload);
      return true;
    }
  } catch {
    // Insecure context / denied permission / unfocused document.
    // Fall through to the execCommand path rather than reporting
    // success we did not earn.
  }
  if (typeof document !== 'undefined' && typeof document.execCommand === 'function') {
    let ta = null;
    try {
      ta = document.createElement('textarea');
      ta.value = payload;
      ta.setAttribute('readonly', '');
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      return document.execCommand('copy') === true;
    } catch {
      return false;
    } finally {
      if (ta && ta.parentNode) ta.parentNode.removeChild(ta);
    }
  }
  return false;
}

export default function CopyButton({
  value,
  label = 'Copy',
  copiedLabel = 'Copied',
  failedLabel = 'Copy failed',
  className = '',
  size = 13,
  withText = false,
  onResult,
  testId = 'copy-button',
}) {
  // 'idle' | 'copied' | 'failed'. There is no optimistic state: the
  // button never claims anything before the write has settled.
  const [status, setStatus] = useState('idle');
  const timerRef = useRef(null);
  const aliveRef = useRef(true);

  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, []);

  const onCopy = useCallback(async () => {
    const text = typeof value === 'function' ? value() : value;
    const payload = typeof text === 'string' ? text : String(text ?? '');
    if (!payload) return;
    const ok = await writeToClipboard(payload);
    if (!aliveRef.current) return;
    setStatus(ok ? 'copied' : 'failed');
    onResult?.(ok);
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => {
      if (aliveRef.current) setStatus('idle');
    }, RESET_MS);
  }, [value, onResult]);

  const copied = status === 'copied';
  const failed = status === 'failed';
  const name = copied ? copiedLabel : (failed ? failedLabel : label);

  return (
    <button
      type="button"
      className={`v2-copy-btn${copied ? ' is-copied' : ''}${failed ? ' is-failed' : ''}${className ? ` ${className}` : ''}`}
      onClick={onCopy}
      aria-label={name}
      title={name}
      data-state={status}
      data-testid={testId}
      style={failed ? { color: 'var(--v2-state-error)' } : undefined}
    >
      {copied && <Check size={size} aria-hidden="true" />}
      {failed && <AlertTriangle size={size} aria-hidden="true" />}
      {!copied && !failed && <Copy size={size} aria-hidden="true" />}
      {/* Exactly one live region, and never a duplicate of the visible
          label: with `withText` the visible span IS the announcement,
          otherwise an off-screen one carries it. */}
      {withText ? (
        <span className="v2-copy-btn__text" role="status" aria-live="polite">{name}</span>
      ) : (
        <span role="status" aria-live="polite" style={SR_ONLY}>
          {status === 'idle' ? '' : name}
        </span>
      )}
    </button>
  );
}
