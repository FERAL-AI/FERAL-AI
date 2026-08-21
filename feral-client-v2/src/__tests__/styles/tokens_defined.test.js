/**
 * tokens.css structure guards.
 *
 * The sheet is two layers: a palette (--p-*) that holds every literal
 * colour in the client, and a semantic layer (--v2-*) where every value
 * derives from a palette slot. Three theme states have to be complete:
 *
 *   1. bare :root                                        light
 *   2. @media (prefers-color-scheme: dark) guarded as
 *      :root:not([data-theme="light"]):not(.v2-light)     dark
 *   3. :root[data-theme="dark"] / :root.v2-dark           dark
 *
 * Two defects these were written against, both of which were live:
 *
 *   D1. Four tokens were referenced by production CSS/JSX with no
 *       fallback and declared nowhere: --v2-border and --v2-surface
 *       (pages.css, the chat-trace toggle and list), --v2-border-subtle
 *       (pages.css .v2-mindmap-tooltip and ConsciousnessMindMap.jsx edge
 *       strokes) and --v2-font-sans (pages.css .v2-mindmap-label). An
 *       unresolvable var() with no fallback is invalid at computed-value
 *       time, so `border: 1px solid var(--v2-border)` drew no border at
 *       all and `background: var(--v2-surface)` rendered transparent.
 *       Nothing warned; the elements just came out unstyled.
 *
 *   D2. A token declared only inside the prefers-color-scheme block is
 *       absent in the un-stamped state. The old sheet was dark-on-bare-
 *       :root with a hand-maintained light duplicate inside the media
 *       query, so the two light blocks could and did drift.
 *
 * These run in jsdom against a model of the cascade (see
 * ../_helpers/tokens.js). `e2e/tokens-themes.spec.js` measures the same
 * three states in a real Chromium and is the authority; this file exists
 * so the same defect fails in a second and reproduces without a browser.
 */
import fs from 'node:fs';
import path from 'node:path';
import { describe, it, expect } from 'vitest';

import {
  RAW_TOKENS,
  REQUIRED_STATES,
  allPaletteNames,
  allSemanticNames,
  declaredIn,
  resolveIn,
  toRgba,
} from '../_helpers/tokens';

const SRC_DIR = path.resolve(__dirname, '../..');
const stripComments = (s) => s.replace(/\/\*[\s\S]*?\*\//g, '');

function walk(dir, out = []) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (entry.name === 'node_modules' || entry.name === '__tests__') continue;
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) walk(full, out);
    else if (/\.(css|jsx?)$/.test(entry.name)) out.push(full);
  }
  return out;
}

/** Every --v2-* name a production file reads, and where from. */
function referencedTokens() {
  const refs = new Map();
  for (const file of walk(SRC_DIR)) {
    const text = stripComments(fs.readFileSync(file, 'utf8'));
    for (const m of text.matchAll(/var\(\s*(--v2-[\w-]+)\s*([,)])/g)) {
      const name = m[1];
      if (!refs.has(name)) refs.set(name, new Set());
      refs.get(name).add(path.relative(SRC_DIR, file));
    }
  }
  return refs;
}

const REFERENCED = referencedTokens();
const DECLARED = allSemanticNames();

describe('every referenced token is declared (D1)', () => {
  it('finds a non-trivial number of references at all', () => {
    expect(REFERENCED.size).toBeGreaterThan(60);
  });

  it('no production file reads a --v2-* that tokens.css never declares', () => {
    const missing = [...REFERENCED.entries()]
      .filter(([name]) => !DECLARED.includes(name))
      .map(([name, files]) => `${name} (${[...files].join(', ')})`);
    expect(
      missing,
      `referenced but never declared, so these render as the property's `
      + `initial value: ${missing.join(' | ')}`,
    ).toEqual([]);
  });

  it('declares nothing that no one reads', () => {
    // Not a correctness bug, but an unread token is dead weight that the
    // next person has to reason about. Fail loudly rather than accrete.
    const unread = DECLARED.filter((n) => !REFERENCED.has(n));
    expect(unread, `declared but unreferenced: ${unread.join(', ')}`).toEqual([]);
  });
});

describe('all three theme states are complete (D2)', () => {
  it.each(REQUIRED_STATES)('%s resolves every --v2-* to a concrete value', (state) => {
    const unresolved = DECLARED.filter((name) => resolveIn(state, name) === null);
    expect(
      unresolved,
      `these do not resolve in the "${state}" state: ${unresolved.join(', ')}`,
    ).toEqual([]);
  });

  it.each(REQUIRED_STATES)('%s leaves no unsubstituted var()', (state) => {
    const leftovers = DECLARED
      .map((name) => [name, resolveIn(state, name)])
      .filter(([, value]) => value != null && value.includes('var('));
    expect(leftovers.map(([n]) => n), 'var() chain did not terminate').toEqual([]);
  });

  it('every --p-* slot is declared in every state, not just the dark ones', () => {
    const palette = allPaletteNames();
    expect(palette.length).toBeGreaterThan(15);
    for (const state of REQUIRED_STATES) {
      const declared = declaredIn(state);
      const missing = palette.filter((n) => !declared.has(n));
      expect(missing, `palette slots missing in "${state}": ${missing.join(', ')}`).toEqual([]);
    }
  });

  it('the two dark states agree exactly', () => {
    const diffs = [];
    for (const name of DECLARED) {
      const viaMedia = resolveIn('darkMedia', name);
      const viaAttr = resolveIn('darkAttr', name);
      const viaClass = resolveIn('darkClass', name);
      if (viaMedia !== viaAttr) diffs.push(`${name}: media=${viaMedia} attr=${viaAttr}`);
      if (viaMedia !== viaClass) diffs.push(`${name}: media=${viaMedia} class=${viaClass}`);
    }
    expect(diffs, `explicit dark disagrees with system dark: ${diffs.join(' | ')}`).toEqual([]);
  });

  it('an explicit light choice survives a dark system preference', () => {
    // This is what the :not([data-theme="light"]):not(.v2-light) guards on
    // the media block buy. Without them the toggle is one-way: you could
    // pick dark on a light machine but never light on a dark one.
    const diffs = DECLARED
      .filter((name) => resolveIn('lightForced', name) !== resolveIn('light', name))
      .map((name) => `${name}: forced=${resolveIn('lightForced', name)}`);
    expect(diffs, `[data-theme="light"] drifts from the light theme: ${diffs.join(' | ')}`)
      .toEqual([]);
  });

  it('the themes actually differ, so a broken guard cannot pass vacuously', () => {
    const flipped = DECLARED.filter((n) => resolveIn('light', n) !== resolveIn('darkMedia', n));
    expect(flipped.length, 'light and dark resolve identically for everything').toBeGreaterThan(30);
  });
});

describe('the palette is the only place a colour is written', () => {
  const CSS = stripComments(RAW_TOKENS);
  // The semantic layer starts at the first --v2-* declaration.
  const semanticStart = CSS.search(/--v2-[\w-]+\s*:/);
  const SEMANTIC = CSS.slice(semanticStart);

  it('the file is ordered palette first, semantics second', () => {
    expect(semanticStart).toBeGreaterThan(0);
    expect(CSS.slice(0, semanticStart)).toMatch(/--p-[\w-]+\s*:/);
    // No palette slot may be declared after the semantic layer opens, or
    // "the palette is at the top" stops being true.
    expect(SEMANTIC).not.toMatch(/--p-[\w-]+\s*:/);
  });

  it('no hex, rgb() or named colour literal below the palette layer', () => {
    const literals = [
      ...SEMANTIC.matchAll(/#[0-9a-fA-F]{3,8}\b/g),
      ...SEMANTIC.matchAll(/\brgba?\(\s*[\d.]/g),
      ...SEMANTIC.matchAll(/\bhsla?\(/g),
      ...SEMANTIC.matchAll(/:\s*(white|black|red|green|blue|gray|grey)\b/g),
    ].map((m) => m[0]);
    expect(
      literals,
      `literal colours outside the palette layer: ${literals.join(', ')}`,
    ).toEqual([]);
  });

  it('every colour-valued token traces back to a palette slot', () => {
    const notDerived = [];
    for (const name of DECLARED) {
      const state = declaredIn('light').get(name);
      if (state === undefined) continue;
      const resolved = resolveIn('light', name);
      // A value that parses as a colour, or paints one, must have come
      // from --p-*. Non-colour tokens (sizes, radii, motion, fonts) are
      // exempt by construction: they contain no colour to hardcode.
      const paints = toRgba(resolved) != null || /gradient|box-shadow|\d+px .*rgb/.test(resolved);
      if (!paints) continue;
      // Walk the declaration text of this token and everything it names.
      const seen = new Set();
      const stack = [name];
      let touchesPalette = false;
      while (stack.length) {
        const cur = stack.pop();
        if (seen.has(cur)) continue;
        seen.add(cur);
        const raw = declaredIn('light').get(cur);
        if (raw === undefined) continue;
        for (const m of raw.matchAll(/var\(\s*(--[\w-]+)/g)) {
          if (m[1].startsWith('--p-')) touchesPalette = true;
          else stack.push(m[1]);
        }
      }
      if (!touchesPalette) notDerived.push(name);
    }
    expect(
      notDerived,
      `colour tokens that do not reference the palette: ${notDerived.join(', ')}`,
    ).toEqual([]);
  });
});

describe('the toggle spellings the client actually stamps', () => {
  const CSS = stripComments(RAW_TOKENS);

  it('honours both [data-theme] and the v2-light / v2-dark classes', () => {
    // useTheme.js and main.jsx stamp the classes. [data-theme] is the
    // portable spelling. Dropping either half silently disables the
    // menubar toggle for every user.
    for (const selector of [
      ':root[data-theme="dark"]',
      ':root.v2-dark',
      ':root[data-theme="light"]',
      ':root.v2-light',
    ]) {
      expect(CSS, `tokens.css never mentions ${selector}`).toContain(selector);
    }
  });

  it('guards the media block against both light spellings', () => {
    const guarded = CSS.match(
      /@media \(prefers-color-scheme: dark\)\s*\{\s*:root:not\(\[data-theme="light"\]\):not\(\.v2-light\)/g,
    );
    expect(guarded, 'the dark media block is not guarded against an explicit light choice')
      .not.toBeNull();
    // Both the palette block and the semantic block need the guard.
    expect(guarded.length).toBe(2);
  });

  it('sets color-scheme for every state so UA controls follow the theme', () => {
    expect(CSS).toMatch(/:root\s*\{[^}]*color-scheme:\s*light dark/);
    const darkScheme = CSS.match(/color-scheme:\s*dark\s*;/g) || [];
    expect(darkScheme.length, 'both dark states must set color-scheme: dark').toBe(2);
    expect(CSS).toMatch(/:root\[data-theme="light"\],\s*:root\.v2-light\s*\{\s*color-scheme:\s*light;/);
  });
});
