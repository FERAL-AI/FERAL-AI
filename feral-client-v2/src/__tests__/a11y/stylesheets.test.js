/**
 * Stylesheet-level obligations: visible focus, and reduced motion that is
 * actually honoured.
 *
 * jsdom does not load the page stylesheets and does not resolve
 * getComputedStyle against them, so a rendered-DOM assertion here would
 * pass no matter what the CSS said. These read the sheets as text, which
 * is the only non-vacuous way to check them in this suite.
 *
 * Fails against `git show HEAD:src/styles/pages.css` (focus) and
 * `HEAD:src/styles/ui.css` (motion).
 */
import fs from 'node:fs';
import path from 'node:path';
import { describe, it, expect } from 'vitest';

const STYLES_DIR = path.resolve(__dirname, '../../styles');
const SHEETS = ['tokens.css', 'ui.css', 'pages.css', 'markdown.css'];
/** Comments are stripped so a prose block cannot be read as a selector. */
const stripComments = (css) => css.replace(/\/\*[\s\S]*?\*\//g, '');
const CSS = Object.fromEntries(
  SHEETS.map((f) => [f, stripComments(fs.readFileSync(path.join(STYLES_DIR, f), 'utf8'))]),
);
const ALL_CSS = Object.values(CSS).join('\n');

/** Every `selector { body }` pair. Rule bodies here never nest braces. */
function rules(css) {
  const out = [];
  const re = /([^{}]+)\{([^{}]*)\}/g;
  let m;
  while ((m = re.exec(css))) {
    out.push({ selector: m[1].trim(), body: m[2] });
  }
  return out;
}

/** Contents of every @media (prefers-reduced-motion: reduce) block. */
function reducedMotionBlocks(css) {
  const out = [];
  const marker = '@media (prefers-reduced-motion: reduce)';
  let idx = css.indexOf(marker);
  while (idx !== -1) {
    const open = css.indexOf('{', idx);
    let depth = 0;
    let i = open;
    for (; i < css.length; i += 1) {
      if (css[i] === '{') depth += 1;
      else if (css[i] === '}') {
        depth -= 1;
        if (depth === 0) break;
      }
    }
    out.push(css.slice(open + 1, i));
    idx = css.indexOf(marker, i);
  }
  return out;
}

const REDUCED = SHEETS.flatMap((f) => reducedMotionBlocks(CSS[f]));

describe('visible focus', () => {
  /**
   * The interactive elements named in the audit. Each was focusable with
   * no focus style at all, so a keyboard user could not see where they
   * were. .v2-btn / .v2-input / .v2-dock-btn / .v2-code-editor already
   * had one and are listed to catch a regression.
   */
  const MUST_HAVE_FOCUS = [
    '.v2-hub-item',
    '.v2-tab',
    '.v2-skill-pin',
    '.v2-device-card',
    '.v2-seg-btn',
    '.v2-btn',
    '.v2-dock-btn',
  ];

  it.each(MUST_HAVE_FOCUS)('%s declares a :focus-visible style', (selector) => {
    const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const re = new RegExp(`${escaped}:focus-visible`);
    expect(re.test(ALL_CSS), `${selector} has no :focus-visible rule`).toBe(true);
  });

  it('every focused element gets an indicator, not just a tweak', () => {
    // Collect every selector whose *own* last compound is :focus-visible
    // (so `.v2-dock-btn:focus-visible .v2-dock-label`, which reveals a
    // tooltip rather than drawing the ring, is not one of them) and merge
    // the bodies of all the rules that target it, because a selector can
    // legitimately be split across a shared ring rule and an override.
    const bodies = new Map();
    for (const sheet of SHEETS) {
      for (const rule of rules(CSS[sheet])) {
        for (const raw of rule.selector.split(',')) {
          const sel = raw.trim();
          const last = sel.split(/\s+/).pop() || '';
          if (!last.includes(':focus-visible')) continue;
          bodies.set(sel, (bodies.get(sel) || '') + rule.body);
        }
      }
    }
    expect(bodies.size, 'no :focus-visible rules at all').toBeGreaterThan(0);

    const bad = [];
    for (const [sel, body] of bodies) {
      const draws = /box-shadow\s*:|outline\s*:\s*(?!none)|border-color\s*:|background\s*:/
        .test(body);
      if (!draws) bad.push(sel);
    }
    expect(bad, `:focus-visible rules that render no indicator: ${bad.join(', ')}`).toEqual([]);
  });

  it('no rule kills the focus outline without replacing it', () => {
    const bad = [];
    for (const sheet of SHEETS) {
      for (const rule of rules(CSS[sheet])) {
        if (!/outline\s*:\s*none/.test(rule.body)) continue;
        // `outline: none` inside a :focus rule that supplies its own ring
        // is fine.
        if (rule.selector.includes(':focus') && /box-shadow\s*:|border-color\s*:/.test(rule.body)) {
          continue;
        }
        // Otherwise the same element must get a ring back from a sibling
        // :focus / :focus-visible rule somewhere in the sheets.
        const restored = rule.selector.split(',').every((sel) => {
          const base = sel.trim();
          const escaped = base.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
          return new RegExp(`${escaped}:focus(-visible|-within)?\\b`).test(ALL_CSS);
        });
        if (!restored) bad.push(`${sheet}: ${rule.selector}`);
      }
    }
    expect(bad, `outline removed with no replacement: ${bad.join(', ')}`).toEqual([]);
  });
});

describe('prefers-reduced-motion is honoured, not just declared', () => {
  it('zeroes the duration tokens', () => {
    expect(REDUCED.length).toBeGreaterThan(0);
    const tokenBlock = REDUCED.find((b) => b.includes('--v2-dur-base'));
    expect(tokenBlock, 'no reduced-motion override for the --v2-dur-* tokens').toBeTruthy();
    expect(tokenBlock).toMatch(/--v2-dur-fast:\s*0ms/);
    expect(tokenBlock).toMatch(/--v2-dur-base:\s*0ms/);
    expect(tokenBlock).toMatch(/--v2-dur-slow:\s*0ms/);
  });

  /**
   * The token zeroing above only reaches animations whose duration IS a
   * token. Every keyframe in this client declares its own literal
   * duration, so each of those needs an explicit override; the tokens
   * gave the appearance of reduced-motion support without any of it.
   */
  it('every animation with a hardcoded duration has an explicit override', () => {
    const literalDuration = /animation:\s*[\w-]+\s+[\d.]+m?s/;
    const unhandled = [];

    for (const sheet of SHEETS) {
      const sheetReduced = reducedMotionBlocks(CSS[sheet]).join('\n');
      for (const rule of rules(CSS[sheet])) {
        if (!literalDuration.test(rule.body)) continue;
        // Skip the reduced-motion blocks themselves.
        if (sheetReduced.includes(rule.body)) continue;

        // The override may target the element or any ancestor selector in
        // the same compound, so match on the last simple class name.
        const classes = rule.selector.match(/\.[\w-]+/g) || [];
        const covered = classes.some((c) => REDUCED.some((b) => b.includes(c)));
        if (!covered) unhandled.push(`${sheet}: ${rule.selector}`);
      }
    }

    expect(
      unhandled,
      `animations that never stop under reduced motion: ${unhandled.join(' | ')}`,
    ).toEqual([]);
  });

  it('never dims a status indicator as the way of stopping it', () => {
    // Turning an indicator's opacity down is a regression dressed as an
    // accessibility fix: the reduced-motion user ends up with a fainter
    // signal than the animated one.
    for (const block of REDUCED) {
      for (const rule of rules(`x{${block}}`).slice(1)) {
        const m = rule.body.match(/opacity:\s*([\d.]+)/);
        if (!m) continue;
        if (!/dot|indicator|cursor/i.test(rule.selector)) continue;
        expect(
          Number(m[1]),
          `${rule.selector} dims to ${m[1]} under reduced motion`,
        ).toBeGreaterThanOrEqual(0.6);
      }
    }
  });
});
