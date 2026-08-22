/**
 * "For you today" audit guards.
 *
 * Three defects, all in the same family: a request that failed reported
 * nothing, or reported success.
 *
 *  1. POST /api/ideas/refresh answers 200 with
 *     `{success:false, degraded:{morning_brief:"..."}}` when a
 *     generator raised. The brain added that field specifically so
 *     "nothing to suggest today" and "the generators are broken" could
 *     be told apart (api/routes/ideas.py:76-96). The pane read only
 *     `today`, so a fully broken engine rendered the "Nothing to
 *     suggest yet" empty state.
 *  2. Accept / dismiss were bare try/finally around an `apiFetch` that
 *     THROWS on a non-2xx (lib/api.js:138-142). A 404 ("Unknown idea
 *     id") or 503 ("IdeasEngine not initialised") escaped the click
 *     handler as an unhandled rejection and this pane said nothing.
 *  3. The `install_routine` accept path wrapped its POST in
 *     `catch { /* best-effort *\/ }`, so accepting a routine idea
 *     removed the row and looked successful even when the install
 *     failed outright.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, waitFor, fireEvent, act } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import ForYouToday from '../../components/ForYouToday';
import { StubWebSocket } from '../_helpers/renderV2';

afterEach(() => {
  vi.unstubAllGlobals();
});

const IDEA = {
  id: 'i1',
  kind: 'work',
  text: 'You paused "ship v2026.4.23" 6h ago. Resume?',
  source_signals: ['consciousness:intent:x'],
  action: { kind: 'install_routine', verb: 'install', payload: { routine_id: 'wind_down' } },
  severity: 'info',
};

/**
 * A fetch stub that can answer with a real non-2xx, which the shared
 * `installFetchMock` helper cannot (it hardcodes ok/200).
 */
function stubFetch(handler) {
  vi.stubGlobal('fetch', vi.fn((input, init) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    const { status = 200, body = {} } = handler(url, init) || {};
    return Promise.resolve({
      ok: status >= 200 && status < 300,
      status,
      statusText: 'x',
      clone() { return this; },
      json: () => Promise.resolve(body),
      text: () => Promise.resolve(JSON.stringify(body)),
      headers: new Map(),
    });
  }));
}

function renderPane() {
  StubWebSocket.instances = [];
  vi.stubGlobal('WebSocket', StubWebSocket);
  if (!window.matchMedia) {
    window.matchMedia = vi.fn().mockImplementation((q) => ({
      matches: false, media: q, onchange: null,
      addEventListener: vi.fn(), removeEventListener: vi.fn(),
      addListener: vi.fn(), removeListener: vi.fn(), dispatchEvent: vi.fn(),
    }));
  }
  return render(<MemoryRouter><ForYouToday /></MemoryRouter>);
}

describe('ForYouToday: a broken generator is not an empty day', () => {
  it('surfaces `degraded` from POST /api/ideas/refresh', async () => {
    stubFetch((url, init) => {
      if (url.includes('/api/ideas/refresh') && init?.method === 'POST') {
        return {
          status: 200,
          body: {
            success: false,
            degraded: {
              morning_brief: 'AttributeError: no attribute get_all_baselines',
              refresh_waiting_user: 'RuntimeError: store closed',
            },
            new_ideas: [],
            today: [],
          },
        };
      }
      return { status: 200, body: { ideas: [], count: 0 } };
    });

    const { getByLabelText, container } = renderPane();
    await waitFor(() => expect(container.textContent).toContain('Nothing to suggest yet'));
    await act(async () => { fireEvent.click(getByLabelText('Refresh ideas')); });

    await waitFor(() => {
      expect(container.textContent).toContain('Idea generation degraded');
    });
    expect(container.textContent).toContain('morning_brief');
    expect(container.textContent).toContain('refresh_waiting_user');
  });

  it('clears a stale error once a refresh succeeds', async () => {
    let fail = true;
    stubFetch((url, init) => {
      if (url.includes('/api/ideas/refresh') && init?.method === 'POST') {
        if (fail) return { status: 503, body: { detail: 'IdeasEngine not initialised' } };
        return { status: 200, body: { success: true, degraded: {}, new_ideas: [], today: [] } };
      }
      return { status: 200, body: { ideas: [], count: 0 } };
    });

    const { getByLabelText, container } = renderPane();
    await waitFor(() => expect(container.textContent).toContain('Nothing to suggest yet'));
    await act(async () => { fireEvent.click(getByLabelText('Refresh ideas')); });
    await waitFor(() => expect(container.textContent).toContain('IdeasEngine not initialised'));

    fail = false;
    await act(async () => { fireEvent.click(getByLabelText('Refresh ideas')); });
    await waitFor(() => {
      expect(container.textContent).not.toContain('IdeasEngine not initialised');
    });
  });
});

describe('ForYouToday: a failed accept says so', () => {
  it('keeps the row and names the failure when the accept POST 404s', async () => {
    stubFetch((url, init) => {
      if (url.includes('/api/routines') && init?.method === 'POST') {
        return { status: 200, body: { ok: true } };
      }
      if (url.includes('/accept')) {
        return { status: 404, body: { detail: 'Unknown idea id i1' } };
      }
      return { status: 200, body: { ideas: [IDEA], count: 1 } };
    });

    const { findByTestId, container } = renderPane();
    const btn = await findByTestId('foryou-accept-i1');
    await act(async () => { fireEvent.click(btn); });

    await waitFor(() => {
      expect(container.textContent).toContain('Accept failed');
    });
    expect(container.textContent).toContain('Unknown idea id i1');
    // The row must still be there. The idea was not accepted.
    expect(container.querySelector('[data-testid="foryou-accept-i1"]')).toBeTruthy();
  });

  it('does not swallow a failed install_routine and call it accepted', async () => {
    stubFetch((url, init) => {
      if (url.includes('/api/routines') && init?.method === 'POST') {
        return { status: 500, body: { detail: 'scheduler not initialised' } };
      }
      if (url.includes('/accept')) {
        // Would succeed, but we must never reach it.
        return { status: 200, body: { success: true } };
      }
      return { status: 200, body: { ideas: [IDEA], count: 1 } };
    });

    const { findByTestId, container } = renderPane();
    const btn = await findByTestId('foryou-accept-i1');
    await act(async () => { fireEvent.click(btn); });

    await waitFor(() => {
      expect(container.textContent).toContain('Accept failed');
    });
    expect(container.textContent).toContain('scheduler not initialised');
    expect(container.querySelector('[data-testid="foryou-accept-i1"]')).toBeTruthy();
  });

  it('keeps the row and names the failure when the dismiss POST fails', async () => {
    stubFetch((url) => {
      if (url.includes('/dismiss')) {
        return { status: 503, body: { detail: 'IdeasEngine not initialised' } };
      }
      return { status: 200, body: { ideas: [IDEA], count: 1 } };
    });

    const { findByTestId, container } = renderPane();
    const btn = await findByTestId('foryou-dismiss-i1');
    await act(async () => { fireEvent.click(btn); });

    await waitFor(() => {
      expect(container.textContent).toContain('Dismiss failed');
    });
    expect(container.querySelector('[data-testid="foryou-dismiss-i1"]')).toBeTruthy();
  });
});
