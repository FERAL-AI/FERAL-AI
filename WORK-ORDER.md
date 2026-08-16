# FERAL work order, 2026-08-16

Derived from four research passes (interaction/IA, visual/motion, platform and
skills, hardware and devices) plus two owner-reported bugs. Every item cites
`file:line` so nothing here rests on a summary.

## How to read this

- **P0** is a live security hole or a published claim that is false. Do first.
- **P1** is a documented path a third party cannot complete.
- **P2** is broken or dead shipped functionality.
- **P3** is the platform story: what makes FERAL worth building on.
- **P4** is design quality.

Each item states cost and blast radius. Where an item says "verify in a
browser", that is not optional politeness: the test suite runs in jsdom, which
has no layout engine, so scroll, overlap, focus order and visual regressions
are invisible to it. See "Verification policy" at the end.

Nothing here is started. Items marked IN PROGRESS have a worker running.

---

## P0. Security

### P0.1 A third-party skill can declare itself safe and never prompt

`security/safety_resolver.py:302-305` returns the manifest's own
`safety_tier` before consulting the danger map or the heuristic. The comment
says it: "Manifest wins outright." The only gate above it is a deny list of
hardcoded first-party tool names (`security/dangerous_tools.py:92-274`) that a
third-party skill id cannot match, and `get_danger_level` returns `SAFE` for
anything unlisted (`dangerous_tools.py:396`, and line 51 states the default).

So an installed skill declaring `"safety_tier": "safe"` executes with no
confirmation, on every surface, indefinitely.

The clamp that fixes this already exists and is already applied to a strictly
less important field: `skills/result_budget.py:278-284` ignores a declared
`result_budget` unless `skill_id in builtin_skill_ids()`. That governs how many
characters of a result reach the model. It is not applied to whether the user
is asked before the code runs.

**Do:** in `_safety_from_manifest`, ignore `safety_tier: "safe"` and
`read_only_hint: true` when the skill is not built-in. Allow a third party to
escalate (`confirm`, `deny`, `requires_user_approval`); never to de-escalate.

**Cost:** ~15 lines. `builtin_skill_ids()` and `_find_endpoint` already exist.
**Breaks:** any installed third-party skill relying on `safe` starts prompting.
`~/.feral/skills/` is empty and the marketplace has nothing published, so the
blast radius is zero today and will never be smaller.
**Verify:** unit test asserting a non-builtin manifest declaring `safe` still
resolves to a confirming level, and that a builtin one does not regress.

### P0.2 Install asks nothing; uninstall asks

`feral-client-v2/src/pages/Marketplace.jsx:135-137` posts straight to
`/api/marketplace/install`. No dialog, no permission list, no warning. The card
shows name, version, description, publisher, install count
(`Marketplace.jsx:123-133`) and nothing about capability.

`Marketplace.jsx:168` calls `window.confirm` to uninstall.

The API could not supply permissions if the UI asked:
`api/routes/marketplace_browser.py:79,115` never touch the field, and
`skills/package.py:72-84` returns eight keys, none of them `permissions`.

**Do:** two-step install. `POST /api/marketplace/preview` returns manifest,
permissions and signature status; the client renders them; install requires a
token from the preview.

**Cost:** ~150 lines across two files.
**Breaks:** any caller posting directly to `/api/marketplace/install`, which is
this page and its tests.
**Verify:** in a browser. This is a consent dialog; its whole value is that a
person reads it.

### P0.3 The UI takes the unsigned install path under a "signed registry" header

A real signing chain exists: `cli/publish.py:1-25` (Ed25519, SHA-256, detached
signature), `feral-registry/feral_registry/signing.py:16-29` verifies,
`cli/install.py:128-155` re-verifies and exits non-zero on failure.

`marketplace_browser.py:97-99` probes for `install_from_registry`, which exists
on `AppRegistry` (`agents/app_registry.py:373`) but not on `MarketplaceClient`.
So the probe fails and line 103 falls through to the unverified path. The page
header reads "Signed community registry at registry.feral.sh"
(`Marketplace.jsx:29`).

Also: `_install_from_archive` (`skills/marketplace.py:192`) copies to disk at
line 219 and validates at line 221, ignoring the result (line 225). Validation
runs after the files land.

**Do:** implement `install_from_registry` on `MarketplaceClient` reusing
`cli/install.py`'s `_verify` and `_safe_extract`. Delete the silent fallthrough
rather than leaving it as a downgrade. Move validation before extraction.

**Cost:** ~80 lines, mostly relocating verified code.
**Breaks:** unsigned installs from the UI, which is the intent. Keep
`source_url` as a CLI-only path with a loud warning; local iteration needs it.
**Verify:** a test that a tampered tarball is refused, and one that the UI path
and the CLI path reach the same verifier.

### P0.4 The sandbox policy has one enforcement call site

`state.policy` is consulted exactly once in production: `can_read_sensor` at
`api/routes/security_and_hardware.py:134`. Every other reference is `to_dict`,
an assignment, or a dashboard label (`api/routes/dashboard.py:206`).

`can_use_actuator`, `can_capture_camera`, `can_use_mcp_server`,
`can_generate_skills`, `is_skill_blocked`, `max_movement_speed`,
`can_access_domain`, `can_use_tier` and `needs_confirmation` have zero
production callers. They exist, they are tested, and nothing asks them.

Separately, four of them fail open on a partial policy
(`security/sandbox_policy.py:450-501`): an empty allowlist means "allow
everything", where `can_access_domain` in the same class means "allow nothing"
(line 435-444). `max_movement_speed` defaults to 50%, not 0.

`can_access_domain` having no caller also means the generic HTTP runner
(`skills/executor.py:609-619`) calls any URL a manifest names, with no domain
check, while `network.allowed_domains` is the first thing an operator would
configure.

**Do:** wire the three that matter (`can_use_mcp_server` in
`mcp/client.py:845`, `can_capture_camera` on the capture path,
`can_use_actuator` beside the existing sensor check), flip the four fail-open
defaults to match `can_access_domain`, and delete any method still unwired
after that. A security control that exists only in tests reads as coverage and
is worse than none.

**Cost:** small per call site; the methods are written.
**Breaks:** the shipped default policy lists the sensors and actuators FERAL
uses (`sandbox_policy.py:86-117`), so a default install is unaffected. A
partial operator-written policy that currently permits everything starts
denying. `feral doctor` should say so.
**Verify:** a test per wired call site, and one asserting an empty allowlist
denies rather than permits.

---

## P1. Published claims that are false

### P1.1 HUP_SPEC section 6 is entirely unimplemented

`api/server.py:2329-2330` sets `granted_capabilities` to exactly what the node
requested and `denied_capabilities` to a literal empty list. Repo-wide,
`granted_capabilities` appears twice outside `build/`: the schema
(`models/protocol.py:1031`) and that line.

`feral-nodes/HUP_SPEC.md:855-875` specifies per-device capability tiers, a UI
at "Settings to Devices to <device> to Capabilities", and four MUSTs including
"MUST drop `camera_frame` and `microphone_chunk` events from nodes whose
camera/audio tier is disabled". None of it exists: no tier state, no UI, no
enforcement.

This is Apache-2.0, public, written as requirements, on the camera and
microphone consent axis. The rest of this repo is unusually honest about its
limits; `brain_can_initiate: false` (`api/device_view.py:250`) is the proof.

**Do:** decide, then act. Either implement (persist per-device grants, compute
`granted`/`denied` from that store, drop ungranted event types at
`server.py:3440`, ship the toggle UI) or amend section 6 to describe what is
enforced. Shipping a spec whose security section is fiction is the worse of the
two.

**Cost:** implementing is a week including UI. Amending is an hour.
**Breaks:** implementing means every currently-paired device needs a default
grant on upgrade, or a one-time migration prompt.
**Verify:** a test that an ungranted `camera_frame` is dropped, plus the
spec-to-code assertion in P1.4.

### P1.2 The documented pairing flow cannot be completed

`HUP_SPEC.md:104-118` documents a 6-digit pair code the operator "types into
the dashboard 'Type a pair code' field". No such field exists. Grepping every
client and the desktop shell for `/api/devices/pair/code/claim`,
`/pair/announce` and `/pair/status` returns only `feral-core/tests/`.

The spec also names the wrong endpoint: it says `POST /api/devices/pair` takes
`{"code","name","node_id"}`, but the real route (`api/routes/devices.py:780-822`)
has no `code` parameter and does not resolve pending codes.

The code format disagrees three ways: spec says 6-digit numeric
(`HUP_SPEC.md:106-108`), the Python SDK generates 6 digits
(`python-node-sdk/src/feral_node_sdk/pairing.py:94-96`), `SECURITY.md:139` and
`devices.py:938` claim 8-char base32 (~38 bits), and the store accepts
`[A-Za-z0-9_-]{4,64}` (`security/device_pairing.py:1439`). SECURITY.md's
brute-force math is computed on entropy the shipped SDK does not produce: 6
digits is ~20 bits.

**Do:** ship the pair-code input (~40 lines of JSX calling the existing
endpoint), fix the spec's endpoint reference, and reconcile the code format in
one place with the generator named in the spec.

**Cost:** small. **Breaks:** codes in flight during the change, a 600-second
window. Keep the loose regex one minor version for pinned SDKs.
**Verify:** a test claiming a code end to end, and P1.4's spec assertion.

### P1.3 mDNS discovery cannot find a broadcasting brain

`HUP_SPEC.md:177` and both SDKs
(`python-node-sdk/src/feral_node_sdk/discovery.py:16`,
`ts-node-sdk/src/discovery.ts:31`) look for `_feral-brain._tcp.local.`. The
brain advertises `_feral._tcp.local.` (`feral-core/services/mdns.py:181-182`).
`discover_brain()` returns `None` on a network where the brain is broadcasting.
The TXT record also lacks the spec's `node_path` and sends
`version=<FERAL_VERSION>` rather than `version=1`.

**Do:** advertise `_feral-brain._tcp.local.` alongside the existing type, with
`node_path=/v1/node`, `tls` and `version=1` in TXT.

**Cost:** a second `ServiceInfo` in `services/mdns.py:162`. Additive.
**Breaks:** nothing.
**Verify:** a test asserting the advertised type matches the constant both SDKs
use.

### P1.4 Spec-to-code assertions

The repo already pins `hup_version` across five surfaces. Extend the pattern:
assert the mDNS service type matches section 4.3, that `/api/devices/pair`
accepts the body section 4.1 documents, and that every `event_type` in the
section 5.4 table has a branch in `server.py`.

**Cost:** small. **Breaks:** the tests fail immediately, which means fixing
P1.1 through P1.3 before they go green. That is the point: it converts "prose
drifts silently" into "prose drift fails CI".

### P1.5 The published SDK produces plugins the runtime cannot load

`sdk/python/feral_sdk/plugin.py:15` defines `FeralPlugin` as a plain object.
`skills/registry.py:119` requires `issubclass(obj, BaseSkill)`. A plugin built
with the published SDK is scanned, matches nothing, and the loader returns at
line 125 with no log line. The developer gets a skill that installs, registers,
appears in the tool list, and errors on every call.

Also in that file: `to_manifest()` emits `"brand": {"icon": ...}` but
`BrandProfile` has `icon_set` (`models/skill_manifest.py:21`), so Pydantic drops
it silently. And `feral_sdk.SkillManifest` is a different class with a different
shape from `models.skill_manifest.SkillManifest`, under the same name.

**Do:** make `FeralPlugin` subclass `BaseSkill`, or make the loader accept the
documented duck type. Add a log line to the silent fall-through either way.

**Cost:** small. **Breaks:** nothing that currently works, because nothing does.
**Verify:** a test that loads a plugin built with the published SDK.

---

## P2. Broken or dead shipped functionality

### P2.1 Chat scrolls out of view (IN PROGRESS, owner-reported)

Not caused by recent work: no layout property in any chat or shell selector
changed since v2026.8.9; the only chat rule touched was a focus outline. The
fix is in the layout, not in a revert.

**Verify:** in a browser, at more than one window height. jsdom cannot see this.

### P2.2 Skills hot-reload silently does nothing (IN PROGRESS, owner-reported)

Two independent faults.

Reporting: `api/routes/skills.py:55-68` returns `{"ok": false, "skill_id": ...}`
with HTTP 200 and no `error` key, so `apiFetch` does not throw and
`pages/Skills.jsx:50-62` calls `setReloaded(id)` on anything that does not
throw. A reload that did nothing renders as "Hot-reloaded". This is the defect
class two releases removed, surviving inside the change meant to close it.

Cause: `skills/registry.py:280` resolves first-party manifests as
`manifests/{skill_id}.json`, but eight of 39 shipped manifests have a filename
that differs from their `skill_id` (`notes.json` holds `notes_memory`, plus
`calendar`, `github`, `messaging`, `robot_action`, `smart_home`, `spotify`,
`task`). All eight fall through to `logger.warning("no manifest found")` at
line 302 and return False. `cli/install.py:202` calls this endpoint after every
install.

**Do:** fix the lookup to resolve by `skill_id` field rather than filename, or
rename the eight files. Then make the route return a non-2xx on failure.
**Verify:** a Python test reloading each of the eight, and a client test that
`{"ok": false}` does not render as success.

### P2.3 Two of the desktop app's three native controls are dead

`desktop/src/main.js:144` loads the client in an `<iframe>`, so there is no
Tauri IPC. The global voice shortcut (`src-tauri/src/main.rs:544`) and the
tray's Quick Chat (`main.rs:627`) both emit `voice-activation`, which
`main.js:351-352` re-dispatches as `feral-voice-activation` on the outer
document. Nothing listens for it anywhere in the repo, and nothing could: the
client is in a different document.

**Do:** interim, a `postMessage` bridge plus a listener in the client (~40
lines) revives both. Properly, drop the iframe and bundle the client as the
Tauri frontend, which also unlocks `invoke()`, drag regions and window
vibrancy.

**Cost:** interim is an afternoon. The bundle change is the largest item in this
document.
**Verify:** press the shortcut. There is no other way.

### P2.4 iOS reconnect hot-loops the radio

`feral-nodes/ios-node-sdk/Sources/FeralNodeSDK/HUPWebSocket.swift:226-245`
escalates `delayMs` only in a `catch`, but `openSocket()` (line 111-120) is
declared `throws` and contains no throwing statement, so the catch is
unreachable. `delayMs` is also reset to `initialMs` on every entry. An
off-network phone loops: sleep 0-100ms, "connect", fire `onReconnect`, return,
`receive()` throws, repeat. The spec's 30-second cap
(`HUP_SPEC.md:64`) is never approached.

**Do:** await a real handshake signal before returning success, and hoist
`delayMs` to instance state.
**Cost:** small refactor; `Tests/FeralNodeSDKTests/HUPWebSocketReconnectTests.swift`
exists. **Breaks:** reconnect after a genuine blip goes from ~50ms to the first
backoff step, which is correct.

### P2.5 The browser node never reconnects

`feral-client-v2/src/node/BrowserNode.js:283-292` sets phase `closed` and emits
a recoverable error. Nothing reopens the socket. This is the path
`PairDeviceModal.jsx:39` presents as the default. Recovery happens only if the
user notices and reloads, which re-runs `Pair.jsx:164-188` from the stored
bearer.

**Do:** port the supervisor from
`python-node-sdk/src/feral_node_sdk/node.py:351-393` (jittered exponential
backoff, 30s cap, re-handshake).
**Cost:** ~60 lines. **Breaks:** `Pair.jsx`'s treatment of `closed` as terminal.

### P2.6 Glasses frames are never remembered

`api/server.py:3990-4010` does exactly one thing with a `glasses_frame`:
`buf.ingest(...)`. No perception update, no change detection, no scene
analysis, no memory write. `_handle_video_frame` (`server.py:3748-3804`) does
all four. So the channel `HUP_SPEC.md:462-466` calls "the canonical channel for
vision context" gets strictly less processing than the generic camera channel.

`scene_description` reaches memory nowhere in the codebase.
`HUP_SPEC.md:452-454` states captions are stored in episodic memory. Frames
live 30 deep in RAM (`perception/glasses_buffer.py`), captions live in a
per-session struct, and a restart erases both.

Note for accuracy: the senses work earlier this week made glasses frames
*reachable for a turn*. It did not make them remembered. Those are different
claims.

**Do:** route `glasses_frame` through the change detector and scene analysis,
gated hard by the detector and a per-hour cap (the buffer's own docstring warns
against a per-frame vision-LLM loop). Write captions, text only, never frame
bytes, to episodic memory with a retention policy the user can see and clear.

**Cost:** real money per caption. The cost surface must show it before this
ships. **Breaks:** turns a free ingest path into a metered one.

### P2.7 Two false statements in shipped UI

`api/device_view.py:258-262` tells every user "tokens last 24h by default".
HUP daemons get 24h sliding (`security/device_pairing.py:80`); browser phones,
the majority path, get 30 days (line 84).

`components/PairDeviceModal.jsx:264-266` and `node/BrowserNode.js:21` both
promise "tab-hidden more than 60s auto-pauses". The handler
(`BrowserNode.js:294-300`) records `_pausedAt` when hidden and pauses only on
return to visible after 60s. Nothing pauses while hidden. A privacy claim that
is not enforced is worse than no claim.

**Do:** read the real TTL per credential kind; either enforce the pause on a
timer armed at hide, or describe what the code does.
**Cost:** small. **Verify:** a test per claim.

---

## P3. The platform story

### P3.1 Give `BaseSkill` a scoped context

`skills/base.py:23` passes `endpoint_id`, `args`, `vault`. No memory handle. The
twelve first-party impls that need one do `from api.state import state`
(`skills/impl/notes_memory.py:44` and others), which
`skills/EXTENSION_RULES.md:50-55` explicitly forbids. Core also branches on
concrete skill ids in at least five places
(`agents/direct_execution.py:83`, `agents/tool_dispatch_validator.py:313,351`,
`agents/tool_runner.py:1198`, `api/routes/ambient.py:209`) plus six hardcoded
first-party id lists, which rule 1 of the same document forbids.

There is also no scoping if a third party does reach memory: `skill_id` appears
in `memory/store.py` only in `execution_log`. Notes, episodes and the knowledge
graph have no owner column, no namespace, no quota. One skill's `impl.py` can
read every note and the whole graph, and neither the user nor the audit log
would show it.

**Do:** a `ctx` exposing `ctx.memory.save/search` tagged with the calling
`skill_id`, `ctx.call(skill_id, endpoint_id, args)` routed through
`SkillExecutor.execute` so it inherits the gate and the audit row, and
`ctx.emit_ui(tree)`. Add `owner_skill_id` to notes and episodes so reads can be
scoped and a user can see and revoke what a skill stored.

**Cost:** a week. Keyword-only with a default so 29 impls keep working; the
column is an additive migration.
**Breaks:** nothing immediately. The real work is migrating the twelve
first-party impls off `api.state` in the same change, so `EXTENSION_RULES.md`
becomes true rather than aspirational.

### P3.2 One SDUI schema

Three vocabularies disagree and none is canonical: `genui/a2ui_protocol.py:21-48`
(28 types), `genui/generator.py:26-31` (26), and `ui/SduiRenderer.jsx` (33,
and the only one that decides what a user sees). Eleven types render as a
dashed placeholder (`SduiRenderer.jsx:380-392`), including `MapView`, `Chart`
and `Table`, which the brain actively generates
(`agents/genui_generator.py:108`, `genui/generator.py:304,318`) and instructs
the LLM to build (`genui/generator.py:52-55`).

`Markdown` renders as preformatted text with a comment claiming no parser is
bundled; `lib/markdown.jsx` is 202 lines and Chat imports it for every message.
`Icon` discards the name and draws a circle, while lucide is bundled.

`ui_hint` is `Optional[str]` with the honored set in a comment. Across 39
manifests there are 190 declarations in 12 values; ~41 are outside the honored
set and silently fall through, including 7 uses of `"card"` (near-miss for
`detail_card`) and every auto-generated hardware skill
(`hardware/capability_skill.py:156`).

Also likely: `SduiRenderer.jsx:91` calls `useCallback` after four early returns
(lines 77, 78, 79, 88), so hook count varies by node shape. `applySduiPatches`
(line 825) can replace an object node with a string, which would throw
"Rendered more hooks than during the previous render". Read, not reproduced.

**Do:** put the vocabulary in `models/protocol.py` beside `HUP_VERSION`,
generate the other two from it, add a drift test in the shape of the existing
`HUP_VERSION` guard, retype `ui_hint` as a `Literal`, and implement `Table`,
`CodeBlock` and `Chart` (no dependency needed) or remove them from both brain
vocabularies.

**Cost:** schema plus drift test, a couple of days. Table and CodeBlock an
afternoon each; Chart as inline SVG a day.
**Breaks:** the `ui_hint` retype fails ~41 first-party declarations at load.
Fix them in the same change.

### P3.3 SDUI actions have no receipt

`SduiRenderer.jsx:294-306` calls the handler and returns. No pending state, no
disable, no confirmation. `sendUiEvent` is fire-and-forget
(`hooks/useFeralSocket.js:57`), and `lib/ws.js:107-115` returns `false` when the
socket is not open, which nobody reads here. A click during a reconnect goes
nowhere silently.

**Do:** `idle -> pending -> settled | failed`, using the `sendOrFail` result
that already exists and is already used by Chat
(`pages/Chat.jsx:797-799`). Without a brain-side ack you can still honestly
render "sent, waiting" versus "could not send".
**Cost:** small for the honest version; a wire addition for a true ack.
**Breaks:** nothing if the client tolerates an older brain that never acks.

### P3.4 MCP hygiene, then sampling

Four small fixes: stdio sends `"initialized"` where the spec requires
`"notifications/initialized"` (`mcp/client.py:257`; the HTTP path gets it right
at line 377); `prompts/list` advertises two prompts and `prompts/get` does not
exist (`mcp/server.py:471-485`); `listChanged: True` is declared
(`server.py:74-75`) and never emitted, so a connected client never learns about
an install or a hot-plugged device; three protocol versions coexist
(`server.py:79` pins 2024-11-05, `client.py:53` uses 2025-06-18).

Then the strategic one: the client declares `"capabilities": {}`
(`client.py:252,353`). Declaring and implementing `sampling` would let any MCP
server ask FERAL to run a completion locally, without its own API key, against
the 16-provider router already wired. That is a differentiated position for a
local-first host and it costs one handler.

**Cost:** fixes an afternoon; sampling a day or two. **Breaks:** nothing.

### P3.5 Manifest fields that do nothing

Traced to their consumers: `permissions` (zero readers), `max_calls_per_hour`
(zero), `triggers` (zero, deliberately), `auth.type: "oauth2"` (zero;
`executor.py:593-601` handles `api_key` and `bearer` only, while seven manifests
declare oauth2 and a working broker sits unconnected at
`integrations/oauth_manager.py`), `flows` (one indirect consumer;
`FlowStep.condition` and `then_endpoint_id` never read).

**Do:** for each, wire it or delete it. `permissions` is wired by P0.2. OAuth is
the one worth building: Home Assistant's config-flow pattern maps onto SDUI,
which FERAL already has, terminating in a vault write.

---

## P4. Design

Ordered by leverage, not by size.

### P4.1 Build the type ramp the token file already promised

`styles/tokens.css:90-92` states it deliberately narrowed the grey separation
because "hierarchy here is carried by size, weight and tracking". That system
was never built. Measured: `--v2-text-secondary` and `--v2-text-tertiary` are
**1.14:1 against each other**, one colour to the eye. 54 of 74 `font-weight`
declarations are `600`. The 7-step ramp is used as 3 steps, and
`--v2-size-lg` appears once in the whole client (`index.css:63`, the pre-boot
error card). 105 of 239 font-size declarations bypass the ramp, at 16 distinct
sizes including half-pixel values.

`.v2-chat-body` is 15px; assistant turns render through `.v2-md` at 14px
(`markdown.css:9`). User and assistant text differ by 1px for no chosen reason.

**Do:** collapse to 7 sizes, three real weights, give `lg` a job (pane titles),
derive tracking and leading from size, remove the 14/15 contradiction.
**Cost:** ~200 declarations, the largest diff in P4 and the highest visible
change per line.

### P4.2 Give depth a recess direction, and fix light mode with it

Composited, the four surface levels are 1.02:1 to 1.16:1 apart in dark and
about 1.01:1 apart in light. The user chat bubble is **1.05:1** against the
page in dark, 1.08:1 in light; its shape is carried entirely by a 1px hairline.

Light mode's deeper problem: every depth cue in the system is a *lighten*
operation. `--v2-glass-tint` is a white top-down fade that computes to
**exactly 1.000:1** over a light surface. You cannot lighten white.

Five of seven tokens fail AA against the raw `--v2-bg-deep` `#DEE0E6`, and two
routes paint it flat with tertiary text on top
(`pages.css:857,864,988,999,1007`). Those are live AA failures, not a
theoretical ceiling.

**Do:** add a recess direction (well / flat / raised) using the existing
`--v2-well`; flip the light glass tint to darken; strengthen the light hairline;
restore the light inset highlight; stop rendering text on raw `--v2-bg-deep`.
**Cost:** mostly token values plus an audit of ~40 surface call sites.

### P4.3 The decision queue

Seven surfaces emit "the brain needs a yes or no": permission requests,
refusals, budget caps, Forge drafts, ideas, paused thoughts, proactive alerts.
Each got a bespoke component. `components/ProactiveToast.jsx:6-7,88-91` keeps
one at a time, newer replaces older, and nothing persists it, so an alert that
fires while you are on another page is destroyed. `hooks/useBrainEvents.js:22`
keeps its buffer in component state, so every event history dies on unmount.

**Do:** one store, one count, a badge on the Dock, a rail on Home, and a
permanent settled receipt per decision using the pattern Chat already
implements for permissions (`pages/Chat.jsx:929-937`). Chat keeps rendering
decisions inline; the queue is a second view, not a relocation.
**Cost:** ~150 lines plus one emitter change per surface.

### P4.4 Make the palette an action palette

`⌘K` opens `HubLauncher`, whose 15 items exclude seven of the eight Dock
primaries. Verified: it cannot reach Chat, Devices, Home, Flows, Apps, Canvas
or Settings. `shell/Menubar.jsx:10-11` still carries a comment promising a
command palette "later". `⌘.` expands an ambient strip whose content is gated
to dev builds (`shell/Ambient.jsx:24,82`).

**Do:** Do / Go / Ask sections. The Ask row hands the query to the composer,
which is what makes it FERAL's palette rather than a copy of Linear's. All 24
routes plus the 16 Settings sections as deep links, which already work via
`?section=` (`pages/Settings.jsx:31-44`).

### P4.5 Orb discipline

One recipe is stretched across a 15x size range. `.v2-orb-halo` is
`inset: -10%` with `blur(20px)` (`ui.css:377-389`): at 320px that is a soft
glow, at 22px the blur radius is 91% of the orb and it is a smudge. Every
assistant row renders `mode="idle"` (`pages/Chat.jsx:984`), which is an infinite
4.2s animation, so a 60-turn thread runs 30 concurrent animated layers meaning
nothing.

The canvas orb (`pages/phone/VoiceOrb.jsx`) has drifted: its idle colour is
`#6E6E76`, the pre-fix `--v2-text-tertiary`, and its comment naming the token is
now wrong. It also schedules `requestAnimationFrame` unconditionally
(line 113-121) with no reduced-motion check, unlike
`ConsciousnessMindMap.jsx:117-123` which does it correctly.

**Do:** size-relative halo and ring; delete the per-message avatar and show one
orb at the live turn; sync the canvas colour; add the missing reduced-motion
gate; move `speaking` off `--v2-state-live`, which means "healthy" everywhere
else.
**Cost:** about an hour for the last four.

### P4.6 Freshness and the pending tier

`hooks/useResource.js:129-134` returns `{data, error, loading, refresh}` with no
fetch timestamp, so the twelve converted pages cannot say whether a number is
four seconds or forty minutes old. Home reaches around the hook to
`useSystemHealth.lastFetched` to get it (`pages/Home.jsx:272`). Only two
surfaces render an "as of" stamp.

`lib/api.js` has no `AbortController` and no timeout anywhere; the one exception
is a hand-rolled 30s abort on Home whose comment explains exactly why
(`pages/Home.jsx:337-341`).

And the kill switch still gives no sign it is working while it works
(`pages/Oversight.jsx:147-167`): no disable, no label change, so a second click
sends the opposite command.

**Do:** add `lastFetched`, `stale` and a default timeout to `useResource`; one
`<Freshness>` in `Pane`'s header. Add a `useAction` encoding: tier 1, report
what the client did (always safe); tier 2, settle to the server's echoed value;
tier 3, never predict the outcome locally, because the brain is the authority
on whether a tool ran and the client cannot compute it.
**Cost:** ~140 lines, no contract change, upgrades twelve pages at once.

### P4.7 Two remaining false empty states

`pages/Geofences.jsx:16-21` is try/finally with no catch and renders
`EmptyState "No geofences"` on failure. `components/ResumeCockpit.jsx:86-102`
wraps `Promise.allSettled` in try/catch, so the catch is dead code and
`setError(null)` runs unconditionally; a failed
`/api/consciousness/state` renders "Clean slate. No in-flight intents, flows,
or paused thoughts." That is on Home, on the pane whose purpose is telling you
what you have not finished.

`pages/Home.jsx:196` has the same shape and renders "No skills loaded yet.
Check the Brain boot log", the exact claim `pages/Skills.jsx` was fixed to stop
making.

**Do:** convert to `useResource` and `ErrorState`. These were missed because
they were outside every worker's ownership list.

---

## Documentation to update, in the same change as the code

- `feral-nodes/HUP_SPEC.md`: section 4.1 endpoint and body, section 4.3 service
  type, section 5.4.2 episodic memory claim, section 6 (implement or withdraw).
- `SECURITY.md:139`: pair-code entropy math, once the format is reconciled.
- `docs/ADDING_SKILLS.md`: currently documents only the fork-the-monorepo path,
  omits the mandatory `AUTOLOAD_MODULES` step without which a skill silently
  never loads, and misdescribes `requires_daemon` (line 41) as controlling
  WebSocket routing when `executor.py:576` routes on `method == "WS_EXECUTE"`.
- `skills/EXTENSION_RULES.md`: either enforce rules 1 and 2 or mark the twelve
  known violations as grandfathered with a migration plan.
- `sdk/python` README and `PLUGIN_SDK.md`: memory is mentioned zero times.
- `CHANGELOG.md`: entries for 2026.6.18 and 2026.6.19 still contain six
  "fill me in" stubs. Left as-is rather than invented; fill or delete.
- `desktop/README.md:94`: points at `src/floating-window.html`; the file is at
  `desktop/floating-window.html`.

---

## Verification policy

This changed because the test suite gave false confidence on the owner's chat
bug.

**Run the full Python suite** (about 7 minutes) before a commit that touches
`feral-core`, and before any release. Not after every edit.

**Run targeted tests** continuously while working.

**Verify in a real browser** for anything involving layout, scroll, overlap,
focus order, colour, motion or window chrome. Vitest runs in jsdom, which has
no layout engine: it cannot see a scroll container, an element behind the dock,
a focus ring, or a contrast failure. A green suite is not evidence that a
visual change is correct, and presenting it as such is the same defect class
this codebase has spent two releases removing.

**Prove a test fails against the old code** before trusting it. A test written
after a fix usually describes the fix and passes either way.

**State what was not verified.** The camera-light fix is verified at the track
level and has never been observed on real hardware; that belongs in the report,
not in a footnote.
