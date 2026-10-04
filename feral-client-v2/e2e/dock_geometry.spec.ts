/** Consumer dock geometry: persistent labels, six pinned destinations and
 * More, gentle lift without resizing, and reachable controls on phones. */
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

test('labeled tiles and icons meet the consumer design dimensions', async ({ page }) => {
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

  expect(m.tile).toBe(74);
  expect(m.icon).toBe(22);
  expect(m.gap).toBe(3);
  expect(m.radius).toBeGreaterThanOrEqual(10);
});

test('hover gives a gentle lift without resizing labeled destinations', async ({ page }) => {
  await stub(page); await page.goto('/console');
  const tiles = page.locator('.v2-dock-btn');
  await expect(tiles).toHaveCount(7);
  const before = await tiles.evaluateAll((items) => items.map((item) => {
    const r = item.getBoundingClientRect(); return { width: r.width, height: r.height, top: r.top };
  }));
  await tiles.nth(3).hover();
  const after = await tiles.evaluateAll((items) => items.map((item) => {
    const r = item.getBoundingClientRect(); return { width: r.width, height: r.height, top: r.top };
  }));
  for (let i = 0; i < before.length; i += 1) {
    expect(after[i].width).toBeCloseTo(before[i].width, 1);
    expect(after[i].height).toBeCloseTo(before[i].height, 1);
    expect(before[i].top - after[i].top).toBeGreaterThanOrEqual(-0.1);
    expect(before[i].top - after[i].top).toBeLessThanOrEqual(2.1);
  }
  expect(before[3].top - after[3].top).toBeGreaterThan(1);
  await page.mouse.move(10, 10);
  expect(await tiles.evaluateAll((items) => items.every((item) => !(item as HTMLElement).style.transform))).toBe(true);
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

test('Home remains reachable through More and marks its navigation context', async ({ page }) => {
  await stub(page); await page.goto('/console');
  const more = page.locator('.v2-dock-btn').filter({ hasText: 'More' });
  await more.click();
  const palette = page.getByRole('dialog', { name: 'Command palette' });
  await expect(palette).toBeVisible();
  await palette.getByRole('searchbox', { name: 'Search commands and pages' }).fill('Home');
  await palette.getByRole('option').filter({ hasText: 'Home' }).first().click();
  await expect(page).toHaveURL(/\/home$/);
  await expect(more).toHaveClass(/is-active/);
  await expect(more).toHaveAttribute('aria-pressed', 'false');
  await expect(palette).toHaveCount(0);
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
    expect(n).toBe(7);
    await expect(page.locator('.v2-dock-btn[href="/approvals"]')).toBeVisible();

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
