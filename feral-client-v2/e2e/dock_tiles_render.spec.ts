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

test('the hover label stays a floating tooltip, not a flex sibling', async ({ page }) => {
  await page.goto('/console');
  const info = await page.evaluate(() => {
    const label = document.querySelector('.v2-dock-label') as HTMLElement;
    const tile = label.closest('.v2-dock-btn') as HTMLElement;
    const l = label.getBoundingClientRect();
    const t = tile.getBoundingClientRect();
    return {
      position: getComputedStyle(label).position,
      // A tooltip sits ABOVE its tile; a flex sibling sits inside it.
      aboveTile: l.bottom <= t.top + 1,
    };
  });
  expect(info.position).toBe('absolute');
  expect(info.aboveTile).toBe(true);
});

test('hovering a tile names it', async ({ page }) => {
  // The tooltip is the only thing that tells you what an icon-only dock
  // tile is. It lives OUTSIDE the tile box (bottom: calc(100% + 8px)),
  // so an overflow: hidden on the tile clips it and hovering tells you
  // nothing. That shipped.
  await page.goto('/console');
  const tile = page.locator('.v2-dock-btn').nth(2);
  const label = tile.locator('.v2-dock-label');

  await expect(label).toHaveCSS('opacity', '0');
  await tile.hover();
  await expect(label).toHaveCSS('opacity', '1');
  await expect(label).toBeVisible();
  await expect(label).not.toHaveText('');

  // Painted, not merely opaque behind a clip. An elementFromPoint hit
  // test is the wrong instrument here: a tooltip sets
  // pointer-events: none, so the point resolves to whatever is behind
  // it and the check fails on a perfectly visible label. What actually
  // matters is that it has real size, sits on screen, and that no
  // ancestor clips it away.
  const seen = await page.evaluate(() => {
    const l = document.querySelectorAll('.v2-dock-btn')[2].querySelector('.v2-dock-label') as HTMLElement;
    const b = l.getBoundingClientRect();
    let clippedBy = '';
    for (let n = l.parentElement; n; n = n.parentElement) {
      const o = getComputedStyle(n).overflow;
      if (o === 'hidden' || o === 'clip') {
        const nb = n.getBoundingClientRect();
        if (b.top < nb.top || b.bottom > nb.bottom || b.left < nb.left || b.right > nb.right) {
          clippedBy = n.className || n.tagName;
          break;
        }
      }
    }
    return {
      w: b.width, h: b.height,
      onScreen: b.top >= 0 && b.left >= 0 && b.bottom <= innerHeight && b.right <= innerWidth,
      clippedBy,
    };
  });
  expect(seen.w).toBeGreaterThan(20);
  expect(seen.h).toBeGreaterThan(8);
  expect(seen.onScreen).toBe(true);
  expect(seen.clippedBy, `the hover label is clipped by .${seen.clippedBy}`).toBe('');
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
