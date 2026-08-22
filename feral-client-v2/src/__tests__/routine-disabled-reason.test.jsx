/**
 * A routine the BRAIN turned off must say so, and say why.
 *
 * The brain now disables a routine that can never succeed instead of
 * firing it every minute forever and alerting about the failures. That
 * ends the retry loop and the nag, but it also means the only place the
 * user can find out what happened is this list. Rendering an
 * auto-disabled routine as "paused" tells him he did something he did
 * not do, and shows none of the recorded reason.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, waitFor, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { StubWebSocket } from './_helpers/renderV2';

import Flows from '../pages/Flows';

const REASON =
  'This routine fires an action on a fixed poll, but its condition '
  + "(biometric.inferred_state == 'sleeping') is not evaluated at fire time. "
  + 'It has been turned off instead of retried every minute.';

function renderWith(routines) {
  vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({
    ok: true,
    status: 200,
    statusText: 'OK',
    json: () => Promise.resolve({
      flows: [], packs: [], automations: [], skills: [], routines,
    }),
    text: () => Promise.resolve('{}'),
    headers: new Map(),
    clone() { return this; },
  })));
  StubWebSocket.instances = [];
  vi.stubGlobal('WebSocket', StubWebSocket);
  return render(<MemoryRouter initialEntries={['/']}><Flows /></MemoryRouter>);
}

async function openRoutines(getByRole, container) {
  fireEvent.click(getByRole('tab', { name: /Routines/i }));
  await waitFor(() => {
    expect(container.querySelector('.v2-flow-card')).not.toBeNull();
  });
}

/** StatusDot renders its label as aria-label on a decorative span, so the
 *  status word never appears in textContent. */
function statusLabel(container) {
  return container
    .querySelector('.v2-flow-card .v2-dot')
    ?.getAttribute('aria-label');
}

describe('an auto-disabled routine', () => {
  beforeEach(() => { vi.restoreAllMocks(); });
  afterEach(() => { vi.unstubAllGlobals(); });

  it('shows the recorded reason', async () => {
    const { container, getByRole } = renderWith([{
      id: 7,
      description: '[auto] smart_home_hue: trigger on sleep_detected',
      cron_expr: 'every 1m',
      enabled: false,
      disabled_reason: REASON,
    }]);
    await openRoutines(getByRole, container);

    await waitFor(() => {
      expect(
        container.querySelector('[data-testid="v2-routine-disabled-reason"]'),
      ).not.toBeNull();
    });
    expect(container.textContent).toContain("biometric.inferred_state == 'sleeping'");
  });

  it('is labelled "turned off", not "paused"', async () => {
    const { container, getByRole } = renderWith([{
      id: 7,
      description: '[auto] smart_home_hue: trigger on sleep_detected',
      cron_expr: 'every 1m',
      enabled: false,
      disabled_reason: REASON,
    }]);
    await openRoutines(getByRole, container);

    expect(statusLabel(container)).toBe('turned off');
  });

  it('can still be resumed or deleted', async () => {
    const { container, getByRole } = renderWith([{
      id: 7,
      description: '[auto] smart_home_hue: trigger on sleep_detected',
      cron_expr: 'every 1m',
      enabled: false,
      disabled_reason: REASON,
    }]);
    await openRoutines(getByRole, container);

    await waitFor(() => {
      expect(container.textContent).toMatch(/Resume/);
    });
    expect(container.textContent).toMatch(/Delete/);
  });
});

describe('a routine the user paused himself', () => {
  beforeEach(() => { vi.restoreAllMocks(); });
  afterEach(() => { vi.unstubAllGlobals(); });

  it('still reads "paused" and invents no reason', async () => {
    const { container, getByRole } = renderWith([{
      id: 8,
      description: 'nightly digest',
      cron_expr: 'daily 21:00',
      enabled: false,
      disabled_reason: '',
    }]);
    await openRoutines(getByRole, container);

    expect(statusLabel(container)).toBe('paused');
    expect(
      container.querySelector('[data-testid="v2-routine-disabled-reason"]'),
    ).toBeNull();
  });
});
