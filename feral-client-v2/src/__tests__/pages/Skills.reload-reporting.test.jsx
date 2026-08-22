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

/**
 * The button moved into the per-skill detail sheet, so reaching it means
 * opening the card first. That relocation is itself the fix for the
 * user-reported "hot-reload does nothing": the outcome used to render in
 * a page-level banner above a 42-card grid, measured on a live brain at
 * y = -71px for the first card and y = -3365px for a lower one, i.e.
 * off-screen for both success and failure. It renders at the click site
 * now, which is inside the sheet. `Modal` portals to document.body, so
 * assertions read document.body rather than the render container.
 */
async function clickHotReload() {
  const card = await screen.findByTestId('v2-skill-card');
  fireEvent.click(card);
  const button = await screen.findByTestId('v2-skill-reload');
  fireEvent.click(button);
}

describe('Skills hot-reload reporting', () => {
  beforeEach(() => { clearAllGlobalErrors(); });
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); clearAllGlobalErrors(); });

  it('does not claim success when a 200 body says ok:false', async () => {
    // The exact pre-fix wire shape: success status, ok:false, no `error`.
    renderSkills(() => makeResponse(200, {
      ok: false,
      skill_id: 'calendar_google',
    }));

    await clickHotReload();

    await waitFor(() => {
      expect(document.body.querySelectorAll('[data-testid="v2-error-state"]').length)
        .toBeGreaterThan(0);
    });
    expect(document.body.querySelectorAll('[data-testid="v2-skill-reload-ok"]').length).toBe(0);
    // And it must say what the user is left with, not just that something
    // went wrong: the old code is still the code that is running.
    expect(document.body.textContent).toMatch(/still what is running/i);
  });

  it('reports the brain-supplied reason when the reload conflicts', async () => {
    renderSkills(() => makeResponse(409, {
      ok: false,
      skill_id: 'calendar_google',
      code: 'no_source',
      error: "nothing on disk to reload for 'calendar_google'",
    }));

    await clickHotReload();

    await waitFor(() => {
      expect(document.body.querySelectorAll('[data-testid="v2-error-state"]').length)
        .toBeGreaterThan(0);
    });
    expect(document.body.querySelectorAll('[data-testid="v2-skill-reload-ok"]').length).toBe(0);
    expect(document.body.textContent).toMatch(/nothing on disk to reload/i);
  });

  it('still confirms a reload that actually happened', async () => {
    renderSkills(() => makeResponse(200, {
      ok: true,
      skill_id: 'calendar_google',
    }));

    await clickHotReload();

    await waitFor(() => {
      expect(screen.getByTestId('v2-skill-reload-ok').textContent)
        .toMatch(/calendar_google/i);
    });
    expect(document.body.querySelectorAll('[data-testid="v2-error-state"]').length).toBe(0);
  });

  /**
   * GUARD for the user-reported defect. The outcome must be inside the
   * detail sheet, next to the button that produced it, not somewhere else
   * on the page. Rendering it into a page-level banner is what made a
   * working button look inert.
   */
  it('renders the outcome inside the sheet that holds the button', async () => {
    renderSkills(() => makeResponse(200, { ok: true, skill_id: 'calendar_google' }));

    await clickHotReload();

    const dialog = await screen.findByRole('dialog');
    await waitFor(() => {
      expect(dialog.querySelectorAll('[data-testid="v2-skill-reload-ok"]').length).toBe(1);
    });
    const button = screen.getByTestId('v2-skill-reload');
    expect(dialog.contains(button)).toBe(true);
  });

  it('keeps a failed reload inside the sheet too', async () => {
    renderSkills(() => makeResponse(409, {
      ok: false,
      skill_id: 'calendar_google',
      code: 'no_source',
      error: "nothing on disk to reload for 'calendar_google'",
    }));

    await clickHotReload();

    const dialog = await screen.findByRole('dialog');
    await waitFor(() => {
      expect(dialog.querySelectorAll('[data-testid="v2-skill-reload-error"]').length).toBe(1);
    });
  });
});
