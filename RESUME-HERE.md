# FERAL session record, 2026-08-21 to 2026-08-22

**2026.8.14 is released and live on PyPI.** Everything below is done and
merged unless a section says otherwise.

This file replaces the earlier pause-point notes. It is the durable
record of what the seven audit lanes found, because the worktrees they
ran in are disposable and their reports were not written down anywhere
else.

---

## What shipped

Two releases in this session.

**2026.8.13** ("surfaces that painted correctly and did nothing") and
**2026.8.14** ("the surfaces nobody had driven"). 8.14 exists because
8.13 shipped a regression: the Skills page could not load on it.

Final gates on the tagged commit `ca12e7753`:

| gate | result |
|---|---|
| pytest | 9893 passed, 32 skipped, 0 failed |
| vitest | 1251 passed / 157 files |
| e2e (vite preview) | 98 passed |
| real-brain e2e (new, opt-in) | 61 passed, 28 routes, 1003 controls, 51 REST paths |
| CI on main | green, both Linux pytest legs |
| published wheel | 70 dashboard files, 59 fonts, 3 icons, all CSS refs resolving |

---

## The one theme

Almost every defect this session was the same shape: **a surface that
rendered, passed its tests, and had never been connected to anything.**

Not crashes. Not exceptions. Green suites over dead wiring. The reason
so much of it survived so long is that the tests asserted markup while
the failures were in layout, in field names, or in whether a click
landed at all.

Three sub-patterns worth remembering:

1. **Reading a field that does not exist.** `cost_today`, `tokens_used`,
   `autonomy`, `health.cognitive_load`, `stats.active`,
   `status_by_channel`, jobs `description`, `memory.tokens`. Every one
   read off a payload that has never contained it. The value silently
   became `undefined`, then `0` or `''`, then invisible.
2. **A verb rendered as a label.** The Jobs stop control was a `<span>`.
   The dock stack was inert. `cancellable_via` was computed and thrown
   away.
3. **Geometry, which jsdom cannot see.** An inert dock stack, a voice
   pill covering the dock, a toast covering the kill switch, a pane
   covering its own toggle, a hot-reload banner at y = -3365px.

---

## The seven lanes, and what each found

Worktrees are gone; this is the surviving record. Every finding below
was measured against a running brain, not inferred.

### Lane: Skills page
- **Hot-reload was never broken.** Its outcome banner rendered at
  **y = -71px** on the first card and **y = -3365px** on a lower one,
  off-screen for success and failure alike. The button worked; the
  report was thousands of pixels above the click.
- Card heights in one grid were `[1055, 1055, 1055, 547, 547, 527, ...]`.
  Now a uniform 176px.
- `GET /skills` sent `endpoints` as an **integer** while two components
  guard with `Array.isArray`. The endpoint chip was dead code in both.
- **The marketplace never disappeared.** `/marketplace` is a live route
  over `/api/marketplace/*`. Skills simply had no link to it.
- No manifest has an icon field. Icons are derived from `categories`,
  which every manifest does declare. Nothing invented.
- A real CSS trap: `-webkit-line-clamp` only clamps a `-webkit-box`, and
  a direct child of a grid container is blockified, so the clamp
  silently did nothing.

### Lane: Chat page
- **Committed turns were erased.** `normaliseUiMessages` projected every
  row to `{id, role, text}`, so at commit time each turn lost `tools`,
  `reasoning`, `timeline`, `model` and `usage`. Measured: **4 tool cards
  live during a turn, 0 in the transcript afterwards.** Every Chat test
  renders outside the Shell provider, where the fallback setter does no
  normalising, which is why 1140 green tests never saw it.
- The tool card head declared **3 grid columns for 6 children**, so tool
  names wrapped under the duration and arguments clipped to `re…`, `ap…`.
- `tool_start` / `tool_result` frames do arrive; verified against the
  orchestrator emission site.
- **Still open:** tool traces do not survive a page reload.
  `serialiseConversationMessages` persists only `role`/`content`, and a
  trace saved mid-flight would rehydrate as a card spinning forever.

### Lane: the failing routine
- Root cause of the nag you reported. A `JobType.TRIGGERED` routine was
  refused correctly, but the refusal stopped the **action** and not the
  **poll**: the row stayed `enabled = 1` at `every 1m`, was re-armed
  every minute, and each refusal wrote a non-success. The stalled-routine
  alert then reported it forever.
- The refusal is permanent by construction, so every retry was
  guaranteed to skip. Such a routine is now **disabled at first refusal**
  with the reason stored on the row and announced once.

### Lane: Settings, Memory, Devices
- **`/api/devices/connected` returned empty with three daemons
  attached.** It replaced the daemon list with the handoff registry,
  which only the messaging bridge writes. The Live pane had never been
  renderable.
- **Memory search had never returned a result.** The page sent `?q=` and
  the route declares `query`. Proof: `?q=quokka` → `[]`,
  `?query=quokka` → both notes.
- **`MemoryStore.search_all`, the four-tier hybrid, had no HTTP route at
  all.** Now `GET /api/memory/search`, verified end to end:
  `"Perth"` → 8 strong of 14 across 3 tiers; `"zzzznotathing"` → 0 of 12.
- **Push "Send test" said "Test push sent."** while the brain returned
  `sent: 0, failed: 1, degraded: ["no push credentials configured"]`.
- **Devices "Invoke" had never invoked anything**: it posted
  `{device_id, method, args}` to a route reading
  `{node_id, command, params}`, then reported `"Node not connected: "`
  with nothing after the colon, blaming the device.
- "Clear unclaimed" was permanently disabled; four swallowed failures;
  MCP refusals rendered in the **green success** chip.

### Lane: Home page
- **The Briefing tab rendered zero DOM.**
- **Tab selection was overwritten by the 15s poll**, so tabs reverted
  while you watched.
- A **stale heart rate shown as live** (`heart_rate_fresh` ignored).
- `health.cognitive_load` does not exist; the Load figure only ever came
  from its fallback.
- In-flight jobs read `description`, which no source emits.
- Four `degraded[]` arrays discarded, so a failed read rendered as calm
  emptiness.

### Lane: end-to-end real-brain suite
- **The error toast covered the supervisor kill switch.** On
  `/oversight`, the stack pinned at `top:72 right:20` covered
  **"Pause actions" at (1045,71)** and Refresh for its six-second life.
  The message telling you something went wrong sat on the button that
  stops it.
- `.v2-chat-pane` covered its own Save toggle, from a stale variable:
  `top: calc(var(--v2-menubar-height) + 24px)` resolved to 24px once the
  menubar was retired.
- **Fixed the e2e flakiness properly, without retries.** Both flakes read
  a painted value in a round trip separate from the state change, while a
  CSS animation ran. Reproduced the marketplace one to three decimals
  (284.595 mid-animation vs the 283.595 the CI failure reported), and
  showed the system-bar one reads byte-identical green in the flip frame.
  Seven consecutive clean full runs.
- **`shell_navigation.spec.ts` walked 23 of 28 destinations**, missing
  `/console`, the default landing view. Three tests saying "every
  destination" had never visited the first screen a user sees.
- `frame-ancestors` was delivered in a `<meta>` CSP, where browsers
  discard it, and a test asserted its presence: it pinned the defect.
- `/favicon.ico` was declared by `index.html` and allowlisted by the
  brain, and existed in neither.

### My own work (not a lane)
Dock, work rail, system bar, Approvals, Jobs, Console, palette, voice
duplication, and the two Brain-popover metrics. Plus the release
mechanics for both 8.13 and 8.14.

---

## Defects I introduced and then caught

Recorded because the pattern matters more than the fixes.

1. **A 500 on `/api/dashboard`**, the endpoint the whole shell polls.
   `getattr(state, "started_at", default)` looks defensive and is not: a
   MagicMock has every attribute, so the default never applied. I had
   verified the field against a live brain and not re-run the Python
   suite.
2. **`callable(runtime_status)`** was False because it is a `@property`,
   so the field returned `{}` on every request: present, permanently
   empty, no error, no log, no failing test. Found by making the
   bail-out print the type it saw.
3. **The dock did not fit a phone** once Home made it ten tiles: two
   tiles pushed outside the container at 375px, one being Settings.
4. **Content negotiation without `Vary`**, which shipped in 8.13 and
   broke the Skills page for every user of that release.
5. **The first fix for the kill-switch toast made it worse**: making the
   stack pointer-transparent left the dismiss X itself sitting on
   Refresh.

---

## Instruments that lied, twice

Both times the measurement was wrong, not the app, and both times it
would have sent someone chasing bugs that do not exist:

- `elementFromPoint` counting **"scrolled below the fold"** as
  **"covered"**. First occurrence: 4 false positives. Second: **58**
  across four routes. The fix is to scroll each control into view before
  hit-testing. Anything measuring reachability must do this.
- Reading a computed value mid-animation. See the flakiness note above.

---

## Still open, deliberately

- **`AppsPublish` tabs** convey selection by CSS class alone: no
  `role="tab"`, `aria-selected`, `aria-pressed` or `aria-current`.
- **`Shell.jsx` fires `POST /api/conversations/save`** 450ms after mount
  on **every** route, including `/settings` and `/grants`. It is the only
  write any destination makes at rest.
- **Chat tool traces do not survive a reload** (see the Chat lane).
- **A supervisor race**, ~0.9% over 111 runs: `_monitor` cancels the
  consumer tasks and *then* gathers them, under a comment claiming they
  drain the queue. Cancelled tasks drain nothing, so a caller can see
  `status: completed` with silently lost output.
- **`/approvals` is polled by three components.**
- **594 pre-existing em dashes** in `feral-client-v2/src`. Nothing in CI
  enforces the rule; 0 were added this session.
- **`remove_key`/`store_key` case normalisation** needs a migration.

---

## Traps that will cost you time

- **Agent worktrees are cut from `origin/main`, not the branch tip.**
  Every lane this session started 57-60 commits stale and recovered only
  because the brief made base verification its first required action.
  Keep that in every brief.
- **The version-coherence bot commits with `[skip ci]`.** A tag landing
  on that commit fires **no workflow at all**, which silently swallowed
  the first 8.13 tag: the tag existed on the remote and did nothing.
  Always tag an explicit commit you have checked.
- **`feral-client/`** at the repo root is 241MB of leftover v1 artifacts,
  untracked and NOT gitignored. Never `git add -A` in this repo.
- **`.claude/worktrees` was 4.2GB** across 42 worktrees. The six from
  this session were fully merged and removed. The rest predate it and
  carry branches like `fix/memory-blockers-2026-07`; check each for
  unmerged commits before removing.
- **`feral-core/build/`** is a complete duplicate tree. It contaminated a
  wheel build during this session. Exclude with `^build/`.
- **CI had two macOS assumptions and a missing ripgrep** that halted the
  Linux matrix at 24%, hiding everything behind them because the job runs
  with `-x`. All three fixed.

---

## How to reproduce the real-brain suite

```bash
cd feral-core
FERAL_HOME=<scratch> python -m uvicorn api.server:app --port 9461 &
curl -X POST localhost:9461/api/setup/complete \
  -H 'Content-Type: application/json' \
  -d '{"settings":{},"credentials":{},"identity":{}}'   # else it bounces to /setup

cd ../feral-client-v2
FERAL_E2E_REAL_BRAIN=1 FERAL_E2E_URL=http://127.0.0.1:9461 npx playwright test
```

Or `make e2e-real-brain`. It is opt-in, so `make e2e` and the required
CI gate are unaffected; without the flag those specs skip themselves.
