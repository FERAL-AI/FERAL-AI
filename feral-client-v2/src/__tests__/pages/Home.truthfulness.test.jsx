/**
 * Home page truthfulness contract (Phase 1).
 *
 * Pins the post-fix behaviour for the hero "Brain" stat card and the
 * new "Subdevices" tile. Before this PR the Brain card was a literal
 * `<StatusDot tone="live" pulse /> online` regardless of real state —
 * the operator caught it; the audit-r6 / r7 sweeps confirmed it. The
 * tests below render the page against a mocked /api/dashboard and
 * verify the strings that ship to the user.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { waitFor } from '@testing-library/react';
import { renderV2 } from '../_helpers/renderV2';
import Home from '../../pages/Home';
import { _getSharedSocketForTesting } from '../../hooks/useFeralSocket';
import { _resetSystemHealthForTesting } from '../../hooks/useSystemHealth';

// AUDIT-r14 Lane 12 — Home + GlassBrain + Shell now share a single
// module-level useSystemHealth store. That cache is correct in
// production but leaks across the 6 cases below, since each test
// renders Home with a *different* dashboardFetch() mock. Without a
// reset, the previous case's snapshot (e.g. {subdevices_total:1,
// subdevices_live:1}) would briefly paint into the next case's tile
// before the new fetch lands. Linux CI under v8 coverage was the
// canary — its scheduler interleaved the prior-test promise with the
// new test's render, so the failing assertions read ' 1/1' instead of
// the case's expected '0/2' / 'unavailable'. Reset the store and the
// fetch-mock counter at the start of every case so each test sees a
// clean slate.
beforeEach(() => {
  _resetSystemHealthForTesting();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function dashboardWith(extra = {}) {
  return {
    devices: [],
    device_count: 0,
    online_count: 0,
    paired_count: 0,
    paired_offline_count: 0,
    subdevices_total: 0,
    subdevices_live: 0,
    channels: [],
    session_count: 0,
    health: {},
    memory: {},
    skills_count: 0,
    llm_available: false,
    audio_available: false,
    sync: {},
    wasm_available: false,
    wake_word_enabled: false,
    taskflows: {},
    boot: {},
    demo: false,
    is_demo_mode: false,
    somatic: {},
    ...extra,
  };
}

function dashboardFetch(extra) {
  return (url) => {
    if (url.includes('/api/dashboard')) return dashboardWith(extra);
    return null; // falls through to DEFAULT_FETCH_BODY
  };
}

describe('Home — Phase 1 truthfulness sweep', () => {
  it('hero Brain stat is no longer hardcoded online — it has a binding', async () => {
    // The exact label depends on the WS state machine, which our
    // StubWebSocket auto-opens; we mostly assert that a measurable
    // binding exists. The previous code would have shipped the
    // literal "online" string regardless of any input — we pin
    // against that regression here.
    const { container } = renderV2(<Home />, {
      fetch: dashboardFetch({}),
    });
    const brainStat = await waitFor(() => {
      const el = container.querySelector('[data-testid="v2-home-brain-stat"]');
      if (!el) throw new Error('brain stat tile not rendered');
      return el;
    });
    // Either online / reconnecting / offline — never one of those is
    // an acceptable answer; the hardcoded version always read "online".
    expect(brainStat.textContent.trim()).toMatch(/online|reconnecting|offline/);
    // The dot has a label attribute now (truthfulness contract).
    const dot = brainStat.querySelector('.v2-dot');
    expect(dot).toBeTruthy();
    expect(dot.getAttribute('aria-label') || '').toMatch(/Brain (online|reconnecting|offline)/);
  });

  it('subdevices stat tile is hidden when the brain reports zero subdevices ever', async () => {
    const { container } = renderV2(<Home />, {
      fetch: dashboardFetch({ subdevices_total: 0, subdevices_live: 0 }),
    });
    // Allow the initial /api/dashboard fetch to land.
    await waitFor(() => {
      const tile = container.querySelector('[data-testid="v2-home-subdevices-stat"]');
      // Truthful: when the brain has never seen a sub-device the tile
      // is omitted. Inventing a "0/0" zero-row would be a small lie
      // that drifts toward the kind of placeholder the audit caught.
      expect(tile).toBeNull();
    });
  });

  it('subdevices stat tile renders live / total when the brain reports any sub-device', async () => {
    const { container } = renderV2(<Home />, {
      fetch: dashboardFetch({ subdevices_total: 1, subdevices_live: 1 }),
    });
    const tile = await waitFor(() => {
      const el = container.querySelector('[data-testid="v2-home-subdevices-stat"]');
      if (!el) throw new Error('subdevices tile missing despite total > 0');
      return el;
    });
    expect(tile.textContent).toContain('1/1');
    const dot = tile.querySelector('.v2-dot');
    expect(dot).toBeTruthy();
    // 1 live → live tone with pulse animation.
    expect(dot.className).toContain('v2-dot--live');
    expect(dot.className).toContain('is-pulse');
  });

  it('subdevices tile downgrades to off when no subdevice is inside its heartbeat window', async () => {
    const { container } = renderV2(<Home />, {
      fetch: dashboardFetch({ subdevices_total: 2, subdevices_live: 0 }),
    });
    const tile = await waitFor(() => {
      const el = container.querySelector('[data-testid="v2-home-subdevices-stat"]');
      if (!el) throw new Error('subdevices tile missing despite total > 0');
      return el;
    });
    expect(tile.textContent).toContain('0/2');
    const dot = tile.querySelector('.v2-dot');
    // 0 live → off tone, no pulse. The previous hardcoded `tone="live"
    // pulse` would have failed this assertion.
    expect(dot.className).toContain('v2-dot--off');
    expect(dot.className).not.toContain('is-pulse');
  });

  it('subdevices tile updates in real time on a subdevice_update WS event', async () => {
    // Phase-1 validation pass Item 18: assert the Subdevices tile
    // mirrors the brain's truth-store via WS deltas, not just the
    // 15s /api/dashboard poll. We seed with 1/1 live, then push a
    // `subdevice_update` with `live: false` and verify the tile
    // flips off the pulsing dot WITHOUT advancing time past the
    // poll interval. The previous polling-only build would have
    // shown a stale "live" dot for up to ~15s after a glasses BLE
    // drop — exactly the lie this fix was added to kill.
    const seedSubdevice = {
      node_id: 'feral-iphone-abc',
      capability: 'jw_health_glasses',
      status: 'ready',
      provenance: 'ble',
      attrs: { device_name: 'TheoraGlasses-1234' },
      first_seen: 0,
      last_seen: Date.now() / 1000,
      live: true,
      liveness_window_s: 30,
    };
    const { container } = renderV2(<Home />, {
      fetch: dashboardFetch({
        subdevices_total: 1,
        subdevices_live: 1,
        devices: [
          {
            node_id: 'feral-iphone-abc',
            type: 'phone',
            connected: true,
            subdevices: [seedSubdevice],
          },
        ],
      }),
    });

    // First tile state: live + pulse from the seed.
    const tile = await waitFor(() => {
      const el = container.querySelector('[data-testid="v2-home-subdevices-stat"]');
      if (!el) throw new Error('subdevices tile not seeded from /api/dashboard');
      return el;
    });
    await waitFor(() => {
      expect(tile.textContent).toContain('1/1');
      expect(tile.querySelector('.is-pulse')).toBeTruthy();
    });

    // Push a delta: the same row but `live: false` (e.g. brain
    // sweep_stale derated it). Tile must flip OFF the pulsing dot
    // synchronously — no fake timers, no poll wait.
    //
    // FeralSocket is a process-wide singleton (the
    // `sharedSocket` in `hooks/useFeralSocket.js`). We grab it
    // via the test-only `_getSharedSocketForTesting` accessor and
    // fan the simulated `state_push` directly into the listener
    // set — that's the same path `socket.subscribe(fn)` would
    // hit if a real WS frame had arrived.
    const sock = await waitFor(() => {
      const s = _getSharedSocketForTesting();
      if (!s || s.listeners.size === 0) {
        throw new Error('shared socket / listener not yet wired');
      }
      return s;
    });
    const startTs = Date.now();
    const delta = {
      type: 'state_push',
      event: 'subdevice_update',
      data: { ...seedSubdevice, live: false, status: 'disconnected' },
    };
    sock.listeners.forEach((fn) => fn(delta));

    await waitFor(() => {
      expect(tile.textContent).toContain('0/1');
      const dot = tile.querySelector('.v2-dot');
      expect(dot.className).toContain('v2-dot--off');
      expect(dot.className).not.toContain('is-pulse');
    });
    // Real-time bound: must update inside 1s on a CI runner. The
    // production target is much tighter (~tens of ms) but vitest
    // jsdom + macrotask scheduling adds slop.
    expect(Date.now() - startTs).toBeLessThan(1000);
  });

  it('subdevices tile renders unavailable warning when brain reports subdevices_unavailable', async () => {
    // Phase-1 validation pass Item 6: when the brain's truth store
    // can't be read, the tile renders a real warn state instead of
    // silently displaying empty 0/0.
    const { container } = renderV2(<Home />, {
      fetch: dashboardFetch({
        subdevices_total: 0,
        subdevices_live: 0,
        subdevices_unavailable: 'OperationalError: database is locked',
      }),
    });
    const tile = await waitFor(() => {
      const el = container.querySelector('[data-testid="v2-home-subdevices-stat"]');
      if (!el) throw new Error('subdevices tile missing');
      return el;
    });
    expect(tile.textContent).toContain('unavailable');
    const dot = tile.querySelector('.v2-dot');
    expect(dot.className).toContain('v2-dot--warn');
  });
});
