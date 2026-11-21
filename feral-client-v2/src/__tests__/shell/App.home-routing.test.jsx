import React from 'react';
import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter, Outlet, useLocation } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import App from '../../App';

// Exercise the real route table while keeping page/network lifecycle out of
// this routing regression. Home rendering has its own page suites.
vi.mock('../../shell/Shell', () => ({ default: () => <Outlet /> }));
vi.mock('../../pages/Home', () => ({ default: () => <h1>Home overview</h1> }));
vi.mock('../../pages/Chat', () => ({ default: () => <h1>Chat conversation</h1> }));

function Location() {
  return <output data-testid="location">{useLocation().pathname}</output>;
}

afterEach(cleanup);

describe('Home route continuity', () => {
  for (const entry of ['/', '/home', '/ambient']) {
    it(`opens Home from ${entry}`, async () => {
      render(<MemoryRouter initialEntries={[entry]}><App /><Location /></MemoryRouter>);
      expect(await screen.findByRole('heading', { name: 'Home overview' })).toBeInTheDocument();
      expect(screen.getByTestId('location')).toHaveTextContent(/^\/home$/);
      expect(screen.queryByRole('heading', { name: 'Chat conversation' })).toBeNull();
    });
  }

  it('preserves direct Chat navigation', async () => {
    render(<MemoryRouter initialEntries={['/chat']}><App /><Location /></MemoryRouter>);
    expect(await screen.findByRole('heading', { name: 'Chat conversation' })).toBeInTheDocument();
    expect(screen.getByTestId('location')).toHaveTextContent(/^\/chat$/);
  });
});
