/**
 * Guards for the five Skills-page defects the user reported from real
 * screenshots of the live brain.
 *
 * Each `it` below fails on the pre-fix page, and the failure is the
 * defect itself rather than a proxy for it:
 *
 *  1. wall of text     : the card printed the whole `description` (up to
 *                        ~2,000 chars for `macos_ax`) plus four trigger
 *                        phrases, for all 42 skills.
 *  2. uneven cards     : measured on a live brain, one grid held cards
 *                        527px to 1055px tall.
 *  3. no icon          : nothing rendered a glyph per skill.
 *  6. no marketplace   : /marketplace exists and is in the nav, but the
 *                        Skills page had no link to it.
 *  7. no way to add    : no link to /forge (create) either.
 *
 * Defect 4 (hot-reload reporting) is pinned in
 * Skills.reload-reporting.test.jsx, which owns that surface.
 *
 * jsdom computes no layout, so uniform card size cannot be measured
 * here. What IS asserted is the mechanism that makes it uniform: the
 * card carries no per-skill text long enough to change its height, and
 * the full text lives only in the sheet. The pixel claim is checked
 * against a real browser in e2e/skills_cards.spec.ts.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, screen, waitFor, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

import Skills from '../../pages/Skills';
import { clearAllGlobalErrors } from '../../hooks/useGlobalErrors';

/** A long description, in the shape `macos_ax` actually ships. */
const LONG = `Read and operate native Mac apps as a TEXT tree instead of pixels. `
  + `macos_ax__snapshot prints every element of an app's windows with a stable ref, `
  + `its role, its label and its screen bounds. PRECONDITIONS, true for every `
  + `endpoint: (1) macOS only; on any other host every call returns 501. (2) The `
  + `Accessibility grant. Every endpoint returns status 403 with error `
  + `'tcc_denied:accessibility' when the process hosting FERAL is not trusted.`;

const SKILLS = [
  {
    skill_id: 'macos_ax',
    name: 'Mac Accessibility',
    description: LONG,
    endpoints: [
      { id: 'snapshot', method: 'PYTHON', description: 'Print the AX tree.', read_only: true },
      { id: 'click', method: 'PYTHON', description: 'Press an element by ref.', read_only: false },
    ],
    endpoint_count: 2,
    trigger_phrases: ['what is on screen', 'click the X button'],
    categories: ['system', 'desktop', 'accessibility'],
    version: '2.0.0',
  },
  {
    skill_id: 'spotify_music',
    name: 'Spotify',
    description: 'Control Spotify playback and search the catalog.',
    endpoints: [{ id: 'now_playing', method: 'PYTHON', description: 'What is playing.', read_only: true }],
    endpoint_count: 1,
    trigger_phrases: ['play music'],
    categories: ['music', 'entertainment', 'media'],
    version: '2.0.0',
  },
];

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

function renderSkills(rows = SKILLS) {
  vi.stubGlobal('fetch', vi.fn((input) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    if (url.includes('/api/skills/pending')) return Promise.resolve(makeResponse(200, { pending: [] }));
    return Promise.resolve(makeResponse(200, rows));
  }));
  return render(<MemoryRouter><Skills /></MemoryRouter>);
}

describe('Skills cards', () => {
  beforeEach(() => { clearAllGlobalErrors(); });
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); clearAllGlobalErrors(); });

  it('shows one line per card, not the whole description', async () => {
    const { container } = renderSkills();
    const card = (await screen.findAllByTestId('v2-skill-card'))[0];

    // The pre-fix card rendered `description` verbatim.
    expect(card.textContent).not.toContain('PRECONDITIONS');
    expect(card.textContent).not.toContain('tcc_denied');
    // It does still say what the skill is, in one sentence.
    expect(card.textContent).toMatch(/Read and operate native Mac apps/);

    // No trigger phrases on the card. They were four chips per card.
    expect(container.querySelectorAll('.v2-skill-card-phrases').length).toBe(0);
  });

  it('keeps card text short enough that content cannot set the height', async () => {
    renderSkills();
    (await screen.findAllByTestId('v2-skill-card'))[0];
    const cards = screen.getAllByTestId('v2-skill-card');
    expect(cards.length).toBe(2);
    for (const card of cards) {
      // 240 chars is roomy for icon + name + id + two clamped lines +
      // chips, and far below the 2,000-char description that made the
      // pre-fix cards differ by 528px.
      expect(card.textContent.length).toBeLessThan(240);
    }
    // And the long and the short skill end up within a hair of each
    // other, which is what "same size" means once the CSS clamps.
    const [long, short] = cards.map((c) => c.textContent.length);
    expect(Math.abs(long - short)).toBeLessThan(120);
  });

  it('renders an icon on every card', async () => {
    const { container } = renderSkills();
    (await screen.findAllByTestId('v2-skill-card'))[0];
    const icons = container.querySelectorAll('.v2-skill-card-icon svg');
    expect(icons.length).toBe(2);
  });

  it('opens a sheet with the full description, phrases and endpoints', async () => {
    renderSkills();
    const card = (await screen.findAllByTestId('v2-skill-card'))[0];
    fireEvent.click(card);

    const dialog = await screen.findByRole('dialog');
    expect(dialog.textContent).toContain('PRECONDITIONS');
    expect(dialog.textContent).toContain('what is on screen');
    expect(dialog.textContent).toContain('macos_ax__snapshot');
    expect(dialog.textContent).toContain('macos_ax__click');
    expect(dialog.textContent).toMatch(/v2\.0\.0/);
  });

  it('says in plain words what hot-reload does', async () => {
    renderSkills();
    fireEvent.click((await screen.findAllByTestId('v2-skill-card'))[0]);
    const dialog = await screen.findByRole('dialog');
    expect(dialog.textContent).toMatch(/from disk/i);
    expect(dialog.textContent).toMatch(/Nothing restarts/i);
  });

  it('offers a way to install a skill and a way to create one', async () => {
    const { container } = renderSkills();
    (await screen.findAllByTestId('v2-skill-card'))[0];

    const hrefs = [...container.querySelectorAll('a')].map((a) => a.getAttribute('href'));
    expect(hrefs).toContain('/marketplace');
    expect(hrefs).toContain('/forge');
  });

  it('renders the endpoint count, which the integer payload made dead code', async () => {
    renderSkills();
    const card = (await screen.findAllByTestId('v2-skill-card'))[0];
    expect(card.textContent).toMatch(/2 endpoints/);
  });

  it('survives a brain that still sends endpoints as an integer', async () => {
    // The old wire shape. The count chip must not read "NaN" or crash.
    renderSkills([{ ...SKILLS[1], endpoints: 1, endpoint_count: undefined }]);
    const card = (await screen.findAllByTestId('v2-skill-card'))[0];
    expect(card.textContent).not.toMatch(/NaN/);
    fireEvent.click(card);
    const dialog = await screen.findByRole('dialog');
    await waitFor(() => expect(dialog.textContent).toMatch(/no endpoint detail/i));
  });
});
