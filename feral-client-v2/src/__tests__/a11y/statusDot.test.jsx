/**
 * StatusDot: accessible name and the non-colour channel.
 *
 * Two independent defects lived in this component:
 *
 *  1. `role={label ? 'status' : 'presentation'}`. Roughly 20 of ~35 call
 *     sites passed no label, so for those the dot declared itself
 *     decorative and the state it carried was deleted from the
 *     accessibility tree entirely.
 *  2. `--live`, `--warn`, `--error` and `--neutral` were the same 8px
 *     filled circle in four hues, so the state was unreadable to anyone
 *     with a red/green deficiency.
 *
 * These tests fail against `git show HEAD:src/ui/StatusDot.jsx` and
 * `HEAD:src/styles/ui.css` respectively.
 */
import fs from 'node:fs';
import path from 'node:path';
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render } from '@testing-library/react';
import StatusDot, { __resetStatusDotWarnings } from '../../ui/StatusDot';

const UI_CSS = fs.readFileSync(
  path.resolve(__dirname, '../../styles/ui.css'),
  'utf8',
);

afterEach(() => {
  vi.restoreAllMocks();
  // Optional call so that running this file against a StatusDot that does
  // not export the seam (an older copy, e.g. when checking these tests are
  // not vacuous) fails on the real assertions rather than in teardown.
  __resetStatusDotWarnings?.();
});

describe('StatusDot accessible name', () => {
  it('never renders as presentational, even with no label', () => {
    const { container } = render(<StatusDot tone="live" />);
    const dot = container.querySelector('.v2-dot');
    expect(dot).toBeTruthy();
    expect(dot.getAttribute('role')).not.toBe('presentation');
    expect(dot.getAttribute('role')).not.toBe('none');
    expect(dot.getAttribute('aria-hidden')).not.toBe('true');
  });

  it('exposes a non-empty accessible name for every tone with no label', () => {
    for (const tone of ['live', 'warn', 'error', 'neutral', 'off']) {
      const { container, unmount } = render(<StatusDot tone={tone} />);
      const dot = container.querySelector('.v2-dot');
      const name = dot.getAttribute('aria-label');
      expect(name, `tone="${tone}" produced no accessible name`).toBeTruthy();
      expect(name.trim().length).toBeGreaterThan(0);
      unmount();
    }
  });

  it('prefers the caller-supplied label over the tone fallback', () => {
    const { container } = render(<StatusDot tone="live" label="Brain connected" />);
    expect(container.querySelector('.v2-dot').getAttribute('aria-label'))
      .toBe('Brain connected');
  });

  it('warns in dev when a label is missing, and does not throw', () => {
    const spy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    expect(() => render(<StatusDot tone="error" />)).not.toThrow();
    expect(spy).toHaveBeenCalled();
    expect(String(spy.mock.calls[0][0])).toContain('StatusDot');
  });

  it('does not warn when a label is supplied', () => {
    const spy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    render(<StatusDot tone="error" label="Sync failed" />);
    expect(spy).not.toHaveBeenCalled();
  });

  it('warns once per tone rather than once per row', () => {
    const spy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    render(
      <div>
        <StatusDot tone="warn" />
        <StatusDot tone="warn" />
        <StatusDot tone="warn" />
      </div>,
    );
    expect(spy).toHaveBeenCalledTimes(1);
  });
});

describe('StatusDot second channel', () => {
  /**
   * Pull the declaration block for a selector out of ui.css. Deliberately
   * a text assertion and not getComputedStyle: jsdom does not load the
   * page stylesheet at all, so a computed-style check here would pass
   * vacuously no matter what the CSS said.
   */
  function block(selector) {
    const re = new RegExp(`\\${selector}\\s*\\{([^}]*)\\}`);
    const m = UI_CSS.match(re);
    return m ? m[1] : null;
  }

  const TONES = ['live', 'warn', 'error', 'neutral', 'off'];

  it('gives every tone a rule of its own', () => {
    for (const tone of TONES) {
      expect(block(`.v2-dot--${tone}`), `.v2-dot--${tone} has no rule`).toBeTruthy();
    }
  });

  it('distinguishes tones by more than hue', () => {
    // A tone qualifies as carrying a second channel if its rule changes
    // the silhouette: a clip-path, a border-radius override, or a border.
    const shapeProps = /clip-path|border-radius|border\s*:/;
    const shaped = TONES.filter((t) => shapeProps.test(block(`.v2-dot--${t}`) || ''));

    // `live` is the baseline circle from .v2-dot, so it is allowed to
    // declare no shape of its own. Every other tone must differ from it.
    const mustDiffer = TONES.filter((t) => t !== 'live');
    for (const tone of mustDiffer) {
      expect(
        shaped.includes(tone),
        `.v2-dot--${tone} differs from .v2-dot--live by colour alone`,
      ).toBe(true);
    }
  });

  it('gives warn and error different silhouettes from each other', () => {
    const warn = block('.v2-dot--warn');
    const error = block('.v2-dot--error');
    const clip = (s) => (s.match(/clip-path:\s*([^;]+)/) || [])[1];
    expect(clip(warn)).toBeTruthy();
    expect(clip(error)).toBeTruthy();
    expect(clip(warn)).not.toBe(clip(error));
  });

  it('stops the pulse under prefers-reduced-motion without dimming the dot', () => {
    // The keyframe declares its own 1.6s duration, so zeroing the
    // --v2-dur-* tokens does not reach it. There has to be an explicit
    // override.
    const reduced = UI_CSS.match(
      /@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{([\s\S]*?)\n\}/g,
    ) || [];
    const dotRule = reduced.find((b) => b.includes('.v2-dot.is-pulse'));
    expect(dotRule, 'no reduced-motion override for .v2-dot.is-pulse').toBeTruthy();
    expect(dotRule).toMatch(/animation:\s*none/);
    expect(dotRule).toMatch(/opacity:\s*1/);
  });
});
