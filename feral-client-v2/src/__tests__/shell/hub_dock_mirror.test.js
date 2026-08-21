/**
 * The Hub list and the Dock's exclusion set must not drift.
 *
 * `HUB_ROUTES` in Dock.jsx is a hand-maintained mirror of `HUB_ITEMS` in
 * HubLauncher.jsx, and the comment above it says what happens when they
 * disagree: the Dock has no active entry for a Hub route, so arriving at
 * one makes the dock go blank. Adding a Hub item and forgetting the
 * mirror is a silent, one-line mistake with a visible symptom nobody
 * traces back to the list.
 *
 * Adding /approvals was exactly that opportunity, so this pins it.
 */
import fs from 'node:fs';
import path from 'node:path';
import { describe, it, expect } from 'vitest';

const SRC = path.resolve(__dirname, '../..');

function read(rel) {
  return fs.readFileSync(path.join(SRC, rel), 'utf8');
}

/** Routes declared in HubLauncher's HUB_ITEMS. */
function hubItemRoutes() {
  const src = read('components/HubLauncher.jsx');
  const block = src.slice(src.indexOf('const HUB_ITEMS'), src.indexOf('];', src.indexOf('const HUB_ITEMS')));
  return new Set([...block.matchAll(/to:\s*'([^']+)'/g)].map((m) => m[1]));
}

/** Routes listed in Dock's HUB_ROUTES exclusion set. */
function dockMirrorRoutes() {
  const src = read('shell/Dock.jsx');
  const block = src.slice(src.indexOf('const HUB_ROUTES'), src.indexOf(']);', src.indexOf('const HUB_ROUTES')));
  return new Set([...block.matchAll(/'([^']+)'/g)].map((m) => m[1]));
}

/** Routes with their own primary NavLink in the Dock. */
function dockPrimaryRoutes() {
  const src = read('shell/Dock.jsx');
  const block = src.slice(0, src.indexOf('const HUB_ROUTES'));
  return new Set([...block.matchAll(/to:\s*'([^']+)'/g)].map((m) => m[1]));
}

describe('Hub and Dock stay in sync', () => {
  it('every Hub item either sits in the mirror or has its own Dock link', () => {
    // /oversight is deliberately absent from HUB_ROUTES because it has a
    // primary NavLink that highlights itself. The rule is therefore not
    // "every Hub item is in the mirror", it is "every Hub item can light
    // something up", and a route in neither set makes the Dock go blank.
    const mirror = dockMirrorRoutes();
    const primary = dockPrimaryRoutes();
    const orphaned = [...hubItemRoutes()].filter((r) => !mirror.has(r) && !primary.has(r));
    expect(orphaned, `Hub items that would blank the Dock: ${orphaned.join(', ')}`).toEqual([]);
  });

  it('the mirror does not list routes the Hub dropped', () => {
    const hub = hubItemRoutes();
    // /memory/context is a legitimate sub-route of a Hub item and is
    // reached from GlassBrain, so it is allowed to be in the mirror
    // without its own tile.
    const allowedExtras = new Set(['/memory/context']);
    const stale = [...dockMirrorRoutes()].filter((r) => !hub.has(r) && !allowedExtras.has(r));
    expect(stale, `Dock HUB_ROUTES lists routes no Hub item declares: ${stale.join(', ')}`).toEqual([]);
  });

  it('every Hub route is a real route in App.jsx', () => {
    const app = read('App.jsx');
    const declared = new Set([...app.matchAll(/path="([^"]+)"/g)].map((m) => m[1]));
    const unrouted = [...hubItemRoutes()].filter((r) => !declared.has(r));
    expect(unrouted, `Hub items with no <Route>: ${unrouted.join(', ')}`).toEqual([]);
  });

  it('approvals is reachable, which is the whole point of adding it', () => {
    expect(hubItemRoutes().has('/approvals')).toBe(true);
    expect(read('App.jsx')).toContain('path="/approvals"');
  });
});
