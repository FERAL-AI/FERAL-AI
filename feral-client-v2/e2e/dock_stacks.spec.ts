/**
 * Press and hold a dock tile and its contents fan out above the dock.
 *
 * The approved design: "Press and hold a tile, or right-click it, and
 * its contents fan out above the dock: the two approvals with an
 * approve verb, the running jobs with kill and steer. You act from the
 * stack without navigating anywhere."
 *
 * jsdom cannot express any of this: the hold is a real timer against a
 * real pointer, and "sits above the dock and inside the viewport" is a
 * layout question. So it lives here.
 *
 * The one that matters most is that a hold does NOT also follow the
 * link. The tile is an anchor, so without cancelling the click a hold
 * would open the stack and navigate away from the page you were about
 * to act on, which makes the gesture worse than useless.
 *
 * Note on the route mocks below: Playwright matches the most recently
 * registered route first, so the catch-all is registered BEFORE the
 * specific one. Registering them the other way round makes every stack
 * come back empty, which is how this spec first failed.
 */
import { test, expect } from '@playwright/test';
test('press and hold fans a stack, and does not navigate', async ({ page }) => {
  await page.route('**/api/**', r => r.fulfill({ status: 200, contentType: 'application/json', body: '{}' }));
  await page.route('**/api/approvals*', r => r.fulfill({ status: 200, contentType: 'application/json',
    body: JSON.stringify({ count: 2, approvals: [
      { request_id: 'r1', tool_name: 'coding_tools__write_file', args: { path: '~/Projects/build.sh' }, safety_level: 'confirm' },
      { request_id: 'r2', tool_name: 'shell__run', args: { command: 'rm -rf tmp' }, safety_level: 'critical' }] }) }));
  await page.goto('/console');
  const tile = page.locator('.v2-dock-btn[href="/approvals"]');
  await expect(tile).toBeVisible();

  const box = await tile.boundingBox();
  await page.mouse.move(box!.x + box!.width / 2, box!.y + box!.height / 2);
  await page.mouse.down();
  await page.waitForTimeout(700);          // longer than HOLD_MS
  await page.mouse.up();

  const stack = page.locator('.v2-stack');
  await expect(stack).toBeVisible();
  await expect(stack.getByText('coding_tools__write_file')).toBeVisible();
  await expect(stack.getByText('~/Projects/build.sh')).toBeVisible();
  expect(await stack.getByText('approve').count()).toBe(2);
  // The hold must not also follow the link.
  expect(new URL(page.url()).pathname).toBe('/console');

  // It sits above the dock and inside the viewport.
  const s = await stack.boundingBox();
  const d = await page.locator('.v2-dock').boundingBox();
  expect(s!.y + s!.height).toBeLessThanOrEqual(d!.y + 2);
  expect(s!.x).toBeGreaterThanOrEqual(0);

  await page.keyboard.press('Escape');
  await expect(stack).toBeHidden();
});

test('right-click opens the same stack', async ({ page }) => {
  await page.route('**/api/**', r => r.fulfill({ status: 200, contentType: 'application/json', body: '{}' }));
  await page.route('**/api/approvals*', r => r.fulfill({ status: 200, contentType: 'application/json',
    body: JSON.stringify({ count: 1, approvals: [{ request_id: 'r1', tool_name: 'browser__evaluate', args: {} }] }) }));
  await page.goto('/console');
  await page.locator('.v2-dock-btn[href="/approvals"]').click({ button: 'right' });
  await expect(page.locator('.v2-stack')).toBeVisible();
  await expect(page.locator('.v2-stack').getByText('browser__evaluate')).toBeVisible();
});

test('a tile with nothing to act on does not open a stack', async ({ page }) => {
  await page.route('**/api/**', r => r.fulfill({ status: 200, contentType: 'application/json', body: '{}' }));
  await page.goto('/console');
  await page.locator('.v2-dock-btn[href="/settings"]').click({ button: 'right' });
  await expect(page.locator('.v2-stack')).toHaveCount(0);
});
