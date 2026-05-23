// Lane 12 Wave 3 — useSystemHealth single-store dedup test.
//
// Pins the contract that AUDIT-r14 finding 03 surfaced: every
// dashboard consumer (Home, GlassBrain, Shell ambient) used to mount
// its own /api/dashboard poll. The new store guarantees one shared
// in-flight fetch even when many components subscribe at once.

import { renderHook, act, waitFor } from '@testing-library/react';
import { describe, it, expect, beforeEach, vi } from 'vitest';

import {
  useSystemHealth,
  useSomaticHealth,
  refreshSystemHealth,
  _resetSystemHealthForTesting,
} from '../../hooks/useSystemHealth';

const apiJsonMock = vi.fn();

vi.mock('../../lib/api', () => ({
  apiJson: (...args) => apiJsonMock(...args),
}));

describe('useSystemHealth', () => {
  beforeEach(() => {
    _resetSystemHealthForTesting();
    apiJsonMock.mockReset();
  });

  it('exposes the dashboard snapshot to subscribers', async () => {
    apiJsonMock.mockResolvedValue({
      session_count: 3,
      device_count: 2,
      somatic: { cognitive_load: 0.55, heart_rate: 72 },
    });
    const { result } = renderHook(() => useSystemHealth());
    await waitFor(() => expect(result.current.data).not.toBeNull());
    expect(result.current.data.session_count).toBe(3);
    expect(result.current.data.somatic.heart_rate).toBe(72);
  });

  it('coalesces parallel subscribers into a single in-flight request', async () => {
    apiJsonMock.mockResolvedValue({ session_count: 1 });
    renderHook(() => useSystemHealth());
    renderHook(() => useSystemHealth());
    renderHook(() => useSomaticHealth());
    await waitFor(() => expect(apiJsonMock).toHaveBeenCalled());
    // Three subscribers should ride a single fetch on first mount.
    expect(apiJsonMock).toHaveBeenCalledTimes(1);
  });

  it('useSomaticHealth derives orb mode from cognitive load', async () => {
    apiJsonMock.mockResolvedValue({ somatic: { cognitive_load: 0.8, heart_rate: 90 } });
    const { result } = renderHook(() => useSomaticHealth());
    await waitFor(() => expect(result.current.heartRate).toBe(90));
    expect(result.current.orbMode).toBe('alerting');
  });

  it('refreshSystemHealth bypasses inflight cache on demand', async () => {
    apiJsonMock.mockResolvedValue({ session_count: 7 });
    renderHook(() => useSystemHealth());
    await waitFor(() => expect(apiJsonMock).toHaveBeenCalled());
    await act(async () => { await refreshSystemHealth(); });
    expect(apiJsonMock.mock.calls.length).toBeGreaterThanOrEqual(2);
  });
});
