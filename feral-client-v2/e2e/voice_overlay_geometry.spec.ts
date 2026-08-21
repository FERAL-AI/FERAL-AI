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

        // Show it the way a live voice session does. Shell.jsx puts
        // is-voice-mode on the shell for exactly as long as the session
        // runs, and the strip the pill sits in is reserved by that
        // class, so a test that sets only is-visible measures a state
        // the app never enters.
        ov.classList.add('is-visible');
        document.querySelector('.v2-shell')?.classList.add('is-voice-mode');

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

  /* This ran at one viewport, 1680x878, which is the single width where
     the docked pill happens to clear the dock. The pill is right: 20px
     with max-width calc(100vw - 40px), so below roughly 600px it spans
     the whole strip and lands squarely on the centred dock. Measured on
     the code this loop was added to catch:

        768px   6 of 9 tiles unreachable, 67.7% of the dock covered
        430px   9 of 9,                   100%
        375px   9 of 9,                    96.5%

     A geometry guard that tests one viewport is testing one number. */
  for (const width of [1920, 1680, 1280, 1024, 768, 640, 430, 375]) {
    test(`does not cover the dock it sits over at ${width}px`, async ({ page }) => {
      await page.route('**/api/dashboard*', async (route) => {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ health: { status: 'ok', skills: { count: 0 } } }),
        });
      });
      await page.setViewportSize({ width, height: 878 });
      await page.goto('/chat');
      await expect(page.locator('.v2-dock')).toBeVisible();

      const blocked = await page.evaluate(() => {
        const ov = document.querySelector('.v2-voice-overlay');
        ov?.classList.add('is-visible');
        document.querySelector('.v2-shell')?.classList.add('is-voice-mode');
        // The pill fades and slides in. Measuring mid-transition reads
        // the old position, which is how a covered dock can look clear.
        return new Promise((resolve) => {
          setTimeout(() => {
            resolve([...document.querySelectorAll('.v2-dock a, .v2-dock button')]
              .filter((el) => {
                const b = el.getBoundingClientRect();
                const hit = document.elementFromPoint(
                  b.left + b.width / 2, b.top + b.height / 2,
                );
                return !(hit === el || el.contains(hit as Node));
              })
              .map((el) => (el.textContent || '').trim() || el.getAttribute('aria-label')));
          }, 600);
        });
      });

      expect(blocked, `dock items under the voice overlay at ${width}px: ${(blocked as string[]).join(', ')}`).toEqual([]);
    });
  }

  /* The composer invariant had the same shape of hole as the dock one:
     asserted once, at 1680. Both are now swept, because the pill moved
     and "it cleared the composer at the width I checked" is exactly the
     claim that was wrong the first time. */
  for (const width of [1920, 1280, 768, 430, 375]) {
    test(`clears the chat composer at ${width}px`, async ({ page }) => {
      await page.route('**/api/**', r => r.fulfill({
        status: 200, contentType: 'application/json', body: '{}',
      }));
      await page.setViewportSize({ width, height: 878 });
      await page.goto('/chat');
      await expect(page.locator('.v2-chat-composer')).toBeVisible();

      const m = await page.evaluate(() => new Promise((resolve) => {
        document.querySelector('.v2-voice-overlay')?.classList.add('is-visible');
        document.querySelector('.v2-shell')?.classList.add('is-voice-mode');
        setTimeout(() => {
          const ov = document.querySelector('.v2-voice-overlay')!.getBoundingClientRect();
          const c = document.querySelector('.v2-chat-composer')!.getBoundingClientRect();
          const covered = (sel: string) => {
            const el = document.querySelector(sel);
            if (!el) return false;
            const b = el.getBoundingClientRect();
            const hit = document.elementFromPoint(b.left + b.width / 2, b.top + b.height / 2);
            return !(hit === el || el.contains(hit as Node));
          };
          resolve({
            clearance: ov.top - c.bottom,
            send: covered('.v2-chat-send'),
            mic: covered('.v2-chat-mic'),
          });
        }, 700);
      })) as { clearance: number; send: boolean; mic: boolean };

      expect(m.clearance, `pill overlaps the composer by ${(-m.clearance).toFixed(1)}px`)
        .toBeGreaterThan(MIN_CLEARANCE);
      expect(m.send, 'Send is under the voice pill').toBe(false);
      expect(m.mic, 'the composer mic is under the voice pill').toBe(false);
    });
  }
});
