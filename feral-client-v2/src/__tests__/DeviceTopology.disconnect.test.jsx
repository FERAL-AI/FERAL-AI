/**
 * DeviceTopology must be able to say "disconnected".
 *
 * Owner complaint: "devices that were connected and then disconnected
 * still show as connected".
 *
 * The literal defect: `DeviceTopology.jsx` rendered the NODE dot as a
 * hardcoded `<StatusDot tone="live" pulse />`. It was defensible while
 * `/api/devices/connected` returned live daemons only, but the brain
 * now also returns `offline[]` (nodes that dropped), so a hardcoded
 * live dot is a straightforward lie. The sub-device dot at the row
 * below it was already bound to `s.live` and stays that way.
 *
 * Second defect, same surface: a peripheral seen through six
 * install-scoped `feral-iphone-*` node ids used to render as six
 * separate things. The brain groups them now, and the component must
 * render the grouped shape (`name`, `also_seen_via`, `last_seen_age_s`)
 * rather than the raw capability id.
 */
import { describe, it, expect } from 'vitest';
import { renderV2 } from './_helpers/renderV2';
import DeviceTopology from '../components/DeviceTopology';

const OFFLINE_PHONE = {
  node_id: 'feral-iphone-6053b3cdc4ed',
  type: 'iphone',
  connected: false,
  status: 'disconnected',
  last_seen: Date.now() / 1000 - 5.4 * 86400,
  last_seen_age_s: 5.4 * 86400,
  also_known_as: [
    'feral-iphone-299994e5', 'feral-iphone-2a210fa1', 'feral-iphone-415d9bef',
    'feral-iphone-79447a4cd1ed', 'feral-iphone-c29f7fd3',
  ],
  reconnect: {
    brain_can_initiate: false,
    why: 'The brain has no way to wake a node that is not already holding a WebSocket.',
    steps: ['Open the FERAL app on feral-iphone-6053b3cdc4ed.'],
  },
  subdevices: [
    {
      capability: 'jw_health_glasses',
      name: 'W300',
      status: 'ready',
      live: false,
      last_seen_age_s: 5.4 * 86400,
      via_node_id: 'feral-iphone-6053b3cdc4ed',
      also_seen_via: ['feral-iphone-2a210fa1', 'feral-iphone-415d9bef'],
      observations: 6,
    },
    {
      capability: 'veepoo_wristband',
      name: 'VITRO',
      status: 'ready',
      live: false,
      last_seen_age_s: 65.4 * 86400,
      via_node_id: 'feral-iphone-2a210fa1',
      also_seen_via: [],
      observations: 1,
    },
  ],
};

const LIVE_PHONE = {
  ...OFFLINE_PHONE,
  connected: true,
  status: 'connected',
  last_seen_age_s: 0,
  reconnect: undefined,
};

function dots(container) {
  return Array.from(container.querySelectorAll('.v2-dot'));
}

describe('DeviceTopology disconnect truth', () => {
  it('renders a disconnected node with an off dot, not a live one', () => {
    const { getAllByTestId, container } = renderV2(
      <DeviceTopology connected={[]} offline={[OFFLINE_PHONE]} />,
    );
    const branches = getAllByTestId('v2-topology-branch');
    expect(branches).toHaveLength(1);
    const nodeDot = branches[0].querySelector('.v2-device-head .v2-dot');
    expect(nodeDot.className).toContain('v2-dot--off');
    expect(nodeDot.className).not.toContain('v2-dot--live');
    expect(nodeDot.className).not.toContain('is-pulse');
    expect(dots(container).some((d) => d.className.includes('v2-dot--live'))).toBe(false);
  });

  it('says the word disconnected and how long ago', () => {
    const { getByTestId } = renderV2(
      <DeviceTopology connected={[]} offline={[OFFLINE_PHONE]} />,
    );
    const meta = getByTestId('v2-topology-node-state');
    expect(meta.textContent.toLowerCase()).toContain('disconnected');
    expect(meta.textContent).toMatch(/5 days ago/);
  });

  it('keeps the live dot for a node that is actually connected', () => {
    const { getAllByTestId } = renderV2(
      <DeviceTopology connected={[LIVE_PHONE]} offline={[]} />,
    );
    const branch = getAllByTestId('v2-topology-branch')[0];
    const nodeDot = branch.querySelector('.v2-device-head .v2-dot');
    expect(nodeDot.className).toContain('v2-dot--live');
  });

  it('nests the peripherals under the phone, never beside it', () => {
    const { getAllByTestId } = renderV2(
      <DeviceTopology connected={[]} offline={[OFFLINE_PHONE]} />,
    );
    // One branch (the phone), two sub chips inside it.
    expect(getAllByTestId('v2-topology-branch')).toHaveLength(1);
    const branch = getAllByTestId('v2-topology-branch')[0];
    expect(branch.querySelectorAll('[data-testid="v2-topology-sub"]')).toHaveLength(2);
  });

  it('shows one pair of glasses, not six, and says how many installs saw it', () => {
    const { getAllByTestId } = renderV2(
      <DeviceTopology connected={[]} offline={[OFFLINE_PHONE]} />,
    );
    const subs = getAllByTestId('v2-topology-sub');
    const glasses = subs.filter((s) => /W300|Glasses/i.test(s.textContent));
    expect(glasses).toHaveLength(1);
    // The name from the truth store beats the generic icon label.
    expect(glasses[0].textContent).toContain('W300');
    expect(glasses[0].getAttribute('title')).toMatch(/6 observation/i);
  });

  it('states that the brain cannot reconnect the device itself', () => {
    const { getByTestId } = renderV2(
      <DeviceTopology connected={[]} offline={[OFFLINE_PHONE]} />,
    );
    const hint = getByTestId('v2-topology-reconnect');
    expect(hint.textContent).toMatch(/Open the FERAL app/i);
    // No button: there is nothing the brain can do on click.
    expect(hint.querySelector('button')).toBeNull();
  });

  it('still renders the brain card and empty state with nothing attached', () => {
    const { getByText } = renderV2(<DeviceTopology connected={[]} offline={[]} />);
    expect(getByText('FERAL Brain')).toBeInTheDocument();
    expect(getByText(/Awaiting node/i)).toBeInTheDocument();
  });
});
