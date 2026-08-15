/**
 * THE STANDING RULE, applied to three more surfaces.
 *
 * See `no-false-empty-states.test.jsx` for the rule and the template
 * this file follows: a failed fetch must never render an affirmative
 * negative. "We asked the brain and there is nothing" and "we could not
 * ask the brain" are different claims and must look different.
 *
 * The three converted here, and what each used to assert about a brain
 * it had never reached:
 *
 *   AppsPublish  "Apps currently installed on this brain: …" forever,
 *                because `.catch(() => setHealth(null))` fed a ternary
 *                whose falsy arm is an ellipsis. A permanent spinner is
 *                the same lie in a different costume: it says the answer
 *                is still coming when the request already failed.
 *   Memory       "No notes saved yet", "No episodes yet", "No tool calls
 *                yet", "Knowledge graph is empty", "No results", each
 *                with a fabricated `(0)` in the pane title, off list
 *                state that was `[]` because nothing had overwritten it.
 *   Wiki         "Pages (0)" over "No pages yet / Compile or ingest
 *                content to populate the wiki", which sends the user to
 *                re-ingest a wiki that may well be full.
 *
 * Each surface gets three tests: the brain answers badly, the brain
 * never answers, and the brain truthfully answers with nothing (which
 * must still render the genuine empty state, because a page that cries
 * error over a real zero is the same bug mirrored).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, waitFor, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { StubWebSocket } from './_helpers/renderV2';
import { clearAllGlobalErrors } from '../hooks/useGlobalErrors';

import AppsPublish from '../pages/AppsPublish';
import Memory from '../pages/Memory';
import Wiki from '../pages/Wiki';

// Failure fixtures, same shapes as the parent suite.

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

function renderFailing(ui, { fetchImpl = httpFailure() } = {}) {
  vi.stubGlobal('fetch', fetchImpl);
  StubWebSocket.instances = [];
  vi.stubGlobal('WebSocket', StubWebSocket);
  return render(<MemoryRouter>{ui}</MemoryRouter>);
}

function renderEmptyOk(ui, { body = {} } = {}) {
  vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({
    ok: true,
    status: 200,
    statusText: 'OK',
    json: () => Promise.resolve(typeof body === 'function' ? body() : body),
    text: () => Promise.resolve('{}'),
    headers: new Map(),
    clone() { return this; },
  })));
  StubWebSocket.instances = [];
  vi.stubGlobal('WebSocket', StubWebSocket);
  return render(<MemoryRouter>{ui}</MemoryRouter>);
}

// Sentences that assert a quantity the client never learned.
const AFFIRMATIVE_NEGATIVES = [
  /no notes saved/i,
  /no episodes/i,
  /no tool calls/i,
  /knowledge graph is empty/i,
  /no results/i,
  /no pages yet/i,
  /compile or ingest content/i,
  /apps currently installed/i,
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
  expect(container.textContent).toMatch(/not an empty result/i);
  return nodes[0];
}

beforeEach(() => {
  clearAllGlobalErrors();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  clearAllGlobalErrors();
});

// AppsPublish

describe('AppsPublish: failed fetch', () => {
  it('does not leave "Apps currently installed on this brain: …" on screen', async () => {
    const { container } = renderFailing(<AppsPublish />);
    await waitFor(() => expectErrorState(container));
    expectNoAffirmativeNegative(container);
    // And no ellipsis standing in for a count we never got.
    expect(container.textContent).not.toMatch(/installed on this brain: …/);
  });

  it('says the same thing when the brain is unreachable', async () => {
    const { container } = renderFailing(<AppsPublish />, { fetchImpl: networkFailure() });
    await waitFor(() => expectErrorState(container));
    expectNoAffirmativeNegative(container);
    expect(container.textContent).toMatch(/could not reach the brain/i);
  });

  it('still reports a real zero when the brain has no apps installed', async () => {
    const { container } = renderEmptyOk(<AppsPublish />, { body: { apps: [], count: 0 } });
    await waitFor(() => {
      expect(container.textContent).toMatch(/Apps currently installed on this brain/i);
    });
    expect(container.querySelector('[data-testid="v2-error-state"]')).toBeNull();
  });
});

// Memory

describe('Memory: failed fetch', () => {
  async function openTab(getByRole, container, name) {
    fireEvent.click(getByRole('tab', { name }));
    await waitFor(() => {
      expect(container.querySelector('[data-testid="v2-error-state"]')).not.toBeNull();
    });
  }

  it('Recent does not report "No notes saved yet"', async () => {
    const { container } = renderFailing(<Memory />);
    await waitFor(() => expectErrorState(container));
    expectNoAffirmativeNegative(container);
    // The per-tier totals are measurements too.
    expect(container.textContent).not.toMatch(/0 episodes/);
    expect(container.textContent).not.toMatch(/0 notes/);
  });

  it('Recent flags the stats it could not read', async () => {
    const { container, findByTestId } = renderFailing(<Memory />);
    const chip = await findByTestId('memory-stats-degraded');
    expect(chip.textContent).toMatch(/unavailable/i);
    expect(chip.textContent).toMatch(/stats_unreachable/);
    expectNoAffirmativeNegative(container);
  });

  it('Episodes does not report "No episodes yet" or "Episodes (0)"', async () => {
    const { container, getByRole } = renderFailing(<Memory />);
    await openTab(getByRole, container, /Episodes/i);
    expectNoAffirmativeNegative(container);
    expect(container.textContent).not.toMatch(/Episodes \(0\)/);
  });

  it('Exec log does not report "No tool calls yet"', async () => {
    const { container, getByRole } = renderFailing(<Memory />);
    await openTab(getByRole, container, /Exec log/i);
    expectNoAffirmativeNegative(container);
    expect(container.textContent).not.toMatch(/Execution log \(0\)/);
  });

  it('Knowledge does not report "Knowledge graph is empty"', async () => {
    const { container, getByRole } = renderFailing(<Memory />);
    await openTab(getByRole, container, /Knowledge/i);
    expectNoAffirmativeNegative(container);
    expect(container.textContent).not.toMatch(/Knowledge graph \(0\)/);
  });

  it('Search does not report "No results" for a search that never ran', async () => {
    const { container, getByRole, getByPlaceholderText } = renderFailing(<Memory />);
    fireEvent.click(getByRole('tab', { name: /Search/i }));
    fireEvent.change(getByPlaceholderText(/What do I know about/i), {
      target: { value: 'espresso' },
    });
    fireEvent.click(getByRole('button', { name: /Search/i }));
    await waitFor(() => expectErrorState(container));
    expectNoAffirmativeNegative(container);
  });

  it('distinguishes a 401 from a server fault', async () => {
    const { container } = renderFailing(<Memory />, {
      fetchImpl: httpFailure(401, { detail: 'missing api key' }),
    });
    const node = await waitFor(() => expectErrorState(container));
    expect(node.getAttribute('data-kind')).toBe('auth');
    expect(container.textContent).toMatch(/Settings/i);
  });

  it('still renders the genuine empty states when the brain answers with nothing', async () => {
    const { container, getByRole } = renderEmptyOk(<Memory />, {
      body: {
        memories: [], notes: [], episodes: [], entries: [], entities: [],
        ok: true, totals: { notes: 0, episodes: 0, knowledge_triples: 0 },
      },
    });
    await waitFor(() => {
      expect(container.textContent).toMatch(/No notes saved yet/i);
    });
    expect(container.querySelector('[data-testid="v2-error-state"]')).toBeNull();
    fireEvent.click(getByRole('tab', { name: /Episodes/i }));
    await waitFor(() => {
      expect(container.textContent).toMatch(/No episodes yet/i);
    });
    expect(container.querySelector('[data-testid="v2-error-state"]')).toBeNull();
  });
});

// Wiki

describe('Wiki: failed fetch', () => {
  it('does not report "Pages (0)" or "No pages yet"', async () => {
    const { container } = renderFailing(<Wiki />);
    await waitFor(() => expectErrorState(container));
    expectNoAffirmativeNegative(container);
    expect(container.textContent).not.toMatch(/Pages \(0\)/);
  });

  it('says the same thing when the brain is unreachable', async () => {
    const { container } = renderFailing(<Wiki />, { fetchImpl: networkFailure() });
    await waitFor(() => expectErrorState(container));
    expectNoAffirmativeNegative(container);
    expect(container.textContent).toMatch(/could not reach the brain/i);
  });

  it('still renders the genuine empty state when the wiki really is empty', async () => {
    const { container } = renderEmptyOk(<Wiki />, { body: { pages: [] } });
    await waitFor(() => {
      expect(container.textContent).toMatch(/No pages yet/i);
    });
    expect(container.querySelector('[data-testid="v2-error-state"]')).toBeNull();
  });
});
