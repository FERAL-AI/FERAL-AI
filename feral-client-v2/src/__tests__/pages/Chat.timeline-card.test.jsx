/**
 * S1 thesis closer (cut-list item #8) — Chat.jsx WS handler for the
 * brain's ``timeline`` frame.
 *
 * When the orchestrator dispatches ``notes_memory__fused_timeline``
 * for a temporal-recall query ("what did I do yesterday?"), it
 * emits a dedicated ``timeline`` FeralMessage alongside the streaming
 * chat response. The Chat page mounts a TimelineCard bubble keyed by
 * session_id + query so repeats of the same question replace the
 * previous card instead of stacking.
 *
 * These tests pin:
 *   1. A ``timeline`` frame arrives → a TimelineCard bubble appears.
 *   2. The card consumes the canonical {entries, window, degraded_sources}
 *      payload (not the legacy {sections, date} shape).
 *   3. A repeat of the same {session_id, query} replaces the prior
 *      card in place (the WebUI never stacks duplicate cards for the
 *      same question).
 */

import React from 'react';
import { describe, it, expect, afterEach, vi } from 'vitest';
import { act, cleanup, screen } from '@testing-library/react';
import { renderV2 } from '../_helpers/renderV2';
import Chat from '../../pages/Chat';

const listeners = new Set();

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
  sendOrFail: vi.fn(() => ({ ok: true })),
};

vi.mock('../../hooks/useFeralSocket', () => ({
  useFeralSocket: () => fakeSocket,
  sendUiEvent: vi.fn(),
  _getSharedSocketForTesting: () => fakeSocket,
}));

function dispatch(frame) {
  for (const fn of listeners) fn(frame);
}

afterEach(() => {
  listeners.clear();
  cleanup();
});

const sampleFrame = (overrides = {}) => ({
  type: 'timeline',
  session_id: 'sess-1',
  payload: {
    session_id: 'sess-1',
    query: 'what did I do yesterday?',
    window: { from: '2026-05-26T00:00:00', to: '2026-05-27T00:00:00', label: 'yesterday' },
    summary: '',
    entries: [
      {
        source: 'episode', type: 'episode',
        timestamp: 1716700000,
        title: 'standup',
        content: 'said 9am works',
        metadata: { id: 'ep-1' },
      },
      {
        source: 'note', type: 'note',
        timestamp: 1716710000,
        title: 'pick up groceries',
        content: 'milk, eggs',
        metadata: { id: 'note-1' },
      },
    ],
    sources_queried: ['episode', 'note', 'calendar', 'health', 'screen_loop'],
    degraded_sources: [
      { source: 'calendar', reason: 'no_token' },
      { source: 'screen_loop', reason: 'no_query_api' },
    ],
    ...overrides,
  },
});

describe('Chat — timeline frame (S1 closer)', () => {
  it('inserts a TimelineCard bubble when the timeline WS frame arrives', async () => {
    renderV2(<Chat />);

    await act(async () => {
      dispatch(sampleFrame());
    });

    const card = screen.getByTestId('timeline-card');
    expect(card).toBeInTheDocument();
    expect(card.textContent).toMatch(/Yesterday/i);
    expect(card.textContent).toMatch(/standup/i);
    expect(card.textContent).toMatch(/Chat unavailable|Calendar unavailable/i);
  });

  it('renders degraded chips for unconfigured sources', async () => {
    renderV2(<Chat />);

    await act(async () => {
      dispatch(sampleFrame());
    });

    const chips = screen.getAllByTestId('timeline-degraded-chip');
    // Calendar + screen_loop unavailable from the seed payload.
    expect(chips.length).toBeGreaterThanOrEqual(2);
    const joined = chips.map((c) => c.textContent).join(' | ');
    expect(joined).toMatch(/Calendar unavailable/i);
    expect(joined).toMatch(/Screen activity unavailable/i);
  });

  it('replaces the previous card for repeated {session_id, query} dispatches', async () => {
    renderV2(<Chat />);

    await act(async () => {
      dispatch(sampleFrame());
    });
    expect(screen.getAllByTestId('timeline-card').length).toBe(1);

    // Same session + same query → second frame replaces the first.
    await act(async () => {
      dispatch(sampleFrame({
        entries: [
          { source: 'episode', type: 'episode', timestamp: 1, title: 'new episode', content: 'updated', metadata: { id: 'ep-99' } },
        ],
      }));
    });

    const cards = screen.getAllByTestId('timeline-card');
    expect(cards.length).toBe(1);
    expect(cards[0].textContent).toMatch(/new episode/);
    expect(cards[0].textContent).not.toMatch(/standup/);
  });

  it('stacks a separate card when the query differs', async () => {
    renderV2(<Chat />);

    await act(async () => {
      dispatch(sampleFrame());
    });
    await act(async () => {
      dispatch(sampleFrame({ query: 'summarize my morning' }));
    });

    const cards = screen.getAllByTestId('timeline-card');
    expect(cards.length).toBe(2);
  });
});
