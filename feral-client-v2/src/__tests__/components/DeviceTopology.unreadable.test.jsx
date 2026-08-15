/**
 * DeviceTopology: "there are no devices" and "we could not read the
 * device list" are different claims.
 *
 * The component rendered "Awaiting node, pair an iPhone or browser
 * daemon to populate the mesh" for both, because both arrive as an
 * empty `connected` array. Devices.jsx patched around it by
 * suppressing the whole component when its fetch failed, which fixed
 * one caller and left the component itself asserting a fact it had no
 * evidence for at every other mount.
 */
import { describe, it, expect } from 'vitest';
import { renderV2 } from '../_helpers/renderV2';
import DeviceTopology from '../../components/DeviceTopology';

describe('DeviceTopology empty versus unreadable', () => {
  it('says "awaiting node" only when the brain really reported no devices', () => {
    const { queryByTestId, getByTestId } = renderV2(
      <DeviceTopology connected={[]} offline={[]} />,
    );
    expect(getByTestId('v2-topology-empty').textContent).toMatch(/Awaiting node/i);
    expect(queryByTestId('v2-topology-unreadable')).toBeNull();
  });

  it('renders an unreadable warning instead when the caller says the fetch failed', () => {
    const { queryByTestId, getByTestId, container } = renderV2(
      <DeviceTopology
        connected={[]}
        offline={[]}
        unreadable={{ detail: 'Request failed (503)' }}
      />,
    );
    const warn = getByTestId('v2-topology-unreadable');
    expect(warn.textContent).toMatch(/unreadable/i);
    // The brain's own error text reaches the operator.
    expect(warn.textContent).toContain('Request failed (503)');
    // And the affirmative negative is gone.
    expect(queryByTestId('v2-topology-empty')).toBeNull();
    expect(container.textContent).not.toMatch(/Awaiting node/i);
    // A warn dot, never a live one. We know nothing about the mesh.
    expect(warn.querySelector('.v2-dot').className).toContain('v2-dot--warn');
  });

  it('keeps the warning when the read was partial, not just when it was empty', () => {
    const connected = [{ node_id: 'feral-iphone-abc', name: 'iPhone', type: 'iphone' }];
    const { getByTestId, getAllByTestId } = renderV2(
      <DeviceTopology connected={connected} unreadable="offline list unavailable" />,
    );
    expect(getAllByTestId('v2-topology-branch')).toHaveLength(1);
    expect(getByTestId('v2-topology-unreadable').textContent)
      .toContain('offline list unavailable');
  });

  it('falls back to a generic reason when the caller passes a bare truthy flag', () => {
    const { getByTestId } = renderV2(<DeviceTopology connected={[]} unreadable />);
    expect(getByTestId('v2-topology-unreadable').textContent)
      .toMatch(/could not be fetched/i);
  });
});
