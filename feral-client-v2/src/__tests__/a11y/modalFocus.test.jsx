/**
 * Modal: focus containment and focus restore.
 *
 * The dialog declared `aria-modal="true"` while handling Escape and
 * nothing else. Focus was never moved into it, Tab walked straight out
 * into the page behind it (which stayed fully interactive), and on close
 * focus was left on <body> rather than on the control that opened it.
 *
 * jsdom does not implement native Tab traversal, so these tests assert on
 * what the component itself does: it must consume the Tab keydown at the
 * wrap points and move focus. Every assertion below fails against
 * `git show HEAD:src/ui/Modal.jsx`.
 */
import React, { useState } from 'react';
import { describe, it, expect } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/react';
import Modal from '../../ui/Modal';

const TAB = { key: 'Tab', code: 'Tab' };
const SHIFT_TAB = { key: 'Tab', code: 'Tab', shiftKey: true };

function dialog() {
  return document.querySelector('[role="dialog"]');
}
function focusables() {
  return Array.from(dialog().querySelectorAll('button, input, [href], [tabindex]:not([tabindex="-1"])'));
}

function Harness({ initiallyOpen = false } = {}) {
  const [open, setOpen] = useState(initiallyOpen);
  return (
    <div>
      <button type="button" data-testid="outside-before">before</button>
      <button type="button" data-testid="trigger" onClick={() => setOpen(true)}>
        open
      </button>
      <button type="button" data-testid="outside-after">after</button>
      <Modal open={open} onClose={() => setOpen(false)} title="Test dialog">
        <button type="button" data-testid="inner-first">first</button>
        <input data-testid="inner-input" />
        <button type="button" data-testid="inner-last">last</button>
      </Modal>
    </div>
  );
}

describe('Modal initial focus', () => {
  it('moves focus into the dialog when it opens', async () => {
    const { getByTestId } = render(<Harness />);
    const trigger = getByTestId('trigger');
    trigger.focus();
    fireEvent.click(trigger);

    await waitFor(() => {
      expect(dialog()).toBeTruthy();
      expect(dialog().contains(document.activeElement)).toBe(true);
    });
  });

  it('makes the dialog container programmatically focusable without joining the tab order', () => {
    render(<Harness initiallyOpen />);
    expect(dialog().getAttribute('tabindex')).toBe('-1');
  });
});

describe('Modal focus trap', () => {
  it('wraps Tab from the last focusable back to the first', () => {
    render(<Harness initiallyOpen />);
    const items = focusables();
    const first = items[0];
    const last = items[items.length - 1];

    last.focus();
    expect(document.activeElement).toBe(last);

    const handled = fireEvent.keyDown(last, TAB);
    // fireEvent returns false when a listener called preventDefault.
    expect(handled, 'Tab on the last element was not intercepted').toBe(false);
    expect(document.activeElement).toBe(first);
  });

  it('wraps shift+Tab from the first focusable back to the last', () => {
    render(<Harness initiallyOpen />);
    const items = focusables();
    const first = items[0];
    const last = items[items.length - 1];

    first.focus();
    const handled = fireEvent.keyDown(first, SHIFT_TAB);
    expect(handled, 'shift+Tab on the first element was not intercepted').toBe(false);
    expect(document.activeElement).toBe(last);
  });

  it('pulls focus back in when it is somewhere outside the dialog', () => {
    const { getByTestId } = render(<Harness initiallyOpen />);
    const outside = getByTestId('outside-before');
    outside.focus();
    expect(dialog().contains(document.activeElement)).toBe(false);

    fireEvent.keyDown(outside, TAB);
    expect(dialog().contains(document.activeElement)).toBe(true);
  });

  it('does not intercept Tab in the middle of the dialog', () => {
    const { getByTestId } = render(<Harness initiallyOpen />);
    const middle = getByTestId('inner-input');
    middle.focus();
    // Not prevented: the browser's own sequential navigation is correct
    // here and the trap must not fight it.
    expect(fireEvent.keyDown(middle, TAB)).toBe(true);
  });

  it('keeps shift+Tab from escaping backwards off the container', () => {
    render(<Harness initiallyOpen />);
    const d = dialog();
    d.focus();
    expect(document.activeElement).toBe(d);

    const items = focusables();
    fireEvent.keyDown(d, SHIFT_TAB);
    expect(document.activeElement).toBe(items[items.length - 1]);
  });
});

describe('Modal focus restore', () => {
  it('returns focus to the trigger when the dialog closes', async () => {
    const { getByTestId } = render(<Harness />);
    const trigger = getByTestId('trigger');
    trigger.focus();
    fireEvent.click(trigger);

    await waitFor(() => expect(dialog()).toBeTruthy());
    expect(document.activeElement).not.toBe(trigger);

    fireEvent.keyDown(window, { key: 'Escape', code: 'Escape' });

    await waitFor(() => {
      expect(dialog()).toBeNull();
      expect(document.activeElement).toBe(trigger);
    });
  });

  it('returns focus to the trigger when closed via the close button', async () => {
    const { getByTestId, getByLabelText } = render(<Harness />);
    const trigger = getByTestId('trigger');
    trigger.focus();
    fireEvent.click(trigger);

    await waitFor(() => expect(dialog()).toBeTruthy());
    // Without this the assertion below is vacuous: a Modal that never
    // moves focus in the first place leaves it on the trigger throughout
    // and "restores" it by doing nothing.
    expect(dialog().contains(document.activeElement)).toBe(true);
    expect(document.activeElement).not.toBe(trigger);

    fireEvent.click(getByLabelText('Close'));

    await waitFor(() => {
      expect(dialog()).toBeNull();
      expect(document.activeElement).toBe(trigger);
    });
  });

  it('releases the body scroll lock on close', async () => {
    const { getByTestId } = render(<Harness />);
    fireEvent.click(getByTestId('trigger'));
    await waitFor(() => expect(document.body.style.overflow).toBe('hidden'));

    fireEvent.keyDown(window, { key: 'Escape', code: 'Escape' });
    await waitFor(() => expect(document.body.style.overflow).toBe(''));
  });
});

describe('Modal trap with nothing focusable inside', () => {
  it('pins focus on the container rather than letting Tab out', () => {
    render(
      <Modal open onClose={() => {}} dismissible={false}>
        <p>Nothing focusable in here.</p>
      </Modal>,
    );
    const d = dialog();
    d.focus();
    const handled = fireEvent.keyDown(d, TAB);
    expect(handled).toBe(false);
    expect(document.activeElement).toBe(d);
  });
});
