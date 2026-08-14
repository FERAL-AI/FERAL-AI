/**
 * THE STANDING RULE, and the template for testing it.
 *
 * A failed fetch must never render an affirmative negative. "We asked
 * the brain and there is nothing" and "we could not ask the brain" are
 * different claims and must look different on screen.
 *
 * Before this suite, every page in the client wrote its fetch as
 * `.catch(() => setThings([]))` (or omitted the catch entirely, which
 * has the same effect because the initial state is already `[]`), and
 * then rendered an empty state off `things.length === 0`. So with the
 * brain unreachable the client asserted "No anomalies detected" on a
 * health surface, "No devices paired yet" next to a "Pair your first
 * device" button, and "No skills loaded / Check the Brain boot log".
 * The global ErrorToast auto-dismisses after 6 seconds; the false
 * empty state stayed on screen forever.
 *
 * How to extend this file when you add a surface that fetches:
 *
 *   1. Render it with `renderFailing(...)`, which stubs `fetch` as a
 *      500, a network drop, or a 401. All three are covered below.
 *   2. Assert `expectNoAffirmativeNegative(container)`: no sentence
 *      that states a quantity the client never learned.
 *   3. Assert an <ErrorState /> rendered: `v2-error-state`.
 *   4. Keep the matching success-path test that proves the genuine
 *      empty state still renders. A page that shows an error when the
 *      brain truthfully returned nothing is the same bug mirrored.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, waitFor, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { StubWebSocket } from './_helpers/renderV2';
import { clearAllGlobalErrors } from '../hooks/useGlobalErrors';
import { _resetSystemHealthForTesting } from '../hooks/useSystemHealth';

import Health from '../pages/Health';
import Devices from '../pages/Devices';
import Skills from '../pages/Skills';
import GlassBrain from '../pages/GlassBrain';
import HubLauncher from '../components/HubLauncher';
import ConsciousnessMindMap from '../components/ConsciousnessMindMap';

// ── Failure fixtures ────────────────────────────────────────────

/** The brain answered, badly. */
export function httpFailure(status = 500, body = { error: 'brain is on fire' }) {
  return vi.fn(() => Promise.resolve({
    ok: false,
    status,
    statusText: 'Error',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
    headers: new Map(),
    clone() { return this; },
  }));
}

/** The brain never answered. No server, wrong port, laptop asleep. */
export function networkFailure() {
  return vi.fn(() => Promise.reject(new TypeError('Failed to fetch')));
}

function renderFailing(ui, { fetchImpl = httpFailure(), route = '/' } = {}) {
  vi.stubGlobal('fetch', fetchImpl);
  StubWebSocket.instances = [];
  vi.stubGlobal('WebSocket', StubWebSocket);
  return render(<MemoryRouter initialEntries={[route]}>{ui}</MemoryRouter>);
}

/** Success fixture, so the genuine empty states stay covered. */
function renderEmptyOk(ui, { body = {}, route = '/' } = {}) {
  vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({
    ok: true,
    status: 200,
    statusText: 'OK',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
    headers: new Map(),
    clone() { return this; },
  })));
  StubWebSocket.instances = [];
  vi.stubGlobal('WebSocket', StubWebSocket);
  return render(<MemoryRouter initialEntries={[route]}>{ui}</MemoryRouter>);
}

// Sentences that assert a quantity. None of them may appear when the
// client never received one.
const AFFIRMATIVE_NEGATIVES = [
  /no devices/i,
  /no anomalies/i,
  /no events/i,
  /no skills/i,
  /no metrics/i,
  /no vitals/i,
  /no active sources/i,
  /no in-flight/i,
  /pair your first device/i,
  /pair a device/i,
  /awaiting node/i,
];

function expectNoAffirmativeNegative(container) {
  const text = container.textContent || '';
  for (const re of AFFIRMATIVE_NEGATIVES) {
    expect(
      re.test(text),
      `A failed fetch rendered the affirmative negative ${re}.\nRendered: ${text.slice(0, 400)}`,
    ).toBe(false);
  }
}

function expectErrorState(container) {
  const nodes = container.querySelectorAll('[data-testid="v2-error-state"]');
  expect(nodes.length, 'expected at least one <ErrorState />').toBeGreaterThan(0);
  // Every ErrorState spells out the distinction in plain language.
  expect(container.textContent).toMatch(/not an empty result/i);
  return nodes[0];
}

beforeEach(() => {
  clearAllGlobalErrors();
  _resetSystemHealthForTesting();
});

afterEach(() => {
  vi.unstubAllGlobals();
  _resetSystemHealthForTesting();
});

// ── Health ──────────────────────────────────────────────────────
// The worst instance: this is a health surface, and a dropped request
// used to tell the user their vitals were fine.

describe('Health: failed fetch', () => {
  async function openTab(getByRole, container, name) {
    fireEvent.click(getByRole('tab', { name }));
    await waitFor(() => {
      expect(container.querySelector('[data-testid="v2-error-state"]')).not.toBeNull();
    });
  }

  it('Summary does not report "Metrics tracked 0 / Recent alerts 0"', async () => {
    const { container } = renderFailing(<Health />);
    await waitFor(() => expectErrorState(container));
    expectNoAffirmativeNegative(container);
    // The three stat tiles are gone entirely; a zero is a measurement.
    expect(container.textContent).not.toMatch(/Metrics tracked/i);
  });

  it('Alerts does not report "No anomalies detected"', async () => {
    const { container, getByRole } = renderFailing(<Health />);
    await openTab(getByRole, container, /Alerts/i);
    expectNoAffirmativeNegative(container);
    expectErrorState(container);
    // And no fabricated count in the pane title.
    expect(container.textContent).not.toMatch(/Alerts \(0\)/);
  });

  it('Metrics does not report "No metrics yet"', async () => {
    const { container, getByRole } = renderFailing(<Health />);
    await openTab(getByRole, container, /Metrics/i);
    expectNoAffirmativeNegative(container);
    expectErrorState(container);
  });

  it('Today does not report "No vitals yet" or "No active sources"', async () => {
    const { container, getByRole } = renderFailing(<Health />);
    await openTab(getByRole, container, /Today/i);
    expectNoAffirmativeNegative(container);
    // Both panes fail independently, so both say so.
    expect(
      container.querySelectorAll('[data-testid="v2-error-state"]').length,
    ).toBeGreaterThanOrEqual(2);
  });

  it('still renders the genuine empty states when the brain answers with nothing', async () => {
    const { container, getByRole } = renderEmptyOk(<Health />, {
      body: { alerts: [], metrics: [], devices: [], data: {} },
    });
    fireEvent.click(getByRole('tab', { name: /Alerts/i }));
    await waitFor(() => {
      expect(container.textContent).toMatch(/No anomalies detected/i);
    });
    expect(container.querySelector('[data-testid="v2-error-state"]')).toBeNull();
  });
});

// ── Devices ─────────────────────────────────────────────────────
// `Promise.allSettled` never rejects, so the page's catch block was
// dead code and `setError(null)` ran on every tick, including the one
// where all four calls had just failed.

describe('Devices: failed fetch', () => {
  it('does not report "No devices paired yet" or offer "Pair your first device"', async () => {
    const { container } = renderFailing(<Devices />);
    await waitFor(() => expectErrorState(container));
    expectNoAffirmativeNegative(container);
  });

  it('says the same thing when the brain is simply unreachable', async () => {
    const { container } = renderFailing(<Devices />, { fetchImpl: networkFailure() });
    await waitFor(() => expectErrorState(container));
    expectNoAffirmativeNegative(container);
    expect(container.textContent).toMatch(/could not reach the brain/i);
  });

  it('still renders the genuine empty state when the brain answers with nothing', async () => {
    const { container } = renderEmptyOk(<Devices />, {
      body: { devices: [], offline: [], nodes: [] },
    });
    await waitFor(() => {
      expect(container.textContent).toMatch(/No devices paired yet/i);
    });
    expect(container.querySelector('[data-testid="v2-error-state"]')).toBeNull();
  });
});

// ── Skills ──────────────────────────────────────────────────────

describe('Skills: failed fetch', () => {
  it('does not report "No skills loaded / Check the Brain boot log"', async () => {
    const { container } = renderFailing(<Skills />);
    await waitFor(() => expectErrorState(container));
    expectNoAffirmativeNegative(container);
    expect(container.textContent).not.toMatch(/Check the Brain boot log/i);
    // The count in the pane title is a measurement too.
    expect(container.textContent).not.toMatch(/Skills \(0\)/);
  });

  it('still renders the genuine empty state when the brain has no skills', async () => {
    const { container } = renderEmptyOk(<Skills />, { body: { skills: [], pending: [] } });
    await waitFor(() => {
      expect(container.textContent).toMatch(/No skills loaded/i);
    });
    expect(container.querySelector('[data-testid="v2-error-state"]')).toBeNull();
  });
});

// ── HubLauncher ─────────────────────────────────────────────────
// The comment in the file records that this exact CTA lying was a
// previous audit finding. The failure path reintroduced it.

describe('HubLauncher: failed fetch', () => {
  it('shows neither pair CTA when the device counts could not be read', async () => {
    const { container } = renderFailing(<HubLauncher open onClose={() => {}} />);
    // Let the failing probe settle before asserting.
    await waitFor(() => {
      expect(container.querySelector('.v2-hub-grid')).not.toBeNull();
    });
    await Promise.resolve();
    expectNoAffirmativeNegative(container);
    expect(container.querySelector('.v2-hub-cta')).toBeNull();
  });

  it('still shows the pair CTA when the brain really reports zero paired', async () => {
    const { container } = renderEmptyOk(<HubLauncher open onClose={() => {}} />, {
      body: { paired_count: 0, online_count: 0, device_count: 0 },
    });
    await waitFor(() => {
      expect(container.textContent).toMatch(/Pair a device/i);
    });
  });
});

// ── ConsciousnessMindMap + GlassBrain ───────────────────────────

describe('ConsciousnessMindMap: failed fetch', () => {
  it('does not report "No in-flight consciousness entities."', async () => {
    const { container } = renderFailing(<ConsciousnessMindMap />);
    await waitFor(() => expectErrorState(container));
    expectNoAffirmativeNegative(container);
  });

  it('distinguishes 401 from a server fault', async () => {
    const { container } = renderFailing(<ConsciousnessMindMap />, {
      fetchImpl: httpFailure(401, { detail: 'missing api key' }),
    });
    const node = await waitFor(() => expectErrorState(container));
    expect(node.getAttribute('data-kind')).toBe('auth');
    expect(container.textContent).toMatch(/401/);
    expect(container.textContent).toMatch(/Settings/i);
  });

  it('still renders the genuine empty state when the store is idle', async () => {
    const { container } = renderEmptyOk(<ConsciousnessMindMap />, { body: { entities: [] } });
    await waitFor(() => {
      expect(container.textContent).toMatch(/No in-flight consciousness entities/i);
    });
    expect(container.querySelector('[data-testid="v2-error-state"]')).toBeNull();
  });
});

describe('GlassBrain: failed fetch', () => {
  it('reports the in-flight count as unknown rather than 0', async () => {
    const { container } = renderFailing(<GlassBrain />);
    await waitFor(() => expectErrorState(container));
    const tile = Array.from(container.querySelectorAll('.v2-glass-brain-vital'))
      .find((el) => /In-flight/.test(el.textContent));
    expect(tile).toBeTruthy();
    expect(tile.textContent).toMatch(/unknown/i);
    expect(tile.textContent).not.toMatch(/In-flight0/);
  });
});
