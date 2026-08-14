/**
 * Oversight — supervisor audit log + kill switch.
 *
 * The page does three things:
 *   1. Lists /api/supervisor/events with filter chips.
 *   2. Shows /api/supervisor/stats in pill form (total + paused flag).
 *   3. Toggles the kill switch via POST /api/supervisor/pause.
 *
 * These tests exercise all three with honest fetch mocks — no shortcuts.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { act, fireEvent, render, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { installFetchMock, StubWebSocket } from '../_helpers/renderV2';
import { renderV2 } from '../_helpers/renderV2';
import Oversight from '../../pages/Oversight';

const navigateMock = vi.fn();
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom');
  return {
    ...actual,
    useNavigate: () => navigateMock,
  };
});

function renderWithHistory(ui, { entries = ['/glass-brain', '/oversight'], index = 1, fetch } = {}) {
  installFetchMock(fetch);
  vi.stubGlobal('WebSocket', StubWebSocket);
  return render(
    <MemoryRouter initialEntries={entries} initialIndex={index}>
      {ui}
    </MemoryRouter>,
  );
}

const baseEvents = [
  {
    event_id: 'e1',
    ts: Date.now() / 1000,
    source: 'web',
    kind: 'handle_command',
    session_id: 'sess-12345678',
    actor: 'user',
    payload_summary: 'hello there',
    payload_hash: 'hash1',
    decision: 'allowed',
    latency_ms: 42,
  },
  {
    event_id: 'e2',
    ts: Date.now() / 1000 - 5,
    source: 'cron',
    kind: 'handle_command',
    session_id: 'routine-42',
    actor: 'system',
    payload_summary: 'morning briefing',
    payload_hash: 'hash2',
    decision: 'denied',
    latency_ms: 1,
  },
  {
    event_id: 'e3',
    ts: Date.now() / 1000 - 10,
    source: 'twin',
    kind: 'twin_action',
    session_id: '',
    actor: 'twin',
    payload_summary: 'draft email to sam',
    payload_hash: 'hash3',
    decision: 'queued',
    latency_ms: 5,
  },
];

const baseStats = {
  total: 3,
  by_source: { web: 1, cron: 1, twin: 1 },
  paused: false,
};

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  navigateMock.mockClear();
});
afterEach(() => {
  vi.useRealTimers();
});

function makeResponder({ events = baseEvents, stats = baseStats } = {}) {
  return (url) => {
    if (url.includes('/api/supervisor/events')) {
      return { count: events.length, events };
    }
    if (url.includes('/api/supervisor/stats')) {
      return stats;
    }
    return {};
  };
}

/**
 * The shared installFetchMock always answers 200, which cannot express the
 * cases that matter for a safety control: the Supervisor route raises 503
 * ("Supervisor not initialised") and the POST answers with the real paused
 * flag rather than the one that was requested. This mock takes
 * `{status, body}` per URL so both are reachable.
 */
function installStatusFetchMock(responder) {
  vi.stubGlobal(
    'fetch',
    vi.fn((input, init) => {
      const url = typeof input === 'string' ? input : input?.url || '';
      const { status = 200, body = {} } = responder(url, init) || {};
      const text = () => Promise.resolve(JSON.stringify(body));
      return Promise.resolve({
        ok: status >= 200 && status < 300,
        status,
        statusText: String(status),
        json: () => Promise.resolve(body),
        text,
        clone: () => ({ text }),
        headers: new Map(),
      });
    }),
  );
}

function renderWithStatuses(responder) {
  installStatusFetchMock(responder);
  StubWebSocket.instances = [];
  vi.stubGlobal('WebSocket', StubWebSocket);
  if (!window.matchMedia) {
    window.matchMedia = vi.fn().mockImplementation((q) => ({
      matches: false, media: q, onchange: null,
      addEventListener: vi.fn(), removeEventListener: vi.fn(),
      addListener: vi.fn(), removeListener: vi.fn(), dispatchEvent: vi.fn(),
    }));
  }
  return render(
    <MemoryRouter initialEntries={['/oversight']}>
      <Oversight />
    </MemoryRouter>,
  );
}

const SUPERVISOR_503 = { status: 503, body: { detail: 'Supervisor not initialised' } };

/** Text of the "Paused" stat pill, label included: "Pausedyes" / "Pausedno". */
function pausedPillText(getByText) {
  return getByText('Paused').closest('.v2-oversight-stat').textContent;
}

describe('Oversight page', () => {
  it('renders header + event rows', async () => {
    const { findByText, getByText } = renderV2(<Oversight />, {
      fetch: makeResponder(),
    });
    expect(getByText(/Oversight/i)).toBeInTheDocument();
    expect(await findByText('hello there')).toBeInTheDocument();
    expect(await findByText('morning briefing')).toBeInTheDocument();
    expect(await findByText('draft email to sam')).toBeInTheDocument();
  });

  it('shows kill switch label based on paused state', async () => {
    const { findByRole, rerender } = renderV2(<Oversight />, {
      fetch: makeResponder({ stats: { ...baseStats, paused: false } }),
    });
    const btn = await findByRole('button', { name: /Pause actions/i });
    expect(btn).toBeInTheDocument();

    vi.unstubAllGlobals();
    const { findByRole: findR2 } = renderV2(<Oversight />, {
      fetch: makeResponder({ stats: { ...baseStats, paused: true } }),
    });
    expect(await findR2('button', { name: /Resume/i })).toBeInTheDocument();
  });

  it('displays stat pills for total + per-source counts', async () => {
    const { findAllByText } = renderV2(<Oversight />, {
      fetch: makeResponder(),
    });
    // Total pill
    expect((await findAllByText('3')).length).toBeGreaterThanOrEqual(1);
    // At least one per-source pill (web/cron/twin)
    expect((await findAllByText(/^web$/)).length).toBeGreaterThanOrEqual(1);
  });

  it('renders empty-state chip when events list is empty and not loading', async () => {
    const { findByText } = renderV2(<Oversight />, {
      fetch: makeResponder({ events: [] }),
    });
    // "No events match this filter" is the empty state title.
    expect(await findByText(/No events match this filter/i)).toBeInTheDocument();
  });

  it('renders a leading Back button in the page header', () => {
    const { getByRole } = renderV2(<Oversight />, { fetch: makeResponder() });
    expect(getByRole('button', { name: /Back/i })).toBeInTheDocument();
  });

  it('clicking Back calls navigate(-1) when there is in-app history', () => {
    const { getByRole } = renderWithHistory(<Oversight />, {
      fetch: makeResponder(),
    });
    fireEvent.click(getByRole('button', { name: /Back/i }));
    expect(navigateMock).toHaveBeenCalledWith(-1);
  });

  it('clicking Back falls back to /glass-brain on a deep-linked open', () => {
    const { getByRole } = renderWithHistory(<Oversight />, {
      entries: ['/oversight'],
      index: 0,
      fetch: makeResponder(),
    });
    fireEvent.click(getByRole('button', { name: /Back/i }));
    expect(navigateMock).toHaveBeenCalledWith('/glass-brain');
  });
});

describe('Oversight kill switch honesty', () => {
  it('does not claim the Brain is paused when the pause request 503s', async () => {
    const { findByRole, getByRole, getByText, queryByRole, findByTestId } = renderWithStatuses((url) => {
      if (url.includes('/api/supervisor/pause')) return SUPERVISOR_503;
      if (url.includes('/api/supervisor/events')) {
        return { status: 200, body: { count: baseEvents.length, events: baseEvents } };
      }
      if (url.includes('/api/supervisor/stats')) return { status: 200, body: baseStats };
      return { status: 200, body: {} };
    });

    const btn = await findByRole('button', { name: /Pause actions/i });
    await act(async () => { fireEvent.click(btn); });

    // The control rolls back: nothing paused, so nothing says paused.
    expect(getByRole('button', { name: /Pause actions/i })).toBeInTheDocument();
    expect(queryByRole('button', { name: /Resume/i })).toBeNull();
    expect(pausedPillText(getByText)).toContain('no');

    const alert = await findByTestId('oversight-control-error');
    expect(alert.textContent).toMatch(/was not applied/i);
    expect(alert.textContent).toMatch(/Supervisor is unreachable/i);
  });

  it('follows the paused value the server returns, not the optimistic guess', async () => {
    // /stats is down, so the 4 s poll cannot quietly correct the control:
    // the POST response body is the only truth in this test. The server
    // answers `paused: false` to a request that asked for `true`.
    const { findByRole, getByRole, getByText, queryByRole } = renderWithStatuses((url) => {
      if (url.includes('/api/supervisor/pause')) return { status: 200, body: { paused: false } };
      if (url.includes('/api/supervisor/events')) return { status: 200, body: { count: 0, events: [] } };
      if (url.includes('/api/supervisor/stats')) return SUPERVISOR_503;
      return { status: 200, body: {} };
    });

    const btn = await findByRole('button', { name: /Pause actions/i });
    await act(async () => { fireEvent.click(btn); });

    expect(getByRole('button', { name: /Pause actions/i })).toBeInTheDocument();
    expect(queryByRole('button', { name: /Resume/i })).toBeNull();
    expect(pausedPillText(getByText)).toContain('no');
  });

  it('applies the pause the server confirms', async () => {
    const { findByRole, getByText } = renderWithStatuses((url) => {
      if (url.includes('/api/supervisor/pause')) return { status: 200, body: { paused: true } };
      if (url.includes('/api/supervisor/events')) return { status: 200, body: { count: 0, events: [] } };
      if (url.includes('/api/supervisor/stats')) return SUPERVISOR_503;
      return { status: 200, body: {} };
    });

    const btn = await findByRole('button', { name: /Pause actions/i });
    await act(async () => { fireEvent.click(btn); });

    expect(await findByRole('button', { name: /Resume/i })).toBeInTheDocument();
    expect(pausedPillText(getByText)).toContain('yes');
  });

  it('flags the paused pill as unconfirmed when the stats fetch fails', async () => {
    const { findByTestId } = renderWithStatuses((url) => {
      if (url.includes('/api/supervisor/events')) {
        return { status: 200, body: { count: baseEvents.length, events: baseEvents } };
      }
      if (url.includes('/api/supervisor/stats')) return SUPERVISOR_503;
      return { status: 200, body: {} };
    });

    const alert = await findByTestId('oversight-control-error');
    expect(alert.textContent).toMatch(/not confirmed/i);
    expect(alert.textContent).toMatch(/Supervisor is unreachable/i);
  });
});

describe('Oversight audit log honesty', () => {
  it('shows an unreachable-Supervisor error, not "no events", when the fetch fails', async () => {
    const { findByTestId, queryByText } = renderWithStatuses((url) => {
      if (url.includes('/api/supervisor/events')) return SUPERVISOR_503;
      if (url.includes('/api/supervisor/stats')) return { status: 200, body: baseStats };
      return { status: 200, body: {} };
    });

    const box = await findByTestId('oversight-events-error');
    expect(box.textContent).toMatch(/Audit log unavailable/i);
    expect(box.textContent).toMatch(/Supervisor is unreachable/i);
    expect(queryByText(/No events match this filter/i)).toBeNull();
  });

  it('still renders the plain empty state when the fetch succeeds with no rows', async () => {
    const { findByText, queryByTestId } = renderWithStatuses((url) => {
      if (url.includes('/api/supervisor/events')) return { status: 200, body: { count: 0, events: [] } };
      if (url.includes('/api/supervisor/stats')) return { status: 200, body: baseStats };
      return { status: 200, body: {} };
    });

    expect(await findByText(/No events match this filter/i)).toBeInTheDocument();
    await waitFor(() => expect(queryByTestId('oversight-events-error')).toBeNull());
  });
});
