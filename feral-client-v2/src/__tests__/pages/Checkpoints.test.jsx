/**
 * Checkpoints: undo a turn.
 *
 * Every fixture here is a real response, captured by driving
 * `CheckpointStore` against a temp FERAL_HOME: write a pre-existing file
 * and create a new one in one turn, edit one of them afterwards to
 * produce drift, then call plan_revert / revert_turn / revert_turn(force).
 * Nothing below was written from the route docstring.
 */
import React from 'react';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('../../lib/api', () => ({
  apiJson: vi.fn(),
  apiFetch: vi.fn(),
}));
import { apiJson, apiFetch } from '../../lib/api';
import Checkpoints, {
  describeAction, describeUndone, driftedPaths, isDriftRefusal, formatWhen,
} from '../../pages/Checkpoints';

const A = '/tmp/cpwork/a.txt';
const B = '/tmp/cpwork/b.txt';
const C = '/tmp/cpwork/c.txt';

const NOTE =
  'Checkpoints cover file writes (coding_tools__write_file, ' +
  'coding_tools__edit_file), restored from stashed bytes, and three creations ' +
  'undone by their inverse call (calendar_google__create_event, ' +
  'feral_reminders__create, feral_routines__create). Anything ' +
  'coding_tools__bash changed in this turn (shell redirects, sed -i, ' +
  'formatters, package installs, git commands) is NOT reverted and is not ' +
  'tracked here. Neither is any other action: sent email cannot be unsent, ' +
  'chat messages cannot be un-notified, and purchases cannot be undone.';

const TURNS = {
  count: 1,
  turns: [{
    turn_id: 'turn1', session_id: 's1', writes: 2, files: 2, actions: 1,
    started_at: 1787283850.725763, ended_at: 1787283850.72709,
  }],
  note: NOTE,
};

const FILE_A = { path: A, status: 'restorable', action: 'restore', detail: '', kind: 'file' };
const FILE_B = { path: B, status: 'restorable', action: 'delete', detail: '', kind: 'file' };

/** The turn also created a calendar event, undone by a compensating call. */
const EVENT_ROW = {
  path: '', status: 'reversible', action: 'compensate',
  detail: 'calls calendar_google__delete_event to undo it.',
  kind: 'action', target: 'evt_9x',
  tool_name: 'calendar_google__create_event',
  inverse_tool: 'calendar_google__delete_event',
  label: 'calendar event',
};

/** plan_revert on a clean turn. */
const CLEAN_PLAN = {
  turn_id: 'turn1',
  writes: [],
  actions: [],
  plan: {
    success: true, turn_id: 'turn1', dry_run: true, refused: false, error_code: '',
    forced: false, reverted: [], reverted_actions: [], reverted_count: 0,
    partial: false, skipped: [], drifted: [],
    files: [FILE_A, FILE_B],
    actions: [EVENT_ROW],
    entries: [FILE_A, FILE_B, EVENT_ROW],
    bash_not_covered: true, note: NOTE,
  },
};

const DRIFT_ROW = {
  path: A, status: 'drifted', action: 'restore',
  detail: 'file changed after the agent wrote it; reverting would discard that change.',
  kind: 'file',
};

/** plan_revert once a.txt has drifted. Note refused is FALSE here. */
const DRIFTED_PLAN = {
  turn_id: 'turn1',
  writes: [],
  actions: [],
  plan: {
    ...CLEAN_PLAN.plan,
    drifted: [DRIFT_ROW],
    files: [DRIFT_ROW, FILE_B],
    entries: [DRIFT_ROW, FILE_B, EVENT_ROW],
  },
};

/** The real refusal envelope: a 200 body, not an HTTP error. */
const REFUSAL = {
  success: false, turn_id: 'turn1', dry_run: false, refused: true,
  error_code: 'revert_refused_drift', forced: false,
  reverted: [], reverted_actions: [], reverted_count: 0, partial: false,
  skipped: [], drifted: [DRIFT_ROW],
  files: [DRIFT_ROW, FILE_B], actions: [EVENT_ROW],
  entries: [DRIFT_ROW, FILE_B, EVENT_ROW],
  error: '1 file(s) changed after the agent wrote them. Refusing to overwrite them. ' +
         'Re-run with force to revert anyway (their newer content will be lost).',
};

const FORCED_OK = {
  success: true, turn_id: 'turn1', dry_run: false, refused: false,
  error_code: '', forced: true, reverted: [A, B],
  reverted_actions: [{ ...EVENT_ROW, status: 'reverted', detail: '' }],
  reverted_count: 3, partial: false, skipped: [], drifted: [DRIFT_ROW],
};

/**
 * A revert that restored the file and could not undo the event. Captured
 * the same way as the rest: a 200 body carrying `error`, so apiFetch
 * throws it exactly like the refusal.
 */
const PARTIAL = {
  success: false, turn_id: 'turn1', dry_run: false, refused: false,
  error_code: 'revert_incomplete', forced: false,
  reverted: [C], reverted_actions: [], reverted_count: 1, partial: true,
  skipped: [{ ...EVENT_ROW, status: 'failed', action: 'skip', detail: 'Internal Server Error' }],
  drifted: [],
  files: [{ path: C, status: 'restorable', action: 'restore', detail: '', kind: 'file' }],
  actions: [{ ...EVENT_ROW, status: 'failed', action: 'skip', detail: 'Internal Server Error' }],
  entries: [],
  error: '1 action(s) could not be reverted.',
};

beforeEach(() => {
  apiJson.mockReset();
  apiFetch.mockReset();
});

function routeJson(detail = CLEAN_PLAN) {
  apiJson.mockImplementation((path) => {
    if (path.startsWith('/api/checkpoints/turns/')) return Promise.resolve(detail);
    if (path.startsWith('/api/checkpoints/turns')) return Promise.resolve(TURNS);
    return Promise.resolve({});
  });
}

describe('pure helpers read the shapes the store actually emits', () => {
  it('describes a restore, a delete and a skip in words', () => {
    expect(describeAction({ path: A, action: 'restore' })).toMatch(/a\.txt will be restored/);
    expect(describeAction({ path: B, action: 'delete' })).toMatch(/b\.txt will be deleted/);
    expect(describeAction({ path: A, action: 'skip', detail: 'no blob' }))
      .toMatch(/a\.txt will be left alone: no blob/);
  });

  it('finds drift in `drifted`, which is not `skipped`', () => {
    // The route docstring warns about exactly this and the live store
    // confirmed it: a drifted file keeps action "restore" and never
    // appears in `skipped`, so a UI reading `skipped` reports nothing.
    expect(REFUSAL.skipped).toEqual([]);
    expect(driftedPaths(REFUSAL)).toEqual([A]);
    expect(driftedPaths(CLEAN_PLAN.plan)).toEqual([]);
  });

  it('identifies a refusal by refused and error_code, not by dry_run', () => {
    expect(isDriftRefusal(REFUSAL)).toBe(true);
    // A dry run over the same drifted turn is NOT a refusal, and its
    // dry_run flag is true while the refusal's is false, so keying off
    // dry_run inverts the meaning.
    expect(DRIFTED_PLAN.plan.dry_run).toBe(true);
    expect(REFUSAL.dry_run).toBe(false);
    expect(isDriftRefusal(DRIFTED_PLAN.plan)).toBe(false);
    expect(isDriftRefusal(FORCED_OK)).toBe(false);
    expect(isDriftRefusal({})).toBe(false);
  });

  it('describes an action by what it is, not by a path it does not have', () => {
    // Actions carry an empty `path`. A page that reads `path` renders a
    // blank row and tells the user nothing is going to happen.
    expect(describeAction(EVENT_ROW)).toMatch(/calendar event evt_9x will be deleted/);
    expect(describeAction({ ...EVENT_ROW, status: 'already_reverted', action: 'skip' }))
      .toMatch(/calendar event evt_9x is already gone/);
    expect(describeAction({
      ...EVENT_ROW, status: 'failed', action: 'skip', detail: 'Internal Server Error',
    })).toMatch(/will be left alone: Internal Server Error/);
  });

  it('counts files and actions separately, from the envelope lists', () => {
    // reverted_count is the total. Reporting it as "file(s) restored"
    // would claim a file came back when an event was deleted instead.
    expect(describeUndone(FORCED_OK)).toBe('2 file(s) restored, 1 action(s) undone');
    expect(describeUndone(PARTIAL)).toBe('1 file(s) restored');
    expect(describeUndone({})).toBe('0 file(s) restored');
  });

  it('does not read a partial revert as a refusal', () => {
    expect(isDriftRefusal(PARTIAL)).toBe(false);
    expect(PARTIAL.partial).toBe(true);
    expect(PARTIAL.error_code).toBe('revert_incomplete');
  });

  it('formats a timestamp and survives a missing one', () => {
    expect(formatWhen(1787283850.725763)).not.toBe('');
    expect(formatWhen(0)).toBe('');
    expect(formatWhen(undefined)).toBe('');
  });
});

describe('the page', () => {
  it('always shows that bash changes are not covered', async () => {
    routeJson();
    render(<Checkpoints />);
    await waitFor(() => expect(screen.getByText(/NOT reverted/)).toBeInTheDocument());
  });

  it('lists turns that wrote files', async () => {
    routeJson();
    render(<Checkpoints />);
    await waitFor(() => expect(screen.getByText('2 files')).toBeInTheDocument());
  });

  it('shows what an undo would do before doing it', async () => {
    routeJson();
    render(<Checkpoints />);
    await waitFor(() => expect(screen.getByText('2 files')).toBeInTheDocument());
    fireEvent.click(screen.getByText('2 files'));
    await waitFor(() => expect(screen.getByText(/a\.txt will be restored/)).toBeInTheDocument());
    expect(screen.getByText(/b\.txt will be deleted/)).toBeInTheDocument();
  });

  it('warns about drift in the plan, before the user commits', async () => {
    routeJson(DRIFTED_PLAN);
    render(<Checkpoints />);
    await waitFor(() => expect(screen.getByText('2 files')).toBeInTheDocument());
    fireEvent.click(screen.getByText('2 files'));
    await waitFor(() =>
      expect(screen.getByText(/1 file has changed since the turn/)).toBeInTheDocument());
  });

  it('reverts through the real endpoint', async () => {
    routeJson();
    apiFetch.mockResolvedValue({ ok: true, json: async () => ({ ...FORCED_OK, forced: false }) });
    render(<Checkpoints />);
    await waitFor(() => expect(screen.getByText('2 files')).toBeInTheDocument());
    fireEvent.click(screen.getByText('2 files'));
    await waitFor(() => expect(screen.getByText('Undo this turn')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Undo this turn'));
    await waitFor(() => expect(apiFetch).toHaveBeenCalled());
    const [path, opts] = apiFetch.mock.calls[0];
    expect(path).toBe('/api/checkpoints/revert');
    expect(opts.method).toBe('POST');
    expect(JSON.parse(opts.body)).toEqual({ turn_id: 'turn1', force: false });
  });

  it('treats a refusal as a refusal even though it arrives as a 200', async () => {
    routeJson(DRIFTED_PLAN);
    apiFetch.mockResolvedValue({ ok: true, json: async () => REFUSAL });
    render(<Checkpoints />);
    await waitFor(() => expect(screen.getByText('2 files')).toBeInTheDocument());
    fireEvent.click(screen.getByText('2 files'));
    await waitFor(() => expect(screen.getByText('Undo this turn')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Undo this turn'));
    await waitFor(() => expect(screen.getByText(/Nothing was undone/)).toBeInTheDocument());
    expect(screen.getByText(A)).toBeInTheDocument();
  });

  it('offers force only after the brain has refused and named the files', async () => {
    routeJson(DRIFTED_PLAN);
    apiFetch.mockResolvedValue({ ok: true, json: async () => REFUSAL });
    render(<Checkpoints />);
    await waitFor(() => expect(screen.getByText('2 files')).toBeInTheDocument());
    fireEvent.click(screen.getByText('2 files'));
    await waitFor(() => expect(screen.getByText('Undo this turn')).toBeInTheDocument());

    // Not before: a destructive control must not be the first thing on screen.
    expect(screen.queryByText(/Undo anyway/)).not.toBeInTheDocument();

    fireEvent.click(screen.getByText('Undo this turn'));
    await waitFor(() => expect(screen.getByText(/Undo anyway/)).toBeInTheDocument());
  });

  it('sends force only when the user picks the force button', async () => {
    routeJson(DRIFTED_PLAN);
    apiFetch.mockResolvedValue({ ok: true, json: async () => REFUSAL });
    render(<Checkpoints />);
    await waitFor(() => expect(screen.getByText('2 files')).toBeInTheDocument());
    fireEvent.click(screen.getByText('2 files'));
    await waitFor(() => expect(screen.getByText('Undo this turn')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Undo this turn'));
    await waitFor(() => expect(screen.getByText(/Undo anyway/)).toBeInTheDocument());

    apiFetch.mockResolvedValue({ ok: true, json: async () => FORCED_OK });
    fireEvent.click(screen.getByText(/Undo anyway/));
    await waitFor(() => expect(apiFetch.mock.calls.length).toBe(2));
    expect(JSON.parse(apiFetch.mock.calls[0][1].body).force).toBe(false);
    expect(JSON.parse(apiFetch.mock.calls[1][1].body).force).toBe(true);
  });

  it('reports the empty case rather than an error', async () => {
    apiJson.mockResolvedValue({ count: 0, turns: [], note: NOTE });
    render(<Checkpoints />);
    await waitFor(() => expect(screen.getByText('Nothing to undo')).toBeInTheDocument());
  });

  it('says so when the brain is unreachable', async () => {
    apiJson.mockRejectedValue(new Error('Failed to fetch'));
    render(<Checkpoints />);
    await waitFor(() => expect(screen.getByText(/Failed to fetch/)).toBeInTheDocument());
  });
});

describe('the refusal path as the real apiFetch delivers it', () => {
  /**
   * These two exist because mocking apiFetch as a plain {ok, json}
   * resolver hid a bug that a browser found immediately.
   *
   * The real apiFetch inspects even a 200 response body and, when it
   * carries a non-empty `error` string, throws an ApiError and pushes a
   * global error toast (see bodyHasError in lib/api.js). The drift
   * refusal is a 200 whose envelope carries exactly that string, so the
   * page's success path never ran and the refusal rendered as nothing.
   * The fix is `silent: true` plus reading the envelope off `.raw`.
   */
  class FakeApiError extends Error {
    constructor(body) {
      super(body.error);
      this.name = 'ApiError';
      this.status = 200;
      this.raw = body;
    }
  }

  it('renders the refusal when apiFetch throws it instead of returning it', async () => {
    routeJson(DRIFTED_PLAN);
    apiFetch.mockRejectedValue(new FakeApiError(REFUSAL));
    render(<Checkpoints />);
    await waitFor(() => expect(screen.getByText('2 files')).toBeInTheDocument());
    fireEvent.click(screen.getByText('2 files'));
    await waitFor(() => expect(screen.getByText('Undo this turn')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Undo this turn'));

    await waitFor(() => expect(screen.getByText(/Nothing was undone/)).toBeInTheDocument());
    expect(screen.getByText(/Undo anyway/)).toBeInTheDocument();
  });

  it('asks apiFetch not to raise a global toast for a designed refusal', async () => {
    routeJson(DRIFTED_PLAN);
    apiFetch.mockRejectedValue(new FakeApiError(REFUSAL));
    render(<Checkpoints />);
    await waitFor(() => expect(screen.getByText('2 files')).toBeInTheDocument());
    fireEvent.click(screen.getByText('2 files'));
    await waitFor(() => expect(screen.getByText('Undo this turn')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Undo this turn'));
    await waitFor(() => expect(apiFetch).toHaveBeenCalled());
    expect(apiFetch.mock.calls[0][1].silent).toBe(true);
  });

  it('still reports a genuine failure as an error', async () => {
    routeJson();
    apiFetch.mockRejectedValue(new Error('Failed to fetch'));
    render(<Checkpoints />);
    await waitFor(() => expect(screen.getByText('2 files')).toBeInTheDocument());
    fireEvent.click(screen.getByText('2 files'));
    await waitFor(() => expect(screen.getByText('Undo this turn')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Undo this turn'));
    await waitFor(() => expect(screen.getByText(/Failed to fetch/)).toBeInTheDocument());
  });

  it('keeps the confirmation on screen after the turn collapses', async () => {
    // The success path calls loadTurns() and closes the open turn, which
    // unmounts everything inside it. Rendering the confirmation there
    // destroyed it on the same tick, so a successful undo said nothing.
    routeJson();
    apiFetch.mockResolvedValue({
      ok: true,
      json: async () => ({ ...FORCED_OK, forced: false }),
    });
    render(<Checkpoints />);
    await waitFor(() => expect(screen.getByText('2 files')).toBeInTheDocument());
    fireEvent.click(screen.getByText('2 files'));
    await waitFor(() => expect(screen.getByText('Undo this turn')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Undo this turn'));

    await waitFor(() => expect(screen.getByText(/Undone\. 2 file\(s\) restored/)).toBeInTheDocument());
    // And the turn really did collapse, so this is not passing by accident.
    expect(screen.queryByText('Undo this turn')).not.toBeInTheDocument();
  });

  it('shows the actions a turn created alongside its files', async () => {
    routeJson();
    render(<Checkpoints />);
    await waitFor(() => expect(screen.getByText('1 action')).toBeInTheDocument());
    fireEvent.click(screen.getByText('2 files'));

    await waitFor(() => expect(
      screen.getByText(/calendar event evt_9x will be deleted/),
    ).toBeInTheDocument());
  });

  it('reports a partial revert as partly done, not as a failed request', async () => {
    // The dangerous shape. A 200 carrying `error` is thrown by apiFetch
    // exactly like the refusal is, and reporting it as a generic failure
    // tells the user nothing came back when the file restore did.
    routeJson();
    apiFetch.mockRejectedValue(new FakeApiError(PARTIAL));
    render(<Checkpoints />);
    await waitFor(() => expect(screen.getByText('2 files')).toBeInTheDocument());
    fireEvent.click(screen.getByText('2 files'));
    await waitFor(() => expect(screen.getByText('Undo this turn')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Undo this turn'));

    await waitFor(() => expect(
      screen.getByText(/Partly undone\. 1 file\(s\) restored\. 1 action\(s\) could not be reverted\./),
    ).toBeInTheDocument());
  });
});
