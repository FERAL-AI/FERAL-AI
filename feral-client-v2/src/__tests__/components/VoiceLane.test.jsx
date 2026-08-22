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

/*
 * What used to be here were two greps over ui.css looking for the two
 * exact selectors the takeover was originally written with:
 *
 *   /is-voice-mode[^{]*\.v2-shell-main\s*\{[^}]*filter:\s*brightness/
 *   /is-voice-mode[^{]*\.v2-dock\s*\{[^}]*pointer-events:\s*none/
 *
 * Both rules had already been deleted from the stylesheet, so both
 * assertions were `expect(css).not.toMatch(<something absent>)` and
 * could never fail for any reason. They passed on every run while the
 * behaviour they were named after came back through a different door:
 * Expand still produced role="dialog" aria-modal="true" at inset: 0 and
 * z-index 200 over a scrim, covering the whole viewport, and a real
 * click on a dock tile timed out underneath it.
 *
 * That is CLAUDE.md trap 3 exactly. A test pinned to the shape of one
 * old fix tests that one fix, not the property. These test the property:
 * the takeover is reachable only on purpose, and it is always escapable.
 */
describe('the page is never taken away', () => {
  const overlay = () => document.querySelector('.v2-voice-overlay');

  const renderOverlay = async (voice = {}) => {
    vi.resetModules();
    vi.doMock('../../shell/VoiceContext', () => ({
      useVoice: () => ({ active: true, state: 'active', stop: () => {}, ...voice }),
      VoiceProvider: ({ children }) => children,
    }));
    const { default: VoiceOverlay } = await import('../../shell/VoiceOverlay');
    render(<VoiceOverlay />);
  };

  it('starts docked, so voice never takes the viewport unbidden', async () => {
    await renderOverlay();
    expect(overlay().getAttribute('data-variant')).toBe('docked');
    // A pill is not a dialog and must not claim the page is inert.
    expect(overlay().getAttribute('role')).toBe('region');
    expect(overlay().getAttribute('aria-modal')).toBeNull();
  });

  it('reaches fullscreen only by clicking Expand', async () => {
    await renderOverlay();
    fireEvent.click(screen.getByLabelText('Expand voice'));
    expect(overlay().getAttribute('data-variant')).toBe('fullscreen');
    expect(overlay().getAttribute('aria-modal')).toBe('true');
  });

  it('Escape leaves fullscreen, so the dock is never unreachable', async () => {
    await renderOverlay();
    fireEvent.click(screen.getByLabelText('Expand voice'));
    expect(overlay().getAttribute('data-variant')).toBe('fullscreen');

    fireEvent.keyDown(window, { key: 'Escape' });
    expect(
      overlay().getAttribute('data-variant'),
      'fullscreen covers the dock at inset: 0 and declared itself modal '
      + 'with no keyboard way out',
    ).toBe('docked');
  });

  it('Minimize leaves fullscreen too', async () => {
    await renderOverlay();
    fireEvent.click(screen.getByLabelText('Expand voice'));
    fireEvent.click(screen.getByLabelText('Minimize voice'));
    expect(overlay().getAttribute('data-variant')).toBe('docked');
  });

  it('ending a session drops fullscreen, so the next start is not a takeover', async () => {
    await renderOverlay();
    fireEvent.click(screen.getByLabelText('Expand voice'));
    expect(overlay().getAttribute('data-variant')).toBe('fullscreen');
    // The component resets to docked whenever it goes inactive.
    expect(fs.readFileSync(
      path.resolve(__dirname, '../../shell/VoiceOverlay.jsx'), 'utf8',
    )).toMatch(/if \(!visible\) setVariant\('docked'\)/);
  });
});

describe('one session, one way to end it', () => {
  it('the overlay drops its End when a lane is on screen', async () => {
    // Three ways to stop a session were visible at once: this overlay's
    // "End voice", the composer lane's end button, and the system bar's
    // global toggle. The lane's sits directly under the field you are
    // looking at, so the overlay's is the redundant one.
    vi.resetModules();
    vi.doMock('../../shell/VoiceContext', () => ({
      useVoice: () => ({
        active: true, state: 'active', stop: () => {}, laneMounted: true,
      }),
      useRegisterVoiceLane: () => {},
      VoiceProvider: ({ children }) => children,
    }));
    const { default: VoiceOverlay } = await import('../../shell/VoiceOverlay');
    render(<VoiceOverlay />);

    expect(screen.queryByText('End voice')).toBeNull();
    // Expand is a different action with no lane equivalent, so it stays.
    expect(screen.getByLabelText('Expand voice')).toBeInTheDocument();
  });

  it('keeps its End on every surface that has no lane', async () => {
    // The lane renders only in the chat composer. The overlay is the
    // only voice surface on every other route, so suppressing this
    // unconditionally would leave those with no way out at all.
    vi.resetModules();
    vi.doMock('../../shell/VoiceContext', () => ({
      useVoice: () => ({
        active: true, state: 'active', stop: () => {}, laneMounted: false,
      }),
      useRegisterVoiceLane: () => {},
      VoiceProvider: ({ children }) => children,
    }));
    const { default: VoiceOverlay } = await import('../../shell/VoiceOverlay');
    render(<VoiceOverlay />);

    expect(screen.getByText('End voice')).toBeInTheDocument();
  });

  it('keeps its End in fullscreen even with a lane, because the lane is covered', async () => {
    vi.resetModules();
    vi.doMock('../../shell/VoiceContext', () => ({
      useVoice: () => ({
        active: true, state: 'active', stop: () => {}, laneMounted: true,
      }),
      useRegisterVoiceLane: () => {},
      VoiceProvider: ({ children }) => children,
    }));
    const { default: VoiceOverlay } = await import('../../shell/VoiceOverlay');
    render(<VoiceOverlay />);

    fireEvent.click(screen.getByLabelText('Expand voice'));
    expect(screen.getByText('End voice')).toBeInTheDocument();
  });

  it('renders standalone, with no Router', async () => {
    // The first version read useLocation().pathname to decide this,
    // which is a different question that happens to correlate, and it
    // made the overlay unrenderable outside a Router: eight standalone
    // tests failed at once. The dependency is the lane, not the URL.
    vi.resetModules();
    vi.doMock('../../shell/VoiceContext', () => ({
      useVoice: () => ({ active: true, state: 'active', stop: () => {} }),
      useRegisterVoiceLane: () => {},
      VoiceProvider: ({ children }) => children,
    }));
    const { default: VoiceOverlay } = await import('../../shell/VoiceOverlay');
    expect(() => render(<VoiceOverlay />)).not.toThrow();
  });
});
