/**
 * Home page audit guards.
 *
 * Each case below pins a defect found by driving the real Home page
 * against a live brain (feral-core on 127.0.0.1:9462, fresh FERAL_HOME,
 * setup completed). Every payload used here is either a verbatim copy of
 * what that brain answered or the exact shape its source constructs.
 *
 * The defects, in the order they are asserted:
 *
 *  1. Clicking a mode tab was silently undone. `refresh` runs on a 15s
 *     interval and applied `/api/ambient/snapshot.suggested_mode`
 *     unconditionally, so the tab the operator picked reverted within
 *     one tick. Measured: click "Briefing" at 23:27 local, wait 18s,
 *     active tab reads "Wind-Down".
 *  2. Briefing mode rendered NOTHING when all four of its sections were
 *     empty, which is every fresh install. Zero DOM output, so the tab
 *     was indistinguishable from a dead button. It also discarded the
 *     `degraded[]` list the brain sends specifically so a failed
 *     section could be told apart from an empty one.
 *  3. `/api/dashboard.health` has no `cognitive_load` key. The Load
 *     tile read one anyway; the real field is `dashboard.somatic
 *     .cognitive_load`.
 *  4. A stale wearable reading (which the brain reports as
 *     `heart_rate_stale` + `heart_rate_fresh: false`, never as
 *     `heart_rate`) rendered as a bare number with a `via <source>`
 *     attribution, exactly like a live one.
 *  5. The Channels row read `info.enabled`, a key `ChannelManager.stats`
 *     does not produce, so a running-but-unconnected channel and a
 *     channel degraded with a recorded reason both rendered as "off",
 *     identical to a channel that was never configured.
 *  6. The Desk "In-flight jobs" row read `j.description`, a key no
 *     `/api/jobs` aggregator source emits, so it dropped the job name
 *     and printed the brain's internal kind instead.
 *  7. `counts_by_kind` always ships all six sources including zeros, so
 *     six ": 0" chips rendered under the "Idle" empty state.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { waitFor, act, fireEvent } from '@testing-library/react';
import { renderV2 } from '../_helpers/renderV2';
import Home, { channelState } from '../../pages/Home';
import { _resetSystemHealthForTesting } from '../../hooks/useSystemHealth';

beforeEach(() => {
  _resetSystemHealthForTesting();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

/** Verbatim from the live brain, trimmed to the keys Home reads. */
function liveDashboard(extra = {}) {
  return {
    devices: [],
    device_count: 0,
    online_count: 0,
    paired_count: 0,
    paired_offline_count: 0,
    subdevices_total: 0,
    subdevices_live: 0,
    subdevices_unavailable: null,
    paired_unavailable: null,
    channels: [],
    session_count: 1,
    health: {},
    skills_count: 42,
    somatic: {},
    boot: {},
    ...extra,
  };
}

function fetchMap(map) {
  return (url) => {
    for (const [frag, body] of Object.entries(map)) {
      if (url.includes(frag)) return body;
    }
    return null;
  };
}

describe('Home: mode tabs survive the poll (defect 1)', () => {
  it('keeps the tab the operator clicked when snapshot suggests another', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { getByRole } = renderV2(<Home />, {
      fetch: fetchMap({
        '/api/dashboard': liveDashboard(),
        // What the live brain answered at 23:25 local.
        '/api/ambient/snapshot': {
          time: '2026-08-21T23:25:26',
          suggested_mode: 'wind_down',
          vitals: {},
          degraded: [],
        },
      }),
    });

    const briefingTab = await waitFor(() => getByRole('button', { name: /Briefing/i }));
    // The snapshot has already steered us to wind_down; now override it.
    await act(async () => { fireEvent.click(briefingTab); });
    expect(briefingTab.getAttribute('aria-pressed')).toBe('true');

    // One full 15s refresh tick.
    await act(async () => { await vi.advanceTimersByTimeAsync(16_000); });

    expect(
      getByRole('button', { name: /Briefing/i }).getAttribute('aria-pressed'),
    ).toBe('true');
  });
});

describe('Home: briefing mode is never blank (defect 2)', () => {
  const emptyBriefing = {
    greeting: 'Good evening',
    sleep: null,
    agenda: [],
    weather: null,
    goals: [],
    vip_emails: [],
    degraded: ['vip_emails:not_implemented'],
  };

  it('renders an empty state instead of nothing when every section is empty', async () => {
    const { getByRole, container } = renderV2(<Home />, {
      fetch: fetchMap({
        '/api/dashboard': liveDashboard(),
        '/api/ambient/briefing': emptyBriefing,
        '/api/ambient/snapshot': { suggested_mode: 'briefing', degraded: [] },
      }),
    });
    const tab = await waitFor(() => getByRole('button', { name: /Briefing/i }));
    await act(async () => { fireEvent.click(tab); });
    await waitFor(() => {
      expect(container.textContent).toContain('Nothing to brief yet');
    });
  });

  it('does not warn about vip_emails, a section this page does not render', async () => {
    const { getByRole, container } = renderV2(<Home />, {
      fetch: fetchMap({
        '/api/dashboard': liveDashboard(),
        '/api/ambient/briefing': emptyBriefing,
        '/api/ambient/snapshot': { suggested_mode: 'briefing', degraded: [] },
      }),
    });
    const tab = await waitFor(() => getByRole('button', { name: /Briefing/i }));
    await act(async () => { fireEvent.click(tab); });
    await waitFor(() => expect(container.textContent).toContain('Nothing to brief yet'));
    expect(container.querySelector('[data-testid="v2-home-briefing-degraded"]')).toBeNull();
  });

  it('names the sections that FAILED so an error is not read as a quiet morning', async () => {
    const { getByRole, container } = renderV2(<Home />, {
      fetch: fetchMap({
        '/api/dashboard': liveDashboard(),
        '/api/ambient/briefing': {
          ...emptyBriefing,
          degraded: ['sleep', 'agenda', 'goals', 'vip_emails:not_implemented'],
        },
        '/api/ambient/snapshot': { suggested_mode: 'briefing', degraded: [] },
      }),
    });
    const tab = await waitFor(() => getByRole('button', { name: /Briefing/i }));
    await act(async () => { fireEvent.click(tab); });
    const chip = await waitFor(() => {
      const el = container.querySelector('[data-testid="v2-home-briefing-degraded"]');
      if (!el) throw new Error('degraded chip missing');
      return el;
    });
    expect(chip.textContent).toContain('sleep');
    expect(chip.textContent).toContain('agenda');
    expect(chip.textContent).toContain('goals');
    expect(chip.textContent).not.toContain('vip_emails');
    expect(container.textContent).toContain('Briefing could not be assembled');
  });
});

describe('Home: a failed calendar read is visible (defect 8)', () => {
  it('says the calendar lookup failed instead of rendering nothing', async () => {
    const { container } = renderV2(<Home />, {
      fetch: fetchMap({
        '/api/dashboard': liveDashboard(),
        // ambient.py:225-229 answers this shape when the lookup RAISED.
        '/api/ambient/next_event': {
          event: null,
          degraded: 'TimeoutError: calendar backend did not respond',
          hint: 'Calendar lookup failed. Check Settings > Integrations.',
        },
      }),
    });
    const el = await waitFor(() => {
      const n = container.querySelector('[data-testid="v2-home-next-event-degraded"]');
      if (!n) throw new Error('degraded next-event line missing');
      return n;
    });
    expect(el.textContent).toContain('Calendar unavailable');
    expect(el.textContent).toContain('TimeoutError');
  });

  it('stays silent when there is simply no calendar connected', async () => {
    const { container } = renderV2(<Home />, {
      fetch: fetchMap({
        '/api/dashboard': liveDashboard(),
        // Verbatim from the live brain with no calendar integration.
        '/api/ambient/next_event': {
          event: null,
          hint: 'Connect Google Calendar via Settings > Integrations',
        },
      }),
    });
    await waitFor(() => expect(container.textContent).toContain('Skills'));
    expect(container.querySelector('[data-testid="v2-home-next-event-degraded"]')).toBeNull();
  });
});

describe('Home: a failed wind-down section is visible (defect 9)', () => {
  it('names the sections that could not be read', async () => {
    const { getByRole, container } = renderV2(<Home />, {
      fetch: fetchMap({
        '/api/dashboard': liveDashboard(),
        '/api/ambient/wind_down': {
          day_recap: { completed_tasks: [], active_durations_s: 0, key_episodes: [] },
          episodes: [],
          sleep_prep: { time_to_bed_min: 1414, hints: ['Plan tomorrow'] },
          journal_prompt: 'What did you learn today?',
          degraded: ['completed_tasks', 'key_episodes'],
        },
      }),
    });
    const tab = await waitFor(() => getByRole('button', { name: /Wind-Down/i }));
    await act(async () => { fireEvent.click(tab); });
    const chip = await waitFor(() => {
      const el = container.querySelector('[data-testid="v2-home-winddown-degraded"]');
      if (!el) throw new Error('wind-down degraded chip missing');
      return el;
    });
    expect(chip.textContent).toContain('completed_tasks');
    expect(chip.textContent).toContain('key_episodes');
  });

  it('shows no chip on a healthy wind-down', async () => {
    const { getByRole, container } = renderV2(<Home />, {
      fetch: fetchMap({
        '/api/dashboard': liveDashboard(),
        // Verbatim from the live brain.
        '/api/ambient/wind_down': {
          day_recap: { completed_tasks: [], active_durations_s: 0, key_episodes: [] },
          episodes: [],
          sleep_prep: { time_to_bed_min: 1414, hints: ['Plan tomorrow'] },
          journal_prompt: 'What did you learn today?',
          degraded: [],
        },
      }),
    });
    const tab = await waitFor(() => getByRole('button', { name: /Wind-Down/i }));
    await act(async () => { fireEvent.click(tab); });
    await waitFor(() => expect(container.textContent).toContain('Journal prompt'));
    expect(container.querySelector('[data-testid="v2-home-winddown-degraded"]')).toBeNull();
  });
});

describe('Home: Load reads a field the brain actually sends (defect 3)', () => {
  it('renders dashboard.somatic.cognitive_load, not dashboard.health.cognitive_load', async () => {
    const { container } = renderV2(<Home />, {
      fetch: fetchMap({
        '/api/dashboard': liveDashboard({
          // The brain never puts cognitive_load on `health`. If the page
          // still reads it there, this decoy wins and the tile says 90%.
          health: { cognitive_load: 0.9 },
          somatic: { cognitive_load: 0.42, heart_rate: 0 },
        }),
      }),
    });
    // Settle first. `somatic` is read straight off the shared store
    // while `dashboard` is mirrored into local state one effect later,
    // so there is a render in between where `dashboard` is still null
    // and the decoy cannot win yet. Waiting on a dashboard-only value
    // (skills_count: 42) pins the assertion to the steady state.
    await waitFor(() => {
      const labels = [...container.querySelectorAll('.v2-stat-label')];
      const skills = labels.find((el) => el.textContent === 'Skills');
      if (!skills || !skills.parentElement.textContent.includes('42')) {
        throw new Error('dashboard not mirrored yet');
      }
    });
    const labels = [...container.querySelectorAll('.v2-stat-label')];
    const load = labels.find((el) => el.textContent === 'Load');
    expect(load).toBeTruthy();
    expect(load.parentElement.textContent).toContain('42%');
    expect(load.parentElement.textContent).not.toContain('90%');
  });
});

describe('Home: stale heart rate is marked stale (defect 4)', () => {
  it('shows the stale value as last known rather than as a live reading', async () => {
    const { container } = renderV2(<Home />, {
      fetch: fetchMap({
        '/api/dashboard': liveDashboard({
          // Exactly what api/routes/dashboard.py:341-348 writes when the
          // only sample available is older than the 120s live window.
          health: {
            heart_rate_stale: 88,
            heart_rate_source: 'veepoo_wristband',
            heart_rate_fresh: false,
          },
          somatic: { heart_rate: 88, cognitive_load: 0 },
        }),
      }),
    });
    const tile = await waitFor(() => {
      const el = container.querySelector('[data-testid="v2-home-hr-stat"]');
      if (!el || !el.textContent.includes('88')) throw new Error('hr not rendered yet');
      return el;
    });
    expect(tile.textContent).toContain('88');
    const sub = container.querySelector('[data-testid="v2-home-hr-sub"]');
    expect(sub).toBeTruthy();
    expect(sub.textContent).toContain('last known');
    expect(sub.textContent).toContain('veepoo_wristband');
  });

  it('does not mark a fresh reading stale', async () => {
    const { container } = renderV2(<Home />, {
      fetch: fetchMap({
        '/api/dashboard': liveDashboard({
          health: {
            heart_rate: 71,
            heart_rate_source: 'w300_glasses',
            heart_rate_fresh: true,
          },
          somatic: { heart_rate: 71, cognitive_load: 0 },
        }),
      }),
    });
    const sub = await waitFor(() => {
      const el = container.querySelector('[data-testid="v2-home-hr-sub"]');
      if (!el) throw new Error('hr sub missing');
      return el;
    });
    expect(sub.textContent).toContain('via w300_glasses');
    expect(sub.textContent).not.toContain('last known');
  });
});

describe('channelState (defect 5)', () => {
  // The row shape below was produced by driving the real
  // `ChannelManager.stats` property (feral-core/channels/base.py:1849)
  // rather than transcribed from the source.
  it('a degraded channel is not reported as off', () => {
    const st = channelState({
      running: true,
      connected: false,
      known_chats: 0,
      degraded: true,
      failure_count: 3,
      degraded_reason: 'auth expired',
      access_configured: false,
      allowed_sender_count: 0,
      allowed_chat_count: 0,
      pairing_window_open: false,
      pending_senders: [],
    });
    expect(st.label).toBe('degraded');
    expect(st.tone).toBe('error');
    expect(st.reason).toBe('auth expired');
  });

  it('a started-but-unconnected channel reads as starting, not off', () => {
    const st = channelState({ running: true, connected: false, degraded: false });
    expect(st.label).toBe('starting');
    expect(st.tone).toBe('warn');
  });

  it('a connected channel reads as connected', () => {
    const st = channelState({ running: true, connected: true, degraded: false });
    expect(st.label).toBe('connected');
    expect(st.tone).toBe('live');
  });

  it('a stopped channel reads as off', () => {
    const st = channelState({ running: false, connected: false, degraded: false });
    expect(st.label).toBe('off');
    expect(st.tone).toBe('off');
  });

  it('never depends on `enabled`, which the brain does not send', () => {
    // If the implementation still keys off `enabled`, this row (the real
    // shape, which has no `enabled`) collapses to "off".
    expect(channelState({ running: true, connected: false }).label).not.toBe('off');
  });
});

describe('Home: Desk in-flight jobs name the job (defect 6)', () => {
  // Verbatim from GET /api/jobs?limit=10 on the live brain after
  // creating one real routine through POST /api/routines.
  const liveJobs = {
    count: 1,
    counts_by_kind: {
      taskflow: 0, routine: 1, specialist: 0,
      tool_genesis: 0, daemon: 0, background_bash: 0,
    },
    items: [{
      id: 'routine-4',
      kind: 'routine',
      name: 'Audit probe routine for Home page',
      status: 'scheduled',
      started_at: 1787380284.5123851,
      progress: null,
      context_session_id: null,
      cancellable_via: 'DELETE /api/routines/4',
      detail: { cron: 'every 5m', next_run: 1787380584.5123851 },
    }],
    degraded: {},
    as_of: 1787380284.526336,
  };

  it('renders the job name and the human kind label', async () => {
    const { getByRole, container } = renderV2(<Home />, {
      fetch: fetchMap({
        '/api/dashboard': liveDashboard(),
        '/api/jobs': liveJobs,
      }),
    });
    const desk = await waitFor(() => getByRole('button', { name: /Desk/i }));
    await act(async () => { fireEvent.click(desk); });

    const grid = await waitFor(() => {
      const el = container.querySelector('.v2-home-grid');
      if (!el || !el.textContent.includes('In-flight')) throw new Error('desk grid missing');
      return el;
    });
    expect(grid.textContent).toContain('Audit probe routine for Home page');
    expect(grid.textContent).toContain('Routine');
    // The raw internal kind must not be what the operator reads.
    expect(grid.textContent).not.toContain('routine ·');
  });

  it('shows no zero-count job chips beside the Idle empty state (defect 7)', async () => {
    const { container } = renderV2(<Home />, {
      fetch: fetchMap({
        '/api/dashboard': liveDashboard(),
        '/api/jobs': {
          count: 0,
          counts_by_kind: {
            taskflow: 0, routine: 0, specialist: 0,
            tool_genesis: 0, daemon: 0, background_bash: 0,
          },
          items: [],
          degraded: {},
        },
      }),
    });
    await waitFor(() => {
      expect(container.textContent).toContain('No active jobs');
    });
    expect(container.textContent).not.toContain('Shell job: 0');
    expect(container.textContent).not.toContain('TaskFlow: 0');
  });
});
