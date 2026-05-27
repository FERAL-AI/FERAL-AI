/**
 * W10 (V1.0 cut-list #8 → S6 thesis full) — the yellow inline
 * BudgetExceededBanner now also lights up when the brain emits the
 * background-subsystem cap-hit signal.
 *
 * The brain wraps every cross-subsystem broadcast (BudgetLoopGuard,
 * proactive engine, cron, email, mqtt, screen_loop) as
 *   { type: 'state_push', event: <name>, data: <payload> }
 * via BrainState.broadcast_event. Pre-v2026.5.43 the Chat handler
 * only branched on top-level ``msg.type === 'budget_exceeded'`` (the
 * orchestrator's session-scoped frame) and silently dropped the
 * ``cost_cap_hit`` event, so ScreenLoop hitting its hourly cap never
 * surfaced any operator-visible signal.
 *
 * These tests pin the contract:
 *
 * 1. ``state_push`` with ``event === 'cost_cap_hit'`` renders the
 *    same yellow banner, keyed by ``call_site``.
 * 2. The legacy ``budget_exceeded`` frame still works (backwards
 *    compat — chat-path orchestrator emit unchanged).
 * 3. The banner copy prefers the ``subsystem`` field when present
 *    (e.g. ``"ScreenLoop budget reached"`` instead of
 *    ``"Screen_loop budget reached"``).
 */

import React from 'react';
import { describe, it, expect, afterEach, vi } from 'vitest';
import { act, cleanup, screen } from '@testing-library/react';
import { renderV2 } from '../_helpers/renderV2';
import Chat, { budgetBannerFromCapHit } from '../../pages/Chat';

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

describe('Chat — cost_cap_hit banner (S6 ScreenLoop closer)', () => {
  it('renders BudgetExceededBanner when cost_cap_hit arrives wrapped as state_push', async () => {
    renderV2(<Chat />);

    await act(async () => {
      dispatch({
        type: 'state_push',
        event: 'cost_cap_hit',
        data: {
          type: 'cost_cap_hit',
          call_site: 'screen_loop',
          subsystem: 'ScreenLoop',
          cap_dollars: 0.10,
          current_dollars: 0.10,
          window: 'hour',
          reset_at: Math.floor(Date.now() / 1000) + 600,
          paused_until: Math.floor(Date.now() / 1000) + 600,
          ts: Date.now() / 1000,
        },
      });
    });

    const banner = screen.getByTestId('budget-exceeded-banner');
    expect(banner).toBeInTheDocument();
    // Subsystem-driven copy — explicit "ScreenLoop" wins over the
    // humanized call_site ("Screen loop"). Pinning this catches a
    // regression where someone forgets to thread the prop through.
    expect(banner.textContent).toMatch(/ScreenLoop budget reached/);
    expect(banner.textContent).toMatch(/\$0\.10/);
  });

  it('renders BudgetExceededBanner when budget_exceeded arrives (backwards-compat)', async () => {
    renderV2(<Chat />);

    await act(async () => {
      dispatch({
        type: 'budget_exceeded',
        payload: {
          call_site: 'chat',
          cap_dollars: 0.10,
          current_dollars: 0.12,
          reset_at: Math.floor(Date.now() / 1000) + 600,
        },
      });
    });

    const banner = screen.getByTestId('budget-exceeded-banner');
    expect(banner).toBeInTheDocument();
    expect(banner.textContent).toMatch(/Chat budget reached/);
  });

  it('ignores unrelated state_push events (no banner)', async () => {
    renderV2(<Chat />);

    await act(async () => {
      dispatch({
        type: 'state_push',
        event: 'something_else',
        data: { call_site: 'screen_loop' },
      });
    });

    expect(screen.queryByTestId('budget-exceeded-banner')).toBeNull();
  });
});

describe('budgetBannerFromCapHit normalizer', () => {
  it('coerces both payload shapes to the same banner state', () => {
    const capHit = budgetBannerFromCapHit({
      call_site: 'screen_loop',
      subsystem: 'ScreenLoop',
      cap_dollars: 0.10,
      current_dollars: 0.10,
      reset_at: 123,
    });
    expect(capHit).toEqual({
      callSite: 'screen_loop',
      capDollars: 0.10,
      currentDollars: 0.10,
      resetAt: 123,
      subsystem: 'ScreenLoop',
    });

    const chatBudget = budgetBannerFromCapHit({
      call_site: 'chat',
      cap_dollars: 0.10,
      current_dollars: 0.12,
      reset_at: 456,
    });
    expect(chatBudget).toEqual({
      callSite: 'chat',
      capDollars: 0.10,
      currentDollars: 0.12,
      resetAt: 456,
      subsystem: null,
    });
  });

  it('falls back to the provided default site id when call_site is missing', () => {
    expect(budgetBannerFromCapHit({}, 'fallback').callSite).toBe('fallback');
  });
});
