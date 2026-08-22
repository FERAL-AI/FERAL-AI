/**
 * A fixed overlay must never cover a page control.
 *
 * Two instances of the same shape, found by the real-brain sweep. The
 * serious one: the global error stack is pinned at `top:72 right:20`,
 * which is exactly where a page puts its own action row. Measured on
 * /oversight at 1280x720 with one forced 500, the toast covered
 * "Pause actions" at (1045,71) and "Refresh" at (1182,71) for its full
 * six-second life. "Pause actions" is the supervisor kill switch, so
 * the message telling you something had gone wrong sat on top of the
 * button that stops it.
 *
 * The second: `.v2-chat-pane` resolved to `top: 24px` once the menubar
 * was retired and `--v2-menubar-height` became 0, so the Save pane
 * opened over the Save button that opens it. Clicking Save again to
 * close hit `.v2-chat-pane-hint`.
 *
 * Relocating alone does not fix this class: the opposite corner is
 * already taken by ProactiveToast at `right:20 bottom:130`, and any
 * fixed corner eventually collides with something. The toast is
 * therefore BOTH moved clear of the action row and made transparent to
 * the pointer, and the first attempt at just the latter is why this
 * file also asserts the dismiss button is not itself the thing doing
 * the covering.
 */
import { test, expect } from '@playwright/test';

/** Controls the pointer cannot reach, scrolling each into view first. */
const COVERED = `
  [...document.querySelectorAll('.v2-shell-main button, .v2-shell-main a')]
    .map((el) => {
      const r0 = el.getBoundingClientRect();
      if (!r0.width || !r0.height) return null;
      // Scrolled into view first: without this the check counts "below
      // the fold" as "covered", which reports dozens of controls that
      // are perfectly reachable.
      el.scrollIntoView({ block: 'center', inline: 'nearest' });
      const r = el.getBoundingClientRect();
      if (r.bottom < 0 || r.top > window.innerHeight) return null;
      const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
      if (hit === el || el.contains(hit)) return null;
      return {
        label: (el.getAttribute('aria-label') || el.textContent || '?').trim().slice(0, 30),
        blockedBy: hit ? (hit.className?.toString().slice(0, 50) || hit.tagName) : 'nothing',
      };
    })
    .filter(Boolean)
`;

test('the error toast never covers a page control', async ({ page }) => {
  await page.route('**/api/**', (r) => r.fulfill({
    status: 200, contentType: 'application/json', body: '{}',
  }));
  // One real failure, which is what raises the global stack.
  await page.route('**/api/supervisor**', (r) => r.fulfill({
    status: 500, contentType: 'application/json', body: JSON.stringify({ error: 'boom' }),
  }));

  await page.setViewportSize({ width: 1280, height: 720 });
  await page.goto('/oversight');

  const toast = page.locator('[data-testid="error-toast-stack"]');
  await expect(toast, 'no error surface, so this test proves nothing').toBeVisible();

  // The stack must not take clicks at all.
  await expect(toast).toHaveCSS('pointer-events', 'none');

  const covered = await page.evaluate(COVERED) as { label: string; blockedBy: string }[];
  expect(
    covered,
    `covered while the toast is up: ${covered.map((c) => `${c.label} by ${c.blockedBy}`).join(', ')}`,
  ).toEqual([]);
});

test('the kill switch stays clickable while an error is on screen', async ({ page }) => {
  // The specific failure: you cannot stop the thing that is erroring
  // because the error is sitting on the stop button.
  await page.route('**/api/**', (r) => r.fulfill({
    status: 200, contentType: 'application/json', body: '{}',
  }));
  await page.route('**/api/supervisor**', (r) => r.fulfill({
    status: 500, contentType: 'application/json', body: JSON.stringify({ error: 'boom' }),
  }));

  await page.setViewportSize({ width: 1280, height: 720 });
  await page.goto('/oversight');
  await expect(page.locator('[data-testid="error-toast-stack"]')).toBeVisible();

  for (const name of [/pause actions/i, /^refresh$/i]) {
    const btn = page.getByRole('button', { name }).first();
    if (await btn.count()) {
      // Playwright's own actionability check refuses a click that
      // another element would intercept, so this fails for the right
      // reason rather than needing a hit test.
      await btn.click({ timeout: 3000 });
    }
  }
});

test('the toast is still dismissable, and its own X covers nothing', async ({ page }) => {
  // The first fix made the stack pointer-transparent but re-enabled the
  // dismiss button, and at top:72 that one element landed exactly on
  // "Refresh". A toast you cannot dismiss is not the answer either.
  await page.route('**/api/**', (r) => r.fulfill({
    status: 200, contentType: 'application/json', body: '{}',
  }));
  await page.route('**/api/supervisor**', (r) => r.fulfill({
    status: 500, contentType: 'application/json', body: JSON.stringify({ error: 'boom' }),
  }));

  await page.setViewportSize({ width: 1280, height: 720 });
  await page.goto('/oversight');

  const cards = page.locator('[data-testid="error-toast-stack"] .v2-error-toast-card');
  // Wait for the stack rather than counting straight after goto: the
  // failing call happens after mount, so an immediate count reads 0 and
  // fails for a timing reason with nothing to do with the fix.
  await expect(cards.first()).toBeVisible();
  const before = await cards.count();
  expect(before).toBeGreaterThan(0);

  const dismiss = page.getByRole('button', { name: /dismiss error/i }).first();
  await expect(dismiss).toBeVisible();
  await dismiss.click({ timeout: 3000 });

  // One card goes, not necessarily the whole stack: a page that failed
  // several calls queues several, and asserting the stack empties made
  // this fail for a reason that has nothing to do with the fix.
  await expect(cards).toHaveCount(before - 1);
});

test('the chat save pane opens below the control that opens it', async ({ page }) => {
  await page.route('**/api/**', (r) => r.fulfill({
    status: 200, contentType: 'application/json', body: '{}',
  }));
  await page.setViewportSize({ width: 1280, height: 720 });
  await page.goto('/chat');

  const save = page.getByRole('button', { name: /save/i }).first();
  await save.click();
  await expect(page.locator('.v2-chat-pane')).toBeVisible();

  const geo = await page.evaluate(() => {
    const pane = document.querySelector('.v2-chat-pane')!.getBoundingClientRect();
    const btn = [...document.querySelectorAll('button')]
      .find((b) => /save/i.test(b.textContent || ''))!.getBoundingClientRect();
    return { paneTop: pane.top, saveBottom: btn.bottom };
  });
  expect(
    geo.paneTop,
    `the pane opens at ${geo.paneTop} over its own toggle, which ends at ${geo.saveBottom}`,
  ).toBeGreaterThanOrEqual(geo.saveBottom);

  // And the toggle still closes it, which is what the overlap broke.
  await save.click({ timeout: 3000 });
  await expect(page.locator('.v2-chat-pane')).toHaveCount(0);
});
