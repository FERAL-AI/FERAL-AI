/**
 * Shell navigation: reachability and geometry, in a real browser.
 *
 * The v2 shell used to carry six ways to move around: a Dock of eight
 * fixed slots, a Hub popup of fifteen tiles, a Menubar that navigated
 * nothing, an ambient layer bound to an undocumented Cmd-Period chord,
 * and two panes inside Chat. It is two now: the Dock and the command
 * palette.
 *
 * jsdom has no layout engine, so every claim below about overlap,
 * horizontal scroll or "exactly one control is lit" that depends on
 * painted geometry has to be measured here. The unit suite pins the
 * index against the router; this file pins what a person sees.
 *
 * What it asserts:
 *   1. All 23 destinations are reachable THROUGH the palette, and each
 *      one lights exactly one Dock control on arrival. A blank Dock is
 *      the bug the old hand-written Hub/Dock mirror shipped twice.
 *   2. No shell chrome overlaps the chat composer.
 *   3. No route scrolls the page sideways, down to a 375px viewport.
 *   4. The open palette never covers the Dock.
 *   5. The system bar trigger opens the same dialog Cmd-K does, and
 *      survives a narrow viewport.
 *   6. The Ask row lands the typed query in the chat composer.
 */
import { test, expect, Page } from '@playwright/test';

/** Every destination the palette indexes, label as rendered. */
const DESTINATIONS: Array<[string, string]> = [
  ['/', 'Home'],
  ['/chat', 'Chat'],
  ['/canvas', 'Canvas'],
  ['/flows', 'Flows'],
  ['/intents', 'Intents'],
  ['/timeline', 'Timeline'],
  ['/apps', 'Apps'],
  ['/apps/publish', 'Publish an app'],
  ['/marketplace', 'Market'],
  ['/skills', 'Skills'],
  ['/forge', 'Forge'],
  ['/webhooks', 'Webhooks'],
  ['/memory', 'Memory'],
  ['/memory/context', 'Memory context'],
  ['/wiki', 'Wiki'],
  ['/identity', 'Identity'],
  ['/devices', 'Devices'],
  ['/geofences', 'Places'],
  ['/health', 'Health'],
  ['/oversight', 'Oversight'],
  ['/glass-brain', 'Brain'],
  ['/agents', 'Agents'],
  ['/settings', 'Settings'],
];

/**
 * One permissive stub for every REST call the shell and its pages make.
 * The point of this file is the chrome, not any page's data, and an
 * unstubbed fetch against the preview server returns index.html, which
 * makes pages throw on JSON.parse and take the shell down with them.
 */
const STUB_BODY = {
  ok: true,
  status: 'ok',
  version: '2026.8.8',
  health: { status: 'ok', skills: { count: 0 } },
  items: [], results: [], skills: [], devices: [], nodes: [], sessions: [],
  memories: [], events: [], routines: [], pending: [], installed: [],
  timeline: [], taskflows: [], intents: [], providers: [], models: [],
  entities: [], approvals: [], messages: [], conversations: [], notes: [],
  // `geofences` is load-bearing rather than decorative: pages/Geofences.jsx
  // does `setFences(d.geofences || d || [])`, so a payload without the key
  // hands the whole response object to `.map` and takes the shell down
  // with an error boundary. Same shape for the other list keys below.
  geofences: [], webhooks: [], agents: [], flows: [], drafts: [], tools: [],
  packages: [], apps: [], alerts: [], episodes: [], documents: [], logs: [],
  snapshots: [], todos: [], history: [], categories: [],
  channels: {}, data: {}, config: {}, identity: {}, metrics: {},
  somatic: { cognitive_load: 0.2, heart_rate: 0 },
  paired_count: 0, online_count: 0, device_count: 0, session_count: 0,
  total: 0,
};

async function stubApi(page: Page) {
  await page.route('**/api/**', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(STUB_BODY),
    });
  });
}

async function openPalette(page: Page) {
  await page.keyboard.press('ControlOrMeta+k');
  await expect(page.getByRole('dialog', { name: 'Command palette' })).toBeVisible();
}

/** Navigate the way a user does: palette, type, click the exact row. */
async function gotoViaPalette(page: Page, to: string, label: string) {
  await openPalette(page);
  await page.locator('.v2-cmdk-search').fill(label);
  const escaped = label.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const row = page.locator('.v2-cmdk-row').filter({
    has: page.locator('.v2-cmdk-row-label', { hasText: new RegExp(`^${escaped}$`) }),
  }).first();
  await expect(row, `palette has no row for ${label}`).toBeVisible();
  await row.click();
  await page.waitForURL((url) => url.pathname === to, { timeout: 5000 });
}

/** Bounding boxes for a selector, in the live layout. */
async function rects(page: Page, selector: string) {
  return page.evaluate((sel) => {
    return [...document.querySelectorAll(sel)]
      .map((el) => {
        const r = el.getBoundingClientRect();
        return { left: r.left, top: r.top, right: r.right, bottom: r.bottom, width: r.width, height: r.height };
      })
      .filter((r) => r.width > 0 && r.height > 0);
  }, selector);
}

type Rect = { left: number; top: number; right: number; bottom: number; width: number; height: number };

function intersects(a: Rect, b: Rect) {
  return !(a.right <= b.left || a.left >= b.right || a.bottom <= b.top || a.top >= b.bottom);
}

test.describe('Shell navigation', () => {
  test('every destination is reachable through the palette and lights exactly one Dock control', async ({ page }) => {
    test.setTimeout(180_000);
    await stubApi(page);
    await page.goto('/');
    await expect(page.locator('.v2-dock')).toBeVisible();

    const unreachable: string[] = [];
    const misLit: string[] = [];

    for (const [to, label] of DESTINATIONS) {
      await openPalette(page);
      await page.locator('.v2-cmdk-search').fill(label);
      const row = page.locator('.v2-cmdk-row').filter({
        has: page.locator('.v2-cmdk-row-label', { hasText: new RegExp(`^${label.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}$`) }),
      }).first();
      await expect(row).toBeVisible();
      await row.click();

      await page.waitForURL((url) => url.pathname === to, { timeout: 5000 })
        .catch(() => unreachable.push(`${label} -> ${to} (landed on ${new URL(page.url()).pathname})`));

      // The Dock must say where you are. Exactly one control, never zero.
      const litCount = await page.locator('.v2-dock-btn.is-active').count();
      if (litCount !== 1) misLit.push(`${to}: ${litCount} lit`);
    }

    expect(unreachable, `destinations the palette could not reach: ${unreachable.join(', ')}`).toEqual([]);
    expect(misLit, `routes where the Dock did not light exactly one control: ${misLit.join(', ')}`).toEqual([]);
  });

  test('no shell chrome overlaps the chat composer', async ({ page }) => {
    await stubApi(page);
    await page.goto('/chat');
    const composer = page.locator('.v2-chat-composer');
    await expect(composer).toBeVisible();

    const [composerRect] = await rects(page, '.v2-chat-composer');
    const chrome = [
      ...(await rects(page, '.v2-dock-list')),
      ...(await rects(page, '.v2-menubar')),
    ];
    const offenders = chrome.filter((r) => intersects(r, composerRect));
    expect(offenders, `chrome overlapping the composer: ${JSON.stringify(offenders)}`).toEqual([]);
  });

  test('no route scrolls the page sideways, at 1280 and at 375', async ({ page }) => {
    test.setTimeout(180_000);
    await stubApi(page);
    const offenders: string[] = [];

    for (const width of [1280, 375]) {
      await page.setViewportSize({ width, height: 800 });
      // Enter through the root and move with the palette, which is how a
      // person reaches these routes. A hard `goto` on the two depth-2
      // routes does not survive the bundle's relative asset base; see the
      // "known gap" test at the bottom of this file.
      await page.goto('/');
      await expect(page.locator('.v2-dock')).toBeVisible();
      for (const [to, label] of DESTINATIONS) {
        await gotoViaPalette(page, to, label);
        await expect(page.locator('.v2-dock'), `no dock at ${width}px on ${to}`).toBeVisible();
        const over = await page.evaluate(() => {
          const de = document.documentElement;
          return {
            doc: de.scrollWidth - de.clientWidth,
            body: document.body.scrollWidth - document.body.clientWidth,
          };
        });
        // 1px of subpixel rounding is not a sideways scroll.
        if (over.doc > 1 || over.body > 1) offenders.push(`${width}px ${to}: doc +${over.doc}, body +${over.body}`);
      }
    }
    expect(offenders, `routes that scroll sideways: ${offenders.join(', ')}`).toEqual([]);
  });

  test('every depth-1 destination survives a hard load', async ({ page }) => {
    test.setTimeout(120_000);
    await stubApi(page);
    const blank: string[] = [];
    for (const [to] of DESTINATIONS) {
      if (to.slice(1).includes('/')) continue; // depth-2, see below
      await page.goto(to);
      const ok = await page.locator('.v2-dock').isVisible().catch(() => false);
      if (!ok) blank.push(to);
    }
    expect(blank, `routes that render nothing on a hard load: ${blank.join(', ')}`).toEqual([]);
  });
  /**
   * FIXED. This was recorded as a `test.fail()` reproduction by the lane
   * that found it, on the reasoning that the suite would go red the
   * moment someone fixed the bundle and the annotation would have to
   * come off. That is exactly what happened, so it is a normal
   * assertion now.
   *
   * `vite.config.js` used to set `base: './'`, with a comment claiming
   * relative
   * asset refs "work at both mount points". They work at the two mount
   * ROOTS. They do not work at any route one level deeper: index.html
   * asks for `./assets/index-<hash>.js`, which the browser resolves
   * against `/apps/publish` to `/apps/assets/index-<hash>.js`, and the
   * SPA fallback answers that with index.html at status 200. The page
   * then executes HTML as JavaScript and renders nothing at all.
   *
   * Measured against `vite preview` on the production bundle:
   *   curl -o /dev/null -w '%{http_code}' /apps/assets/index-<hash>.js
   *     -> 200, and the body is `<!DOCTYPE html>`.
   *
   * It bites `/memory/context`, which has been a shipped navigation
   * destination since before the palette, plus `/apps/publish`,
   * `/apps/:app_id`, `/pair/:device_id/*` and `/setup/legacy`. In-app
   * navigation to all of them was fine; a bookmark, a refresh, or a link
   * someone pasted was a white screen.
   *
   * The base is '/' now, which resolves identically from every route and
   * from every mount point, so the /v2/ alias the relative base existed
   * for is unaffected. Verified against a running brain: all four routes
   * request /assets/index-<hash>.js and receive text/javascript, and a
   * hard load of /memory/context boots the app instead of rendering 0
   * characters into an empty #root.
   */
  test('a hard load of /memory/context renders the shell', async ({ page }) => {
    await stubApi(page);
    await page.goto('/memory/context');
    await expect(page.locator('.v2-dock')).toBeVisible({ timeout: 3000 });
  });

  test('the open palette never covers the Dock', async ({ page }) => {
    await stubApi(page);
    await page.goto('/chat');
    await openPalette(page);

    const [dialog] = await rects(page, '.v2-cmdk');
    const [dock] = await rects(page, '.v2-dock-list');
    expect(dialog).toBeTruthy();
    expect(dock).toBeTruthy();
    expect(
      intersects(dialog, dock),
      `palette ${JSON.stringify(dialog)} overlaps dock ${JSON.stringify(dock)}`,
    ).toBe(false);

    // And it stays inside the viewport rather than running off the top.
    const vp = page.viewportSize()!;
    expect(dialog.top).toBeGreaterThanOrEqual(0);
    expect(dialog.bottom).toBeLessThanOrEqual(vp.height + 1);
  });

  test('the system bar trigger opens the same dialog, and survives a 375px viewport', async ({ page }) => {
    await stubApi(page);
    await page.goto('/');

    const trigger = page.getByRole('button', { name: 'Open the command palette' });
    await expect(trigger).toBeVisible();
    await trigger.click();
    await expect(page.getByRole('dialog', { name: 'Command palette' })).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(page.getByRole('dialog', { name: 'Command palette' })).toBeHidden();

    await page.setViewportSize({ width: 375, height: 700 });
    // The label collapses but the control must not disappear: dropping
    // it would leave Cmd-K as the only way in, and a phone has no
    // Cmd-K.
    await expect(trigger).toBeVisible();
    // The Menubar was retired when the instrument-panel design's single
    // system bar took its place; the palette trigger moved there with
    // the theme and voice controls. The invariant is unchanged: the
    // control stays inside its bar at a phone width.
    const [bar] = await rects(page, '.v2-sysbar');
    const [triggerRect] = await rects(page, '.v2-sysbar-cmd');
    expect(triggerRect.right).toBeLessThanOrEqual(bar.right + 1);
    expect(triggerRect.left).toBeGreaterThanOrEqual(bar.left - 1);
  });

  test('the Ask row hands the typed query to the chat composer', async ({ page }) => {
    await stubApi(page);
    await page.goto('/');
    await openPalette(page);
    await page.locator('.v2-cmdk-search').fill('why did the last flow stall');

    const ask = page.locator('.v2-cmdk-row').filter({ hasText: 'Ask FERAL:' }).first();
    await expect(ask).toBeVisible();
    await ask.click();

    await page.waitForURL((url) => url.pathname === '/chat');
    await expect(page.locator('.v2-chat-input')).toHaveValue('why did the last flow stall');
  });

  test('the palette finds the Dock primaries the Hub could not', async ({ page }) => {
    await stubApi(page);
    await page.goto('/');
    // Verbatim from the work order: the Hub's fifteen items excluded
    // seven of the eight Dock primaries, so this list matched nothing.
    for (const label of ['Chat', 'Devices', 'Home', 'Flows', 'Apps', 'Canvas', 'Settings']) {
      await openPalette(page);
      await page.locator('.v2-cmdk-search').fill(label);
      await expect(
        page.locator('.v2-cmdk-row-label', { hasText: new RegExp(`^${label}$`) }).first(),
        `palette could not find ${label}`,
      ).toBeVisible();
      await page.keyboard.press('Escape');
    }
  });
});
