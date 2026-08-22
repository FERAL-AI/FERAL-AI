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
