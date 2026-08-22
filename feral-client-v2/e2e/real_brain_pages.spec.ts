/**
 * Every destination, hard-loaded against a LIVE brain.
 *
 * Opt-in: needs `FERAL_E2E_REAL_BRAIN=1` and `FERAL_E2E_URL` pointing
 * at a running instance that is serving the v2 bundle. See
 * `real_brain_util.ts` for why the stubbed lane cannot see any of this.
 *
 * Per destination this asserts, in one hard navigation (not a
 * client-side route change, because a bookmark and a refresh are how
 * these break):
 *
 *   1. the shell mounted and the page rendered real content, not the
 *      SPA fallback and not the error boundary,
 *   2. nothing the page requested 404'd or 500'd,
 *   3. nothing the page requested other than the page itself was
 *      answered with HTML. The brain's SPA catch-all turns every
 *      missing path into a 200, so this is the assertion that catches a
 *      JSON fetch, a script or an icon being handed index.html,
 *   4. every REST path the page called is registered on the brain,
 *      checked against the brain's own openapi.json with the catch-all
 *      mount removed,
 *   5. zero uncaught console errors, zero unhandled rejections and zero
 *      WebSocket errors.
 */
import { test, expect } from '@playwright/test';
import {
  REAL_BRAIN, SKIP_REASON, readDestinations, record, settle,
  dataExchanges, badStatuses, htmlAnsweredNonDocument, brainRoutes,
} from './real_brain_util';

const DESTINATIONS = readDestinations();
const BASE = process.env.FERAL_E2E_URL || '';

test.skip(!REAL_BRAIN, SKIP_REASON);

/** Every REST path any page called, unioned across the walk. */
const calledApiPaths = new Set<string>();

test.describe('Real brain: every destination hard-loads', () => {
  for (const dest of DESTINATIONS) {
    test(`${dest.to} (${dest.label})`, async ({ page }) => {
      test.setTimeout(60_000);
      const rec = record(page);

      const response = await page.goto(dest.to, { waitUntil: 'domcontentloaded' });
      expect(response, `no response for ${dest.to}`).not.toBeNull();
      expect(
        response!.status(),
        `${dest.to} was not served: ${response!.status()}`,
      ).toBeLessThan(400);

      // The shell is the proof the bundle executed. A route whose asset
      // refs resolve wrong gets index.html back as JavaScript and
      // renders nothing into #root at status 200.
      await expect(
        page.locator('.v2-shell'),
        `${dest.to}: the shell never mounted`,
      ).toBeVisible({ timeout: 15_000 });
      await expect(page.locator('.v2-dock')).toBeVisible();

      // Not the error boundary.
      await expect(
        page.locator('.v2-error-shell'),
        `${dest.to} rendered the error boundary`,
      ).toHaveCount(0);

      await settle(page);

      // The router redirects anything unknown to '/', so a destination
      // that quietly bounced is a destination that does not exist.
      expect(
        new URL(page.url()).pathname,
        `${dest.to} did not stay put`,
      ).toBe(dest.to);

      // Real content, not an empty <main>.
      const body = await page.locator('main.v2-shell-main').evaluate((el) => ({
        children: el.childElementCount,
        text: (el as HTMLElement).innerText.replace(/\s+/g, ' ').trim().length,
      }));
      expect(body.children, `${dest.to}: <main> has no children`).toBeGreaterThan(0);
      expect(body.text, `${dest.to}: <main> rendered no text`).toBeGreaterThan(10);

      const api = dataExchanges(rec.exchanges);
      for (const e of api) calledApiPaths.add(e.pathname);
      // Printed, not merely asserted: a page that talks to nothing is
      // itself worth seeing in the log, and this lane's whole claim is
      // "the page really did call the brain".
      // eslint-disable-next-line no-console
      console.log(
        `[real-brain] ${dest.to} -> ${api.length} data calls: `
        + `${[...new Set(api.map((e) => `${e.status} ${e.method} ${e.pathname}`))].sort().join(' | ') || 'NONE'}`,
      );

      const bad = badStatuses(rec.exchanges).map(
        (e) => `${e.status} ${e.method} ${e.pathname}`,
      );
      expect(
        [...new Set(bad)],
        `${dest.to}: requests the brain 404'd or 500'd`,
      ).toEqual([]);

      const html = htmlAnsweredNonDocument(rec.exchanges).map(
        (e) => `${e.pathname} -> ${e.status} ${e.contentType}`,
      );
      expect(
        [...new Set(html)],
        `${dest.to}: non-document requests answered with HTML (a 200 the caller cannot use)`,
      ).toEqual([]);

      expect(
        [...new Set(rec.pageErrors)],
        `${dest.to}: uncaught errors and unhandled rejections`,
      ).toEqual([]);
      expect(
        [...new Set(rec.consoleErrors)],
        `${dest.to}: console errors`,
      ).toEqual([]);

      // The shell's socket is how the composer, the live job feed and
      // the proactive toasts reach the brain. `page.route` cannot
      // intercept a WebSocket, so the stubbed lane has never once
      // observed one, working or broken.
      expect(
        [...new Set(rec.socketErrors)],
        `${dest.to}: WebSocket errors`,
      ).toEqual([]);
      // eslint-disable-next-line no-console
      console.log(`[real-brain] ${dest.to} sockets: ${[...new Set(rec.sockets)].join(' | ') || 'NONE'}`);
    });
  }

  /**
   * The one place in this lane that stubs anything, and it stubs to
   * force an error path rather than to avoid the brain.
   *
   * The HTML-for-JSON detector is the reason this file exists (the
   * Skills page shipped broken because a JSON fetch received HTML at
   * status 200), and a detector that has never been seen to fire is
   * indistinguishable from one that cannot. So: make one endpoint
   * answer HTML and check the detector names it. If this test ever
   * fails, every green result above means nothing.
   *
   * Only one route is registered, so the "most recently registered
   * route wins" trap that shadows catch-alls elsewhere in this
   * directory does not apply here. There is no catch-all to shadow.
   */
  test('the HTML-for-JSON detector actually fires (forced error path)', async ({ page }) => {
    const rec = record(page);
    await page.route('**/api/dashboard*', (r) => r.fulfill({
      status: 200,
      contentType: 'text/html; charset=utf-8',
      body: '<!DOCTYPE html><html><body>not json</body></html>',
    }));
    await page.goto('/console', { waitUntil: 'domcontentloaded' });
    await settle(page, 1000);
    const flagged = htmlAnsweredNonDocument(rec.exchanges).map((e) => e.pathname);
    expect(flagged, 'a JSON endpoint answering HTML went unnoticed').toContain('/api/dashboard');
  });

  test('every REST path the walk called is registered on the brain', async ({ request }) => {
    // Runs last in file order, so `calledApiPaths` holds the union of
    // everything the destinations above requested. It asserts the
    // opposite direction from the per-page checks: a path can answer 200
    // through the SPA catch-all and still not be a route anyone
    // declared, which is why `brainRoutes` drops `/{full_path}` from the
    // matcher set before this runs.
    expect(calledApiPaths.size, 'no REST traffic was recorded at all').toBeGreaterThan(0);
    const routes = await brainRoutes(request, BASE);
    const orphans = [...calledApiPaths].filter((p) => !routes.match(p)).sort();
    // eslint-disable-next-line no-console
    console.log(`[real-brain] ${calledApiPaths.size} distinct REST paths called across the walk`);
    expect(
      orphans,
      `client called paths the brain does not register: ${orphans.join(', ')}`,
    ).toEqual([]);
  });
});
