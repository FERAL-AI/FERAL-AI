import {
  MessageSquare, ListChecks, Cpu, LayoutDashboard, SquareStack,
  Settings as SettingsIcon, AppWindow, Shield,
  Hammer, Wrench, Database, BookOpen, Users, UserCircle2,
  HeartPulse, Crosshair, Clock, BrainCircuit, Globe, MapPin, Store,
  BrainCog, Upload, ShieldAlert, Undo2, FolderLock, Gauge, Activity,
} from 'lucide-react';

/**
 * The navigation index. One list, one source of truth.
 *
 * The defect this file exists to kill: the Dock used to carry a
 * hand-written `HUB_ROUTES` Set that restated, by hand, which routes
 * lived behind the Hub popup. The popup's own item list lived in a
 * different file. Adding a destination to one and not the other left
 * every button in the Dock unlit on arrival, because the Hub button's
 * highlight was driven by the Set and nothing else claimed the route.
 * A blank dock is what a user sees; a stale Set is what a reviewer
 * sees, and they do not look the same.
 *
 * Now the two are the same list. `DESTINATIONS` is every shell route a
 * person can navigate to. `DOCK_ITEMS` is the subset pinned to the
 * Dock, spliced out of that list by path. "Behind the palette" is
 * computed as `DESTINATIONS - DOCK_ITEMS`, so it cannot go stale: add a
 * route to `DESTINATIONS` and it is, that instant, in the palette, in
 * search, and lighting the palette button when you arrive on it.
 *
 * `src/__tests__/shell/navigation.index.test.jsx` pins this list against
 * the real <Route> elements in App.jsx in both directions.
 */

/**
 * Every static destination inside the Shell layout route.
 *
 * `group` is display grouping in the palette only; it carries no
 * behaviour. Dynamic routes (`/apps/:app_id`) are deliberately absent:
 * they have no navigable literal path, they are reached by clicking a
 * specific app. The guard test knows to skip parameterised routes.
 */
export const DESTINATIONS = [
  // Core loop
  // Console is the default landing view, and the design's headline is
  // why: "the default view is the machine, not a transcript. Chat is
  // one place you go." Home stays reachable by name for anyone who
  // wants the briefing.
  { to: '/console', label: 'Console', Icon: Gauge, desc: 'What the machine is doing right now', group: 'Core' },
  { to: '/jobs', label: 'Jobs', Icon: Activity, desc: 'Everything running, across all six sources', group: 'Core' },
  { to: '/', label: 'Home', Icon: LayoutDashboard, desc: 'Overview, resume where you left off', group: 'Core' },
  { to: '/chat', label: 'Chat', Icon: MessageSquare, desc: 'Talk to the brain', group: 'Core' },
  { to: '/canvas', label: 'Canvas', Icon: SquareStack, desc: 'Gen-UI surfaces the brain drew', group: 'Core' },

  // Work
  { to: '/flows', label: 'Flows', Icon: ListChecks, desc: 'Task flows and runs', group: 'Work' },
  { to: '/intents', label: 'Intents', Icon: Crosshair, desc: 'Goal plans and today', group: 'Work' },
  { to: '/timeline', label: 'Timeline', Icon: Clock, desc: 'Chronological activity', group: 'Work' },

  // Capability
  { to: '/apps', label: 'Apps', Icon: AppWindow, desc: 'Installed apps and their surfaces', group: 'Capability' },
  { to: '/apps/publish', label: 'Publish an app', Icon: Upload, desc: 'Package and publish to the registry', group: 'Capability' },
  { to: '/marketplace', label: 'Market', Icon: Store, desc: 'Browse and install registry items', group: 'Capability' },
  { to: '/skills', label: 'Skills', Icon: Wrench, desc: 'Loaded skills and hot-reload', group: 'Capability' },
  { to: '/forge', label: 'Forge', Icon: Hammer, desc: 'Tool Genesis drafts and promote', group: 'Capability' },
  { to: '/webhooks', label: 'Webhooks', Icon: Globe, desc: 'Inbound integrations', group: 'Capability' },

  // Knowledge
  { to: '/memory', label: 'Memory', Icon: Database, desc: 'Notes, episodes, execution log', group: 'Knowledge' },
  { to: '/memory/context', label: 'Memory context', Icon: BrainCog, desc: 'What multi-memory surfaced per LLM turn', group: 'Knowledge' },
  { to: '/wiki', label: 'Wiki', Icon: BookOpen, desc: 'Long-form knowledge and ingest', group: 'Knowledge' },
  { to: '/identity', label: 'Identity', Icon: UserCircle2, desc: 'IDENTITY / SOUL / MEMORY editors', group: 'Knowledge' },

  // Hardware
  { to: '/devices', label: 'Devices', Icon: Cpu, desc: 'Paired nodes, pairing, topology', group: 'Hardware' },
  { to: '/geofences', label: 'Places', Icon: MapPin, desc: 'Geofences and location', group: 'Hardware' },
  { to: '/health', label: 'Health', Icon: HeartPulse, desc: 'Baseline metrics and alerts', group: 'Hardware' },

  // Oversight. /oversight holds the kill switch, so it is pinned to the
  // Dock as well as indexed here: a safety control the operator has to
  // remember a path to is one they will not find while something is
  // going wrong.
  { to: '/oversight', label: 'Oversight', Icon: Shield, desc: 'Supervisor audit and kill switch', group: 'Oversight' },
  { to: '/glass-brain', label: 'Brain', Icon: BrainCircuit, desc: 'Live 3D Glass Brain', group: 'Oversight' },
  { to: '/agents', label: 'Agents', Icon: Users, desc: 'Agent Mitosis specialists', group: 'Oversight' },
  // These three arrived after this index was first written. Each has a
  // working REST API that had no UI at any click cost: GET/POST
  // /api/approvals, POST /api/checkpoints/revert, and GET/POST/DELETE
  // /api/security/grants. Approvals is the load-bearing one: a tool call
  // raised by a cron job, a channel or the phone blocks until someone
  // answers it, so it has to be reachable from wherever you are.
  { to: '/approvals', label: 'Needs you', Icon: ShieldAlert, desc: 'Tool calls blocked on your decision', group: 'Oversight' },
  { to: '/checkpoints', label: 'Undo', Icon: Undo2, desc: 'Put back files a turn wrote', group: 'Oversight' },
  { to: '/grants', label: 'Folders', Icon: FolderLock, desc: 'Which folders FERAL can use', group: 'Oversight' },

  { to: '/settings', label: 'Settings', Icon: SettingsIcon, desc: 'Every knob, sixteen sections', group: 'System' },
];

/**
 * The paths pinned to the Dock, in Dock order. Each must exist in
 * `DESTINATIONS`; the guard test enforces that, because a Dock path
 * with no destination entry is a tile the palette cannot find.
 */
// The eight the approved design pins, verbatim: "Console, Chat, Needs
// you, Jobs, Skills, Memory, Devices, Settings. Chosen because you
// return to them, not because they are important."
//
// The previous eight (Home, Chat, Flows, Devices, Apps, Canvas,
// Oversight, Settings) were picked on importance, which is exactly the
// selection rule the design rejects: Oversight matters enormously and
// you visit it twice a year, so it belongs in the palette, while Needs
// you blocks work every day and had no tile at all.
export const DOCK_PATHS = [
  '/console', '/chat', '/approvals', '/jobs',
  '/skills', '/memory', '/devices', '/settings',
];

const byPath = new Map(DESTINATIONS.map((d) => [d.to, d]));

/** Dock tiles, resolved from DESTINATIONS so labels and icons agree. */
export const DOCK_ITEMS = DOCK_PATHS.map((to) => {
  const found = byPath.get(to);
  if (!found) {
    // Not a silent fallback: a Dock path with no destination is a
    // programming error and the guard test fails on it. Throwing here
    // means it also fails loudly in a browser instead of rendering a
    // tile with no label.
    throw new Error(`Dock path ${to} is not in DESTINATIONS`);
  }
  return found;
});

const dockPathSet = new Set(DOCK_PATHS);

/**
 * Destinations that have no Dock tile. Derived, never restated.
 * These are the routes on which the palette button carries the active
 * highlight, so that arriving anywhere leaves exactly one lit control.
 */
export const PALETTE_ONLY = DESTINATIONS.filter((d) => !dockPathFor(d.to));

/**
 * The Dock tile that claims `pathname`, or '' if none does.
 *
 * This has to reproduce NavLink's matching rule, not a set membership
 * test, and getting that wrong is a real defect the guard test caught:
 * NavLink is active for descendants, so on `/apps/publish` the `/apps`
 * tile lights up. A plain `DOCK_PATHS.includes(pathname)` says
 * `/apps/publish` has no tile, the palette button lights as well, and
 * the Dock shows two active controls at once, which tells the operator
 * they are in two places.
 *
 * `/` is exact-match only, mirroring the `end` prop on its NavLink.
 */
export function dockPathFor(pathname) {
  if (!pathname) return '';
  // `/` is exact-match only: every path starts with it, so prefix
  // matching would make it claim the whole app. It is still only a Dock
  // path when the Dock actually pins it. This used to return '/'
  // unconditionally, which was true while Home held a tile; once the
  // approved design replaced it with Console, `/` claimed a tile that
  // does not exist AND reported itself as not palette-only, so arriving
  // on Home lit nothing at all.
  if (pathname === '/') return dockPathSet.has('/') ? '/' : '';
  let best = '';
  for (const to of DOCK_PATHS) {
    if (to === '/') continue;
    if (pathname === to || pathname.startsWith(`${to}/`)) {
      // Longest match wins, so a future nested Dock tile beats its parent.
      if (to.length > best.length) best = to;
    }
  }
  return best;
}

/**
 * True when `pathname` is a known destination that no Dock tile claims.
 * This is the palette button's active state.
 */
export function isPaletteOnlyPath(pathname) {
  return byPath.has(pathname) && !dockPathFor(pathname);
}

/**
 * Settings sections, deep-linkable through `?section=`. Settings.jsx
 * reads that param and selects the panel, so these are real
 * destinations rather than labels; the guard test pins this list
 * against the `SECTIONS` array in pages/Settings.jsx.
 */
export const SETTINGS_SECTIONS = [
  'Self', 'General', 'Providers', 'Memory', 'Channels', 'Autonomy', 'Voice',
  'Access', 'Twin', 'Security', 'Integrations', 'Cost', 'Sync', 'Handoff', 'Push', 'MCP',
];

/** `?section=` deep links, indexed by the palette alongside routes. */
export const SETTINGS_DESTINATIONS = SETTINGS_SECTIONS.map((name) => ({
  to: `/settings?section=${encodeURIComponent(name)}`,
  label: `Settings: ${name}`,
  Icon: SettingsIcon,
  desc: `Open the ${name} settings section`,
  group: 'System',
}));

/** Everything the palette's Go section offers. */
export const GO_ITEMS = [...DESTINATIONS, ...SETTINGS_DESTINATIONS];

/** Case-insensitive match over label, path and description. */
export function matchesQuery(item, query) {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  return (
    item.label.toLowerCase().includes(q)
    || String(item.to || '').toLowerCase().includes(q)
    || String(item.desc || '').toLowerCase().includes(q)
  );
}
