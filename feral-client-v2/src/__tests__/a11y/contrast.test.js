/**
 * Colour contrast, WCAG 2.1 AA for text, computed rather than eyeballed.
 *
 * These read the real token values out of styles/tokens.css and compute
 * the contrast ratio, so the numbers in the token comments cannot drift
 * away from the numbers the tokens actually produce.
 *
 * Backgrounds. Dark mode text sits on a translucent glass panel over the
 * shell gradient, so the effective background is the panel composited
 * over #1F1F27 (the top stop of --v2-shell-ambient). All four dark
 * surfaces are computed below and the worst one has to pass, because
 * --v2-text-tertiary is used on every one of them. Light mode composites
 * *towards white*, so the un-glassed #F2F3F8 is the conservative end and
 * is used directly.
 *
 * Fails against `git show HEAD:src/styles/tokens.css`.
 */
import fs from 'node:fs';
import path from 'node:path';
import { describe, it, expect } from 'vitest';

const TOKENS = fs.readFileSync(
  path.resolve(__dirname, '../../styles/tokens.css'),
  'utf8',
);

// ── WCAG 2.1 relative luminance + contrast ratio ──────────────────
function channel(c) {
  const s = c / 255;
  return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
}
function parseHex(hex) {
  const h = hex.replace('#', '').trim();
  return [
    parseInt(h.slice(0, 2), 16),
    parseInt(h.slice(2, 4), 16),
    parseInt(h.slice(4, 6), 16),
  ];
}
function luminance(rgb) {
  return 0.2126 * channel(rgb[0]) + 0.7152 * channel(rgb[1]) + 0.0722 * channel(rgb[2]);
}
function contrast(a, b) {
  const la = luminance(typeof a === 'string' ? parseHex(a) : a);
  const lb = luminance(typeof b === 'string' ? parseHex(b) : b);
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}
/** Composite an rgba fill over an opaque hex background. */
function composite([r, g, b, alpha], baseHex) {
  const base = parseHex(baseHex);
  return [
    alpha * r + (1 - alpha) * base[0],
    alpha * g + (1 - alpha) * base[1],
    alpha * b + (1 - alpha) * base[2],
  ];
}

// ── Token extraction ──────────────────────────────────────────────
// tokens.css declares each token three times: the dark :root block, the
// :root.v2-light block, and the prefers-color-scheme duplicate. Index 0
// is dark, index 1 is the explicit light class.
function readToken(name, occurrence) {
  const re = new RegExp(`--${name}:\\s*([^;]+);`, 'g');
  const hits = [...TOKENS.matchAll(re)].map((m) => m[1].trim());
  expect(hits.length, `--${name} not found in tokens.css`).toBeGreaterThan(occurrence);
  return hits[occurrence];
}
const dark = (name) => readToken(name, 0);
const light = (name) => readToken(name, 1);

function rgbaTuple(value) {
  const m = value.match(/rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*\)/);
  expect(m, `not an rgba() value: ${value}`).toBeTruthy();
  return [Number(m[1]), Number(m[2]), Number(m[3]), Number(m[4])];
}
function toHex(rgb) {
  return `#${rgb.map((v) => Math.round(v).toString(16).padStart(2, '0').toUpperCase()).join('')}`;
}

const AA = 4.5;
const DARK_SHELL = '#1F1F27';
const LIGHT_SHELL = '#F2F3F8';

/** Every opaque background --v2-text-tertiary can land on in dark mode. */
function darkBackgrounds() {
  const out = { 'shell base': DARK_SHELL };
  for (const key of ['surface-0', 'surface-1', 'surface-2', 'surface-elev']) {
    out[key] = toHex(composite(rgbaTuple(dark(`v2-${key}`)), DARK_SHELL));
  }
  return out;
}

describe('WCAG AA, dark theme', () => {
  it('--v2-text-tertiary clears 4.5:1 on every dark surface', () => {
    const fg = dark('v2-text-tertiary');
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
    for (const name of ['v2-text-secondary', 'v2-text-primary']) {
      const ratio = contrast(dark(name), DARK_SHELL);
      expect(ratio, `--${name} measured ${ratio.toFixed(2)}:1`).toBeGreaterThanOrEqual(AA);
    }
  });
});

describe('WCAG AA, light theme', () => {
  const cases = [
    ['v2-text-tertiary', '.v2-device-meta / .v2-p--tiny / .v2-btn--ghost'],
    ['v2-text-secondary', '.v2-p--muted'],
    ['v2-state-live', '.v2-chip--live / .v2-chip--loose'],
    ['v2-state-warn', '.v2-chip--warn / .v2-chip--strict'],
    ['v2-state-error', '.v2-chip--error'],
  ];

  it.each(cases)('--%s clears 4.5:1 on the light shell (%s)', (token) => {
    const fg = light(token);
    expect(fg, `--${token} light value is not a hex: ${fg}`).toMatch(/^#[0-9A-Fa-f]{6}$/);
    const ratio = contrast(fg, LIGHT_SHELL);
    expect(
      ratio,
      `--${token} ${fg} on ${LIGHT_SHELL} measured ${ratio.toFixed(2)}:1`,
    ).toBeGreaterThanOrEqual(AA);
  });
});

describe('token coherence', () => {
  /**
   * tokens.css states that each --v2-*-rgb triplet must stay in sync with
   * the hex above it. Raising a state colour without raising its triplet
   * would leave every rgb(var(--…-rgb) / a) tint on the old hue.
   */
  const pairs = [
    ['v2-accent', 'v2-accent-rgb'],
    ['v2-state-live', 'v2-state-live-rgb'],
    ['v2-state-warn', 'v2-state-warn-rgb'],
    ['v2-state-error', 'v2-state-error-rgb'],
  ];

  it.each(pairs)('--%s matches --%s in both themes', (hexToken, rgbToken) => {
    for (const [themeName, read] of [['dark', dark], ['light', light]]) {
      const rgb = parseHex(read(hexToken));
      const triplet = read(rgbToken).split(/\s+/).map(Number);
      expect(
        triplet,
        `${themeName} --${rgbToken} does not match --${hexToken}`,
      ).toEqual(rgb);
    }
  });

  it('light --v2-state-*-soft alphas are built from the current hue', () => {
    for (const name of ['v2-state-live', 'v2-state-warn', 'v2-state-error']) {
      const hex = parseHex(light(name));
      const soft = rgbaTuple(light(`${name}-soft`)).slice(0, 3);
      expect(soft, `--${name}-soft drifted off --${name}`).toEqual(hex);
    }
  });

  /**
   * The prefers-color-scheme block is a hand-maintained duplicate of the
   * :root.v2-light block. A token raised in one and not the other means
   * users on system-light get the old failing colour.
   */
  it('the media-query light block duplicates the .v2-light block exactly', () => {
    for (const name of [
      'v2-text-tertiary', 'v2-text-secondary', 'v2-text-primary',
      'v2-state-live', 'v2-state-warn', 'v2-state-error',
      'v2-state-live-rgb', 'v2-state-warn-rgb', 'v2-state-error-rgb',
      'v2-state-live-soft', 'v2-state-warn-soft', 'v2-state-error-soft',
    ]) {
      expect(readToken(name, 2), `--${name} diverges between the two light blocks`)
        .toBe(readToken(name, 1));
    }
  });
});
