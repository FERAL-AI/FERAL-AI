/**
 * Voice must not take the page away from you.
 *
 * The approved design puts voice in the composer row: the text field is
 * replaced in place by a pill, with mute and end as separate controls
 * ("The mic starts voice; mute and end are separate").
 *
 * What shipped was a fixed overlay at z-index 200 whose fullscreen
 * variant dimmed the page with filter: brightness(0.4) and set
 * pointer-events: none on the dock. Starting voice hid the machine at
 * the moment you would most want to watch it.
 */
import fs from 'node:fs';
import path from 'node:path';
import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';

import VoiceLane, { meterBars, laneLabel } from '../../components/VoiceLane';

const UI_CSS = path.resolve(__dirname, '../../styles/ui.css');

describe('the lane reports state rather than just "on"', () => {
  it.each([
    [{ phase: 'listening' }, 'Listening'],
    [{ phase: 'processing' }, 'Thinking'],
    [{ phase: 'speaking' }, 'Speaking'],
    [{ state: 'degraded' }, 'Voice paused'],
    [{ muted: true }, 'Muted'],
  ])('says %o -> %s', (input, expected) => {
    expect(laneLabel(input)).toBe(expected);
  });

  it('muted wins over the phase, because that is what the user did', () => {
    expect(laneLabel({ phase: 'listening', muted: true })).toBe('Muted');
  });
});

describe('the meter', () => {
  it('fills proportionally and clamps', () => {
    expect(meterBars(0)).toEqual([false, false, false, false, false]);
    expect(meterBars(1)).toEqual([true, true, true, true, true]);
    expect(meterBars(0.6).filter(Boolean).length).toBe(3);
  });

  it('treats nonsense as silence rather than throwing', () => {
    expect(meterBars(undefined).some(Boolean)).toBe(false);
    expect(meterBars(NaN).some(Boolean)).toBe(false);
    expect(meterBars(-5).some(Boolean)).toBe(false);
    expect(meterBars(99).every(Boolean)).toBe(true);
  });
});

describe('mute and end are separate controls', () => {
  it('offers both, and they do different things', () => {
    const onMute = vi.fn();
    const onEnd = vi.fn();
    render(<VoiceLane voice={{ phase: 'listening' }} onMute={onMute} onEnd={onEnd} />);

    fireEvent.click(screen.getByLabelText('Mute the microphone'));
    expect(onMute).toHaveBeenCalledTimes(1);
    expect(onEnd).not.toHaveBeenCalled();

    fireEvent.click(screen.getByLabelText('End the voice session'));
    expect(onEnd).toHaveBeenCalledTimes(1);
  });

  it('the mute control announces its pressed state', () => {
    const { rerender } = render(<VoiceLane voice={{}} muted={false} onMute={() => {}} onEnd={() => {}} />);
    expect(screen.getByLabelText('Mute the microphone').getAttribute('aria-pressed')).toBe('false');
    rerender(<VoiceLane voice={{}} muted onMute={() => {}} onEnd={() => {}} />);
    expect(screen.getByLabelText('Unmute the microphone').getAttribute('aria-pressed')).toBe('true');
  });

  it('surfaces a pipeline error instead of swallowing it', () => {
    render(<VoiceLane voice={{ phase: 'error', phaseError: 'piper voice missing' }} onMute={() => {}} onEnd={() => {}} />);
    expect(screen.getByText('piper voice missing')).toBeInTheDocument();
  });
});

describe('the page is never taken away', () => {
  const css = fs.readFileSync(UI_CSS, 'utf8');

  it('nothing dims the main area when voice is on', () => {
    // filter: brightness(0.4) on .v2-shell-main was the takeover.
    expect(css).not.toMatch(/is-voice-mode[^{]*\.v2-shell-main\s*\{[^}]*filter:\s*brightness/);
  });

  it('the dock stays interactive when voice is on', () => {
    expect(css).not.toMatch(/is-voice-mode[^{]*\.v2-dock\s*\{[^}]*pointer-events:\s*none/);
  });
});
