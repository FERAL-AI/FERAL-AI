/**
 * Devices page — one card shape, detail in the modal, working controls.
 *
 * Measured against the live brain before the change, on a 1440px
 * viewport with three live daemons, two claimed pairings and three mesh
 * nodes: a Live card was 343x149, a Paired card 520x162 and a HUP mesh
 * card 343x87. Three panes, three sizes, and the Live card printed the
 * node type, manufacturer, model, up to five capability chips, a
 * "Haptic: unwired" chip and one chip per sub-device. After: every card
 * is 255x68.
 *
 * Size is a CSS fact and jsdom computes no layout, so these tests pin
 * the two things that produced the size difference and that a
 * regression would have to reintroduce: the per-pane content, and the
 * per-card action button. The card markup is pinned structurally
 * (dot + name + one meta line and nothing else), and the detail is
 * asserted to be reachable in the modal instead.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { fireEvent, waitFor } from '@testing-library/react';
import { renderV2 } from '../_helpers/renderV2';
import Devices from '../../pages/Devices';

afterEach(() => { vi.unstubAllGlobals(); });

const LIVE = {
  node_id: 'spd-phone-01',
  type: 'phone',
  capabilities: ['camera', 'microphone', 'gps', 'haptic', 'screen', 'accelerometer', 'notify'],
  platform: 'ios',
  manufacturer: 'Apple',
  model: 'iPhone 16 Pro',
  status: 'connected',
  connected: true,
  subdevices: [{
    node_id: 'spd-phone-01',
    capability: 'jw_health_glasses',
    status: 'ready',
    provenance: 'ble',
    attrs: { device_name: 'Theora-1234' },
    last_seen: Date.now() / 1000,
    live: true,
    liveness_window_s: 30,
  }],
};

const PAIRED = {
  device_id: 'pair-1',
  name: 'Kitchen bridge',
  label: 'Kitchen bridge',
  kind: 'hup',
  claimed_at: 1714000000,
  last_seen: 1714000000,
  capabilities: ['speaker', 'microphone', 'display'],
  is_device: true,
};

const MESH = { node_id: 'spd-phone-01', node_type: 'phone', platform: 'ios', online: true };

function responder({ unclaimed = [], hardwareDevice, invokeSpy } = {}) {
  return (url, init) => {
    if ((init?.method || 'GET') === 'POST' && url.includes('/api/hardware/invoke')) {
      invokeSpy?.(JSON.parse(init.body));
      return { success: true, result: { ok: 1 } };
    }
    if (url.includes('/api/devices/paired?include_unclaimed=true')) return { devices: [PAIRED, ...unclaimed] };
    if (url.includes('/api/devices/paired')) return { devices: [PAIRED] };
    if (url.includes('/api/devices/connected')) return { devices: [LIVE], offline: [] };
    if (url.includes('/api/hardware/mesh')) return { nodes: [MESH] };
    if (url.includes('/api/hardware/device/')) return hardwareDevice ?? { error: 'Device not found: x' };
    if (url.includes('/api/dashboard')) return { latest_health: null };
    return {};
  };
}

async function cards(container, testid) {
  return waitFor(() => {
    const found = container.querySelectorAll(`[data-testid="${testid}"]`);
    if (found.length === 0) throw new Error(`no ${testid} rendered`);
    return found;
  });
}

describe('Devices — every card is the same card', () => {
  it('renders one .v2-device-card per row in every pane', async () => {
    const { container } = renderV2(<Devices />, { fetch: responder() });
    await cards(container, 'v2-devices-live-card');
    await cards(container, 'v2-devices-paired-card');
    await cards(container, 'v2-devices-mesh-card');
    const all = container.querySelectorAll('.v2-device-card');
    expect(all.length).toBe(3);
    for (const card of all) {
      expect(card.className).toContain('v2-device-card');
      // Same padding class in every pane: a card that pads differently
      // is a card that measures differently.
      expect(card.className).toContain('v2-glass--pad-sm');
    }
  });

  it('carries only a dot, a name and one meta line — no chips, no buttons', async () => {
    const { container } = renderV2(<Devices />, { fetch: responder() });
    await cards(container, 'v2-devices-live-card');
    for (const card of container.querySelectorAll('.v2-device-card')) {
      expect(card.querySelectorAll('.v2-device-name').length).toBe(1);
      expect(card.querySelectorAll('.v2-device-meta').length).toBe(1);
      // The capability chip block and the per-card Revoke button are
      // what made the Live card 149px and the Paired card 162px tall.
      expect(card.querySelector('.v2-device-caps')).toBeNull();
      expect(card.querySelector('button')).toBeNull();
      expect(card.querySelector('ol')).toBeNull();
    }
  });

  it('does not print capability names on the card', async () => {
    const { container } = renderV2(<Devices />, { fetch: responder() });
    const live = (await cards(container, 'v2-devices-live-card'))[0];
    expect(live.textContent).not.toContain('microphone');
    expect(live.textContent).not.toContain('iPhone 16 Pro');
    // The count is the summary that replaced them.
    expect(live.textContent).toContain('7 capabilities');
  });
});

describe('Devices — the detail lives in the modal', () => {
  it('opens on click and shows capabilities and sub-devices', async () => {
    const { container } = renderV2(<Devices />, { fetch: responder() });
    const live = (await cards(container, 'v2-devices-live-card'))[0];
    fireEvent.click(live);
    const dialog = await waitFor(() => {
      const d = document.querySelector('[role="dialog"]');
      if (!d) throw new Error('no dialog');
      return d;
    });
    expect(dialog.textContent).toContain('microphone');
    expect(dialog.textContent).toContain('iPhone 16 Pro');
    await waitFor(() => {
      expect(dialog.querySelector('[data-testid="v2-device-subdevice-chip"]')).toBeTruthy();
    });
    const chip = dialog.querySelector('[data-testid="v2-device-subdevice-chip"]');
    expect(chip.querySelector('.v2-dot').className).toContain('v2-dot--live');
    expect(chip.getAttribute('title')).toContain('provenance: ble');
  });

  it('opens the paired row detail and offers Revoke there', async () => {
    const { container } = renderV2(<Devices />, { fetch: responder() });
    const paired = (await cards(container, 'v2-devices-paired-card'))[0];
    fireEvent.click(paired);
    const dialog = await waitFor(() => {
      const d = document.querySelector('[role="dialog"]');
      if (!d) throw new Error('no dialog');
      return d;
    });
    expect(dialog.textContent).toContain('speaker');
    expect(dialog.querySelector('[data-testid="v2-devices-forget"]').textContent)
      .toContain('Revoke pairing');
  });

  it('a 200 {error} from /api/hardware/device does not blank the device', async () => {
    // The brain used to answer 200 with an {"error": ...} body for an id
    // that is not a mesh node, and the modal set that object AS its
    // device: Type and Capabilities went blank with no explanation.
    const { container } = renderV2(<Devices />, {
      fetch: responder({ hardwareDevice: { error: 'Device not found: pair-1' } }),
    });
    const paired = (await cards(container, 'v2-devices-paired-card'))[0];
    fireEvent.click(paired);
    const dialog = await waitFor(() => {
      const d = document.querySelector('[role="dialog"]');
      if (!d) throw new Error('no dialog');
      return d;
    });
    await waitFor(() => {
      expect(dialog.querySelector('[data-testid="v2-devices-detail-note"]')).toBeTruthy();
    });
    expect(dialog.textContent).toContain('speaker');
    expect(dialog.textContent).toContain('hup');
  });

  it('renders capability manifests by name, not [object Object]', async () => {
    const { container } = renderV2(<Devices />, {
      fetch: responder({
        hardwareDevice: {
          device_id: 'spd-phone-01',
          device_type: 'phone',
          capabilities: [
            { id: 'camera_snap', name: 'Camera', category: 'sensor' },
            { id: 'gps_location', name: 'GPS', category: 'sensor' },
          ],
        },
      }),
    });
    const live = (await cards(container, 'v2-devices-live-card'))[0];
    fireEvent.click(live);
    const dialog = await waitFor(() => {
      const d = document.querySelector('[role="dialog"]');
      if (!d) throw new Error('no dialog');
      return d;
    });
    await waitFor(() => expect(dialog.textContent).toContain('camera_snap'));
    expect(dialog.textContent).not.toContain('[object Object]');
  });
});

describe('Devices — controls that used to be dead', () => {
  it('posts the canonical {node_id, command, params} invoke body', async () => {
    // It posted {device_id, method, args}. All three keys missed, so the
    // brain invoked node_id="" and answered "Node not connected: ".
    const invokeSpy = vi.fn();
    const { container } = renderV2(<Devices />, { fetch: responder({ invokeSpy }) });
    const live = (await cards(container, 'v2-devices-live-card'))[0];
    fireEvent.click(live);
    const dialog = await waitFor(() => {
      const d = document.querySelector('[role="dialog"]');
      if (!d) throw new Error('no dialog');
      return d;
    });
    fireEvent.change(dialog.querySelector('input.v2-input'), { target: { value: 'gps_location' } });
    fireEvent.change(dialog.querySelector('textarea'), { target: { value: '{"accuracy":"high"}' } });
    fireEvent.submit(dialog.querySelector('form'));
    await waitFor(() => expect(invokeSpy).toHaveBeenCalled());
    expect(invokeSpy.mock.calls[0][0]).toEqual({
      node_id: 'spd-phone-01',
      command: 'gps_location',
      params: { accuracy: 'high' },
    });
  });

  it('arms "Clear unclaimed" from the endpoint that returns unclaimed rows', async () => {
    // `/api/devices/paired` filters unclaimed rows out by default, so
    // counting them in that list could only ever produce zero and the
    // button was permanently disabled.
    const { container, getByTestId } = renderV2(<Devices />, {
      fetch: responder({
        unclaimed: [{ device_id: 'tok-1', kind: 'pending', claimed_at: null, last_seen: null }],
      }),
    });
    await cards(container, 'v2-devices-paired-card');
    await waitFor(() => {
      const btn = getByTestId('v2-devices-clear-unclaimed');
      expect(btn.disabled).toBe(false);
      expect(btn.textContent).toContain('(1)');
    });
  });

  it('leaves "Clear unclaimed" disabled when there is nothing to prune', async () => {
    const { container, getByTestId } = renderV2(<Devices />, { fetch: responder() });
    await cards(container, 'v2-devices-paired-card');
    await waitFor(() => {
      expect(getByTestId('v2-devices-clear-unclaimed').disabled).toBe(true);
    });
  });
});
