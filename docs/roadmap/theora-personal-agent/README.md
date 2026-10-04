# Theora personal agent: product and engineering plan

**Current implementation checkpoint: October 3, 2026.** Published backend source
`c8651d16bff8dd633587c999fd908084f65f66c2` repairs foreground terminal commitment
and configured chat output allowances; 623 isolated integration checks pass.
The immutable 9.35 app contains the preceding implementation `12cc62c4`, exposes
21 destinations and passes actual avatar selection and single-stage provider
setup. Its whole-turn chat acceptance failed; later source fixes are not yet in
that app. Native passive-health recovery is being integrated before the next
build. Storage cleanup preserved useful history and recovered disk headroom.
Mac acceptance leads; Linux expansion and Gen-UI are deferred. Start with
[current work](WORK_STATE.md), [completion plan](EXECUTION_PLAN.md),
[request coverage](REQUEST_COVERAGE.md), [release readiness](RELEASE_READINESS.md)
and [native inventory](../../../desktop-native/FEATURE_PARITY.md).

Contributor principles live in [AGENTS.md](../../../AGENTS.md) and
[codex.md](../../../codex.md). They require exclusive worker ownership,
source-bound evidence, explicit staging, professional public records, protected
user data and bounded artifact retention. The [resume procedure](RESUME_WORK.md)
explains how another session continues this checkout without restarting work.

Prepared September 30, 2026 from FERAL and Theora source inspection, focused verification, and primary-source research. This is a proposed product plan, not a claim that the described capabilities are shipping. No application implementation, publishing, payments, deployments or external messages were performed for this plan.

**Subsequent verification:** the user requested direct testing of core/desktop while leaving iOS with its separate agent. Bounded corrections to reproduced defects were made after this initial plan. See [the verification report](VERIFICATION_REPORT.md) for exact evidence, results and remaining limits; the paragraph above describes the initial planning phase.

Historical verification-phase checks (not a fresh run of the current native product): **11,667 core tests passed (50 skipped), 1,348 client tests passed, 30 real-brain destination checks passed, five selected real-brain control walks passed**. Native macOS compile and production JS builds passed. See the report for overlap, warnings, test fixtures and hardware/platform/payment scenarios that remain unverified.

## What we are building

One personal agent that a person can talk to through their glasses, iPhone, Mac or Linux computer. It remembers useful context with permission, uses quality-labelled health observations where relevant, carries out explicitly authorized tasks, and reports verified outcomes. The person chooses its name, avatar and voice, can inspect and correct its memory, and can use supported self-hosted or hosted deployments. The native Theora iPhone app and FERAL desktop are clients of the same agent rather than separate assistants with occasionally exchanged summaries.

Suggested first promise: **Remember what matters and help me follow through.** The platform can eventually cover shopping and more integrations; the first product must demonstrate a recurring benefit before expanding its claims.

The candidate advantage is the complete loop from real-world context to grounded memory, approved action and verified outcome. Memory, agent tools, avatars and biometric coaching individually have competitors. Product-market fit and a claim that no company has solved a problem cannot be established by repository review or public research.

## Read this plan

| Document | Purpose |
|---|---|
| [Current work checkpoint](WORK_STATE.md) | Tested source/candidate, current evidence, worker ownership, next ready cards and external dependencies |
| [Contributor rules](../../../AGENTS.md) | Authorization, privacy, verification, worker ownership, resource retention and clean Git publication |
| [Codex continuation rules](../../../codex.md) | Checkpoint cadence, source/candidate distinctions and resuming the existing project |
| [Latest native candidate](NATIVE_9_35_ACCEPTANCE.md) | Exact packaged source, actual setup success, failed whole-turn chat and preserved resource evidence |
| [Chat completion and output allowance](MAC_CHAT_TERMINAL_BUDGET_EVIDENCE.md) | Published backend repairs, 623 integration checks and remaining rebuilt-app acceptance |
| [Managed chained voice integration](MANAGED_CHAINED_VOICE_INTEGRATION_EVIDENCE.md) | Durable voice/chat ownership and source fixtures; actual audio remains separate |
| [Single-stage Mac onboarding](MAC_SINGLE_STAGE_ONBOARDING_EVIDENCE.md) | Shared reviewed provider setup and prevention of duplicate onboarding |
| [Native action admission](NATIVE_ACTION_ADMISSION_EVIDENCE.md) | Retained HTTP client ownership, uncertain effects and refusal after runtime replacement |
| [Profile continuity](NATIVE_PROFILE_ARCHIVE_EVIDENCE.md) | Reviewed archive/restore, runtime quiescence and data preservation |
| [Previous native candidate](NATIVE_9_34_ACCEPTANCE.md) | Preserved source-specific archive/readers acceptance and historical limitations |
| [Coding session evidence](CODING_SESSION_EVIDENCE.md) | Trusted caller continuity, exact handle ownership, local REST propagation and actual ACP-pipe regression results |
| [Python SDK HTTP evidence](PYTHON_SDK_EVIDENCE.md) | Registered routes/authentication/policy tests, transport failures and developer-facing failure contracts |
| [Whole-turn receipts](CHAT_TURN_RECEIPTS_EVIDENCE.md) | Durable exact-turn identity/status, duplicate requests, capability negotiation and cancellation isolation |
| [Python/Node SDK transport](SDK_WEBSOCKET_EVIDENCE.md) | Authenticated session-bound chat, total deadlines and exact terminal receipts; legacy refusal before task submission |
| [Live SDK thread continuity](SDK_THREAD_CONTINUITY_EVIDENCE.md) | Retained authenticated sockets, actual provider context across turns and explicit context loss after closure |
| [Durable model context next card](DATA01_CONTEXT_CHECKPOINT_PLAN.md) | Source-inspected checkpoint design, stable session locks, privacy boundaries and proposed restart/race acceptance |
| [Local request context fitting](MODEL_CONTEXT_EVIDENCE.md) | Whole-turn wire views, complete protected input and tool schemas, typed refusal without unintended fallback |
| [Dependency security evidence](SECURITY_DEPENDENCY_EVIDENCE.md) | Nine affected installed copies patched; web, extension and device SDK tests/builds; default-branch alerts remain separate |
| [Core type repairs](TYPE_RATCHET_CORE_EVIDENCE.md) | Five attributed core diagnostics, bounded keyword changes and focused evidence |
| [Receipt type follow-up](TYPE_RATCHET_RECEIPT_FOLLOWUP.md) | Four attributed storage/gateway diagnostics, unchanged strict identity validation and remaining ratchet |
| [Identity memory maintenance](MEMORY_MAINTENANCE_EVIDENCE.md) | Actual asynchronous SQLite reads, scoped maintenance and failure preservation |
| [Packaged runtime equality](PACKAGED_SOURCE_EVIDENCE.md) | Repeatable frozen-commit comparison, stale/missing/extra source failures and bounded tests |
| [Native9.27 acceptance](NATIVE_9_27_ACCEPTANCE.md) | Actual packaged action outcomes, persistence, new-conversation crash and daily-use context blocker |
| [Type-ratchet evidence](TYPE_RATCHET_EVIDENCE.md) | Eight source-attributed security annotations, focused checks and remaining Ubuntu ratchet acceptance |
| [Populated native acceptance](NATIVE_POPULATED_ACCEPTANCE.md) | Immutable old-candidate GUI results, confirmed accessibility crash and task defects requiring repair |
| [Native accessibility evidence](NATIVE_ACCESSIBILITY_EVIDENCE.md) | AppKit replacement reproducer, actual copy/link checks and remaining app-wide selectable-text coverage |
| [Native accessibility follow-up](NATIVE_ACCESSIBILITY_FOLLOWUP.md) | New conversation crash reproduction and systematic172-expression AppKit migration; packaged acceptance separate |
| [Native automation next card](NATIVE_AUTOMATION_CONTRACT_PLAN.md) | Confirmed existing scheduled-automation routes, missing native controls and backend Run Now dependency |
| [Ubuntu type follow-up](TYPE_RATCHET_FOLLOWUP.md) | Measured851-error remaining ratchet and source-attributed next repairs |
| [Local model capacity](LOCAL_MODEL_CONTEXT_EVIDENCE.md) | Selected-runtime context preflight, full request estimates, configured-only fallback and focused evidence |
| [Workflow dispatch evidence](TASKFLOW_DISPATCH_EVIDENCE.md) | One-call exact workflow reviews, conservative interrupted-effect recovery and truthful pending text |
| [Direct fallback evidence](DIRECT_FALLBACK_EVIDENCE.md) | Refuse guessed actions after model failure, preserve ToolRunner authority and handle pending results truthfully |
| [Automatic-review and resume procedure](RESUME_WORK.md) | Verified permission controls, CLI commands, interruption reconciliation and persistent execution cadence |
| [Executable app completion plan](EXECUTION_PLAN.md) | Source-backed task cards, existing-system reuse, dependencies, acceptance, developer platform, parallel worker waves and publication |
| [Instinct-style integration research](INSTINCT_RESEARCH.md) | Five requested verdicts: iMessage, per-user Link wallet, browser executor, selected glasses capture and exact integration map; docs versus tests distinguished |
| [User request coverage](REQUEST_COVERAGE.md) | Full conversation checklist, current status, completed research boundary and next three implementation priorities |
| [Current release readiness](RELEASE_READINESS.md) | Full-product work packages, source gaps, actual evidence, task/memory/social/voice/payment/messaging contracts and CI/distribution gates |
| [Product research](PRODUCT_RESEARCH.md) | Current competition, initial customer hypothesis, interviews, paid pilot and sensor-value experiments |
| [Architecture and contracts](ARCHITECTURE_AND_CONTRACTS.md) | Agent ownership, deployment, durable memory, device sync, actions, voice, health and integrations |
| [Core and commerce research](CORE_COMMERCE_RESEARCH.md) | Existing FERAL evidence, payment-protocol access limits, purchase state machine, relay and desktop work |
| [iOS agent handoff](IOS_AGENT_HANDOFF.md) | Eleven file-specific tickets, dependencies, Apple constraints and acceptance tests |
| [Experience and avatars](EXPERIENCE_AND_AVATARS.md) | Glasses journeys, consumer UI, avatar choice, accessibility and truthful state |
| [Execution and release gates](EXECUTION_AND_RELEASE_GATES.md) | Ordered cross-team backlog, testing, open-source release, operations, costs and decision register |
| [Direct verification](VERIFICATION_REPORT.md) | Reproduced defects, bounded corrections, test evidence and unverified release scenarios |

These documents are linked parts of one plan. The architecture defines proposed shared contracts; the research documents explain evidence and uncertainties; the handoff is intended to be directly usable by the Theora iOS agent.

## Existing foundation and the gaps that matter

FERAL already supplies orchestration, tools, approvals, SQLite memory, voice providers, HUP device communication, peer sharing and a Tauri desktop shell. Theora already supplies a native SwiftUI app, account/backend services, health ingestion, glasses/wristband adapters, audio handling, partial brain pairing and durable ambient-transcript forwarding. Those are substantial foundations.

The remaining work changes their relationship. There are presently two conversation/memory paths on iPhone, no comprehensive owner-isolated consumer identity model, and incomplete native protocol handling. Same-owner private continuity is different from FERAL's existing scoped sharing between brains. The current purchase helper produces a preview, not a completed transaction. The relay needs real-edge validation. Desktop release signing/updating and iOS reproducible builds are incomplete.

Hardware must remain accurately described: current Theora health-temple glasses, Veepoo wristband and Theora Eye camera/microphone glasses expose different capabilities. Future synchronized raw PPG/PTT and additional temple sensing need their own hardware and validation track. A vendor SDK method does not prove a sensor exists or produces validated measurements on a particular unit.

## Recommended sequence

1. Complete native parity and actual acceptance for the existing single-user local runtime, preserve data through migration, and close clean installation/developer extension gates. Do not rebuild existing sessions, memory or coding infrastructure. The detailed dependency order is in [the execution plan](EXECUTION_PLAN.md).
2. Deliver one agent across phone and desktop: grounded conversation memory, glasses voice, durable offline capture, tasks and a reviewable action inbox.
3. Run a narrow paid pilot around conversation commitments and follow-through. Evaluate whether glasses and health context add value beyond phone-only memory.
4. Add shopping search and trustworthy phone checkout handoff. Build and test the transaction engine with a merchant simulator and approved provider sandboxes.
5. Enable completed purchases only for named integrated merchants after approval, idempotency, receipts, reconciliation and support work pass release gates.
6. Expand integrations, constrained delegated purchases, sensors and shared workflows according to measured demand.

Avatar choice can ship during the consumer pilot with a small licensed catalog. It should not delay reliable capture, memory or approvals.

## Working assumptions and decisions still open

The planning default is an adult, US-first pilot among iPhone/Mac glasses wearers with frequent conversations and commitments. Linux support is an explicit engineering cohort, not assumed to have identical customer demand. An optional isolated hosted brain plus supported self-hosting is the recommended availability design; choosing the operator, business model and permitted data flows remains a product decision. Pending user answers may change these defaults.

Other decisions: exact launch countries and intended health claims; supported OS/CPU/device matrix; whether Theora is the product name and FERAL the engine; the first integrations and merchant; hardware redistribution rights; retention defaults; price; account requirements; hosting budget. The user identified the Instinct reference as [instinct.com](https://instinct.com); the requested five-part local integration investigation is now in [the research report](INSTINCT_RESEARCH.md), with documentation eligibility distinct from live acceptance. The historical Dot product and the later requested ChatGPT dots are separate references; the former closure notice does not establish the latter feature status. Current ChatGPT dots and the exact Meta reference remain experience-research tasks.

These are design questions, not permission to enable cloud processing, recording, research training or spending for a user. The actual product must collect those permissions at the relevant moment.

## Verification performed

The focused existing suite for spend caps, wearer approvals, paired-token REST authentication, scoped sharing and relay behavior passed **138 tests** after running with permitted local socket access. The initial sandbox run had 119 passes and localhost-bind failures/errors; the permitted rerun resolved them. Tests used isolated temporary FERAL storage and the hash embedding provider. This verifies existing mocked/local behavior, not live merchant payments, a production relay, a clinical sensor, or a built iPhone/desktop release.

Earlier project exploration passed 161 selected backend/client/website tests. No iOS builds, vendor-device tests or live purchases were performed. Release evidence still required is enumerated in [the release plan](EXECUTION_AND_RELEASE_GATES.md).
