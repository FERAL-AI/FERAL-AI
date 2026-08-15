/**
 * The four items left open by the accessibility pass.
 *
 * Two are behavioural (HubLauncher had no focus trap, Pair rendered a dot
 * animating a keyframe that does not exist), one is a measurement
 * (--v2-accent as text was under AA), and the fourth is recorded as a known
 * limit rather than a fix.
 */
import React from 'react';
import { describe, it, expect } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import fs from 'node:fs';
import path from 'node:path';

import HubLauncher from '../../components/HubLauncher';

const TOKENS = fs.readFileSync(
  path.resolve(__dirname, '../../styles/tokens.css'),
  'utf8',
);

function luminance(hex) {
  const h = hex.replace('#', '');
  const ch = [0, 2, 4]
    .map((i) => parseInt(h.slice(i, i + 2), 16) / 255)
    .map((v) => (v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4));
  return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2];
}

function contrast(a, b) {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

/** Every declaration of `name`, in source order: dark block first. */
function declarations(name) {
  return [...TOKENS.matchAll(new RegExp(`--${name}:\\s*(#[0-9A-Fa-f]{6})`, 'g'))]
    .map((m) => m[1]);
}

describe('--v2-accent-text clears AA where the accent did not', () => {
  // Composited surfaces, same values the token comments cite.
  const SHELL_BASE = '#1F1F27';
  const SURFACE_ELEV = '#2E2E38';

  it('the plain accent really was failing, which is why a second token exists', () => {
    const [accent] = declarations('v2-accent');
    // Not a regression guard on the accent: it documents WHY the split
    // exists, so nobody later "simplifies" it back to one token.
    expect(contrast(accent, SHELL_BASE)).toBeLessThan(4.5);
    expect(contrast(accent, SURFACE_ELEV)).toBeLessThan(4.5);
  });

  it('meets AA on the shell base and on the worst dark surface', () => {
    const [dark] = declarations('v2-accent-text');
    expect(contrast(dark, SHELL_BASE)).toBeGreaterThanOrEqual(4.5);
    // The elevated surface is where most accent-coloured chips sit, and it
    // is the case a check against the page background alone would miss.
    expect(contrast(dark, SURFACE_ELEV)).toBeGreaterThanOrEqual(4.5);
  });

  it('meets AA in light mode', () => {
    const light = declarations('v2-accent-text')[1];
    expect(light).toBeTruthy();
    expect(contrast(light, '#ECEEF3')).toBeGreaterThanOrEqual(4.5);
  });

  it('is declared in the dark block and both light blocks', () => {
    // tokens.css carries .v2-light and a prefers-color-scheme duplicate.
    // A value that lands in only one of them is the drift this catches.
    expect(declarations('v2-accent-text')).toHaveLength(3);
  });
});

describe('no stylesheet animates a keyframe that does not exist', () => {
  const STYLE_DIR = path.resolve(__dirname, '../../styles');
  const SRC_DIR = path.resolve(__dirname, '../..');

  function walk(dir, out = []) {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      if (entry.name === 'node_modules' || entry.name === '__tests__') continue;
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) walk(full, out);
      else if (/\.(css|jsx?)$/.test(entry.name)) out.push(full);
    }
    return out;
  }

  /**
   * Comments describe bugs, including this one: ui.css documents the old
   * `animation: pulse 1s` that named a keyframe which never existed. Scanning
   * raw text reports that prose as a live reference. Strip comments first or
   * the check fails on its own documentation.
   */
  const stripComments = (s) => s.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');

  it('every animation name referenced is defined somewhere', () => {
    const files = walk(SRC_DIR);
    // Every stylesheet, not just src/styles: the five v2Orb* keyframes live
    // in src/index.css, and scanning only the styles directory reports all
    // of them as missing.
    const css = files
      .filter((f) => f.endsWith('.css'))
      .map((f) => fs.readFileSync(f, 'utf8'))
      .join('\n');
    const defined = new Set(
      [...css.matchAll(/@keyframes\s+([\w-]+)/g)].map((m) => m[1]),
    );
    expect(defined.size).toBeGreaterThan(8);

    const missing = [];
    for (const file of files) {
      const body = stripComments(fs.readFileSync(file, 'utf8'));
      // `animation: <name> <duration>` in CSS and in inline JS style objects.
      for (const m of body.matchAll(/animation:\s*["']?([A-Za-z][\w-]*)\s+[\d.]+m?s/g)) {
        const name = m[1];
        if (name === 'none' || defined.has(name)) continue;
        missing.push(`${path.relative(SRC_DIR, file)} -> ${name}`);
      }
    }

    // Pair.jsx animated `v2-pulse`, which was defined nowhere, so the
    // indicator was silently static. Inline, it was also above the
    // stylesheet and therefore unreachable by the reduced-motion overrides.
    expect(missing).toEqual([]);
  });
});

describe('HubLauncher contains focus, as aria-modal promises', () => {
  const renderHub = () => render(
    <MemoryRouter>
      <button type="button" data-testid="outside">outside</button>
      <HubLauncher open onClose={() => {}} />
    </MemoryRouter>,
  );

  it('keeps Tab inside the dialog', () => {
    renderHub();
    const dialog = screen.getByRole('dialog', { name: /hub launcher/i });
    const focusables = dialog.querySelectorAll(
      'a[href], button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])',
    );
    expect(focusables.length).toBeGreaterThan(1);

    const last = focusables[focusables.length - 1];
    last.focus();
    fireEvent.keyDown(window, { key: 'Tab' });

    // Wrapped to the first item rather than escaping to the page behind.
    expect(dialog.contains(document.activeElement)).toBe(true);
    expect(document.activeElement).not.toBe(screen.getByTestId('outside'));
  });

  it('pulls focus back when it is outside the dialog', () => {
    renderHub();
    const dialog = screen.getByRole('dialog', { name: /hub launcher/i });

    screen.getByTestId('outside').focus();
    fireEvent.keyDown(window, { key: 'Tab' });

    expect(dialog.contains(document.activeElement)).toBe(true);
  });

  it('wraps backwards from the first item to the last', () => {
    renderHub();
    const dialog = screen.getByRole('dialog', { name: /hub launcher/i });
    const focusables = dialog.querySelectorAll(
      'a[href], button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])',
    );

    focusables[0].focus();
    fireEvent.keyDown(window, { key: 'Tab', shiftKey: true });

    expect(document.activeElement).toBe(focusables[focusables.length - 1]);
  });
});
