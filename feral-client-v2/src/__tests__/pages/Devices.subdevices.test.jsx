/**
 * Devices page — sub-device tree binding (Phase 1).
 *
 * The /api/devices/connected payload carries `subdevices: [...]` per
 * node, populated by the brain's NodeSubdeviceStore. One chip per
 * sub-device is rendered with a dot tone bound to the row's `live`
 * flag — the pre-Phase-1 build had no binding at all.
 *
 * The chips moved from the Live card into the device detail modal when
 * the four panes were unified onto one fixed-size card: one chip per
 * sub-device is exactly the kind of detail that made a card 149px tall
 * and a different size from its neighbours. The `live` binding these
 * tests exist to guard is unchanged, so they open the card first.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { fireEvent, waitFor } from '@testing-library/react';
import { renderV2 } from '../_helpers/renderV2';
import Devices from '../../pages/Devices';

/** Click the Live card and return its detail dialog. */
async function openDeviceDetail(container) {
  const card = await waitFor(() => {
    const el = container.querySelector('[data-testid="v2-devices-live-card"]');
    if (!el) throw new Error('no device card rendered');
    return el;
  });
  fireEvent.click(card);
  return waitFor(() => {
    const dialog = document.querySelector('[role="dialog"]');
    if (!dialog) throw new Error('detail modal did not open');
    return dialog;
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

function fetchWithSubdevices(rows) {
  return (url) => {
    if (url.includes('/api/devices/connected')) {
      return {
        devices: [
          {
            node_id: 'feral-iphone-abc',
            type: 'phone',
            capabilities: ['host_phone'],
            platform: 'iOS',
            manufacturer: 'Apple',
            model: 'iPhone',
            status: 'connected',
            subdevices: rows,
          },
        ],
      };
    }
    if (url.includes('/api/devices/paired')) {
      return { devices: [] };
    }
    if (url.includes('/api/hardware/mesh')) {
      return { nodes: [] };
    }
    return null;
  };
}

describe('Devices — sub-device chips', () => {
  it('renders a live chip for each ready sub-device', async () => {
    const { container } = renderV2(<Devices />, {
      fetch: fetchWithSubdevices([
        {
          node_id: 'feral-iphone-abc',
          capability: 'jw_health_glasses',
          status: 'ready',
          provenance: 'ble',
          attrs: { device_name: 'Theora-1234', rssi: -52 },
          first_seen: 0,
          last_seen: Date.now() / 1000,
          live: true,
          liveness_window_s: 30,
        },
      ]),
    });

    const dialog = await openDeviceDetail(container);
    const chip = await waitFor(() => {
      const el = dialog.querySelector('[data-testid="v2-device-subdevice-chip"]');
      if (!el) throw new Error('subdevice chip not rendered');
      return el;
    });
    expect(chip.textContent).toContain('jw_health_glasses');
    const dot = chip.querySelector('.v2-dot');
    expect(dot).toBeTruthy();
    expect(dot.className).toContain('v2-dot--live');
    expect(dot.className).toContain('is-pulse');
    // Tooltip surfaces the truth fields the user can verify by
    // hovering — capability, provenance, last-seen age, window.
    expect(chip.getAttribute('title')).toContain('provenance: ble');
    expect(chip.getAttribute('title')).toContain('heartbeat window 30 s');
    expect(chip.getAttribute('title')).toContain('device: Theora-1234');
  });

  it('renders a stale chip for a sub-device past its heartbeat window', async () => {
    const longAgo = Date.now() / 1000 - 600;
    const { container } = renderV2(<Devices />, {
      fetch: fetchWithSubdevices([
        {
          node_id: 'feral-iphone-abc',
          capability: 'jw_health_glasses',
          status: 'ready',
          provenance: 'ble',
          attrs: {},
          first_seen: longAgo,
          last_seen: longAgo,
          live: false,
          liveness_window_s: 30,
        },
      ]),
    });

    const dialog = await openDeviceDetail(container);
    const chip = await waitFor(() => {
      const el = dialog.querySelector('[data-testid="v2-device-subdevice-chip"]');
      if (!el) throw new Error('subdevice chip not rendered');
      return el;
    });
    const dot = chip.querySelector('.v2-dot');
    // Truthful: row exists in the store but heartbeat window expired
    // → dot is `off`, no pulse. The status string is preserved so
    // operators can still see why it was "ready" before.
    expect(dot.className).toContain('v2-dot--off');
    expect(dot.className).not.toContain('is-pulse');
  });

  it('omits the sub-device row when the brain reports none', async () => {
    const { container } = renderV2(<Devices />, {
      fetch: fetchWithSubdevices([]),
    });
    // Live pane still renders the connected node, and its detail modal
    // carries no sub-device row.
    const dialog = await openDeviceDetail(container);
    expect(dialog.querySelector('[data-testid="v2-device-subdevice-chip"]')).toBeNull();
    expect(container.querySelector('[data-testid="v2-device-subdevice-chip"]')).toBeNull();
  });
});
