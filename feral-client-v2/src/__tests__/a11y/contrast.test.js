/**
 * Colour contrast, WCAG 2.1 AA for text, computed rather than eyeballed.
 *
 * These read the real token values out of styles/tokens.css and compute
 * the contrast ratio, so the numbers in the token comments cannot drift
 * away from the numbers the tokens actually produce.
 *
 * Resolution goes through ../_helpers/tokens, which applies the cascade
 * for a named theme state and follows the var() chain down to the
 * palette. It used to index occurrences of a token positionally
 * (0 = dark, 1 = light, 2 = the media duplicate), which only worked
 * while every token was a hex literal repeated in a fixed order. Since
 * the sheet was split into a palette layer and a semantic layer there
 * are no hexes at all below the palette, and position no longer implies
 * theme.
 *
 * Backgrounds. Dark mode text sits on a translucent glass panel over the
 * shell gradient, so the effective background is the panel composited
 * over #1F1F27 (the top stop of --v2-shell-ambient). All four dark
 * surfaces are computed below and the worst one has to pass, because
 * --v2-text-tertiary is used on every one of them. Light mode composites
 * *towards white*, so the un-glassed #F2F3F8 is the conservative end and
 * is used directly.
 */
import { describe, it, expect } from 'vitest';

import {
  colorIn,
  composite,
  contrast,
  parseHex,
  resolveIn,
  toHex,
} from '../_helpers/tokens';

const AA = 4.5;
const DARK_SHELL = '#1F1F27';
const LIGHT_SHELL = '#F2F3F8';

/** Opaque hex of a token in a state, alpha discarded. */
const hexOf = (state, name) => toHex(colorIn(state, name));

/** Every opaque background --v2-text-tertiary can land on in dark mode. */
function darkBackgrounds() {
  const out = { 'shell base': DARK_SHELL };
  for (const key of ['surface-0', 'surface-1', 'surface-2', 'surface-elev']) {
    out[key] = toHex(composite(colorIn('darkMedia', `--v2-${key}`), DARK_SHELL));
  }
  return out;
}

describe('WCAG AA, dark theme', () => {
  it('--v2-text-tertiary clears 4.5:1 on every dark surface', () => {
    const fg = hexOf('darkMedia', '--v2-text-tertiary');
    expect(fg).toMatch(/^#[0-9A-Fa-f]{6}$/);
    const failures = [];
    for (const [name, bg] of Object.entries(darkBackgrounds())) {
      const ratio = contrast(fg, bg);
      if (ratio < AA) failures.push(`${name} (${bg}): ${ratio.toFixed(2)}:1`);
    }
    expect(
      failures,
      `--v2-text-tertiary ${fg} is below AA on: ${failures.join(', ')}`,
    ).toEqual([]);
  });

  it('--v2-text-secondary and --v2-text-primary clear 4.5:1 on the shell base', () => {
    for (const name of ['--v2-text-secondary', '--v2-text-primary']) {
      const ratio = contrast(hexOf('darkMedia', name), DARK_SHELL);
      expect(ratio, `${name} measured ${ratio.toFixed(2)}:1`).toBeGreaterThanOrEqual(AA);
    }
  });
});

describe('WCAG AA, light theme', () => {
  const cases = [
    ['--v2-text-tertiary', '.v2-device-meta / .v2-p--tiny / .v2-btn--ghost'],
    ['--v2-text-secondary', '.v2-p--muted'],
    ['--v2-state-live', '.v2-chip--live / .v2-chip--loose'],
    ['--v2-state-warn', '.v2-chip--warn / .v2-chip--strict'],
    ['--v2-state-error', '.v2-chip--error'],
  ];

  it.each(cases)('%s clears 4.5:1 on the light shell (%s)', (token) => {
    const rgba = colorIn('light', token);
    expect(rgba, `${token} does not resolve to a colour in the light state`).toBeTruthy();
    expect(rgba[3], `${token} is translucent, so it cannot be measured as text`).toBe(1);
    const fg = toHex(rgba);
    const ratio = contrast(fg, LIGHT_SHELL);
    expect(
      ratio,
      `${token} ${fg} on ${LIGHT_SHELL} measured ${ratio.toFixed(2)}:1`,
    ).toBeGreaterThanOrEqual(AA);
  });
});

describe('token coherence', () => {
  /**
   * Each --v2-*-rgb triplet must stay in sync with the colour above it.
   * A state colour raised without its triplet would leave every
   * rgb(var(--…-rgb) / a) tint on the old hue. Both now come from one
   * palette slot, so drift is structurally impossible; this stays as the
   * guard that says so.
   */
  const pairs = [
    ['--v2-accent', '--v2-accent-rgb'],
    ['--v2-state-live', '--v2-state-live-rgb'],
    ['--v2-state-warn', '--v2-state-warn-rgb'],
    ['--v2-state-error', '--v2-state-error-rgb'],
  ];

  it.each(pairs)('%s matches %s in every theme state', (colorToken, rgbToken) => {
    for (const state of ['light', 'darkMedia', 'darkAttr']) {
      const rgb = colorIn(state, colorToken).slice(0, 3);
      const triplet = resolveIn(state, rgbToken).split(/[\s,]+/).map(Number);
      expect(
        triplet,
        `${state}: ${rgbToken} does not match ${colorToken}`,
      ).toEqual(rgb);
    }
  });

  it('the --v2-state-*-soft alphas are built from the current hue, in both themes', () => {
    for (const state of ['light', 'darkMedia']) {
      for (const name of ['--v2-state-live', '--v2-state-warn', '--v2-state-error']) {
        const hue = colorIn(state, name).slice(0, 3);
        const soft = colorIn(state, `${name}-soft`);
        expect(soft.slice(0, 3), `${state}: ${name}-soft drifted off ${name}`).toEqual(hue);
        expect(soft[3], `${state}: ${name}-soft is not a tint`).toBeLessThan(1);
      }
    }
  });

  /**
   * The old sheet kept a hand-maintained duplicate of the light block
   * inside @media (prefers-color-scheme: light); a token raised in one
   * and not the other meant users on system-light kept the failing
   * colour. The duplicate is gone, but the equivalent hazard is that the
   * three theme states disagree, so assert on the states directly.
   */
  it('every measured token resolves the same in both spellings of each theme', () => {
    const measured = [
      '--v2-text-primary', '--v2-text-secondary', '--v2-text-tertiary',
      '--v2-state-live', '--v2-state-warn', '--v2-state-error',
      '--v2-state-live-rgb', '--v2-state-warn-rgb', '--v2-state-error-rgb',
      '--v2-state-live-soft', '--v2-state-warn-soft', '--v2-state-error-soft',
      '--v2-accent', '--v2-accent-text', '--v2-accent-rgb',
    ];
    for (const name of measured) {
      expect(resolveIn('darkAttr', name), `${name} diverges between the two dark states`)
        .toBe(resolveIn('darkMedia', name));
      expect(resolveIn('lightForced', name), `${name} diverges between the two light states`)
        .toBe(resolveIn('light', name));
      // And the themes must genuinely differ, or the checks above are vacuous.
      expect(resolveIn('light', name), `${name} is identical in light and dark`)
        .not.toBe(resolveIn('darkMedia', name));
    }
  });

  it('the dark tertiary measurement quoted in tokens.css is the real one', () => {
    // tokens.css cites #96969E on #1F1F27 at 5.57:1. Recompute it rather
    // than trusting the prose.
    expect(hexOf('darkMedia', '--v2-text-tertiary')).toBe('#96969E');
    expect(contrast(parseHex('#96969E'), DARK_SHELL)).toBeCloseTo(5.57, 1);
  });
});
