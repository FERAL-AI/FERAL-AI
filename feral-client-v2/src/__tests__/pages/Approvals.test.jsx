/**
 * Approvals: the surface that did not exist.
 *
 * `GET /api/approvals` shipped with approve and reject and this client
 * never called it: grepping `src/` for `api/approvals` returned zero
 * hits. The only approval UI was a live `permission_request` frame in
 * Chat, which renders only if Chat is mounted at that instant, never
 * rehydrates on mount, and is deliberately absent from
 * `CHAT_FRAME_TYPES` so it is not session-filtered.
 *
 * A tool call raised by a cron job, a Discord message or the phone
 * therefore blocked with nothing on screen anywhere.
 */
import React from 'react';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

const apiJson = vi.fn();
const apiFetch = vi.fn();
vi.mock('../../lib/api', () => ({
  apiJson: (...a) => apiJson(...a),
  apiFetch: (...a) => apiFetch(...a),
}));

import Approvals, { originOf, describeCall, waitedFor, explainSources } from '../../pages/Approvals';

const PENDING = {
  count: 2,
  approvals: [
    {
      request_id: 'r1',
      session_id: 'channel_discord_4471',
      tool_name: 'coding_tools__write_file',
      args: { path: '/Users/me/Projects/build.sh' },
      safety_level: 'confirm',
      created_at: Date.now() / 1000 - 45,
      // Verbatim output of
      //   resolve_policy('browser__evaluate', {}, surface='api', registry=<42 builtin skills>)
      // captured by running it, not written from memory. The first
      // version of this fixture was invented and rendered fine while the
      // real payload would have shown "[object Object]" and, before the
      // route fix, nothing at all.
      policy_sources: {
        manifest: { safety_tier: 'confirm', read_only_hint: false, requires_user_approval: true },
        danger_map: 'critical',
        legacy_substring: 'legacy_substring:unknown_default',
      },
    },
    {
      request_id: 'r2',
      session_id: '',
      tool_name: 'coding_tools__bash',
      args: { command: 'rm -rf ~/Downloads/*' },
      safety_level: 'critical',
      created_at: Date.now() / 1000 - 5,
      policy_sources: {},
    },
  ],
};

beforeEach(() => {
  apiJson.mockReset();
  apiFetch.mockReset();
  vi.useFakeTimers({ shouldAdvanceTime: true });
});
afterEach(() => vi.useRealTimers());

describe('origin is derived from the session id', () => {
  it('names the channel a request came from', () => {
    expect(originOf('channel_discord_4471').label).toContain('discord');
  });
  it('recognises phone, voice and cron surfaces', () => {
    expect(originOf('phone-abc').kind).toBe('phone');
    expect(originOf('voice-abc').kind).toBe('voice');
    expect(originOf('cron_morning').kind).toBe('cron');
  });
  it('falls back to this chat rather than inventing a source', () => {
    expect(originOf('').kind).toBe('local');
    expect(originOf('sess-123').kind).toBe('chat');
  });
});

describe('the row says what the call will do', () => {
  it('surfaces the argument that matters', () => {
    expect(describeCall(PENDING.approvals[0])).toContain('build.sh');
    expect(describeCall(PENDING.approvals[1])).toContain('rm -rf');
  });
  it('degrades to the tool name when there is no obvious argument', () => {
    expect(describeCall({ tool_name: 'x__y', args: {} })).toBe('x__y');
  });
  it('never renders a runaway string', () => {
    const long = describeCall({ tool_name: 't', args: { command: 'a'.repeat(9999) } });
    expect(long.length).toBeLessThan(200);
  });
});

describe('waiting time', () => {
  it('reads in seconds, minutes and hours', () => {
    const now = 1_000_000;
    expect(waitedFor(now - 30, now)).toBe('30s');
    expect(waitedFor(now - 120, now)).toBe('2m');
    expect(waitedFor(now - 7200, now)).toBe('2h');
  });
  it('refuses a nonsense timestamp rather than printing a decade', () => {
    expect(waitedFor(0, 1_000_000_000)).toBe('');
  });
});

describe('the page', () => {
  it('lists everything pending across every surface', async () => {
    apiJson.mockResolvedValue(PENDING);
    render(<Approvals />);
    await waitFor(() => {
      expect(screen.getByText(/build\.sh/)).toBeInTheDocument();
      expect(screen.getByText(/rm -rf/)).toBeInTheDocument();
    });
  });

  it('says where each request came from', async () => {
    apiJson.mockResolvedValue(PENDING);
    render(<Approvals />);
    await waitFor(() => expect(screen.getByText(/from discord message/i)).toBeInTheDocument());
  });

  it('shows why the brain is asking', async () => {
    apiJson.mockResolvedValue(PENDING);
    render(<Approvals />);
    await waitFor(() => expect(screen.getByText(/Danger map: critical/)).toBeInTheDocument());
    expect(screen.getByText(/Skill declares: confirm, author asks every time/)).toBeInTheDocument();
  });

  it('never renders a raw object into the reason list', async () => {
    apiJson.mockResolvedValue(PENDING);
    const { container } = render(<Approvals />);
    await waitFor(() => expect(screen.getByText(/Danger map: critical/)).toBeInTheDocument());
    expect(container.textContent).not.toContain('[object Object]');
  });

  it('approves through the real endpoint and removes the row', async () => {
    apiJson.mockResolvedValue(PENDING);
    apiFetch.mockResolvedValue({ ok: true, json: async () => ({}) });
    render(<Approvals />);
    await waitFor(() => expect(screen.getAllByText('Approve')).toHaveLength(2));
    fireEvent.click(screen.getAllByText('Approve')[0]);
    await waitFor(() => {
      expect(apiFetch).toHaveBeenCalledWith('/api/approvals/r1/approve', expect.objectContaining({ method: 'POST' }));
      expect(screen.queryByText(/build\.sh/)).not.toBeInTheDocument();
    });
  });

  it('declines through the reject endpoint', async () => {
    apiJson.mockResolvedValue(PENDING);
    apiFetch.mockResolvedValue({ ok: true, json: async () => ({}) });
    render(<Approvals />);
    await waitFor(() => expect(screen.getAllByText('Decline')).toHaveLength(2));
    fireEvent.click(screen.getAllByText('Decline')[1]);
    await waitFor(() =>
      expect(apiFetch).toHaveBeenCalledWith('/api/approvals/r2/reject', expect.objectContaining({ method: 'POST' })));
  });

  it('keeps the row when the decision fails', async () => {
    apiJson.mockResolvedValue(PENDING);
    apiFetch.mockResolvedValue({ ok: false, status: 409, json: async () => ({ detail: 'already decided' }) });
    render(<Approvals />);
    await waitFor(() => expect(screen.getAllByText('Approve')).toHaveLength(2));
    fireEvent.click(screen.getAllByText('Approve')[0]);
    await waitFor(() => expect(screen.getByText(/already decided/)).toBeInTheDocument());
    expect(screen.getByText(/build\.sh/)).toBeInTheDocument();
  });

  it('says nothing is waiting rather than rendering an empty list', async () => {
    apiJson.mockResolvedValue({ count: 0, approvals: [] });
    render(<Approvals />);
    await waitFor(() => expect(screen.getByText(/Nothing is waiting on you/i)).toBeInTheDocument());
  });

  it('reports an unreachable brain instead of looking empty', async () => {
    apiJson.mockRejectedValue(new Error('connection refused'));
    render(<Approvals />);
    await waitFor(() => expect(screen.getByText(/connection refused/)).toBeInTheDocument());
  });

  it('polls, because an approval can arrive while the page is open', async () => {
    apiJson.mockResolvedValue({ count: 0, approvals: [] });
    render(<Approvals />);

    // Counts calls to /api/approvals specifically, not every fetch.
    // The page also reads /api/autonomy and /api/policy once on mount,
    // to explain an empty queue, and a bare call count conflated those
    // with the poll and failed at 3.
    const approvalCalls = () =>
      apiJson.mock.calls.filter(([url]) => String(url).includes('/api/approvals')).length;

    await waitFor(() => expect(approvalCalls()).toBe(1));
    vi.advanceTimersByTime(4100);
    await waitFor(() => expect(approvalCalls()).toBeGreaterThan(1));
  });

  it('reads the tier and the policy once, not on every poll', async () => {
    // They do not change on their own and this page is often left open.
    apiJson.mockResolvedValue({ count: 0, approvals: [] });
    render(<Approvals />);

    const contextCalls = () => apiJson.mock.calls.filter(
      ([url]) => String(url).includes('/api/autonomy') || String(url).includes('/api/policy'),
    ).length;

    await waitFor(() => expect(contextCalls()).toBe(2));
    vi.advanceTimersByTime(20000);
    expect(contextCalls()).toBe(2);
  });
});


describe('explainSources reads the resolver output the resolver actually emits', () => {
  it('flattens the nested manifest instead of stringifying it', () => {
    const out = explainSources({
      manifest: { safety_tier: 'confirm', read_only_hint: false, requires_user_approval: true },
    });
    expect(out).toEqual(['Skill declares: confirm, author asks every time']);
    expect(out.join(' ')).not.toContain('[object Object]');
  });

  it('marks a read-only endpoint as such', () => {
    expect(explainSources({ manifest: { safety_tier: 'safe', read_only_hint: true } }))
      .toEqual(['Skill declares: safe, read only']);
  });

  it('drops the unknown_default heuristic, which is on nearly every row', () => {
    expect(explainSources({ legacy_substring: 'legacy_substring:unknown_default' })).toEqual([]);
  });

  it('keeps a heuristic that actually matched, without its prefix', () => {
    expect(explainSources({ legacy_substring: 'legacy_substring:delete' }))
      .toEqual(['Name heuristic: delete']);
  });

  it('says nothing about a danger level of safe', () => {
    expect(explainSources({ danger_map: 'safe' })).toEqual([]);
  });

  it('reports a surface denial, which arrives as the only key', () => {
    expect(explainSources({ surface_deny: true })).toEqual(['Blocked for this surface']);
  });

  it('survives the empty and missing cases the route can return', () => {
    expect(explainSources({})).toEqual([]);
    expect(explainSources(undefined)).toEqual([]);
    expect(explainSources(null)).toEqual([]);
  });
});
