import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Check, Copy } from 'lucide-react';

/**
 * CopyButton is the single copy affordance for the chat surface
 * (message body, fenced code block, tool args/result pane).
 *
 * Icon-only by default so it adds no noise to the reading column and
 * no stray text to `textContent` assertions; the accessible name comes
 * from `aria-label`. Confirmation is a 1.6s icon swap plus a polite
 * live region, so screen readers hear "Copied" without a toast.
 *
 * `navigator.clipboard` is absent in jsdom and on non-secure origins,
 * so we fall back to a hidden `<textarea>` + `document.execCommand`
 * and, failing that, simply do nothing visible rather than throwing
 * inside a render tree.
 */
export default function CopyButton({
  value,
  label = 'Copy',
  copiedLabel = 'Copied',
  className = '',
  size = 13,
  withText = false,
}) {
  const [copied, setCopied] = useState(false);
  const timerRef = useRef(null);

  useEffect(() => () => {
    if (timerRef.current) clearTimeout(timerRef.current);
  }, []);

  const onCopy = useCallback(async () => {
    const text = typeof value === 'function' ? value() : value;
    const payload = typeof text === 'string' ? text : String(text ?? '');
    if (!payload) return;
    let ok = false;
    try {
      if (navigator?.clipboard?.writeText) {
        await navigator.clipboard.writeText(payload);
        ok = true;
      }
    } catch {
      ok = false;
    }
    if (!ok && typeof document !== 'undefined' && document.execCommand) {
      try {
        const ta = document.createElement('textarea');
        ta.value = payload;
        ta.setAttribute('readonly', '');
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        ok = document.execCommand('copy');
        document.body.removeChild(ta);
      } catch {
        ok = false;
      }
    }
    if (!ok) return;
    setCopied(true);
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => setCopied(false), 1600);
  }, [value]);

  return (
    <button
      type="button"
      className={`v2-copy-btn${copied ? ' is-copied' : ''}${className ? ` ${className}` : ''}`}
      onClick={onCopy}
      aria-label={copied ? copiedLabel : label}
      title={copied ? copiedLabel : label}
      data-testid="copy-button"
    >
      {copied
        ? <Check size={size} aria-hidden="true" />
        : <Copy size={size} aria-hidden="true" />}
      {withText && <span className="v2-copy-btn__text">{copied ? copiedLabel : label}</span>}
    </button>
  );
}
