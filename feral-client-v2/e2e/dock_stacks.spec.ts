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

/**
 * The stack must be clickable, not merely visible.
 *
 * Every assertion above passes against a stack nobody can touch.
 * `toBeVisible()` checks the box model and visibility, and knows
 * nothing about `pointer-events`, so a panel painting at full opacity
 * with correct geometry and inert buttons satisfies all of them. That
 * is exactly what shipped: `.v2-stack` is a sibling of `.v2-dock-list`
 * under `.v2-dock`, which sets `pointer-events: none` for the whole
 * transparent bar and relies on the list to turn it back on. The stack
 * inherited none. `document.elementFromPoint` at the Open button's
 * centre returned `main.v2-shell-main`, the page behind the dock, and a
 * real click timed out after 3000ms. All four combinations of
 * 1512px/375px and light/dark reproduced it.
 *
 * So this asserts the two things the visibility checks cannot: that a
 * real click lands and does what it says, and that no ancestor has
 * turned the panel off. The click alone would be enough to fail, but
 * the computed check names the cause instead of just timing out.
 */
test('the stack is clickable, not just visible', async ({ page }) => {
  await page.route('**/api/**', r => r.fulfill({ status: 200, contentType: 'application/json', body: '{}' }));
  await page.route('**/api/approvals*', r => r.fulfill({ status: 200, contentType: 'application/json',
    body: JSON.stringify({ count: 1, approvals: [
      { request_id: 'r1', tool_name: 'coding_tools__write_file', args: { path: '~/Projects/build.sh' }, safety_level: 'confirm' }] }) }));
  await page.goto('/console');

  const tile = page.locator('.v2-dock-btn[href="/approvals"]');
  const box = await tile.boundingBox();
  await page.mouse.move(box!.x + box!.width / 2, box!.y + box!.height / 2);
  await page.mouse.down();
  await page.waitForTimeout(700);
  await page.mouse.up();

  const stack = page.locator('.v2-stack');
  await expect(stack).toBeVisible();

  // Nothing above it may have switched the pointer off.
  const inert = await page.evaluate(() => {
    const el = document.querySelector('.v2-stack');
    if (!el) return 'no stack';
    const dead: string[] = [];
    for (const node of [el, ...el.querySelectorAll('button')]) {
      if (getComputedStyle(node).pointerEvents === 'none') {
        dead.push(node.className || node.tagName);
      }
    }
    // And the topmost element at the panel's own centre must be the
    // panel, not whatever is painted behind the transparent dock bar.
    const b = el.getBoundingClientRect();
    const hit = document.elementFromPoint(b.left + b.width / 2, b.top + b.height / 2);
    const behind = el.contains(hit as Node) ? '' : `centre hits ${(hit as Element)?.className || hit?.nodeName}`;
    return [dead.length ? `pointer-events none on: ${dead.join(', ')}` : '', behind]
      .filter(Boolean).join('; ');
  });
  expect(inert, 'the stack is painted but inert').toBe('');

  // The load-bearing assertion: a real click, and it navigates.
  await stack.getByRole('button', { name: /Open Needs you/i }).click({ timeout: 3000 });
  await expect(page).toHaveURL(/\/approvals$/);
});

/**
 * A verb that fails has to say so.
 *
 * Both the stack and the work rail call the brain with `silent: true`,
 * which suppresses the global error surface deliberately: a transient
 * poll failure must not throw a banner over the shell. The cost was
 * that a 404 on approve left the row sitting in place with zero error
 * text anywhere in the DOM. Leaving the row is correct (removing it
 * would claim a decision that did not land) but silence is not: it
 * looks exactly like a click that missed, so the user clicks again.
 */
test('a failed approve from the stack says so on the row', async ({ page }) => {
  await page.route('**/api/**', r => r.fulfill({ status: 200, contentType: 'application/json', body: '{}' }));
  await page.route('**/api/approvals*', r => r.fulfill({ status: 200, contentType: 'application/json',
    body: JSON.stringify({ count: 1, approvals: [
      { request_id: 'r1', tool_name: 'coding_tools__write_file', args: { path: '~/x.sh' }, safety_level: 'confirm' }] }) }));
  // The decision itself fails, the way it does when the brain has
  // already timed the request out.
  await page.route('**/api/approvals/r1/approve', r => r.fulfill({
    status: 404, contentType: 'application/json',
    body: JSON.stringify({ error: 'no such approval request' }),
  }));

  await page.goto('/console');
  const tile = page.locator('.v2-dock-btn[href="/approvals"]');
  const box = await tile.boundingBox();
  await page.mouse.move(box!.x + box!.width / 2, box!.y + box!.height / 2);
  await page.mouse.down();
  await page.waitForTimeout(700);
  await page.mouse.up();

  const stack = page.locator('.v2-stack');
  await expect(stack).toBeVisible();
  await stack.getByRole('button', { name: 'approve' }).click();

  // The row stays...
  await expect(stack.getByText('coding_tools__write_file')).toBeVisible();
  // ...and now it explains itself.
  await expect(stack.locator('.v2-stack-failed')).toBeVisible();
  const text = await stack.locator('.v2-stack-failed').textContent();
  expect((text || '').trim().length, 'the failure message is empty').toBeGreaterThan(0);
});
