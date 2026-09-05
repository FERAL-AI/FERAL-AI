/**
 * Jobs page: the header has to describe the list under it, and a routine
 * has to say when it next runs rather than how old it is.
 *
 * Both defects were reproduced against the live brain. `GET /api/jobs`
 * returned five rows and the header above them read "0 active", because
 * the count filtered on `status in (running, connected)` while the list
 * rendered every row the brain sent: routines arrive at status
 * "scheduled" and mitosis specialists at "ready", so neither was ever
 * counted. A page whose headline number contradicts the rows directly
 * below it is worse than one with no number.
 *
 * The second defect is on the routine row itself. It rendered
 * `ranFor(started_at)`, and for a routine `started_at` is `created_at`:
 * the row for a routine created 71 days ago read "scheduled · 1722h 29m",
 * which a reader takes as "scheduled 71 days out". The brain already
 * sends `detail.next_run` (api/routes/jobs.py), so the row shows that.
 */
import React from 'react';
import { describe, it, expect, vi, afterEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { renderV2 } from '../_helpers/renderV2';
import Jobs, { nextRunIn, ranFor } from '../../pages/Jobs';

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

// Shaped exactly like the live payload: one running flow, one scheduled
// routine, one ready specialist. Only the first is "running".
const NOW = 1_770_000_000;
const JOBS_PAYLOAD = {
  items: [
    {
      id: 'flow-1',
      kind: 'taskflow',
      name: 'Morning briefing',
      status: 'running',
      started_at: NOW - 90,
      progress: 0.5,
      cancellable_via: 'POST /api/taskflows/flow-1/cancel',
      detail: { step: 2, steps: 4 },
    },
    {
      id: 'routine-11',
      kind: 'routine',
      name: 'nightly at 9pm',
      status: 'scheduled',
      // 71 days ago, which is what produced "1722h 29m" on screen.
      started_at: NOW - 71 * 24 * 3600,
      progress: null,
      cancellable_via: 'DELETE /api/routines/11',
      detail: { cron: '0 21 * * *', next_run: NOW + 4 * 3600 + 36 * 60 },
    },
    {
      id: 'spec-a',
      kind: 'specialist',
      name: 'research worker',
      status: 'ready',
      started_at: NOW - 600,
      progress: null,
      detail: {},
    },
  ],
  counts_by_kind: { taskflow: 1, routine: 1, specialist: 1 },
  degraded: {},
};

function renderJobs(payload = JOBS_PAYLOAD) {
  return renderV2(<Jobs />, {
    fetch: (url) => (url.includes('/api/jobs') ? payload : { items: [] }),
  });
}

describe('the jobs header', () => {
  it('counts every row it lists, not just the running ones', async () => {
    renderJobs();
    // Three rows on screen, one of them running. The old header said
    // "0 active" for exactly this payload once the routine and the
    // specialist were the only rows.
    await waitFor(() => {
      expect(screen.getByText('3 listed · 1 running')).toBeInTheDocument();
    });
    expect(screen.queryByText(/^\d+ active$/)).not.toBeInTheDocument();
  });

  it('says "0 running" without claiming the list is empty', async () => {
    const noneRunning = {
      ...JOBS_PAYLOAD,
      items: JOBS_PAYLOAD.items.filter((i) => i.status !== 'running'),
    };
    renderJobs(noneRunning);
    await waitFor(() => {
      expect(screen.getByText('2 listed · 0 running')).toBeInTheDocument();
    });
  });
});

describe('a routine row', () => {
  it('shows when it next fires, not how long ago it was created', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(NOW * 1000);
    renderJobs();
    await waitFor(() => {
      expect(screen.getByText(/next in 4h 36m/)).toBeInTheDocument();
    });
    // The routine's age must not appear anywhere: 71 days is 1704h.
    expect(screen.queryByText(/17\d\dh/)).not.toBeInTheDocument();
  });

  it('still shows elapsed time for a job that really did start', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(NOW * 1000);
    renderJobs();
    await waitFor(() => {
      expect(screen.getByText(/1m 30s/)).toBeInTheDocument();
    });
  });
});

describe('nextRunIn', () => {
  it('renders hours and minutes until the next fire', () => {
    expect(nextRunIn(NOW + 4 * 3600 + 36 * 60, NOW)).toBe('next in 4h 36m');
  });

  it('renders minutes alone under an hour', () => {
    expect(nextRunIn(NOW + 12 * 60, NOW)).toBe('next in 12m');
  });

  it('says nothing when the brain named no next run', () => {
    // Silence is the correct answer here. Falling back to the row's age
    // is what produced "scheduled · 1722h 29m" in the first place.
    expect(nextRunIn(0, NOW)).toBe('');
    expect(nextRunIn(undefined, NOW)).toBe('');
    expect(nextRunIn(null, NOW)).toBe('');
  });

  it('says a run that is already due is due now', () => {
    expect(nextRunIn(NOW - 5, NOW)).toBe('next due now');
  });

  it('says nothing for a run more than a year out, like ranFor', () => {
    expect(nextRunIn(NOW + 400 * 24 * 3600, NOW)).toBe('');
    expect(ranFor(NOW - 400 * 24 * 3600, NOW)).toBe('');
  });
});
