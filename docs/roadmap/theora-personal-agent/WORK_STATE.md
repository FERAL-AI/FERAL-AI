# FERAL completion checkpoint

Updated October 4, 2026. Parent/integrator owns this file. Reconcile it with actual
Git, CI and processes after resuming; it is a checkpoint, not a live process lock.
See [resume procedure](RESUME_WORK.md), [execution plan](EXECUTION_PLAN.md) and
[all user requirements](REQUEST_COVERAGE.md).

## Current status at a glance

### October 4 native browser integration and packaged acceptance

- Implementation `b9afadb236e6e39d798a311d38955bb9e8051e74` is pushed to
  `feat/native-product-release-foundation-20261001` in
  [draft PR310](https://github.com/FERAL-AI/FERAL-AI/pull/310); main is unchanged.
  Native Browser is the 22nd destination. Existing tools/image delivery remain;
  no new action authority or implicit browser launch was introduced.
- Immutable **2026.9.37/build2026100401** now contains b9af, including the preceding
  setup/learning corrections. Its 52 native inputs and 489 packaged Python files,
  optimized build, bundled-runtime probes and strict ad-hoc signature pass.
  Native SHA `a661c5f8ca32b96b12a447224cf8c7f8e1704c789599349b5ad2c253f2f91142`.
- Actual isolated GUI: avatar/local setup, retained navigation, consent, real
  masked Chrome frames, intended-field Unicode, synthetic click outcome,
  approved coordinate input marker, Stop, navigation retirement and expiry/error
  clearing passed. Both hosts quit normally with exit 0; exact backend/browser
  listeners closed. Tasks were synthetic/manual/headless, not model-selected.
  [Artifact and actual limits](NATIVE_9_37_ACCEPTANCE.md).
- Frozen combined backend: 1,230 passed across 43 suites, one opt-in Chrome skip;
  1,297 Python inputs unchanged. Separate 26 focused/real-Chrome checks and 44
  screenshot-to-model wire checks pass; counts overlap. Browser 74, Providers 50,
  Onboarding 52 and linked model/desktop/error checks plus production typecheck
  pass. Ruff passes. Full mypy remains 809 errors in 233 files, no normalized
  additions/removals; type-cleanliness is not claimed.
- All executed required b9af CI workflows pass: general `37226066225`, native
  `37226066257`, desktop `37226066246`, docs `37226066274`, naming `37226066277`,
  version `37226066240`. Real-brain E2E `37226066229` is skipped. Later harness/docs
  revisions do not change the immutable package identity.
- A pixel-control defect in the committed acceptance harness was diagnosed in real
  Chrome; a private corrected harness passed 11 checks. Parent integrated its
  strict 9-by-9 patch repair; committed exact-source rerun is next. Failed evidence
  is retained. GUI launcher preflight defects stopped before dispatch and were
  corrected independently of product source.
- Two follow-up cards: reject unbound mutating REST calls before an empty-session
  approval is queued, preserving existing strict identity/approval gates; add
  actual verified Playwright selector input markers. Current explicit-session
  coordinate execution works. Native approval-button acceptance remains open for
  duplicate-label Computer Use targeting. No unknown effect was replayed.
- Canonical 9.37 and one 9.36 rollback copy remain; obsolete generated 9.34/9.35
  copies were retired, about 1.2 GB. Personal profiles/models/history were retained.
  All source workers released their paths; parent owns evidence/publication.
- Next: publish the harness/evidence checkpoint and run its exact committed Chrome
  probe, then the two follow-up cards, desktop viewing/foreground ownership and
  task-linked browser takeover. Full product audio/accounts/devices/Messages/
  commerce/migration/distribution gates remain explicit. Preserve unrelated
  AUDIT-FIXES.md edits unstaged. [Browser contract](BROWSER_VIEW_EVIDENCE.md).

### Preceding October 4 Mac setup checkpoint

- Published implementation correction:
  `213b94a3e9b21bfe44a70c63fc913570dfd144fb`, on
  `feat/native-product-release-foundation-20261001` in
  [draft PR310](https://github.com/FERAL-AI/FERAL-AI/pull/310). Main remains unchanged.
  The immutable package remains source
  `9a40b9ac8e7d155c16a5504a3332c1dca912ad41`. The later setup/status/learning
  corrections and research are published but not assembled into that package.
  Resolve current Git/remote identity before resuming; documentation-only
  checkpoint revisions may follow the implementation commit.
- Actual candidate **2026.9.36/build2026100303** is built and audited:51 native
  inputs and488 packaged Python files matched9a40. Normal tool-enabled local
  arithmetic and saved-context chat completed with durable receipts. Normal Quit,
  same-profile reviewed restart and remembered-codeword recall passed. The recall
  added an extra ACK, so strict response formatting did not pass. Replies took
  71–77 seconds. Current-candidate GUI Stop and real audio remain open.
  [Actual evidence](NATIVE_9_36_ACCEPTANCE.md).
- On resumption both exact acceptance host/backend were absent; the held restart
  host receipt reports normal exit0. No unknown app was killed or relaunched.
  Account, microphone/speaker, glasses, Messages, payment, migration, clean-machine
  install and signed distribution were not verified by this reconciliation.
- Baseline9a40 native, desktop, docs, naming and version CI pass. General CI
  `37170582632` had one managed-voice disconnect fixture failure,13,361 passes,
  83 skipped and75.45% coverage; all other executed jobs pass. Its test assumed
  terminal commit immediately after asynchronous surface cleanup. A controlled
  commit barrier now proves running status before exact cancelled/unknown status,
  retaining no-TTS/no-replay checks. New-head CI remains required.
  At the213b94 snapshot docs/naming/version pass, desktop/native/general CI are
  running and real-brain E2E is skipped. Do not interpret running as passed.
- The final setup/status/learning integration passes695 checks across31 suites,
  native production typecheck, core Ruff and whitespace. All1,292 Python inputs
  stayed unchanged. The preceding learning-only wave passed664; counts overlap.
  Full mypy remains809 errors in 233 files with zero added/removed normalized
  diagnostics versus the retained baseline. This is not type-clean. Automatic
  discovery now honors boot/live learning settings and the learner budget;
  explicit generation retains approval and the chat budget. Revocation cannot
  undo already incurred model cost. [Learning evidence](SELF_LEARNING_SWITCH_EVIDENCE.md) and
  [combined evidence](SETUP_PARITY_EVIDENCE.md).
- Three parallel read-only audits completed provider/setup, voice/multitasking
  and browser/messaging/commerce research. The new
  [integration plan](MULTITASKING_AND_EASY_SETUP_PLAN.md) includes subscription/API/
  local choice, three-step installation, durable background jobs while talking,
  task-owned browser/desktop resources, three verified shopping offers, exact
  payment review, scoped shared memory, CLI oversight and iOS/glasses handoff.
  New feature APIs/models/account/device behavior are proposals or documentation
  confirmation until their specific gates pass.
- Native catalog shape and CLI provider-switch defects are repaired. The native
  Providers/Onboarding fixtures pass50/52 assertions, and CLI targeted checks
  pass30. All worker source ownership is released; parent owns the frozen
  integration and publication. No new runtime, wallet, account or model
  was installed by the audits. Keep unrelated AUDIT-FIXES.md edits unstaged.
- Storage is approximately30 GiB free at the October4 check. The separate home
  Git store previously fell from approximately33 GiB to847 MiB with all54 commits,
  refs/reflogs/worktree indexes protected and full fsck passed. Later recurrence
  was881 MiB; the forced-object writer remains unproven. Do not clean recent
  objects from timestamps alone. Working data/models/history remain preserved.
- Parent owns documentation, shared contracts and publication. Setup/status/
  learning sources are integrated, frozen, checked and published with explicit
  reviewed files. Assemble that exact source before claiming packaged fixes.
  Preserve9.36 evidence and keep at most one preceding generated
  app copy after bounded ownership/retention review.
- Next engineering order: Mac setup/recovery/latency and packaged learning gates;
  reviewed local installation; supported ChatGPT plan adapter; durable task/
  resource ownership; conversational voice; enrolled messaging and quote-bound
  payments; physical phone/glasses and proactive acceptance. Independent ready
  cards can run in parallel; shared authority contracts integrate sequentially.
  Mac remains first; Linux expansion and Gen-UI stay deferred.

### Earlier reconciliations

The records below describe preceding checkpoints. They do not supersede the
current publication, worker allocation or actual GUI result above.

#### Latest integration and actual-app priority

- The corrected checkpoint was published as `a2500a526`. Mac usability and
  actual app journeys now lead execution; CI diagnosis proceeds in parallel and
  is a release gate rather than a dependency for every independent feature.
- Computer Use inventory succeeded again after the previous native-pipe failure.
  Actual GUI acceptance can resume with an isolated profile; inventory success
  is not a completed app test. Candidate9.34 remains immutable during that journey.
- Provider-review integrated checks passed. Committed-turn broader integration
  passed567 tests across28 suites with1,288 frozen inputs unchanged. Exact f41
  versus working-source nonincremental mypy remains812 diagnostics, zero changes.
  [Integration evidence](MAC_PROVIDER_CONTEXT_INTEGRATION_EVIDENCE.md).
- The startup diagnostic repair passes the real Process fixture's original28
  assertions/30s deadline, RuntimeHealth62 and lifeline2. The fixture now carries
  the assembled app's local-network policy and retains bounded server/native/
  passive-health phases. The old fixture also passed locally; remote root cause
  remains unproven. No startup timeout was increased.
- The fresh model wrapper refused before launch because its requested port was
  occupied. The pre-existing Ollama catalog matches the cached acceptance model;
  the worker neither created nor stopped that service. No recovery probe ran.
  Root is preparing isolated actual GUI acceptance using that external service
  with explicit ownership and no model download.
- Next: integrate and publish the verified source, exercise actual Mac journeys,
  repair observed failures, then complete managed voice. No further Linux or
  Gen-UI engineering is assigned.

- Published source is now `f41c204fea819f2a918d27f0715145ee9b99792d` in
  [draft PR310](https://github.com/FERAL-AI/FERAL-AI/pull/310); main remains unchanged.
  [General CI37134269808](https://github.com/FERAL-AI/FERAL-AI/actions/runs/37134269808)
  passed, including the backend PR lane, web, SDK and type-count checks.
  Docs, naming and version checks passed. The opt-in real-brain job was skipped.
- [Native CI37134269877](https://github.com/FERAL-AI/FERAL-AI/actions/runs/37134269877)
  passed production typecheck and feature/linked checks, then failed the original
  30-second Process fixture. Its retained evidence now shows that the interpreter
  and child launched, but startup did not become ready; the receipt precedes
  HTTP-server construction and does not establish a listening health endpoint.
  Root cause remains unconfirmed. Bounded startup diagnostics are the next Mac card.
- The already-triggered [Desktop Build37134269807](https://github.com/FERAL-AI/FERAL-AI/actions/runs/37134269807)
  passed after the Linux payload correction. Further Linux engineering is deferred;
  Mac delivery owns the active workstreams. This is not Linux GUI acceptance.
- Mac9.34 remains the immutable candidate built from `4eded179e`; its actual
  offline archive/readers round-trip passed. It does not contain the newer source
  fixes. No completed actual9.34 local-model recovery receipt is available yet.
  The earlier reported active run had not launched: a worker was waiting for
  command approval. The fresh resource-bounded probe requires an actual launch
  and completion receipt before its status can be reported as running or passed.
- Local, not yet published: provider confirmation now rejects changed drafts,
  repeated review and changed saved configuration. The integrated native runner
  passed21 provider fixture groups/33 assertions,27 linked-model groups,39 desktop
  assertions and five error-presentation assertions. These are mocked HTTP/wire
  tests, not real provider, keychain or GUI acceptance.
- Local, not yet published: attachment-bound committed-turn receipts pass171
  focused backend checks. Parent integration/full typing remain open. This is
  a prerequisite for managed voice, not completion of the voice experience.
- Worker allocation: Mac startup diagnosis; immutable packaged local-model
  recovery; completed committed-turn work awaiting parent integration. Parent
  owns contracts, integration, documentation and publication. Gen-UI remains
  deferred; coding expansion follows dependable setup, recovery and voice.

The following status bullets record the preceding publication checkpoint and
are superseded by the reconciliation above where they describe current activity.

- Published source: `4eded179e2060c226739511fc53f5a8bde0eaa16` on the existing
  review branch; main is unchanged and PR310 remains open/unmerged.
- Exact-head general CI [37131532584](https://github.com/FERAL-AI/FERAL-AI/actions/runs/37131532584)
  passed: **13,013 backend tests, 83 skipped, 75.24% coverage**, Python SDK112,
  web build/coverage/Playwright, asset coherence and the type-count gate.
  The main-only backend matrix was skipped. The two independent workflows below
  are failed; this is not an all-checks-green release.
- Native workflow37131532534 passed production typecheck and all feature/linked
  checks, then its genuine Process fixture timed out at the unchanged30s bound.
  The old harness lost phase/child diagnostics. Local28 assertions pass, but the
  remote blocked phase remains unknown. A narrow three-file diagnostics repair
  preserves bounded synthetic evidence on failure; next-head CI must diagnose it.
- Linux workflow37131532547 built the actual Debian package, then failed
  extracted-payload verification. Inspection confirms Tauri uses productName
  `FERAL` for `usr/lib/FERAL`; the smoke helper incorrectly assumed the binary
  name `feral-desktop` for that directory. Its original receipt lacks the first
  failing guard. Config-bound paths and phase diagnostics are now corrected;
  19 payload tests and seven platform tests pass. Corrected package acceptance
  remains open. Further Linux engineering is deferred while Mac delivery is prioritized.
- Native9.34/build2026100301 is **built**, with51 compiled native sources and487
  packaged production Python files matching4eded. Strict ad-hoc signature and
  bounded bundle audit pass. Actual bundled offline archive/readers round-trip
  passes with fresh synthetic data; original and restored bytes, preference/avatar
  hashes, context fence and negative refusal cases are verified.
  [Exact candidate evidence](NATIVE_9_34_ACCEPTANCE.md).
- Actual9.34 local-model recovery is active with unchanged deadlines. Available
  disk fell from910MiB to250MiB during host/model activity; the owner monitors its
  resource floor and must preserve any incomplete result. No disk-caused app crash
  is established. Do not rebuild or replace the canonical9.34 app while it is used.
- Worker ownership: native fixture diagnostics and the narrow Linux repair are
  frozen. The former Linux worker now audits Mac cloud/local onboarding gaps;
  the recovery worker owns its private actual probe. The bundled-update guard
  is frozen and passes63 tests; its worker now prepares the concrete managed
  chained-voice contract before any overlapping edits.
  Parent owns documentation, integration and Git. Mac delivery is the current
  priority. Gen-UI expansion stays deferred; coding-engine expansion follows
  dependable cloud/local setup, recovery, voice and data continuity.
- Ready publication: bundle update protection, native bounded failure diagnostics,
  the previously frozen Linux path correction and actual9.34 archive evidence.
  [Guard and diagnostics evidence](BUNDLED_UPDATE_GUARD_EVIDENCE.md).
  Mac provider-review repair is active in two separate Swift files: stale drafts,
  reused confirmation and changed saved configuration all dispatched writes in
  the baseline reproduction. Keep those unfinished edits out of this publication.

Historical results below remain source-bound records. GUI/audio, cloud accounts, physical
devices, clean installation, signing and managed saved-context voice are open.

### Prior pre-publication checkpoint

- **Current published source before this integration:** `ec1c301fade5301fa604b29333612f5fa094b075`,
  [draft PR310](https://github.com/FERAL-AI/FERAL-AI/pull/310), unmerged.
  Main remains `452a012557d06274063b72546433257b4694d611`.
  Exact-head backend CI passed **12,871 tests / 83 skipped / 75.10% coverage**;
  web, native and the unchanged mypy count gate passed (808 diagnostics, not
  zero type errors). Generated web-asset coherence now passes too. All executed
  jobs in CI run37125909725 and native run37125909767 succeeded. Opt-in live-brain
  and main-only jobs were skipped. The full CI result belongs to this source;
  the integration below requires its own exact-head run.
- **Actual full browser acceptance:** all **61 tests passed** against the frozen
  `7f818da08139952b1698644469e7016563512cd5` snapshot. All 1,127 tracked
  source entries stayed unchanged, loaded-module provenance matched, and owned
  processes/listener stopped cleanly. The walk excludes unsafe controls; ten
  Skills controls exceeded its cap, and five clicks had no verified visible
  effect. [Full browser evidence](LIVE_WEB_TRACKED_ACCEPTANCE_20261003.md).
- **Latest packaged candidate:** 9.33/build2026100207, exact `7f818da` source.
  Packaged local-model recovery/restart passed, with all 485 production files,
  executable hash and strict ad-hoc signature unchanged. A verified copy is
  preserved at `/private/tmp/feral-candidate-9-33-preserved.app`.
  [9.33 evidence](NATIVE_9_33_ACCEPTANCE.md). This candidate excludes the local
  attempt/preference/archive/layout changes below. Actual Mac GUI/audio acceptance is
  still unavailable because Computer Use cannot start its native pipe.
- **Frozen integration for publication:** attempt-v1 voice identity and bounded
  media/control liveness repair; portable native preferences and reviewed offline
  backup/restore; exact native runtime quiescence and explicit profile layout;
  Linux x86_64/glibc payload staging and executable package CI.
  Combined backend checks pass **455 tests**. Frozen full typing remains **808**
  with zero diagnostic changes and all **1,284 inputs unchanged**, digest
  `102f1cef34c100fa25eedb6424d97fc68c745df170351fcdef6747fb89d69973`.
  Native archive/preference/layout checks pass **280/71/87 assertions**; genuine
  Process-backed ownership checks pass **28**. Linux selector/smoke unit checks
  pass **20**, but the actual Linux package has not yet run. Production native
  typecheck passes. Earlier passing checks preceded the reproduced deadlock;
  the current checks include its repair. Managed saved-context voice remains
  refused pending the separately verified turn-manager/checkpoint adapter.
  See [integration evidence](VOICE_ATTEMPT_PREFERENCE_INTEGRATION_EVIDENCE.md),
  [archive evidence](NATIVE_PROFILE_ARCHIVE_EVIDENCE.md) and
  [Linux evidence](LINUX_DESKTOP_PAYLOAD_EVIDENCE.md).
- **Final native correction verified:** the retained-client reproduction dispatched
  one POST before repair and zero after pause or same-address replacement with
  the shared HTTP epoch guard. Gate34, Agent42, Workflow83, Integration80,
  Configuration checks, archive280 and voice55 pass in the final integrated
  runner, plus27 mocked model groups/desktop 39/error 5. Complete production
  typecheck passes. The private exact final-save path remains available while
  all new native actions are fenced. [Admission evidence](NATIVE_ACTION_ADMISSION_EVIDENCE.md).
- **Active ownership:** worker sources and parent native wiring are frozen and
  reviewed. Other workers prepared bounded disposable packaged archive and
  recovery probes. Parent now publishes the coherent wave and assembles exact
  source9.34/build2026100301; 9.34 is not yet built. Staging completed with cached
  CPython 3.11.15/SQLite 3.53.1/FTS5, OpenCode 1.18.10, v2 assets and Python SDK import
  containment. New-head CI and actual bundled probes remain required. Computer
  Use was retried after restoring documentation and still reports its native-pipe
  startup failure; no current GUI/audio certification is available.
  Gen-UI expansion is deferred; additional coding-engine work follows dependable
  setup, voice and data continuity. Next ready implementation cards are the
  bundled-update guard and managed chained-voice adapter, with native identity
  contracts settled before overlapping source edits.

### Earlier checkpoint records

- Review branch: [draft PR310](https://github.com/FERAL-AI/FERAL-AI/pull/310).
  Latest published source is `86a8b54ed273642da40776e75609f5a45a197924`; verify
  current remote tip before resuming. Main remains `452a01255`; no merge or release.
- Exact7f PR verification: native, SDK, web, docs and naming passed. Remote mypy808
  matches all frozen local diagnostics against the unchanged812 count baseline.
  Backend failed:12,867 passed/3 failed/83 skipped/one setup error,75.10% coverage.
  Two fixtures omitted real connection/session ownership; corrections pass132
  focused tests. The setup sys.modules iteration now snapshots before iterating;
  the affected ambient suite passes16 tests. Corrections published86a8b54; new
  exact86 backend CI passed12,871/83 skipped/75.10%, native and mypy808 passed.
  The web job failed after1,358 passing tests with a post-unmount Shell hydration
  callback. Five deferred-response regressions reproduced it; the correction
  passes201 shell tests and awaits new-head full CI. See
  [hydration evidence](WEB_HYDRATION_LIFECYCLE_EVIDENCE.md).
  Prior55a99 CI passed12,783/83 skipped/74.93%; do not apply it to7f.
- Packaged **9.32/build2026100206** contains exact source `61550e74f`.
  Build/signature/runtime/source checks passed. Actual disposable packaged-backend
  Stop → explicit recovery → local-model recall → restart → recall passed.
  Foreign/stale recovery refused; four terminal receipts and zero tool executions;
  no cancelled task replay; both owned processes/listeners stopped cleanly.
  Both startup inventories exposed zero tools from the empty installed fixtures.
  Strict signature, hash and all 484 source comparisons passed after the run.
  The exact artifact is preserved at `/private/tmp/feral-candidate-9-32-preserved.app`;
  the preserved copy also passed strict signature and all 484 source comparisons.
  Earlier 9.31 failed attempts remain preserved. See
  [candidate acceptance](NATIVE_9_32_ACCEPTANCE.md).
  Native GUI acceptance remains blocked by Computer Use native-pipe startup failure.
- Integrated: timestamp/ACP/connector repairs, validated extension reloads with
  cancellation/registry-owner fencing, redacted errors, strict explicit approval
  precedence and the concrete browser adapter. Final combined backend1003 passed;
  full configured mypy818 versus812 still failed,27 removed/zero added compared
  with prior published diagnostics. Exact-source remote CI on `d2158fc6a` passed:
  backend12,654/83 skipped/74.77% coverage, native and Python SDK112 passed.
  Remote mypy also818; against the tracked baseline22 added/16 removed remain.
  See
  [integration recheck](INTEGRATION_RECHECK_20261002.md).
- Current completion wave implements interrupted-context recovery, reviewed
  cloud setup and offline profile archive. Final backend integration:570 passed,
  19 warnings in18.99s. Frozen full mypy:811 diagnostics versus812 baseline,
  zero added/seven removed against d215; the current PR type ratchet also passed.
  See [completion evidence](COMPLETION_WAVE_EVIDENCE.md).
- Remaining release gates include new-wave required CI, current GUI acceptance,
  migration/upgrade, signed distribution and clean-machine installation. Account,
  physical-device, Linux and broader feature acceptance remain separate gates.
- Published55a99: speech-only interruption/overlapping utterance admission and
  default archive coverage. Combined279 tests and unchanged811 diagnostics passed
  before the exact-head full CI result above. See
  [interruption evidence](VOICE_OUTPUT_INTERRUPTION_EVIDENCE.md) and
  [archive evidence](PROFILE_ARCHIVE_COVERAGE_EVIDENCE.md).
- Current frozen source wave: shared owner-checked voice configuration, exact
  engine callback ownership, recognition readiness before the first microphone
  bytes and native chained audio/cancellation handling. Engine285 passed/2 skipped;
  configuration156 passed/2 skipped; typed recognition57 passed; native44 voice
  assertions and production typecheck passed. These counts overlap. Whole-core
  Ruff passes; frozen mypy808 has zero added/three removed versus verified55a99;
  all1,278 inputs stayed unchanged, digest
  `94d837e9582b9f875a7d4d96329ad22eef583439c6b45cfe84eddf592ef3da21`.
  [Startup/ownership evidence](VOICE_STARTUP_OWNERSHIP_EVIDENCE.md).
  Candidate9.33/build2026100207 is assembled at exact7f:485 production files match;
  strict signature/runtime audit passed. Actual attempt2 completed Stop/recovery/
  real local-model recall but restart exceeded100s readiness. Attempt3 passed the
  complete unchanged journey/deadline in a fresh profile, including restart recall,
  four receipts/zero tools and clean owned shutdown. Post-run hash/signature/all485
  sources match. Earlier failed records remain; timeout cause remains unresolved.
  [9.33 acceptance](NATIVE_9_33_ACCEPTANCE.md).
  A live browser run passed60 checks but failed copied-source integrity; do not
  certify it. Exact7f tracked rerun passed60/failed1 Chat with an ENOSPC write error;
  all1,127 source entries stayed unchanged, but end provenance was incomplete.
  Focused resource-controlled Chat/keyboard rerun passed4 tests with complete
  provenance and unchanged sources. Fresh full61 run with the corrected shutdown
  observer is active; full browser acceptance remains open.
  Next wave active: backend/native attempt-v1 ACK/media/control correlation and
  explicit native preference/archive continuity. Native attempt fixtures55 pass;
  preference component64 pass but remains unwired until archive/UI integration.
  Candidate9.32 remains unchanged and excludes these later source corrections.
  Its recovery probe passed with a disposable empty-tool fixture;
  it does not certify tool-enabled tasks, voice, cloud accounts or GUI operation.
  Gen-UI expansion is deferred; coding-engine expansion follows dependable setup,
  recovery, voice and data continuity. Keep the candidate frozen
  for the pending GUI test. Do not rewrite existing public history.

## Objective and standing scope

Finish the existing FERAL system as a dependable native app and open-source
developer platform, preserving its runtime, memory, tools, clients and data.
Then integrate the requested receipts/concurrent sessions, voice multitasking,
CLI oversight, proactive assistance, phone/glasses continuity, messaging,
commerce, collaboration and Linux capabilities through existing contracts.

The user requested autonomous engineering, parallel workers, tests, documentation
and GitHub publication. Continue authorized reversible work without redundant
confirmation. Sandbox/managed requirements still apply. Auto-review must be
selected by the user/runtime; these files cannot grant filesystem access or
authorize real purchases/messages, personal-data reset, merge or release.
The user has now requested continuation and full application implementation after
the research/checkpoint report. Continue repository engineering in dependency
order; this does not grant real account login, messaging or purchase authorization.

## Repository and published work

| Field | Checkpoint |
|---|---|
| Checkout | ASOS inside the user's thoera-mac workspace; verify actual root |
| Origin | `https://github.com/FERAL-AI/FERAL-AI.git` |
| Working branch | `feat/native-product-release-foundation-20261001` |
| Published review | [Draft PR #310](https://github.com/FERAL-AI/FERAL-AI/pull/310), open/unmerged |
| Current checkpoint | Publishedec1c301 all executedCI passed; backend12,871/83 skipped/75.10%. Exact7f9.33 packaged recovery/restart and actual browser61 passed. Frozen backend455/type808 verified; native retained-client admission repair and9.34 assembly next |
| Prior correction | `2754ce677`: wait for stale-heart-rate metadata without weakening assertions |
| Unrelated local edit | `AUDIT-FIXES.md`; preserve and exclude unless separately reviewed |
| Disk observation | Latest2.2GiB free; five inspected completed-test mypy caches removed(~225MiB), logs/profiles/models/artifacts retained. Availability fluctuates; reuse staged runtime and caches during packaging; no disk-caused app crash established |

The checkpoint's own commit cannot name its future hash. Read current HEAD from
Git/helper; evidence below explicitly names the source it tested. Do not update
every historic source identifier to HEAD or regenerate a baseline to make a gate
appear green.

## Dated source and candidate verification

Core commit **90b75587a** contains the following working core wave and its fixture
repairs. Its SDK authoring follow-up adds verified Python runtime adapters and
Node loopback plugin hosting: **112 Python tests and 82 Node tests passed**, plus
isolated wheel import and actual central-runtime invocation using an installed
Node tarball. [SDK authoring evidence](SDK_PLUGIN_AUTHORING_EVIDENCE.md) states
the dependency/bundling and loader-acknowledgement limits. Both commits are being
published to the existing draft PR. Both are now published. Actual
[CI37079499104](https://github.com/FERAL-AI/FERAL-AI/actions/runs/37079499104)
on `205f468a2` completed the backend with **12,540 passed, 1 failed, 83 skipped,
585 warnings in831.17s; coverage74.66%**, above the unchanged50% floor. Sole
failure is `test_web_disconnect_stops_voice`: its bare voice mock does not
declare node ownership, so the new exact-ownership guard refuses teardown.
The fixture repair now explicitly tests empty, bound and unknown ownership;
108 local tests passed with production ownership checks intact. Generic Node CI passed81 tests but
failed the real-runtime test because its Node-only job has no repository Python
environment. The correction retains mandatory runtime testing in both installed
brain jobs, with standalone81 unit tests and explicit validated interpreter
selection. Local82 tests pass with no skips; corrected remote verification is
pending. Native, web, lint and docs checks passed on205. Nonblocking mypy still
fails **845 errors against812**. No new packaged app is asserted here.

Source **634595c7194aedaf6eb9b8c91cbacb87fedea4bd** is committed/pushed.
[CI37071282138](https://github.com/FERAL-AI/FERAL-AI/actions/runs/37071282138)
and [native37071282156](https://github.com/FERAL-AI/FERAL-AI/actions/runs/37071282156),
docs/naming/version passed. Completed backend job111051144969: **12,426 passed,
83 skipped,574 warnings in841.38s; coverage74.42%**. Generic Python SDK92 passed;
web/device/extension/generic Node22 and other required checks passed. Live-brain
and main-branch Linux matrix skipped. Nonblocking mypy111051062407 **failed:
848 errors in238 files,1,257 checked** against812 baseline. Two new server
return/coroutine annotations are being repaired in the working wave; do not
call848 unchanged debt or claim the type ratchet passed.

Immutable **2026.9.29/build2026100203** was built from that source, executable
SHA256`013ccc1250f6ed8c58bf80254ff332f93e0957226691f72c6c41f5c388f2f102`.
All481 production Python files match the commit; bundle audit passed13,060files,
265Mach-O/9internal links, Python 3.11.15/SQLite 3.53.1/FTS5/OpenCode 1.18.10.
Ad-hoc signing is separate from Developer-ID/notarization. Actual isolated GUI
has now passed the previous Security layout crash path, rich/code Copy, three
real tracked local-model replies, exact Stop, deliberate Quit/backend-child
absence, same-candidate reopen and exact status reconciliation without replay.
The worker froze [9.29 acceptance](NATIVE_9_29_ACCEPTANCE.md). These bounded
results do not establish unrestricted daily use or account/device/distribution
acceptance. The candidate contains no later working activation/Forge fixes.

The new committed core wave implements:
DATA01C exact attachments/readiness/writer cleanup, parent authenticated WS/HUP
preludes and mandatory boot installation, primary snapshot guards, create-only
Forge drafts, actual executor review admission and executable extension example.
Whole core manifest1261Python files digest
`23fed780ce1e9eef2d1a6af6c2fc2470e295e51ac44a5780b7073d0344523b2f`.
Full run log`/private/tmp/feral-wave4-full-backend-20261002.log` ended exit 1 with
coverage INTERNALERROR: a patched SQLite connection remained active; pytest
cleanup also retained patched Path.exists. All 1,261 manifest hashes remained
unchanged. There is no valid full-suite coverage/summary from that run. Two early
socket failures reproduced sandbox binding denial, then passed outside the sandbox.
Fresh patch-only fixtures passed with and without coverage; full teardown leak
origin remains unresolved. FD soft limit 256 is observed, exhaustion unconfirmed;
4.8 GiB disk remained free and no ENOSPC was observed.

Parent combined 27-suite regression passed **597 tests, 19 warnings in 19.49s**.
Afterward the existing primary transcript fixture was updated to bind the real
new guard, and approval identity validation stopped false/zero/empty container
values becoming omitted owners. Primary/ingress/approval regression passed
**46 tests, 18 warnings in 2.20s**. Forge path narrowing passed 43 tests; the first
targeted invocation omitted --no-cov and failed the global coverage floor despite
43 passing cases; the corrected targeted command passed. Full local mypy now
measures **845 errors in 238 files, 1,261 checked**, after removing two new Forge
errors. This is still above the unchanged 812 baseline; Ubuntu verification pending.
Additional frozen fixture repairs keep config/vault audit writes in disposable
profiles (37 config and 24 integration tests passed) and initialize the real
optional collaborator in a synthetic voice proxy (86 voice/attribution/activation
tests passed). Original assertions and production behavior were preserved.
See [full-run investigation](FULL_BACKEND_TRIAGE_20261002.md).
Mandatory boot now installs the lifecycle, unknown legacy sessions remain legacy,
and managed voice/handoff/manual context mutations remain explicitly unavailable.
No native candidate contains this working wave yet.

Native saved-context and Agents/Workflows/Automation/Connections gates are
implemented. Full pre-label37-feature matrix passed. The narrow
Workflow scope-label refinement passed its affected runner:80 Workflow assertions,
25 linked model groups,39 Desktop and5 Error assertions. Final production
typecheck passed. Parent review then found late readback/refresh publication gaps
in four panels. Corrected final four-suite run passed Specialists42, Workflow83,
Automation43, Connections41; linked Model25 groups, Desktop39, Error5 and final
production typecheck passed. [Native evidence](NATIVE_SAVED_CONTEXT_EVIDENCE.md)
keeps each tested revision distinct. No new candidate has been assembled.
Developer worker completed bounded9.30 launcher/read-only checkpoint probes:
47 disposable assertions, Ruff, syntax and CLI help passed; no GUI launch.
Parent staged the noneditable Python SDK in the bundled interpreter under the
existing locked constraints; actual staged import/location and BaseSkill adapter
probe passed. This does not repair the separate loader false-acknowledgement
card in [registry recovery plan](REGISTRY_RELOAD_RECOVERY_PLAN.md).
The required local full-suite recheck completed exit0:12,576 passed/50 skipped/
574 warnings in818.52s;75.03% coverage against unchanged50% floor. It retained
PR performance exclusions and read-only descriptor/mock observer;1261 frozen
core files digest39a8b055528037f4a4652d67f9372a32c4871b3fed10791be3333ae14ca7d1d8
were unchanged after completion. No teardown/coverage crash or surviving tracked
mock recurred. Peak sampled descriptors148 against256; earlier cascade cause
remains unproven. Exact-source Ubuntu CI37082003338 passed all required jobs:
12,543 backend tests/83 skipped/592 warnings in702.14s, coverage74.66%; Python
SDK112 and Node81 unit +1 mandatory actual-runtime tests passed. Native
CI37082003388 passed full feature/linked fixtures, typecheck, actual child lifeline,
bundle-audit and frozen-source comparison checks. Mypy remains nonblocking
failure845 versus812; live-brain and main-branch Linux matrix skipped.
See [recheck evidence](FULL_BACKEND_RECHECK_20261002.md).
Parent owns source freeze, shared docs, explicit-path
publication and candidate identity. Preserved AUDIT-FIXES.md stays excluded;
historic failed candidate evidence stays intact. Next: publish coherent corrections
and native integration, verify corrected-source remote CI and local full suite,
then assemble/audit an exact-source9.30 candidate for actual isolated acceptance.

### Active9.30 artifact and next owned cards

Built **2026.9.30/build2026100204**, source
`dd69c7bf5175517966a363591fa8137782358535`, executable SHA256
`421d9756b42b3fd55a88102c396994112da561841b49e07171fe58d3aaf915b4`.
All482 production Python files match that commit. Bounded bundle audit passed
13,089 files/265 Mach-O/9 internal links; CPython 3.11.15/SQLite 3.53.1/FTS5 and
OpenCode 1.18.10 probes passed. Strict ad-hoc signature verification passed.
Actual packaged SDK0.1 import/location and BaseSkill factory probe also passed.
Prior9.29 artifact is preserved at `/private/tmp/feral-candidate-9-29-preserved.app`
with its original executable hash. No release/signing certification follows.

GUI acceptance is **not run**: worker initialization, reset, repeated inventory
and independent parent inventory all returned `Sky Computer Use native pipe
startup failed`, with no apps/browsers. No candidate was launched. The exact
disposable GUI profile passed launcher check-only at
`/private/tmp/feral-native-populated-saved-context-20261002`; it contains synthetic
settings only. Restore the Computer Use runtime before actual GUI journeys;
do not attribute the tool failure to FERAL or substitute a fixture for GUI proof.

Completed independent cards after the preceding whole-backend freeze:

- Native worker completed actual packaged-backend headless context/restart and
  cancellation acceptance at `/private/tmp/feral-native-9-30-headless-20261002`.
  The bounded probe and9.30 evidence are frozen. Actual local inference/backend
  results stay distinct from GUI not-run. Artifact/resources remain frozen.
- Developer worker completed registry/implementation/reload prepare-and-publish,
  helper-mediated registration capture, recovery tests and the SDK false-ACK
  negative probe correction. Review follow-ups add exact live registry ownership,
  retained cancelled preparation tasks and redacted exceptions. Final focused159
  runtime/112 Python SDK/82 Node SDK cases passed; the later browser adapter
  follow-up passed453 controlled tests and introduced no normalized type error.
- Type worker completed TYPE01H timestamp narrowing:107 tests passed, five
  focused errors removed, older optional SID error retained. Files are frozen.
  TYPE01I/J failover-fixture/ACP checks passed76 tests and removed12 focused
  diagnostics. Connection-table checks passed33 tests and removed four focused
  diagnostics while retaining two older WhatsApp errors. All these files are
  frozen; no new global type count is asserted. These edits are not covered by
  the completed full backend result above.

The separate strict-mode manifest approval card passed324 tests. Final parent
integration on corea320f54bf passed1003 tests/seven warnings in17.84s;
1264 Python files digeste41154bfa63cc5725c60e21622a6729e335e409f9071fb084c4742afadd3bb62
were unchanged. Full configured local mypy818 errors/234 files/1264 checked still
exceeds812. Compared to the prior published845-error log,27 diagnostic occurrences
were removed and none added. No baseline, suppression or cast was added.
[Final integration evidence](INTEGRATION_RECHECK_20261002.md) keeps the preceding
643-test/819-error check distinct from the corrected browser-wrapper follow-up.
All workers are frozen; parent owns publication and subsequent exact-source CI.

Parent owns final integration, global type measurement, checkpoint/PR and Git.
Do not overlap worker files or overwrite uncommitted work after an interruption.
Observe the current source-specific remote CI result before the next push.

Source **cd1571ef94aa2fe244023d56ba468f7ba3266700** is committed/pushed.
[CI37066669634](https://github.com/FERAL-AI/FERAL-AI/actions/runs/37066669634)
and [native37066669777](https://github.com/FERAL-AI/FERAL-AI/actions/runs/37066669777),
docs/naming/version passed. Actual backend job111036192549: **12,369 passed,
83 skipped,572 warnings in788.00s**, coverage74% rounded. Generic SDK/web/device/
extension/architecture/syntax/Ruff/assets passed; live-brain/main Linux skipped.
Mypy job111036125955 still failed: **846 errors in238 files,1,253 checked** versus
812 baseline, with no checkpoint-code diagnostics. The actual9.28 artifact
below remains on66c7; this publication does not rebuild it.

Source **66c7cd500d7ec353c68da97f44b99d6201af9abe** is committed/pushed.
[CI37043435460](https://github.com/FERAL-AI/FERAL-AI/actions/runs/37043435460)
and [native37043435313](https://github.com/FERAL-AI/FERAL-AI/actions/runs/37043435313)
**passed**. Parent fetched completed backend job110958970036: **12,305 passed,
83 skipped,574 warnings in844.56s**, coverage74% rounded. Generic Python/Node22,
web/extension/device SDKs, architecture/syntax/Ruff/assets and docs/naming/version
passed. Live-brain/main Linux matrix skipped. Nonblocking mypy job110958888836
still failed: **846 errors in238 files,1,250 checked**, baseline812. All four
attributed receipt follow-up diagnostics are absent; ratchet is not green.

Native **2026.9.28/build2026100202** was built/signed on that frozen source;
executable SHA256`dba3785d53634fb699986c2ade9290f42ff5c0631c63224510bfb34350d165f0`.
All479 production Python files match that commit. Bundle audit passed13,054
files/265 Mach-O/9 internal links, pinned Python 3.11.15/SQLite 3.53.1/FTS5 and
OpenCode 1.18.10. Ad-hoc verification is not Developer-ID/notarization.
Actual isolated app acceptance ended **failed**. Original New conversation/
retained-error AX path survives, real42 reply passed, and growing context now
narrows complete optional schemas below the byte budget. **A new SIGTRAP during
Permissions and Cost navigation was confirmed**; no crash-free claim.
An earlier unexplained normal exit0 and early response-timeout banner are
preserved independently. New IPS identifies main-thread EXC_BREAKPOINT/SIGTRAP
through `+[NSApplication _crashOnException:]` and repeated NSView constraint
updates, distinct from the old accessibility-label recursion. The exact-time OS
layout-cycle log points to `NativeSelectableTextField.fittingSize` invalidating
intrinsic content size during measurement. A focused purity assertion failed
on that live-field mutation and passes after measuring a copied cell. Small
actual Security and Unicode/copy probes pass; they did not reproduce the full
candidate geometry crash. The privacy-redacted exception reason and full next
candidate acceptance remain open; disk space is not an established cause.
9.28 acceptance is frozen. No process from that candidate remains owned/running.

The next reviewed source wave passed **235 integrated backend tests,7 warnings
in9.38s**. It includes the inactive trusted runtime-context lifecycle, direct
owner progress and explicit interval automation follow-up. The frozen complete
native fixture runner passed all36 feature suites, linked25 groups, desktop 39
and error 5 assertions. Production typecheck and18 child-lifeline/auditor/source-
equality regression tests passed. The next assembly is
9.29/build2026100203 (subsequently built and tested above). See
[integration evidence](NATIVE_CONTEXT_INTEGRATION_EVIDENCE.md). Production
checkpoint activation is not enabled: authenticated presave readiness and every
writer/cleanup/restore boundary must close first.

The next storage/progress/workflow slice passed **165 integrated backend tests,
8 warnings in5.80s** on frozen sources. Warnings include an older memory suite's
event-loop-closed worker warning and restored receipt-limit environment leakage.
[Progress evidence](CHAT_PROGRESS_IDENTITY_EVIDENCE.md) records the exact command.
DATA01A independently passed62 new tests, codec mypy and Ruff; checkpoint storage
does not yet restore live model history. Workflow67 native fixture assertions and
three real registered-router/SQLite cases passed; linked/app acceptance is separate.
Native receipt adoption remains uncommitted working code while these independent
slices are integrated. Do not mistake current working files for the9.28 payload.

Prior source **4e19f07f8603196fd73ecdafd0e127dbad865e69** also passed
[required CI37038556986](https://github.com/FERAL-AI/FERAL-AI/actions/runs/37038556986)
and [native37038557228](https://github.com/FERAL-AI/FERAL-AI/actions/runs/37038557228).
Parent fetched backend job110942764029: **12,278 passed/83 skipped/574 warnings
in829.15s**, coverage74% rounded. New generic Python HTTP/WS and generic Node22
jobs passed. Existing web/device/registry/lint/syntax/architecture checks passed;
live-brain/main Linux matrix skipped. Mypy job110942696634 failed **850 errors
in238 files,1,248 checked**, baseline812. Two newly introduced receipt-store
diagnostics are repaired in the next working wave; full new-source count is
separate. Never call the type gate passed.

Source **739a20c0b56d2a2aa9956cde395a79b6dd5e0935**:
[CI37033165436](https://github.com/FERAL-AI/FERAL-AI/actions/runs/37033165436)
and [native37033165446](https://github.com/FERAL-AI/FERAL-AI/actions/runs/37033165446)
**passed**. Parent fetched completed job logs: Ubuntu/Python3.11 PR backend
**12,252 passed / 83 skipped / 567 warnings in923.32s**, coverage74% rounded.
Web coverage/build, API-stubbed Playwright, syntax, Ruff, architecture, assets,
device SDKs, generic Python HTTP SDK and registry passed. Docs/naming/version
passed. Live-brain and push-main Linux matrix skipped. Non-blocking mypy remains
**failed:852 errors in241 files,1,245 checked**, baseline812.

Immutable native **2026.9.27 /2026100201**, executable SHA256
`1071d7b679730a7d2cdca09c422aba4b65a6ef94ada6b8357a10ad8fcc4556d0`,
stages runtime source **34a43e640597ca721fb915149f29c9b73b51394a**. Parent compared
all478 bundled production Python files against that Git source: no missing files
or mismatches. Bounded bundle audit passed13,052 files/265 Mach-O/9 internal links,
Python 3.11.15/SQLite 3.53.1/FTS5/OpenCode 1.18.10. Ad-hoc signing passes;
Developer ID/notarization remain open. Current CORE04/SDK edits are absent from
this candidate. [Actual9.27 results](NATIVE_9_27_ACCEPTANCE.md) remain separately
owned and frozen by the native worker: reviewed Deny/Allow and exact file readback
passed, but New conversation caused another confirmed SIGSEGV. Conversation
growth also triggers a byte guard after a few turns; neither gate is closed.

## Historical checked source

Source **6b368ccf7**, [CI run 37024016126](https://github.com/FERAL-AI/FERAL-AI/actions/runs/37024016126):

- Corrected-source web build/coverage and Playwright **passed remotely**. Browser
  specs stub API responses; live-brain workflow was skipped.
- Ruff, architecture, syntax, asset coherence, node SDKs, registry and extension
  checks **passed**. Generic SDKs under `sdk/python` and `sdk/node` are separate
  from the node SDK checks.
- Docs, naming and version workflows **passed**.
- Backend Ubuntu/Python 3.11 PR fast lane **passed: 12,127 tests / 83 skipped,
  73.94% coverage**. Parent re-queried the completed job and its actual log.
  This is the PR lane; the separate Linux matrix was skipped.
- [Native run 37024015923](https://github.com/FERAL-AI/FERAL-AI/actions/runs/37024015923)
  **passed** production typecheck, feature/linked/desktop checks, real child
  lifeline tests and bundle-auditor fixtures. This job does not operate the app GUI.
- Non-blocking mypy **failed: 859 errors in 244 files, 1,240 checked**, versus the
  recorded 812-error ratchet. The 47-count rise requires attributable-regression
  triage; it must not be called wholly unchanged debt. Use matching Ubuntu/Python
  conditions; the logs forbid regenerating this baseline on macOS.

Packaged Mac artifact inspected directly, without a new launch: **2026.9.26 /
2026100117**, `desktop-native/build/FERAL Native Preview.app`; executable SHA-256
`3dbfbbd63af7206bd99d81065e6f8a3f239bf0eb4bd3e31584f6c2cbdae7a0e3`.
Historical 9.26 avatar-onboarding/quit/relaunch passed. Earlier actual reply/import/
coding outcomes belong to their earlier candidates. This artifact is not certified
as containing every later source commit or completing the populated-profile matrix.

## Published earlier source slices

- **CODE-01A/B:** trusted caller-bound external-agent continuity plus exact-scope REST list/poll/permission/cancel. Parent combined check **162 passed / 6 warnings**; final API-only fixture-import check **27 passed / 6 warnings**. Real ACP fixture pipes, not general actual-engine acceptance. [Evidence](CODING_SESSION_EVIDENCE.md).
- **DEV-01A:** registered generic Python SDK HTTP routes, caller credentials, explicit approval/session fields, truthful HTTP/application errors. **25 passed / 5 warnings**. Existing brain PR/main jobs now include this suite; WebSocket and Node remain separate. [Evidence](PYTHON_SDK_EVIDENCE.md).
- **TYPE-01A:** precise vault/bootstrap review/loop/Future typing; **116 passed / 7 warnings**, focused mypy clean in three files. Full Ubuntu ratchet remains required. [Evidence](TYPE_RATCHET_EVIDENCE.md).
- Parent preserved failed initial collection/default-home and fixture-import-order runs; corrected disposable-profile combined run passed. Ruff/diff checks passed on these slices. No baseline or authorization assertions were relaxed.

## Historical parallel wave and ownership

| Worker | Exclusive scope | Active work |
|---|---|---|
| Parent | Shared docs/CI/Git and next assembly | Core90b75587a committed; SDK follow-up publication/CI next; failed full run disclosed |
| coding_session_isolation | SDK authoring adapters/examples/tests/evidence frozen and transferred | Python112/Node82 plus wheel/tarball checks passed; next loader card needs separate ownership |
| python_sdk_contracts | Type/full-run triage and four fixture-isolation repairs frozen and transferred | Config37/integration24/voice-attribution-activation86 passed; further harness/doctor work is separate |
| native_populated_acceptance | Immutable9.29 report; saved-context source transferred; four feature-model task gates | Agents/Workflow/Automation/Connections, their tests and scoped shared native policy wiring; no assembly or GUI until integrated freeze |

Original immutable bundle evidence is frozen in [NATIVE_POPULATED_ACCEPTANCE.md](NATIVE_POPULATED_ACCEPTANCE.md): actual rich persistence/search/rename/pin/relaunch and one local-model reply passed. **SIGSEGV with excessive SwiftUI accessibility recursion is confirmed by macOS report and watched exit**. Context truncation, guessed Weather fallback, missing result success key and pending-review false-completion text are also reproduced. Do not call this app release-ready.

Source can change in these exclusive areas during targeted checks; freeze all workers before full integration suites and candidate assembly. Recheck free disk before packaging. Preserve unrelated `AUDIT-FIXES.md`. No real external accounts/messages/purchases or personal data changes occurred.

Historical next action before634595c: publish the integrated wave and build/audit9.29, then run guarded actual
acceptance. Activate runtime checkpoints only after every writer/cleanup/restore
path is fenced. Then memory migration/native parity/voice and exact-device/iOS
contracts. Messaging/browser/Link, social/Linux/distribution remain explicit
cards. Durable processing receipts do not implement read/seen or prove effects.

## External acceptance and decision dependencies

Signing/notarization credentials, named permitted provider/account identities,
real mic/audio, physical iPhone/glasses/background matrix, clean-machine upgrades
and a Linux host matrix must be checked. They are declared dependencies, not proof
that access is unavailable. GitHub reported default-branch dependency alerts; inspect
affected packages/applicability before declaring them fixed or irrelevant.

## Update record format

For each integrated card append: date; card and owner; exclusive files; source SHA;
exact verification command/exit/result and evidence class; candidate/hash where
relevant; remaining acceptance; commit/PR; next action. Mark implemented,
verified and published independently. Preserve failed runs and stale claims in a
dated record instead of overwriting them as if they had passed.

## October 2 workflow setup verification

- Parent reviewed the helper and ran its 10 isolated tests successfully, exact
  targeted Ruff successfully, and actual human/JSON status modes successfully.
  Helper reported the expected checkout/origin, HEAD, dirty paths, 8.1 GiB free
  and all recovery documents present. It does not read file contents/history,
  check remote CI or recover workers.
- Added the same standard-library helper tests to the existing CI syntax job.
  At source `6b368ccf7`, the remote syntax job also passed those 10 helper tests.
- Parent checked CI YAML parsing/step placement, diff whitespace, repository
  naming, 135 tracked shipped-document leakage targets and changed-document
  local links successfully. The unfiltered leakage scan's three pre-existing
  ignored handoff files remain outside publication; they were not changed.
- Resume/approval flags were checked against installed CLI help and official
  fetched OpenAI docs. No second session, goal, daemon, hook or global config
  change was started. The current managed permission profile remains unchanged.

## October 2 integrated core repair wave

- Parent frozen-backend integration: **320 passed, 7 warnings in 24.48 seconds**, exit 0, using explicit disposable home/data storage. Exact command covers direct fallback, coding/ACP/context/REST, TaskFlow/exact approvals, deterministic pending response, Ollama metadata/budget, approvals, orchestration and streaming. Log: `/private/tmp/feral-integrated-contracts-20261002.log` (local artifact, not shipped).
- CORE-03A worker: **299 passed / 7 warnings**, current deny/plan/lease and exact-once internal approval checks; interrupted effects remain unknown and cannot auto-replay. [Evidence](TASKFLOW_DISPATCH_EVIDENCE.md).
- MODEL-01A worker: **202 passed / 7 warnings**, final dedicated **30 passed**; actual selected context checked without downloads/allocation changes. [Evidence](LOCAL_MODEL_CONTEXT_EVIDENCE.md).
- Parent direct fallback: refuse ambiguous/untrusted/mutating fallback, use central ToolRunner, handle pending/error envelopes; **26 passed** with context suite. [Evidence](DIRECT_FALLBACK_EVIDENCE.md).
- Exact-source Ubuntu type result at `3ecb95bbe`: CI run **37030468541**, mypy job **110915704575**, **851 errors / 241 files / 1,240 checked**, baseline812. The three repaired vault/bootstrap files have no diagnostics; reduction859→851 is measured, not inferred. This is still a failing non-blocking ratchet. Native CI **37030468564 passed**; required overall/backend workflow still in progress when queried. Generic Python SDK HTTP step passed remotely.
- New native candidate will be **2026.9.27 / 2026100201**, after committing frozen source and restaging its exact backend. A/B accessibility probe identified another explicit-label SIGSEGV; AppKit selectable text passed actual AX/copy. This remains narrow chat/recovery/rich scope, not app-wide172 remaining selectable expressions in28 feature files. New full-bundle acceptance has not yet run.
- Next read-only audits: CORE-04 whole-turn WebSocket terminal/cancel contract and DEV-01B Python/Node session/auth/deadline parity. Existing SDKs mis-handle greeting/session identity and per-round finality; gateway abort reports success without cancelling. Do not implement consumer success against a fabricated task-terminal marker. Coordinate protocol before the next source wave.

- Native source is frozen. Production typecheck and actual AppKit probe copy,
  Markdown/link/code behavior passed. Parent registered SelectableText in the
  default native CI fixture inventory; initial Bash empty-array failure was
  corrected, final component+linked20/desktop 39/error 5 checks passed. [Evidence](NATIVE_ACCESSIBILITY_EVIDENCE.md).
- CORE04 audit reproduced queued-turn cancellation tearing down an active
  same-session turn's children. Next wave must fix lock ownership before
  claiming exact-turn abort, persist receipts before durable claims and dedupe
  identical requests without replay. SDK must wait for the correlated whole-turn
  terminal, not greetings/intermediate prose/provider-round finality.

## October 2 publication and full-CI fixture correction

Repair wave source **34a43e640597ca721fb915149f29c9b73b51394a** is committed and
pushed. Native9.27 candidate stages that frozen backend; subsequent CORE04/SDK
working-tree edits belong to a later wave and cannot enter this candidate.

Source3ecb95bbe full Ubuntu backend CI completed **1 failed, 12,157 passed,
83 skipped, 570 warnings**, coverage74% rounded. Failure was the existing
`test_unknown_approval_never_reaches_engine` fixture lacking new handle/owner/
registry-index fields. Parent corrected the fixture to match the real managed
session contract, preserving its409 refusal and no-engine-execution assertions.
Final targeted coding-reviewable+REST check: **37 passed, 6 warnings, 3.62s**;
Ruff passed. The failed full run remains failed; new exact-source remote coverage
must pass before closing that gate.

Staging first failed when sandbox blocked OpenCode's normal cache initialization;
properly escalated staging passed pinned Python 3.11.15/SQLite 3.53.1/FTS5/loadable
extensions/module imports/webUIv2 and OpenCode version checks. Initial production
build was blocked by Swift macro sandbox; the escalated build is separate.

## October 2 CORE04 / DEV01B integration

- Frozen production runtime/SDK source passed parent combined **309 tests,
  one pre-existing skip,79 warnings in12.25s**. Command uses explicit disposable
  home/data before collection and includes receipt/abort/WS/gateway/workflow/
  exact approval/coding REST/type-behavior suites. Local log:
  `/private/tmp/feral-turn-sdk-integrated-20261002.log`. Initial command named a
  nonexistent test file and collected nothing; corrected inventory was used.
- Worker receipt suite:226 passed/1 skipped; SDK72 Python/53 Node passed.
  Parent installed locked TypeScript5.9.3 locally, ran exact `npm test`:53 passed.
  Registry access first failed under network sandbox; properly escalated install
  succeeded. New generic Node22 job is distinct from device SDK job; Python job
  now includes HTTP/WS suites.
- Parent focused mypy found one new SDK enum-input type error. Explicit string
  validation repaired it; final SDK72 passed/7 warnings in3.05s and focused
  chat_turns/client mypy passed, noting unchecked untyped function bodies. No
  baseline/ignore changes. Three core files separately passed37 tests and mypy.
- CORE04 persists identity/term digest and processing terminal, not full
  transcript/read/seen or effect success. SDK is authenticated/session-bound
  and negotiates before prompt; neither retry nor approval is automatic.
- Actual9.27 accepted the exact reviewed synthetic write21bytes; denied file
  stayed absent. Latest crash IPS confirms accessibility recursion at New
  conversation. Quit/relaunch preserved rich metadata, reply and approved file.
  Next source repair must be assembled as a new candidate;9.27 is immutable.
- Next owned cards: native accessibility/New conversation repair; MODEL01B
  whole-request context fitting and typed byte refusal; DEV01C bounded live SDK
  thread sockets. Durable context after process shutdown remains a separate
  backend checkpoint gate. All22 product areas remain in REQUEST_COVERAGE.

## October 2 next source wave integration

- MODEL01B fits a request-only view by omitting older whole turns while retaining
  protected input and complete required tool schemas. Saved transcript is not
  rewritten. Byte/schema/context refusals remain typed and cannot trigger an
  unintended cloud/direct fallback. Controlled16-turn normal/stream regressions
  pass; actual native conversation-growth acceptance remains pending.
- DEV01C retains one authenticated socket/reader per exact live SDK thread,
  with bounded channels/retired IDs and no automatic reconnect/replay. Actual
  registered ASGI/provider tests demonstrate preceding turns in context and
  simultaneous A/B isolation. Final worker checks:92 Python/72 Node passed.
  Closing/restarting does not provide durable model history. [Next backend
  checkpoint card](DATA01_CONTEXT_CHECKPOINT_PLAN.md) documents inspected writers,
  privacy and locking gaps; it is a proposal, not implemented durability.
- Parent frozen runtime/SDK/maintenance integration: **330 passed,7 warnings
  in9.52s**, exit0, disposable home/data. Exact selected suite inventory is in
  local `/private/tmp/feral-runtime-sdk-wave2-20261002.log`; Node72 passed with
  locked TypeScript5.9.3. Edited Python Ruff and diff checks pass.
- Identity maintenance now awaits real asynchronous SQLite episode reads.
  Four new real-store cases reproduce and repair the previous TypeError/
  unawaited-coroutine behavior, test exact-session/global reads and preserve
  existing content on failure.51 related memory/receipt tests passed.
- Four attributed CORE04 receipt-store/gateway type diagnostics are repaired without
  raising the ratchet. Focused checks still find pre-existing store/workspace
  diagnostics; new-source Ubuntu count remains separate. Exact canonical UUID
  gateway validation preserves missing/invalid identifier refusal;21 abort/
  gateway tests pass after this final boundary repair. [Evidence](TYPE_RATCHET_RECEIPT_FOLLOWUP.md).
- Packaged-core equality tool: nine real temporary-Git tests pass; actual9.27
  comparison matches478 committed production Python files. It does not certify
  native compilation, signing, dependency or clean-install provenance.
- Security patch wave covers all nine affected installed dependency copies.
  Web1358/extension19/device SDK39 tests, typecheck/build and asset comparison
  pass. Regenerated web assets are byte-identical. Default-branch alerts remain
  open until that branch receives the fixes. [Evidence](SECURITY_DEPENDENCY_EVIDENCE.md).
- Native172-expression AppKit migration passes production typecheck and final
  selected fixture runner, exit0: component groups plus linked20/desktop 39/
  error 5. First runner failed because the standalone desktop fixture omitted
  the new shared text dependency; parent repaired that compile source list,
  and the rerun passed. A minimal New conversation/error disclosure A/B
  reproduces original SIGSEGV and passes exact AppKit copy with normal exit.
  Package9.28/build2026100202 only after native freeze and source commit;
  compare staged backend to that full SHA before actual GUI acceptance.
- All original22 requirement areas remain tracked. No personal data reset,
  real account/message/purchase, merge or release occurred.
