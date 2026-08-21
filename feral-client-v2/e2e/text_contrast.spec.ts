/**
 * Quiet text still has to be readable (WCAG 2.2 SC 1.4.3, AA, 4.5:1).
 *
 * `--v2-text-tertiary` is not decoration. It dresses empty-state hints,
 * every ghost-button label, job counts, rail placeholders and more,
 * across most of the app, so it carries the body-text obligation.
 *
 * It had been tuned by arithmetic against a *nominal* ground colour,
 * and light glass composites toward white, so the panel a reader
 * actually sees is lighter than the value the sums assumed. Measured in
 * a browser against the real composited ground it was 4.46:1 in light:
 * under AA by four hundredths, invisible to any check that does not
 * walk the real ancestor chain. #676C74 measures 4.60:1 there.
 *
 * This composites the way the browser does, walking up for the first
 * opaque ancestor background and blending any alpha along the way.
 * jsdom returns no computed colours worth reading and has no layout, so
 * this cannot be a vitest test.
 *
 * Caveat kept honest: backdrop-filter blur contributions are not
 * modelled, so a value within a few hundredths of the line should be
 * re-measured by hand rather than trusted from here.
 */
import { test, expect } from '@playwright/test';

const AA = 4.5;

/** Elements that carry quiet text, and a route each is reachable on. */
const ROUTES = ['/console', '/', '/jobs', '/settings', '/skills'];

const PROBE = `(() => {
  const f = (c) => { c /= 255; return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4); };
  const lum = ([r, g, b]) => 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
  const parse = (s) => (s.match(/[\\d.]+/g) || []).map(Number);
  const over = (fg, bg, a) => fg.map((c, i) => c * a + bg[i] * (1 - a));
  const groundOf = (el) => {
    let n = el;
    while (n && n !== document.documentElement) {
      const v = parse(getComputedStyle(n).backgroundColor);
      if (v.length >= 3 && (v[3] === undefined || v[3] > 0.999)) return v.slice(0, 3);
      if (v.length === 4 && v[3] > 0) return over(v.slice(0, 3), groundOf(n.parentElement), v[3]);
      n = n.parentElement;
    }
    return [255, 255, 255];
  };
  const sels = ['.v2-empty-state-hint', '.v2-btn--ghost', '.v2-jobs-count',
                '.v2-rail-quiet', '.v2-stack-quiet', '.v2-cp-turn-id',
                '.v2-sysbar-vital', '.v2-settings-btn.is-active'];
  const out = [];
  for (const sel of sels) {
    for (const el of document.querySelectorAll(sel)) {
      const cs = getComputedStyle(el);
      if (!(el.textContent || '').trim()) continue;
      const bg = groundOf(el.parentElement);
      const fgv = parse(cs.color);
      const fg = fgv.length === 4 && fgv[3] < 1 ? over(fgv.slice(0, 3), bg, fgv[3]) : fgv.slice(0, 3);
      const L1 = lum(fg), L2 = lum(bg);
      const ratio = (Math.max(L1, L2) + 0.05) / (Math.min(L1, L2) + 0.05);
      out.push({ sel, ratio: Math.round(ratio * 100) / 100, fg: cs.color,
                 bg: 'rgb(' + bg.map(Math.round).join(',') + ')' });
      break;
    }
  }
  return out;
})()`;

for (const theme of ['light', 'dark']) {
  test(`quiet text clears AA in ${theme} mode`, async ({ page }) => {
    await page.route('**/api/**', (r) => r.fulfill({
      status: 200, contentType: 'application/json', body: '{}',
    }));
    // The app reads its theme from localStorage, not prefers-color-scheme,
    // so setting the emulated colour scheme alone measures light twice.
    await page.addInitScript((t) => {
      try { localStorage.setItem('feral_ui_theme', t); } catch { /* private mode */ }
    }, theme);

    const failures: string[] = [];
    for (const route of ROUTES) {
      await page.goto(route);
      await page.waitForTimeout(250);

      // useTheme stamps classes, not the data attribute: applyTheme
      // toggles `v2-light` / `v2-dark` on the root. Asserting on
      // [data-theme] here read null and measured light twice.
      const applied = await page.evaluate(() => (
        document.documentElement.classList.contains('v2-dark') ? 'dark'
          : document.documentElement.classList.contains('v2-light') ? 'light'
            : '(unstamped)'
      ));
      expect(applied, `theme did not apply on ${route}`).toBe(theme);

      const rows = await page.evaluate(PROBE) as
        { sel: string; ratio: number; fg: string; bg: string }[];
      for (const r of rows) {
        if (r.ratio < AA) {
          failures.push(`${route} ${r.sel} ${r.ratio}:1 (${r.fg} on ${r.bg})`);
        }
      }
    }

    expect(failures, `below AA in ${theme}: ${failures.join(' | ')}`).toEqual([]);
  });
}
