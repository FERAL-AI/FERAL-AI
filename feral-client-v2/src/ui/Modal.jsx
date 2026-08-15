import React, { useEffect, useRef } from 'react';
import { createPortal } from 'react-dom';
import Glass from './Glass';
import useFocusTrap from './useFocusTrap';

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

  // Focus containment lives in one place so this dialog and the Hub
  // launcher cannot drift apart. See ui/useFocusTrap.js.
  useFocusTrap(open, getDialog);

  // Escape stays here: it is this component's own dismissal policy, not
  // part of trapping focus, and `dismissible` gates it.
  useEffect(() => {
    if (!open || !dismissible) return undefined;
    const onKey = (e) => {
      if (e.key === 'Escape') onClose?.();
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
