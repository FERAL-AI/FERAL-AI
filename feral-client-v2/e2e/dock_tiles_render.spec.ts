/**
 * Every dock tile must actually show its icon.
 *
 * This exists because the dock shipped with all eight tiles rendering as
 * empty squares and I did not catch it. The unit tests were green: they
 * asserted the data-state attribute, the count badge and the CSS
 * keyframes, none of which say anything about whether a user can see
 * the icon.
 *
 * The cause was a one-line CSS override. `.v2-dock-label` is
 * position: absolute on purpose, because it is the hover tooltip that
 * floats above the tile. A rule raising the z-index of the icon and the
 * label together set BOTH to position: relative, which put the label
 * into the flex flow, where it took width from the icon; the icon is a
 * flex item with the default flex-shrink: 1, so it collapsed to 0px
 * wide, and the tile's overflow: hidden clipped what was left.
 *
 * Measured on the broken build: svg width 0, positioned at x=635 while
 * its own tile started at x=647.
 *
 * jsdom cannot catch this. It has no layout, so a 0px-wide icon and a
 * 20px one are indistinguishable there.
 */
import { test, expect } from '@playwright/test';

test.beforeEach(async ({ page }) => {
  await page.route('**/api/**', (r) =>
    r.fulfill({ status: 200, contentType: 'application/json', body: '{}' }));
});

test('every dock tile paints a visible icon inside itself', async ({ page }) => {
  await page.goto('/console');
  await expect(page.locator('.v2-dock')).toBeVisible();

  const bad = await page.evaluate(() => {
    const out: string[] = [];
    for (const tile of document.querySelectorAll('.v2-dock-btn')) {
      const name = tile.getAttribute('title') || '(unnamed)';
      const svg = tile.querySelector('svg');
      if (!svg) { out.push(`${name}: no icon element`); continue; }
      const t = tile.getBoundingClientRect();
      const s = svg.getBoundingClientRect();
      if (s.width < 8 || s.height < 8) {
        out.push(`${name}: icon is ${s.width.toFixed(1)}x${s.height.toFixed(1)}`);
        continue;
      }
      // And it has to be inside the tile, or overflow:hidden eats it.
      if (s.left < t.left - 1 || s.right > t.right + 1) {
        out.push(`${name}: icon at x=${s.left.toFixed(0)} is outside its tile (${t.left.toFixed(0)}..${t.right.toFixed(0)})`);
      }
      if (getComputedStyle(svg).opacity === '0' || getComputedStyle(svg).visibility === 'hidden') {
        out.push(`${name}: icon is not visible`);
      }
    }
    return out;
  });

  expect(bad, `dock tiles with an unusable icon:\n${bad.join('\n')}`).toEqual([]);
});

test('persistent labels sit below visible icons inside their tiles', async ({ page }) => {
  await page.goto('/console');
  await expect(page.locator('.v2-dock-label')).toHaveCount(7);
  const bad = await page.locator('.v2-dock-btn').evaluateAll((tiles) => tiles.flatMap((tile) => {
    const label = tile.querySelector('.v2-dock-label') as HTMLElement;
    const icon = tile.querySelector('svg')!;
    const l = label.getBoundingClientRect(), i = icon.getBoundingClientRect(), t = tile.getBoundingClientRect();
    return getComputedStyle(label).opacity !== '1' || l.width < 8 || l.height < 8
      || l.top < i.bottom || l.bottom > t.bottom + 1 || l.left < t.left - 1 || l.right > t.right + 1
      || !label.textContent?.trim() ? [label.textContent || 'unnamed'] : [];
  }));
  expect(bad, 'labels must remain readable without hovering or shrinking the icon').toEqual([]);
});

test('keyboard focus reaches a named dock destination', async ({ page }) => {
  await page.goto('/console');
  const memory = page.locator('.v2-dock-btn[href="/memory"]');
  await expect(memory.locator('.v2-dock-label')).toHaveText('Memory');
  await memory.focus(); await expect(memory).toBeFocused();
  await memory.press('Enter'); await expect(page).toHaveURL(/\/memory$/);
});

test('the busy ring is not clipped away', async ({ page }) => {
  // The ring sits at inset: -3px, outside the tile, for the same reason.
  await page.goto('/console');
  const visible = await page.evaluate(() => {
    const tile = document.querySelectorAll('.v2-dock-btn')[0] as HTMLElement;
    tile.setAttribute('data-state', 'busy');
    const ring = tile.querySelector('.v2-dock-ring')!;
    const r = ring.getBoundingClientRect(), t = tile.getBoundingClientRect();
    const grew = r.width > t.width && r.height > t.height;
    const clipped = getComputedStyle(tile).overflow === 'hidden';
    tile.setAttribute('data-state', '');
    return { grew, clipped };
  });
  expect(visible.clipped).toBe(false);
  expect(visible.grew).toBe(true);
});

test('the live-state fill never covers the icon', async ({ page }) => {
  await page.goto('/console');
  const covered = await page.evaluate(() => {
    const tile = document.querySelector('.v2-dock-btn') as HTMLElement;
    tile.setAttribute('data-state', 'needs');   // the full-height fill
    const svg = tile.querySelector('svg')!;
    const b = svg.getBoundingClientRect();
    const hit = document.elementFromPoint(b.left + b.width / 2, b.top + b.height / 2);
    const bad = !(hit === svg || svg.contains(hit as Node) || (hit as HTMLElement)?.closest('.v2-dock-btn') === tile);
    tile.setAttribute('data-state', '');
    return bad;
  });
  expect(covered).toBe(false);
});
