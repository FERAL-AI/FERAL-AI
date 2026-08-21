/**
 * A bad response must not take the whole shell down.
 *
 * `setFences(d.geofences || d || [])` reads as a defensive fallback and
 * is the opposite of one: when the key is absent or null it hands the
 * entire response object to setFences, and the render calls
 * `fences.map`, which throws TypeError into the error boundary. The user
 * loses the whole shell, not just this page.
 *
 * The live brain answers {"geofences": [], "fences": []}, and an empty
 * array is truthy, so it never fired in practice. Any error envelope, a
 * renamed key, or an older brain is a white screen.
 */
import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('../../lib/api', () => ({ apiJson: vi.fn(), apiFetch: vi.fn() }));
import { apiJson } from '../../lib/api';
import Geofences from '../../pages/Geofences';

const draw = () => render(<MemoryRouter><Geofences /></MemoryRouter>);

beforeEach(() => { apiJson.mockReset(); });

describe('Geofences survives every response shape', () => {
  it('renders the real brain response', async () => {
    // Captured from a running brain: GET /api/geofences
    apiJson.mockResolvedValue({ geofences: [], fences: [] });
    draw();
    await waitFor(() => expect(screen.getByText('No geofences')).toBeInTheDocument());
  });

  it('renders fences when there are some', async () => {
    apiJson.mockResolvedValue({
      geofences: [{ id: 'g1', name: 'Home', lat: 1, lon: 2, radius_m: 50 }],
    });
    draw();
    await waitFor(() => expect(screen.getByText(/Home/)).toBeInTheDocument());
  });

  it.each([
    ['the key is absent', { ok: true }],
    ['the key is null', { geofences: null }],
    ['an error envelope', { error: 'boom' }],
    ['a bare array at the top level', []],
    ['null', null],
  ])('does not crash when %s', async (_label, payload) => {
    apiJson.mockResolvedValue(payload);
    // Before the fix each of these threw
    // "TypeError: fences.map is not a function" during render.
    expect(() => draw()).not.toThrow();
    await waitFor(() => expect(screen.getByText('No geofences')).toBeInTheDocument());
  });

  it('falls back to the sibling `fences` key the brain also sends', async () => {
    apiJson.mockResolvedValue({ fences: [{ id: 'g2', name: 'Office', lat: 3, lon: 4 }] });
    draw();
    await waitFor(() => expect(screen.getByText(/Office/)).toBeInTheDocument());
  });
});
