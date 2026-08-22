/**
 * The system bar is a row of controls, not a status line.
 *
 * The approved design says each vital opens a popover you act from.
 * Two failures this guards, both of which look fine in jsdom:
 *
 * 1. A popover that paints and cannot be clicked. Exactly what happened
 *    to the dock stack, which rendered at full opacity with correct
 *    geometry inside an ancestor carrying `pointer-events: none`, and
 *    passed a `toBeVisible()` spec for as long as it was broken.
 * 2. A popover that opens off the right edge of the window. It is
 *    anchored to a button near the viewport edge, so this is a real
 *    risk and invisible without layout.
 */
import { test, expect } from '@playwright/test';

const DASHBOARD = {
  device_count: 3, online_count: 3, skills_count: 42, llm_available: true,
  uptime_s: 15132,
  memory: { episodes: 12410, notes: 8, knowledge_triples: 5, embedded_chunks: 60, vec_index_mode: 'numpy' },
  budget: { enabled: true, daily_budget_usd: 10, daily_spend_usd: 1.84 },
  autonomy: 'loose',
  channels: [], devices: [], boot: {}, health: {},
};

async function stub(page) {
  // Playwright matches the MOST RECENTLY registered route first, so the
  // catch-all goes first and the specific ones after it. Registered the
  // other way round, every stub below is shadowed by `{}` and the bar
  // renders as if the brain answered nothing, which is precisely the
  // bug this file exists to catch. Same trap is noted in
  // e2e/dock_stacks.spec.ts, where it was found the first time.
  await page.route('**/api/**', (r) => r.fulfill({
    status: 200, contentType: 'application/json', body: '{}',
  }));
  await page.route('**/api/dashboard*', (r) => r.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify(DASHBOARD),
  }));
  await page.route('**/api/approvals*', (r) => r.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ count: 2, approvals: [
      { request_id: 'r1', tool_name: 'coding_tools__write_file', args: { path: '~/Projects/build.sh' } },
      { request_id: 'r2', tool_name: 'shell__run', args: { command: 'rm -rf tmp' } }] }),
  }));
  await page.route('**/api/jobs*', (r) => r.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ items: [
      { id: 'j1', name: 'npm run build', kind: 'background_bash', status: 'running',
        started_at: Date.now() / 1000 - 252, cancellable_via: 'POST /api/jobs/j1/cancel' }] }),
  }));
}

test('every vital the brain can answer is on the bar', async ({ page }) => {
  await stub(page);
  await page.goto('/console');
  await expect(page.locator('.v2-sysbar')).toBeVisible();
  await expect(page.locator('.v2-ext').first()).toBeVisible();

  const labels = await page.locator('.v2-ext').evaluateAll(
    (els) => els.map((e) => (e.getAttribute('aria-label') || '').trim()),
  );
  // Cost and autonomy are the two that read as absent for as long as
  // they were wired to fields the dashboard does not have.
  expect(labels.join(' | ')).toContain('$1.84');
  expect(labels.join(' | ')).toContain('loose');
  expect(labels.join(' | ')).toContain('waiting on you');
  // 12,410 episodes, not a token count.
  await expect(page.locator('.v2-ext', { hasText: '12.4k' })).toBeVisible();
  // And the count badge, which is what makes "2 waiting" readable.
  await expect(page.locator('.v2-ext-count')).toHaveText('2');
});

for (const width of [1512, 1024, 768]) {
  test(`a popover opens, stays on screen and is clickable at ${width}px`, async ({ page }) => {
    await stub(page);
    await page.setViewportSize({ width, height: 860 });
    await page.goto('/console');
    await page.locator('.v2-ext[aria-label*="waiting on you"]').click();

    const pop = page.locator('.v2-pop');
    await expect(pop).toBeVisible();
    await expect(pop.getByText('coding_tools__write_file')).toBeVisible();

    const bad = await page.evaluate(() => {
      const el = document.querySelector('.v2-pop');
      if (!el) return 'no popover';
      const dead: string[] = [];
      for (const n of [el, ...el.querySelectorAll('button')]) {
        if (getComputedStyle(n).pointerEvents === 'none') dead.push(n.className || n.tagName);
      }
      const r = el.getBoundingClientRect();
      const off = r.right > window.innerWidth + 1 || r.left < -1;
      const hit = document.elementFromPoint(r.left + r.width / 2, r.top + 20);
      return [
        dead.length ? `pointer-events none on ${dead.join(', ')}` : '',
        off ? `off screen: left ${r.left.toFixed(0)} right ${r.right.toFixed(0)} of ${window.innerWidth}` : '',
        el.contains(hit) ? '' : `centre hits ${(hit as Element)?.className || hit?.nodeName}`,
      ].filter(Boolean).join('; ');
    });
    expect(bad, 'the popover is painted but not usable').toBe('');

    // The load-bearing assertion: the verb is a real, clickable control.
    await expect(pop.getByRole('button', { name: 'approve' }).first()).toBeEnabled();
  });
}

test('a job popover offers kill only when the brain names a route', async ({ page }) => {
  await stub(page);
  await page.goto('/console');
  await page.locator('.v2-ext[aria-label*="running"]').click();
  const pop = page.locator('.v2-pop');
  await expect(pop).toBeVisible();
  await expect(pop.getByText('npm run build')).toBeVisible();
  await expect(pop.getByRole('button', { name: 'kill' })).toBeEnabled();
});

test('the autonomy popover marks the current tier and offers the others', async ({ page }) => {
  await stub(page);
  await page.goto('/console');
  await page.locator('.v2-ext[aria-label*="Autonomy"]').click();
  const pop = page.locator('.v2-pop');
  await expect(pop).toBeVisible();
  for (const tier of ['strict', 'hybrid', 'loose']) {
    await expect(pop.getByText(tier, { exact: true })).toBeVisible();
  }
  await expect(pop.locator('.v2-pop-v.is-current')).toHaveText('current');
});

test('the rail collapses with B and with its own control', async ({ page }) => {
  await stub(page);
  await page.goto('/console');
  await expect(page.locator('.v2-rail')).toBeVisible();

  // The design documents the shortcut: "Press B to collapse the rail."
  await page.keyboard.press('b');
  await expect(page.locator('.v2-rail')).toHaveCount(0);
  await page.keyboard.press('b');
  await expect(page.locator('.v2-rail')).toBeVisible();

  await page.locator('.v2-sysbar-icon[aria-label*="Collapse the rail"]').click();
  await expect(page.locator('.v2-rail')).toHaveCount(0);
});

test('B does not collapse the rail while you are typing', async ({ page }) => {
  // `b` is a letter before it is a shortcut. The palette's search field
  // is used here rather than the chat composer because the composer is
  // disabled until the brain socket connects, and a disabled input
  // cannot demonstrate anything about typing.
  await stub(page);
  await page.goto('/console');
  await expect(page.locator('.v2-rail')).toBeVisible();

  await page.keyboard.press('ControlOrMeta+k');
  const search = page.locator('.v2-cmdk-search');
  await expect(search).toBeVisible();
  await search.fill('');
  await search.type('big bold ideas');

  await expect(search).toHaveValue('big bold ideas');
  await expect(
    page.locator('.v2-rail'),
    'a letter key ate the rail mid-sentence',
  ).toBeVisible();
});

test('the brand light is green when the brain answers and red when it does not', async ({ page }) => {
  // A dot beside a product name reads as a status light, so it has to
  // BE one. It was a conic gradient ring: identical whether the brain
  // was up or gone.
  await stub(page);
  await page.goto('/console');
  const mark = page.locator('.v2-sysbar-mark');
  await expect(mark).toHaveAttribute('data-up', 'yes');

  const green = await mark.evaluate((el) => getComputedStyle(el).backgroundColor);

  // Now fail every source the shared poller reads, the way a stopped
  // brain does. `reachable` is false only when ALL of them fail, so one
  // slow endpoint must not turn this red.
  await page.route('**/api/**', (r) => r.abort());
  await expect(mark).toHaveAttribute('data-up', 'no', { timeout: 15000 });

  const red = await mark.evaluate((el) => getComputedStyle(el).backgroundColor);
  expect(red, 'the light looks identical up and down').not.toBe(green);

  // Colour alone is not an accessible signal.
  await expect(page.locator('.v2-sr-only')).toHaveText(/not responding/i);
});

test('the Brain popover leads with uptime and names the model', async ({ page }) => {
  await stub(page);
  await page.route('**/api/llm/status*', (r) => r.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ available: true, provider: 'openai', model: 'gpt-5.6-sol' }),
  }));
  await page.goto('/console');
  await page.locator('.v2-ext[aria-label*="Brain"]').click();

  const pop = page.locator('.v2-pop');
  await expect(pop).toBeVisible();
  // The design leads this one with a single large number.
  await expect(pop.locator('.v2-pop-big b')).toHaveText(/\d/);
  await expect(pop.locator('.v2-pop-big span')).toHaveText('uptime');
  // And says WHICH model, which "LLM: available" could not.
  await expect(pop.getByText('openai / gpt-5.6-sol')).toBeVisible();
});
