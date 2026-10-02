# FERAL app completion: executable plan

Reconciled October 2, 2026 by three parallel source/research audits and the parent integration audit. This supersedes the dependency order in older planning prose where it would require rebuilding existing sessions, memory or workflows before packaging the supported local system. It does not supersede their safety or acceptance contracts.

## Starting point and scope

FERAL already has an orchestration runtime, tools, approvals, persisted conversations, session working memory, long-lived memory/wiki/episodes, CRDT peer sync, workflow storage, voice engines, device transport, channel adapters, managed coding agents, plugin/app/device SDKs and a complete web client. The SwiftUI/AppKit app exposes **21 destinations**, not an empty replacement shell. Its searchable command palette, menu-bar access, login-registration adapter, bounded app confirmations and surface updates already have implementations.

The engineering job is to preserve and expose the existing system, complete genuine omissions, verify the assembled app, and extend these interfaces for the requested new experiences. Do not create another agent, memory database, coding engine or independent payment authority.

The supported baseline is a single-user local installation. Account-isolated hosted services, cross-user social discovery and commerce are additional requirements, not prerequisites for every existing local function. All requested features remain on this plan; milestones describe dependency order, not deletion of scope or permission to label unfinished features complete.

The existing installed app and personal data stay intact while the native candidate is isolated. Migration must be explicit and reversible. Source implementations, fixture results, actual app/account/device outcomes and distribution acceptance are separate columns in the evidence ledger.

## Current verification checkpoint

Source: `7397eb627038c196113f5f82ef54d9514c7ab063`, published in [draft PR #310](https://github.com/FERAL-AI/FERAL-AI/pull/310).

| Evidence | Observed result | Boundary |
|---|---|---|
| [Backend PR fast lane, run 36977000540](https://github.com/FERAL-AI/FERAL-AI/actions/runs/36977000540) | **12,127 passed, 83 skipped**, coverage gate passed | Ubuntu/Python 3.11 PR lane, excludes performance directory; warnings include asynchronous cleanup and environment/network-test hygiene. Not physical/account/native acceptance. |
| [Native checks, run 36977000549](https://github.com/FERAL-AI/FERAL-AI/actions/runs/36977000549) | **Success** | Production typecheck, feature/linked-model checks, process-lifeline and bundle-auditor fixtures. Does not assemble, install or exercise the GUI. |
| Web Playwright, SDK/extension, Ruff, architecture, syntax and bundled assets in CI | **Success** | Per-job boundaries apply; Playwright job uses stubbed API specs. |
| Web coverage job in that run | **1 failed, 1,357 passed** | Stale-heart-rate test observed the somatic value before the dashboard metadata mirror. Assertions now wait for metadata; full local coverage command exits 0 after that test-only correction. Remote corrected-source rerun required. |
| Backend mypy | **Failure, non-blocking** | Existing type baseline remains technical debt; do not describe all checks as green. |
| Actual packaged Mac 2026.9.26 | Avatar onboarding, normal quit and relaunch passed after two startup traps were fixed | Latest complete populated-profile and action matrix remains open. Earlier successful local reply/import/coding checks belong to their earlier candidates. |

Older local full-suite failures remain historical evidence. The newer remote backend success closes that specific PR fast-lane gap; it does not erase warnings, expand platform coverage or certify every new feature.

## Milestone 1: dependable, extensible app for the existing system

Start here. Several items can run independently. Do not wait for a social network or new multi-account architecture to finish a local Mac app.

| Card / ownership | Reuse and concrete change | Completion evidence | Depends on / effort |
|---|---|---|---|
| BASE-01 / parent | Correct stale parity/coverage docs; close current web CI failure; keep evidence bound to source and candidate | Required CI passes on corrected source, failures remain visible; implementation and acceptance are distinct | Immediate / small |
| NATIVE-01 / native acceptance worker | `APIModel`, Conversation/RichChat/ChatTools/Attachments: complete latest populated-profile conversation journey | New/reopen/rename/pin/search; rich old records survive save; attachment allow/deny; stream/cancel/reconnect/snapshot/branch; no duplicate command, misdelivery or content loss | Stable assembled candidate / medium |
| NATIVE-02 / security worker | Reuse Vault initialization coordinator/router and native onboarding; wire safe reviewed fresh-key setup | Disposable new encrypted profile, real signed OS key storage, deny/cancel/drift, relaunch, provider activation and real reply; original artifacts preserved | OS signing and authorized account access / large |
| NATIVE-03 / native acceptance worker | Oversight/Security/Operations/RuntimeHealth: exercise existing approvals, grants, jobs, checkpoints and lifecycle | Exact allow/deny; pause blocks dispatch; cancel owned work; revoke grant; restore safely; process exit, sleep/wake and reopen; unknown effects never replay | Stable candidate; CORE-03 where workflows involved / medium |
| NATIVE-04 / native parity worker | Finish concrete missing controls: routine run-now and automation CRUD; Forge generation/proposals/stats; per-skill account configuration; peer/sync setup; push; complete status/cost presentation | Separate endpoint-backed subcards; exact payload/readback; denial/error/empty/persistence cases; real adapters only where available | Existing routes; distinguish absent backend support / medium per subcard |
| NATIVE-05 / native parity worker | Finish bounded native GenUI component inventory and developer validate/install entry; retain existing confirmations/patch validation | Existing sample app opens, confirms/denies an action, patches/navigates and uninstalls; unsupported components remain explicit; no cross-app response leakage | DEV-01 and component inventory / medium |
| NATIVE-06 / native acceptance worker | Existing voice capture/playback/configuration; add missing global shortcut/floating controls after core audio works | Real mic/headphones: start/mute/interrupt/stop, deny permissions, drop provider, switch thread; capture and late playback stop accurately | Authorized provider/device access / medium–large |
| DATA-01 / memory worker | Explicit migration from existing installation using store/config/vault schemas, not changing defaults silently | Disposable populated old profile migrates with transcripts/wiki/memory/identity/settings/grants intact; backup and rollback recover original; crashes/WAL and version conflicts tested | Schema manifest; NATIVE-02 for encrypted profiles / large |
| DEV-01 / developer worker | Reuse Python/Node SDKs, manifests, skill registry, app publisher and HUP/device SDKs | Clean-clone tutorial builds a plugin and app, validates/installs/invokes through actual authorization and uninstalls; examples agree with real HTTP/WS authentication/schema; SDK compatibility tests in CI | Contract audit / medium |
| DEV-02 / developer worker | Publish capability/version matrix, contributor commands, architecture, examples, extension permissions and compatibility/deprecation rules | A developer follows docs without private assets or undeclared services; positive and denied tool/device flows demonstrated | DEV-01 / medium |
| DIST-01 / distribution worker | Existing Mac staging/build/audit: source/resource hashes, clean machine install, licenses/SBOM, Developer-ID/notarization, update/rollback | App runs without checkout/system Python; signature/stapling verified; data survives update and rollback; actual login registration/next-login launch; failures block publication | DATA-01; signing credentials; redistribution review / large |

Milestone 1 exits when the complete enabled existing capability matrix is usable through the supported app, data survives migration/restart/upgrade, developers can extend it through tested public interfaces, and clean installation passes. A screenshot, source-only claim or fixture suite alone does not close it.

## Milestone 2: richer conversation, sessions, memory, voice and oversight

| Card / ownership | Reuse and concrete change | Completion evidence | Depends on / effort |
|---|---|---|---|
| CORE-01 / conversation worker | Existing conversations/sessions/store/snapshots: native concurrent-session binding | Two active chats: independent replies/cancel/reviews, shared deliberately stored long-lived fact, private working transcripts; rich persistence across relaunch | NATIVE-01 / medium |
| CORE-02 / conversation worker | Extend `FeralMessage` IDs, transcript ordering and existing SQLite store with durable message/receipt events and cursor/revision semantics | Lost ACK/resend runs once; delivered/seen require respective acknowledgements; two clients catch up in stable order; stale autosave cannot erase turns; deletion survives reconnect | CORE-01; IOS-03 contract agreement / large |
| CORE-03 / runtime worker | Harden existing `agents/taskflow.py` and ToolRunner: central dispatch, session authority, persisted attempt/outcome and recovery reconciliation | Confirm-class workflow requires approval; denied/expired/paused dispatch refused; restart after possible commit does not repeat effect; safe reads resume; unknown remains unknown | Existing policy/workflow storage / large |
| CODE-01 / coding worker | Propagate verified conversation/session identity through `api/routes/coding.py`, tool context and `external_agent`; reuse ACP continuity/index/activity | Chats A/B in same repo never resume one another; real disposable edit/test with exact approval; deny/cancel/death/reattach and remembered digest work | CORE-01 / medium |
| CODE-02 / coding worker | Expose supported installed OpenCode/Codex/Claude adapters; opt-in CLI wrappers/hooks feeding existing activity episodes | Managed and opted-in standalone runs appear with repo/session/source; opt-out records nothing; secret sentinels absent; test success needs actual result evidence | CODE-01; adapter-specific access / medium–large |
| MEM-01 / memory worker | Existing store/retriever/wiki/episodes/CRDT: provenance/corrections, owner-scoped same-user device continuity, tombstones and full backup | Two-device conflict/correction/delete/restore; original/summary/vector/upload inventory; offline reconciliation; corrupt backup/model migration rollback | CORE-02, DATA-01 / large |
| VOICE-01 / voice worker | Existing realtime/chained/fallback engines plus one foreground voice/background-task coordinator | Real conversation stays responsive during background work; barge-in stops speech with explicit task-cancel semantics; right-thread results; no late audio after switching; 30-minute real run | CORE-01/03, NATIVE-06 / large |
| AUTO-01 / runtime worker | Existing schedulers/proactive scene policy; unify all effectful entry points with central dispatcher | Fresh context/quiet hours/dismissal/budget; suggested versus authorized actions clear; pause/revoke blocks every tested entry point | CORE-03 / medium–large |
| UX-01 / native UI worker | Existing avatar/name/logo/identity and navigation; consistent activity/action inbox, receipts, rich artifacts, accessible layout | First-use choice persists; task/review/result visible from owning chat; keyboard/VoiceOver/reduced motion; no raw infrastructure details in ordinary conversation | CORE-02; existing Operations/Oversight / medium |
| MODEL-01 / provider worker | Existing provider/router/catalog/login paths; capability-specific activation and evals | Supported login/expiry/disconnect; real configured inference and voice; truthful unavailable capabilities; local download/storage preflight; context/cost/fallback checks | Account/model availability; NATIVE-02 as applicable / medium |

Observed code gaps, not guesses: TaskFlow `skill.invoke` currently calls `skill.execute(...,{})` after an explicit-deny check, bypassing the full ToolRunner review/session route. Its running-flow recovery requeues work. Coding continuity accepts `conversation_id`, but current callers omit it and fall back to engine/workspace. Conversation full-document saves can overwrite concurrent turns. These warrant narrow hardening of existing modules, not replacement runtimes.

Developer source inspection also found contract drift to resolve in DEV-01: both generic SDK clients request `/api/health`, while the registered dashboard health route is `/health`; their constructors expose no credential option and chat handling assumes an older greeting/stream shape. The developer journey must be checked against the actual authenticated server, not only a generated manifest. Device SDK CI success does not certify these separate generic SDKs.

## Milestone 3: phone/glasses, iMessage, accounts and commerce

The iOS app remains the other agent's implementation responsibility. This team supplies executable shared contracts, fixtures, backend changes and acceptance evidence.

| Card / ownership | Reuse and concrete change | Completion evidence | Depends on / effort |
|---|---|---|---|
| DEVICE-01 / device-contract worker | Existing handoff/node/session paths: exact node selection, durable checkpoint/expiry/ACK and camera capture correlation | Two same-class devices cannot steal context; offline/restart/cancel/revoke; selected camera request IDs/freshness; late/wrong-source frame refused | CORE-02/03; IOS-01–04 / large |
| IOS-01 onward / user's iOS agent | Existing ConversationCoordinator, FERAL relay and transcript outbox; unify the conversation record while preserving ACK/retry semantics | Build/protocol/state tests; real signed iPhone/glasses audio/camera/Bluetooth/background matrix; no duplicate turn/effect | [iOS handoff](IOS_AGENT_HANDOFF.md), DEVICE-01, MEM-01 / separate implementation |
| MSG-01 / messaging worker | Add iMessage to existing channel abstraction after [the research report](INSTINCT_RESEARCH.md); per-owner sender/recipient binding and ingress dedupe | Authorized test identities: send/receive/attachment/reconnect/denied Automation; no loop or uncertain-send retry; receipt states accurately reflect bridge support | CORE-02/03; bridge choice/platform support / medium–large |
| WEB-01 / browser worker | Retain existing CDP/Playwright driver, add planner only where needed; dedicated authenticated browser profile and explicit Google scopes | Actual permitted search/account/booking journeys; MFA/CAPTCHA handoff; changed account/text/terms renew review; verified confirmation | CORE-03, credentials / medium–large |
| PAY-01 / commerce worker | Existing commerce policy + per-user Link CLI/device login adapter; quote-bound approval and action/attempt/receipt reconciliation | Disposable/supported test flow: exact total/merchant, approval denial/expiry, changed quote, duplicate/late result, uncertain charge and cancellation; no card secrets in transcripts/logs | CORE-03, WEB-01; verified Link eligibility / large |
| PAY-02 / commerce worker | Named merchant checkout, receipt/status/refund support and glasses→same-task phone/iMessage continuation | Physical selected-glasses request, exact checkout approval, merchant receipt and reconciliation; named supported merchants documented | PAY-01, DEVICE-01, MSG-01; authorized live tests / large |

Research does not authorize account login, external messages or spending. Follow the user's requested research-before-code boundary for the new Instinct integration. The cafe scenario in that report is a real end-to-end acceptance test; it is not a substitute for the product.

## Milestone 4: collaboration, Linux distribution and operations

| Card / ownership | Reuse and concrete change | Completion evidence | Depends on / effort |
|---|---|---|---|
| SOCIAL-01 / collaboration worker | Existing peer roster/invitations/scopes/CRDT; shared task/project membership and explicitly scoped collaboration | Two separate installations share selected records/tasks only; private health/ambient records remain private; revoke/expire/block and hostile peer tests | CORE-02/03, MEM-01; cross-owner namespace contract / large |
| SOCIAL-02 / device/social worker | Opt-in friends/proximity discovery adapter distinct from memory federation | Real supported device/background tests for discoverable state, matching, consent, blocking and expiry; no automatic private-memory exchange | SOCIAL-01, DEVICE-01 / large |
| DIST-02 / Linux worker | Preserve existing Linux runtime/web client; decide desktop toolkit against native integration requirements, then package equivalent supported power | Named distributions/CPU/X11/Wayland install/update/rollback/audio/tray/shortcuts/credentials; actual feature matrix, not a Mac build claim | Platform design spike; DIST-01 conventions / large |
| OPS-01 / operations/product worker | Existing doctor/logs/config; redacted diagnostics, deletion/export, recovery/support and cost controls | Recovery/backup drills, uninstall/unlink/delete proof, privacy/security/contribution docs, vulnerability contact, measured task success and recurring usefulness | All enabled milestone gates / medium–large |

Experience research remains an explicit OPS-01 subcard: inspect the requested ChatGPT dots interaction in the available app, identify the exact Meta/Muse reference before attributing features, compare task/navigation/voice flows, and translate findings into UX-01 acceptance. The five-part Instinct integration report is complete; that does not mean every competitor-reference investigation or product-market-fit experiment is complete.

Linux toolkit selection requires a bounded comparison of UI/accessibility/OS integration and maintenance costs; neither SwiftUI nor calling the old Tauri shell native settles the user's requirement. Keep existing Linux clients usable during that work.

## Coverage of the conversation

The [22-row request tracker](REQUEST_COVERAGE.md) remains the checklist. Map it to this backlog:

| Requested area | Cards |
|---|---|
| Full app, preservation, clean UI, install/upgrade | BASE-01, NATIVE-01–06, DATA-01, DIST-01/02, UX-01 |
| Developers build on FERAL; source/docs/GitHub systematic work | DEV-01/02; publication rules below |
| Avatar/logo/personality | UX-01 plus existing Identity; asset rights/import limits/sync |
| Texting/read/seen and multiple sessions | CORE-01/02, UX-01 |
| Lifetime/shared memory | MEM-01, DATA-01; no separate memory system |
| Calls, task multitasking, excellent current voice/models | VOICE-01, NATIVE-06, MODEL-01 |
| Coding/OpenCode and CLI oversight | CODE-01/02, NATIVE-03 |
| Proactive assistance | CORE-03, AUTO-01 |
| Phone/glasses/health, selected camera “get me that” | DEVICE-01, IOS-01 onward, PAY-02; hardware classes tested separately |
| Instinct/iMessage/Google/browse/book/buy/Link/confirmations | MSG-01, WEB-01, PAY-01/02; five-item research prerequisite |
| FERAL-to-FERAL/multiplayer/friends/street matching | SOCIAL-01/02 |
| PMF/differentiation and trustworthy open-source operation | OPS-01; retained usefulness/payment willingness measured, not asserted |

## Worker assignment and integration rules

This environment has four concurrent agent slots: parent plus three workers. The completed investigation used native parity, runtime contracts and Instinct research workers. Use the same bounded waves for implementation:

1. Parent closes CI/publication and freezes candidate identity. Worker A runs native daily-use acceptance; B finishes named native parity subcards; C handles developer contracts/distribution investigation. A reports defects with reproductions before overlapping files are assigned for fixes.
2. Parent owns protocol/identity contract integration. Worker A owns conversation receipts; B coding identity/activity; C memory/migration. Integrate sequentially before the full-suite run.
3. Worker A owns TaskFlow dispatch/recovery; B voice coordination; C device/iOS contract fixtures. None invents a parallel dispatcher, session database or memory ledger.
4. After the requested research checkpoint, assign messaging, browser/account and commerce adapters in separate owned files. Contract changes remain parent-owned. Social/Linux/UX use later slots or independent waves; they are not forgotten.

Every task card must acquire exclusive files, name tests and dependencies, then land a coherent commit with docs and evidence. No shared editing during full-suite verification. Actual signing, accounts and physical hardware are dependency gates; workers can complete fixtures/isolated adapters without claiming those gates passed.

Use the existing GitHub repository and reviewable PRs. Stage explicit paths, preserve unrelated edits, fetch before push, never force-push or merge/release to bypass checks. Update the PR summary around the final change. Require corrected-source CI, appropriate actual app acceptance and signed distribution gates before publishing binaries. Verify branch protection separately; workflow presence is not evidence that protection is configured.

## Schedule and completion accounting

Effort labels above are planning judgments: small is bounded documentation/test/interface work; medium spans a coherent integration; large spans multiple contracts, recovery cases and external acceptance. They are not measured durations or promised delivery dates. Each large card must be split after its interface spike into independently verifiable changes.

Track progress by cards whose exit evidence passed, not screenshots, test totals or percentages of source files. Milestone 1 has a finite native action inventory; it is much closer than the entire newly requested social/commerce ecosystem. A calendar forecast should follow the first integrated native acceptance wave and the signing/account/device dependency check. Report implementation effort separately from waiting for external access. Uncertainty in an external integration must not stall unrelated local-app completion.

The next concrete checkpoint is BASE-01 plus NATIVE-01/03 and DEV-01 contract verification: corrected-source CI, the latest app exercised with a populated disposable profile, and a working clean-clone extension example. That checkpoint produces the measured defect list and work rate for the next schedule revision.
