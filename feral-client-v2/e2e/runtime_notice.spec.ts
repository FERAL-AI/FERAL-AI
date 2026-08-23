/**
 * The runtime notice strip, measured in a browser that has layout.
 *
 * WHAT IT REPORTS. A Python process never reloads its source, so
 * `pip install --upgrade feral-ai` against a brain that is already
 * serving succeeds and changes nothing: the process keeps executing the
 * code it read at import. `GET /api/dashboard` now carries a `runtime`
 * block saying whether the running version and the installed version
 * agree. One real install served two days and one hour from a build
 * that predated four releases with nothing on screen to say so.
 *
 * WHY THIS FILE EXISTS ALONGSIDE THE VITEST SUITE. The unit tests run
 * in jsdom, which has no layout: every box is 0x0 and no stylesheet is
 * applied, so a component can pass them while being invisible,
 * zero-height, or underneath something else. Both of those have
 * happened in this client recently (a hot-reload banner at y = -3365px,
 * and the error toast covering the supervisor kill switch, each with
 * green unit tests). The four things below can only be established
 * here:
 *
 *   1. it is on screen, with real height, directly under the system bar
 *   2. the page is laid out BELOW it, not underneath it: this strip has
 *      no dismiss control by design, so a strip that covered something
 *      would cover it forever
 *   3. no control anywhere on the page becomes unreachable
 *   4. the healthy brain gets no strip and no reserved pixel
 *
 * ONE TRAP, DOCUMENTED. `elementFromPoint` on an element that is
 * scrolled below the fold reports whatever is painted at those
 * coordinates, which reads as "covered". Doing that without scrolling
 * first produced 58 false positives here once. Every hit test below
 * scrolls its target into view first, exactly as
 * `overlays_never_cover_controls.spec.ts` does.
 */
import { test, expect } from '@playwright/test';

const STALE_RUNTIME = {
  running_version: '2026.8.21',
  installed_version: '2026.8.25',
  stale: true,
  uptime_s: 179460.0,
  pid: 12061,
  detail: 'This brain is running 2026.8.21 but 2026.8.25 is installed. '
    + 'A running process never reloads its code, so the upgrade has not taken '
    + 'effect. Restart the brain (`feral restart`, or stop and re-run '
    + '`feral serve`) to pick it up. Uptime 2d 1h.',
};

const HEALTHY_RUNTIME = {
  running_version: '2026.8.25',
  installed_version: '2026.8.25',
  stale: false,
  uptime_s: 12.0,
  pid: 12061,
  detail: 'Running the installed version (2026.8.25).',
};

const DASHBOARD = {
  device_count: 1, online_count: 1, skills_count: 4, llm_available: true,
  uptime_s: 179460, memory: {}, budget: {}, autonomy: 'loose',
  channels: [], devices: [], boot: {}, health: {},
};

/**
 * Playwright matches the MOST RECENTLY registered route first, so the
 * catch-all is registered before the specific one. The other way round
 * every dashboard stub is shadowed by `{}` and the strip never appears,
 * which would make this whole file pass for the wrong reason.
 */
async function stub(page, runtime: object | null) {
  await page.route('**/api/**', (r) => r.fulfill({
    status: 200, contentType: 'application/json', body: '{}',
  }));
  await page.route('**/api/dashboard*', (r) => r.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(runtime ? { ...DASHBOARD, runtime } : DASHBOARD),
  }));
}

/** Controls the pointer cannot reach, each scrolled into view first. */
const COVERED = `
  [...document.querySelectorAll('.v2-shell-main button, .v2-shell-main a, .v2-rail button, .v2-sysbar button')]
    .map((el) => {
      const r0 = el.getBoundingClientRect();
      if (!r0.width || !r0.height) return null;
      // Without this, "below the fold" reads as "covered": 58 false
      // positives the first time this probe was written.
      el.scrollIntoView({ block: 'center', inline: 'nearest' });
      const r = el.getBoundingClientRect();
      if (r.bottom < 0 || r.top > window.innerHeight) return null;
      const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
      if (hit === el || el.contains(hit)) return null;
      return {
        label: (el.getAttribute('aria-label') || el.textContent || '?').trim().slice(0, 30),
        blockedBy: hit ? (hit.className?.toString().slice(0, 60) || hit.tagName) : 'nothing',
      };
    })
    .filter(Boolean)
`;

test('a healthy brain gets no strip and no reserved space', async ({ page }) => {
  await stub(page, HEALTHY_RUNTIME);
  await page.goto('/console');
  await expect(page.locator('.v2-sysbar')).toBeVisible();
  // Wait for a dashboard-fed vital so we know the payload landed and
  // this is not just "the poll has not answered yet".
  await expect(page.locator('.v2-ext[aria-label*="Autonomy"]')).toBeVisible();

  await expect(page.locator('[data-testid="runtime-notice"]')).toHaveCount(0);

  const geo = await page.evaluate(() => {
    const shell = document.querySelector('.v2-shell') as HTMLElement;
    const body = document.querySelector('.v2-shell-body')!.getBoundingClientRect();
    const bar = document.querySelector('.v2-sysbar')!.getBoundingClientRect();
    return {
      rowsVar: shell.style.getPropertyValue('--v2-runtime-notice-rows'),
      bodyTop: body.top,
      barBottom: bar.bottom,
    };
  });
  // Not one pixel of chrome is reserved for a strip that is not there.
  expect(geo.rowsVar).toBe('');
  expect(geo.bodyTop).toBeCloseTo(geo.barBottom, 0);
});

test('a stale brain gets a visible strip that says what to do', async ({ page }) => {
  await stub(page, STALE_RUNTIME);
  await page.goto('/console');

  const strip = page.locator('[data-testid="runtime-notice"]');
  await expect(strip).toBeVisible();

  // The remedy, in words, on screen. Not just "something is wrong".
  await expect(strip).toContainText('Restart FERAL to finish updating.');
  await expect(strip).toContainText('feral restart');
  await expect(strip).toContainText('2026.8.21');
  await expect(strip).toContainText('2026.8.25');

  // A status message, and reachable: the copy control is a real button
  // that takes focus from the keyboard.
  await expect(strip).toHaveAttribute('role', 'status');
  const copy = page.getByRole('button', { name: /copy feral restart/i });
  await expect(copy).toBeVisible();
  await copy.focus();
  await expect(copy).toBeFocused();

  // Nothing dismisses it, because nothing but a restart clears the
  // condition it reports.
  await expect(strip.getByRole('button')).toHaveCount(1);
});

for (const width of [1512, 1280, 900, 768]) {
  test(`the strip has real height and the page starts below it at ${width}px`, async ({ page }) => {
    await stub(page, STALE_RUNTIME);
    await page.setViewportSize({ width, height: 800 });
    await page.goto('/console');
    await expect(page.locator('[data-testid="runtime-notice"]')).toBeVisible();

    const geo = await page.evaluate(() => {
      const strip = document.querySelector('[data-testid="runtime-notice"]')!.getBoundingClientRect();
      const bar = document.querySelector('.v2-sysbar')!.getBoundingClientRect();
      const body = document.querySelector('.v2-shell-body')!.getBoundingClientRect();
      const main = document.querySelector('.v2-shell-main')!.getBoundingClientRect();
      // `.v2-rail` is `display: none` under 900px, and a hidden element
      // reports an all-zero rect. Reading that as a top of 0 would fail
      // this spec at 768px for a reason that has nothing to do with the
      // strip, so a rail with no box is reported as absent instead.
      const railEl = document.querySelector('.v2-rail');
      const rail = railEl ? railEl.getBoundingClientRect() : null;
      return {
        strip: { top: strip.top, bottom: strip.bottom, height: strip.height, width: strip.width },
        barBottom: bar.bottom,
        bodyTop: body.top,
        railTop: rail && rail.height > 0 ? rail.top : null,
        mainTop: main.top,
        viewportWidth: window.innerWidth,
      };
    });

    // Painted, not collapsed and not off screen. The banner that
    // rendered at y = -3365px passed a jsdom visibility test.
    expect(geo.strip.height, 'the strip has no height').toBeGreaterThan(16);
    expect(geo.strip.top, 'the strip is above the viewport').toBeGreaterThanOrEqual(0);
    expect(geo.strip.width).toBeCloseTo(geo.viewportWidth, 0);

    // Directly under the system bar, with no gap and no overlap.
    expect(geo.strip.top).toBeCloseTo(geo.barBottom, 0);

    // THE LOAD-BEARING ONE: the page begins where the strip ends. A
    // strip with no dismiss control that overlapped the page would
    // cover whatever is under it for as long as the brain stays stale.
    expect(
      geo.bodyTop,
      `the page starts at ${geo.bodyTop} but the strip runs to ${geo.strip.bottom}`,
    ).toBeGreaterThanOrEqual(geo.strip.bottom - 0.5);
    if (geo.railTop !== null) {
      expect(geo.railTop).toBeGreaterThanOrEqual(geo.strip.bottom - 0.5);
    }
    expect(geo.mainTop).toBeGreaterThanOrEqual(geo.strip.bottom - 0.5);
  });
}

test('the strip covers no control on the page, the rail or the bar', async ({ page }) => {
  await stub(page, STALE_RUNTIME);
  await page.setViewportSize({ width: 1280, height: 720 });
  await page.goto('/console');
  await expect(page.locator('[data-testid="runtime-notice"]')).toBeVisible();

  const covered = await page.evaluate(COVERED) as { label: string; blockedBy: string }[];
  expect(
    covered,
    `unreachable while the strip is up: ${covered.map((c) => `${c.label} by ${c.blockedBy}`).join(', ')}`,
  ).toEqual([]);
});

test('the chat snapshots pane still opens below its own toggle', async ({ page }) => {
  // The strip pushes the page down, and anything anchored to the top of
  // the page area has to follow it. `.v2-chat-pane` is fixed-position
  // and was anchored to `--v2-sysbar-height`, which does not move: it
  // would have re-opened over the Save button that opens it, which is
  // the exact defect `overlays_never_cover_controls.spec.ts` records
  // from when the menubar was retired.
  await stub(page, STALE_RUNTIME);
  await page.setViewportSize({ width: 1280, height: 720 });
  await page.goto('/chat');
  await expect(page.locator('[data-testid="runtime-notice"]')).toBeVisible();

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

  await save.click({ timeout: 3000 });
  await expect(page.locator('.v2-chat-pane')).toHaveCount(0);
});
