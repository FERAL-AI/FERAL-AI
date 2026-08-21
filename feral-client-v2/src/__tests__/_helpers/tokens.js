/**
 * A cascade resolver for styles/tokens.css.
 *
 * jsdom does not load the page stylesheets, so getComputedStyle against a
 * rendered element tells you nothing about what the tokens actually
 * resolve to. The tests that predate this helper worked around that by
 * counting occurrences of a token in the file and indexing them
 * positionally (0 = dark, 1 = light, 2 = the media duplicate). That is
 * only correct while the file is a flat list of hexes in a fixed order,
 * and it cannot see the two failure modes that matter:
 *
 *   - a token declared in ONE theme state, which silently falls back to
 *     the other theme's value (or to nothing) in the states that missed
 *     it. A token declared only inside the prefers-color-scheme block is
 *     the classic version: it is simply absent when the media query does
 *     not match.
 *   - a token referenced by a component and declared nowhere, which
 *     renders as the property's initial value with no error anywhere.
 *
 * So this parses the sheet, applies the real cascade for a named theme
 * state, and resolves var() chains to a concrete value the way a browser
 * would. It is a model of a browser, not a browser: the e2e spec
 * `e2e/tokens-themes.spec.js` measures the same three states in Chromium
 * and is the authority. This exists so a unit test can fail fast on the
 * same defect.
 */
import fs from 'node:fs';
import path from 'node:path';

const TOKENS_PATH = path.resolve(__dirname, '../../styles/tokens.css');

export const RAW_TOKENS = fs.readFileSync(TOKENS_PATH, 'utf8');

const stripComments = (css) => css.replace(/\/\*[\s\S]*?\*\//g, '');

/**
 * The three theme states tokens.css promises, plus the fourth that the
 * :not() guards exist for: an explicit light choice on a dark system.
 */
export const THEME_STATES = {
  /** Bare <html>, system light or no preference. */
  light: { prefersDark: false, stamped: null },
  /** Bare <html>, system dark. */
  darkMedia: { prefersDark: true, stamped: null },
  /** <html data-theme="dark"> on a light system. */
  darkAttr: { prefersDark: false, stamped: 'dark' },
  /** <html class="v2-dark"> on a light system. */
  darkClass: { prefersDark: false, stamped: 'dark' },
  /** <html data-theme="light"> on a dark system. */
  lightForced: { prefersDark: true, stamped: 'light' },
};

/** The three states the task treats as the contract. */
export const REQUIRED_STATES = ['light', 'darkMedia', 'darkAttr'];

/**
 * Parse the sheet into flat rules. Rule bodies in tokens.css never nest
 * braces, and the only at-rule is @media, so one level of nesting is all
 * that has to be handled.
 */
function parseRules(css) {
  const out = [];
  let order = 0;

  const consume = (source, media) => {
    const re = /([^{}]+)\{([^{}]*)\}/g;
    let m;
    while ((m = re.exec(source))) {
      out.push({
        media,
        selectors: m[1].split(',').map((s) => s.trim()).filter(Boolean),
        body: m[2],
        order: order++,
      });
    }
  };

  // Pull the @media blocks out first, recording their condition, then
  // feed what is left through the same rule scanner.
  let rest = '';
  let i = 0;
  while (i < css.length) {
    const at = css.indexOf('@media', i);
    if (at === -1) {
      rest += css.slice(i);
      break;
    }
    rest += css.slice(i, at);
    const open = css.indexOf('{', at);
    let depth = 0;
    let j = open;
    for (; j < css.length; j += 1) {
      if (css[j] === '{') depth += 1;
      else if (css[j] === '}') {
        depth -= 1;
        if (depth === 0) break;
      }
    }
    consume(css.slice(open + 1, j), css.slice(at + 6, open).trim());
    i = j + 1;
  }
  consume(rest, null);
  return out.sort((a, b) => a.order - b.order);
}

const RULES = parseRules(stripComments(RAW_TOKENS));

/** Declarations of a rule body, in source order. */
function declarations(body) {
  const out = [];
  const re = /(--[\w-]+)\s*:\s*([^;]+);/g;
  let m;
  while ((m = re.exec(body))) out.push([m[1], m[2].trim()]);
  return out;
}

/**
 * Specificity for the selector shapes this sheet uses. `:root`, `[attr]`
 * and `.class` are all class-level, and `:not()` contributes its most
 * specific argument, which for every selector here is one class-level
 * unit.
 */
function specificity(selector) {
  return (selector.match(/:root|\[[^\]]+\]|\.[\w-]+/g) || []).length;
}

function mediaApplies(condition, state) {
  if (!condition) return true;
  if (/prefers-color-scheme:\s*dark/.test(condition)) return state.prefersDark;
  if (/prefers-color-scheme:\s*light/.test(condition)) return !state.prefersDark;
  // prefers-reduced-motion and anything else: treated as not matching, so
  // the default (motion allowed) is what the resolver reports.
  return false;
}

function selectorMatches(selector, state) {
  if (!selector.startsWith(':root')) return false;
  const wantsDark = /\[data-theme="dark"\]|\.v2-dark/.test(selector);
  const wantsLight = /:root\[data-theme="light"\]|:root\.v2-light/.test(selector)
    && !/:not\(/.test(selector);
  const excludesLight = /:not\(\[data-theme="light"\]\)|:not\(\.v2-light\)/.test(selector);

  if (wantsDark && state.stamped !== 'dark') return false;
  if (wantsLight && state.stamped !== 'light') return false;
  if (excludesLight && state.stamped === 'light') return false;
  return true;
}

/**
 * The winning declaration for every custom property in one theme state,
 * unresolved (var() references intact).
 */
export function declaredIn(stateName) {
  const state = THEME_STATES[stateName];
  if (!state) throw new Error(`unknown theme state: ${stateName}`);
  const winners = new Map();
  const applicable = [];

  for (const rule of RULES) {
    if (!mediaApplies(rule.media, state)) continue;
    for (const selector of rule.selectors) {
      if (!selectorMatches(selector, state)) continue;
      applicable.push({ spec: specificity(selector), order: rule.order, body: rule.body });
      break;
    }
  }
  applicable.sort((a, b) => (a.spec - b.spec) || (a.order - b.order));
  for (const rule of applicable) {
    for (const [name, value] of declarations(rule.body)) winners.set(name, value);
  }
  return winners;
}

/**
 * Substitute var() references until no `var(` is left. Returns null when
 * a reference names a property that is not declared in this state and
 * carries no fallback, which is exactly the browser behaviour that makes
 * the declaration invalid at computed-value time.
 */
export function resolveIn(stateName, name) {
  const declared = declaredIn(stateName);
  const start = declared.get(name);
  if (start === undefined) return null;

  const substitute = (value, depth) => {
    if (depth > 32) throw new Error(`var() cycle resolving ${name}`);
    const idx = value.indexOf('var(');
    if (idx === -1) return value;

    // Find the matching close paren for this var(.
    let depthParen = 0;
    let end = idx + 3;
    for (; end < value.length; end += 1) {
      if (value[end] === '(') depthParen += 1;
      else if (value[end] === ')') {
        depthParen -= 1;
        if (depthParen === 0) break;
      }
    }
    const inner = value.slice(idx + 4, end);
    const comma = inner.indexOf(',');
    const ref = (comma === -1 ? inner : inner.slice(0, comma)).trim();
    const fallback = comma === -1 ? null : inner.slice(comma + 1).trim();

    let replacement = declared.get(ref);
    if (replacement === undefined) replacement = fallback;
    if (replacement === undefined || replacement === null) return null;

    const next = `${value.slice(0, idx)}${replacement}${value.slice(end + 1)}`;
    return substitute(next, depth + 1);
  };

  const out = substitute(start, 0);
  // Runs of whitespace inside a CSS value are not significant, and the
  // three theme blocks sit at different indent levels, so compare on a
  // normalised form or every multi-line value reads as a difference.
  return out === null ? null : out.replace(/\s+/g, ' ').trim();
}

/** Every --v2-* name the sheet declares, in any state. */
export function allSemanticNames() {
  const names = new Set();
  for (const rule of RULES) {
    for (const [name] of declarations(rule.body)) {
      if (name.startsWith('--v2-')) names.add(name);
    }
  }
  return [...names].sort();
}

/** Every --p-* name the sheet declares, in any state. */
export function allPaletteNames() {
  const names = new Set();
  for (const rule of RULES) {
    for (const [name] of declarations(rule.body)) {
      if (name.startsWith('--p-')) names.add(name);
    }
  }
  return [...names].sort();
}

// ── Colour parsing ────────────────────────────────────────────────

export function parseHex(hex) {
  const h = hex.replace('#', '').trim();
  const full = h.length === 3 ? h.split('').map((c) => c + c).join('') : h;
  return [
    parseInt(full.slice(0, 2), 16),
    parseInt(full.slice(2, 4), 16),
    parseInt(full.slice(4, 6), 16),
  ];
}

/**
 * Parse any colour form this sheet produces into [r, g, b, a].
 * Handles `#rrggbb`, `rgb(r, g, b)`, `rgba(r, g, b, a)` and the modern
 * `rgb(r g b / a)` the semantic layer composes.
 */
export function toRgba(value) {
  if (value == null) return null;
  const v = value.trim();
  if (v.startsWith('#')) return [...parseHex(v), 1];

  const m = v.match(/^rgba?\(([^)]+)\)$/i);
  if (!m) return null;
  const body = m[1];
  const [colorPart, alphaPart] = body.includes('/') ? body.split('/') : [body, null];
  const nums = colorPart.trim().split(/[\s,]+/).filter(Boolean).map(Number);
  if (nums.length < 3 || nums.some(Number.isNaN)) return null;
  let alpha = 1;
  if (alphaPart != null) alpha = Number(alphaPart.trim());
  else if (nums.length === 4) alpha = nums[3];
  return [nums[0], nums[1], nums[2], alpha];
}

/** The opaque hex of a colour, ignoring its alpha. */
export function toHex(rgb) {
  return `#${rgb.slice(0, 3)
    .map((v) => Math.round(v).toString(16).padStart(2, '0').toUpperCase())
    .join('')}`;
}

/** Composite an [r, g, b, a] over an opaque hex background. */
export function composite(rgba, baseHex) {
  const base = parseHex(baseHex);
  const a = rgba[3];
  return [
    a * rgba[0] + (1 - a) * base[0],
    a * rgba[1] + (1 - a) * base[1],
    a * rgba[2] + (1 - a) * base[2],
  ];
}

// ── WCAG 2.1 relative luminance + contrast ────────────────────────

function channel(c) {
  const s = c / 255;
  return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
}

export function luminance(rgb) {
  return 0.2126 * channel(rgb[0]) + 0.7152 * channel(rgb[1]) + 0.0722 * channel(rgb[2]);
}

export function contrast(a, b) {
  const la = luminance(typeof a === 'string' ? parseHex(a) : a);
  const lb = luminance(typeof b === 'string' ? parseHex(b) : b);
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}

/** Resolve a token in a state and return it as [r, g, b, a]. */
export function colorIn(stateName, name) {
  return toRgba(resolveIn(stateName, name));
}
