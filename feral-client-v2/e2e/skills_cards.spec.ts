/**
 * Skills page: same-size cards, and a hot-reload outcome the user can
 * actually see.
 *
 * These two claims are pixel claims, and jsdom cannot check either of
 * them: it has no layout engine, so every element there has a zero box
 * and no CSS custom property resolves. The unit tests
 * (`src/__tests__/pages/Skills.cards.test.jsx`) pin the mechanism, this
 * pins the result in a real browser.
 *
 * What was measured before the fix, driving the real page against a live
 * brain with 42 skills:
 *   - card heights in one grid: 1055, 1055, 1055, 547, 547, 547, 527, …
 *   - clicking Hot-reload on the first card put the outcome banner at
 *     y = -71px; on a lower card, y = -3365px. Off-screen both times,
 *     for success and for failure alike, which is why the button read as
 *     doing nothing.
 *
 * Run: cd feral-client-v2 && npx playwright test e2e/skills_cards.spec.ts
 */
import { test, expect } from '@playwright/test';

/**
 * Two skills whose descriptions differ by more than 20x, which is the
 * real spread: `weather_current` ships 63 characters and `macos_ax`
 * ships over 2,000. If content still drove height, these two cards
 * could not come out the same size.
 */
const LONG_DESCRIPTION = 'Read and operate native Mac apps as a TEXT tree instead of pixels. '
  + 'macos_ax__snapshot prints every element of an app\'s windows with a stable ref, its role, '
  + 'its label and its screen bounds ([ax12] AXButton "Back" (24,105 28x28)); macos_ax__click '
  + 'then presses ax12 by name. This is the desktop equivalent of browser__snapshot / '
  + 'browser__click and needs no screenshot and no image model, so it is the RIGHT TOOL '
  + 'whenever the question is "what is on screen", "what can I click", or "click the X button" '
  + 'in a Mac app. PRECONDITIONS, true for every endpoint: (1) macOS only; on any other host '
  + 'every call returns 501. (2) The Accessibility grant. Every endpoint returns status 403 '
  + 'with error \'tcc_denied:accessibility\' when the process hosting FERAL is not trusted, and '
  + 'the user must switch FERAL on under System Settings -> Privacy & Security -> Accessibility '
  + 'and then the brain must be restarted. (3) The target app must already be running.';

const SKILLS = [
  {
    skill_id: 'macos_ax',
    name: 'Mac Accessibility',
    description: LONG_DESCRIPTION,
    endpoints: [
      { id: 'snapshot', method: 'PYTHON', description: 'Print the AX tree of an app.', read_only: true },
      { id: 'click', method: 'PYTHON', description: 'Press an element by its ref.', read_only: false },
    ],
    endpoint_count: 2,
    trigger_phrases: ['what is on screen', 'click the X button', 'read the window'],
    categories: ['system', 'desktop', 'accessibility'],
    version: '2.0.0',
  },
  {
    skill_id: 'weather_current',
    name: 'Weather',
    description: 'Get current weather conditions and forecasts for any location.',
    endpoints: [],
    endpoint_count: 0,
    trigger_phrases: ["what's the weather"],
    categories: ['weather', 'utility'],
    version: '1.0.0',
  },
  {
    skill_id: 'spotify_music',
    name: 'Spotify',
    description: 'Control Spotify playback (play/pause, skip forward/back, queue, set volume, '
      + 'play playlist), search the catalog, and list the user\'s playlists.',
    endpoints: [{ id: 'now_playing', method: 'PYTHON', description: 'What is playing now.', read_only: true }],
    endpoint_count: 1,
    trigger_phrases: ['play music', 'skip this song'],
    categories: ['music', 'entertainment', 'media'],
    version: '2.0.0',
  },
];

async function stubBrain(page, reload?: { status: number; body: object }) {
  await page.route('**/api/skills/reload*', async (route) => {
    const answer = reload || { status: 200, body: { ok: true, skill_id: 'macos_ax' } };
    await route.fulfill({
      status: answer.status,
      contentType: 'application/json',
      body: JSON.stringify(answer.body),
    });
  });
  await page.route('**/api/skills/pending', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ pending: [] }) });
  });
  // `/skills` is both an SPA route and a REST route, which is the
  // collision api/server.py resolves by content negotiation (see the
  // note above `@app.get("/skills")` there). Playwright's pattern
  // matches the page navigation too, so the document request has to fall
  // through to the real server or the browser is handed JSON instead of
  // the app.
  await page.route('**/skills', async (route) => {
    if (route.request().resourceType() === 'document') {
      await route.fallback();
      return;
    }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(SKILLS) });
  });
}

async function openSkills(page) {
  await page.goto('/skills');
  await expect(page.getByTestId('v2-skill-card').first()).toBeVisible();
}

test.describe('Skills page cards', () => {
  test('every card is the same size regardless of description length', async ({ page }) => {
    await stubBrain(page);
    await openSkills(page);

    const boxes = await page.getByTestId('v2-skill-card').evaluateAll(
      (els) => els.map((e) => {
        const r = e.getBoundingClientRect();
        return { h: Math.round(r.height), w: Math.round(r.width) };
      }),
    );
    expect(boxes.length).toBe(SKILLS.length);

    const heights = boxes.map((b) => b.h);
    expect(Math.min(...heights)).toBeGreaterThan(0);
    // Identical, not merely close. The height is set in one place
    // (--v2-skill-card-h in styles/pages.css), so any spread at all
    // means content is driving it again.
    expect(Math.max(...heights) - Math.min(...heights)).toBe(0);
  });

  test('no card overflows its box, so the 2000-char description is really clamped', async ({ page }) => {
    await stubBrain(page);
    await openSkills(page);

    const overflow = await page.getByTestId('v2-skill-card').evaluateAll(
      (els) => els.map((e) => e.scrollHeight - e.clientHeight),
    );
    // A clamped card scrolls no further than it shows. Before the fix
    // the card had no height at all and simply grew.
    for (const o of overflow) expect(o).toBeLessThanOrEqual(1);
  });

  test('each card carries an icon that has painted', async ({ page }) => {
    await stubBrain(page);
    await openSkills(page);

    const icons = page.locator('.v2-skill-card-icon svg');
    await expect(icons).toHaveCount(SKILLS.length);
    const box = await icons.first().boundingBox();
    expect(box, 'the icon has no layout box').not.toBeNull();
    expect(box!.width).toBeGreaterThan(8);
    expect(box!.height).toBeGreaterThan(8);
  });

  test('a successful hot-reload is reported inside the viewport', async ({ page }) => {
    await stubBrain(page);
    await openSkills(page);

    // The LAST card, i.e. the case that used to put the banner
    // thousands of pixels above the click.
    const card = page.getByTestId('v2-skill-card').last();
    await card.scrollIntoViewIfNeeded();
    await card.click();

    await page.getByTestId('v2-skill-reload').click();
    const result = page.getByTestId('v2-skill-reload-ok');
    await expect(result).toBeVisible();

    const box = await result.boundingBox();
    const viewport = page.viewportSize()!;
    expect(box, 'the outcome has no layout box').not.toBeNull();
    expect(box!.y).toBeGreaterThanOrEqual(0);
    expect(box!.y).toBeLessThanOrEqual(viewport.height);
  });

  test('a refused hot-reload is reported inside the viewport, with the reason', async ({ page }) => {
    await stubBrain(page, {
      status: 409,
      body: {
        ok: false,
        skill_id: 'weather_current',
        code: 'no_source',
        error: "nothing on disk to reload for 'weather_current'",
      },
    });
    await openSkills(page);

    const card = page.locator('[data-skill-id="weather_current"]');
    await card.scrollIntoViewIfNeeded();
    await card.click();
    await page.getByTestId('v2-skill-reload').click();

    const result = page.getByTestId('v2-skill-reload-error');
    await expect(result).toBeVisible();
    await expect(result).toContainText(/nothing on disk to reload/i);
    await expect(result).toContainText(/still what is running/i);
    // The `no_source` case gets the one explanation that actually helps:
    // this skill lives in the brain's Python, not in a file.
    await expect(result).toContainText(/nothing on disk to re-read/i);

    const box = await result.boundingBox();
    const viewport = page.viewportSize()!;
    expect(box).not.toBeNull();
    expect(box!.y).toBeGreaterThanOrEqual(0);
    expect(box!.y).toBeLessThanOrEqual(viewport.height);
  });

  test('the page offers a route to the marketplace and to the forge', async ({ page }) => {
    await stubBrain(page);
    await openSkills(page);

    const market = page.getByRole('link', { name: /install a skill/i });
    const forge = page.getByRole('link', { name: /create a skill/i });
    await expect(market).toBeVisible();
    await expect(forge).toBeVisible();
    await expect(market).toHaveAttribute('href', '/marketplace');
    await expect(forge).toHaveAttribute('href', '/forge');
  });
});
