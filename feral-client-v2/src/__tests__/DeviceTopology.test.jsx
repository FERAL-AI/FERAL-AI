/**
 * DeviceTopology — pin the demo-pretty mesh visualization on the
 * Devices page (2026-06-05 demo prep).
 *
 * Guards:
 *   1. The brain card always renders (even when there are zero
 *      connected nodes), so the demo has a baseline visual.
 *   2. When the brain reports connected nodes with sub-devices, each
 *      sub-device chip shows up under its parent node and inherits
 *      a `live`/`stale` status dot from the truth-store row.
 *   3. Fresh HR + SpO2 from `/api/dashboard.latest_health` render
 *      as accent chips in the pane header. Stale samples don't.
 */
import { describe, it, expect } from 'vitest';
import { renderV2 } from './_helpers/renderV2';
import DeviceTopology from '../components/DeviceTopology';

describe('DeviceTopology', () => {
  it('renders the brain card even with no connected nodes', () => {
    const { getByText } = renderV2(<DeviceTopology connected={[]} />);
    expect(getByText('FERAL Brain')).toBeInTheDocument();
    expect(getByText(/Awaiting node/i)).toBeInTheDocument();
  });

  it('renders a connected phone node with its sub-device chips', () => {
    const connected = [{
      node_id: 'feral-iphone-abc',
      name: 'iPhone',
      type: 'iphone',
      subdevices: [
        { capability: 'jw_health_glasses', status: 'ready', live: true, last_seen: Date.now() / 1000 },
        { capability: 'veepoo_wristband', status: 'ready', live: false, last_seen: 0 },
      ],
    }];
    const { getByText, getAllByText, getAllByTestId } = renderV2(
      <DeviceTopology connected={connected} />,
    );
    expect(getByText('FERAL Brain')).toBeInTheDocument();
    // The phone label appears in both the StatusDot's aria-label and
    // the h3 device-name; assertion just needs at least one occurrence.
    expect(getAllByText('iPhone').length).toBeGreaterThan(0);
    expect(getAllByTestId('v2-topology-sub')).toHaveLength(2);
  });

  it('renders fresh HR + SpO2 badges in the pane header', () => {
    const connected = [{
      node_id: 'feral-iphone-abc',
      name: 'iPhone',
      type: 'iphone',
      subdevices: [],
    }];
    const latestHealth = {
      heart_rate: 73,
      heart_rate_fresh: true,
      heart_rate_source: 'veepoo_wristband',
      spo2: 97,
      spo2_fresh: true,
      spo2_source: 'jw_health_glasses',
    };
    const { getByText, getByTestId } = renderV2(
      <DeviceTopology connected={connected} latestHealth={latestHealth} />,
    );
    expect(getByTestId('v2-topology-badge-hr')).toBeInTheDocument();
    expect(getByTestId('v2-topology-badge-spo2')).toBeInTheDocument();
    expect(getByText(/73 bpm/)).toBeInTheDocument();
    expect(getByText(/97%/)).toBeInTheDocument();
  });

  it('does NOT render a stale HR badge', () => {
    const connected = [{ node_id: 'n', name: 'iPhone', type: 'iphone', subdevices: [] }];
    const latestHealth = {
      heart_rate_stale: 115,
      heart_rate_source: 'apple_healthkit',
      heart_rate_fresh: false,
    };
    const { queryByTestId } = renderV2(
      <DeviceTopology connected={connected} latestHealth={latestHealth} />,
    );
    expect(queryByTestId('v2-topology-badge-hr')).toBeNull();
  });
});
