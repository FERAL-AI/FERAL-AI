/**
 * The navigation mirror guard.
 *
 * This replaces the Dock/Hub mirror check. The old shape of the bug:
 * the Hub popup owned one hand-written list of destinations and the
 * Dock owned a second hand-written Set naming the same routes, purely
 * so the Hub button knew when to light up. Add a destination to one and
 * not the other and arriving on that route left EVERY control in the
 * Dock unlit. That is not a cosmetic regression, it is a navigation bar
 * that has stopped saying where you are, and it shipped twice.
 *
 * The structure is different now (one derived list in
 * shell/navigation.js) but the invariant it protects is the same one,
 * stated in both directions and against the real router:
 *
 *   1. Every static <Route> inside the Shell layout is in the index.
 *   2. Every entry in the index has a real <Route>.
 *   3. Every Dock tile is also indexed by the palette, so search can
 *      find the eight things people use most. The Hub could not: it
 *      excluded seven of the eight.
 *   4. Arriving on any indexed route lights exactly one Dock control.
 *   5. The Settings deep links match the sections Settings.jsx renders.
 */
import fs from 'node:fs';
import path from 'node:path';
import { describe, it, expect, afterEach, vi } from 'vitest';
import { renderV2 } from '../_helpers/renderV2';
import Dock from '../../shell/Dock';
import {
  DESTINATIONS, DOCK_ITEMS, DOCK_PATHS, PALETTE_ONLY,
  GO_ITEMS, SETTINGS_SECTIONS, isPaletteOnlyPath, dockPathFor,
} from '../../shell/navigation';

afterEach(() => {
  vi.unstubAllGlobals();
});

const SRC = path.resolve(__dirname, '../..');
const appSource = fs.readFileSync(path.join(SRC, 'App.jsx'), 'utf8');
const settingsSource = fs.readFileSync(path.join(SRC, 'pages/Settings.jsx'), 'utf8');

/**
 * The <Route path="…"> entries nested inside `<Route element={<Shell />}>`.
 *
 * Read out of the file rather than out of an exported constant on
 * purpose: the thing that decides whether a URL renders anything is the
 * JSX in App.jsx. A guard that compared the index against a second
 * exported list would be the exact two-lists-drifting shape this test
 * exists to prevent.
 */
function shellRoutePaths() {
  const start = appSource.indexOf('<Route element={<Shell />}>');
  expect(start).toBeGreaterThan(-1);
  const body = appSource.slice(start);
  const paths = [];
  const re = /<Route\s+path="([^"]+)"([^>]*)>/g;
  let m = re.exec(body);
  while (m) {
    const [, routePath, rest] = m;
    // Redirects are not destinations. `/ambient` and `*` both render
    // <Navigate>, and a redirect target is already indexed in its own
    // right, so indexing the source would put a duplicate tile in the
    // palette that lands somewhere else.
    const isRedirect = /element={<Navigate/.test(rest);
    // Parameterised routes have no literal URL a person can navigate
    // to from a list, so they are deliberately outside the index.
    const isDynamic = routePath.includes(':');
    if (!isRedirect && !isDynamic && routePath !== '*') paths.push(routePath);
    m = re.exec(body);
  }
  return paths;
}

describe('the navigation index mirrors the router', () => {
  it('indexes every static route the Shell mounts', () => {
    const routes = shellRoutePaths();
    const indexed = new Set(DESTINATIONS.map((d) => d.to));
    const missing = routes.filter((p) => !indexed.has(p));
    expect(missing).toEqual([]);
  });

  it('has a real route behind every indexed destination', () => {
    const routes = new Set(shellRoutePaths());
    const dangling = DESTINATIONS.map((d) => d.to).filter((p) => !routes.has(p));
    expect(dangling).toEqual([]);
  });

  it('finds at least the 23 destinations that shipped before the palette', () => {
    // A floor, not an equality: adding a destination is fine, silently
    // dropping one is what this catches.
    expect(DESTINATIONS.length).toBeGreaterThanOrEqual(23);
  });

  it('keeps every destination that was reachable from the old Dock and Hub', () => {
    // Verbatim from the shipped Dock PRIMARY_ITEMS + the Settings tile,
    // and HubLauncher's HUB_ITEMS. If a refactor drops one of these,
    // the user lost a page.
    const previouslyReachable = [
      // eight Dock slots
      '/', '/chat', '/flows', '/devices', '/apps', '/canvas', '/oversight', '/settings',
      // fifteen Hub items
      '/forge', '/skills', '/memory', '/wiki', '/agents', '/identity', '/health',
      '/intents', '/timeline', '/glass-brain', '/marketplace', '/webhooks',
      '/geofences', '/oversight', '/memory/context',
    ];
    const indexed = new Set(DESTINATIONS.map((d) => d.to));
    const lost = previouslyReachable.filter((p) => !indexed.has(p));
    expect(lost).toEqual([]);
  });
});

describe('the palette indexes what the Dock pins', () => {
  it('finds all eight Dock destinations', () => {
    // The defect: HubLauncher's fifteen items excluded seven of the
    // eight Dock primaries, so typing "chat" or "settings" into the
    // one search box in the app matched nothing.
    const indexed = new Set(GO_ITEMS.map((d) => d.to));
    const missing = DOCK_PATHS.filter((p) => !indexed.has(p));
    expect(missing).toEqual([]);
    expect(DOCK_ITEMS).toHaveLength(8);
  });

  it('gives every destination exactly one owner: a Dock tile or the palette', () => {
    const paletteOnly = new Set(PALETTE_ONLY.map((d) => d.to));
    for (const d of DESTINATIONS) {
      const tile = dockPathFor(d.to);
      // Exclusive-or: claimed by a tile, or palette-only, never both
      // and never neither.
      expect(Boolean(tile) !== paletteOnly.has(d.to)).toBe(true);
    }
  });

  it('lets a Dock tile claim its descendant routes, the way NavLink does', () => {
    // The defect this pins: NavLink without `end` is active for
    // descendants, so `/apps/publish` lights the `/apps` tile. A plain
    // set-membership test says `/apps/publish` has no tile, the palette
    // button lights too, and the Dock shows two active controls, which
    // tells the operator they are in two places at once.
    // Asserted against whatever the Dock actually pins, so this keeps
    // testing the NavLink descendant rule when the Dock's composition
    // changes. It used to hardcode /apps, which the approved design
    // moved to the palette.
    const pinned = DOCK_PATHS.find((d) => d !== '/');
    expect(dockPathFor(pinned)).toBe(pinned);
    expect(dockPathFor(`${pinned}/child`)).toBe(pinned);
    expect(isPaletteOnlyPath(`${pinned}/child`)).toBe(false);
    // `/` is exact-match only, mirroring `end` on its NavLink. Without
    // that, every route in the app would light whatever pins it. It is
    // only a Dock path when the Dock pins it, which it no longer does.
    expect(dockPathFor('/')).toBe(DOCK_PATHS.includes('/') ? '/' : '');
    const notPinned = DESTINATIONS.map((d) => d.to)
      .find((t) => t !== '/' && !DOCK_PATHS.includes(t));
    expect(dockPathFor(notPinned)).toBe('');
  });

  it('reports palette-only membership for a route with no tile, and not for one with a tile', () => {
    // /memory/context is a descendant of the pinned /memory tile now,
    // so it lights Memory, not the palette. That is the NavLink rule.
    expect(isPaletteOnlyPath('/memory/context')).toBe(false);
    expect(isPaletteOnlyPath('/glass-brain')).toBe(true);
    // Derived from the Dock rather than named, for the same reason.
    for (const to of DOCK_PATHS) expect(isPaletteOnlyPath(to)).toBe(false);
    const paletteOnly = DESTINATIONS.map((d) => d.to)
      .find((t) => t !== '/' && !DOCK_PATHS.some((d) => t === d || t.startsWith(`${d}/`)));
    expect(isPaletteOnlyPath(paletteOnly)).toBe(true);
    // An unknown path is nobody's: the Dock stays unlit rather than
    // claiming a route it does not own.
    expect(isPaletteOnlyPath('/not-a-route')).toBe(false);
  });
});

describe('every destination lights exactly one Dock control', () => {
  // This is the assertion the blank-dock bug would have failed. It runs
  // the real Dock at every indexed route.
  const lit = (container) => [...container.querySelectorAll('.v2-dock-btn')]
    .filter((el) => el.className.includes('is-active'))
    .map((el) => el.querySelector('.v2-dock-label')?.textContent);

  for (const dest of DESTINATIONS) {
    it(`lights one control on ${dest.to}`, () => {
      const { container, unmount } = renderV2(<Dock />, { route: dest.to });
      expect(lit(container)).toHaveLength(1);
      unmount();
    });
  }

  it('lights the destination tile itself for a Dock route', () => {
    const to = DOCK_PATHS.find((d) => d !== '/');
    const label = DOCK_ITEMS.find((d) => d.to === to).label;
    const { container } = renderV2(<Dock />, { route: to });
    expect(lit(container)).toEqual([label]);
  });

  it('lights the Command tile for a palette-only route', () => {
    // Chosen from the index rather than named: it must be a route no
    // Dock tile owns, including as a descendant. /memory/context used
    // to qualify and no longer does, because /memory is pinned now.
    const to = DESTINATIONS.map((d) => d.to)
      .find((t) => t !== '/' && !DOCK_PATHS.some((d) => t === d || t.startsWith(`${d}/`)));
    const { container } = renderV2(<Dock />, { route: to });
    expect(lit(container)).toEqual(['Command']);
  });

  it('keeps the safety surfaces one click away', () => {
    // This used to require /oversight to hold a Dock tile, on the
    // argument that a safety control you have to remember a path to is
    // one you will not find while something is going wrong. That
    // argument is still right; the approved design answers it
    // differently.
    //
    // The design pins "Needs you", which is where a blocked tool call
    // actually waits, and moves Oversight (the audit trail and kill
    // switch) into the palette's eighteen. Needs you is the surface you
    // touch during an incident; Oversight is the one you read after.
    //
    // So the invariant is now: the blocking surface is pinned, and the
    // kill switch is still reachable by name rather than buried inside
    // another page's header.
    const { container } = renderV2(<Dock />);
    const labels = [...container.querySelectorAll('.v2-dock-label')]
      .map((el) => el.textContent);
    expect(labels).toContain('Needs you');
    expect(DESTINATIONS.map((d) => d.to)).toContain('/oversight');
  });
});

describe('Settings deep links match the sections Settings.jsx renders', () => {
  it('names the same sixteen sections, in the same order', () => {
    const m = settingsSource.match(/const SECTIONS = \[([\s\S]*?)\];/);
    expect(m).toBeTruthy();
    const actual = [...m[1].matchAll(/'([^']+)'/g)].map((x) => x[1]);
    expect(actual).toEqual(SETTINGS_SECTIONS);
  });

  it('offers each section as a ?section= deep link', () => {
    const links = GO_ITEMS.filter((i) => i.to.startsWith('/settings?section='));
    expect(links).toHaveLength(SETTINGS_SECTIONS.length);
    for (const s of SETTINGS_SECTIONS) {
      expect(links.some((l) => l.to === `/settings?section=${encodeURIComponent(s)}`)).toBe(true);
    }
  });
});
