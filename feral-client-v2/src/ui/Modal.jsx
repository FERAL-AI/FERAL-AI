import React, { useEffect, useRef } from 'react';
import { createPortal } from 'react-dom';
import Glass from './Glass';

/**
 * Everything a browser will land on with Tab. Deliberately not filtered by
 * visibility: jsdom reports `offsetParent === null` for every element, so a
 * visibility filter would make the trap look empty under test while behaving
 * correctly in a browser. Disabled controls are already excluded by selector.
 */
const FOCUSABLE = [
  'a[href]',
  'area[href]',
  'button:not([disabled])',
  'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  'iframe',
  'audio[controls]',
  'video[controls]',
  '[contenteditable]:not([contenteditable="false"])',
  '[tabindex]:not([tabindex="-1"])',
].join(', ');

/**
 * Modal — translucent overlay sheet. Closes on ESC or backdrop click
 * (unless ``dismissible`` is false). Never uses inline styles.
 *
 * Focus management. The dialog declares ``aria-modal="true"``, which is a
 * promise that the rest of the page is inert while it is open. It used to
 * handle Escape and nothing else: focus was never moved into the dialog, Tab
 * walked straight out into the page behind it, and closing left focus on
 * <body> rather than on the control that opened it. All three are handled
 * below; ``aria-modal`` is now a description of the behaviour instead of a
 * claim about it.
 *
 * Mount strategy. The modal renders via React Portal onto document.body
 * so it is NOT a descendant of .v2-shell-main. .v2-shell-main creates
 * its own stacking context (positive z-index above .v2-ambient), which
 * historically trapped the modal below the dock + menubar even though
 * its CSS z-index value was higher. The portal puts the backdrop in
 * the body's stacking context where the named `--z-modal` constant
 * (see styles/_z.css) places it cleanly above the dock (--z-dock).
 *
 * Roadmap §A.2: this fixes the user-reported "click 'Pair a device' →
 * row appears in the historical list but the modal never becomes
 * visible" bug by making the modal actually paint on top.
 */
export default function Modal({
  open,
  onClose,
  title,
  children,
  actions,
  dismissible = true,
  size = 'md',
}) {
  const backdropRef = useRef(null);
  const getDialog = () => backdropRef.current?.querySelector('[role="dialog"]') || null;

  /*
   * Body scroll lock, initial focus, and focus restore. Keyed on `open`
   * alone: the previous single effect also depended on `onClose`, so a
   * parent that passes an inline arrow (most of them do) re-ran the whole
   * body on every render. That was harmless when the effect only bound a
   * key listener; with focus restore in the teardown it would have yanked
   * focus back to the trigger mid-session.
   */
  useEffect(() => {
    if (!open) return undefined;
    if (typeof document === 'undefined') return undefined;

    const previouslyFocused = document.activeElement;
    document.body.style.overflow = 'hidden';

    // Focus the dialog container rather than its first control. Focusing
    // the first control would land on whatever happens to be first, which
    // in several of these dialogs is a destructive action; the container
    // announces the dialog's name and Tab proceeds into the content.
    const dialog = getDialog();
    dialog?.focus?.();

    return () => {
      document.body.style.overflow = '';
      // Restore to the trigger so the keyboard user resumes where they
      // left off instead of at the top of the document. Skipped when the
      // trigger has been unmounted by whatever the dialog did, since
      // focusing a detached node silently sends focus to <body>.
      if (
        previouslyFocused
        && typeof previouslyFocused.focus === 'function'
        && previouslyFocused.isConnected !== false
      ) {
        previouslyFocused.focus();
      }
    };
  }, [open]);

  /*
   * Escape to close, plus the Tab cycle. Without the Tab half, `aria-modal`
   * is a claim the markup does not honour: focus walks straight out of the
   * dialog into the page behind it, which is still fully interactive.
   */
  useEffect(() => {
    if (!open) return undefined;
    const onKey = (e) => {
      if (e.key === 'Escape' && dismissible) {
        onClose?.();
        return;
      }
      if (e.key !== 'Tab') return;
      const dialog = getDialog();
      if (!dialog) return;

      const items = Array.from(dialog.querySelectorAll(FOCUSABLE))
        .filter((el) => el.getAttribute('aria-hidden') !== 'true');
      const active = document.activeElement;

      if (items.length === 0) {
        // Nothing tabbable inside: keep focus pinned on the container so
        // Tab cannot walk out into the page underneath.
        e.preventDefault();
        dialog.focus();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];

      if (!dialog.contains(active)) {
        e.preventDefault();
        (e.shiftKey ? last : first).focus();
        return;
      }
      // Focus is on the container itself (the initial position). Forward
      // Tab is fine, the browser walks into the content. Backward Tab is
      // not: the dialog is portaled to the end of <body>, so shift+Tab
      // would land on the page behind it.
      if (active === dialog) {
        if (e.shiftKey) {
          e.preventDefault();
          last.focus();
        }
        return;
      }
      if (e.shiftKey && active === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && active === last) {
        e.preventDefault();
        first.focus();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, dismissible, onClose]);

  if (!open) return null;
  if (typeof document === 'undefined') return null;

  const node = (
    <div
      ref={backdropRef}
      className="v2-modal-backdrop"
      role="presentation"
      data-testid="v2-modal-backdrop"
      onClick={(e) => {
        if (e.target === e.currentTarget && dismissible) onClose?.();
      }}
    >
      <Glass
        as="div"
        level="elev"
        radius="lg"
        padding="none"
        className={`v2-modal v2-modal-card v2-modal--${size}`}
        role="dialog"
        aria-modal="true"
        aria-label={title || 'Dialog'}
        /* -1 so the container is programmatically focusable for the
           initial focus move without joining the Tab order itself. */
        tabIndex={-1}
      >
        {title && (
          <header className="v2-modal-header">
            <h2 className="v2-modal-title">{title}</h2>
            {dismissible && (
              <button
                type="button"
                className="v2-btn v2-btn--ghost v2-modal-close"
                onClick={() => onClose?.()}
                aria-label="Close"
              >
                ×
              </button>
            )}
          </header>
        )}
        <div className="v2-modal-body">{children}</div>
        {actions && <footer className="v2-modal-footer">{actions}</footer>}
      </Glass>
    </div>
  );

  return createPortal(node, document.body);
}
