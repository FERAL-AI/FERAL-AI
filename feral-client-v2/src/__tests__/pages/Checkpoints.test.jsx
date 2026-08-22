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
  describeAction, driftedPaths, isDriftRefusal, formatWhen,
} from '../../pages/Checkpoints';

const A = '/tmp/cpwork/a.txt';
const B = '/tmp/cpwork/b.txt';

const NOTE =
  'Checkpoints cover coding_tools__write_file and coding_tools__edit_file only. ' +
  'Anything coding_tools__bash changed in this turn (shell redirects, sed -i, ' +
  'formatters, package installs, git commands) is NOT reverted and is not tracked here.';

const TURNS = {
  count: 1,
  turns: [{
    turn_id: 'turn1', session_id: 's1', writes: 2, files: 2,
    started_at: 1787283850.725763, ended_at: 1787283850.72709,
  }],
  note: NOTE,
};

/** plan_revert on a clean turn. */
const CLEAN_PLAN = {
  turn_id: 'turn1',
  writes: [],
  plan: {
    success: true, turn_id: 'turn1', dry_run: true, refused: false, error_code: '',
    forced: false, reverted: [], reverted_count: 0, skipped: [], drifted: [],
    files: [
      { path: A, status: 'restorable', action: 'restore', detail: '' },
      { path: B, status: 'restorable', action: 'delete', detail: '' },
    ],
    bash_not_covered: true, note: NOTE,
  },
};

const DRIFT_ROW = {
  path: A, status: 'drifted', action: 'restore',
  detail: 'file changed after the agent wrote it; reverting would discard that change.',
};

/** plan_revert once a.txt has drifted. Note refused is FALSE here. */
const DRIFTED_PLAN = {
  turn_id: 'turn1',
  writes: [],
  plan: {
    ...CLEAN_PLAN.plan,
    drifted: [DRIFT_ROW],
    files: [
      DRIFT_ROW,
      { path: B, status: 'restorable', action: 'delete', detail: '' },
    ],
  },
};

/** The real refusal envelope: a 200 body, not an HTTP error. */
const REFUSAL = {
  success: false, turn_id: 'turn1', dry_run: false, refused: true,
  error_code: 'revert_refused_drift', forced: false,
  reverted: [], reverted_count: 0, skipped: [], drifted: [DRIFT_ROW],
  error: '1 file(s) changed after the agent wrote them. Refusing to overwrite them. ' +
         'Re-run with force to revert anyway (their newer content will be lost).',
};

const FORCED_OK = {
  success: true, turn_id: 'turn1', dry_run: false, refused: false,
  error_code: '', forced: true, reverted_count: 2, skipped: [], drifted: [DRIFT_ROW],
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
});
