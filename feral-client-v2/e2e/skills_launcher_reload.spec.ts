/**
 * Skills launcher — the hot-reload outcome has to be visible in a real
 * browser, not only in the DOM.
 *
 * The unit test (`src/__tests__/components/SkillsLauncher.reload-reporting.test.jsx`)
 * proves the component reports a failed reload. It cannot prove the
 * report is legible: jsdom has no layout engine, resolves no CSS custom
 * properties and paints nothing, so an inline style reading
 * `var(--v2-state-error)` looks identical there whether the token exists
 * or not. The note in `SkillsLauncher.jsx` is styled from tokens.css
 * through inline styles (the component has no stylesheet of its own and
 * stylesheets are owned elsewhere), which is exactly the arrangement
 * jsdom cannot check.
 *
 * So this drives the built bundle in Chrome and asserts the things only
 * a browser knows: the note is on screen, it has a real box, its colour
 * resolved to something other than the initial value, and it does not
 * push the launcher into a horizontal overflow.
 *
 * Run: cd feral-client-v2 && npx playwright test e2e/skills_launcher_reload.spec.ts
 */
import { test, expect } from '@playwright/test';

test.use({ channel: 'chrome' });

const SKILLS = [
  {
    skill_id: 'calendar_google',
    name: 'Google Calendar',
    description: 'Calendar access',
    endpoints: [],
    trigger_phrases: ['what is on my calendar'],
  },
  {
    skill_id: 'weather_current',
    name: 'Weather',
    description: 'Current conditions',
    endpoints: [],
    trigger_phrases: [],
  },
];

async function stubBrain(page, reload: { status: number; body: object }) {
  await page.route('**/api/skills/reload*', async (route) => {
    await route.fulfill({
      status: reload.status,
      contentType: 'application/json',
      body: JSON.stringify(reload.body),
    });
  });
  await page.route('**/skills', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(SKILLS),
    });
  });
}

async function openLauncher(page) {
  await page.goto('/');
  await page.getByRole('button', { name: /view all/i }).first().click();
  await expect(page.getByRole('dialog', { name: /all skills/i })).toBeVisible();
}

async function reloadFirstSkill(page) {
  await page.getByRole('button', { name: /hot-reload skill/i }).first().click();
}

test.describe('SkillsLauncher hot-reload outcome', () => {
  test('a refused reload is visible, legible and inside the panel', async ({ page }) => {
    // 409 is what the brain answers for a reload it cannot perform.
    await stubBrain(page, {
      status: 409,
      body: {
        ok: false,
        skill_id: 'calendar_google',
        code: 'no_source',
        error: "nothing on disk to reload for 'calendar_google'",
      },
    });
    await openLauncher(page);
    await reloadFirstSkill(page);

    const note = page.getByTestId('skill-reload-failed-calendar_google');
    await expect(note).toBeVisible();
    await expect(note).toContainText(/nothing on disk to reload/i);
    await expect(note).toContainText(/still what is running/i);

    const box = await note.boundingBox();
    expect(box, 'the note has no layout box at all').not.toBeNull();
    expect(box!.height).toBeGreaterThan(10);
    expect(box!.width).toBeGreaterThan(50);

    // The colour must come from the token, i.e. it must have resolved to
    // a real colour rather than falling back to the inherited text
    // colour because `--v2-state-error` was not in scope here.
    const painted = await note.evaluate((el) => {
      const style = getComputedStyle(el);
      const root = getComputedStyle(document.documentElement);
      return {
        color: style.color,
        background: style.backgroundColor,
        token: root.getPropertyValue('--v2-state-error').trim(),
      };
    });
    expect(painted.token, '--v2-state-error is not defined on :root').not.toBe('');
    expect(painted.color).not.toBe('rgba(0, 0, 0, 0)');
    expect(painted.background).not.toBe('rgba(0, 0, 0, 0)');

    // And it must not blow the panel out sideways.
    const overflow = await page.evaluate(() => {
      const panel = document.querySelector('.v2-skills-launcher');
      if (!panel) return null;
      return { scroll: panel.scrollWidth, client: panel.clientWidth };
    });
    expect(overflow).not.toBeNull();
    expect(overflow!.scroll).toBeLessThanOrEqual(overflow!.client + 1);

    // Retry re-posts rather than only clearing the message.
    let posts = 0;
    await page.route('**/api/skills/reload*', async (route) => {
      posts += 1;
      await route.fulfill({
        status: 409,
        contentType: 'application/json',
        body: JSON.stringify({ ok: false, error: 'still nothing on disk' }),
      });
    });
    await note.getByRole('button', { name: /retry/i }).click();
    await expect(page.getByTestId('skill-reload-failed-calendar_google')).toBeVisible();
    expect(posts).toBe(1);
  });

  test('a 200 that says ok:false is reported as a failure, not a success', async ({ page }) => {
    // The shape a brain older than the reload-status fix still sends.
    await stubBrain(page, {
      status: 200,
      body: { ok: false, skill_id: 'calendar_google' },
    });
    await openLauncher(page);
    await reloadFirstSkill(page);

    await expect(page.getByTestId('skill-reload-failed-calendar_google')).toBeVisible();
    await expect(page.getByTestId('skill-reload-ok-calendar_google')).toHaveCount(0);
  });

  test('a reload that happened is confirmed', async ({ page }) => {
    await stubBrain(page, {
      status: 200,
      body: { ok: true, skill_id: 'calendar_google' },
    });
    await openLauncher(page);
    await reloadFirstSkill(page);

    const note = page.getByTestId('skill-reload-ok-calendar_google');
    await expect(note).toBeVisible();
    await expect(note).toContainText(/Hot-reloaded calendar_google/i);
    const box = await note.boundingBox();
    expect(box!.height).toBeGreaterThan(10);
  });
});
