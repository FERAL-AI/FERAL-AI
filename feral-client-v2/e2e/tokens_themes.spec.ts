/**
 * Theme tokens, measured in a real browser.
 *
 * The vitest guards in src/__tests__/styles/tokens_defined.test.js model
 * the cascade. This measures it. jsdom does not load stylesheets at all,
 * so nothing in the unit suite can see what a token actually computes
 * to, and the failure this is aimed at is invisible in source:
 *
 *   a custom property whose var() chain does not resolve is not an
 *   error. The declaration that reads it becomes invalid at
 *   computed-value time and silently falls back to the property's
 *   initial value. `border: 1px solid var(--v2-border)` with
 *   --v2-border undeclared draws no border, `background:
 *   var(--v2-surface)` renders fully transparent, and the console says
 *   nothing. Four tokens were in exactly that state
 *   (--v2-border, --v2-border-subtle, --v2-surface, --v2-font-sans).
 *
 * The other half is theme completeness. A token declared only inside
 * @media (prefers-color-scheme: dark) is absent in the un-stamped state,
 * which is what the page renders as before the theme class is applied.
 * So all three states are measured, plus the two the :not() guards
 * exist for.
 *
 * Every number here is read back out of Chromium, not asserted from the
 * stylesheet text.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { test, expect, type Page } from '@playwright/test';

// package.json sets "type": "module", so __dirname is not defined here.
const HERE = path.dirname(fileURLToPath(import.meta.url));
const TOKENS_CSS = fs.readFileSync(
  path.resolve(HERE, '../src/styles/tokens.css'),
  'utf8',
);

/** Every --v2-* the sheet declares, comments stripped so prose is not a name. */
const SEMANTIC_NAMES = [
  ...new Set(
    [...TOKENS_CSS.replace(/\/\*[\s\S]*?\*\//g, '').matchAll(/(--v2-[\w-]+)\s*:/g)]
      .map((m) => m[1]),
  ),
].sort();

/** The subset that paints a flat colour, so it can be read back as rgb(). */
// --v2-surface, --v2-border and --v2-border-subtle are deliberately
// absent. They were referenced with no fallback and declared nowhere;
// commit 268fa7c1f fixed that by pointing every consumer at a token
// that exists, so declaring them would leave tokens nothing reads.
const COLOR_NAMES = [
  '--v2-bg-base',
  '--v2-bg-deep',
  '--v2-surface-0',
  '--v2-surface-1',
  '--v2-surface-2',
  '--v2-surface-elev',
  '--v2-hairline',
  '--v2-hairline-strong',
  '--v2-hairline-focus',
  '--v2-fill-subtle',
  '--v2-fill',
  '--v2-fill-strong',
  '--v2-fill-heavy',
  '--v2-scrim',
  '--v2-well',
  '--v2-ambient-spot-a',
  '--v2-ambient-spot-b',
  '--v2-ambient-base',
  '--v2-text-primary',
  '--v2-text-secondary',
  '--v2-text-tertiary',
  '--v2-text-inverse',
  '--v2-on-accent',
  '--v2-accent',
  '--v2-accent-soft',
  '--v2-accent-ring',
  '--v2-accent-text',
  '--v2-state-live',
  '--v2-state-live-soft',
  '--v2-state-warn',
  '--v2-state-warn-soft',
  '--v2-state-error',
  '--v2-state-error-soft',
  '--v2-surface-immersive',
  '--v2-on-immersive',
];

type Stamp = { attr?: string; cls?: string } | null;
type Measurement = {
  colorScheme: string;
  declared: Record<string, string>;
  painted: Record<string, string>;
};

async function measure(page: Page, stamp: Stamp, names: string[], colorNames: string[]) {
  return page.evaluate(
    ({ stamp: s, names: n, colorNames: c }) => {
      const root = document.documentElement;
      // Start from a genuinely bare root. The client stamps v2-light on
      // boot, and the un-stamped state is precisely what has to be
      // measured, so clear it first.
      root.classList.remove('v2-light', 'v2-dark');
      root.removeAttribute('data-theme');
      if (s?.attr) root.setAttribute('data-theme', s.attr);
      if (s?.cls) root.classList.add(s.cls);

      const cs = getComputedStyle(root);
      const declared: Record<string, string> = {};
      for (const name of n) declared[name] = cs.getPropertyValue(name).trim();

      const probe = document.createElement('div');
      probe.style.position = 'fixed';
      probe.style.left = '-9999px';
      document.body.appendChild(probe);
      const painted: Record<string, string> = {};
      for (const name of c) {
        probe.style.backgroundColor = '';
        probe.style.setProperty('background-color', `var(${name})`);
        painted[name] = getComputedStyle(probe).backgroundColor;
      }
      probe.remove();

      return { colorScheme: cs.colorScheme, declared, painted } as {
        colorScheme: string;
        declared: Record<string, string>;
        painted: Record<string, string>;
      };
    },
    { stamp, names, colorNames },
  );
}

async function open(page: Page) {
  await page.route('**/api/**', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ ok: true, items: [], devices: [], skills: [], sessions: [] }),
  }));
  await page.goto('/');
  await page.waitForSelector('body');
}

/** Transparent black is what an unresolvable var() collapses to. */
const UNRESOLVED = 'rgba(0, 0, 0, 0)';

test.describe('token themes, measured in Chromium', () => {
  test('light: bare root, system light', async ({ page }) => {
    await open(page);
    const m = await measure(page, null, SEMANTIC_NAMES, COLOR_NAMES);
    expectAllDeclared(m);
    expect(m.colorScheme).toBe('light dark');
    expect(m.painted['--v2-bg-base']).toBe('rgb(236, 238, 243)');
    expect(m.painted['--v2-text-primary']).toBe('rgb(11, 11, 13)');
    expect(m.painted['--v2-surface-0']).toBe('rgba(255, 255, 255, 0.48)');
    expect(m.painted['--v2-hairline']).toBe('rgba(0, 0, 0, 0.08)');
    expect(m.painted['--v2-accent']).toBe('rgb(0, 102, 204)');
    expect(m.painted['--v2-state-live']).toBe('rgb(23, 118, 54)');
  });

  test.describe('system dark', () => {
    test.use({ colorScheme: 'dark' });

    test('dark: bare root, prefers-color-scheme dark', async ({ page }) => {
      await open(page);
      const m = await measure(page, null, SEMANTIC_NAMES, COLOR_NAMES);
      expectAllDeclared(m);
      expect(m.colorScheme).toBe('dark');
      expect(m.painted['--v2-bg-base']).toBe('rgb(22, 22, 28)');
      expect(m.painted['--v2-text-primary']).toBe('rgb(245, 245, 247)');
      expect(m.painted['--v2-surface-0']).toBe('rgba(22, 22, 26, 0.38)');
      expect(m.painted['--v2-hairline']).toBe('rgba(255, 255, 255, 0.1)');
      expect(m.painted['--v2-accent']).toBe('rgb(10, 132, 255)');
      expect(m.painted['--v2-state-live']).toBe('rgb(48, 209, 88)');
    });

    test('an explicit light choice beats a dark system preference', async ({ page }) => {
      await open(page);
      const attr = await measure(page, { attr: 'light' }, SEMANTIC_NAMES, COLOR_NAMES);
      const cls = await measure(page, { cls: 'v2-light' }, SEMANTIC_NAMES, COLOR_NAMES);
      for (const m of [attr, cls]) {
        expectAllDeclared(m);
        expect(m.colorScheme).toBe('light');
        expect(m.painted['--v2-bg-base']).toBe('rgb(236, 238, 243)');
        expect(m.painted['--v2-text-primary']).toBe('rgb(11, 11, 13)');
        expect(m.painted['--v2-hairline']).toBe('rgba(0, 0, 0, 0.08)');
      }
      expect(cls.painted).toEqual(attr.painted);
    });
  });

  test('an explicit dark choice beats a light system preference', async ({ page }) => {
    await open(page);
    const attr = await measure(page, { attr: 'dark' }, SEMANTIC_NAMES, COLOR_NAMES);
    const cls = await measure(page, { cls: 'v2-dark' }, SEMANTIC_NAMES, COLOR_NAMES);
    for (const m of [attr, cls]) {
      expectAllDeclared(m);
      expect(m.colorScheme).toBe('dark');
      expect(m.painted['--v2-bg-base']).toBe('rgb(22, 22, 28)');
      expect(m.painted['--v2-text-primary']).toBe('rgb(245, 245, 247)');
      expect(m.painted['--v2-surface-0']).toBe('rgba(22, 22, 26, 0.38)');
      expect(m.painted['--v2-hairline']).toBe('rgba(255, 255, 255, 0.1)');
      expect(m.painted['--v2-accent']).toBe('rgb(10, 132, 255)');
    }
    expect(cls.painted).toEqual(attr.painted);
  });

  test('the three states agree where they should and differ where they should', async ({
    browser,
  }) => {
    const lightCtx = await browser.newContext({ colorScheme: 'light' });
    const darkCtx = await browser.newContext({ colorScheme: 'dark' });
    const lightPage = await lightCtx.newPage();
    const darkPage = await darkCtx.newPage();
    await open(lightPage);
    await open(darkPage);

    const bare = await measure(lightPage, null, SEMANTIC_NAMES, COLOR_NAMES);
    const media = await measure(darkPage, null, SEMANTIC_NAMES, COLOR_NAMES);
    const stamped = await measure(lightPage, { attr: 'dark' }, SEMANTIC_NAMES, COLOR_NAMES);

    // The explicit toggle must reproduce the system-dark theme exactly.
    expect(stamped.painted).toEqual(media.painted);
    // And dark must actually be a different theme from light.
    const differing = COLOR_NAMES.filter((n) => bare.painted[n] !== media.painted[n]);
    expect(differing.length).toBeGreaterThan(25);

    await lightCtx.close();
    await darkCtx.close();
  });

  test('a component that reads a token actually paints it', async ({ page }) => {
    await open(page);
    // End to end rather than token-level: pages.css builds this from
    // var(--v2-border) and var(--v2-surface), both of which resolved to
    // nothing before the palette split, so the element rendered with no
    // border and a transparent background.
    const result = await page.evaluate(() => {
      const el = document.createElement('button');
      el.className = 'v2-chat-trace-toggle';
      document.body.appendChild(el);
      const cs = getComputedStyle(el);
      const out: Record<string, string> = {
        borderColor: cs.borderTopColor,
        borderStyle: cs.borderTopStyle,
        borderWidth: cs.borderTopWidth,
        background: cs.backgroundColor,
      };
      el.remove();

      // --v2-font-sans was the fourth undeclared token. A dropped
      // font-family is even quieter than a dropped border: the label
      // simply inherits whatever the parent had.
      const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      const label = document.createElementNS('http://www.w3.org/2000/svg', 'text');
      label.setAttribute('class', 'v2-mindmap-label');
      svg.appendChild(label);
      document.body.appendChild(svg);
      out.labelFont = getComputedStyle(label).fontFamily;
      svg.remove();
      return out;
    });
    expect(result.borderStyle).toBe('solid');
    expect(result.borderWidth).toBe('1px');
    expect(result.borderColor).not.toBe(UNRESOLVED);
    expect(result.background).not.toBe(UNRESOLVED);
    // The system stack, not the document default.
    expect(result.labelFont).toContain('-apple-system');
  });
});

function expectAllDeclared(m: Measurement) {
  // An unresolvable var() chain makes getPropertyValue return the empty
  // string, and makes anything painted with it collapse to transparent.
  const undeclared = Object.entries(m.declared)
    .filter(([, v]) => v === '')
    .map(([k]) => k);
  expect(undeclared, `tokens that resolve to nothing: ${undeclared.join(', ')}`).toEqual([]);

  const transparent = Object.entries(m.painted)
    .filter(([, v]) => v === UNRESOLVED)
    .map(([k]) => k);
  expect(transparent, `tokens that paint nothing: ${transparent.join(', ')}`).toEqual([]);
}
