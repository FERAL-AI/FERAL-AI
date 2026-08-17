/**
 * App install-consent spec.
 *
 * The unit tests prove the wiring: no install request before confirm, the
 * skills are in the tree, the token is echoed back. They cannot prove the
 * thing that makes a consent sheet worth building, which is that a person
 * can read it and act on it. jsdom has no layout engine, so "the skill
 * list is in the document" and "the skill list is on screen, below the
 * app's own permissions, with Install reachable" are different claims and
 * only one of them is checkable there.
 *
 * This runs in real Chrome and asserts geometry:
 *   - the sheet paints inside the viewport,
 *   - the app's own reach, the skills it installs, and the skills it
 *     cannot install are three visibly distinct blocks in that order,
 *   - a skill that cannot be installed shows its reason, its cost and its
 *     fix, all on screen and not clipped,
 *   - the confirm button says what it will actually do, and both
 *     decisions stay inside the sheet,
 *   - an unverified app disables Install and lists nothing.
 *
 * `/api/*` is stubbed; the broader e2e program runs against a real brain.
 */
import { test, expect, Route } from '@playwright/test';

// Runs on the config's default `chromium` project. It deliberately does
// NOT pin `channel: 'chrome'`: that only ever borrowed whatever Chrome a
// machine happened to have, and it hid the fact that the Playwright
// browsers had never been downloaded here. `make dev-deps` now fetches
// chromium, and `make e2e` runs this.

const APP_ITEM = {
  id: 'trail-app',
  kind: 'app',
  name: 'Trail',
  version: '2.0.0',
  description: 'Plans hikes and keeps field notes.',
  publisher: 'acme-labs',
  downloads: 88,
};

const APP_PREVIEW = {
  success: true,
  app: {
    app_id: 'trail-app',
    version: '2.0.0',
    author: 'acme-labs',
    description: 'Plans hikes and keeps field notes.',
    brand: { name: 'Trail' },
    entry_surface_id: 'home',
    surfaces: [{ surface_id: 'home', kind: 'authored' }],
  },
  source: { origin: 'registry_id', value: 'trail-app' },
  signature: { verified: true, publisher: 'acme-labs', sha256: 'b'.repeat(64), reason: '' },
  permission_details: [
    {
      id: 'network:api.trail.example',
      label: 'Contact api.trail.example',
      description: "This app's surfaces may send and receive data from api.trail.example. No other server is reachable from them.",
      known: true,
    },
  ],
  skill_dependencies: {
    declared: ['trail_notes', 'map_tiles', 'ghost_skill'],
    already_installed: [
      {
        skill_id: 'map_tiles',
        name: 'Map Tiles',
        version: '3.0.0',
        permissions: ['network'],
        permission_details: [
          { id: 'network', label: 'Internet access', description: 'Contact servers on the internet.' },
        ],
      },
    ],
    to_install: [
      {
        skill_id: 'trail_notes',
        name: 'Trail Notes',
        version: '1.4.0',
        publisher: 'acme-labs',
        permissions: ['filesystem', 'code_execution'],
        permission_details: [
          { id: 'filesystem', label: 'Your files', description: 'Read and write files on this computer, including documents outside FERAL’s own folder.' },
          { id: 'code_execution', label: 'Run programs', description: 'Run shell commands and other programs on this computer under your user account.' },
        ],
        signature: { verified: true, sha256: 'c'.repeat(64) },
      },
    ],
    unavailable: [
      {
        skill_id: 'ghost_skill',
        reason: "'ghost_skill' is not published in the registry at https://registry.feral.sh",
        remediation: {
          code: 'not_published',
          message: "'ghost_skill' is not published in the FERAL registry, so there is nothing for FERAL to verify or download.",
          action: 'Ask the publisher of this app to publish the skill to registry.feral.sh. If you have its source and you trust it, you can publish it yourself: run `feral publisher login` once, then `feral publish --skill <dir>`, then install it with `feral install ghost_skill`.',
          command: '',
        },
        impact: [
          { surface_id: 'home', action_id: 'sync_0', description: 'Sync your notes to the cloud', target: 'ghost_skill/sync' },
        ],
      },
    ],
  },
  degraded: true,
  install_token: 'e2e-app-token',
};

type Stub = { verified: boolean };

const installApiStubs = async (page, stub: Stub = { verified: true }) => {
  const installs: unknown[] = [];
  await page.route('**/api/**', async (route: Route) => {
    const url = route.request().url();
    const ok = (body: unknown, status = 200) =>
      route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

    if (url.includes('/api/marketplace/catalog')) return ok({ items: [APP_ITEM], kind: 'app' });
    if (url.includes('/api/marketplace/installed')) return ok({ skills: [] });
    if (url.includes('/api/apps/preview')) {
      if (stub.verified) return ok(APP_PREVIEW);
      // What the brain really returns for a bundle that did not verify.
      return ok({
        success: false,
        error: 'registry signature verification failed: sha256 mismatch',
        signature: { verified: false, reason: 'sha256 mismatch', sha256: '', publisher: '' },
      }, 422);
    }
    if (url.includes('/api/apps/install')) {
      installs.push(route.request().postDataJSON());
      return ok({ success: true, app: { app_id: 'trail-app' }, degraded: true });
    }
    if (url.includes('/api/dashboard') || url.includes('/api/identity')
        || url.includes('/api/setup/status') || url.includes('/health')) {
      return ok({ ok: true, status: 'ok', identity: {}, somatic: { cognitive_load: 0.2 } });
    }
    return ok({});
  });
  return installs;
};

const openSheet = async (page) => {
  await page.goto('/marketplace');
  await page.getByText('app', { exact: true }).click();
  await expect(page.getByText('Trail').first()).toBeVisible();
  await page.getByRole('button', { name: /^Install$/ }).first().click();
};

test.describe('App install consent', () => {
  test('the sheet separates the app from the code it installs, and Install is reachable', async ({ page }) => {
    const installs = await installApiStubs(page);
    await openSheet(page);

    const dialog = page.getByRole('dialog');
    await expect(dialog).toBeVisible();
    const dialogBox = (await dialog.boundingBox())!;
    const viewport = page.viewportSize()!;

    // The sheet paints inside the viewport.
    expect(dialogBox.x).toBeGreaterThanOrEqual(0);
    expect(dialogBox.y).toBeGreaterThanOrEqual(0);
    expect(dialogBox.x + dialogBox.width).toBeLessThanOrEqual(viewport.width);
    expect(dialogBox.y + dialogBox.height).toBeLessThanOrEqual(viewport.height);

    const sig = page.getByTestId('v2-app-install-signature');
    await expect(sig).toBeVisible();
    await expect(sig).toContainText(/Signature verified/i);
    await expect(sig).toContainText(/acme-labs/);

    // Three distinct blocks, laid out in order: what the app reaches,
    // what code it installs, what it cannot install.
    const appPerms = page.getByTestId('v2-install-permissions').first();
    const newSkills = page.getByTestId('v2-app-install-new-skills');
    const existing = page.getByTestId('v2-app-install-existing-skills');
    const unavailable = page.getByTestId('v2-app-install-unavailable-skills');
    for (const block of [appPerms, newSkills, existing, unavailable]) {
      await expect(block).toBeVisible();
    }
    const boxes = await Promise.all(
      [appPerms, newSkills, existing, unavailable].map(async (b) => (await b.boundingBox())!),
    );
    for (let i = 1; i < boxes.length; i += 1) {
      expect(
        boxes[i].y,
        'the sections must stack, not overlap',
      ).toBeGreaterThanOrEqual(boxes[i - 1].y + boxes[i - 1].height - 1);
    }

    // The skill that runs code is named with what it reaches.
    await expect(newSkills).toContainText('Trail Notes');
    await expect(newSkills).toContainText(/Run shell commands/i);

    // The one FERAL cannot install carries reason, cost and fix.
    await expect(unavailable).toContainText(/not published in the FERAL registry/i);
    await expect(unavailable).toContainText(/Sync your notes to the cloud/);
    await expect(unavailable).toContainText(/feral publish --skill/);

    // The button says what it will actually do.
    const confirm = page.getByTestId('v2-app-install-confirm');
    const cancel = page.getByTestId('v2-app-install-cancel');
    await expect(confirm).toContainText(/Install without 1 skill/);
    await expect(confirm).toBeEnabled();
    for (const b of [(await confirm.boundingBox())!, (await cancel.boundingBox())!]) {
      expect(b.y + b.height).toBeLessThanOrEqual(viewport.height);
      expect(b.y + b.height).toBeLessThanOrEqual(dialogBox.y + dialogBox.height + 1);
      expect(b.width).toBeGreaterThan(40);
    }

    await page.screenshot({ path: 'e2e/.artifacts/app-install-consent.png', fullPage: false });

    // Nothing installed while the sheet sits open.
    expect(installs).toHaveLength(0);

    await confirm.click();
    await expect(dialog).toBeHidden();
    expect(installs).toHaveLength(1);
    expect(installs[0]).toMatchObject({ install_token: 'e2e-app-token' });
  });

  test('the sheet scrolls inside itself and never scrolls the page', async ({ page }) => {
    await page.setViewportSize({ width: 900, height: 600 });
    await installApiStubs(page);
    await openSheet(page);

    const dialog = page.getByRole('dialog');
    await expect(dialog).toBeVisible();
    const dialogBox = (await dialog.boundingBox())!;
    expect(dialogBox.y + dialogBox.height).toBeLessThanOrEqual(600);

    const body = dialog.locator('.v2-modal-body');
    const overflow = await body.evaluate((el) => el.scrollHeight > el.clientHeight);
    expect(overflow, 'an app with three dependency blocks should scroll inside the sheet').toBe(true);
    const pageScrolls = await page.evaluate(
      () => document.documentElement.scrollHeight > window.innerHeight,
    );
    expect(pageScrolls).toBe(false);

    const confirm = page.getByTestId('v2-app-install-confirm');
    await expect(confirm).toBeVisible();
    expect((await confirm.boundingBox())!.y + (await confirm.boundingBox())!.height)
      .toBeLessThanOrEqual(600);

    await page.screenshot({ path: 'e2e/.artifacts/app-install-consent-short-viewport.png' });
  });

  test('cancelling installs nothing', async ({ page }) => {
    const installs = await installApiStubs(page);
    await openSheet(page);
    await expect(page.getByRole('dialog')).toBeVisible();
    await page.getByTestId('v2-app-install-cancel').click();
    await expect(page.getByRole('dialog')).toBeHidden();
    expect(installs).toHaveLength(0);
  });

  test('an unverified app cannot be installed', async ({ page }) => {
    const installs = await installApiStubs(page, { verified: false });
    await openSheet(page);

    const sig = page.getByTestId('v2-app-install-signature');
    await expect(sig).toBeVisible();
    await expect(sig).toContainText(/Not verified/i);
    await expect(sig).toContainText(/sha256 mismatch/i);
    // No skill list on a bundle nobody vouched for: it would be a
    // description of code with no known author.
    await expect(page.getByTestId('v2-app-install-new-skills')).toHaveCount(0);
    await expect(page.getByTestId('v2-app-install-unavailable-skills')).toHaveCount(0);
    await expect(page.getByTestId('v2-app-install-confirm')).toBeDisabled();

    await page.screenshot({ path: 'e2e/.artifacts/app-install-consent-unverified.png' });
    expect(installs).toHaveLength(0);
  });
});
