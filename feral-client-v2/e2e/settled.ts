/**
 * Wait for CSS animations and transitions to finish before measuring.
 *
 * Why this file exists, with numbers.
 *
 * Two specs in this directory were intermittently failing in a full run
 * and passing in isolation, with `workers: 1, fullyParallel: false,
 * retries: 0`. Neither was a timeout and neither was cross-test state.
 * Both were the same mistake: reading a PAINTED value in a separate
 * round trip from the state change that produces it, while a CSS
 * animation or transition was still in flight. A round trip that takes
 * 2ms on an idle machine takes 50ms on a loaded one, which is what made
 * the outcome depend on what else the suite was doing.
 *
 * 1. `marketplace_install_consent.spec.ts` measured five permission rows
 *    with five separate `boundingBox()` calls. `.v2-modal` opens with
 *    `animation: v2ModalIn 180ms`, which is
 *    `scale(0.96) translateY(8px)` to `scale(1) translateY(0)`.
 *    Measured: row 0's bottom read mid-animation is 284.595, the same
 *    row settled is 274.203, and row 1 settled starts at 282.203. The
 *    spec asserts `rows[i].y >= rows[i-1].bottom - 1`, so mixing one
 *    mid-animation reading with one settled reading reports a 2px
 *    overlap in a layout that actually has an 8px gap. The observed CI
 *    failure was "Expected >= 283.5953063964844, Received
 *    282.58624267578125", and 283.595 is 284.595 minus the 1px
 *    tolerance, to three decimal places.
 *
 * 2. `system_bar_vitals.spec.ts` reads `backgroundColor` off
 *    `.v2-sysbar-mark` straight after `data-up` flips to "no".
 *    `.v2-sysbar-mark` carries `transition: background 120ms`, and
 *    measured from inside the page, the colour IN the frame the
 *    attribute flips is still `rgb(60, 123, 80)`, i.e. byte-identical
 *    to the green the spec compares it against. Whether the spec sees
 *    green or red is decided by how many frames elapse between
 *    Playwright noticing the attribute and the next round trip landing.
 *
 * The fix in both places is to wait for the animation the page is
 * actually running, not to retry until the machine cooperates. Retries
 * would have hidden exactly the class of defect this suite exists to
 * catch: a value that is briefly wrong on screen.
 */
import type { Locator, Page } from '@playwright/test';

/**
 * Resolve once every FINITE animation and transition on `locator` (and
 * its subtree) has finished.
 *
 * Infinite animations are skipped rather than awaited: the dock's
 * breathe, the run dot's blink and the "not responding" mark pulse all
 * run forever by design, and awaiting `finished` on one of those hangs
 * until the test times out.
 */
export async function animationsSettled(locator: Locator) {
  await locator.evaluate(async (el) => {
    const running = el.getAnimations({ subtree: true }).filter((a) => {
      const iterations = a.effect?.getTiming().iterations;
      return iterations !== Infinity;
    });
    await Promise.all(running.map((a) => a.finished.catch(() => undefined)));
    // One more frame, so the final computed values are the ones a
    // getBoundingClientRect or getComputedStyle will read back.
    await new Promise((resolve) => requestAnimationFrame(() => resolve(null)));
  });
}

/**
 * Bounding boxes for every element `selector` matches, read in ONE
 * frame.
 *
 * Playwright's `boundingBox()` is one round trip per element, so a loop
 * over N elements compares N different moments in time. Any assertion
 * that relates two elements to each other (this row sits below that
 * one, this control is inside that box) has to read them together or it
 * is not measuring a layout, it is measuring a race.
 */
export async function boxesInOneFrame(page: Page, selector: string) {
  return page.evaluate((sel) => [...document.querySelectorAll(sel)].map((el) => {
    const r = el.getBoundingClientRect();
    return {
      x: r.x, y: r.y, width: r.width, height: r.height,
      top: r.top, right: r.right, bottom: r.bottom, left: r.left,
    };
  }), selector);
}
