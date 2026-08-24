/**
 * The strip that says the brain is not the build you installed.
 *
 * The condition it reports is completely silent today: a Python
 * process never reloads its source, so `pip install --upgrade
 * feral-ai` against a running brain succeeds and changes nothing. One
 * real install served for two days and one hour from code that
 * predated four releases, and the dashboard kept rendering the whole
 * time.
 *
 * WHAT THIS FILE CAN AND CANNOT PROVE. vitest runs in jsdom, which has
 * no layout engine: every box here is 0x0 and `getComputedStyle` never
 * applies a stylesheet. So this file is the LOGIC half only, and says
 * so out loud, because two recent defects in this client passed unit
 * tests exactly like these while being invisible on screen (a banner
 * at y = -3365px, and an error toast parked on top of the supervisor
 * kill switch).
 *
 * The half that needs a browser lives in
 * `e2e/runtime_notice.spec.ts`: that the strip is on screen, that the
 * page is laid out BELOW it rather than under it, and that it covers
 * no control.
 */
import React from 'react';
import { render, screen, waitFor, act } from '@testing-library/react';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';

import RuntimeNotice, { runtimeNotices, humanUptime } from '../../shell/RuntimeNotice';
import {
  refreshSystemHealth,
  _resetSystemHealthForTesting,
} from '../../hooks/useSystemHealth';

const apiJsonMock = vi.fn();

vi.mock('../../lib/api', () => ({
  apiJson: (...args) => apiJsonMock(...args),
}));

/** The `runtime` block exactly as `config/staleness.py` builds it. */
const STALE = {
  running_version: '2026.8.21',
  installed_version: '2026.8.25',
  stale: true,
  uptime_s: 179460.0,
  pid: 12061,
  detail: 'This brain is running 2026.8.21 but 2026.8.25 is installed. '
    + 'A running process never reloads its code, so the upgrade has not '
    + 'taken effect. Restart the brain (`feral restart`, or stop and '
    + 're-run `feral serve`) to pick it up. Uptime 2d 1h.',
};

const HEALTHY = {
  running_version: '2026.8.25',
  installed_version: '2026.8.25',
  stale: false,
  uptime_s: 12.0,
  pid: 12061,
  detail: 'Running the installed version (2026.8.25).',
};

/**
 * Render inside a `.v2-shell` element, because that is where the strip
 * reserves its height and the real shell always provides one.
 */
function renderInShell() {
  const shell = document.createElement('div');
  shell.className = 'v2-shell';
  document.body.appendChild(shell);
  const utils = render(<RuntimeNotice />, { container: shell });
  return { shell, ...utils };
}

describe('runtimeNotices', () => {
  it('reports the stale build, with the remedy and both versions', () => {
    const [notice, ...rest] = runtimeNotices(STALE);
    expect(rest).toEqual([]);
    expect(notice.id).toBe('stale-build');
    expect(notice.headline).toMatch(/restart/i);
    expect(notice.command).toBe('feral restart');
    expect(notice.detail).toBe('Running 2026.8.21, but 2026.8.25 is installed. Up 2d 1h.');
  });

  it('says nothing when the brain is running what is installed', () => {
    expect(runtimeNotices(HEALTHY)).toEqual([]);
  });

  it('says nothing when the runtime block is absent or unusable', () => {
    // Every shape a client can actually be handed: an old brain with no
    // `runtime` key at all, the route's own failure envelope, and junk.
    for (const value of [
      undefined, null, {}, [], 'stale', 42, true,
      { detail: 'unavailable', stale: false },
      { stale: 'true' },
      { stale: 1 },
    ]) {
      expect(runtimeNotices(value), JSON.stringify(value)).toEqual([]);
    }
  });

  it('falls back to the brain sentence when the versions are unreadable', () => {
    // `stale` without two usable version strings must still say
    // something: a headline over an empty line is worse than the
    // brain's own wording.
    const [notice] = runtimeNotices({
      stale: true, running_version: '', installed_version: null,
      detail: 'Restart the brain to pick it up.',
    });
    expect(notice.detail).toBe('Restart the brain to pick it up.');
  });

  it('drops the uptime clause rather than printing a bad number', () => {
    for (const bad of [undefined, null, 'soon', NaN, -5]) {
      const [notice] = runtimeNotices({ ...STALE, uptime_s: bad });
      expect(notice.detail).toBe('Running 2026.8.21, but 2026.8.25 is installed.');
    }
  });
});

describe('humanUptime', () => {
  it('matches the brain formatting', () => {
    // Same buckets as config/staleness._human_uptime.
    expect(humanUptime(179460)).toBe('2d 1h');
    expect(humanUptime(11700)).toBe('3h 15m');
    expect(humanUptime(720)).toBe('12m');
    // A real zero is a reading and prints.
    expect(humanUptime(0)).toBe('0m');
  });

  it('does not turn a missing uptime into "0m"', () => {
    // `Number(null)` is 0, and a coercing check therefore reports a
    // brain that has been up no time at all when the payload simply did
    // not carry the field. Caught by this file before it shipped.
    for (const missing of [null, undefined, '', '720', {}, NaN, -1]) {
      expect(humanUptime(missing), String(missing)).toBe('');
    }
  });
});

describe('<RuntimeNotice /> against the shared dashboard store', () => {
  beforeEach(() => {
    _resetSystemHealthForTesting();
    apiJsonMock.mockReset();
  });

  afterEach(() => {
    document.body.innerHTML = '';
  });

  it('renders the remedy, the versions and a copy control when stale', async () => {
    apiJsonMock.mockResolvedValue({ runtime: STALE });
    renderInShell();

    const strip = await screen.findByTestId('runtime-notice');
    // A status message, so assistive tech announces it without the
    // interruption an alert would cause.
    expect(strip).toHaveAttribute('role', 'status');
    expect(strip.textContent).toMatch(/Restart FERAL to finish updating/);
    expect(strip.textContent).toMatch(/feral restart/);
    expect(strip.textContent).toMatch(/2026\.8\.21/);
    expect(strip.textContent).toMatch(/2026\.8\.25/);

    // The one control is a real button, so it is in the tab order.
    const copy = screen.getByTestId('runtime-notice-copy');
    expect(copy.tagName).toBe('BUTTON');
    expect(copy).toHaveAttribute('aria-label', 'Copy feral restart');

    // And there is no way to dismiss it, because the condition does not
    // clear until somebody restarts the brain.
    expect(screen.queryByRole('button', { name: /dismiss|close|hide/i })).toBeNull();
  });

  it('renders NOTHING at all when the brain is not stale', async () => {
    apiJsonMock.mockResolvedValue({ runtime: HEALTHY });
    const { shell } = renderInShell();
    await waitFor(() => expect(apiJsonMock).toHaveBeenCalled());

    // Not "an empty container", not "a hidden chip". No element.
    expect(screen.queryByTestId('runtime-notice')).toBeNull();
    expect(shell.querySelector('.v2-runtime-notice')).toBeNull();
    expect(shell.textContent).toBe('');
  });

  it('renders nothing when the payload has no runtime block', async () => {
    apiJsonMock.mockResolvedValue({ session_count: 3 });
    const { shell } = renderInShell();
    await waitFor(() => expect(apiJsonMock).toHaveBeenCalled());
    expect(shell.querySelector('.v2-runtime-notice')).toBeNull();
  });

  it('survives the dashboard call failing outright', async () => {
    apiJsonMock.mockRejectedValue(new Error('brain is down'));
    const { shell } = renderInShell();
    await waitFor(() => expect(apiJsonMock).toHaveBeenCalled());
    expect(shell.querySelector('.v2-runtime-notice')).toBeNull();
  });

  it('adds no second poll: it rides the shell dashboard store', async () => {
    apiJsonMock.mockResolvedValue({ runtime: STALE });
    renderInShell();
    await screen.findByTestId('runtime-notice');
    expect(apiJsonMock).toHaveBeenCalledTimes(1);
    expect(apiJsonMock.mock.calls[0][0]).toBe('/api/dashboard');
  });

  it('reserves its height on the shell, and gives it back', async () => {
    // The reservation is what keeps the strip off the top of the page
    // instead of on top of it. jsdom cannot prove the pixels, only that
    // the row count crosses over and is cleaned up; the pixels are
    // e2e/runtime_notice.spec.ts.
    apiJsonMock.mockResolvedValue({ runtime: STALE });
    const { shell, unmount } = renderInShell();
    await screen.findByTestId('runtime-notice');
    expect(shell.style.getPropertyValue('--v2-runtime-notice-rows')).toBe('1');

    // A restart makes the brain healthy again on the next tick, and the
    // page must get its 28px back.
    apiJsonMock.mockResolvedValue({ runtime: HEALTHY });
    await act(async () => { await refreshSystemHealth(); });
    await waitFor(() => expect(screen.queryByTestId('runtime-notice')).toBeNull());
    expect(shell.style.getPropertyValue('--v2-runtime-notice-rows')).toBe('');

    apiJsonMock.mockResolvedValue({ runtime: STALE });
    await act(async () => { await refreshSystemHealth(); });
    await screen.findByTestId('runtime-notice');
    expect(shell.style.getPropertyValue('--v2-runtime-notice-rows')).toBe('1');

    unmount();
    expect(shell.style.getPropertyValue('--v2-runtime-notice-rows')).toBe('');
  });
});
