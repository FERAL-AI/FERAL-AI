import React from 'react';
import { cleanup, fireEvent, screen, waitFor } from '@testing-library/react';
import { Route, Routes } from 'react-router-dom';
import { renderV2 } from '../_helpers/renderV2';
import Shell from '../../shell/Shell';

vi.mock('../../hooks/useMachineVitals', () => ({ useMachineVitals: () => ({
  reachable: true, running: 0, needs: 0, devices: 0, episodes: 0,
  costKnown: false, autonomy: '',
}) }));

function draw() {
  return renderV2(<Routes><Route element={<Shell />}>
    <Route path="/chat" element={<p>Conversation space</p>} />
    <Route path="/console" element={<p>Runtime details</p>} />
  </Route></Routes>, { route: '/chat' });
}

beforeEach(() => localStorage.removeItem('feral_v2_activity_open'));
afterEach(() => { cleanup(); localStorage.removeItem('feral_v2_activity_open'); vi.unstubAllGlobals(); });

it('starts with a quiet conversation area and remembers an explicitly opened activity sidebar', async () => {
  const view = draw();
  expect(view.container.querySelector('.v2-rail')).toBeNull();
  expect(screen.getByRole('main')).toHaveTextContent('Conversation space');
  fireEvent.click(screen.getByRole('button', { name: 'Show activity sidebar' }));
  expect(view.container.querySelector('.v2-rail')).toBeTruthy();
  expect(screen.getByRole('button', { name: 'Hide activity sidebar' })).toHaveAttribute('aria-expanded', 'true');
  await waitFor(() => expect(localStorage.getItem('feral_v2_activity_open')).toBe('true'));
  view.unmount();
  const remount = draw();
  expect(remount.container.querySelector('.v2-rail')).toBeTruthy();
});

it('opens More by an explicit control, finds advanced destinations and navigates there', async () => {
  draw();
  fireEvent.click(screen.getByRole('button', { name: 'More tools and search' }));
  const dialog = screen.getByRole('dialog', { name: 'Command palette' });
  expect(dialog).toBeInTheDocument();
  const input = screen.getByRole('searchbox', { name: 'Search commands and pages' });
  await waitFor(() => expect(input).toHaveFocus());
  fireEvent.change(input, { target: { value: 'Console' } });
  fireEvent.keyDown(input, { key: 'Enter' });
  await waitFor(() => expect(screen.getByRole('main')).toHaveTextContent('Runtime details'));
  expect(screen.queryByRole('dialog', { name: 'Command palette' })).not.toBeInTheDocument();
});

it('keeps telemetry behind an accessible disclosure with Escape recovery', () => {
  const { container } = draw();
  const details = container.querySelector('.v2-status-details');
  expect(details.open).toBe(false);
  const summary = screen.getByText('Activity', { selector: '.v2-status-label' }).parentElement;
  details.open = true;
  summary.focus();
  fireEvent.keyDown(summary, { key: 'Escape' });
  expect(details.open).toBe(false);
  expect(summary).toHaveFocus();
  expect(screen.getByRole('link', { name: 'Skip to content' })).toHaveAttribute('href', '#main-content');
});
