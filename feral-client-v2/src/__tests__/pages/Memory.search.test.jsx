/**
 * Memory — the Search tab, which had never returned a result.
 *
 * It called `/internal/memory/search?q=<term>`. That route declares its
 * parameter as `query`, so FastAPI bound `query` to "" and the handler's
 * `if not query: return []` fired on every search this page has ever
 * run. Measured against a live brain holding two notes that both
 * matched: `?q=quokka` -> `[]`, `?query=quokka` -> both notes with
 * scores. An empty result set reads as "nothing matched", so the page
 * rendered "No results" and the store looked empty rather than
 * un-queried. It also read `r.score`, a key the notes route does not
 * return (it returns `relevance_score`), so the score chip was dead too.
 *
 * It now calls `/api/memory/search`, which runs the store's four-tier
 * hybrid `search_all` and reports the per-tier degradations that path
 * records.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { fireEvent, waitFor } from '@testing-library/react';
import { renderV2 } from '../_helpers/renderV2';
import Memory from '../../pages/Memory';

afterEach(() => { vi.unstubAllGlobals(); });

const HITS = {
  query: 'quokka',
  count: 3,
  tiers: { entity: 1, note: 1, knowledge: 1 },
  degradations: [],
  degraded: false,
  results: [
    { tier: 'entity', score: 0.83, id: 'e1', name: 'quokka', type: 'thing', mentions: 2, summary: 'Entity: quokka (thing)' },
    {
      tier: 'note',
      score: 0.75,
      id: 'n1',
      content: 'The quokka lives on Rottnest Island near Perth',
      tags: ['zoology'],
      created_at: 1714000000,
    },
    { tier: 'knowledge', score: 0.42, id: 'k1', subject: 'quokka', predicate: 'lives_on', object: 'Rottnest Island' },
  ],
};

function responder(payload = HITS, seen = []) {
  return (url) => {
    seen.push(url);
    if (url.includes('/api/memory/search')) return payload;
    if (url.includes('/internal/memory/stats')) {
      return { observability: { embedding_provider: 'fastembed', active_vector_store: 'numpy_fallback', chunk_count: 5 } };
    }
    if (url.includes('/api/memory/stats')) return { ok: true, totals: { episodes: 0, notes: 2, knowledge_triples: 1 } };
    if (url.includes('/internal/memory/recent')) return { memories: [] };
    return {};
  };
}

async function search(container, getByTestId, term = 'quokka') {
  fireEvent.click(await waitFor(() => {
    const t = [...container.querySelectorAll('button')].find((b) => b.textContent.trim() === 'Search');
    if (!t) throw new Error('no Search tab');
    return t;
  }));
  const input = await waitFor(() => getByTestId('memory-search-input'));
  fireEvent.change(input, { target: { value: term } });
  fireEvent.click(getByTestId('memory-search-submit'));
}

describe('Memory search', () => {
  it('queries the cross-tier route, never the dead ?q= on the notes route', async () => {
    const seen = [];
    const { container, getByTestId } = renderV2(<Memory />, { fetch: responder(HITS, seen) });
    await search(container, getByTestId);
    await waitFor(() => {
      expect(seen.some((u) => u.includes('/api/memory/search?q=quokka'))).toBe(true);
    });
    expect(seen.some((u) => u.includes('/internal/memory/search'))).toBe(false);
  });

  it('renders a row per tier with the tier named', async () => {
    const { container, getByTestId } = renderV2(<Memory />, { fetch: responder() });
    await search(container, getByTestId);
    await waitFor(() => {
      const tiers = [...container.querySelectorAll('[data-testid="memory-search-tier"]')]
        .map((el) => el.textContent);
      expect(tiers).toEqual(['Entity', 'Note', 'Knowledge']);
    });
  });

  it('renders a knowledge triple as readable text, not raw JSON', async () => {
    const { container, getByTestId } = renderV2(<Memory />, { fetch: responder() });
    await search(container, getByTestId);
    await waitFor(() => {
      expect(container.textContent).toContain('quokka lives_on Rottnest Island');
    });
    expect(container.textContent).not.toContain('"predicate"');
  });

  it('shows the score, which the old row never could', async () => {
    const { container, getByTestId } = renderV2(<Memory />, { fetch: responder() });
    await search(container, getByTestId);
    await waitFor(() => expect(container.textContent).toContain('score 0.750'));
  });

  it('marks a below-threshold hit weak instead of presenting it as a match', async () => {
    const { container, getByTestId } = renderV2(<Memory />, { fetch: responder() });
    await search(container, getByTestId);
    await waitFor(() => {
      const weak = container.querySelectorAll('[data-testid="memory-search-weak"]');
      // Only the 0.42 knowledge row is weak; 0.83 and 0.75 are not.
      expect(weak.length).toBe(1);
    });
    expect(container.textContent).toContain('2 strong of 3 hits');
  });

  it('offers a facet per tier and filters on it', async () => {
    const { container, getByTestId } = renderV2(<Memory />, { fetch: responder() });
    await search(container, getByTestId);
    const facets = await waitFor(() => {
      const el = getByTestId('memory-search-facets');
      if (el.querySelectorAll('button').length < 2) throw new Error('no facets');
      return el;
    });
    const noteFacet = [...facets.querySelectorAll('button')].find((b) => b.textContent.startsWith('Note'));
    expect(noteFacet.textContent).toContain('(1)');
    fireEvent.click(noteFacet);
    await waitFor(() => {
      expect(container.querySelectorAll('[data-testid="memory-search-tier"]').length).toBe(1);
    });
  });

  it('names a failed tier instead of shrinking the result set silently', async () => {
    const degraded = {
      ...HITS,
      count: 1,
      tiers: { note: 1 },
      degraded: true,
      degradations: [{ tier: 'episode', error: 'EmbeddingDimensionMismatch: 1536 != 384' }],
      results: [HITS.results[1]],
    };
    const { container, getByTestId } = renderV2(<Memory />, { fetch: responder(degraded) });
    await search(container, getByTestId);
    const chip = await waitFor(() => {
      const el = getByTestId('memory-search-degraded');
      if (!el) throw new Error('no degraded chip');
      return el;
    });
    expect(chip.textContent).toContain('episode tier failed');
    expect(chip.textContent).toContain('partial, not empty');
    expect(container).toBeTruthy();
  });

  it('an empty result set says every tier answered', async () => {
    const empty = { query: 'zzz', count: 0, tiers: {}, degradations: [], degraded: false, results: [] };
    const { container, getByTestId } = renderV2(<Memory />, { fetch: responder(empty) });
    await search(container, getByTestId, 'zzz');
    await waitFor(() => {
      expect(container.textContent).toContain('Every tier answered and none of them matched');
    });
  });

  it('names the engine that answered', async () => {
    const { container, getByTestId } = renderV2(<Memory />, { fetch: responder() });
    fireEvent.click(await waitFor(() => {
      const t = [...container.querySelectorAll('button')].find((b) => b.textContent.trim() === 'Search');
      if (!t) throw new Error('no Search tab');
      return t;
    }));
    await waitFor(() => {
      expect(getByTestId('memory-search-engine').textContent).toContain('fastembed');
    });
    expect(getByTestId('memory-search-engine').textContent).toContain('numpy_fallback');
  });
});
