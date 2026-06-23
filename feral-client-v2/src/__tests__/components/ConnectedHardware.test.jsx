/**
 * ConnectedHardware — public-demo "what physical hardware is attached
 * to the brain right now" pane.
 *
 * Pins:
 *   1. Empty state renders the truthful "No hardware connected" copy
 *      when /api/hardware/fleet returns `devices: []`.
 *   2. A registered device with `last_verified.verified === true`
 *      renders the green "verified ✓" chip.
 *   3. `verified === false` renders the red "verified ✗" chip.
 *   4. `verified === null` renders the neutral "unverified" chip.
 *   5. `last_verified === null` omits the chip entirely — the surface
 *      MUST NOT invent a status when the honesty loop has not run.
 *   6. A failed fetch surfaces an error chip but does NOT crash the
 *      component (graceful degrade — Home stays alive).
 */
import { describe, it, expect, afterEach, vi } from 'vitest';
import { renderV2 } from '../_helpers/renderV2';
import ConnectedHardware from '../../components/ConnectedHardware';

afterEach(() => {
  vi.unstubAllGlobals();
});

function fleet(devices) {
  return {
    devices,
    verifications: {},
    mesh: { nodes: [], announced_devices: [] },
    stats: {},
  };
}

function fleetFetch(devices) {
  return (url) => {
    if (url.includes('/api/hardware/fleet')) return fleet(devices);
    return null; // fall through to DEFAULT_FETCH_BODY
  };
}

describe('ConnectedHardware', () => {
  it('renders the empty state when the brain reports no hardware', async () => {
    const { findByText, queryByTestId } = renderV2(<ConnectedHardware />, {
      fetch: fleetFetch([]),
    });
    expect(await findByText(/No hardware connected/i)).toBeInTheDocument();
    expect(queryByTestId('v2-home-connected-hardware')).toBeNull();
  });

  it('renders a card per device with a verified-ok chip when last_verified.verified === true', async () => {
    const devices = [
      {
        device_id: 'cutebot-01',
        name: 'CuteBot',
        device_type: 'robot',
        manufacturer: 'ELECFREAKS',
        model: 'CuteBot v2',
        connection_type: 'ble',
        location: 'desk',
        battery_powered: true,
        sensors: [],
        actuators: [],
        capabilities: [
          { id: 'move_forward', name: 'move forward' },
          { id: 'set_lights', name: 'set lights' },
        ],
        last_verified: {
          capability: 'set_lights',
          success: true,
          verified: true,
          observed: { rgb: [255, 0, 0] },
          expected: [{ rgb: [255, 0, 0] }],
          error: null,
        },
      },
    ];
    const { findByTestId, getByText, container } = renderV2(<ConnectedHardware />, {
      fetch: fleetFetch(devices),
    });
    const card = await findByTestId('v2-home-hardware-card');
    expect(getByText('CuteBot')).toBeInTheDocument();
    // Scope the device-type / connection-type assertions to the card —
    // the pane copy mentions "robots" too, so a top-level /robot/
    // match would be ambiguous.
    const meta = card.querySelector('.v2-device-meta');
    expect(meta).toBeTruthy();
    expect(meta.textContent).toContain('robot');
    expect(meta.textContent).toContain('ble');
    expect(getByText('2 capabilities')).toBeInTheDocument();

    const okChip = await findByTestId('v2-home-hardware-verified-ok');
    expect(okChip).toBeInTheDocument();
    const dot = okChip.querySelector('.v2-dot');
    expect(dot.className).toContain('v2-dot--live');
    expect(dot.className).toContain('is-pulse');
    // Pin the singular vs plural copy on a 1-capability device too.
    expect(container.textContent).not.toContain('2 capabilitys');
  });

  it('renders a failed-verify chip when verified === false', async () => {
    const devices = [
      {
        device_id: 'cutebot-02',
        name: 'CuteBot-Alt',
        device_type: 'robot',
        connection_type: 'serial',
        capabilities: [{ id: 'move_forward' }],
        last_verified: {
          capability: 'move_forward',
          success: false,
          verified: false,
          observed: null,
          expected: [{ moved: true }],
          error: 'no encoder feedback',
        },
      },
    ];
    const { findByTestId } = renderV2(<ConnectedHardware />, {
      fetch: fleetFetch(devices),
    });
    const chip = await findByTestId('v2-home-hardware-verified-fail');
    expect(chip).toBeInTheDocument();
    const dot = chip.querySelector('.v2-dot');
    expect(dot.className).toContain('v2-dot--error');
  });

  it('renders an unverified chip when verified === null', async () => {
    const devices = [
      {
        device_id: 'sensor-03',
        name: 'SoilProbe',
        device_type: 'sensor',
        connection_type: 'i2c',
        capabilities: [{ id: 'read_moisture' }],
        last_verified: {
          capability: 'read_moisture',
          success: true,
          verified: null,
          observed: null,
          expected: [],
          error: null,
        },
      },
    ];
    const { findByTestId } = renderV2(<ConnectedHardware />, {
      fetch: fleetFetch(devices),
    });
    const chip = await findByTestId('v2-home-hardware-verified-unknown');
    expect(chip).toBeInTheDocument();
    const dot = chip.querySelector('.v2-dot');
    expect(dot.className).toContain('v2-dot--neutral');
  });

  it('omits the verify chip entirely when last_verified is null', async () => {
    const devices = [
      {
        device_id: 'fresh-04',
        name: 'FreshPair',
        device_type: 'robot',
        connection_type: 'ble',
        capabilities: [],
        last_verified: null,
      },
    ];
    const { findByText, queryByTestId } = renderV2(<ConnectedHardware />, {
      fetch: fleetFetch(devices),
    });
    expect(await findByText('FreshPair')).toBeInTheDocument();
    // No chip of any kind — the honesty loop has not reported, so we
    // refuse to render a status string.
    expect(queryByTestId('v2-home-hardware-verified-ok')).toBeNull();
    expect(queryByTestId('v2-home-hardware-verified-fail')).toBeNull();
    expect(queryByTestId('v2-home-hardware-verified-unknown')).toBeNull();
  });

  it('degrades gracefully when /api/hardware/fleet fails (no crash)', async () => {
    // Throwing from the responder makes the stubbed `fetch` reject
    // synchronously, which apiFetch catches and re-raises as an
    // ApiError (see lib/api.js — "networkErr" branch). The contract
    // we're pinning: ConnectedHardware surfaces an error chip instead
    // of throwing into Home and tearing down the whole page.
    const { findByTestId } = renderV2(<ConnectedHardware />, {
      fetch: (url) => {
        if (url.includes('/api/hardware/fleet')) {
          throw new Error('hardware registry offline');
        }
        return null;
      },
    });
    const errChip = await findByTestId('v2-home-hardware-error');
    expect(errChip).toBeInTheDocument();
    expect(errChip.textContent.toLowerCase()).toMatch(/hardware|offline|failed/);
  });
});
