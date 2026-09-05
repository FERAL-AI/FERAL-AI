// The vitals poller has to carry the brain's ledger figures through.
//
// `/api/dashboard`.budget now comes from `CostBudget`, the object that both
// bills and refuses calls, and carries `hour_spend_usd` / `hour_cap_usd`
// alongside the daily pair. Before that it came from a settings key nothing
// writes, so the bar reported $0.00 on an install spending $9.99 an hour.
//
// The distinction that matters here is absent vs zero. An older brain sends
// no hourly fields at all and must read as "unknown", not as "$0.00 spent".

import { renderHook, waitFor } from '@testing-library/react';
import { describe, it, expect, beforeEach, vi } from 'vitest';

import { useMachineVitals, __resetVitals } from '../../hooks/useMachineVitals';

const apiJsonMock = vi.fn();

vi.mock('../../lib/api', () => ({
  apiJson: (...args) => apiJsonMock(...args),
}));

function dashboard(budget) {
  return (path) => {
    if (path.startsWith('/api/dashboard')) return Promise.resolve({ budget });
    return Promise.resolve({});
  };
}

describe('useMachineVitals cost fields', () => {
  beforeEach(() => {
    __resetVitals();
    apiJsonMock.mockReset();
  });

  it('carries the ledger hour figures through', async () => {
    apiJsonMock.mockImplementation(dashboard({
      enabled: true,
      source: 'cost_budget',
      hour_spend_usd: 9.992715,
      hour_cap_usd: 10,
      daily_spend_usd: 24.5,
      daily_budget_usd: 0,
    }));
    const { result } = renderHook(() => useMachineVitals());
    await waitFor(() => expect(result.current.hourKnown).toBe(true));
    expect(result.current.hourCost).toBeCloseTo(9.992715);
    expect(result.current.hourCap).toBe(10);
    expect(result.current.cost).toBe(24.5);
    expect(result.current.budgetOn).toBe(true);
  });

  it('reads a brain with no hourly fields as unknown, not as zero spend', async () => {
    apiJsonMock.mockImplementation(dashboard({
      enabled: false,
      daily_spend_usd: 1.84,
      daily_budget_usd: 0,
    }));
    const { result } = renderHook(() => useMachineVitals());
    await waitFor(() => expect(result.current.costKnown).toBe(true));
    expect(result.current.hourKnown).toBe(false);
    expect(result.current.cost).toBe(1.84);
  });

  it('treats a reported zero as a reading', async () => {
    apiJsonMock.mockImplementation(dashboard({
      enabled: true, hour_spend_usd: 0, hour_cap_usd: 10,
    }));
    const { result } = renderHook(() => useMachineVitals());
    await waitFor(() => expect(result.current.hourKnown).toBe(true));
    expect(result.current.hourCost).toBe(0);
    expect(result.current.hourCap).toBe(10);
  });
});
