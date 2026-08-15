/**
 * THE STANDING RULE, applied to the four automation surfaces.
 *
 * See `no-false-empty-states.test.jsx` for the rule itself and the
 * template this file follows. In short: a failed fetch must never
 * render an affirmative negative, because "we asked the brain and there
 * is nothing" and "we could not ask the brain" are different claims.
 *
 * The surfaces covered here are Flows, Intents, Agents and Forge. Every
 * one of them wrote its fetch in one of the four shapes that produce
 * the defect:
 *
 *   - `.catch(() => setX([]))`      Flows' skill picker
 *   - `.catch(() => setX({}))`      the three Stats tabs. The worst of
 *                                   the set, because a stats surface
 *                                   that substitutes an object the
 *                                   brain never sent is presenting the
 *                                   absence of a measurement as one.
 *   - `try { … } finally`           no catch at all, so the rejection
 *                                   escaped unhandled and the list
 *                                   stayed at its initial `[]`
 *   - `Promise.allSettled`          never rejects, so the surrounding
 *                                   catch was dead code and the
 *                                   `if (fulfilled)` guards had no else
 *
 * Each `describe` therefore has both halves: a failure test asserting
 * the affirmative negative is gone and an <ErrorState /> is present,
 * and a success test proving the genuine empty state still renders. A
 * page that cries error when the brain truthfully returned nothing is
 * the same bug mirrored.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, waitFor, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { StubWebSocket } from './_helpers/renderV2';
import { clearAllGlobalErrors } from '../hooks/useGlobalErrors';

import Flows from '../pages/Flows';
import Intents from '../pages/Intents';
import Agents from '../pages/Agents';
import Forge from '../pages/Forge';

// ── Failure fixtures ────────────────────────────────────────────

/** The brain answered, badly. */
function httpFailure(status = 500, body = { error: 'brain is on fire' }) {
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
function networkFailure() {
  return vi.fn(() => Promise.reject(new TypeError('Failed to fetch')));
}

function renderFailing(ui, { fetchImpl = httpFailure(), route = '/' } = {}) {
  vi.stubGlobal('fetch', fetchImpl);
  StubWebSocket.instances = [];
  vi.stubGlobal('WebSocket', StubWebSocket);
  return render(<MemoryRouter initialEntries={[route]}>{ui}</MemoryRouter>);
}

/**
 * Success fixture, so the genuine empty states stay covered. `body` may
 * be a plain object served to every path, or a function of the URL when
 * one pane needs a different shape from its neighbour.
 */
function renderOk(ui, { body = {}, route = '/' } = {}) {
  const resolve = typeof body === 'function' ? body : () => body;
  vi.stubGlobal('fetch', vi.fn((input) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    const payload = resolve(url) ?? {};
    return Promise.resolve({
      ok: true,
      status: 200,
      statusText: 'OK',
      json: () => Promise.resolve(payload),
      text: () => Promise.resolve(JSON.stringify(payload)),
      headers: new Map(),
      clone() { return this; },
    });
  }));
  StubWebSocket.instances = [];
  vi.stubGlobal('WebSocket', StubWebSocket);
  return render(<MemoryRouter initialEntries={[route]}>{ui}</MemoryRouter>);
}

/**
 * The stats panes are the reason `.catch(() => setStats({}))` mattered,
 * so they get asserted on structure rather than prose: the heading that
 * frames a grid as a completed measurement must not be on screen, and
 * neither must any tile. (The <ErrorState /> naturally names the thing
 * it failed to load, so a plain text match would find "Mitosis stats"
 * inside the error message itself.)
 */
function expectNoStatsClaim(container, heading) {
  expect(container.querySelectorAll('.v2-stat-value').length).toBe(0);
  const headings = Array.from(container.querySelectorAll('.v2-pane-title'))
    .map((n) => n.textContent);
  expect(headings).not.toContain(heading);
  expect(container.textContent).toMatch(/invented measurement/i);
}

// Sentences that assert a quantity, or the absence of one. None of them
// may appear when the client never received an answer.
const AFFIRMATIVE_NEGATIVES = [
  /no drafts pending/i,
  /no capability gaps/i,
  /nothing generated yet/i,
  /nothing planned for today/i,
  /no plans yet/i,
  /no first-party personas/i,
  /no specialists yet/i,
  /spawn first specialist/i,
  /no recurring patterns/i,
  /no flows yet/i,
  /create your first flow/i,
  /no first-party workflow packs/i,
  /no routines/i,
  /no automations/i,
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

/** Click a tab and wait for its pane to settle into the error branch. */
async function openFailingTab(getByRole, container, name) {
  fireEvent.click(getByRole('tab', { name }));
  await waitFor(() => {
    expect(container.querySelector('[data-testid="v2-error-state"]')).not.toBeNull();
  });
}

beforeEach(() => {
  clearAllGlobalErrors();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

// ── Forge ───────────────────────────────────────────────────────
// The priority instance: `Stats` used `.catch(() => setStats({}))`, so
// a dropped request moved the page out of its loading branch with a
// value the brain never sent and rendered the stats heading over a grid
// with no counters in it.

describe('Forge Stats: failed fetch', () => {
  it('renders no stat tiles and does not present the stats heading', async () => {
    const { container, getByRole } = renderFailing(<Forge />);
    await openFailingTab(getByRole, container, /Stats/i);
    expectNoStatsClaim(container, 'Tool Genesis stats');
    expectErrorState(container);
  });

  it('still renders every counter the brain does report', async () => {
    const { container, getByRole } = renderOk(<Forge />, {
      body: { pending: [], drafts_generated: 0, approved: 4 },
    });
    fireEvent.click(getByRole('tab', { name: /Stats/i }));
    await waitFor(() => {
      expect(container.textContent).toMatch(/Tool Genesis stats/i);
    });
    const values = Array.from(container.querySelectorAll('.v2-stat-value')).map((n) => n.textContent);
    // A zero the brain actually sent is a measurement and must survive.
    expect(values).toContain('0');
    expect(values).toContain('4');
    expect(container.querySelector('[data-testid="v2-error-state"]')).toBeNull();
  });

  it('says so when the brain answers with no counters at all', async () => {
    const { container, getByRole } = renderOk(<Forge />, { body: {} });
    fireEvent.click(getByRole('tab', { name: /Stats/i }));
    await waitFor(() => {
      expect(container.textContent).toMatch(/reports no Tool Genesis counters/i);
    });
    expect(container.querySelector('[data-testid="v2-error-state"]')).toBeNull();
  });
});

describe('Forge Pending: failed fetch', () => {
  it('does not report "No drafts pending" when neither queue answered', async () => {
    const { container } = renderFailing(<Forge />);
    await waitFor(() => expectErrorState(container));
    expectNoAffirmativeNegative(container);
    expect(container.textContent).not.toMatch(/Pending \(0\)/);
  });

  it('says the same thing when the brain is simply unreachable', async () => {
    const { container } = renderFailing(<Forge />, { fetchImpl: networkFailure() });
    await waitFor(() => expectErrorState(container));
    expectNoAffirmativeNegative(container);
    expect(container.textContent).toMatch(/could not reach the brain/i);
  });

  it('distinguishes 401 from a server fault', async () => {
    const { container } = renderFailing(<Forge />, {
      fetchImpl: httpFailure(401, { detail: 'missing api key' }),
    });
    const node = await waitFor(() => expectErrorState(container));
    expect(node.getAttribute('data-kind')).toBe('auth');
    expect(container.textContent).toMatch(/Settings/i);
  });

  it('still renders the genuine empty state when both queues are empty', async () => {
    const { container } = renderOk(<Forge />, { body: { pending: [] } });
    await waitFor(() => {
      expect(container.textContent).toMatch(/No drafts pending/i);
    });
    expect(container.querySelector('[data-testid="v2-error-state"]')).toBeNull();
  });
});

describe('Forge Proposals and Generated: failed fetch', () => {
  it('Proposals does not report "No capability gaps tracked"', async () => {
    const { container, getByRole } = renderFailing(<Forge />);
    await openFailingTab(getByRole, container, /Proposals/i);
    expectNoAffirmativeNegative(container);
    expect(container.textContent).not.toMatch(/Proposals \(0\)/);
  });

  it('Generated does not report "Nothing generated yet"', async () => {
    const { container, getByRole } = renderFailing(<Forge />);
    await openFailingTab(getByRole, container, /Generated/i);
    expectNoAffirmativeNegative(container);
  });

  it('still renders both genuine empty states', async () => {
    const { container, getByRole } = renderOk(<Forge />, {
      body: { pending: [], proposals: [], tools: [] },
    });
    fireEvent.click(getByRole('tab', { name: /Proposals/i }));
    await waitFor(() => {
      expect(container.textContent).toMatch(/No capability gaps tracked/i);
    });
    fireEvent.click(getByRole('tab', { name: /Generated/i }));
    await waitFor(() => {
      expect(container.textContent).toMatch(/Nothing generated yet/i);
    });
    expect(container.querySelector('[data-testid="v2-error-state"]')).toBeNull();
  });
});

// ── Intents ─────────────────────────────────────────────────────

describe('Intents Today: failed fetch', () => {
  it('does not report "Nothing planned for today"', async () => {
    const { container } = renderFailing(<Intents />);
    await waitFor(() => expectErrorState(container));
    expectNoAffirmativeNegative(container);
    expect(container.textContent).not.toMatch(/Today \(0\)/);
  });

  it('still renders the genuine empty state when today really is clear', async () => {
    const { container } = renderOk(<Intents />, { body: { actions: [] } });
    await waitFor(() => {
      expect(container.textContent).toMatch(/Nothing planned for today/i);
    });
    expect(container.querySelector('[data-testid="v2-error-state"]')).toBeNull();
  });
});

describe('Intents Plans and Stats: failed fetch', () => {
  it('Plans does not report "No plans yet"', async () => {
    const { container, getByRole } = renderFailing(<Intents />);
    await openFailingTab(getByRole, container, /All plans/i);
    expectNoAffirmativeNegative(container);
    expect(container.textContent).not.toMatch(/Plans \(0\)/);
  });

  it('Stats renders no counters and not the stats heading', async () => {
    const { container, getByRole } = renderFailing(<Intents />);
    await openFailingTab(getByRole, container, /Stats/i);
    expectNoStatsClaim(container, 'Intent stats');
  });

  it('still renders the genuine empty plan list, and real counters', async () => {
    const { container, getByRole } = renderOk(<Intents />, {
      body: { actions: [], plans: [], active_plans: 0 },
    });
    fireEvent.click(getByRole('tab', { name: /All plans/i }));
    await waitFor(() => {
      expect(container.textContent).toMatch(/No plans yet/i);
    });
    fireEvent.click(getByRole('tab', { name: /Stats/i }));
    await waitFor(() => {
      expect(container.textContent).toMatch(/Intent stats/i);
    });
    expect(
      Array.from(container.querySelectorAll('.v2-stat-value')).map((n) => n.textContent),
    ).toContain('0');
    expect(container.querySelector('[data-testid="v2-error-state"]')).toBeNull();
  });
});

// ── Agents ──────────────────────────────────────────────────────

describe('Agents Personas: failed fetch', () => {
  it('does not report "No first-party personas loaded" or send the user to the boot log', async () => {
    const { container } = renderFailing(<Agents />);
    await waitFor(() => expectErrorState(container));
    expectNoAffirmativeNegative(container);
    // The empty state sends the user to hunt for "Loaded N first-party
    // personas" in a boot log that is not the problem.
    expect(container.textContent).not.toMatch(/Loaded N first-party/i);
    expect(container.textContent).not.toMatch(/Personas \(0\)/);
  });

  it('still renders the genuine empty state when no personas shipped', async () => {
    const { container } = renderOk(<Agents />, { body: { personas: [] } });
    await waitFor(() => {
      expect(container.textContent).toMatch(/No first-party personas loaded/i);
    });
    expect(container.querySelector('[data-testid="v2-error-state"]')).toBeNull();
  });
});

describe('Agents Specialists: failed fetch', () => {
  it('does not report "No specialists yet" or offer "Spawn first specialist"', async () => {
    const { container, getByRole } = renderFailing(<Agents />);
    await openFailingTab(getByRole, container, /Specialists/i);
    expectNoAffirmativeNegative(container);
    expect(container.textContent).not.toMatch(/Specialists \(0\)/);
  });

  it('still renders the genuine empty state plus its CTA', async () => {
    const { container, getByRole } = renderOk(<Agents />, {
      body: { personas: [], agents: [], skills: [] },
    });
    fireEvent.click(getByRole('tab', { name: /Specialists/i }));
    await waitFor(() => {
      expect(container.textContent).toMatch(/No specialists yet/i);
    });
    expect(container.textContent).toMatch(/Spawn first specialist/i);
    expect(container.querySelector('[data-testid="v2-error-state"]')).toBeNull();
  });
});

describe('Agents Proposals and Stats: failed fetch', () => {
  it('Proposals does not report "No recurring patterns yet"', async () => {
    const { container, getByRole } = renderFailing(<Agents />);
    await openFailingTab(getByRole, container, /Proposals/i);
    expectNoAffirmativeNegative(container);
  });

  it('Stats renders no counters and not the stats heading', async () => {
    const { container, getByRole } = renderFailing(<Agents />);
    await openFailingTab(getByRole, container, /Stats/i);
    expectNoStatsClaim(container, 'Mitosis stats');
  });

  it('still renders both genuine empty results', async () => {
    const { container, getByRole } = renderOk(<Agents />, {
      body: (url) => (url.includes('/stats') ? {} : { personas: [], proposals: [] }),
    });
    fireEvent.click(getByRole('tab', { name: /Proposals/i }));
    await waitFor(() => {
      expect(container.textContent).toMatch(/No recurring patterns yet/i);
    });
    fireEvent.click(getByRole('tab', { name: /Stats/i }));
    await waitFor(() => {
      expect(container.textContent).toMatch(/reports no Mitosis counters/i);
    });
    expect(container.querySelector('[data-testid="v2-error-state"]')).toBeNull();
  });
});

// ── Flows ───────────────────────────────────────────────────────

describe('Flows TaskFlows: failed fetch', () => {
  it('does not report "No flows yet" or offer "Create your first flow"', async () => {
    const { container } = renderFailing(<Flows />);
    await waitFor(() => expectErrorState(container));
    expectNoAffirmativeNegative(container);
    expect(container.textContent).not.toMatch(/TaskFlows \(0\)/);
  });

  it('still renders the genuine empty state plus its CTA', async () => {
    const { container } = renderOk(<Flows />, { body: { flows: [], skills: [] } });
    await waitFor(() => {
      expect(container.textContent).toMatch(/No flows yet/i);
    });
    expect(container.textContent).toMatch(/Create your first flow/i);
    expect(container.querySelector('[data-testid="v2-error-state"]')).toBeNull();
  });
});

describe('Flows Packs, Routines and Automations: failed fetch', () => {
  it('Packs does not report "No first-party workflow packs loaded"', async () => {
    const { container, getByRole } = renderFailing(<Flows />);
    await openFailingTab(getByRole, container, /Packs/i);
    expectNoAffirmativeNegative(container);
    expect(container.textContent).not.toMatch(/Loaded N first-party/i);
    expect(container.textContent).not.toMatch(/Workflow packs \(0\)/);
  });

  it('Routines does not report "No routines"', async () => {
    const { container, getByRole } = renderFailing(<Flows />);
    await openFailingTab(getByRole, container, /Routines/i);
    expectNoAffirmativeNegative(container);
    expect(container.textContent).not.toMatch(/Routines \(0\)/);
  });

  it('Automations does not report "No automations"', async () => {
    const { container, getByRole } = renderFailing(<Flows />);
    await openFailingTab(getByRole, container, /Automations/i);
    expectNoAffirmativeNegative(container);
    expect(container.textContent).not.toMatch(/Automations \(0\)/);
  });

  it('still renders all three genuine empty states', async () => {
    const { container, getByRole } = renderOk(<Flows />, {
      body: { flows: [], packs: [], routines: [], automations: [], skills: [] },
    });
    fireEvent.click(getByRole('tab', { name: /Packs/i }));
    await waitFor(() => {
      expect(container.textContent).toMatch(/No first-party workflow packs loaded/i);
    });
    fireEvent.click(getByRole('tab', { name: /Routines/i }));
    await waitFor(() => {
      expect(container.textContent).toMatch(/No routines/i);
    });
    fireEvent.click(getByRole('tab', { name: /Automations/i }));
    await waitFor(() => {
      expect(container.textContent).toMatch(/No automations/i);
    });
    expect(container.querySelector('[data-testid="v2-error-state"]')).toBeNull();
  });
});
