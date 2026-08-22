/**
 * A committed assistant turn keeps everything it was rendered with.
 *
 * Every other Chat test renders <Chat /> on its own, where the page
 * falls back to its local `useState` message list. Mounted for real it
 * is inside <Shell />, whose ChatThreadContext hands Chat a DIFFERENT
 * setter, and that setter used to project each row down to
 * `{id, role, text}`. So the tool cards, the reasoning block, the
 * timeline and the per-turn model/token attribution were all erased at
 * the exact moment the turn committed.
 *
 * Measured against a live brain on port 9452 with the four
 * `tool_start` / `tool_result` pairs of a real screen+browser turn:
 * eight frames arrived at the page, four cards rendered while the turn
 * was in flight, and the committed transcript held zero. These tests
 * therefore mount Chat THROUGH Shell, which is the only arrangement
 * that exercises the setter production uses.
 */
import React from 'react';
import { describe, it, expect, afterEach, vi } from 'vitest';
import { act, cleanup, fireEvent, render } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { installFetchMock, StubWebSocket } from '../_helpers/renderV2';

const listeners = new Set();

vi.mock('../../hooks/useFeralSocket', () => {
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
    setSession: () => {},
    send: () => true,
    sendOrFail: () => ({ ok: true }),
  };
  return {
    useFeralSocket: () => fakeSocket,
    sendUiEvent: () => {},
    _getSharedSocketForTesting: () => fakeSocket,
    _resetSharedSocketForTesting: () => {},
  };
});

vi.mock('../../hooks/useConnectionStatus', () => ({
  useConnectionStatus: () => ({ state: 'open' }),
}));

import Shell from '../../shell/Shell';
import Chat from '../../pages/Chat';

function emit(msg) {
  listeners.forEach((fn) => fn(msg));
}

async function mountChatInShell() {
  installFetchMock();
  StubWebSocket.instances = [];
  vi.stubGlobal('WebSocket', StubWebSocket);
  if (!window.matchMedia) {
    window.matchMedia = vi.fn().mockImplementation((q) => ({
      matches: false,
      media: q,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }));
  }
  let utils;
  await act(async () => {
    utils = render(
      <MemoryRouter initialEntries={['/chat']}>
        <Routes>
          <Route element={<Shell />}>
            <Route path="/chat" element={<Chat />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );
  });
  return utils;
}

async function sendTurn(container, text = 'what is on my screen') {
  const input = container.querySelector('.v2-chat-input');
  const form = container.querySelector('form.v2-chat-composer');
  await act(async () => {
    fireEvent.change(input, { target: { value: text } });
    fireEvent.submit(form);
  });
}

const TOOL_START = {
  type: 'tool_start',
  payload: {
    tool: 'vision__screen_capture',
    call_id: 'c1',
    skill_id: 'vision',
    endpoint_id: 'screen_capture',
    args_preview: '{"region":"full desktop"}',
    display_name: 'Capture screen',
  },
};

const TOOL_RESULT = {
  type: 'tool_result',
  payload: {
    tool: 'vision__screen_capture',
    call_id: 'c1',
    success: true,
    latency_ms: 412,
    result_preview: '1920 x 1200 jpeg',
  },
};

afterEach(() => {
  listeners.clear();
  cleanup();
});

describe('a committed turn keeps its tool trace', () => {
  it('leaves the tool card in the transcript after the answer lands', async () => {
    const { container } = await mountChatInShell();
    await sendTurn(container);

    await act(async () => { emit(TOOL_START); });
    expect(container.querySelectorAll('[data-testid="tool-call-card"]').length)
      .toBeGreaterThan(0);

    await act(async () => {
      emit(TOOL_RESULT);
      emit({ type: 'text_response', payload: { text: 'Three tabs were open.' } });
    });

    // The turn is committed now. The card has to still be there: this is
    // the assertion that was false in the shipped build.
    expect(container.querySelectorAll('[data-testid="tool-call-card"]').length)
      .toBeGreaterThan(0);
    expect(container.textContent).toContain('Capture screen');
    expect(container.textContent).toContain('Three tabs were open.');
  });

  it('keeps a tool-only turn instead of dropping it for having no text', async () => {
    const { container } = await mountChatInShell();
    await sendTurn(container);

    await act(async () => {
      emit(TOOL_START);
      emit(TOOL_RESULT);
      // A tool-only turn: the brain finalises with no prose at all.
      emit({ type: 'text_response', payload: { text: '' } });
    });

    expect(container.querySelectorAll('[data-testid="tool-call-card"]').length)
      .toBeGreaterThan(0);
  });

  it('keeps the per-turn model and token attribution', async () => {
    const { container } = await mountChatInShell();
    await sendTurn(container);

    await act(async () => {
      emit({
        type: 'text_response',
        payload: {
          text: 'Done.',
          model: 'openai/gpt-5',
          usage: { input_tokens: 10, output_tokens: 5, total_tokens: 15 },
        },
      });
    });

    const meta = container.querySelector('[data-testid="chat-turn-meta"]');
    expect(meta).not.toBeNull();
    expect(meta.textContent).toContain('openai/gpt-5');
  });

  it('keeps the reasoning block on the committed turn', async () => {
    const { container } = await mountChatInShell();
    await sendTurn(container);

    await act(async () => {
      emit({
        type: 'stream_delta',
        payload: { kind: 'reasoning', delta: 'checking the open windows' },
      });
      emit({ type: 'text_response', payload: { text: 'Three tabs were open.' } });
    });

    // The section renders collapsed, so assert on the section itself
    // rather than on prose the reader has to expand to see.
    expect(container.querySelector('[data-testid="reasoning-section"]')).not.toBeNull();
    expect(container.textContent).toContain('Three tabs were open.');
  });
});
