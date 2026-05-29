/**
 * v2026.5.44 — Settings → Cost regression tests.
 *
 * Pins three contracts:
 *
 *  (a) The Cost panel renders an input for every backend subsystem
 *      (chat, vision, embedding, routing, screen_loop, proactive,
 *      learner, compaction) plus the global per-hour cap. Without
 *      this, an operator who only sees a "chat" knob would silently
 *      leave ScreenLoop at the factory $0.10 default — exactly the
 *      bug the v2026.5.43 release-candidate live verification turned
 *      up.
 *
 *  (b) Saving a cap writes the FLAT schema
 *      ``{section:'cost', key:'<site>', value:{per_hour_usd: N}}``
 *      to ``/api/config/update``. The persisted shape is
 *      ``cost.<site>.per_hour_usd`` which is the canonical schema
 *      that ``CostBudget._cap_for`` reads first (with the legacy
 *      ``per_call_site_caps.<site>.per_hour_usd`` shape kept as a
 *      back-compat fallback inside the brain).
 *
 *  (c) When the brain returns a pre-existing
 *      ``cost.proactive.per_hour_usd: 0.5`` (flat) the input
 *      pre-fills with "0.5". Pinning this guards the same trap from
 *      the other side — the UI must read what it writes.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, waitFor } from '@testing-library/react';
import { renderV2 } from '../_helpers/renderV2';
import Settings from '../../pages/Settings';

function makeFetcher({ configBody, capturePosts }) {
  return (url, init) => {
    if (init?.method === 'POST' && url.includes('/api/config/update')) {
      try {
        capturePosts.push({ url, body: JSON.parse(init.body) });
      } catch {
        capturePosts.push({ url, body: init.body });
      }
      return { ok: true };
    }
    if (url.includes('/api/config')) {
      return configBody;
    }
    return {};
  };
}

async function openCostTab(findByTestId) {
  // The deeplink ``?section=Cost`` already lands the user on the
  // Cost panel; we just wait for it to mount + finish its initial
  // ``/api/config`` load.
  await findByTestId('cost-section');
}

describe('Settings → Cost', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders an input for every per-subsystem cap', async () => {
    const captured = [];
    const { getByText, findByTestId } = renderV2(
      <Settings />,
      {
        route: '/settings?section=Cost',
        fetch: makeFetcher({
          configBody: { cost: {} },
          capturePosts: captured,
        }),
      },
    );
    await openCostTab(findByTestId);

    // Every subsystem the brain wires a BudgetLoopGuard for must
    // have a knob. If you add a new subsystem to ``BrainState`` and
    // forget to expose its cap here, this test will fail.
    for (const site of [
      'chat', 'vision', 'embedding', 'routing',
      'screen_loop', 'proactive', 'learner', 'compaction',
    ]) {
      await findByTestId(`cost-cap-${site}`);
    }
    await findByTestId('cost-global-hour');
    await findByTestId('cost-global-day');
  });

  it('persists screen_loop cap as cost.screen_loop.per_hour_usd', async () => {
    const captured = [];
    const { getByText, findByTestId } = renderV2(
      <Settings />,
      {
        route: '/settings?section=Cost',
        fetch: makeFetcher({
          configBody: { cost: {} },
          capturePosts: captured,
        }),
      },
    );
    await openCostTab(findByTestId);

    const input = await findByTestId('cost-cap-screen_loop');
    fireEvent.change(input, { target: { value: '20' } });

    const saveBtn = await findByTestId('cost-save-screen_loop');
    fireEvent.click(saveBtn);

    await waitFor(() => {
      const post = captured.find((c) => c.url.includes('/api/config/update'));
      expect(post).toBeTruthy();
      // Pin the canonical flat schema. Body shape persisted by the
      // brain becomes settings.json::cost.screen_loop.per_hour_usd
      // = 20, which is the path ``CostBudget._cap_for`` reads first.
      expect(post.body).toMatchObject({
        section: 'cost',
        key: 'screen_loop',
        value: { per_hour_usd: 20 },
      });
    });
  });

  it('pre-fills an input from a pre-existing flat cost.<site>.per_hour_usd', async () => {
    const captured = [];
    const { getByText, findByTestId } = renderV2(
      <Settings />,
      {
        route: '/settings?section=Cost',
        fetch: makeFetcher({
          configBody: {
            cost: {
              proactive: { per_hour_usd: 0.5 },
            },
          },
          capturePosts: captured,
        }),
      },
    );
    await openCostTab(findByTestId);

    const proactiveInput = await findByTestId('cost-cap-proactive');
    expect(proactiveInput.value).toBe('0.5');
  });

  it('shows an empty input with "No limit" placeholder when no cap is configured', async () => {
    // v2026.5.47 — the cost budget is unlimited by default. The UI
    // must reflect that: an unset per-subsystem cap renders as an
    // empty field with the placeholder "No limit", NOT as "0" or
    // a pre-filled factory default.
    const captured = [];
    const { findByTestId } = renderV2(
      <Settings />,
      {
        route: '/settings?section=Cost',
        fetch: makeFetcher({
          configBody: { cost: {} },
          capturePosts: captured,
        }),
      },
    );
    await openCostTab(findByTestId);

    const input = await findByTestId('cost-cap-screen_loop');
    expect(input.value).toBe('');
    expect(input.getAttribute('placeholder')).toBe('No limit');

    const globalHour = await findByTestId('cost-global-hour');
    expect(globalHour.value).toBe('');
    expect(globalHour.getAttribute('placeholder')).toBe('No limit');
  });

  it('clearing a configured cap persists value:null to remove it', async () => {
    // The operator had a cap set; they empty the input and click
    // Save → the UI POSTs ``value: null`` so the brain clears
    // ``cost.screen_loop`` and CostBudget falls back to unlimited
    // on the next ``reload_from_settings``.
    const captured = [];
    const { findByTestId } = renderV2(
      <Settings />,
      {
        route: '/settings?section=Cost',
        fetch: makeFetcher({
          configBody: { cost: { screen_loop: { per_hour_usd: 20 } } },
          capturePosts: captured,
        }),
      },
    );
    await openCostTab(findByTestId);

    const input = await findByTestId('cost-cap-screen_loop');
    expect(input.value).toBe('20');
    fireEvent.change(input, { target: { value: '' } });

    const saveBtn = await findByTestId('cost-save-screen_loop');
    fireEvent.click(saveBtn);

    await waitFor(() => {
      const post = captured.find((c) => c.url.includes('/api/config/update'));
      expect(post).toBeTruthy();
      expect(post.body).toMatchObject({
        section: 'cost',
        key: 'screen_loop',
        value: null,
      });
    });
  });

  it('falls back to legacy per_call_site_caps when flat shape is absent', async () => {
    // Some installs still have the v2026.5.43 nested shape on disk;
    // the UI must read it so the operator sees their existing value
    // instead of "0".
    const captured = [];
    const { getByText, findByTestId } = renderV2(
      <Settings />,
      {
        route: '/settings?section=Cost',
        fetch: makeFetcher({
          configBody: {
            cost: {
              per_call_site_caps: {
                vision: { per_hour_usd: 0.25 },
              },
            },
          },
          capturePosts: captured,
        }),
      },
    );
    await openCostTab(findByTestId);

    const visionInput = await findByTestId('cost-cap-vision');
    expect(visionInput.value).toBe('0.25');
  });
});
