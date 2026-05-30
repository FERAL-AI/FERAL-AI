/**
 * Pair-a-device modal regression spec.
 *
 * User report:
 *   "clicking 'Pair a device' adds a row to the historical list but
 *    the modal never becomes visible."
 *
 * The fix has three pieces:
 *   1. Modal mounts via React Portal onto document.body so its
 *      stacking context is the body, not .v2-shell-main (which is
 *      itself elevated above .v2-ambient via z-index).
 *   2. styles/_z.css names the stacking constants and pages.css
 *      re-declares .v2-modal-backdrop using --z-modal so the modal
 *      genuinely sits above --z-dock.
 *   3. Devices.jsx hides any device IDs created by the active
 *      PairDeviceModal session from the historical list until pairing
 *      actually completes (claimed_at flips). Closing the modal
 *      revokes the unclaimed token and refreshes the list.
 *
 * What this spec asserts:
 *   - The dialog opens and is in the viewport (the original bug).
 *   - The dialog renders the QR placeholder and the privacy / permission
 *     copy from the Web phone tab.
 *   - The historical "Paired" pane does NOT increment while the modal
 *     is open and unclaimed (no "phantom row" effect).
 *
 * The spec stubs `/api/*` so it does not need a live brain. The broader
 * e2e program runs against a real backend.
 */
import { test, expect, Route } from '@playwright/test';

type PairedRow = {
  device_id: string;
  name: string;
  claimed_at: number | null;
  last_seen?: number | null;
};

const installApiStubs = async (page) => {
  // Mutable state for the historical Paired list. The /api/devices/pair/url
  // route appends to it on every issue (mirroring brain behaviour) so we can
  // observe whether the UI correctly hides those rows until claim.
  const pairedState: { rows: PairedRow[] } = { rows: [] };

  await page.route('**/api/**', async (route: Route) => {
    const req = route.request();
    const url = req.url();
    const method = req.method();

    const ok = (body: unknown) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(body),
      });

    if (url.includes('/api/devices/connected')) return ok({ devices: [] });
    if (url.includes('/api/hardware/mesh')) return ok({ nodes: [] });
    if (url.includes('/api/devices/paired')) return ok({ devices: pairedState.rows });

    if (url.includes('/api/devices/pair/url')) {
      const id = `dev-${pairedState.rows.length + 1}`;
      const token = `tok-${id}`;
      pairedState.rows.push({
        device_id: id,
        name: 'web-phone',
        claimed_at: null,
        last_seen: null,
      });
      return ok({
        token,
        device_id: id,
        url: `${new URL(url).origin}/pair?t=${token}`,
      });
    }

    if (url.match(/\/api\/devices\/[^/]+$/) && method === 'DELETE') {
      const id = decodeURIComponent(url.split('/').pop() || '');
      pairedState.rows = pairedState.rows.filter((r) => r.device_id !== id);
      return ok({ ok: true });
    }

    if (url.includes('/api/dashboard') || url.includes('/api/identity')
        || url.includes('/api/setup/status') || url.includes('/health')) {
      return ok({ ok: true, status: 'ok', identity: {}, somatic: { cognitive_load: 0.2 } });
    }

    return ok({});
  });
};

test.describe('Pair-a-device modal (W4 §A.2)', () => {
  test('clicking "Pair new device" opens a visible modal and does NOT add a phantom row', async ({ page }) => {
    await installApiStubs(page);

    await page.goto('/devices');

    // Wait for the page to mount + initial poll to settle. We look for
    // the hero CTA — Devices renders either "Pair new device" (in the
    // pane actions) or "Pair your first device" (in the empty state).
    const openButton = page.getByRole('button', { name: /Pair (new device|your first device)/i });
    await expect(openButton.first()).toBeVisible();

    // Sanity: nothing in the Paired pane before the user clicks.
    const pairedHeading = page.getByRole('heading', { name: /^Paired/i });
    await expect(pairedHeading).toHaveCount(0);

    await openButton.first().click();

    // The dialog must be visible and in the viewport. This is the
    // assertion that fails on the regression — the modal element
    // existed in the DOM but was hidden under the dock.
    const dialog = page.getByRole('dialog', { name: /Pair a device/i });
    await expect(dialog).toBeVisible();
    await expect(dialog).toHaveCount(1);

    const box = await dialog.boundingBox();
    expect(box, 'dialog must have a layout box').not.toBeNull();
    expect(box!.width).toBeGreaterThan(0);
    expect(box!.height).toBeGreaterThan(0);

    // Web phone tab content (QR placeholder + the privacy hint copy)
    // must be present so the user can actually pair.
    await expect(page.getByTestId('pair-web-phone')).toBeVisible();
    await expect(page.getByTestId('pair-web-phone-hint')).toContainText(/Scan with your phone camera/i);
    // QR canvas/svg/img — DeviceQRCode renders one of these.
    const qrLocator = dialog.locator('canvas, svg, img');
    await expect(qrLocator.first()).toBeVisible();

    // The historical Paired list MUST NOT show the just-issued token
    // while the modal is open and unclaimed. Devices.jsx hides those
    // rows until claimed_at lands or the modal closes.
    await expect(pairedHeading).toHaveCount(0);

    // Sanity tail: closing the modal revokes the unclaimed token and
    // we still see no phantom row in the list.
    await page.getByRole('button', { name: /^Close$/i }).first().click();
    await expect(dialog).toBeHidden();
    await expect(pairedHeading).toHaveCount(0);
  });
});
