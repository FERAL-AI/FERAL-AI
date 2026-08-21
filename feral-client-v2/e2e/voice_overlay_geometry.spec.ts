/**
 * Voice overlay must never sit on top of the chat composer (Playwright).
 *
 * The docked voice pill is `position: fixed` anchored to the bottom
 * right, so anything that makes it taller pushes its top edge upward,
 * into the composer that holds Send and the mic toggle.
 *
 * Measured in Chrome at 1680x878 before the fix:
 *
 *   no status banner   overlay top 790.8, composer bottom 786  -> +4.8px
 *   one banner line    overlay top 785.1                       -> -0.9px
 *   six banner lines   overlay top 774.9, Send bottom 775      -> +0.1px
 *
 * The layout leaves only ~20px of clear space between the composer and
 * the dock while the pill is 74.5px tall, so the clearance cannot be
 * won back by repositioning: the pill has to be stopped from growing.
 * `bottom` plus `max-height` is now the entire budget, and the assertion
 * below is that the budget holds even when the overlay is stuffed with
 * far more status text than the component can produce.
 *
 * jsdom has no layout, so this cannot be a vitest test. It is the only
 * kind of test that would have caught the original overlap.
 */
import { test, expect } from '@playwright/test';

/** Space we insist on between the pill and the composer, in px. */
const MIN_CLEARANCE = 4;

test.describe('docked voice overlay', () => {
  test('never overlaps the composer, however long the status text is', async ({ page }) => {
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

    await page.setViewportSize({ width: 1680, height: 878 });
    await page.goto('/chat');

    await expect(page.locator('.v2-chat-composer')).toBeVisible();

    const measure = async (bannerCount: number) =>
      page.evaluate((n) => {
        const ov = document.querySelector('.v2-voice-overlay');
        const composer = document.querySelector('.v2-chat-composer');
        if (!ov || !composer) return null;

        // Show it the way a live voice session does.
        ov.classList.add('is-visible');

        const meta = ov.querySelector('.v2-voice-meta');
        meta?.querySelectorAll('.v2-voice-status').forEach((e) => e.remove());
        for (let i = 0; i < n; i += 1) {
          const d = document.createElement('div');
          d.className = 'v2-voice-status';
          d.textContent =
            'OpenAI Realtime unavailable: you exceeded your current quota. '
            + 'Falling back to Whisper. '.repeat(4);
          meta?.appendChild(d);
        }

        const o = ov.getBoundingClientRect();
        const c = composer.getBoundingClientRect();
        const send = document.querySelector('.v2-chat-send');
        const mic = document.querySelector('.v2-chat-mic');

        const covered = (el: Element | null) => {
          if (!el) return false;
          const b = el.getBoundingClientRect();
          const hit = document.elementFromPoint(
            b.left + b.width / 2, b.top + b.height / 2,
          );
          return !(hit === el || el.contains(hit as Node));
        };

        return {
          overlayHeight: o.height,
          clearance: o.top - c.bottom,
          sendCovered: covered(send),
          micCovered: covered(mic),
        };
      }, bannerCount);

    const bare = await measure(0);
    expect(bare, 'overlay or composer not found').not.toBeNull();
    expect(bare!.clearance).toBeGreaterThan(MIN_CLEARANCE);
    expect(bare!.sendCovered).toBe(false);
    expect(bare!.micCovered).toBe(false);

    // Eight banners is well beyond anything the component renders. If
    // the height is unbounded this is where the pill swallows Send.
    const stuffed = await measure(8);
    expect(
      stuffed!.clearance,
      `overlay grew into the composer (height ${stuffed!.overlayHeight}px)`,
    ).toBeGreaterThan(MIN_CLEARANCE);
    expect(stuffed!.sendCovered, 'Send button is under the voice overlay').toBe(false);
    expect(stuffed!.micCovered, 'Mic button is under the voice overlay').toBe(false);
  });

  test('does not cover the dock it sits over', async ({ page }) => {
    await page.route('**/api/dashboard*', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ health: { status: 'ok', skills: { count: 0 } } }),
      });
    });
    await page.setViewportSize({ width: 1680, height: 878 });
    await page.goto('/chat');
    await expect(page.locator('.v2-dock')).toBeVisible();

    const blocked = await page.evaluate(() => {
      const ov = document.querySelector('.v2-voice-overlay');
      ov?.classList.add('is-visible');
      return [...document.querySelectorAll('.v2-dock a, .v2-dock button')]
        .filter((el) => {
          const b = el.getBoundingClientRect();
          const hit = document.elementFromPoint(
            b.left + b.width / 2, b.top + b.height / 2,
          );
          return !(hit === el || el.contains(hit as Node));
        })
        .map((el) => (el.textContent || '').trim() || el.getAttribute('aria-label'));
    });

    expect(blocked, `dock items under the voice overlay: ${blocked.join(', ')}`).toEqual([]);
  });
});
