/**
 * Scoped replication is the one capability with no equivalent in a
 * comparable product, and until now it could not be reached from the
 * UI at all. The API took a `scope`, the CLI took a `scope`, the skill
 * declared a `scope`, and the Save-a-memory dialog offered only Content
 * and Tags. The only way for a person to share a memory was to know the
 * HTTP route by hand, which means the feature was not shipped.
 *
 * These pin the three properties that make the field safe:
 *
 *   * private is the default, and omitting sharing sends NO scope key
 *     at all rather than an empty string;
 *   * only scopes a peer has actually been granted are offered, because
 *     a scope nobody holds replicates nowhere and offering it would
 *     promise sharing that silently never happens;
 *   * with no peer, the dialog says so instead of showing an empty
 *     control.
 */
import { describe, it, expect, afterEach, vi } from 'vitest';
import { cleanup, fireEvent } from '@testing-library/react';
import { renderV2 } from '../_helpers/renderV2';
import Memory from '../../pages/Memory';

afterEach(() => { cleanup(); });

function mount({ grants = [], onSave } = {}) {
  return renderV2(<Memory />, {
    fetch: (url, opts) => {
      if (url.includes('/api/sync/scopes')) return { ok: true, grants };
      if (url.includes('/internal/memory/save')) {
        onSave?.(JSON.parse(opts?.body || '{}'));
        return { id: 'abc123', status: 'saved' };
      }
      if (url.includes('/api/memory/stats')) {
        return { ok: true, totals: { episodes: 0, notes: 0, knowledge_triples: 0 } };
      }
      if (url.includes('/internal/memory/recent')) return [];
      return { items: [], memories: [] };
    },
  });
}

async function openDialog(screen) {
  const open = await screen.findByText(/Save memory/i);
  fireEvent.click(open);
  return screen.findByTestId('v2-memory-scope');
}

describe('Save a memory — sharing scope', () => {
  it('offers only scopes a peer has actually been granted', async () => {
    const screen = mount({ grants: [{ node_id: 'peer-1', scope: 'work' }] });
    const select = await openDialog(screen);
    const values = [...select.querySelectorAll('option')].map((o) => o.value);
    expect(values).toContain('');       // private
    expect(values).toContain('work');
    expect(values).toHaveLength(2);
  });

  it('does not invent a scope when no peer holds one', async () => {
    const screen = mount({ grants: [] });
    const select = await openDialog(screen);
    const values = [...select.querySelectorAll('option')].map((o) => o.value);
    expect(values).toEqual(['']);
    expect(document.body.textContent).toContain('No peer brain has been granted a scope');
  });

  it('deduplicates a scope granted to more than one peer', async () => {
    const screen = mount({
      grants: [
        { node_id: 'peer-1', scope: 'work' },
        { node_id: 'peer-2', scope: 'work' },
      ],
    });
    const select = await openDialog(screen);
    const values = [...select.querySelectorAll('option')].map((o) => o.value);
    expect(values).toEqual(['', 'work']);
  });

  it('defaults to private and sends NO scope key', async () => {
    const saved = vi.fn();
    const screen = mount({ grants: [{ node_id: 'p', scope: 'work' }], onSave: saved });
    const select = await openDialog(screen);
    expect(select.value).toBe('');

    fireEvent.change(document.querySelector('textarea'), {
      target: { value: 'a private thought' },
    });
    fireEvent.click(screen.getByText(/^Save$/));

    await vi.waitFor(() => expect(saved).toHaveBeenCalled());
    const body = saved.mock.calls[0][0];
    expect(body.content).toBe('a private thought');
    expect('scope' in body).toBe(false);
  });

  it('sends the chosen scope when the user picks one', async () => {
    const saved = vi.fn();
    const screen = mount({ grants: [{ node_id: 'p', scope: 'work' }], onSave: saved });
    const select = await openDialog(screen);

    fireEvent.change(document.querySelector('textarea'), {
      target: { value: 'a team decision' },
    });
    fireEvent.change(select, { target: { value: 'work' } });
    fireEvent.click(screen.getByText(/^Save$/));

    await vi.waitFor(() => expect(saved).toHaveBeenCalled());
    expect(saved.mock.calls[0][0].scope).toBe('work');
  });
});
