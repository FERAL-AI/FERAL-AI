/**
 * RC polish: when ``socket.send`` (or ``sendOrFail``) reports the WS
 * isn't open, the chat composer used to wipe the user's text the
 * moment they hit Enter — the message simply disappeared. The fix
 * restores the composer value and surfaces a small inline error chip
 * so the user knows to retry.
 */
import { describe, it, expect, afterEach, vi } from 'vitest';
import { act, cleanup, fireEvent } from '@testing-library/react';
import { renderV2 } from '../_helpers/renderV2';
import Chat from '../../pages/Chat';

const listeners = new Set();

vi.mock('../../hooks/useFeralSocket', async () => {
  const fakeSocket = {
    state: 'open',
    subscribe: (fn) => {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
    onState: (fn) => {
      try { fn('open'); } catch { /* test stub */ }
      return () => {};
    },
    // Force the send-failure branch: ``send`` returns false (the
    // contract FeralSocket.send uses when ``ws.readyState !== 1``).
    send: vi.fn(() => false),
    sendOrFail: vi.fn(() => ({ ok: false, reason: 'ws_not_open' })),
  };
  return {
    useFeralSocket: () => fakeSocket,
    sendUiEvent: vi.fn(),
  };
});

afterEach(() => {
  listeners.clear();
  cleanup();
});

describe('Chat — send-failure restores composer', () => {
  it('keeps the user text and shows an inline error chip when send fails', async () => {
    const { container, getByTestId } = renderV2(<Chat />);
    const input = container.querySelector('.v2-chat-input');

    await act(async () => {
      fireEvent.change(input, { target: { value: 'hello' } });
    });
    expect(input.value).toBe('hello');

    const form = container.querySelector('form.v2-chat-composer');
    await act(async () => { fireEvent.submit(form); });

    // a) composer value still ``hello`` — the snapshot-then-clear-only-
    //    on-success contract pinned by RC polish.
    expect(input.value).toBe('hello');

    // b) inline error chip is visible.
    const chip = getByTestId('chat-send-error');
    expect(chip).toBeTruthy();
    expect(chip.textContent.toLowerCase()).toContain('connection');
  });
});
