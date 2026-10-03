# FERAL completion checkpoint

Updated October 2, 2026. Parent/integrator owns this file. Reconcile it with actual
Git, CI and processes after resuming; it is a checkpoint, not a live process lock.
See [resume procedure](RESUME_WORK.md), [execution plan](EXECUTION_PLAN.md) and
[all user requirements](REQUEST_COVERAGE.md).

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
| Current checkpoint | Published core `90b75587a` and SDK `205f468a2`; CI/fixture correction `407d158b2` committed locally; next native9.30 integration and publication underway |
| Prior correction | `2754ce677`: wait for stale-heart-rate metadata without weakening assertions |
| Unrelated local edit | `AUDIT-FIXES.md`; preserve and exclude unless separately reviewed |
| Disk observation | Parent latest df reports8.0GiB free. Availability fluctuates; inspect before heavy builds. No disk-caused crash or personal/cache cleanup established |

The checkpoint's own commit cannot name its future hash. Read current HEAD from
Git/helper; evidence below explicitly names the source it tested. Do not update
every historic source identifier to HEAD or regenerate a baseline to make a gate
appear green.

## Latest checked source and active candidate

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
265Mach-O/9internal links, Python3.11.15/SQLite3.53.1/FTS5/OpenCode1.18.10.
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
The required local full-suite recheck is running with explicit50% coverage floor,
PR performance exclusions and a read-only descriptor/mock observer;1261 frozen
core files have digest39a8b055528037f4a4652d67f9372a32c4871b3fed10791be3333ae14ca7d1d8.
See [recheck evidence](FULL_BACKEND_RECHECK_20261002.md).
Parent owns source freeze, shared docs, explicit-path
publication and candidate identity. Preserved AUDIT-FIXES.md stays excluded;
historic failed candidate evidence stays intact. Next: publish coherent corrections
and native integration, verify corrected-source remote CI and local full suite,
then assemble/audit an exact-source9.30 candidate for actual isolated acceptance.

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
files/265 Mach-O/9 internal links, pinned Python3.11.15/SQLite3.53.1/FTS5 and
OpenCode1.18.10. Ad-hoc verification is not Developer-ID/notarization.
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
native fixture runner passed all36 feature suites, linked25 groups, desktop39
and error5 assertions. Production typecheck and18 child-lifeline/auditor/source-
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
Python3.11.15/SQLite3.53.1/FTS5/OpenCode1.18.10. Ad-hoc signing passes;
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

## Current parallel wave and ownership

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
  corrected, final component+linked20/desktop39/error5 checks passed. [Evidence](NATIVE_ACCESSIBILITY_EVIDENCE.md).
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
properly escalated staging passed pinned Python3.11.15/SQLite3.53.1/FTS5/loadable
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
  selected fixture runner, exit0: component groups plus linked20/desktop39/
  error5. First runner failed because the standalone desktop fixture omitted
  the new shared text dependency; parent repaired that compile source list,
  and the rerun passed. A minimal New conversation/error disclosure A/B
  reproduces original SIGSEGV and passes exact AppKit copy with normal exit.
  Package9.28/build2026100202 only after native freeze and source commit;
  compare staged backend to that full SHA before actual GUI acceptance.
- All original22 requirement areas remain tracked. No personal data reset,
  real account/message/purchase, merge or release occurred.
