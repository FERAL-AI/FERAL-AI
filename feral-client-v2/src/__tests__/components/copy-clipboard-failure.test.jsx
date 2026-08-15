/**
 * A copy button may only confirm a clipboard write that happened.
 *
 * `navigator.clipboard.writeText` returns a Promise and rejects on an
 * insecure origin (plain http on a LAN address, which is exactly how
 * this brain is reached from a second machine), on a denied permission,
 * and when the document is not focused. Two files had their own copy of
 *
 *     try { navigator.clipboard.writeText(text); setCopied(id); }
 *     catch { }
 *
 * The `try` is synchronous, so the rejection lands outside it: the
 * catch cannot run, `setCopied` runs unconditionally, and the check
 * icon appears over an unchanged clipboard. On PairDeviceModal's daemon
 * tab that clipboard was supposed to be holding a pairing token the UI
 * shows exactly once, so the user pastes stale content into a terminal
 * and has to revoke and reissue to recover.
 *
 * Both call sites now go through ui/CopyButton, which awaits the write.
 * These tests drive the rejecting path end to end.
 *
 * "Success" is asserted three ways because a checkmark can be spelled
 * three ways: the lucide check glyph, the `is-copied` class, and the
 * accessible name. None of them may say copied.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import CopyButton from '../../ui/CopyButton';
import PairDeviceModal from '../../components/PairDeviceModal';
import AppsPublish from '../../pages/AppsPublish';

/** Install a clipboard whose writeText rejects, like an insecure origin. */
function rejectingClipboard(message = 'Document is not focused') {
  const writeText = vi.fn(() => Promise.reject(new DOMException(message, 'NotAllowedError')));
  Object.defineProperty(navigator, 'clipboard', {
    value: { writeText },
    configurable: true,
    writable: true,
  });
  return writeText;
}

function resolvingClipboard() {
  const writeText = vi.fn(() => Promise.resolve());
  Object.defineProperty(navigator, 'clipboard', {
    value: { writeText },
    configurable: true,
    writable: true,
  });
  return writeText;
}

/**
 * The button must not be showing any form of "copied".
 * `el` is the button node captured before the click.
 */
function expectNoSuccessAffordance(el) {
  expect(el.querySelector('.lucide-check'), 'a check glyph appeared for a clipboard write that failed').toBeNull();
  expect(el.className).not.toMatch(/is-copied/);
  expect(el.getAttribute('aria-label') || '').not.toMatch(/copied/i);
  expect(el.getAttribute('title') || '').not.toMatch(/copied/i);
}

beforeEach(() => {
  // jsdom does not implement execCommand. Pin it to a failing stub so
  // the fallback path is exercised deterministically rather than being
  // silently skipped.
  document.execCommand = vi.fn(() => false);
});

afterEach(() => {
  cleanup();
  delete navigator.clipboard;
  delete document.execCommand;
  vi.unstubAllGlobals();
});

describe('CopyButton: rejected clipboard write', () => {
  it('does not show a checkmark when writeText rejects', async () => {
    const writeText = rejectingClipboard();
    const { getByTestId } = render(<CopyButton value="tok_secret" label="Copy token" />);
    const btn = getByTestId('copy-button');

    fireEvent.click(btn);
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('tok_secret'));
    // Nothing may be claimed while the write is still in flight.
    expectNoSuccessAffordance(btn);
    await waitFor(() => expect(btn.getAttribute('data-state')).toBe('failed'));
    expectNoSuccessAffordance(btn);
  });

  it('says the copy failed instead of going silent', async () => {
    rejectingClipboard();
    const { getByTestId } = render(<CopyButton value="tok_secret" label="Copy token" />);
    const btn = getByTestId('copy-button');

    fireEvent.click(btn);
    await waitFor(() => expect(btn.getAttribute('aria-label')).toMatch(/copy failed/i));
  });

  it('does not claim success when there is no clipboard API at all', async () => {
    delete navigator.clipboard;
    const { getByTestId } = render(<CopyButton value="tok_secret" label="Copy token" />);
    const btn = getByTestId('copy-button');

    fireEvent.click(btn);
    await waitFor(() => expect(btn.getAttribute('data-state')).toBe('failed'));
    expectNoSuccessAffordance(btn);
  });

  it('still confirms when the write really succeeds', async () => {
    const writeText = resolvingClipboard();
    const { getByTestId } = render(<CopyButton value="tok_secret" label="Copy token" />);
    const btn = getByTestId('copy-button');

    fireEvent.click(btn);
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('tok_secret'));
    await waitFor(() => expect(btn.getAttribute('data-state')).toBe('copied'));
    expect(btn.querySelector('.lucide-check')).not.toBeNull();
    expect(btn.getAttribute('aria-label')).toMatch(/copied/i);
  });
});

describe('PairDeviceModal: pairing token copy', () => {
  it('does not confirm the token was copied when the clipboard write fails', async () => {
    const writeText = rejectingClipboard();
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({
      ok: true,
      status: 200,
      statusText: 'OK',
      json: () => Promise.resolve({
        device_id: 'dev-1',
        node_id: 'my-laptop-bridge',
        token: 'tok_shown_once',
      }),
      text: () => Promise.resolve('{}'),
      headers: new Map(),
    })));

    const { getByRole, getAllByRole, getByPlaceholderText, findAllByText } = render(
      <MemoryRouter>
        <PairDeviceModal open onClose={() => {}} onTokenIssued={() => {}} />
      </MemoryRouter>,
    );

    fireEvent.click(getByRole('tab', { name: /Daemon token/i }));
    fireEvent.change(getByPlaceholderText('my-laptop-bridge'), {
      target: { value: 'my-laptop-bridge' },
    });
    fireEvent.click(getByRole('button', { name: /Issue token/i }));

    // The one-liner carries the token, and it is shown exactly once.
    await findAllByText(/tok_shown_once/);

    const copyBtn = getAllByRole('button', { name: /Copy command/i })[0];
    fireEvent.click(copyBtn);

    await waitFor(() => expect(writeText).toHaveBeenCalled());
    expect(writeText.mock.calls[0][0]).toMatch(/tok_shown_once/);
    expectNoSuccessAffordance(copyBtn);
    await waitFor(() => expect(copyBtn.getAttribute('data-state')).toBe('failed'));
    expectNoSuccessAffordance(copyBtn);
  });
});

describe('AppsPublish: CLI copy', () => {
  it('does not confirm a CLI copy that the clipboard refused', async () => {
    const writeText = rejectingClipboard();
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({
      ok: true,
      status: 200,
      statusText: 'OK',
      json: () => Promise.resolve({ apps: [], count: 0 }),
      text: () => Promise.resolve('{}'),
      headers: new Map(),
    })));

    const { getByRole } = render(
      <MemoryRouter><AppsPublish /></MemoryRouter>,
    );

    const copyBtn = getByRole('button', { name: /Copy command: feral app init coffee-log/i });
    fireEvent.click(copyBtn);

    await waitFor(() => expect(writeText).toHaveBeenCalledWith('feral app init coffee-log'));
    expectNoSuccessAffordance(copyBtn);
    await waitFor(() => expect(copyBtn.getAttribute('data-state')).toBe('failed'));
    expectNoSuccessAffordance(copyBtn);
  });
});
