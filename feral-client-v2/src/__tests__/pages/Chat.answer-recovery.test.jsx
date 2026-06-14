/**
 * Chat answer recovery after navigating away mid-answer.
 *
 * Repro: ask a question, leave /chat (the stream handler + commit live
 * on the Chat page, which unmounts), the brain finishes the turn while
 * away and records it in the server transcript — but nothing wrote it
 * into the thread, so returning to /chat showed a silently dropped
 * reply. The fix re-pulls /api/sessions/primary/transcript on every
 * Chat mount and merges missing turns (deduped by role+text).
 *
 * This test mounts Chat with a transcript that contains an assistant
 * turn the thread doesn't have, and asserts the answer appears.
 */
import React from 'react';
import { describe, it, expect, vi } from 'vitest';
import { render, waitFor, act } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import Shell from '../../shell/Shell';
import Chat from '../../pages/Chat';
import { StubWebSocket } from '../_helpers/renderV2';

const RECOVERED = 'This is the answer that finished while you were on Settings.';

function installMocks() {
  vi.stubGlobal(
    'fetch',
    vi.fn((input) => {
      const url = typeof input === 'string' ? input : input?.url || '';
      let body = { ok: true, status: 'ok', messages: [], entities: [], conversations: [], items: [], id: 'conv-1' };
      if (url.includes('/api/sessions/primary/transcript')) {
        body = { messages: [{ role: 'assistant', text: RECOVERED, ts_ms: 123 }] };
      }
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
  if (!window.matchMedia) {
    window.matchMedia = vi.fn().mockImplementation((q) => ({
      matches: false, media: q, onchange: null,
      addEventListener: vi.fn(), removeEventListener: vi.fn(),
      addListener: vi.fn(), removeListener: vi.fn(), dispatchEvent: vi.fn(),
    }));
  }
}

describe('Chat — recovers an answer that completed while navigated away', () => {
  it('merges the missing assistant turn from the primary transcript on mount', async () => {
    installMocks();
    const { container } = render(
      <MemoryRouter initialEntries={['/chat']}>
        <Routes>
          <Route element={<Shell />}>
            <Route path="/chat" element={<Chat />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );

    await act(async () => {
      const ws = StubWebSocket.instances[StubWebSocket.instances.length - 1];
      if (ws && ws.onopen) ws.onopen({});
      await Promise.resolve();
    });

    await waitFor(() => {
      if (!container.textContent.includes(RECOVERED)) {
        throw new Error('recovered answer not rendered');
      }
    });

    expect(container.textContent).toContain(RECOVERED);
  });
});
