/**
 * ErrorState: "we could not ask" must not read like "there is
 * nothing", and a 401 must not read like a 502.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, fireEvent } from '@testing-library/react';
import ErrorState, { describeApiError } from '../../ui/ErrorState';
import EmptyState from '../../ui/EmptyState';
import { ApiError } from '../../lib/api';

const err = (fields) => new ApiError({ path: '/api/x', ...fields });

describe('describeApiError', () => {
  it('splits auth failures from server faults', () => {
    expect(describeApiError(err({ status: 401 })).kind).toBe('auth');
    expect(describeApiError(err({ status: 403 })).kind).toBe('forbidden');
    expect(describeApiError(err({ status: 404 })).kind).toBe('missing');
    expect(describeApiError(err({ status: 429 })).kind).toBe('busy');
    expect(describeApiError(err({ status: 500 })).kind).toBe('server');
    expect(describeApiError(err({ status: 502 })).kind).toBe('server');
    expect(describeApiError(err({ status: 0, code: 'network' })).kind).toBe('offline');
    expect(describeApiError(err({ status: 418 })).kind).toBe('request');
  });

  it('tells the user what to do about a 401 rather than restating the number', () => {
    const info = describeApiError(err({ status: 401 }), 'health alerts');
    expect(info.title).toMatch(/health alerts/);
    expect(info.hint).toMatch(/API key/i);
    expect(info.hint).toMatch(/Settings/i);
  });

  it('says a 5xx is the brain\'s fault, not the data\'s', () => {
    const info = describeApiError(err({ status: 503, detail: 'upstream gone' }), 'devices');
    expect(info.hint).toMatch(/fault inside the brain/i);
    expect(info.hint).toMatch(/upstream gone/);
  });

  it('says a 404 is a version disagreement, not missing data', () => {
    const info = describeApiError(err({ status: 404 }), 'skills');
    expect(info.hint).toMatch(/Nothing is wrong with your data/i);
  });
});

describe('<ErrorState />', () => {
  it('never looks like an EmptyState', () => {
    const { container: e } = render(<EmptyState title="No skills loaded" />);
    const { container: x } = render(
      <ErrorState error={err({ status: 500 })} what="the skill list" />,
    );
    expect(e.querySelector('.v2-empty-state')).not.toBeNull();
    expect(e.querySelector('[role="alert"]')).toBeNull();
    expect(x.querySelector('.v2-empty-state')).toBeNull();
    expect(x.querySelector('[role="alert"]')).not.toBeNull();
    expect(x.textContent).toMatch(/not an empty result/i);
  });

  it('surfaces status, code and reason, none of which anything read before', () => {
    const { container } = render(
      <ErrorState
        error={err({ status: 502, code: 'upstream', reason: 'llm_down' })}
        what="the skill list"
      />,
    );
    expect(container.textContent).toMatch(/HTTP 502/);
    expect(container.textContent).toMatch(/upstream/);
    expect(container.textContent).toMatch(/llm_down/);
  });

  it('keeps the derived diagnosis when the caller supplies its own hint', () => {
    const { container } = render(
      <ErrorState
        error={err({ status: 401 })}
        what="your devices"
        hint="Nothing has been unpaired."
      />,
    );
    expect(container.textContent).toMatch(/Nothing has been unpaired/);
    expect(container.textContent).toMatch(/401 unauthorized/i);
  });

  it('wires onRetry to a button', () => {
    const onRetry = vi.fn();
    const { getByRole } = render(
      <ErrorState error={err({ status: 500 })} what="x" onRetry={onRetry} />,
    );
    fireEvent.click(getByRole('button', { name: /Retry/i }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it('tones an auth failure differently from a server fault', () => {
    const { container: a } = render(<ErrorState error={err({ status: 403 })} what="x" />);
    const { container: b } = render(<ErrorState error={err({ status: 500 })} what="x" />);
    expect(a.firstChild.getAttribute('data-tone')).toBe('warn');
    expect(b.firstChild.getAttribute('data-tone')).toBe('error');
  });
});
