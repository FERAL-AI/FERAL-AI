/**
 * Marketplace install-consent spec (WORK-ORDER P0.2 / P0.3).
 *
 * The unit tests prove the wiring: no install request before confirm, the
 * token is echoed back, the copy is in the tree. They cannot prove the
 * thing that makes a consent dialog worth building, which is that a
 * person can read it. jsdom has no layout engine, so "the permission
 * list is in the document" and "the permission list is on screen, not
 * clipped by the dock, with the Install button reachable" are different
 * claims and only one of them is checkable there.
 *
 * This runs in real Chrome and asserts geometry:
 *   - the dialog paints inside the viewport,
 *   - every permission row is visible, laid out below the signature
 *     banner, and none of them overlap,
 *   - Cancel and Install are both inside the dialog box and inside the
 *     viewport (the failure mode for a long permission list is a footer
 *     pushed off the bottom of the sheet),
 *   - an unverified package disables Install rather than explaining it
 *     away.
 *
 * `/api/*` is stubbed; the broader e2e program runs against a real brain.
 */
import { test, expect, Route } from '@playwright/test';

// Runs on the config's default `chromium` project. It used to pin
// `channel: 'chrome'` with the note "drive real Chrome, not bundled
// chromium: this is a rendering claim". The rendering claim is real, but
// borrowing the machine's Chrome is not how to keep it: the Playwright
// browsers had simply never been downloaded, so the channel pin made the
// spec run on one developer's laptop and skip everywhere else. `make
// dev-deps` and the CI e2e job both fetch chromium now, so the spec runs
// on a browser the repo controls the version of.

const CATALOG_ITEM = {
  id: 'trail_notes',
  kind: 'skill',
  name: 'Trail Notes',
  version: '2.1.0',
  description: 'Keeps field notes and syncs them to your notebook.',
  publisher: 'acme-labs',
  downloads: 431,
  permissions: ['filesystem', 'network'],
  permission_details: [
    { id: 'filesystem', label: 'Your files', description: 'Read and write files on this computer, including documents outside FERAL’s own folder.' },
    { id: 'network', label: 'Internet access', description: 'Contact servers on the internet and send them data it can reach.' },
  ],
};

const PREVIEW = {
  success: true,
  kind: 'skill',
  id: 'trail_notes',
  name: 'Trail Notes',
  version: '2.1.0',
  publisher: 'acme-labs',
  description: 'Keeps field notes and syncs them to your notebook.',
  permissions: ['filesystem', 'network', 'code_execution', 'screen', 'input_control'],
  permission_details: [
    { id: 'filesystem', label: 'Your files', description: 'Read and write files on this computer, including documents outside FERAL’s own folder.' },
    { id: 'network', label: 'Internet access', description: 'Contact servers on the internet and send them data it can reach.' },
    { id: 'code_execution', label: 'Run programs', description: 'Run shell commands and other programs on this computer under your user account.' },
    { id: 'screen', label: 'Screen contents', description: 'Capture what is on your screen, including any window you have open.' },
    { id: 'input_control', label: 'Keyboard and mouse', description: 'Move the pointer, type, and click in any app as if it were you.' },
  ],
  permissions_source: 'package',
  signature: {
    verified: true,
    publisher: 'acme-labs',
    sha256: '3f786850e387550fdab836ed7e6dc881de23001b3f786850e387550fdab836ed',
    reason: '',
  },
  install_token: 'e2e-preview-token',
  expires_in: 300,
};

type Stub = { verified: boolean };

const installApiStubs = async (page, stub: Stub = { verified: true }) => {
  const installs: unknown[] = [];
  await page.route('**/api/**', async (route: Route) => {
    const url = route.request().url();
    const ok = (body: unknown, status = 200) =>
      route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

    if (url.includes('/api/marketplace/catalog')) return ok({ items: [CATALOG_ITEM], kind: 'skill' });
    if (url.includes('/api/marketplace/installed')) return ok({ skills: [] });
    if (url.includes('/api/marketplace/preview')) {
      if (stub.verified) return ok(PREVIEW);
      // What the brain really returns for a package that did not verify:
      // 400, no install token, and the reason.
      return ok({
        success: false,
        error: 'signature verification failed: sha256 mismatch',
        signature: { verified: false, reason: 'sha256 mismatch', sha256: '', publisher: '' },
      }, 400);
    }
    if (url.includes('/api/marketplace/install')) {
      installs.push(route.request().postDataJSON());
      return ok({ success: true, skill_id: 'trail_notes', permissions: PREVIEW.permissions });
    }
    if (url.includes('/api/dashboard') || url.includes('/api/identity')
        || url.includes('/api/setup/status') || url.includes('/health')) {
      return ok({ ok: true, status: 'ok', identity: {}, somatic: { cognitive_load: 0.2 } });
    }
    return ok({});
  });
  return installs;
};

test.describe('Marketplace install consent', () => {
  test('the consent dialog is readable and Install is reachable', async ({ page }) => {
    const installs = await installApiStubs(page);

    await page.goto('/marketplace');

    const card = page.getByText('Trail Notes').first();
    await expect(card).toBeVisible();
    // The card itself names the capabilities, before any click.
    await expect(page.getByTestId('v2-card-permissions')).toBeVisible();

    await page.getByRole('button', { name: /^Install$/ }).first().click();

    const dialog = page.getByRole('dialog');
    await expect(dialog).toBeVisible();
    const dialogBox = (await dialog.boundingBox())!;
    const viewport = page.viewportSize()!;

    // The sheet paints inside the viewport.
    expect(dialogBox.x).toBeGreaterThanOrEqual(0);
    expect(dialogBox.y).toBeGreaterThanOrEqual(0);
    expect(dialogBox.x + dialogBox.width).toBeLessThanOrEqual(viewport.width);
    expect(dialogBox.y + dialogBox.height).toBeLessThanOrEqual(viewport.height);

    // Signature banner reads as reassurance only when it is true.
    const sig = page.getByTestId('v2-install-signature');
    await expect(sig).toBeVisible();
    await expect(sig).toContainText(/Signature verified/i);
    await expect(sig).toContainText(/acme-labs/);

    // Every permission row is on screen and none of them overlap.
    const rows = page.getByTestId('v2-install-permissions').locator('li');
    await expect(rows).toHaveCount(5);
    const boxes: { x: number; y: number; width: number; height: number }[] = [];
    for (let i = 0; i < 5; i += 1) {
      const row = rows.nth(i);
      await expect(row).toBeVisible();
      boxes.push((await row.boundingBox())!);
    }
    const sigBox = (await sig.boundingBox())!;
    for (const b of boxes) {
      expect(b.height).toBeGreaterThan(20);
      expect(b.y).toBeGreaterThan(sigBox.y + sigBox.height - 1);
    }
    for (let i = 1; i < boxes.length; i += 1) {
      expect(boxes[i].y).toBeGreaterThanOrEqual(boxes[i - 1].y + boxes[i - 1].height - 1);
    }

    // Both decisions are reachable without leaving the sheet.
    const confirm = page.getByTestId('v2-install-confirm');
    const cancel = page.getByTestId('v2-install-cancel');
    await expect(confirm).toBeEnabled();
    const confirmBox = (await confirm.boundingBox())!;
    const cancelBox = (await cancel.boundingBox())!;
    for (const b of [confirmBox, cancelBox]) {
      expect(b.y + b.height).toBeLessThanOrEqual(viewport.height);
      expect(b.y + b.height).toBeLessThanOrEqual(dialogBox.y + dialogBox.height + 1);
      expect(b.width).toBeGreaterThan(40);
    }

    await page.screenshot({ path: 'e2e/.artifacts/install-consent-verified.png', fullPage: false });

    // Nothing installed while the dialog sits open.
    expect(installs).toHaveLength(0);

    await confirm.click();
    await expect(dialog).toBeHidden();
    expect(installs).toHaveLength(1);
    expect(installs[0]).toMatchObject({
      kind: 'skill',
      id: 'trail_notes',
      install_token: 'e2e-preview-token',
    });
  });

  test('cancelling installs nothing', async ({ page }) => {
    const installs = await installApiStubs(page);
    await page.goto('/marketplace');
    await page.getByRole('button', { name: /^Install$/ }).first().click();
    await expect(page.getByRole('dialog')).toBeVisible();
    await page.getByTestId('v2-install-cancel').click();
    await expect(page.getByRole('dialog')).toBeHidden();
    expect(installs).toHaveLength(0);
  });

  test('a long permission list scrolls inside the sheet and keeps Install reachable', async ({ page }) => {
    // Worst case: a skill declaring the whole vocabulary, on a short
    // laptop viewport. The failure this guards against is a footer
    // pushed below the fold, where the only visible choice is to give up.
    await page.setViewportSize({ width: 900, height: 600 });
    const many = Array.from({ length: 22 }, (_, i) => ({
      id: `perm_${i}`,
      label: `Capability ${i}`,
      description: 'A full sentence of consent copy that wraps on a narrow sheet and takes vertical space.',
    }));
    await page.route('**/api/**', async (route: Route) => {
      const url = route.request().url();
      const ok = (b: unknown) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(b) });
      if (url.includes('/api/marketplace/catalog')) return ok({ items: [CATALOG_ITEM] });
      if (url.includes('/api/marketplace/preview')) {
        return ok({ ...PREVIEW, permissions: many.map((m) => m.id), permission_details: many });
      }
      if (url.includes('/api/marketplace/install')) return ok({ success: true });
      return ok({ ok: true, skills: [], items: [] });
    });

    await page.goto('/marketplace');
    await page.getByRole('button', { name: /^Install$/ }).first().click();

    const dialog = page.getByRole('dialog');
    await expect(dialog).toBeVisible();
    const dialogBox = (await dialog.boundingBox())!;
    expect(dialogBox.y + dialogBox.height).toBeLessThanOrEqual(600);

    // The body is the thing that scrolls, not the page.
    const body = dialog.locator('.v2-modal-body');
    const overflow = await body.evaluate((el) => el.scrollHeight > el.clientHeight);
    expect(overflow, 'a 22-permission list should scroll inside the sheet').toBe(true);
    const pageScrolls = await page.evaluate(() => document.documentElement.scrollHeight > window.innerHeight);
    expect(pageScrolls).toBe(false);

    // Install is still on screen without scrolling anything.
    const confirm = page.getByTestId('v2-install-confirm');
    await expect(confirm).toBeVisible();
    const cb = (await confirm.boundingBox())!;
    expect(cb.y + cb.height).toBeLessThanOrEqual(600);

    await page.screenshot({ path: 'e2e/.artifacts/install-consent-long-list.png' });
  });

  test('an unverified package cannot be installed', async ({ page }) => {
    const installs = await installApiStubs(page, { verified: false });
    await page.goto('/marketplace');
    await page.getByRole('button', { name: /^Install$/ }).first().click();

    const sig = page.getByTestId('v2-install-signature');
    await expect(sig).toBeVisible();
    await expect(sig).toContainText(/Not verified/i);
    await expect(sig).toContainText(/sha256 mismatch/i);
    // No permission list on an unverified package: it would be a
    // description of bytes nobody vouched for.
    await expect(page.getByTestId('v2-install-permissions')).toHaveCount(0);
    await expect(page.getByTestId('v2-install-confirm')).toBeDisabled();

    await page.screenshot({ path: 'e2e/.artifacts/install-consent-unverified.png', fullPage: false });
    expect(installs).toHaveLength(0);
  });
});
