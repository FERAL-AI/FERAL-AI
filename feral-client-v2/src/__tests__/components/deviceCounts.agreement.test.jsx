/**
 * One `/api/dashboard` payload, three surfaces, one answer.
 *
 * Home read `online_count`/`paired_count`, GlassBrain read the legacy
 * `device_count` under a bare "Devices" label, and CommandPalette had a
 * third fallback chain again. On the payload below (nothing online,
 * three devices paired) Home said "0/3" and GlassBrain said "0". The
 * same brain, two clicks apart, two different device counts.
 *
 * The derivation now lives in one place (`deviceCounts`, exported from
 * components/DeviceTopology.jsx) and is verified against what
 * feral-core/api/routes/dashboard.py actually returns: `device_count`
 * and `online_count` are both `len(state.daemons)`, so neither is a
 * device total; the total is `online_count + paired_offline_count`,
 * which is the length of the `devices[]` array the same handler
 * builds.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { waitFor } from '@testing-library/react';
import { renderV2 } from '../_helpers/renderV2';
import Home from '../../pages/Home';
import GlassBrain from '../../pages/GlassBrain';
import CommandPalette from '../../shell/CommandPalette';
import { deviceCounts } from '../../components/DeviceTopology';
import { _resetSystemHealthForTesting } from '../../hooks/useSystemHealth';
import { _resetSharedSocketForTesting } from '../../hooks/useFeralSocket';

const PAYLOAD = {
  devices: [],
  // The legacy field. Live-only, and the reason GlassBrain disagreed.
  device_count: 0,
  online_count: 0,
  paired_count: 3,
  paired_offline_count: 3,
  subdevices_total: 0,
  subdevices_live: 0,
  session_count: 0,
  health: {},
  memory: {},
  skills_count: 0,
  somatic: {},
};

const fetchDashboard = (url) => (url.includes('/api/dashboard') ? PAYLOAD : null);

beforeEach(() => {
  _resetSystemHealthForTesting();
  _resetSharedSocketForTesting();
});

afterEach(() => {
  _resetSharedSocketForTesting();
  vi.unstubAllGlobals();
});

function glassBrainVital(container, label) {
  const tiles = [...container.querySelectorAll('.v2-glass-brain-vital')];
  const tile = tiles.find(
    (t) => t.querySelector('.v2-glass-brain-vital-label')?.textContent === label,
  );
  return tile?.querySelector('.v2-glass-brain-vital-value')?.textContent ?? null;
}

describe('device counts agree across Home, GlassBrain and CommandPalette', () => {
  it('derives online / total / offline from the fields the brain actually sends', () => {
    expect(deviceCounts(PAYLOAD)).toMatchObject({
      online: 0, offline: 3, total: 3,
    });
    // A daemon holding a socket without a pairing row must not be lost:
    // paired_count alone would say 3, the honest total is 4.
    expect(deviceCounts({
      online_count: 1, paired_count: 3, paired_offline_count: 3,
    })).toMatchObject({ online: 1, offline: 3, total: 4 });
    // No payload is not zero devices.
    expect(deviceCounts(null)).toMatchObject({ online: null, total: null, known: false });
  });

  it('Home shows 0/3', async () => {
    const { container, unmount } = renderV2(<Home />, { fetch: fetchDashboard });
    await waitFor(() => {
      // Located by the visible label, not by a test id, so the
      // assertion is about what ships to the user.
      const label = [...container.querySelectorAll('.v2-stat-label')]
        .find((el) => el.textContent === 'Devices');
      expect(label?.parentElement?.querySelector('.v2-stat-value')?.textContent)
        .toContain('0/3');
    });
    unmount();
  });

  it('GlassBrain shows 0/3 for the same payload, not the legacy device_count of 0', async () => {
    const { container, unmount } = renderV2(<GlassBrain />, { fetch: fetchDashboard });
    await waitFor(() => {
      expect(glassBrainVital(container, 'Devices')).toBe('0/3');
    });
    unmount();
  });

  it('CommandPalette counts the same three paired devices', async () => {
    const { container, unmount } = renderV2(
      <CommandPalette open onClose={() => {}} />,
      { fetch: fetchDashboard },
    );
    await waitFor(() => {
      expect(container.querySelector('.v2-cmdk-cta-title')?.textContent)
        .toMatch(/^3 devices paired/);
    });
    // The "you have nothing paired" CTA must not fire when three
    // devices are paired and merely offline.
    expect(container.textContent).not.toMatch(/No devices paired yet/);
    unmount();
  });
});
