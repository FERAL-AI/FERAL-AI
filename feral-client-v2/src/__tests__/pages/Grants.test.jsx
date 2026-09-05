/**
 * Folder grants.
 *
 * Fixtures are real: captured by running SandboxPolicy against a temp
 * FERAL_HOME (grant_folder, list_grants, revoke_folder), not written
 * from the route source.
 */
import React from 'react';
import { render, screen, waitFor, fireEvent, within } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('../../lib/api', () => ({ apiJson: vi.fn(), apiFetch: vi.fn() }));
import { apiJson, apiFetch } from '../../lib/api';
import Grants, {
  MODES, modeLabel, canWrite, formatGranted, envelopeOf,
  FILTER_THRESHOLD, isTemporaryPath, orderGrants, filterGrants,
} from '../../pages/Grants';

const P1 = '/Users/me/Projects';
const P2 = '/Users/me/Desktop';

/** A pytest sandbox, in the exact shape found in a live grants file. */
const tempPath = (i) =>
  `/private/var/folders/bn/x/T/pytest-of-me/pytest-${i}/test_a_running_background_job_0/work`;

function manyGrants(n) {
  return {
    grants: [
      ...Array.from({ length: n }, (_, i) => (
        { path: tempPath(i), mode: 'readwrite', granted_at: 1787284000 + i }
      )),
      { path: P1, mode: 'readwrite', granted_at: 1787284841.227888 },
      { path: P2, mode: 'read', granted_at: 1787284000.0 },
    ],
  };
}

const GRANTS = {
  grants: [
    { path: P1, mode: 'readwrite', granted_at: 1787284841.227888 },
    { path: P2, mode: 'read', granted_at: 1787284000.0 },
  ],
};

beforeEach(() => {
  apiJson.mockReset();
  apiFetch.mockReset();
});

/** apiFetch throws on any 200 whose body carries a non-empty `error`. */
class FakeApiError extends Error {
  constructor(body) {
    super(body.error);
    this.name = 'ApiError';
    this.status = 200;
    this.raw = body;
  }
}

describe('helpers', () => {
  it('names both modes in words a person reads', () => {
    expect(modeLabel('readwrite')).toBe('Read and write');
    expect(modeLabel('read')).toBe('Read only');
  });

  it('treats the legacy write alias as writable', () => {
    // The POST route accepts read | readwrite | write, so a policy file
    // written by an older build can still hold 'write'. Rendering that
    // as read-only would understate what FERAL is allowed to do.
    expect(canWrite('write')).toBe(true);
    expect(modeLabel('write')).toBe('Read and write');
    expect(canWrite('readwrite')).toBe(true);
    expect(canWrite('read')).toBe(false);
  });

  it('only offers the two non-legacy modes when granting', () => {
    expect(MODES).toEqual(['read', 'readwrite']);
  });

  it('formats granted_at and survives a missing one', () => {
    expect(formatGranted(1787284841.227888)).not.toBe('');
    expect(formatGranted(0)).toBe('');
    expect(formatGranted(undefined)).toBe('');
  });

  it('reads the envelope from a body or from a thrown ApiError', () => {
    const body = { ok: false, error: 'invalid mode: sideways' };
    expect(envelopeOf(body)).toBe(body);
    expect(envelopeOf(new FakeApiError(body))).toEqual(body);
    expect(envelopeOf(new Error('boom'))).toBe(null);
    expect(envelopeOf(null)).toBe(null);
  });
});

describe('the page', () => {
  it('lists granted folders with their access level', async () => {
    apiJson.mockResolvedValue(GRANTS);
    render(<Grants />);
    await waitFor(() => expect(screen.getByText(P1)).toBeInTheDocument());
    // Scoped to the list: the mode <select> carries the same two labels,
    // so an unscoped query matches the form as well as the rows.
    const list = screen.getByLabelText('Allowed folders');
    expect(within(list).getByText('Read and write')).toBeInTheDocument();
    expect(within(list).getByText('Read only')).toBeInTheDocument();
  });

  it('marks a writable grant differently from a read-only one', async () => {
    apiJson.mockResolvedValue(GRANTS);
    render(<Grants />);
    await waitFor(() => expect(screen.getByText(P1)).toBeInTheDocument());
    const rows = screen.getByLabelText('Allowed folders').querySelectorAll('.v2-grant');
    const badges = [...rows].map((r) => r.querySelector('.v2-grant-mode').dataset.write);
    expect(badges).toEqual(['yes', 'no']);
  });

  it('says plainly that nothing is reachable when there are no grants', async () => {
    apiJson.mockResolvedValue({ grants: [] });
    render(<Grants />);
    await waitFor(() =>
      expect(screen.getByText('No folders allowed yet')).toBeInTheDocument());
  });

  it('grants a folder through the real endpoint', async () => {
    apiJson.mockResolvedValue({ grants: [] });
    apiFetch.mockResolvedValue({
      ok: true, json: async () => ({ ok: true, path: P1, mode: 'readwrite' }),
    });
    render(<Grants />);
    await waitFor(() => expect(screen.getByText('No folders allowed yet')).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText('Folder to allow'), { target: { value: P1 } });
    fireEvent.change(screen.getByLabelText('Access level'), { target: { value: 'readwrite' } });
    fireEvent.click(screen.getByText('Allow'));

    await waitFor(() => expect(apiFetch).toHaveBeenCalled());
    const [path, opts] = apiFetch.mock.calls[0];
    expect(path).toBe('/api/security/grants');
    expect(opts.method).toBe('POST');
    expect(JSON.parse(opts.body)).toEqual({ path: P1, mode: 'readwrite' });
  });

  it('revokes with path as a query parameter, not a body', async () => {
    // The DELETE route signature is `revoke_workspace_folder(path: str)`,
    // which FastAPI binds from the query string. Sending a body here
    // returns 422 and the grant silently stays in place.
    apiJson.mockResolvedValue(GRANTS);
    apiFetch.mockResolvedValue({ ok: true, json: async () => ({ ok: true, path: P1 }) });
    render(<Grants />);
    await waitFor(() => expect(screen.getByText(P1)).toBeInTheDocument());

    fireEvent.click(screen.getByLabelText(`Stop allowing ${P1}`));
    await waitFor(() => expect(apiFetch).toHaveBeenCalled());
    const [path, opts] = apiFetch.mock.calls[0];
    expect(path).toBe(`/api/security/grants?path=${encodeURIComponent(P1)}`);
    expect(opts.method).toBe('DELETE');
    expect(opts.body).toBeUndefined();
  });

  it('shows a validation failure that arrives as a 200 body', async () => {
    apiJson.mockResolvedValue({ grants: [] });
    apiFetch.mockResolvedValue({
      ok: true, json: async () => ({ ok: false, error: 'invalid mode: sideways' }),
    });
    render(<Grants />);
    await waitFor(() => expect(screen.getByText('No folders allowed yet')).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText('Folder to allow'), { target: { value: P1 } });
    fireEvent.click(screen.getByText('Allow'));
    await waitFor(() =>
      expect(screen.getByText('invalid mode: sideways')).toBeInTheDocument());
  });

  it('shows the same failure when apiFetch throws it instead', async () => {
    // This is the shape the real apiFetch produces for that body, and
    // mocking only the resolved form is what hid the identical bug on
    // the checkpoints page.
    apiJson.mockResolvedValue({ grants: [] });
    apiFetch.mockRejectedValue(new FakeApiError({ ok: false, error: 'path is required' }));
    render(<Grants />);
    await waitFor(() => expect(screen.getByText('No folders allowed yet')).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText('Folder to allow'), { target: { value: P1 } });
    fireEvent.click(screen.getByText('Allow'));
    await waitFor(() => expect(screen.getByText('path is required')).toBeInTheDocument());
  });

  it('asks apiFetch not to raise a global toast for a validation message', async () => {
    apiJson.mockResolvedValue({ grants: [] });
    apiFetch.mockResolvedValue({ ok: true, json: async () => ({ ok: true }) });
    render(<Grants />);
    await waitFor(() => expect(screen.getByText('No folders allowed yet')).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText('Folder to allow'), { target: { value: P1 } });
    fireEvent.click(screen.getByText('Allow'));
    await waitFor(() => expect(apiFetch).toHaveBeenCalled());
    expect(apiFetch.mock.calls[0][1].silent).toBe(true);
  });

  it('will not submit an empty path', async () => {
    apiJson.mockResolvedValue({ grants: [] });
    render(<Grants />);
    await waitFor(() => expect(screen.getByText('No folders allowed yet')).toBeInTheDocument());
    expect(screen.getByText('Allow').closest('button')).toBeDisabled();
  });

  it('says so when the brain is unreachable', async () => {
    apiJson.mockRejectedValue(new Error('Failed to fetch'));
    render(<Grants />);
    await waitFor(() => expect(screen.getByText(/Failed to fetch/)).toBeInTheDocument());
  });
});

describe('a long list', () => {
  it('recognises the temp roots the OS hands out and reclaims', () => {
    expect(isTemporaryPath(tempPath(1))).toBe(true);
    expect(isTemporaryPath('/tmp/scratch')).toBe(true);
    expect(isTemporaryPath('/var/folders/bn/x/T/tmpabc')).toBe(true);
    expect(isTemporaryPath(P1)).toBe(false);
    expect(isTemporaryPath('/Users/me')).toBe(false);
    expect(isTemporaryPath(undefined)).toBe(false);
  });

  it('puts the folders a person chose above the scratch directories', () => {
    // The whole complaint: the two real grants were buried under 870
    // pytest sandboxes and could not be found.
    const rows = [
      { path: tempPath(1) }, { path: P1 }, { path: tempPath(2) }, { path: P2 },
    ];
    expect(orderGrants(rows).map((g) => g.path)).toEqual([
      P1, P2, tempPath(1), tempPath(2),
    ]);
  });

  it('filters on a case-insensitive substring of the path', () => {
    const rows = [{ path: P1 }, { path: P2 }, { path: tempPath(3) }];
    expect(filterGrants(rows, 'desktop').map((g) => g.path)).toEqual([P2]);
    expect(filterGrants(rows, 'PYTEST').map((g) => g.path)).toEqual([tempPath(3)]);
    expect(filterGrants(rows, '   ')).toEqual(rows);
    expect(filterGrants(rows, '')).toEqual(rows);
  });

  it('leaves a short list exactly as it was', async () => {
    // Two grants need no filter box; adding one to every install would
    // be noise on the common case.
    apiJson.mockResolvedValue(GRANTS);
    render(<Grants />);
    await waitFor(() => expect(screen.getByText(P1)).toBeInTheDocument());
    expect(screen.queryByLabelText('Filter folders')).toBeNull();
  });

  it('offers a filter and a count once the list is too long to scan', async () => {
    apiJson.mockResolvedValue(manyGrants(FILTER_THRESHOLD + 10));
    render(<Grants />);
    await waitFor(() => expect(screen.getByText(P1)).toBeInTheDocument());

    const total = FILTER_THRESHOLD + 12;
    expect(screen.getByText(`${total} folders`)).toBeInTheDocument();

    const list = screen.getByLabelText('Allowed folders');
    const paths = [...list.querySelectorAll('.v2-grant-path')].map((n) => n.textContent);
    expect(paths.slice(0, 2)).toEqual([P1, P2]);
  });

  it('narrows a long list down to the folder the operator is looking for', async () => {
    apiJson.mockResolvedValue(manyGrants(FILTER_THRESHOLD + 10));
    render(<Grants />);
    await waitFor(() => expect(screen.getByText(P1)).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText('Filter folders'), {
      target: { value: 'Desktop' },
    });
    await waitFor(() =>
      expect(screen.getByText(`1 of ${FILTER_THRESHOLD + 12} folders`)).toBeInTheDocument());
    const list = screen.getByLabelText('Allowed folders');
    expect(list.querySelectorAll('.v2-grant')).toHaveLength(1);
    expect(within(list).getByText(P2)).toBeInTheDocument();
  });

  it('says so when the filter matches nothing, instead of looking empty', async () => {
    apiJson.mockResolvedValue(manyGrants(FILTER_THRESHOLD + 10));
    render(<Grants />);
    await waitFor(() => expect(screen.getByText(P1)).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText('Filter folders'), {
      target: { value: 'nothing-matches-this' },
    });
    await waitFor(() =>
      expect(screen.getByText('No folder matches that filter.')).toBeInTheDocument());
    // Not the "you have no grants at all" empty state, which would be a lie.
    expect(screen.queryByText('No folders allowed yet')).toBeNull();
  });

  it('still revokes the right row after filtering', async () => {
    apiJson.mockResolvedValue(manyGrants(FILTER_THRESHOLD + 10));
    apiFetch.mockResolvedValue({ ok: true, json: async () => ({ ok: true, path: P2 }) });
    render(<Grants />);
    await waitFor(() => expect(screen.getByText(P1)).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText('Filter folders'), {
      target: { value: 'Desktop' },
    });
    await waitFor(() => expect(screen.getByLabelText(`Stop allowing ${P2}`)).toBeInTheDocument());
    fireEvent.click(screen.getByLabelText(`Stop allowing ${P2}`));

    await waitFor(() => expect(apiFetch).toHaveBeenCalled());
    expect(apiFetch.mock.calls[0][0])
      .toBe(`/api/security/grants?path=${encodeURIComponent(P2)}`);
  });
});
