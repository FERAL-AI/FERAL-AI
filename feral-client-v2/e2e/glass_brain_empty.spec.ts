/**
 * Glass Brain — empty-state visual contract (Playwright).
 *
 * User-reported bug:
 *   On /glass-brain when the consciousness graph is empty, a coloured
 *   ball overlapped the centred empty-state prompt.
 *
 * Fix (Option A): hide the legend dots in the Pane `actions`
 * slot when there are no in-flight entities. See
 *   feral-client-v2/src/pages/GlassBrain.jsx (kindRows / actions)
 *   feral-client-v2/src/__tests__/pages/GlassBrain.empty-state.test.jsx
 *
 * This spec covers the runtime contract:
 *   1. Mock the consciousness store to return zero entities.
 *   2. Navigate to /glass-brain.
 *   3. Locate the `.v2-mindmap-empty` text.
 *   4. Enumerate every `border-radius: 50%` element in the page,
 *      filter to non-zero size, assert no rect intersection with the
 *      empty-state bounding box.
 *
 * Dependency note (PR body): this file ships in advance of a
 * `playwright.config.ts` for feral-client-v2. The e2e harness and
 * pair-device config may land in parallel. Until one of those configs
 * lands, this spec runs locally only with `npx playwright test e2e/glass_brain_empty.spec.ts
 * --config=…` against an explicit config; in CI it is collected by
 * `feral-client-v2/playwright.config.ts` once that file lands.
 */
import { test, expect } from '@playwright/test';

test.describe('Glass Brain — empty state', () => {
  test('no border-radius:50% element overlaps the empty-state text', async ({ page }) => {
    // Stub every API the page reaches for so we land in the deterministic
    // empty-graph state regardless of what brain instance Playwright is
    // pointed at. The `/api/consciousness/state` route is the one that
    // drives the mind-map's branch.
    await page.route('**/api/consciousness/state*', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ entities: [] }),
      });
    });
    await page.route('**/api/dashboard*', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          health: { status: 'ok', skills: { count: 0 } },
          session_count: 0,
          device_count: 0,
        }),
      });
    });

    await page.goto('/glass-brain');

    const empty = page.locator('.v2-mindmap-empty');
    await expect(empty).toBeVisible();
    await expect(empty).toContainText(/No in-flight consciousness entities/i);

    // Pull the empty-state bounding box and every dot's bounding box
    // straight from the live DOM via a single evaluate, then assert
    // intersection in Node-land for clearer failure diagnostics.
    const result = await page.evaluate(() => {
      function rectFromDomRect(r: DOMRect) {
        return {
          left: r.left,
          top: r.top,
          right: r.right,
          bottom: r.bottom,
          width: r.width,
          height: r.height,
        };
      }

      const emptyEl = document.querySelector('.v2-mindmap-empty');
      if (!emptyEl) return { emptyRect: null, dots: [] };
      const emptyRect = rectFromDomRect(emptyEl.getBoundingClientRect());

      const dots: Array<{ selector: string; rect: ReturnType<typeof rectFromDomRect>; classes: string }> = [];
      const everything = document.querySelectorAll('*');
      everything.forEach((el) => {
        const cs = window.getComputedStyle(el as Element);
        const br = cs.borderRadius;
        // `border-radius: 50%` resolves to a px value like "4px" on an
        // 8x8 element after browsers compute the percent. We treat
        // anything that is rendered as a circle (round on every corner
        // AND the resolved radius >= half of the smaller side) as a dot.
        const r = el.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) return;
        const tlr = parseFloat(cs.borderTopLeftRadius);
        const trr = parseFloat(cs.borderTopRightRadius);
        const blr = parseFloat(cs.borderBottomLeftRadius);
        const brr = parseFloat(cs.borderBottomRightRadius);
        const minSide = Math.min(r.width, r.height);
        const isCircle = (
          tlr > 0 && trr > 0 && blr > 0 && brr > 0
          && tlr >= minSide / 2 - 0.5
          && trr >= minSide / 2 - 0.5
          && blr >= minSide / 2 - 0.5
          && brr >= minSide / 2 - 0.5
        );
        // Also include raw SVG circles (the mind-map's centre nebula
        // and FERAL anchor dot) — these never contribute computed
        // border-radius styles, so handle them separately.
        const tag = el.tagName.toLowerCase();
        if (!isCircle && tag !== 'circle') return;
        const classes = (el as HTMLElement).className?.toString?.() || '';
        dots.push({
          selector: `${tag}${classes ? '.' + classes.split(/\s+/).filter(Boolean).join('.') : ''}`,
          rect: rectFromDomRect(r),
          classes,
        });
      });

      return { emptyRect, dots };
    });

    expect(result.emptyRect).not.toBeNull();
    const empties = result.emptyRect!;

    function intersects(a: typeof empties, b: typeof empties) {
      return !(a.right <= b.left || a.left >= b.right || a.bottom <= b.top || a.top >= b.bottom);
    }

    const offenders = result.dots.filter((d) => intersects(d.rect, empties));
    expect(offenders, `Dots overlapping empty-state: ${JSON.stringify(offenders, null, 2)}`).toEqual([]);
  });
});
