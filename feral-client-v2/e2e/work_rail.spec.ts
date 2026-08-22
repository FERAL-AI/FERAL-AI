/**
 * The rail, against the mockup's `.panel` / `.rgroup` / `.ritem`.
 *
 * Reported from a screenshot: "the sidebar Needs You in the one you
 * built vs the design you suggested totally different and no clear
 * spacing that separates nothing waiting from Needs you from the stuff
 * under it, then just happened it's showing me a list of events that
 * are so old", and "the sidebar in the suggested design had colors that
 * show needs you and running in a different nice color to separate and
 * to show some interactivity, then I can also reach the recent chat,
 * that's like quick access to things and it should be collapsable".
 *
 * What shipped was three headings with one sentence each, all the same
 * colour, no separation, no recent conversations, and no way to fold
 * anything. Every assertion here failed on it.
 */
import { test, expect } from '@playwright/test';

const NOW = Math.floor(Date.now() / 1000);

async function stub(page, opts: { busy?: boolean } = {}) {
  // Catch-all first: Playwright matches the most recently registered
  // route, so specific stubs must come after it.
  await page.route('**/api/**', (r) => r.fulfill({
    status: 200, contentType: 'application/json', body: '{}',
  }));
  await page.route('**/api/approvals*', (r) => r.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify(opts.busy ? {
      count: 2,
      approvals: [
        { request_id: 'r1', tool_name: 'coding_tools__write_file', args: { path: '~/build.sh' } },
        { request_id: 'r2', tool_name: 'shell__run', args: { command: 'rm -rf tmp' } },
      ],
    } : { count: 0, approvals: [] }),
  }));
  await page.route('**/api/jobs*', (r) => r.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify(opts.busy ? {
      items: [{
        id: 'j1', name: 'npm run build', kind: 'background_bash',
        status: 'running', started_at: NOW - 252,
      }],
    } : { items: [] }),
  }));
  await page.route('**/api/conversations*', (r) => r.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({
      total: 2,
      conversations: [
        { id: 'c1', title: 'SDK handoff', updated_at: NOW - 300, message_count: 12 },
        { id: 'c2', title: 'Screen and browser', updated_at: NOW - 4000, message_count: 4 },
      ],
    }),
  }));
  await page.route('**/api/timeline*', (r) => r.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({
      count: 2,
      entries: [
        // One recent, one five days old. The old one is the shape that
        // was being shown under "Just happened".
        { type: 'chat', timestamp: NOW - 600, title: 'Recent thing' },
        { type: 'chat', timestamp: NOW - 126 * 3600, title: 'Five days ago' },
      ],
    }),
  }));
}

test('sections are separated, not one run-on column', async ({ page }) => {
  await stub(page);
  await page.goto('/console');
  await expect(page.locator('.v2-rail')).toBeVisible();

  const heads = page.locator('.v2-rail-head');
  await expect(heads).toHaveCount(4);

  // Every section after the first carries a real rule and real space.
  const gaps = await page.evaluate(() => [...document.querySelectorAll('.v2-rail-sect')]
    .slice(1)
    .map((s) => {
      const cs = getComputedStyle(s);
      return {
        top: parseFloat(cs.marginTop) + parseFloat(cs.paddingTop),
        border: parseFloat(cs.borderTopWidth),
      };
    }));
  for (const g of gaps) {
    expect(g.top, 'sections run together with no space').toBeGreaterThanOrEqual(12);
    expect(g.border, 'no rule between sections').toBeGreaterThan(0);
  }
});

test('needs you and running carry their own colours when live', async ({ page }) => {
  await stub(page, { busy: true });
  await page.goto('/console');

  const needsHead = page.locator('.v2-rail-head[data-tone="needs"]');
  const runHead = page.locator('.v2-rail-head[data-tone="running"]');
  await expect(needsHead).toBeVisible();
  await expect(runHead).toBeVisible();

  const colours = await page.evaluate(() => {
    const c = (sel: string) => {
      const el = document.querySelector(sel);
      return el ? getComputedStyle(el).color : '';
    };
    return {
      needs: c('.v2-rail-head[data-tone="needs"]'),
      running: c('.v2-rail-head[data-tone="running"]'),
      plain: c('.v2-rail-head:not([data-tone="needs"]):not([data-tone="running"])'),
    };
  });
  // Three distinct states, so "something is waiting on you" is not the
  // same colour as "something is running" or as a quiet heading.
  expect(colours.needs).not.toBe(colours.plain);
  expect(colours.running).not.toBe(colours.plain);
  expect(colours.needs).not.toBe(colours.running);

  // And the cards take the accent stripe.
  const stripes = await page.evaluate(() => {
    const c = (sel: string) => {
      const el = document.querySelector(sel);
      return el ? getComputedStyle(el).borderLeftColor : '';
    };
    return { needs: c('.v2-rail-card--needs'), running: c('.v2-rail-card--running') };
  });
  expect(stripes.needs).not.toBe(stripes.running);
});

test('recent conversations are reachable from the rail', async ({ page }) => {
  await stub(page);
  await page.goto('/console');

  const recent = page.locator('.v2-rail-recent');
  await expect(recent.first()).toBeVisible();
  await expect(page.getByText('SDK handoff')).toBeVisible();

  // Clicking one goes to the conversation, not to a history page.
  await page.getByText('SDK handoff').click();
  await expect(page).toHaveURL(/\/chat$/);
});

test('just happened means recently, not five days ago', async ({ page }) => {
  // It had no window at all: `entries.slice(0, 5)`, so on a quiet brain
  // the five newest rows were all ancient. Measured on the real install:
  // entries stamped 126h19m under a heading that says "just happened".
  await stub(page);
  await page.goto('/console');
  await expect(page.locator('.v2-rail')).toBeVisible();
  await page.waitForTimeout(600);

  const rail = await page.locator('.v2-rail').innerText();
  expect(rail).toContain('Recent thing');
  expect(rail, 'a five-day-old row is under "Just happened"').not.toContain('Five days ago');
});

test('sections fold, and stay folded', async ({ page }) => {
  await stub(page, { busy: true });
  await page.goto('/console');

  const needsHead = page.locator('.v2-rail-head').first();
  await expect(page.getByText('coding_tools__write_file')).toBeVisible();

  await needsHead.click();
  await expect(needsHead).toHaveAttribute('aria-expanded', 'false');
  await expect(page.getByText('coding_tools__write_file')).toHaveCount(0);

  // The choice survives navigation, or the control is not worth having.
  await page.goto('/jobs');
  await expect(page.locator('.v2-rail-head').first())
    .toHaveAttribute('aria-expanded', 'false');

  await page.locator('.v2-rail-head').first().click();
  await expect(page.locator('.v2-rail-head').first())
    .toHaveAttribute('aria-expanded', 'true');
});
