/**
 * Forge must never render a draft as promoted or discarded on an answer
 * it did not read.
 *
 * The regression this pins: `POST /api/skills/approve` answered a False
 * from `SkillGenerator.approve_skill` with HTTP 200 and
 * `{"ok": false, "skill_id": ..., "registered": false}`. No `error` key.
 * `apiFetch` raises on a non-2xx status, or on a 2xx body carrying
 * `error`, so that shape trips neither: `decide()` awaited the call,
 * never read the body, `return`ed out of its fallback loop and refreshed
 * the list. A draft that was never registered looked approved.
 *
 * `/api/tool-genesis/approve` is the first path `decide()` tries and it
 * still sends the same shape today (`{"success": false}` at HTTP 200
 * when the approved tool fails to promote), so the client checks both
 * keys. And the brain the client is talking to is not necessarily the
 * one in this checkout: the desktop bundle ships its own copy of
 * `api/routes/skills.py`. The old wire shapes are driven deliberately
 * below and the page must report failure anyway.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, screen, waitFor, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

import Forge from '../../pages/Forge';
import { clearAllGlobalErrors } from '../../hooks/useGlobalErrors';

const DRAFT = {
  skill_id: 'draft_one',
  name: 'Draft One',
  description: 'A drafted capability',
};

function makeResponse(status, body) {
  const text = JSON.stringify(body);
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? 'OK' : 'Error',
    headers: new Map(),
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(text),
    clone() { return makeResponse(status, body); },
  };
}

/**
 * @param decision  responses keyed by endpoint path fragment.
 */
function renderForge(decision = {}) {
  const calls = [];
  vi.stubGlobal('fetch', vi.fn((input) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    calls.push(url);
    for (const [fragment, make] of Object.entries(decision)) {
      if (url.includes(fragment)) return Promise.resolve(make());
    }
    if (url.includes('/api/skills/pending')) {
      return Promise.resolve(makeResponse(200, { pending: [DRAFT] }));
    }
    if (url.includes('/api/tool-genesis/pending')) {
      return Promise.resolve(makeResponse(200, { proposals: [] }));
    }
    return Promise.resolve(makeResponse(200, {}));
  }));
  const view = render(<MemoryRouter><Forge /></MemoryRouter>);
  return { ...view, calls };
}

async function click(name) {
  const button = await screen.findByRole('button', { name });
  fireEvent.click(button);
}

const NOT_A_TOOL_GENESIS_DRAFT = () => makeResponse(404, { detail: 'No tool draft_one' });

describe('Forge approve/reject reporting', () => {
  beforeEach(() => { clearAllGlobalErrors(); });
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); clearAllGlobalErrors(); });

  it('does not render an approval as done when a 200 body says ok:false', async () => {
    // The exact pre-fix wire shape: success status, ok:false, no `error`.
    const { container } = renderForge({
      '/api/tool-genesis/approve': NOT_A_TOOL_GENESIS_DRAFT,
      '/api/skills/approve': () => makeResponse(200, {
        ok: false,
        skill_id: 'draft_one',
        registered: false,
      }),
    });

    await click(/approve/i);

    await waitFor(() => {
      expect(container.querySelectorAll('[data-testid="v2-error-state"]').length)
        .toBeGreaterThan(0);
    });
    expect(container.textContent).toMatch(/approval of draft_one/i);
    expect(container.textContent).toMatch(/Nothing was registered/i);
  });

  it('reports the brain-supplied reason when the approval conflicts', async () => {
    const { container } = renderForge({
      '/api/tool-genesis/approve': NOT_A_TOOL_GENESIS_DRAFT,
      '/api/skills/approve': () => makeResponse(409, {
        ok: false,
        skill_id: 'draft_one',
        registered: false,
        code: 'not_pending',
        error: "'draft_one' is not in the approval queue, so there was nothing to approve.",
      }),
    });

    await click(/approve/i);

    await waitFor(() => {
      expect(container.querySelectorAll('[data-testid="v2-error-state"]').length)
        .toBeGreaterThan(0);
    });
    expect(container.textContent).toMatch(/not in the approval queue/i);
  });

  it('does not treat a tool-genesis promote failure as an approval', async () => {
    // `/api/tool-genesis/approve` answers 200 `{"success": false}` when the
    // tool is approved but never promoted into the registry. Falling
    // through to /api/skills/approve is what should happen; claiming
    // success is not.
    const { container } = renderForge({
      '/api/tool-genesis/approve': () => makeResponse(200, { success: false, promoted: false }),
      '/api/skills/approve': () => makeResponse(409, {
        ok: false,
        code: 'not_pending',
        error: 'nothing to approve',
      }),
    });

    await click(/approve/i);

    await waitFor(() => {
      expect(container.querySelectorAll('[data-testid="v2-error-state"]').length)
        .toBeGreaterThan(0);
    });
  });

  it('reports a rejection the brain refused', async () => {
    const { container } = renderForge({
      '/api/tool-genesis/reject': NOT_A_TOOL_GENESIS_DRAFT,
      '/api/skills/reject': () => makeResponse(409, {
        ok: false,
        skill_id: 'draft_one',
        rejected: false,
        code: 'not_pending',
        error: "'draft_one' is not in the approval queue, so there was nothing to reject.",
      }),
    });

    await click(/reject/i);

    await waitFor(() => {
      expect(container.querySelectorAll('[data-testid="v2-error-state"]').length)
        .toBeGreaterThan(0);
    });
    expect(container.textContent).toMatch(/rejection of draft_one/i);
    expect(container.textContent).toMatch(/nothing to reject/i);
    expect(container.textContent).toMatch(/Nothing was discarded/i);
  });

  it('still reports nothing when the approval actually happened', async () => {
    const { container } = renderForge({
      '/api/tool-genesis/approve': NOT_A_TOOL_GENESIS_DRAFT,
      '/api/skills/approve': () => makeResponse(200, {
        ok: true,
        skill_id: 'draft_one',
        registered: true,
      }),
    });

    await click(/approve/i);

    await waitFor(() => {
      expect(screen.queryByRole('button', { name: /approve/i })).toBeTruthy();
    });
    expect(container.querySelectorAll('[data-testid="v2-error-state"]').length).toBe(0);
  });
});
