/**
 * Timeline page fetch contract.
 *
 * Demo-blocker repro: the /timeline page was rendering forever as
 * "Loading timeline… (0)" because no HTTP request to /api/timeline
 * was ever issued. The brain is healthy and the REST endpoint
 * (api/routes/timeline.py) returns {entries, count, days} — the bug
 * was purely on the client (fetch effect mis-wired). This test pins
 * the contract: mount Timeline → an HTTP GET to /api/timeline fires
 * → returned entries render in the list.
 */
import React from 'react';
import { describe, it, expect, vi } from 'vitest';
import { render, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import Timeline from '../../pages/Timeline';

const SAMPLE_ENTRIES = [
  { id: 'e1', source: 'chat', time: '2026-05-28T16:00:00Z', text: 'discussed feral demo' },
  { id: 'e2', source: 'events', time: '2026-05-28T15:00:00Z', title: 'standup' },
  { id: 'e3', source: 'memories', time: '2026-05-28T14:00:00Z', summary: 'remembered badr likes coffee' },
];

function installTimelineFetchMock() {
  const calls = [];
  const fetchMock = vi.fn((input) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    calls.push(url);
    return Promise.resolve({
      ok: true,
      status: 200,
      statusText: 'OK',
      clone() { return this; },
      json: () => Promise.resolve({ entries: SAMPLE_ENTRIES, count: SAMPLE_ENTRIES.length, days: 7 }),
      text: () => Promise.resolve(JSON.stringify({ entries: SAMPLE_ENTRIES })),
      headers: new Map(),
    });
  });
  vi.stubGlobal('fetch', fetchMock);
  return { fetchMock, calls };
}

describe('Timeline — HTTP fetch on mount', () => {
  it('issues a GET to /api/timeline on mount and renders returned entries', async () => {
    const { calls } = installTimelineFetchMock();
    const { container, getByText, queryByText } = render(
      <MemoryRouter initialEntries={['/timeline']}>
        <Timeline />
      </MemoryRouter>,
    );

    await waitFor(() => {
      const hit = calls.some((u) => /\/api\/timeline\b/.test(u));
      if (!hit) throw new Error(`no /api/timeline call yet: ${JSON.stringify(calls)}`);
    });

    await waitFor(() => {
      expect(queryByText(/Loading timeline/i)).toBeNull();
    });

    expect(getByText(/discussed feral demo/i)).toBeInTheDocument();
    expect(getByText(/standup/i)).toBeInTheDocument();
    expect(getByText(/remembered badr likes coffee/i)).toBeInTheDocument();
    expect(getByText(/Timeline \(3\)/i)).toBeInTheDocument();
    expect(container.querySelectorAll('[data-testid="timeline-list"] li').length).toBe(3);
  });

  it('Refresh re-fires the HTTP GET', async () => {
    const { calls } = installTimelineFetchMock();
    const { container } = render(
      <MemoryRouter initialEntries={['/timeline']}>
        <Timeline />
      </MemoryRouter>,
    );

    await waitFor(() => {
      const hit = calls.some((u) => /\/api\/timeline\b/.test(u));
      if (!hit) throw new Error('no /api/timeline call');
    });

    const initialCount = calls.filter((u) => /\/api\/timeline\b/.test(u)).length;
    const refreshBtn = Array.from(container.querySelectorAll('button')).find(
      (b) => /refresh/i.test(b.textContent || ''),
    );
    expect(refreshBtn).toBeTruthy();
    refreshBtn.click();

    await waitFor(() => {
      const after = calls.filter((u) => /\/api\/timeline\b/.test(u)).length;
      if (after <= initialCount) throw new Error('refresh did not re-fire the GET');
    });
  });
});
