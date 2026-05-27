/**
 * RC polish: when ``/api/memory/stats`` returns the degraded payload
 * (``ok: false, reason: "stats_timeout"`` — the contract pinned by
 * ``feral-core/tests/test_timeline_episode_source.py``), the Recent
 * tab used to render "0 episodes · 0 notes · 0 knowledge" as if the
 * brain genuinely had no data. The fix surfaces an explicit chip
 * carrying the ``reason`` so the operator knows the numbers below it
 * are unavailable, not zero.
 */
import { describe, it, expect, afterEach } from 'vitest';
import { cleanup } from '@testing-library/react';
import { renderV2 } from '../_helpers/renderV2';
import Memory from '../../pages/Memory';

afterEach(() => { cleanup(); });

describe('Memory Recent tab — degraded stats chip', () => {
  it('shows an unavailable chip when /api/memory/stats reports ok=false', async () => {
    const { findByTestId } = renderV2(<Memory />, {
      fetch: (url) => {
        if (url.includes('/api/memory/stats')) {
          return {
            ok: false,
            reason: 'stats_timeout',
            totals: {
              episodes: 0,
              notes: 0,
              knowledge_triples: 0,
              knowledge: 0,
            },
          };
        }
        if (url.includes('/internal/memory/recent')) {
          return [];
        }
        return { items: [], memories: [] };
      },
    });

    const chip = await findByTestId('memory-stats-degraded');
    expect(chip).toBeTruthy();
    expect(chip.textContent).toContain('Memory stats unavailable');
    expect(chip.textContent).toContain('stats_timeout');
  });

  it('reads canonical knowledge_triples key when stats are healthy', async () => {
    const { findByText } = renderV2(<Memory />, {
      fetch: (url) => {
        if (url.includes('/api/memory/stats')) {
          return {
            ok: true,
            totals: {
              episodes: 42,
              notes: 7,
              knowledge_triples: 11,
            },
          };
        }
        if (url.includes('/internal/memory/recent')) {
          return [];
        }
        return { items: [], memories: [] };
      },
    });

    // Title is rendered as "Recent (42 episodes · 7 notes · 11 knowledge)"
    // — confirming the page picked up the canonical key, not the
    // legacy ``totals.knowledge`` slot.
    const title = await findByText(/42 episodes/);
    expect(title.textContent).toContain('7 notes');
    expect(title.textContent).toContain('11 knowledge');
  });
});
