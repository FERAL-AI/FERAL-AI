/**
 * Click everything, against a LIVE brain, and report what happened.
 *
 * Opt-in: needs `FERAL_E2E_REAL_BRAIN=1` and `FERAL_E2E_URL`.
 *
 * WARNING: this lane MUTATES the brain it points at. It clicks by
 * blocklist, not by allowlist, because the work order's rule is "skip
 * anything destructive but DO exercise reads, refreshes, tabs, filters,
 * modals and navigation" and an allowlist of known-safe labels would
 * quietly stop covering every control added after it was written. Point
 * it at a disposable `FERAL_HOME`. The blocklist lives in
 * `real_brain_util.ts` as `DESTRUCTIVE`.
 *
 * Three defects are asserted on, per the work order:
 *
 *   dead      the control does nothing observable at all: no request,
 *             no navigation, no DOM change. Reported, not failed: a
 *             toggle that is already in the clicked state is legitimately
 *             inert, and only a human can tell those apart. The full
 *             list is printed so it can be read.
 *   threw     the click produced an uncaught error or a console error.
 *             This fails.
 *   lied      a request failed (>=400, or the browser could not complete
 *             it) and the page showed no error toast. "Reports success
 *             while the request failed" is the shape. This fails.
 */
import { test, expect } from '@playwright/test';
import {
  REAL_BRAIN, SKIP_REASON, readDestinations, record, settle,
  enumerateControls, badStatuses, failedRequests, CONTROL_SELECTOR, Control,
} from './real_brain_util';

const DESTINATIONS = readDestinations();

test.skip(!REAL_BRAIN, SKIP_REASON);

/**
 * A fake microphone, granted up front.
 *
 * The voice control sits in the system bar on EVERY route, so without
 * this the walk reports the same headless-Chromium `getUserMedia`
 * failure 28 times and drowns out every real finding. Granting a fake
 * capture device exercises the control for real instead of skipping it.
 */
test.use({
  permissions: ['microphone'],
  launchOptions: {
    args: [
      '--use-fake-device-for-media-stream',
      '--use-fake-ui-for-media-stream',
      '--autoplay-policy=no-user-gesture-required',
    ],
  },
});

type Outcome = {
  route: string;
  label: string;
  tag: string;
  result: string;
  detail: string;
};

/**
 * What the page looks like, for the purpose of "did that click do
 * anything".
 *
 * `<main>`'s HTML alone is not enough and getting that wrong produced a
 * measured false accusation: the rail toggle, the theme toggle and
 * every system-bar vital popover change the DOM entirely outside
 * `<main>`, so a first pass reported "Collapse the rail", "Switch to
 * dark mode", "NEEDS YOU" and "RUNNING" as dead controls when all four
 * work. The fingerprint therefore names each surface a control can
 * legitimately move: the page, the theme class on <html>, the rail, any
 * popover, any dialog, and the voice overlay's hidden state.
 *
 * `states` is the same lesson again for accordions. A work-rail section
 * head only flips its own `aria-expanded` and rewrites the rail's
 * insides, both invisible to everything above, so collapsing "Recent"
 * read as a dead control too. Reading the ARIA state of every control
 * on the page catches accordions, tabs, toggles and switches at once,
 * and unlike diffing the rail's HTML it does not go off every time a
 * poll lands.
 */
async function fingerprint(page: import('@playwright/test').Page) {
  return page.evaluate(() => {
    const main = document.querySelector('main.v2-shell-main');
    const overlay = document.querySelector('.v2-voice-overlay');
    return JSON.stringify({
      main: main ? main.innerHTML : '',
      theme: document.documentElement.className,
      rail: !!document.querySelector('.v2-rail'),
      pops: document.querySelectorAll('.v2-pop').length,
      dialogs: document.querySelectorAll('[role="dialog"]').length,
      voice: overlay ? overlay.getAttribute('aria-hidden') : null,
      details: [...document.querySelectorAll('details')].map((d) => (d as HTMLDetailsElement).open).join(','),
      states: [...document.querySelectorAll(
        '[aria-expanded],[aria-pressed],[aria-selected],[aria-checked]',
      )].map((el) => [
        el.getAttribute('aria-expanded'), el.getAttribute('aria-pressed'),
        el.getAttribute('aria-selected'), el.getAttribute('aria-checked'),
      ].join('')).join('|'),
    });
  });
}

/**
 * How many "something went wrong" surfaces are visible right now.
 *
 * NOT just `.v2-error-toast-card`. Counting only the global toast
 * produced a measured false accusation: clicking "Start sharing" on
 * /devices gets a 400 from POST /api/devices/pair, and
 * `hooks/usePerceptionShare.js:144` uses a bare `fetch`, not `apiFetch`,
 * so `pushGlobalError` never runs and no toast appears. The page is not
 * lying: `components/PerceptionShare.jsx:66-69` renders the message in
 * an inline `.v2-chip--error`. A guard that knows about one error
 * surface and not the other reports working code as broken, which is
 * the same disease as reporting broken code as working.
 *
 * Counted before and after each click, because a surface that was
 * already on screen is not this click's answer.
 */
async function errorSurfaces(page: import('@playwright/test').Page) {
  return page.evaluate(() => {
    const sel = '.v2-error-toast-card, [role="alert"], [class*="error" i], [class*="Error"]';
    let n = 0;
    for (const el of document.querySelectorAll(sel)) {
      const r = el.getBoundingClientRect();
      if (r.width > 0 && r.height > 0) n += 1;
    }
    return n;
  });
}

/**
 * Controls inside `<main>` that `overlaySelector` is painted on top of.
 *
 * Every control is scrolled into view before its centre is hit-tested.
 * Without that, `elementFromPoint` returns whatever is at those
 * coordinates in the viewport and a control merely scrolled below the
 * fold reads as "covered", which manufactures findings by the dozen.
 * A rect is only reported when it BOTH intersects the overlay AND
 * fails the hit test, so a control that is simply off screen is never
 * counted.
 */
async function coveredControls(
  page: import('@playwright/test').Page,
  overlaySelector: string,
) {
  return page.evaluate((sel) => {
    const overlay = document.querySelector(sel);
    if (!overlay) return [`no overlay matching ${sel}`];
    const out: string[] = [];
    for (const el of document.querySelectorAll(
      'main.v2-shell-main button, main.v2-shell-main a[href]',
    )) {
      el.scrollIntoView({ block: 'center', inline: 'nearest' });
      const o = overlay.getBoundingClientRect();
      const r = el.getBoundingClientRect();
      if (r.width === 0 || r.height === 0) continue;
      // Off screen after scrolling is not covered, it is off screen.
      if (r.bottom < 0 || r.top > window.innerHeight) continue;
      const overlaps = !(r.right <= o.left || r.left >= o.right
        || r.bottom <= o.top || r.top >= o.bottom);
      if (!overlaps) continue;
      const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
      if (el.contains(hit) || el === hit) continue;
      const name = (el.getAttribute('aria-label') || (el as HTMLElement).innerText || '').trim();
      out.push(
        `${name || el.className} at ${r.left.toFixed(0)},${r.top.toFixed(0)}`
        + ` covered by ${(hit as Element)?.className || hit?.nodeName}`,
      );
    }
    return out;
  }, overlaySelector);
}

/** Land on `route` from scratch and wait for the shell. */
async function land(page: import('@playwright/test').Page, route: string) {
  await page.goto(route, { waitUntil: 'domcontentloaded' });
  await page.locator('.v2-shell').waitFor({ state: 'visible', timeout: 15_000 });
  await settle(page, 800);
}

test.describe('Real brain: every safe control', () => {
  for (const dest of DESTINATIONS) {
    test(`${dest.to} (${dest.label})`, async ({ page }) => {
      test.setTimeout(300_000);
      const rec = record(page);
      await land(page, dest.to);

      // Per route, not module-level. A module-level accumulator printed
      // by a final "table" test loses everything the moment any test
      // fails, because Playwright starts a fresh worker after a failure
      // and the new worker re-imports this file with an empty array.
      // Measured: one failing test at the end of the walk printed an
      // empty table for all 28 routes that had just passed.
      const rows: Outcome[] = [];

      const threw: string[] = [];
      const lied: string[] = [];
      const dead: string[] = [];
      const unclickable: string[] = [];
      const seenSkips = new Set<string>();

      /**
       * Controls already clicked, keyed by signature plus how many
       * identical siblings preceded it.
       *
       * Addressing by INDEX into a snapshot taken once at the top does
       * not survive a page that changes as you walk it, and this walk
       * changes pages: clicking "New conversation" adds a row to the
       * work rail, which shifts every later index by one. Measured on
       * /oversight before this rewrite, 13 of 28 controls, including
       * Refresh and every Dock link, were reported "not-reached"
       * because the index they were found at now held something else.
       * A guard that quietly stops clicking half the page is worse than
       * no guard.
       *
       * So: re-enumerate before every click and take the first control
       * this route has not clicked yet. Controls that appear part-way
       * through the walk get clicked too, which is the correct
       * behaviour and something the snapshot version could not do.
       */
      const clicked = new Set<string>();
      const keyOf = (list: Control[], i: number) => {
        const sig = list[i].sig;
        let ordinal = 0;
        for (let j = 0; j < i; j += 1) if (list[j].sig === sig) ordinal += 1;
        return `${sig}#${ordinal}`;
      };

      // A cap, because a page that renders one row per memory episode can
      // put hundreds of identical controls on screen and clicking the
      // 400th teaches nothing the 3rd did not.
      const BUDGET = 45;
      let clickedCount = 0;
      let remaining = 0;

      for (let n = 0; n < BUDGET; n += 1) {
        if (new URL(page.url()).pathname !== dest.to) await land(page, dest.to);

        const { safe, skipped } = await enumerateControls(page);
        for (const c of skipped) {
          const k = `${c.sig}|${c.skipReason}`;
          if (seenSkips.has(k)) continue;
          seenSkips.add(k);
          rows.push({
            route: dest.to, label: c.label, tag: c.tag,
            result: 'skipped', detail: c.skipReason,
          });
        }

        let pick = -1;
        for (let i = 0; i < safe.length; i += 1) {
          if (!clicked.has(keyOf(safe, i))) { pick = i; break; }
        }
        if (pick < 0) break;
        remaining = safe.length - clicked.size - 1;
        const control = safe[pick];
        clicked.add(keyOf(safe, pick));
        clickedCount += 1;

        const before = {
          url: page.url(),
          mark: rec.mark(),
          print: await fingerprint(page),
          errors: await errorSurfaces(page),
        };

        const target = page.locator(CONTROL_SELECTOR).nth(control.index);
        let clickError = '';
        try {
          await target.click({ timeout: 4000, noWaitAfter: true });
        } catch (err) {
          // Keep the interception line, not just "Timeout exceeded".
          // "X from Y subtree intercepts pointer events" is the whole
          // finding: it names the thing sitting on top of the control.
          // Without it the report says a control could not be clicked
          // and cannot say what was in the way.
          const lines = (err as Error).message.split('\n').map((l) => l.trim());
          const blame = lines.find((l) => l.includes('intercepts pointer events'));
          clickError = [lines[0], blame].filter(Boolean).join(' <- ');
        }
        await page.waitForTimeout(700);

        const after = rec.since(before.mark);
        const urlChanged = page.url() !== before.url;
        const domChanged = (await fingerprint(page)) !== before.print;
        const dialogOpen = await page.locator('[role="dialog"]:visible, .v2-pop:visible').count() > 0;

        const bad = [
          ...failedRequests(after.exchanges).map((e) => `${e.status} ${e.method} ${e.pathname}`),
          ...after.failures.map((f) => `net-fail ${f.method} ${f.url}: ${f.error}`),
        ];
        const toldTheUser = (await errorSurfaces(page)) > before.errors;

        let result = 'ok';
        const detail: string[] = [];
        if (clickError) {
          // Painted but not usable: an overlay eating the pointer, a
          // control that never became stable. Reported rather than
          // failed, because a control that re-rendered under the cursor
          // is benign and only a human can tell the two apart.
          result = 'click-failed';
          detail.push(clickError);
          unclickable.push(`${control.label}: ${clickError}`);
        }
        if (after.pageErrors.length || after.consoleErrors.length) {
          result = 'threw';
          detail.push(...after.pageErrors, ...after.consoleErrors);
          threw.push(`${control.label}: ${[...after.pageErrors, ...after.consoleErrors][0]}`);
        }
        if (bad.length) {
          detail.push(...bad);
          if (!toldTheUser) {
            result = 'silent-failure';
            lied.push(`${control.label}: ${bad.join(', ')} with no error toast`);
          } else if (result === 'ok') {
            result = 'failed-and-said-so';
          }
        }
        if (result === 'ok' && !urlChanged && !domChanged && !dialogOpen
            && after.exchanges.length === 0) {
          if (control.active) {
            // The tab you are already on, the nav link to the page you
            // are already on, the toggle already in that state. Nothing
            // changing is the correct outcome, so this is not a finding
            // and must not be reported as one.
            result = 'already-active';
          } else {
            result = 'dead';
            dead.push(control.label);
          }
        }
        if (result === 'ok') {
          detail.push([
            urlChanged ? `nav -> ${new URL(page.url()).pathname}` : '',
            dialogOpen ? 'dialog opened' : '',
            domChanged ? 'dom changed' : '',
            after.exchanges.length ? `${after.exchanges.length} requests` : '',
          ].filter(Boolean).join(', '));
        }

        rows.push({
          route: dest.to, label: control.label, tag: control.tag,
          result, detail: detail.join(' ;; ').slice(0, 300),
        });

        if (dialogOpen) {
          // Escape closes the palette, the modals and the vital
          // popovers (VitalPopover binds keydown Escape as well as an
          // outside pointerdown). Anything still open after that gets a
          // fresh load rather than a stray click: an earlier version
          // clicked at (2,2) to dismiss "outside", and (2,2) is the
          // rail-collapse button, so the cleanup step was silently
          // toggling a real control between every measurement.
          await page.keyboard.press('Escape').catch(() => {});
          await page.waitForTimeout(250);
          const stillOpen = await page
            .locator('[role="dialog"]:visible, .v2-pop:visible').count();
          if (stillOpen > 0) await land(page, dest.to);
        }
      }

      if (clickedCount >= BUDGET && remaining > 0) {
        rows.push({
          route: dest.to, label: `(+${remaining} more)`, tag: '-',
          result: 'over-budget', detail: `capped at ${BUDGET} controls per route`,
        });
      }

      // eslint-disable-next-line no-console
      console.log(
        `[controls] ${dest.to}: ${clickedCount} clicked, ${seenSkips.size} skipped, `
        + `${threw.length} threw, ${lied.length} silent failures, ${dead.length} dead, `
        + `${unclickable.length} unclickable`
        + (dead.length ? ` [dead: ${dead.join(' / ')}]` : '')
        + (unclickable.length ? ` [unclickable: ${unclickable.join(' / ')}]` : ''),
      );
      // The deliverable: one line per control, printed while the route
      // that produced it is still the subject, so the outcome of the
      // walk is readable rather than inferred from a pass/fail count.
      // eslint-disable-next-line no-console
      console.log(
        `[controls-table] ${dest.to}\n`
        + rows.map((o) => `  ${o.result}\t${o.tag}\t${o.label}\t${o.detail}`).join('\n'),
      );

      expect(threw, `${dest.to}: controls that threw`).toEqual([]);
      expect(lied, `${dest.to}: controls that failed without telling the user`).toEqual([]);
    });
  }

  /**
   * The one place in this lane that stubs anything, and it stubs to
   * force an error path rather than to avoid the brain.
   *
   * Every route above reported zero failed requests, which is either
   * good news or a detector that cannot fire. Those two look identical
   * in a green log, so make one endpoint fail and check that the same
   * two signals the walk reads, `badStatuses` over the recorded
   * exchanges and the presence of an error toast, both report what
   * really happened. If this test goes red, every "0 silent failures"
   * above means nothing.
   *
   * Only one route is registered, so the "most recently registered
   * route wins" trap that shadows catch-alls elsewhere in this
   * directory does not apply. There is no catch-all to shadow.
   */
  test('the failed-request detector actually fires (forced error path)', async ({ page }) => {
    test.setTimeout(60_000);
    const rec = record(page);
    await page.route('**/api/supervisor/stats*', (r) => r.fulfill({
      status: 500,
      contentType: 'application/json',
      body: JSON.stringify({ error: 'forced by the e2e lane' }),
    }));
    // The page reads supervisor stats during hydration, so the 500
    // happens on load. An earlier version of this test clicked Refresh
    // to provoke it and could not: the error toast the 500 raises lands
    // on top of the Refresh button. See the recorded reproduction
    // below; the detector does not need the click.
    await land(page, '/oversight');

    const bad = badStatuses(rec.exchanges).map((e) => `${e.status} ${e.pathname}`);
    expect(bad, 'a 500 went unnoticed by the walk detector').toContain('500 /api/supervisor/stats');

    // And record which branch the page took, so the report can say
    // whether Oversight tells the user or swallows it.
    const toldTheUser = await page.locator('.v2-error-toast-card').count() > 0;
    const inlineError = await page.getByText(/not confirmed|unavailable/i).count() > 0;
    // eslint-disable-next-line no-console
    console.log(
      `[controls] forced 500 on /api/supervisor/stats: toast=${toldTheUser} inline=${inlineError}`,
    );
    expect(
      toldTheUser || inlineError,
      'a 500 on the supervisor stats read left no visible sign on the page',
    ).toBe(true);
  });

  /**
   * FIXED. This was a recorded reproduction (`test.fail()`), and the
   * annotation came off the moment the layout was fixed, exactly as the
   * note below predicted: a `test.fail()` whose defect is repaired is
   * reported as a failure, which is the mechanism working.
   *
   * The fix is BOTH halves, because either alone is insufficient:
   * the stack moved to `top: 112`, clear of the action row, AND it is
   * `pointer-events: none` so it cannot intercept a click wherever it
   * ends up. Making it transparent alone was tried first and was not
   * enough, because the dismiss button has to take clicks to be
   * dismissable and at `top: 72` that one re-enabled element landed
   * precisely on "Refresh". Moving it alone is not enough either, for
   * the reason recorded below: every fixed corner eventually collides
   * with something.
   *
   * Verified against a live brain with the toast actually on screen:
   * zero covered controls, "Pause actions" and "Refresh" both take a
   * real Playwright click, and the toast still dismisses.
   *
   * The defect. `components/ErrorToast.jsx:70-79` pinned the global error
   * stack to `position: fixed; top: 72; right: 20; zIndex: 65`. Page
   * action rows live in the same corner. Measured on /oversight against
   * a live brain at 1280x720, with one forced 500 on
   * /api/supervisor/stats:
   *
   *   toast stack   1000,72  260x78
   *   Pause actions 1045,71  129x32   elementFromPoint hits the toast
   *   Refresh       1182,71   43x32   elementFromPoint hits the toast
   *
   * "Pause actions" is the supervisor kill switch (POST
   * /api/supervisor/pause, pages/Oversight.jsx:186-196). So for the six
   * seconds an error toast is on screen, the two controls an operator
   * reaches for when something is going wrong, halt everything and try
   * again, are both unreachable, and the thing covering them is the
   * message telling them something went wrong.
   *
   * Not fixed here because the fix is a placement decision, not a bug
   * with one right answer: the obvious alternative corner is already
   * taken by `components/ProactiveToast.jsx:70-73`
   * (`right: 20, bottom: 130`), so moving the error stack there trades
   * this collision for another one.
   */
  test('the error toast does not cover the page action row', async ({ page }) => {
    test.setTimeout(60_000);
    await page.route('**/api/supervisor/stats*', (r) => r.fulfill({
      status: 500, contentType: 'application/json', body: '{"error":"forced"}',
    }));
    await page.goto('/oversight');
    await page.locator('.v2-error-toast-card').first().waitFor({ timeout: 10_000 });

    const blocked = await coveredControls(page, '.v2-error-toast-stack');
    expect(blocked, 'the error toast is sitting on top of real controls').toEqual([]);
  });

  /**
   * RECORDED REPRODUCTION, second instance of the same pattern.
   *
   * `styles/pages.css:1463-1477` pins `.v2-chat-pane` to
   * `position: fixed; top: calc(menubar + 24px); right: 24px; width:
   * 320px; z-index: 60`. The control that opens it, the "Save" toggle
   * at `pages/Chat.jsx:1053-1063`, lives in the chat pane header, which
   * is also top-right. Measured against a live brain at 1280x720 with
   * `scrollY: 0`:
   *
   *   Save toggle       1148,71  77x32
   *   .v2-chat-pane      936,24  320x289
   *   elementFromPoint at the toggle's centre -> .v2-chat-pane-hint
   *
   * So opening the snapshots pane covers its own toggle, and clicking
   * Save again to close it does nothing you can reach. The control walk
   * hit this independently: Playwright's own actionability check, which
   * scrolls into view first and so cannot be a scroll artifact, refused
   * the click with "<p class=v2-chat-pane-hint> from <div
   * class=v2-chat-pane> subtree intercepts pointer events".
   *
   * Same root shape as the error toast above: a fixed overlay anchored
   * to the top-right corner, over a page action row that is also
   * top-right. Two independent surfaces have now landed on it, which is
   * what makes it a pattern rather than a one-off.
   *
   * FIXED, and the cause was a stale variable rather than a bad number.
   * `top: calc(var(--v2-menubar-height) + 24px)` was written when the
   * menubar existed; retiring it set that variable to 0 and quietly
   * moved the pane up into the action row. It is anchored to
   * `--v2-chrome-top` now (the system bar plus any runtime notice
   * rows, so it also follows the strip that appears when the brain is
   * running a build that was replaced on disk), and opens below the
   * row. Verified: pane top 112, Save bottom 103, and the toggle
   * closes it again.
   */
  test('the chat snapshots pane does not cover its own toggle', async ({ page }) => {
    test.setTimeout(60_000);
    await page.goto('/chat');
    await page.locator('.v2-shell').waitFor({ timeout: 15_000 });
    await settle(page, 800);
    await page.getByTestId('chat-save-toggle').click();
    await page.locator('.v2-chat-pane').waitFor({ timeout: 5000 });
    await page.waitForTimeout(400);

    const blocked = await coveredControls(page, '.v2-chat-pane');
    expect(blocked, 'the snapshots pane is sitting on top of real controls').toEqual([]);
  });
});
