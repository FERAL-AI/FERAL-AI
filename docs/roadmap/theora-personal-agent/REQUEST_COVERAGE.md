# User request coverage and next implementation work

**Current checkpoint (October 5).** Immutable 9.44 contains the verified local
readiness/task-status and preceding reliability waves. The next exact approval
and selected vision source passes 2,724 frozen integration checks and native
fixture/typecheck gates; fresh 9.45 packaging is next. Actual new native GUI,
physical voice, model/account and distribution acceptance remain separate.
[Current state](WORK_STATE.md), [new behavior](TASK_APPROVAL_AND_SELECTED_VISION_EVIDENCE.md),
[immutable artifact](NATIVE_9_44_ACCEPTANCE.md).

Reconciled October 4, 2026 against product requirements, existing source inventories and dated acceptance records. This tracks the full product. A source implementation, isolated fixture, actual task outcome and production release are separate evidence classes.

The detailed contracts and acceptance work packages remain in [release readiness](RELEASE_READINESS.md), the [native inventory](../../../desktop-native/FEATURE_PARITY.md) and the [iOS handoff](IOS_AGENT_HANDOFF.md). The implementation branch is published in [draft PR #310](https://github.com/FERAL-AI/FERAL-AI/pull/310); publication is not a release or a passing acceptance matrix.

## Current verified boundary

Actual 9.40/source 250787b2d passes bounded fresh-profile local-provider probing,
existing-Chrome connection/selection, request-only native approval, synthetic page
outcome, Stop/disconnect and normal Quit. Earlier 9.36 local chat/recall and 9.38
markers remain separate historical evidence. Whole-desktop control source already
exists; its complete packaged/model-driven acceptance is not established.
[Historical GUI acceptance](NATIVE_9_40_ACCEPTANCE.md),
[current artifact](NATIVE_9_44_ACCEPTANCE.md), [runtime review](RUNTIME_REVIEW_20261004.md)
and [WORK_STATE](WORK_STATE.md) distinguish the evidence and remaining gates.

## October4 additions and integration coverage

| Requirement | Existing foundation and next card |
|---|---|
| Conversation while agents work on several tasks | TaskFlow/turns/multi-agent; TASK-01 durable job ownership and bounded scheduling, VOICE-02 delegation |
| Subscription/API/local choice | Shared catalog/config and existing adapters; AUTH-01 supported ChatGPT plan registration; Claude SDK eligibility separate |
| Fast reliable browser/computer/screen sharing | Existing CDP/Playwright/AX/GUI; [native operator browser view](BROWSER_VIEW_EVIDENCE.md); RESOURCE-01 independent task targets/foreground ownership and desktop preview remain |
| Clean local text/vision/audio model install | Ollama/vision/STT/TTS adapters; INSTALL-01 reviewed compatible runtime/download/capability/first-response verification |
| Three product images/options in iMessage | Existing channels and preview commerce; MSG-01 receive/dedup/offer fallback, BUY-01 exact quote/Link/merchant verification |
| CLI/web/native settings parity and two–three steps | Same catalog/config, deliberately separate default profiles; SETUP-01 string model DTO and endpoint/fallback repair |
| Mac/phone/glasses voice sync, interruptions/noise | Managed utterance/receipt/HUP foundation; VOICE-02 acoustic/audio ownership, IOS-02 correlated frame and offline handoff |
| Affordable persistent/proactive operation | Existing proactive switches/cost/cooldowns; PROACTIVE-02 event-driven bounded inference; Mac sleep is unavailable local execution |
| Scoped memory and opt-in CLI agent oversight | Existing checkpoints/episodes/sync/ACP; CODE-02 provenance/grants/redaction and reviewed standalone adapters |

Detailed source, current primary links, executed versus documentation evidence,
blockers and acceptance for every row are in
[MULTITASKING_AND_EASY_SETUP_PLAN](MULTITASKING_AND_EASY_SETUP_PLAN.md).
Earlier avatar, social/multiplayer, health, memory, developer extension and
open-source release requirements below remain in scope. New model/API availability
is not proof of a working FERAL adapter.

### Historical evidence

- Latest publication is `4eded179e2060c226739511fc53f5a8bde0eaa16`.
  General CI passes13,013 backend tests/83 skipped/75.24% coverage and web/SDK/type
  gates. Native Process-fixture timeout and Linux extracted-payload check remain
  failed. Native9.34 is built with source/hash/signature checks and its actual
  offline backup/restore/readers journey passes. Local-model recovery is active;
  GUI/audio and release qualification remain open.
  [9.34 evidence](NATIVE_9_34_ACCEPTANCE.md). The older bullets below describe the
  preceding publication/freeze, not the current package state.

- Published `ec1c301fade5301fa604b29333612f5fa094b075`: backend12,871/83 skipped/
  75.10%, web/native/mypy808 count gates and generated-asset coherence pass.
  All executed jobs succeeded; opt-in/main-only jobs were skipped.
- Actual frozen7f browser run passes61 tests with unchanged source inventory,
  loaded-module provenance and clean shutdown. Bounded exclusions remain explicit
  in [browser evidence](LIVE_WEB_TRACKED_ACCEPTANCE_20261003.md). Exact7f packaged
  9.33 recovery/restart also passes and the artifact is preserved.
- Frozen attempt-v1/preference/archive/layout integration passes455 backend tests
  including the registered voice writer-lock/control-starvation repair. Typing
  stays808 with1,284 unchanged inputs; native archive/preference/layout passes
  280/71/87 assertions and genuine Process ownership28. Linux selector/smoke unit
  checks pass20; actual package CI is pending. Publication and exact-source9.34
  assembly are next. Archive UI/NativeModel host and actual microphone/speaker
  behavior remain open. Managed saved-context voice remains refused pending its
  tracked-turn/checkpoint adapter. Current GUI/audio remains blocked by Computer Use.
- Gen-UI expansion is deferred. Additional coding-engine expansion follows
  dependable cloud/local setup, recovery, voice and data continuity. Existing
  runtime, memory, tools and developer interfaces remain the foundation.

### Earlier checkpoint records

- Published `86a8b54ed` includes reviewed cloud setup, bounded offline archives and
  explicit saved-context recovery with a native durable journal. Local combined
  backend: 570 passed. Remote native checks and the unchanged type ratchet pass
  (remote808 exactly matches frozen local against baseline812). Prior55a99 full
  backend passed12,783/83 skipped/74.93%. Exact7f backend failed3 fixtures/one setup
  race;12,867 tests passed/83 skipped/75.10%. Corrections pass132 plus ambient16
  tests. Exact86 backend passes12,871/83 skipped/75.10%, native/type checks pass;
  web failed after1,358 passing tests with a post-unmount hydration callback.
  Its correction passes201 shell tests; new-head CI remains required.
  [Hydration evidence](WEB_HYDRATION_LIFECYCLE_EVIDENCE.md).
  Current startup/engine/recognition
  repairs pass their targeted integration and native checks; frozen typing808 has
  zero added/three removed versus55a99. New-head CI remains required. See
  [startup evidence](VOICE_STARTUP_OWNERSHIP_EVIDENCE.md).
- Exact61550e7-source 9.32 passed assembly/signature/runtime/source comparison and the
  actual packaged-backend Stop/recovery/real local-model recall/restart journey.
  Zero recorded tool executions, no cancelled task replay, exact foreign/stale
  refusal and clean owned shutdown were verified in disposable profiles.
  Current GUI, signed fresh-cloud operation, migration and real voice acceptance
  remain open. See [completion evidence](COMPLETION_WAVE_EVIDENCE.md) and
  [9.32 acceptance](NATIVE_9_32_ACCEPTANCE.md).
- Exact7f-source9.33 also passes its actual packaged Stop/recovery/model recall/
  restart/recall journey and all485 post-run source/hash/signature checks. Prior
  timeouts remain preserved. [9.33 acceptance](NATIVE_9_33_ACCEPTANCE.md).
  Focused Chat/keyboard4-test actual browser run passed; full61 run remains active.
  Voice attempt-v1 and portable native preference/archive integration are in
  progress and are excluded from those immutable candidate results.

### Preceding integration checkpoints

- Latest integrated core `a320f54bf` passed1003 controlled backend tests.
  Full local mypy818 versus812 still fails;27 diagnostics removed/zero added
  compared with the prior published log. Exact-source d2158fc6a required CI/native
  passed:12,654 backend tests/83 skipped/74.77% coverage; remote mypy remains818.
  See
  [integration evidence](INTEGRATION_RECHECK_20261002.md).
- Published source `dd69c7bf5` passed required CI/native checks:12,543 backend
  tests/83 skipped,74.66% coverage. Local full backend passed12,576 tests/50 skipped,
 75.03% coverage. Source identities and exclusions are recorded in the
  [full recheck](FULL_BACKEND_RECHECK_20261002.md). Mypy845 versus812 remains failed.
- Packaged9.30 contains that exact source. Audit and strict ad-hoc signature
  verification passed. Actual bundled-backend local-model replies, original-context
  recall after restart and cancellation/status/no-replay passed in disposable homes.
  Current native GUI acceptance is not run because the control tool cannot initialize.
  See [9.30 evidence](NATIVE_9_30_ACCEPTANCE.md).
- Earlier9.29 passed bounded real GUI Security layout, rich Copy, tracked local
  replies, Stop and Quit/reopen/status reconciliation. That earlier artifact remains
  distinct from9.30. Distribution, migration, account/device and full-feature gates
  remain open. Registry, strict-approval and type repairs are now integrated in
  the new source above; the earlier full-suite result does not cover them.

### Earlier October 2 evidence

- Sourcecd1571ef9 required CI/native passed: **12,369 backend tests,
  83 skipped, coverage74% rounded**. Non-blocking mypy remains failed at846
  versus812 baseline. Live-brain and main Linux matrix skipped.
- [Actual9.27](NATIVE_9_27_ACCEPTANCE.md) passed local reply, exact native
  reviewed Deny/Allow and copy/link. New conversation caused a confirmed
  accessibility recursion crash; context growth blocks later turns. Neither
  daily-use gate is closed.
- New [receipt](CHAT_TURN_RECEIPTS_EVIDENCE.md) and
  [generic SDK](SDK_WEBSOCKET_EVIDENCE.md) source slices passed integrated309
  tests/1 skipped and locked Node53 tests. They add durable processing status,
  exact cancellation and compatibility negotiation before tasks; no read/seen
  or external-effect success claim. DEV01C repairs live SDK thread continuity;
  closed/restarted model context remains a separate checkpoint card. Current
  runtime/SDK integration passed330 tests plus72 Node tests; that source now
  passed remote CI. Actual9.28 passed the old New conversation crash path and
  a local reply, but reproduced a separate Permissions and Cost SIGTRAP,
  an unexplained normal exit and early timeout. App acceptance remains open.
- Next frozen integration passed235 backend tests and all36 native feature
  suites, with linked25 groups and desktop 39/error 5 assertions. Exact native
  task status/Stop, layout repair, Forge drafting and structured intervals are
  implemented. Trusted checkpoint lifecycle passes restart/no-replay tests but
  stays inactive until every writer/cleanup is covered.9.29 is the next assembly,
  not yet an accepted app. [Evidence](NATIVE_CONTEXT_INTEGRATION_EVIDENCE.md).

The following bullets retain earlier evidence; their counts and artifacts do
not describe this new source wave.

- Native macOS 2026.9.26 actually launched to avatar onboarding, quit normally and relaunched after both preferences initialization traps were removed. Earlier 9.23/9.24 launch failures and the second 9.25 trap remain recorded. This establishes bounded startup, not all existing-profile or feature behavior.
- Earlier bounded real Mac checks established a local reply, avatar/name/conversation persistence, selected import/read workflows and one exact reviewed OpenCode command. They do not establish arbitrary coding, external accounts, physical glasses or live purchasing.
- Source `6b368ccf7` required CI and native checks passed remotely: **12,127 backend tests / 83 skipped / 73.94% coverage**, corrected web build/coverage and API-stubbed Playwright. The Linux main-branch matrix and live-brain check were skipped. Non-blocking mypy failed at **859 errors versus baseline 812**; precise vault/bootstrap repairs are locally verified, with full Ubuntu ratchet acceptance pending.
- [Actual populated 9.26 acceptance](NATIVE_POPULATED_ACCEPTANCE.md) preserved rich transcripts, conversation search/rename/pin and a local-model reply. It reproduced an accessibility recursion crash, context truncation, unrelated tool fallback and false completion for a pending action; these are defects being repaired, not passed release gates.
- Coding continuity now binds trusted session identity, isolates same-workspace handles and scopes REST controls (**162 local tests**). Generic Python SDK HTTP routes/auth/error handling are repaired (**25 local tests**) and wired into CI; SDK WebSocket/Node follow-on contracts remain open. See [coding evidence](CODING_SESSION_EVIDENCE.md) and [SDK evidence](PYTHON_SDK_EVIDENCE.md).
- Shared repository rules now exist in [AGENTS.md](../../../AGENTS.md), [codex.md](../../../codex.md) and [CLAUDE.md](../../../CLAUDE.md). Native build/fixture checks are in CI. Packaging, signing and actual platform acceptance remain separate gates.
- The [execution plan](EXECUTION_PLAN.md) separates native parity of the substantial existing system from new feature integration. Existing sessions, memory, TaskFlow, ACP engines and SDKs are reused. Multi-account hosting/social discovery are not prerequisites for packaging the supported single-user local system.

## Every requested product area

| User requirement | Existing evidence and status | Remaining acceptance or deliverable |
|---|---|---|
| Full personal agent inside existing FERAL | **Partial.** Runtime, orchestration, memory, policy, headless contracts and multiple clients exist. Native replacement remains a preview with partial capability coverage. | Complete supported journeys and declare enabled capabilities only after their gates pass. Preserve existing clients and data during migration. |
| Solid native Mac app, clean intuitive UI, feature preservation | **Partial.** SwiftUI/AppKit, avatar-first setup, bounded actual startup, selected real task checks; chat diagnostics and bounded error details implemented. All native inventory rows remain uncertified. | Verify populated old transcripts, composer/navigation, accessibility, permissions, errors, cancellation, recovery and all retained actions. Resolve each inventory omission before replacing the full installed app. |
| Installable Linux app with equivalent supported power | **Unverified release.** Existing Linux/backend/web/Tauri foundations; SwiftUI candidate is Mac-only. | Choose and test supported distributions/architectures, package install/update/rollback, X11/Wayland audio/tray/shortcuts and the declared Linux feature matrix. |
| Native iOS app using the same agent | **Partial; iOS agent owns implementation.** Existing Theora SwiftUI app and FERAL relay/outbox bridges. Two conversation paths and incomplete protocol handling remain. | Execute IOS-01 onward: clean iOS build, owner lifecycle, one durable event record, exact voice/camera correlation, background/device acceptance and shared action reviews. |
| FERAL logo and user-selected avatar at first launch | **Bounded Mac verified.** Supplied logo retained, icons packaged; bundled companion, orb and imported-photo choice exist; actual avatar onboarding observed. | Persistent choice through upgrades and supported same-owner sync; catalog rights, import limits, state-driven animation, static/reduced-motion/accessibility and iOS rendering. Logo and avatar remain distinct. |
| Text like two people: sent/delivered/read/seen and rich conversation | **Requested, not established.** Text/history and rich event handling exist; no verified read/seen contract. | Define durable acceptance, presentation/read evidence, typing/thinking and task progress separately. Never call a queued task completed or claim human-like seeing without a corresponding event. Test duplicate/reordered/reconnected messages. |
| Calls and natural interruptible voice while tasks run | **Partial source.** Voice adapters, microphone/playback/transcripts and review configuration exist. Real latest-native audio and multitasking acceptance remain open. | One audio coordinator, exact turn/stream ownership, barge-in, call controls, task progress during conversation, model fallback, cancellation, budgets and real latency/audio tests. |
| Multiple simultaneous FERAL sessions sharing the owner's memory | **Partial foundation.** Separate app/chat scopes, external coding sessions and shared brain memory exist. This is not comprehensive consumer ownership or session isolation. | Concurrent sessions must preserve turn/action identity, same-owner provenance and conflict rules; another session cannot inherit an unrelated approval. Define persona versus session versus device semantics and test concurrent cancel/reconnect. |
| Shared memory across the user's life and devices | **Partial.** SQLite, knowledge/wiki imports, activity episodes and scoped peer sync exist; selected reads/imports verified. | Owner partitions, source/confidence/correction, incremental same-owner sync, tombstones, full export/delete/restore, vector migration and durable offline reconciliation. Health observations, private facts and execution receipts remain separate records. |
| FERAL users communicate and collaborate; multiplayer | **Partial primitives only.** Peer roster and consent-scoped federation exist. No complete consumer multiplayer experience verified. | Recipient identity, invitations, shared task ownership, exact shared fields, permissions, expiry/revocation, audit and adversarial cross-owner isolation. Preserve private memory by default. |
| Friends or glasses wearers match on the street | **Requested, not built/accepted.** General social sharing contracts do not establish discovery or matching. | Define opt-in discoverability, friendship/consent, bounded proximity/location exchange, blocking, expiry and device/background behavior. No automatic stranger identification or ambient/private-health disclosure. |
| Glasses camera/voice: look at an item, say get me that, continue on phone/iMessage | **Partial transport.** HUP node/session paths and Theora camera/voice relay sources exist. Synthetic hardware checks are distinct from physical results. | Exact selected camera-device authority, request correlation/freshness, visual grounding, durable task handoff and real camera/voice/background tests. Health-temple glasses and Theora Eye camera/microphone glasses are distinct hardware. |
| iMessage conversation with FERAL as a person | **Partial send primitive; research complete.** Messages.app AppleScript endpoint exists; no end-to-end incoming bridge or external delivery acceptance. | Research recommends version-gated native imsg with optional BlueBubbles, then owner/account/recipient binding, idempotent ingress and truthful delivery. Basic workflows preserve SIP; Linux/iOS restrictions remain explicit. See [report](INSTINCT_RESEARCH.md). |
| Browse, book, buy and receive confirmations using Google accounts | **Partial implementation; browser research complete.** CDP plus Playwright controller, OAuth lifecycle and general integration surfaces exist. Current purchase helper is explicitly a read-only preview. | Retain the existing executor, evaluate upstream Browser Use only as an optional planner, then test supported account scopes, real booking/account journeys, exact action review and authoritative receipts. CAPTCHAs, MFA and unsupported merchants need useful handoff states. |
| Stripe Link for Agents approval and one-time card, each OSS user's wallet | **Research complete; adapter open.** Primary sources distinguish consumer CLI wallet access from confidential branded OAuth registration. No wallet connection/payment tested. | Follow [report](INSTINCT_RESEARCH.md); verify pinned schemas/eligibility/expiry in test mode, then quote-bound approval, idempotency, secret handling, unknown-outcome reconciliation and receipts. |
| Reuse OpenCode for coding from chat and CLI | **Partial with bounded actual success.** Bundled engine, external-agent skill, continuation/activity memory and native review controls; one exact command verified. | End-to-end chat and CLI parity, concrete workspace/action reviews, cancel/restart/reattach, truthful failed/empty outcomes, project tests and retained activity. ACP coding transport is distinct from commerce ACP. |
| Oversee what Codex, Claude and other CLI tools have worked on | **Partial for managed agents.** external_agent routes managed OpenCode/Claude/Codex activity into memory episodes. This does not prove capture of arbitrary independently launched terminals. | Consent-scoped managed-agent adapters or explicit CLI wrappers/event import; identify repo/session/owner and source reliability, redact secrets, link result evidence and avoid silently reading every terminal. Test concurrent agent provenance and missing/crashed emitters. |
| ChatGPT sign-in plus cloud-provider and local-model choice | **Partial source/local acceptance.** Codex app-server provider uses the operator's existing codex login rather than extracting tokens. Local/provider setup paths exist; bounded local reply verified. | Actual supported sign-in/expiry/disconnect and provider activation/inference acceptance; clear distinction between Codex sign-in, OpenAI API keys and realtime voice eligibility. Optional model download needs capacity/preflight and truthful missing-model state. |
| Include recently introduced models, use excellent voice and proactively help | **Partial.** Current OpenAI catalog/routing corrections and policy-tested proactive scene dispatch exist. GPT Live metadata is informational while unsupported legacy routing is refused. | Provider-specific capability/eval matrix and real configured inference/audio tests; proper voice adapter rather than catalog-only enablement. Proactivity needs freshness, quiet hours, dismissal, budgets and a consistent review path for every entry point. |
| Learn from ChatGPT, Dot/Dots, Meta and Instinct; strong differentiated PMF | **Research/planning.** Product/experience docs exist; user explicitly identified instinct.com. No source review proves product-market fit or superiority. | Complete exact outstanding references and requested five-part report. Measure recurring benefit, retained usage and payment willingness; test glasses-added value and task success. A cafe demo can be an acceptance scenario, never the release objective. |
| Systematic parallel work, documentation, rules and existing GitHub publication | **Implemented workflow foundation.** Worker ownership rules, public roadmap, native inventory, rules and PR #310 exist. | Keep a single coverage tracker, evidence ledger and dependency backlog updated with each change; run relevant CI and actual acceptance, publish coherent reviewed commits, preserve failure evidence and avoid conflicting file ownership. |
| Open source people control how they use it; install/upgrade securely | **Partial.** Local-first code/provider/autonomy controls exist. Ad-hoc Mac bundle is not consumer distribution. | Explicit inspectable/revocable autonomy and deployment choices, credential isolation, license/dependency review, clean-machine signed install/update/rollback, backups and supported-platform truth. |
| Tested reality, no assumptions, no preview/demo substituted for completion | **Ongoing, incomplete.** Actual startup and bounded task evidence exist; full-suite failures are documented. | Every enabled capability gets source identity, fixture and real outcome evidence. Keep physical/account/merchant/signing gates visibly unverified until executed. Correct tests and product defects without weakening gates. |

## Completed research deliverable and implementation boundary

The [Instinct-style report](INSTINCT_RESEARCH.md) now supplies verdicts for iMessage bridge, stripe/link-cli, Browser Use versus Playwright, existing glasses trigger and exact FERAL module integration. It separates source inspection and 89 isolated passing fixtures from docs-only eligibility/transport claims and untested real accounts/hardware. It lists blockers, risks, steps and the requested cafe acceptance scenario. Existing CDP/Playwright source is not proof of real-account automation.

The original research request said to stop after the report and wait for go. Later authorization to build the full app does not create payment credentials, authorize external messages or waive real-account tests; make the research findings reviewable before enabling those new external effects.

## Next three implementation priorities

Gen-UI expansion is deferred. Preserve existing functionality while prioritizing
fresh local/cloud setup, usable recovery, voice and data continuity. Additional
coding-engine expansion follows these daily app journeys.

1. **Finish the existing app.** Close corrected-source CI, latest populated-profile native acceptance, genuine parity omissions, safe migration, fresh credentials, clean installation and tested developer extension setup. The existing local runtime does not need a new multi-account service first.
2. **Extend the existing contracts.** Reuse sessions/conversations/memory/TaskFlow/ACP; propagate coding conversation identity, add durable receipts, harden workflow authorization/recovery, unify voice/task activity and exact-device handoff. These changes support the newer requests without replacing working infrastructure.
3. **Integrate the new channels and capabilities.** Supply executable iOS contracts; follow the completed research checkpoint for messaging/browser/Link adapters. Social discovery and Linux distribution have independent owned task cards. See [the full execution plan](EXECUTION_PLAN.md) for every dependency and exit test.

Avatar/catalog refinement, Linux packaging investigations and PMF research can proceed with distinct ownership in parallel. Social matching, Link checkout and arbitrary CLI observation remain explicit backlog items rather than implied capability. Production signing, real accounts, named merchants and physical device acceptance still require their own concrete access and evidence.
