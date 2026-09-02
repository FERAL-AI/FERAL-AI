/**
 * Devices page — the per-device capability toggles HUP_SPEC.md section 6
 * has always claimed live at Settings > Devices > <device> > Capabilities.
 *
 * Before this: the detail modal rendered the capability list as read-only
 * `<span class="v2-chip">` elements, and nothing in the whole client
 * called any grant endpoint. The brain matched: it answered every
 * `node_register` with `granted_capabilities = <the node's own
 * declaration>` and `denied_capabilities = []`. So the spec named a
 * screen with no control on it, backing a MUST with no code.
 *
 * These tests pin the three things a toggle has to do: render the
 * operator's current answer, post the change, and re-render from the
 * response rather than from optimistic local state.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { fireEvent, waitFor } from '@testing-library/react';
import { renderV2 } from '../_helpers/renderV2';
import Devices from '../../pages/Devices';

afterEach(() => { vi.unstubAllGlobals(); });

const NODE_ID = 'spd-phone-01';

const LIVE = {
  node_id: NODE_ID,
  type: 'phone',
  capabilities: ['camera', 'microphone', 'haptic'],
  platform: 'ios',
  status: 'connected',
  connected: true,
};

function grantRow(capability, tier, granted, explicit = false) {
  return { capability, tier, granted, explicit };
}

const INITIAL_GRANTS = [
  grantRow('camera', 'camera', true),
  grantRow('microphone', 'audio', true),
  grantRow('haptic', 'active_actuator', true),
];

function responder({ grantSpy, grantsAfterPost } = {}) {
  return (url, init) => {
    const method = init?.method || 'GET';
    if (url.includes(`/api/devices/${NODE_ID}/capabilities`)) {
      if (method === 'POST') {
        grantSpy?.(JSON.parse(init.body));
        return { ok: true, node_id: NODE_ID, capabilities: grantsAfterPost };
      }
      return { node_id: NODE_ID, connected: true, capabilities: INITIAL_GRANTS };
    }
    if (url.includes('/api/devices/paired')) return { devices: [] };
    if (url.includes('/api/devices/connected')) return { devices: [LIVE], offline: [] };
    if (url.includes('/api/hardware/mesh')) return { nodes: [] };
    if (url.includes('/api/hardware/device/')) return { error: 'not a mesh node' };
    if (url.includes('/api/dashboard')) return { latest_health: null };
    return {};
  };
}

async function openDetail(container) {
  const card = await waitFor(() => {
    const found = container.querySelector('[data-testid="v2-devices-live-card"]');
    if (!found) throw new Error('no live card');
    return found;
  });
  fireEvent.click(card);
  return waitFor(() => {
    const dialog = document.querySelector('[role="dialog"]');
    if (!dialog) throw new Error('no dialog');
    return dialog;
  });
}

async function grantChip(dialog, capability) {
  return waitFor(() => {
    const el = dialog.querySelector(`[data-testid="v2-device-capability-${capability}"]`);
    if (!el) throw new Error(`no chip for ${capability}`);
    return el;
  });
}

describe('Devices — per-device capability grants', () => {
  it('renders each capability as a control, not a read-only chip', async () => {
    const { container } = renderV2(<Devices />, { fetch: responder() });
    const dialog = await openDetail(container);
    const chip = await grantChip(dialog, 'camera');
    // A `span` is what shipped. The whole defect is that the spec
    // promised a toggle here and the markup could not be clicked.
    expect(chip.tagName).toBe('BUTTON');
    expect(chip.getAttribute('aria-pressed')).toBe('true');
  });

  it('posts {capability, granted:false} when a granted capability is clicked', async () => {
    const grantSpy = vi.fn();
    const { container } = renderV2(<Devices />, {
      fetch: responder({ grantSpy, grantsAfterPost: INITIAL_GRANTS }),
    });
    const dialog = await openDetail(container);
    fireEvent.click(await grantChip(dialog, 'camera'));
    await waitFor(() => expect(grantSpy).toHaveBeenCalled());
    expect(grantSpy).toHaveBeenCalledWith({ capability: 'camera', granted: false });
  });

  it('re-renders from the response, so a refused change does not look applied', async () => {
    // The brain is the authority on what the grant now is. Flipping local
    // state optimistically is how a control reports success while its
    // request failed, which is one of the four defect classes the e2e
    // note in CLAUDE.md calls structurally invisible.
    const { container } = renderV2(<Devices />, {
      fetch: responder({
        grantsAfterPost: [
          grantRow('camera', 'camera', false, true),
          grantRow('microphone', 'audio', true),
          grantRow('haptic', 'active_actuator', true),
        ],
      }),
    });
    const dialog = await openDetail(container);
    fireEvent.click(await grantChip(dialog, 'camera'));
    await waitFor(async () => {
      const chip = await grantChip(dialog, 'camera');
      expect(chip.getAttribute('aria-pressed')).toBe('false');
    });
    // The untouched ones are unchanged.
    expect((await grantChip(dialog, 'haptic')).getAttribute('aria-pressed')).toBe('true');
  });

  it('re-granting a denied capability posts granted:true', async () => {
    const grantSpy = vi.fn();
    const denied = [
      grantRow('camera', 'camera', false, true),
      grantRow('microphone', 'audio', true),
      grantRow('haptic', 'active_actuator', true),
    ];
    const { container } = renderV2(<Devices />, {
      fetch: (url, init) => {
        const method = init?.method || 'GET';
        if (url.includes(`/api/devices/${NODE_ID}/capabilities`)) {
          if (method === 'POST') {
            grantSpy(JSON.parse(init.body));
            return { ok: true, capabilities: INITIAL_GRANTS };
          }
          return { node_id: NODE_ID, connected: true, capabilities: denied };
        }
        if (url.includes('/api/devices/connected')) return { devices: [LIVE], offline: [] };
        if (url.includes('/api/devices/paired')) return { devices: [] };
        if (url.includes('/api/hardware/mesh')) return { nodes: [] };
        if (url.includes('/api/hardware/device/')) return { error: 'not a mesh node' };
        if (url.includes('/api/dashboard')) return { latest_health: null };
        return {};
      },
    });
    const dialog = await openDetail(container);
    fireEvent.click(await grantChip(dialog, 'camera'));
    await waitFor(() => expect(grantSpy).toHaveBeenCalled());
    expect(grantSpy).toHaveBeenCalledWith({ capability: 'camera', granted: true });
  });
});
