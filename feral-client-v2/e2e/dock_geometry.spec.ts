/**
 * The dock's shape and its response to the pointer.
 *
 * Reported from a screenshot: "the dock has an issue it's too round
 * which is making some of the icons eaten and it looks bad", and "the
 * design you suggested vs what you have is totally different, the
 * spacing and also the size and the way when I hover on it it makes a
 * smooth animation".
 *
 * Measured against the mockup at scratchpad/design/instrument-panel.html
 * (`.dock` and `.dk`), which is the source these numbers come from:
 *
 *                    shipped      mockup
 *   container radius  pill (999)   18px
 *   gap                2px          7px
 *   tile              40x40        44x44, radius 12
 *   icon              20px         22px
 *   hover             flat -2px    scale 1 + 0.5k^2, lift 8k^2
 *
 * A full pill curves so hard at the ends that the outer tiles sit
 * inside the curve, which is the "eaten" the report describes.
 *
 * jsdom has no layout and no getBoundingClientRect worth reading, so
 * none of this can be a vitest test.
 */
import { test, expect } from '@playwright/test';

async function stub(page) {
  await page.route('**/api/**', (r) => r.fulfill({
    status: 200, contentType: 'application/json', body: '{}',
  }));
}

test('the dock is a rounded bar, not a pill that eats its end tiles', async ({ page }) => {
  await stub(page);
  await page.goto('/console');
  const list = page.locator('.v2-dock-list');
  await expect(list).toBeVisible();

  const box = await list.boundingBox();
  const radius = await list.evaluate((el) => parseFloat(getComputedStyle(el).borderTopLeftRadius));

  // A pill's radius is half its height or more. That is the shape that
  // pushes the first and last tiles into the curve.
  expect(radius, `dock radius ${radius} is a pill against height ${box!.height}`)
    .toBeLessThan(box!.height / 2);
  expect(radius).toBeGreaterThan(10);

  // Every tile must sit fully inside the container it is drawn in.
  const outside = await page.evaluate(() => {
    const l = document.querySelector('.v2-dock-list')!.getBoundingClientRect();
    return [...document.querySelectorAll('.v2-dock-btn')]
      .map((t) => {
        const r = t.getBoundingClientRect();
        return (r.left < l.left || r.right > l.right) ? (t.textContent || '').trim() : '';
      })
      .filter(Boolean);
  });
  expect(outside, `tiles hanging outside the dock: ${outside.join(', ')}`).toEqual([]);
});

test('tiles and icons are the size the design specifies', async ({ page }) => {
  await stub(page);
  await page.goto('/console');
  await expect(page.locator('.v2-dock-btn').first()).toBeVisible();

  const m = await page.evaluate(() => {
    const t = document.querySelector('.v2-dock-btn')!;
    const r = t.getBoundingClientRect();
    const svg = t.querySelector('svg')!.getBoundingClientRect();
    const list = getComputedStyle(document.querySelector('.v2-dock-list')!);
    return {
      tile: Math.round(r.width),
      icon: Math.round(svg.width),
      gap: parseFloat(list.gap),
      radius: parseFloat(getComputedStyle(t).borderTopLeftRadius),
    };
  });

  expect(m.tile).toBe(44);
  expect(m.icon).toBe(22);
  expect(m.gap).toBeGreaterThanOrEqual(6);
  expect(m.radius).toBeGreaterThanOrEqual(10);
});

test('hovering magnifies the row, not just the tile under the cursor', async ({ page }) => {
  await stub(page);
  await page.goto('/console');
  const tiles = page.locator('.v2-dock-btn');
  const n = await tiles.count();
  expect(n).toBeGreaterThan(4);

  const mid = Math.floor(n / 2);
  const box = await tiles.nth(mid).boundingBox();
  await page.mouse.move(box!.x + box!.width / 2, box!.y + box!.height / 2);
  await page.waitForTimeout(250);

  const scales = await page.evaluate(() => [...document.querySelectorAll('.v2-dock-btn')]
    .map((t) => {
      const m = /scale\(([\d.]+)\)/.exec((t as HTMLElement).style.transform || '');
      return m ? parseFloat(m[1]) : 1;
    }));

  // The tile under the cursor is the largest.
  expect(scales[mid]).toBeGreaterThan(1.3);
  // Its neighbours are lifted too, and less. This is what makes it a
  // lens rather than a single hover state.
  expect(scales[mid - 1]).toBeGreaterThan(1);
  expect(scales[mid - 1]).toBeLessThan(scales[mid]);
  // And the far end of the row is untouched.
  expect(scales[0]).toBeCloseTo(1, 2);

  // Leaving puts everything back.
  await page.mouse.move(10, 10);
  await page.waitForTimeout(250);
  const after = await page.evaluate(() => [...document.querySelectorAll('.v2-dock-btn')]
    .every((t) => !(t as HTMLElement).style.transform));
  expect(after, 'tiles stayed magnified after the pointer left').toBe(true);
});

test('the magnify is off when the operator asked for reduced motion', async ({ page }) => {
  await stub(page);
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.goto('/console');
  const tiles = page.locator('.v2-dock-btn');
  const box = await tiles.nth(3).boundingBox();
  await page.mouse.move(box!.x + box!.width / 2, box!.y + box!.height / 2);
  await page.waitForTimeout(250);

  const anyScaled = await page.evaluate(() => [...document.querySelectorAll('.v2-dock-btn')]
    .some((t) => ((t as HTMLElement).style.transform || '').includes('scale')));
  expect(anyScaled, 'the dock magnified despite prefers-reduced-motion').toBe(false);
});

test('Home is back on the dock and it goes Home', async ({ page }) => {
  // It was dropped when the dock was cut to the design's eight, which
  // left the whole v2 overview reachable only by remembering the
  // palette shortcut.
  await stub(page);
  await page.goto('/console');

  const home = page.locator('.v2-dock-btn[href="/"]');
  await expect(home, 'no Home tile on the dock').toBeVisible();
  await home.click();
  await expect(page).toHaveURL(/\/$/);

  // And it lights up as the active tile once you are there.
  await expect(page.locator('.v2-dock-btn[href="/"]')).toHaveClass(/is-active/);
});

/**
 * Ten destinations do not fit across a phone.
 *
 * Adding Home took the dock to ten tiles, and the mockup's 44px tile
 * with a 7px gap needs about 523px of row. Measured at 375px, where the
 * container caps at 94vw: two tiles were pushed outside it and became
 * unclickable, and one of them was Settings.
 *
 * Shrinking the tiles is not the fix on its own, because that is
 * arithmetic that has to keep being right as tiles are added, and this
 * is the second time that arithmetic has been wrong. The dock scrolls
 * instead, so an eleventh tile or a narrower phone degrades to "swipe
 * the dock" rather than "two destinations silently vanish".
 */
for (const width of [320, 375, 430]) {
  test(`every dock destination is reachable at ${width}px`, async ({ page }) => {
    await stub(page);
    await page.setViewportSize({ width, height: 860 });
    await page.goto('/console');
    await expect(page.locator('.v2-dock-list')).toBeVisible();

    const tiles = page.locator('.v2-dock a, .v2-dock button');
    const n = await tiles.count();
    expect(n).toBeGreaterThan(8);

    // Reachable means clickable, scrolling to it if the row is a
    // scroller. It does NOT mean visible without scrolling.
    for (let i = 0; i < n; i += 1) {
      const tile = tiles.nth(i);
      await tile.scrollIntoViewIfNeeded();
      const label = (await tile.getAttribute('aria-label'))
        || (await tile.getAttribute('title')) || `tile ${i}`;
      await expect(tile, `${label} is not reachable at ${width}px`).toBeVisible();
      const hit = await tile.evaluate((el) => {
        const b = el.getBoundingClientRect();
        const top = document.elementFromPoint(b.left + b.width / 2, b.top + b.height / 2);
        return top === el || el.contains(top as Node);
      });
      expect(hit, `${label} is covered or off-screen at ${width}px`).toBe(true);
    }
  });
}

test('the narrow dock actually scrolls rather than clipping', async ({ page }) => {
  await stub(page);
  await page.setViewportSize({ width: 375, height: 860 });
  await page.goto('/console');

  const m = await page.locator('.v2-dock-list').evaluate((el) => ({
    client: el.clientWidth, scroll: el.scrollWidth,
  }));
  // If the content is wider than the box, the box must be scrollable,
  // otherwise the overflow is simply hidden and the tiles are gone.
  if (m.scroll > m.client) {
    const overflow = await page.locator('.v2-dock-list')
      .evaluate((el) => getComputedStyle(el).overflowX);
    expect(overflow, 'the dock overflows but does not scroll').toMatch(/auto|scroll/);
  }

  // And the far tile takes a real click once scrolled to.
  const last = page.locator('.v2-dock-btn').last();
  await last.scrollIntoViewIfNeeded();
  await last.click({ timeout: 3000 });
});
