/**
 * The launcher's per-row hot-reload has to say what happened.
 *
 * It was the worst instance of the reload defect in the client: the whole
 * handler was
 *
 *     setBusy(id);
 *     try { await apiFetch(`/api/skills/reload?skill_id=${id}`, {method:'POST'}); }
 *     finally { setBusy(null); }
 *
 * No body read, no catch, no confirmation of any kind. Every outcome
 * looked identical, a spinner that stopped. Two of them went nowhere at
 * all: a thrown `ApiError` became an unhandled rejection, and a brain
 * that predates the reload-status fix answers a reload that did nothing
 * with HTTP 200 and `{"ok": false}` and no `error` key, which `apiFetch`
 * cannot see either. The 200 case is driven deliberately below: the UI
 * talks to whatever brain is running, not to the one in this checkout.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, screen, waitFor, fireEvent } from '@testing-library/react';

import SkillsLauncher from '../../components/SkillsLauncher';
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

function renderLauncher(reload) {
  vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(reload())));
  return render(<SkillsLauncher open onClose={() => {}} skills={[SKILL]} />);
}

async function clickReload() {
  const button = await screen.findByRole('button', { name: /hot-reload skill/i });
  fireEvent.click(button);
}

describe('SkillsLauncher hot-reload reporting', () => {
  beforeEach(() => { clearAllGlobalErrors(); });
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); clearAllGlobalErrors(); });

  it('surfaces a failed reload that answered 200 with ok:false', async () => {
    const { container } = renderLauncher(() => makeResponse(200, {
      ok: false,
      skill_id: 'calendar_google',
    }));

    await clickReload();

    const note = await screen.findByTestId('skill-reload-failed-calendar_google');
    expect(note).toBeTruthy();
    expect(note.textContent).toMatch(/was not reloaded/i);
    // And what the user is left with, not just that something went wrong.
    expect(note.textContent).toMatch(/still what is running/i);
    expect(container.querySelector('[data-testid="skill-reload-ok-calendar_google"]')).toBeNull();
  });

  it('reports the brain-supplied reason on a 409', async () => {
    renderLauncher(() => makeResponse(409, {
      ok: false,
      skill_id: 'calendar_google',
      code: 'no_source',
      error: "nothing on disk to reload for 'calendar_google'",
    }));

    await clickReload();

    const note = await screen.findByTestId('skill-reload-failed-calendar_google');
    expect(note.textContent).toMatch(/nothing on disk to reload/i);
  });

  it('offers a retry that actually re-posts', async () => {
    renderLauncher(() => makeResponse(409, { ok: false, error: 'no source' }));
    await clickReload();
    await screen.findByTestId('skill-reload-failed-calendar_google');

    fireEvent.click(screen.getByRole('button', { name: /retry/i }));

    await waitFor(() => {
      expect(globalThis.fetch).toHaveBeenCalledTimes(2);
    });
  });

  it('confirms a reload that did happen', async () => {
    renderLauncher(() => makeResponse(200, { ok: true, skill_id: 'calendar_google' }));

    await clickReload();

    const note = await screen.findByTestId('skill-reload-ok-calendar_google');
    expect(note.textContent).toMatch(/Hot-reloaded calendar_google/i);
    expect(screen.queryByTestId('skill-reload-failed-calendar_google')).toBeNull();
  });
});
