/**
 * PlanModeBanner.
 *
 * The brain has emitted a `plan_mode` frame since plan mode shipped and
 * nothing in this client consumed it, so a user in plan mode saw their
 * mutating tool calls refused with no indication why. These pin the
 * consumer: the banner appears on the enter frame, disappears on the
 * exit frame, and treats each frame as a full snapshot rather than a
 * delta (which is what the brain actually sends).
 *
 * Payload shape is copied from a live brain, not from the source:
 * agents/orchestrator.py::_emit_plan_mode_frame forwards
 * PlanModeState.describe, and adds `approved` on exit.
 */
import React from 'react';
import { render, screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import PlanModeBanner, { normalizePlanMode } from '../../components/PlanModeBanner';

// Captured verbatim from a live run against a brain on 127.0.0.1:9399.
const ENTER_FRAME = {
  session_id: 'live-plan-1',
  plan_mode: true,
  entered_at: 1785623644.303635,
  reason: '',
  entered_by: 'user',
  plan_count: 0,
  latest_plan: null,
};

const EXIT_FRAME = {
  session_id: 'live-plan-1',
  plan_mode: false,
  entered_at: null,
  reason: '',
  entered_by: '',
  plan_count: 0,
  latest_plan: null,
  approved: true,
};

describe('PlanModeBanner', () => {
  it('renders nothing before any frame arrives', () => {
    const { container } = render(<PlanModeBanner state={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('shows the banner on the live enter frame', () => {
    render(<PlanModeBanner state={ENTER_FRAME} />);
    const banner = screen.getByTestId('plan-mode-banner');
    expect(banner).toBeInTheDocument();
    expect(banner).toHaveTextContent('Plan mode');
  });

  it('tells the user how to leave, since the model cannot', () => {
    render(<PlanModeBanner state={ENTER_FRAME} />);
    const banner = screen.getByTestId('plan-mode-banner');
    expect(banner).toHaveTextContent('/plan approve');
    expect(banner).toHaveTextContent('/plan off');
  });

  it('clears on the live exit frame rather than latching on', () => {
    const { container, rerender } = render(<PlanModeBanner state={ENTER_FRAME} />);
    expect(screen.getByTestId('plan-mode-banner')).toBeInTheDocument();
    rerender(<PlanModeBanner state={EXIT_FRAME} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('surfaces the reason when /plan on <reason> supplied one', () => {
    render(<PlanModeBanner state={{ ...ENTER_FRAME, reason: 'refactor the store' }} />);
    expect(screen.getByTestId('plan-mode-reason')).toHaveTextContent('refactor the store');
  });

  it('omits the reason line when there is none', () => {
    render(<PlanModeBanner state={ENTER_FRAME} />);
    expect(screen.queryByTestId('plan-mode-reason')).toBeNull();
  });

  it('shows the submitted-plan count once a plan exists', () => {
    render(<PlanModeBanner state={{ ...ENTER_FRAME, plan_count: 2 }} />);
    expect(screen.getByTestId('plan-mode-count')).toHaveTextContent('2 submitted');
  });

  it('hides the count at zero instead of rendering "0 submitted"', () => {
    render(<PlanModeBanner state={ENTER_FRAME} />);
    expect(screen.queryByTestId('plan-mode-count')).toBeNull();
  });
});

describe('normalizePlanMode', () => {
  it('treats a missing or malformed payload as "not in plan mode"', () => {
    expect(normalizePlanMode(null)).toBeNull();
    expect(normalizePlanMode(undefined)).toBeNull();
    expect(normalizePlanMode('on')).toBeNull();
    expect(normalizePlanMode({})).toBeNull();
  });

  it('requires plan_mode === true, not merely truthy', () => {
    // The REST route and the frame both send a real boolean. Anything
    // else is a shape we do not recognise, and guessing "on" would show
    // a banner claiming the agent is restricted when it is not.
    expect(normalizePlanMode({ plan_mode: 'true' })).toBeNull();
    expect(normalizePlanMode({ plan_mode: 1 })).toBeNull();
    expect(normalizePlanMode({ plan_mode: false })).toBeNull();
  });

  it('keeps the fields the banner renders', () => {
    expect(normalizePlanMode(ENTER_FRAME)).toEqual({
      reason: '',
      enteredBy: 'user',
      planCount: 0,
    });
  });

  it('tolerates an older brain that omits the newer fields', () => {
    expect(normalizePlanMode({ plan_mode: true })).toEqual({
      reason: '',
      enteredBy: '',
      planCount: 0,
    });
  });
});
