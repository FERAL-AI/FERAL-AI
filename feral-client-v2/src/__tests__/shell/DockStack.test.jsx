/**
 * A tile's contents fan out above the dock, with the verb on the row.
 *
 * The design: "Press and hold a tile, or right-click it, and its
 * contents fan out above the dock: the two approvals with an approve
 * verb, the running jobs with kill and steer. You act from the stack
 * without navigating anywhere."
 *
 * The payload shapes here are the real ones, taken from the running
 * brain: /api/approvals answers {count, approvals:[{request_id,
 * tool_name, args, safety_level, ...}]} and /api/jobs answers
 * {items:[{id, kind, name, status, cancellable_via, ...}]}.
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

vi.mock('../../lib/api', () => ({ apiJson: vi.fn(), apiFetch: vi.fn() }));
import { apiJson, apiFetch } from '../../lib/api';
import DockStack, { rowsFrom, isStackable, STACKABLE, HOLD_MS } from '../../shell/DockStack';

const draw = (to) => render(
  <MemoryRouter><DockStack to={to} onClose={() => {}} /></MemoryRouter>,
);

beforeEach(() => { apiJson.mockReset(); apiFetch.mockReset(); });

describe('only tiles with something to act on have a stack', () => {
  it('approvals and jobs do', () => {
    expect(isStackable('/approvals')).toBe(true);
    expect(isStackable('/jobs')).toBe(true);
  });

  it('a tile with no list does not', () => {
    // Settings has nothing to act on from a stack, so holding it must
    // stay an ordinary press rather than opening an empty popover.
    for (const to of ['/settings', '/chat', '/memory', '/console', '/devices', '/skills']) {
      expect(isStackable(to)).toBe(false);
    }
  });

  it('every stackable path names a real source', () => {
    for (const [, meta] of Object.entries(STACKABLE)) {
      expect(meta.source).toMatch(/^\/api\//);
      expect(meta.title).toBeTruthy();
    }
  });

  it('holds long enough to not be a click', () => {
    expect(HOLD_MS).toBeGreaterThanOrEqual(300);
    expect(HOLD_MS).toBeLessThanOrEqual(800);
  });
});

describe('rows come from the real payloads', () => {
  it('reads an approval into a row with the approve verb', () => {
    const rows = rowsFrom('/approvals', {
      count: 1,
      approvals: [{
        request_id: 'r1', tool_name: 'coding_tools__write_file',
        args: { path: '/Users/me/build.sh' }, safety_level: 'confirm',
      }],
    });
    expect(rows).toEqual([{
      id: 'r1', title: 'coding_tools__write_file',
      sub: '/Users/me/build.sh', verb: 'approve',
    }]);
  });

  it('offers kill only when the brain names a route for it', () => {
    const rows = rowsFrom('/jobs', {
      items: [
        { id: 'a', kind: 'taskflow', name: 'Digest', status: 'running',
          cancellable_via: 'POST /api/taskflows/a/cancel' },
        // Background shell jobs are stopped by a tool call, not a route.
        { id: 'b', kind: 'background_bash', name: 'npm run build', status: 'running',
          cancellable_via: null },
      ],
    });
    expect(rows.map((r) => [r.id, r.verb])).toEqual([['a', 'kill'], ['b', '']]);
  });

  it('leaves finished work out of the stack', () => {
    const rows = rowsFrom('/jobs', {
      items: [
        { id: 'a', name: 'done thing', status: 'completed' },
        { id: 'b', name: 'live thing', status: 'running' },
      ],
    });
    expect(rows.map((r) => r.id)).toEqual(['b']);
  });

  it('survives an empty or malformed payload', () => {
    expect(rowsFrom('/approvals', {})).toEqual([]);
    expect(rowsFrom('/jobs', {})).toEqual([]);
    expect(rowsFrom('/jobs', null)).toEqual([]);
  });
});

describe('acting from the stack', () => {
  it('approves through the real endpoint and drops the row', async () => {
    apiJson.mockResolvedValue({ approvals: [{ request_id: 'r1', tool_name: 'shell__run', args: {} }] });
    apiFetch.mockResolvedValue({ ok: true, json: async () => ({}) });
    draw('/approvals');
    await waitFor(() => expect(screen.getByText('shell__run')).toBeInTheDocument());

    fireEvent.click(screen.getByText('approve'));
    await waitFor(() => expect(apiFetch).toHaveBeenCalled());
    expect(apiFetch.mock.calls[0][0]).toBe('/api/approvals/r1/approve');
    expect(apiFetch.mock.calls[0][1].method).toBe('POST');
    await waitFor(() => expect(screen.queryByText('shell__run')).not.toBeInTheDocument());
  });

  it('says so rather than offering a verb that would 404', async () => {
    apiJson.mockResolvedValue({
      items: [{ id: 'b', kind: 'background_bash', name: 'npm run build', status: 'running', cancellable_via: null }],
    });
    draw('/jobs');
    await waitFor(() => expect(screen.getByText('npm run build')).toBeInTheDocument());
    expect(screen.getByText('no verb')).toBeInTheDocument();
    expect(screen.queryByText('kill')).not.toBeInTheDocument();
  });

  it('reports an empty stack rather than rendering nothing', async () => {
    apiJson.mockResolvedValue({ approvals: [] });
    draw('/approvals');
    await waitFor(() => expect(screen.getByText('Nothing here right now.')).toBeInTheDocument());
  });

  it('does not blank out when the brain is unreachable', async () => {
    apiJson.mockRejectedValue(new Error('Failed to fetch'));
    draw('/jobs');
    await waitFor(() => expect(screen.getByText('Nothing here right now.')).toBeInTheDocument());
  });
});
