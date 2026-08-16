/**
 * The Skills page must never call a hot-reload a success it did not see.
 *
 * The regression this pins: `/api/skills/reload` used to answer a reload
 * that had done nothing with HTTP 200 and `{"ok": false, "skill_id": ...}`:
 * a success status, and a body with no `error` key. `apiFetch` raises on
 * a non-2xx status, and on a 2xx body carrying `error`; that shape trips
 * neither. `Skills.jsx` awaited the call, never read the body, and called
 * `setReloaded(id)` on anything that did not throw, so the page rendered
 * "Hot-reloaded <id>" over a skill whose code had not moved.
 *
 * The brain no longer sends that shape (it answers 404/422 with an
 * `error`), but the client must not depend on that: the desktop bundle
 * ships its own copy of `api/routes/skills.py`, and an operator can point
 * this UI at an older brain. So the first test below drives the OLD wire
 * shape deliberately and requires the page to report failure anyway.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, screen, waitFor, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

import Skills from '../../pages/Skills';
import { clearAllGlobalErrors } from '../../hooks/useGlobalErrors';

const SKILL = {
  skill_id: 'calendar_google',
  name: 'Google Calendar',
  description: 'Calendar access',
  endpoints: [],
  trigger_phrases: [],
};

function makeResponse(status, body) {
  const text = JSON.stringify(body);
  const res = {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? 'OK' : 'Error',
    headers: new Map(),
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(text),
    clone() { return makeResponse(status, body); },
  };
  return res;
}

/**
 * @param reload  the response the reload endpoint answers with.
 */
function renderSkills(reload) {
  const calls = [];
  vi.stubGlobal('fetch', vi.fn((input) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    calls.push(url);
    if (url.includes('/api/skills/reload')) return Promise.resolve(reload());
    if (url.includes('/api/skills/pending')) return Promise.resolve(makeResponse(200, { pending: [] }));
    return Promise.resolve(makeResponse(200, [SKILL]));
  }));
  const view = render(<MemoryRouter><Skills /></MemoryRouter>);
  return { ...view, calls };
}

async function clickHotReload() {
  const button = await screen.findByRole('button', { name: /hot-reload/i });
  fireEvent.click(button);
}

describe('Skills hot-reload reporting', () => {
  beforeEach(() => { clearAllGlobalErrors(); });
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); clearAllGlobalErrors(); });

  it('does not claim success when a 200 body says ok:false', async () => {
    // The exact pre-fix wire shape: success status, ok:false, no `error`.
    const { container } = renderSkills(() => makeResponse(200, {
      ok: false,
      skill_id: 'calendar_google',
    }));

    await clickHotReload();

    await waitFor(() => {
      expect(container.querySelectorAll('[data-testid="v2-error-state"]').length)
        .toBeGreaterThan(0);
    });
    expect(container.textContent).not.toMatch(/Hot-reloaded/i);
    // And it must say what the user is left with, not just that something
    // went wrong: the old code is still the code that is running.
    expect(container.textContent).toMatch(/still what is running/i);
  });

  it('reports the brain-supplied reason when the reload conflicts', async () => {
    const { container } = renderSkills(() => makeResponse(409, {
      ok: false,
      skill_id: 'calendar_google',
      code: 'no_source',
      error: "nothing on disk to reload for 'calendar_google'",
    }));

    await clickHotReload();

    await waitFor(() => {
      expect(container.querySelectorAll('[data-testid="v2-error-state"]').length)
        .toBeGreaterThan(0);
    });
    expect(container.textContent).not.toMatch(/Hot-reloaded/i);
    expect(container.textContent).toMatch(/nothing on disk to reload/i);
  });

  it('still confirms a reload that actually happened', async () => {
    const { container } = renderSkills(() => makeResponse(200, {
      ok: true,
      skill_id: 'calendar_google',
    }));

    await clickHotReload();

    await waitFor(() => {
      expect(container.textContent).toMatch(/Hot-reloaded calendar_google/i);
    });
    expect(container.querySelectorAll('[data-testid="v2-error-state"]').length).toBe(0);
  });
});
