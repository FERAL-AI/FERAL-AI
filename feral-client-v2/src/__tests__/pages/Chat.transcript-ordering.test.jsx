/**
 * Rendered transcript order (operator report 2026-07-28).
 *
 * The maintainer used voice with the chat pane open and their spoken
 * transcription rendered BELOW the assistant reply that answered it.
 * OpenAI's Realtime docs say `input_audio_transcription.completed`
 * "runs asynchronously with Response creation, so this event may come
 * before or after the Response events" — so the brain can legitimately
 * deliver the user transcript AFTER the assistant response, and the
 * client must still render user-then-assistant.
 *
 * This asserts the rendered DOM order, not just the ordering helper.
 */
import { describe, it, expect, afterEach, vi } from 'vitest';
import { act, cleanup } from '@testing-library/react';
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
    send: vi.fn(() => true),
  };
  return {
    useFeralSocket: () => fakeSocket,
    sendUiEvent: vi.fn(),
  };
});

// Supply only `activeSessionId` so the per-thread frame filter is
// live. Deliberately no `messages` / `setMessages`: Chat falls back to
// its own React state for those, whereas a stubbed `setMessages` would
// swallow every update and render nothing.
const ACTIVE_SESSION = 'session-thread-A';

vi.mock('../../shell/Shell', async () => {
  const actual = await vi.importActual('../../shell/Shell');
  return {
    ...actual,
    useChatThread: () => ({ activeSessionId: 'session-thread-A' }),
  };
});

function emit(msg) {
  listeners.forEach((fn) => fn(msg));
}

const GREETING = 'FERAL v2 is listening. What do you need?';

function renderedRows(container) {
  return Array.from(container.querySelectorAll('.v2-chat-row'))
    .map((row) => ({
      role: row.className.includes('v2-chat-row--user') ? 'user' : 'assistant',
      text: row.querySelector('.v2-chat-body')?.textContent?.trim() || '',
    }))
    // Chat seeds its local state with a greeting bubble.
    .filter((r) => r.text !== GREETING);
}

afterEach(() => {
  listeners.clear();
  cleanup();
});

describe('Chat — voice transcript ordering', () => {
  it('renders user-then-assistant when the user transcript arrives LAST', async () => {
    const { container } = renderV2(<Chat />);

    // Assistant reply wins the race and lands first, declaring the
    // user's conversation item as its predecessor.
    await act(async () => {
      emit({
        type: 'transcript',
        payload: {
          text: 'It is sunny in Berlin.',
          role: 'assistant',
          is_partial: false,
          item_id: 'item_assistant',
          previous_item_id: 'item_user',
          seq: 0,
        },
      });
    });

    // The user's own transcription arrives afterwards, with a higher
    // seq — appending by arrival (or sorting by seq alone) would put it
    // below the answer.
    await act(async () => {
      emit({
        type: 'transcript',
        payload: {
          text: "what's the weather",
          role: 'user',
          is_partial: false,
          item_id: 'item_user',
          seq: 1,
        },
      });
    });

    const rows = renderedRows(container);
    expect(rows).toHaveLength(2);
    expect(rows[0]).toEqual({ role: 'user', text: "what's the weather" });
    expect(rows[1]).toEqual({ role: 'assistant', text: 'It is sunny in Berlin.' });
  });

  it('renders in-order arrivals unchanged', async () => {
    const { container } = renderV2(<Chat />);

    await act(async () => {
      emit({
        type: 'transcript',
        payload: {
          text: 'turn on the lights', role: 'user', is_partial: false,
          item_id: 'item_user', seq: 0,
        },
      });
    });
    await act(async () => {
      emit({
        type: 'transcript',
        payload: {
          text: 'Done.', role: 'assistant', is_partial: false,
          item_id: 'item_assistant', previous_item_id: 'item_user', seq: 1,
        },
      });
    });

    expect(renderedRows(container).map((r) => r.role)).toEqual([
      'user', 'assistant',
    ]);
  });

  it('keeps the user transcript right-aligned and the reply left-aligned', async () => {
    // Alignment is driven purely by the wire `role` — pages.css flips
    // the grid only for `.v2-chat-row--user`.
    const { container } = renderV2(<Chat />);

    await act(async () => {
      emit({
        type: 'transcript',
        payload: {
          text: 'hello brain', role: 'user', is_partial: false,
          item_id: 'item_u', seq: 0,
        },
      });
    });

    const userRows = Array.from(container.querySelectorAll('.v2-chat-row'))
      .filter((r) => r.textContent.includes('hello brain'));
    expect(userRows).toHaveLength(1);
    expect(userRows[0].className).toContain('v2-chat-row--user');
  });

  it('replaces a partial with the final in place instead of stacking', async () => {
    const { container } = renderV2(<Chat />);

    await act(async () => {
      emit({
        type: 'transcript',
        payload: {
          text: 'what is the', role: 'user', is_partial: true,
          item_id: 'item_user', seq: 0,
        },
      });
    });
    await act(async () => {
      emit({
        type: 'transcript',
        payload: {
          text: 'what is the weather', role: 'user', is_partial: false,
          item_id: 'item_user', seq: 1,
        },
      });
    });

    const rows = renderedRows(container);
    expect(rows).toHaveLength(1);
    expect(rows[0].text).toBe('what is the weather');
  });

  it('drops transcripts addressed to a different thread', async () => {
    // `transcript` was missing from CHAT_FRAME_TYPES, so voice frames
    // bypassed the per-thread session filter and rendered into
    // whichever thread happened to be open.
    const { container } = renderV2(<Chat />);

    await act(async () => {
      emit({
        type: 'transcript',
        session_id: 'some-other-session',
        payload: {
          text: 'belongs to another thread', role: 'user',
          is_partial: false, item_id: 'item_other', seq: 0,
        },
      });
    });
    expect(renderedRows(container)).toHaveLength(0);

    // A frame for the active session still renders.
    await act(async () => {
      emit({
        type: 'transcript',
        session_id: ACTIVE_SESSION,
        payload: {
          text: 'belongs to this thread', role: 'user',
          is_partial: false, item_id: 'item_mine', seq: 1,
        },
      });
    });
    expect(renderedRows(container).map((r) => r.text))
      .toEqual(['belongs to this thread']);
  });
});
