/**
 * Home: the brain dies mid-session.
 *
 * `useSystemHealth` deliberately retains the last good `/api/dashboard`
 * payload when a poll fails (hooks/useSystemHealth.js, the `catch` in
 * `tick`). Home then mirrored that store with
 *
 *     if (sysHealth.data) { setDashboard(...); setDashboardError(null); }
 *     else if (sysHealth.error) { setDashboardError(...); }
 *
 * and because `sysHealth.data` stays truthy forever after the first
 * success, the `else if` was unreachable from the second poll onward.
 * `dashboardError` was pinned to null, `dashboardOk` was pinned to
 * true, and the documented three-signal offline contract (WS closed
 * AND /health failed AND /api/dashboard failed) could not evaluate to
 * `offline` no matter what the brain did. A stopped brain read
 * "reconnecting…" forever, and the Devices tile went on painting a
 * pulsing green dot off the frozen payload.
 *
 * These cases pin the fixed behaviour: the error is read FIRST, the
 * retained numbers are marked stale rather than presented as live, and
 * `lastDashboardAt` finally reaches the screen.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { act, fireEvent, render, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { DEFAULT_FETCH_BODY, StubWebSocket } from '../_helpers/renderV2';
import Home from '../../pages/Home';
import {
  _resetSystemHealthForTesting,
  refreshSystemHealth,
} from '../../hooks/useSystemHealth';
import {
  _getSharedSocketForTesting,
  _resetSharedSocketForTesting,
} from '../../hooks/useFeralSocket';

const DASHBOARD = {
  devices: [],
  device_count: 3,
  online_count: 3,
  paired_count: 3,
  paired_offline_count: 0,
  subdevices_total: 0,
  subdevices_live: 0,
  session_count: 1,
  health: {},
  memory: {},
  skills_count: 4,
  somatic: {},
};

// The shared helper's fetch stub hardcodes `ok: true`, which cannot
// express "the brain stopped answering". This one can.
let failing = false;

function installFetch() {
  vi.stubGlobal('fetch', vi.fn((input) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    if (failing) {
      return Promise.resolve({
        ok: false,
        status: 503,
        statusText: 'Service Unavailable',
        json: () => Promise.resolve({ detail: 'brain unreachable' }),
        text: () => Promise.resolve('{"detail":"brain unreachable"}'),
        headers: new Map(),
      });
    }
    const body = url.includes('/api/dashboard') ? DASHBOARD : DEFAULT_FETCH_BODY;
    return Promise.resolve({
      ok: true,
      status: 200,
      statusText: 'OK',
      json: () => Promise.resolve(body),
      text: () => Promise.resolve(JSON.stringify(body)),
      headers: new Map(),
    });
  }));
}

function renderHome() {
  installFetch();
  StubWebSocket.instances = [];
  vi.stubGlobal('WebSocket', StubWebSocket);
  if (!window.matchMedia) {
    window.matchMedia = vi.fn().mockImplementation((q) => ({
      matches: false, media: q, onchange: null,
      addEventListener: vi.fn(), removeEventListener: vi.fn(),
      addListener: vi.fn(), removeListener: vi.fn(), dispatchEvent: vi.fn(),
    }));
  }
  return render(<MemoryRouter><Home /></MemoryRouter>);
}

beforeEach(() => {
  failing = false;
  _resetSystemHealthForTesting();
  _resetSharedSocketForTesting();
});

afterEach(() => {
  _resetSharedSocketForTesting();
  vi.unstubAllGlobals();
});

/**
 * The Devices hero tile. Located by its visible "Devices" label rather
 * than by test id, so this file's assertions land on the rendered
 * behaviour on any revision of Home.jsx rather than on the presence of
 * an attribute this change happened to add.
 */
function findDevicesTile(container) {
  const label = [...container.querySelectorAll('.v2-stat-label')]
    .find((el) => el.textContent === 'Devices');
  return label?.parentElement?.querySelector('.v2-stat-value') ?? null;
}

/** ...once the first poll has painted it. */
async function devicesTile(container) {
  return waitFor(() => {
    const el = findDevicesTile(container);
    if (!el || !el.textContent.includes('3')) {
      throw new Error(`devices tile not seeded yet: ${el?.textContent}`);
    }
    return el;
  });
}

describe('Home stale dashboard after a prior success', () => {
  it('marks the dashboard stale and stops the Devices tile pulsing green', async () => {
    const { container } = renderHome();

    // First poll succeeds: 3 of 3 devices online, live dot, pulsing.
    const tile = await devicesTile(container);
    expect(tile.textContent).toContain('3');
    let dot = tile.querySelector('.v2-dot');
    expect(dot.className).toContain('v2-dot--live');
    expect(dot.className).toContain('is-pulse');

    // Brain stops. The store keeps the payload and reports the error.
    failing = true;
    await act(async () => { await refreshSystemHealth(); });

    // The retained numbers are still on screen (that is the point of
    // retaining them), but nothing may claim they are current.
    await waitFor(() => {
      const d = findDevicesTile(container).querySelector('.v2-dot');
      expect(d.className).not.toContain('v2-dot--live');
      expect(d.className).not.toContain('is-pulse');
    });
    dot = findDevicesTile(container).querySelector('.v2-dot');
    expect(dot.className).toContain('v2-dot--warn');

    // And the hero stat is no longer allowed to say the brain is fine.
    const brain = container.querySelector('[data-testid="v2-home-brain-stat"]');
    expect(brain.textContent).not.toMatch(/online/);
  });

  it('renders when the payload was last read, before and after it goes stale', async () => {
    // `lastDashboardAt` was computed at two call sites and rendered at
    // none, so a page frozen on a cached payload gave the operator no
    // way to judge how old the numbers were. Same "As of HH:MM:SS"
    // shape components/ConnectedHardware.jsx already ships.
    const { container } = renderHome();
    await devicesTile(container);

    const stamp = () => container.querySelector('[data-testid="v2-home-dashboard-stamp"]');
    await waitFor(() => expect(stamp()?.textContent ?? '').toMatch(/^As of \d/));

    failing = true;
    await act(async () => { await refreshSystemHealth(); });

    await waitFor(() => {
      expect(stamp().textContent).toMatch(/^Stale\./);
      expect(stamp().textContent).toMatch(/Last read from the brain at \d/);
    });
  });

  it('reaches the documented offline state when all three signals fail', async () => {
    const { container } = renderHome();
    await devicesTile(container);

    // Signal 1 + 2: /api/dashboard and /health both stop answering.
    // The Refresh button re-runs both probes on demand.
    failing = true;
    await act(async () => { await refreshSystemHealth(); });
    await act(async () => {
      fireEvent.click(container.querySelector('[aria-label="Refresh"]'));
      await Promise.resolve();
    });

    // Signal 3: the WebSocket closes. `stopped` suppresses the
    // reconnect timer so the assertion is not racing a re-open.
    await act(async () => {
      const sock = _getSharedSocketForTesting();
      sock.stopped = true;
      sock.ws?.onclose?.({});
    });

    await waitFor(() => {
      const brain = container.querySelector('[data-testid="v2-home-brain-stat"]');
      expect(brain.textContent.trim()).toBe('offline');
      const dot = brain.querySelector('.v2-dot');
      expect(dot.className).toContain('v2-dot--off');
      expect(dot.className).not.toContain('is-pulse');
    });
  });
});
