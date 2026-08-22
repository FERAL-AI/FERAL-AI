/**
 * Two surfaces that reported state and offered nothing to do about it.
 *
 * Reported: "in the status bar the Running thing that shows kill and
 * steer and operations running, also when I click on Open running I see
 * open running but nothing there to click on??", and "when I click on
 * console and it has 3 fields or boxes and right now one has the boxes
 * inside it looking stupid".
 *
 * Both were real:
 *
 *   Jobs   `cancelRouteOf(job)` was computed and then rendered as a
 *          <span> reading "stoppable". The page that lists everything
 *          the brain is doing had no verb on any row, and the one thing
 *          on each row looked like a checkbox that did nothing.
 *
 *   Console the brain figure read `health` off /api/dashboard, which is
 *          `latest_health`, the health-READINGS summary, and `{}` on any
 *          brain with no sensor data. So it rendered a bare "?" beside
 *          two real numbers.
 */
import { test, expect } from '@playwright/test';

const NOW = Math.floor(Date.now() / 1000);

async function stub(page, opts: { stoppable?: boolean; cancelStatus?: number } = {}) {
  // Catch-all first: Playwright matches the most recently registered.
  await page.route('**/api/**', (r) => r.fulfill({
    status: 200, contentType: 'application/json', body: '{}',
  }));
  await page.route('**/api/jobs*', (r) => r.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({
      items: [
        {
          id: 'j1', name: 'npm run build', kind: 'background_bash',
          status: 'running', started_at: NOW - 252,
          cancellable_via: opts.stoppable === false ? '' : 'POST /api/jobs/j1/cancel',
        },
        {
          id: 'j2', name: 'Health Tracker', kind: 'specialist',
          status: 'running', started_at: NOW - 90, cancellable_via: '',
        },
      ],
      counts_by_kind: { background_bash: 1, specialist: 1 },
      degraded: {},
    }),
  }));
  if (opts.cancelStatus) {
    await page.route('**/api/jobs/j1/cancel', (r) => r.fulfill({
      status: opts.cancelStatus, contentType: 'application/json',
      body: JSON.stringify({ error: 'that job is already gone' }),
    }));
  }
}

test('a stoppable job offers a control, not a label', async ({ page }) => {
  await stub(page);
  await page.goto('/jobs');

  const stop = page.getByRole('button', { name: /^Stop npm run build$/i });
  await expect(stop, 'the only affordance on the row is not a control').toBeVisible();
  await expect(stop).toBeEnabled();

  // A job the brain names no route for keeps saying so, and stays a
  // label: a button that 404s is worse than no button.
  await expect(page.getByText('not stoppable here')).toBeVisible();
  expect(await page.getByRole('button', { name: /^Stop / }).count()).toBe(1);
});

test('a stop that fails says so on the row it was clicked on', async ({ page }) => {
  await stub(page, { cancelStatus: 404 });
  await page.goto('/jobs');

  await page.getByRole('button', { name: /^Stop npm run build$/i }).click();

  // The row stays, because the brain did not say it stopped, and the
  // reason appears where the click happened rather than nowhere.
  // Scoped to the jobs list: the work rail shows the same job by name,
  // so an unscoped match is ambiguous (and that ambiguity is itself
  // evidence the rail is reporting the same running work).
  await expect(page.getByLabel('Active jobs').getByText('npm run build')).toBeVisible();
  await expect(page.locator('.v2-job-failed')).toBeVisible();
  const text = await page.locator('.v2-job-failed').textContent();
  expect((text || '').trim().length).toBeGreaterThan(0);
});

test('the console brain figure reports liveness, not a question mark', async ({ page }) => {
  await stub(page);
  await page.goto('/console');

  const figures = page.locator('.v2-console-figure');
  await expect(figures).toHaveCount(3);

  const brain = figures.filter({ hasText: 'brain' });
  await expect(brain).toBeVisible();
  const value = await brain.locator('.v2-console-n').textContent();
  expect(value, 'the brain figure rendered a bare "?"').not.toBe('?');
  expect(['ok', 'down']).toContain((value || '').trim());
});

test('the console brain figure goes down with the brain', async ({ page }) => {
  await stub(page);
  await page.goto('/console');
  await expect(page.locator('.v2-console-figure').filter({ hasText: 'brain' })
    .locator('.v2-console-n')).toHaveText('ok');

  // Fail every source the shared poller reads.
  await page.route('**/api/**', (r) => r.abort());
  await expect(
    page.locator('.v2-console-figure').filter({ hasText: 'brain' }).locator('.v2-console-n'),
    'the console kept claiming the brain was ok while every call failed',
  ).toHaveText('down', { timeout: 15000 });
});

test('the palette search field is marked without a hard rectangle', async ({ page }) => {
  // It carried `outline: 2px solid accent` and the palette focuses the
  // field on open, so that rectangle was on screen every single time
  // the palette was used and read as a rendering fault.
  await stub(page);
  await page.goto('/console');
  await page.keyboard.press('ControlOrMeta+k');

  const search = page.locator('.v2-cmdk-search');
  await expect(search).toBeFocused();

  const style = await search.evaluate((el) => {
    const cs = getComputedStyle(el);
    return { outline: cs.outlineStyle, width: cs.outlineWidth, shadow: cs.boxShadow };
  });
  expect(style.outline === 'none' || style.width === '0px').toBe(true);
  // Removed, not deleted: a focused field must still be marked.
  expect(style.shadow, 'the focused search field has no indicator at all')
    .not.toBe('none');
});

/**
 * An empty Needs You has to answer why it is empty.
 *
 * The tier is the load-bearing fact: on `loose` the brain never stops
 * to ask, so an empty queue means "nothing will ever appear here",
 * which used to render identically to "nothing right now".
 */
test('an empty approvals queue explains itself and can change the tier', async ({ page }) => {
  await page.route('**/api/**', (r) => r.fulfill({
    status: 200, contentType: 'application/json', body: '{}',
  }));
  await page.route('**/api/approvals*', (r) => r.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ count: 0, approvals: [] }),
  }));
  await page.route('**/api/autonomy', (r) => r.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ mode: 'hybrid' }),
  }));
  await page.route('**/api/policy', (r) => r.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({
      permissions: { require_confirmation_above: 'active', auto_approve_categories: ['sensor'] },
      filesystem: { write_paths: ['~/.feral/skills/'] },
      network: { mode: 'allowlist', allowed_domains: ['api.openai.com'] },
    }),
  }));

  await page.goto('/approvals');
  await expect(page.getByText('Nothing is waiting on you')).toBeVisible();

  // The tier, and which one is in force.
  const tiers = page.locator('.v2-appr-tierbtn');
  await expect(tiers).toHaveCount(3);
  await expect(page.locator('.v2-appr-tierbtn[aria-pressed="true"] .v2-appr-tiername'))
    .toHaveText('hybrid');

  // And what the policy actually permits, from the brain.
  await expect(page.getByText(/Runs without asking: sensor/)).toBeVisible();
  await expect(page.getByText(/Held for you above the "active" tier/)).toBeVisible();
});

test('a loose brain says so, because it will never fill this page', async ({ page }) => {
  await page.route('**/api/**', (r) => r.fulfill({
    status: 200, contentType: 'application/json', body: '{}',
  }));
  await page.route('**/api/approvals*', (r) => r.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ count: 0, approvals: [] }),
  }));
  await page.route('**/api/autonomy', (r) => r.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ mode: 'loose' }),
  }));

  await page.goto('/approvals');
  // The heading itself changes, because the fact is different.
  await expect(page.getByText('Nothing will stop and ask you')).toBeVisible();
  await expect(page.locator('.v2-appr-warn')).toBeVisible();
  await expect(page.locator('.v2-appr-warn')).toContainText(/never stops to ask/i);
});
