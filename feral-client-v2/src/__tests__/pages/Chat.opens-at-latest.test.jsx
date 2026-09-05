/**
 * Opening a thread lands on the newest message.
 *
 * Measured on the operator's own 49-message thread on 2026-09-05: a
 * fresh load of /chat left `.v2-chat-log` at scrollTop 0 of scrollHeight
 * 6480, so the page opened on a conversation from days earlier and the
 * reply just sent was six screens down. Twice in a row it read as "my
 * message did not send", which is the worst thing a chat can say when it
 * is lying.
 *
 * The follow-the-tail effect could not do this job: it asks for a SMOOTH
 * scroll after paint, and on any real thread the animation starts from
 * the top and then loses to markdown and tool cards growing scrollHeight
 * underneath it.
 *
 * jsdom does not lay anything out, so these tests drive the geometry
 * directly: scrollHeight and clientHeight are stubbed on the log element
 * and scrollTop is a real writable property. That is enough to pin the
 * decisions this code makes, which are "jump once per conversation",
 * "not before there is anything to jump to", and "never against a user
 * who has scrolled away".
 */

import React from 'react';
import { render, screen } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('../../lib/api', () => ({
  apiJson: vi.fn(async () => ({ messages: [] })),
  apiFetch: vi.fn(async () => ({ ok: true, json: async () => ({}) })),
}));

const threadState = {
  ready: true,
  conversationId: 'thread-one',
  messages: [],
  setMessages: vi.fn(),
  setConversation: vi.fn(),
  loadConversation: vi.fn(),
  startNewConversation: vi.fn(),
  ensureConversation: vi.fn(async () => {}),
  primarySessionId: '',
  isPrimaryThread: true,
  activeSessionToken: '',
  activeSessionId: '',
  askDraft: '',
  setAskDraft: vi.fn(),
  clearAskDraft: vi.fn(),
};

vi.mock('../../shell/Shell', () => ({
  useChatThread: () => threadState,
}));

/** Give the log element the geometry jsdom refuses to compute. */
function measure(el, { scrollHeight, clientHeight }) {
  Object.defineProperty(el, 'scrollHeight', { value: scrollHeight, configurable: true });
  Object.defineProperty(el, 'clientHeight', { value: clientHeight, configurable: true });
}

async function renderChat(messages) {
  threadState.messages = messages;
  const { default: Chat } = await import('../../pages/Chat');
  const utils = render(<Chat />);
  const log = document.querySelector('.v2-chat-log');
  // jsdom lays nothing out, so the layout effect's first run always sees
  // a zero-height log and correctly declines to jump. Every test gives
  // the log its geometry and then re-renders, which is the same order
  // the browser produces: content measures, then the effect runs again.
  // A new array identity, because that is what hydration hands the
  // component and what the effect's dependency list keys on.
  const settle = async () => {
    threadState.messages = [...threadState.messages];
    utils.rerender(<Chat />);
    await new Promise((r) => setTimeout(r, 0));
  };
  return { ...utils, log, settle, Chat };
}

const LONG_THREAD = Array.from({ length: 20 }, (_, i) => ({
  id: `m${i}`,
  role: i % 2 ? 'assistant' : 'user',
  text: `message ${i}`,
}));

beforeEach(() => {
  vi.resetModules();
  threadState.conversationId = 'thread-one';
  Element.prototype.scrollIntoView = vi.fn();
});

describe('opening a thread', () => {
  it('lands on the newest message, not the oldest', async () => {
    const { log, settle } = await renderChat(LONG_THREAD);
    measure(log, { scrollHeight: 6480, clientHeight: 580 });
    await settle();
    expect(log.scrollTop).toBe(6480);
  });

  it('does not jump before anything has been laid out', async () => {
    const { log, settle } = await renderChat([]);
    measure(log, { scrollHeight: 0, clientHeight: 580 });
    await settle();
    expect(log.scrollTop).toBe(0);
  });

  it('jumps again when the user switches to another thread', async () => {
    const { log, settle } = await renderChat(LONG_THREAD);
    measure(log, { scrollHeight: 6480, clientHeight: 580 });
    await settle();
    expect(log.scrollTop).toBe(6480);

    // The user scrolls up to re-read something, then opens a new thread.
    log.scrollTop = 0;
    threadState.conversationId = 'thread-two';
    threadState.messages = [...LONG_THREAD, { id: 'm20', role: 'user', text: 'later' }];
    await settle();
    expect(log.scrollTop).toBe(6480);
  });

  it('leaves the user alone once the thread has settled', async () => {
    const { log, settle } = await renderChat(LONG_THREAD);
    measure(log, { scrollHeight: 6480, clientHeight: 580 });
    await settle();
    expect(log.scrollTop).toBe(6480);

    // Same conversation, user scrolled up to read history, a new message
    // arrives. The jump must not fire again and yank them back; whether
    // the tail is followed is the other effect's decision, driven by the
    // scroll handler, and it is not exercised here.
    log.scrollTop = 1200;
    threadState.messages = [...LONG_THREAD, { id: 'm21', role: 'assistant', text: 'new' }];
    await settle();
    expect(log.scrollTop).toBe(1200);
  });

  it('renders the newest message so there is something to land on', async () => {
    await renderChat(LONG_THREAD);
    expect(screen.getByText('message 19')).toBeTruthy();
  });
});
