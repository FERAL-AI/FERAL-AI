/**
 * The smaller findings from the accessibility pass, each of which is a
 * single wrong or missing attribute:
 *
 *  - ErrorToast set role="alert" and aria-live="polite" on one node. Those
 *    contradict: `alert` carries an implicit assertive live region.
 *  - Menubar rendered the app's only global brain-connection indicator as
 *    a bare coloured span with aria-hidden="true" and no text.
 *  - SduiRenderer's ProgressBar was two anonymous divs, with no
 *    role="progressbar" and no aria-valuenow.
 *
 * Each fails against the corresponding `git show HEAD:` copy.
 */
import React from 'react';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { renderV2 } from '../_helpers/renderV2';
import ErrorToast from '../../components/ErrorToast';
import Menubar from '../../shell/Menubar';
import { SduiNode } from '../../ui/SduiRenderer';
import { pushGlobalError, _resetGlobalErrorsForTesting } from '../../hooks/useGlobalErrors';
import { ApiError } from '../../lib/api';

describe('ErrorToast live region', () => {
  beforeEach(() => {
    _resetGlobalErrorsForTesting();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('does not declare a live-region politeness that contradicts its role', () => {
    pushGlobalError(new ApiError({ detail: 'boom', path: '/x' }));
    render(<ErrorToast />);
    const stack = screen.getByTestId('error-toast-stack');
    const role = stack.getAttribute('role');
    const live = stack.getAttribute('aria-live');

    // role="alert" implies aria-live="assertive". Declaring "polite"
    // alongside it leaves the announcement behaviour up to whichever the
    // screen reader resolves last.
    if (role === 'alert' && live !== null) {
      expect(live).toBe('assertive');
    }
  });
});

describe('Menubar connection indicator', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('exposes the brain connection state to assistive tech', () => {
    const { container } = renderV2(<Menubar />);
    const indicator = container.querySelector('.v2-menubar-left [aria-label]');
    expect(indicator, 'the connection indicator has no accessible name').toBeTruthy();
    expect(indicator.getAttribute('aria-label')).toMatch(/brain/i);
    expect(indicator.getAttribute('aria-hidden')).not.toBe('true');
  });

  it('carries the shape channel, not just a background colour', () => {
    const { container } = renderV2(<Menubar />);
    const indicator = container.querySelector('.v2-menubar-left [aria-label]');
    // A .v2-dot--* tone class is what binds it to the per-tone silhouette
    // in ui.css. The old markup set an inline background colour instead,
    // which is a hue and nothing else.
    expect(indicator.className).toMatch(/v2-dot--(live|warn|error|neutral|off)/);
    expect(indicator.getAttribute('style') || '').not.toMatch(/background/);
  });
});

describe('SduiRenderer ProgressBar', () => {
  const tree = { type: 'ProgressBar', label: 'Indexing memories', value: 0.42 };

  it('is announced as a progress bar carrying its measured value', () => {
    const { container } = render(<SduiNode node={tree} />);
    const bar = container.querySelector('[role="progressbar"]');
    expect(bar, 'ProgressBar renders no progressbar role').toBeTruthy();
    expect(bar.getAttribute('aria-valuenow')).toBe('42');
    expect(bar.getAttribute('aria-valuemin')).toBe('0');
    expect(bar.getAttribute('aria-valuemax')).toBe('100');
  });

  it('has an accessible name even with no label on the node', () => {
    const { container } = render(<SduiNode node={{ type: 'ProgressBar', value: 0.1 }} />);
    const bar = container.querySelector('[role="progressbar"]');
    expect(bar.getAttribute('aria-label')).toBeTruthy();
  });

  it('clamps out-of-range values rather than reporting them', () => {
    const { container } = render(<SduiNode node={{ type: 'ProgressBar', value: 7 }} />);
    expect(container.querySelector('[role="progressbar"]').getAttribute('aria-valuenow')).toBe('100');
  });

  it('draws its track from a theme token, so it survives light mode', () => {
    const { container } = render(<SduiNode node={tree} />);
    const bar = container.querySelector('[role="progressbar"]');
    const style = bar.getAttribute('style') || '';
    expect(style).toMatch(/var\(--v2-/);
    // A raw white alpha is invisible on a light background.
    expect(style).not.toMatch(/rgba\(\s*255\s*,\s*255\s*,\s*255/);
  });
});
