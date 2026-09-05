/**
 * Booting against a brain that is not answering must be quiet.
 *
 * Observed after `feral stop` / `feral start`: the Home page stacked
 * three toasts, "Failed to fetch /api/conversations/save", "Failed to
 * fetch /api/conversations/new" and "Request failed (503)
 * /api/conversations/active/thread", for a boot that then worked
 * perfectly. Every one of those calls has a local fallback and every one
 * of them was written to swallow its own rejection, but swallowing it
 * changed nothing on screen: lib/api.js `surfaceError` pushes the toast
 * BEFORE it throws, so a `.catch(() => {})` cannot opt out of it. The
 * boot chain and the background pollers now pass `{ silent: true }`,
 * and api.js collapses a burst of unreachable-brain failures into one.
 *
 * The second test is the other half of the contract: the silence is
 * scoped to calls that opted out, not to failures in general.
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, waitFor } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import Shell from '../../shell/Shell';
import {
  _resetGlobalErrorsForTesting,
  useGlobalErrors,
} from '../../hooks/useGlobalErrors';
import { _resetToastDedupeForTesting } from '../../lib/api';
import { StubWebSocket } from '../_helpers/renderV2';

function ErrorProbe({ onCount }) {
  const { errors } = useGlobalErrors();
  onCount(errors);
  return null;
}

describe('booting against a brain that is not answering', () => {
  beforeEach(() => {
    _resetGlobalErrorsForTesting();
    _resetToastDedupeForTesting();
    StubWebSocket.instances = [];
    vi.stubGlobal('WebSocket', StubWebSocket);
    if (!window.matchMedia) {
      window.matchMedia = vi.fn().mockImplementation((q) => ({
        matches: false,
        media: q,
        onchange: null,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        addListener: vi.fn(),
        removeListener: vi.fn(),
        dispatchEvent: vi.fn(),
      }));
    }
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    _resetToastDedupeForTesting();
  });

  it('pushes no global error when every request fails', async () => {
    // Exactly what a browser does while `feral start` is still coming
    // up: fetch rejects outright. The old build turned this into two
    // "Failed to fetch" toasts plus a 503.
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));
    let seen = null;
    render(
      <MemoryRouter initialEntries={['/']}>
        <Routes>
          <Route path="/" element={<Shell />}>
            <Route index element={<ErrorProbe onCount={(e) => { seen = e; }} />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );
    // Let the boot chain settle: primary id, active thread, transcript,
    // then the explicit create.
    await waitFor(() => expect(fetch).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 50));
    expect(seen).toEqual([]);
  });

  it('still surfaces a real failure from a call that did not opt out', async () => {
    // The silence above must be scoped to the boot calls, not global.
    const { apiFetch } = await import('../../lib/api');
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 404,
      statusText: 'Not Found',
      text: async () => JSON.stringify({ error: 'no such skill' }),
    }));
    let seen = null;
    render(
      <MemoryRouter>
        <ErrorProbe onCount={(e) => { seen = e; }} />
      </MemoryRouter>,
    );
    await expect(apiFetch('/api/skills/nope')).rejects.toThrow();
    await waitFor(() => expect(seen).toHaveLength(1));
    expect(seen[0].message).toContain('no such skill');
  });
});
