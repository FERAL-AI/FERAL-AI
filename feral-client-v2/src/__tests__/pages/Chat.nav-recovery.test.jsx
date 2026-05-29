/**
 * Chat composer recovery after route navigation.
 *
 * Demo-blocker repro: opening /chat, sending a message, navigating to
 * /memory and /timeline, then returning to /chat left the composer
 * permanently disabled with placeholder "Loading conversation…". The
 * Shell's hydration `ready` flag was the gate, and any silent hiccup
 * left it false forever — even a hard reload didn't recover. The fix
 * starts the shell with ready=true so the composer is interactive
 * from first paint; hydration enriches state but never gates the UI.
 *
 * This test mounts Chat inside the canonical Shell route tree, then
 * navigates away and back, and asserts the composer is enabled and
 * does not show the "Loading conversation…" placeholder.
 */
import React from 'react';
import { describe, it, expect, vi } from 'vitest';
import { render, waitFor, act } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import Shell from '../../shell/Shell';
import Chat from '../../pages/Chat';
import { StubWebSocket } from '../_helpers/renderV2';

function MemoryStub() {
  return <div data-testid="memory-stub">memory page</div>;
}

function installMocks() {
  const calls = [];
  vi.stubGlobal(
    'fetch',
    vi.fn((input) => {
      const url = typeof input === 'string' ? input : input?.url || '';
      calls.push(url);
      const body = { ok: true, status: 'ok', messages: [], entities: [], conversations: [], items: [], id: 'conv-1' };
      return Promise.resolve({
        ok: true,
        status: 200,
        statusText: 'OK',
        clone() { return this; },
        json: () => Promise.resolve(body),
        text: () => Promise.resolve(JSON.stringify(body)),
        headers: new Map(),
      });
    }),
  );
  StubWebSocket.instances = [];
  vi.stubGlobal('WebSocket', StubWebSocket);
  return { calls };
}

function chatInput(container) {
  return container.querySelector('.v2-chat-input');
}

describe('Chat — interactive after nav away/back', () => {
  it('composer is interactive on first mount (ready not gated on hydration)', async () => {
    installMocks();
    const { container } = render(
      <MemoryRouter initialEntries={['/chat']}>
        <Routes>
          <Route element={<Shell />}>
            <Route path="/chat" element={<Chat />} />
            <Route path="/memory" element={<MemoryStub />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );

    // Open the WS so connection state -> 'open'.
    await act(async () => {
      const ws = StubWebSocket.instances[StubWebSocket.instances.length - 1];
      if (ws && ws.onopen) ws.onopen({});
      await Promise.resolve();
    });

    await waitFor(() => {
      const input = chatInput(container);
      if (!input) throw new Error('composer not rendered');
      if (input.placeholder === 'Loading conversation…') {
        throw new Error(`composer still loading: ${input.placeholder}`);
      }
    });

    const input = chatInput(container);
    expect(input).toBeTruthy();
    expect(input.placeholder).not.toMatch(/Loading conversation/i);
    expect(input.disabled).toBe(false);
  });

  it('composer is interactive after unmount → remount (nav away and back)', async () => {
    installMocks();
    // Use a key-driven remount to exercise the Chat lifecycle in
    // isolation. The semantic equivalent of navigating away (Chat
    // unmounts) and back (Chat remounts) inside the Shell route tree,
    // without depending on MemoryRouter's initialEntries (which is
    // only consulted on first mount).
    function Harness({ chatKey, show }) {
      return (
        <MemoryRouter initialEntries={['/chat']}>
          <Routes>
            <Route element={<Shell />}>
              <Route path="/chat" element={show ? <Chat key={chatKey} /> : <MemoryStub />} />
            </Route>
          </Routes>
        </MemoryRouter>
      );
    }

    const { container, rerender } = render(<Harness chatKey="a" show />);

    await act(async () => {
      const ws = StubWebSocket.instances[StubWebSocket.instances.length - 1];
      if (ws && ws.onopen) ws.onopen({});
      await Promise.resolve();
    });

    await waitFor(() => {
      if (!chatInput(container)) throw new Error('initial chat not mounted');
    });

    // "Navigate away" — Chat unmounts.
    rerender(<Harness chatKey="a" show={false} />);
    await waitFor(() => {
      if (!container.querySelector('[data-testid="memory-stub"]')) {
        throw new Error('memory page did not mount');
      }
    });
    expect(chatInput(container)).toBeNull();

    // "Navigate back" — Chat remounts with a fresh key so all of its
    // local state (subscriptions, refs, optimistic loading flags) is
    // built from scratch. The shared Shell + socket persist.
    rerender(<Harness chatKey="b" show />);

    await act(async () => {
      const ws = StubWebSocket.instances[StubWebSocket.instances.length - 1];
      if (ws && ws.onopen) ws.onopen({});
      await Promise.resolve();
    });

    await waitFor(() => {
      const input = chatInput(container);
      if (!input) throw new Error('chat composer not present on remount');
      if (input.placeholder === 'Loading conversation…') {
        throw new Error('composer wedged after remount');
      }
    });

    const input = chatInput(container);
    expect(input.placeholder).not.toMatch(/Loading conversation/i);
    expect(input.disabled).toBe(false);
  });
});
