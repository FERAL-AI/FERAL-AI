/**
 * Every `var(--v2-*)` the app references must actually be defined.
 *
 * An undefined custom property does not fall back to something sensible
 * and does not warn. Measured in Chrome 141 against a page that used one:
 *
 *   border-left: 2px solid var(--v2-border)   -> borderLeftWidth "0px"
 *   stroke: var(--v2-border-subtle)           -> stroke "none"
 *
 * The whole declaration is invalid at computed-value time, so a border
 * vanishes rather than turning the wrong colour, and an SVG stroke stops
 * painting. Both were live: `--v2-border` and `--v2-border-subtle` were
 * referenced six times across pages.css and ConsciousnessMindMap.jsx and
 * defined nowhere, so those borders had no width and those mind-map
 * edges were invisible. Nothing failed, and no test noticed.
 */
import fs from 'node:fs';
import path from 'node:path';
import { describe, it, expect } from 'vitest';

const SRC = path.resolve(__dirname, '../..');

function walk(dir, out = []) {
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) {
      if (e.name === '__tests__' || e.name === 'node_modules') continue;
      walk(p, out);
    } else if (/\.(css|jsx?|tsx?)$/.test(e.name)) {
      out.push(p);
    }
  }
  return out;
}

const files = walk(SRC);

/** Tokens defined anywhere, as `--v2-foo:`. */
const defined = new Set();
for (const f of files) {
  for (const m of fs.readFileSync(f, 'utf8').matchAll(/(--v2-[a-z0-9-]+)\s*:/g)) {
    defined.add(m[1]);
  }
}

describe('design tokens', () => {
  it('has a definition for every token that is referenced', () => {
    const missing = new Map();
    for (const f of files) {
      const text = fs.readFileSync(f, 'utf8');
      for (const m of text.matchAll(/var\(\s*(--v2-[a-z0-9-]+)/g)) {
        if (!defined.has(m[1])) {
          const rel = path.relative(SRC, f);
          if (!missing.has(m[1])) missing.set(m[1], new Set());
          missing.get(m[1]).add(rel);
        }
      }
    }
    const report = [...missing.entries()]
      .map(([tok, where]) => `${tok} referenced in ${[...where].join(', ')}`)
      .join('\n');
    expect(report, `undefined design tokens:\n${report}`).toBe('');
  });

  it('defines the theme-varying tokens in all three theme states', () => {
    // The viewer has three states: bare :root (light), the
    // prefers-color-scheme dark media query, and an explicit
    // [data-theme="dark"] stamp. A colour token defined in fewer than
    // all three renders one theme's ink on the other theme's ground.
    const tokens = fs.readFileSync(path.join(SRC, 'styles/tokens.css'), 'utf8');
    for (const tok of ['--v2-hairline', '--v2-surface-0', '--v2-text-primary', '--v2-state-error']) {
      const count = [...tokens.matchAll(new RegExp(`${tok}\\s*:`, 'g'))].length;
      expect(count, `${tok} is defined ${count} time(s), expected 3`).toBe(3);
    }
  });
});
