import { useEffect } from 'react';

/**
 * Everything a browser will land on with Tab. Deliberately not filtered by
 * visibility: jsdom reports `offsetParent === null` for every element, so a
 * visibility filter would make the trap look empty under test while behaving
 * correctly in a browser. Disabled controls are already excluded by selector.
 */
export const FOCUSABLE = [
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
 * Focus containment for anything declaring `aria-modal="true"`.
 *
 * That attribute is a promise that the rest of the page is inert. Without a
 * Tab cycle it is a claim the markup does not honour: focus walks straight
 * out of the dialog into a page that is still fully interactive, and the
 * user is left tabbing through content a screen reader has been told is not
 * there.
 *
 * Extracted from Modal rather than copied into the second dialog. Two hand
 * written traps drift, and the failure mode when they do is silent: the
 * dialog still looks correct and only keyboard users ever find out.
 *
 * @param open        whether the dialog is currently mounted and visible
 * @param getDialog   () => the element carrying role="dialog", or null
 * @param opts.lockScroll   hide body overflow while open (default true)
 * @param opts.focusOnOpen  'container' focuses the dialog itself, which
 *                          announces its name and leaves Tab to walk into
 *                          the content. 'none' leaves initial focus to the
 *                          caller, for dialogs that put it in a search box.
 */
export default function useFocusTrap(open, getDialog, opts = {}) {
  const { lockScroll = true, focusOnOpen = 'container' } = opts;

  /*
   * Scroll lock, initial focus, and focus restore. Keyed on `open` alone:
   * depending on a caller-supplied close handler re-runs the whole body on
   * every render when the parent passes an inline arrow, and with focus
   * restore in the teardown that yanks focus back to the trigger mid
   * session.
   */
  useEffect(() => {
    if (!open) return undefined;
    if (typeof document === 'undefined') return undefined;

    const previouslyFocused = document.activeElement;
    if (lockScroll) document.body.style.overflow = 'hidden';

    if (focusOnOpen === 'container') {
      getDialog()?.focus?.();
    }

    return () => {
      if (lockScroll) document.body.style.overflow = '';
      // Restore to the trigger so a keyboard user resumes where they left
      // off rather than at the top of the document. Skipped when the trigger
      // has been unmounted by whatever the dialog did, because focusing a
      // detached node silently sends focus to <body>.
      if (
        previouslyFocused
        && typeof previouslyFocused.focus === 'function'
        && previouslyFocused.isConnected !== false
      ) {
        previouslyFocused.focus();
      }
    };
    // getDialog is a stable closure over a ref in both call sites.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, lockScroll, focusOnOpen]);

  useEffect(() => {
    if (!open) return undefined;
    const onKey = (e) => {
      if (e.key !== 'Tab') return;
      const dialog = getDialog();
      if (!dialog) return;

      const items = Array.from(dialog.querySelectorAll(FOCUSABLE))
        .filter((el) => el.getAttribute('aria-hidden') !== 'true');
      const active = document.activeElement;

      if (items.length === 0) {
        // Nothing tabbable inside: pin focus on the container so Tab cannot
        // walk out into the page underneath.
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
      // Focus is on the container itself (the initial position). Forward Tab
      // is fine, the browser walks into the content. Backward Tab is not: a
      // portaled dialog sits at the end of <body>, so shift+Tab would land
      // on the page behind it.
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
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);
}
