# Runtime review and Mac completion order

Reviewed October 4, 2026. Runtime source: `250787b2d1512d2cef98ff77f8ceda607875ee24`;
starting documentation HEAD: `43c1dcb5b8d530f58fb653969601da40fc3078db`.
Three independent read-only workers reviewed computer control/bundle contents,
routines/budgets/workflows, and voice/memory/agent execution. No runtime edits,
app launches, provider calls, downloads, microphones, account access, messages or
purchases were performed. Documentation integration is separate from implementation.
The existing unrelated AUDIT-FIXES.md edits and immutable 9.40 candidate are retained.
All 13 supplied runtime audit source hashes matched the checkout at review start.

## Computer use verdict

Reuse the existing desktop executors. FERAL already registers `gui_computer_use`,
`macos_ax` and `agentic_computer_use`. These provide pointer/keyboard/screenshots,
semantic Mac accessibility actions, and a vision-driven screenshot/action loop.
The Browser panel displays a selected Chrome tab. It does not replace these
whole-computer primitives or establish that their complete model-driven journeys
work in the shipped app.

The missing pieces include verified packaged dependencies, model/tool capability
negotiation, uniform inner-action policy, target/resource ownership, desktop
preview/takeover controls and actual end-to-end acceptance. A connected provider
or a successful screenshot alone does not establish action readiness.

| Reusable option | Official documentation confirms | FERAL decision and remaining test |
| --- | --- | --- |
| Existing FERAL AX/GUI plus ordinary model tools | The OpenAI computer-use guide permits existing function/MCP UI interfaces | First reuse candidate; preserve central executor, exact approvals and current limits; test on actual Mac |
| OpenAI computer tool or code-execution harness | Local execution environment and screenshots remain application responsibilities | Optional compatible provider adapter; native schemas and result loops need fixtures and real configured acceptance |
| Claude computer toolset | Application executes structured member tools and returns correlated results | Map into shared FERAL policy/execution; do not advertise schema-normalization helpers as a working provider integration |
| Gemini computer use | Browser, mobile and desktop environments are documented | Separate wire/capability adapter; no configured FERAL acceptance in this review |
| Cua Driver | MCP/CLI/SDK Mac accessibility and capture driver; macOS14+ | Evaluate only if it closes measured gaps; pin provenance/license, permissions, minimum OS and lifecycle first. Not installed or adopted |
| Existing CDP/Playwright browser paths | Playwright attaches over CDP; its documentation warns of lower protocol fidelity | Preserve verified raw-CDP selected-tab path; independently test Playwright-dependent paths and bundled dependencies |

Primary pages fetched during this review:
[OpenAI computer use](https://developers.openai.com/api/docs/guides/tools-computer-use),
[Claude computer use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool),
[Gemini computer use](https://ai.google.dev/gemini-api/docs/computer-use),
[Cua Driver](https://cua.ai/docs/cua-driver),
[Playwright CDP](https://playwright.dev/docs/api/class-browsertype#browser-type-connect-over-cdp).
These are documentation confirmations, not FERAL interoperability or performance tests.
No claim of fastest or best computer-use implementation is established here.

## Finding dispositions

Passing a probe that asserts a defect confirms the defect; it is not a passing
product acceptance test. Fixtures below use real imported methods with inert
boundary collaborators. Neither actual harmful effects nor acoustic performance
were tested.

| Finding | Independent result | Counterevidence and scope | Required correction/acceptance |
| --- | --- | --- | --- |
| CRON-01 | Confirmed: policy exception can reach direct scheduled skill dispatch with empty context | Explicit hard surface/physical deny still blocks. Not all routine branches bypass execution | Fail closed; shared executor with owner/occurrence/action and exact preauthorization; outage/revocation/changed arguments dispatch zero |
| CRON-02 | Confirmed: recorded successful inert occurrence repeats after injected bookkeeping failure and SQLite reopen | Normal completion/reopen does not duplicate; no OS kill or duplicate real charge tested | Unique occurrence/claim/receipt, atomic completion/rearm where possible; unknown effects reconcile without replay |
| COST-01 | Confirmed: two unreserved admissions can exceed the fixture cap | Completed recorded usage persists normally. No paid usage occurred | Persistent atomic reservation/settlement including input/output and retries; account unknown actual usage rather than dropping it |
| RT-01 | Confirmed: same-wire interrupt waits behind 1/3/5-second tool execution in OpenAI and Gemini classes | Client/provider interruption can differ; existing generation fences remain protective | Bounded retained dispatch with responsive intake; cancellation/replacement/publication fencing; acoustic tests separate |
| RT-02 | Confirmed: false/zero/empty data and failure/truncation envelope fields are lost | Reachable legacy callbacks exist; managed realtime refusal must remain | Canonical presence-aware bounded result envelope with provider wire fixtures |
| AG-01 | Confirmed: empty final output or tool-only exhaustion can report success | Real tools may already have executed; no retry just to improve wording | Return incomplete/exhausted separately from completed outcome and retained effects |
| MEM-02 | Confirmed: role-combined assistant first-person text can become a user relation after model failure | Existing committed-turn learners are reachable; not every memory path does this | Carry speaker/message/evidence identity; user facts only from committed authenticated user input |
| MEM-03 | Confirmed method-level latent regression when an available model fails | Current ambient caller passes no model and its guard works | Apply source/role guard on every heuristic fallback; preserve ambient protection |
| MEM-01 | Confirmed: initial AgentWorker context omits current query; fixture retrieval improves when supplied | Existing replay, identity and forced recall remain; 300-token budget alone is not a measured defect | Pass query/scope then evaluate relevance before increasing budgets |
| WF-01 | Confirmed: awaited executing step holds singleton runner; unrelated ready flow waits | Sleep steps yield; serialization may protect shared effect resources | Bounded per-flow claims and resource lanes; independent flows progress without concurrent cart/device effects |
| AG-02 | Confirmed scoped default runner limitation | ToolRunner, multi-agent and ACP/OpenCode are distinct real executing paths | Explicit unavailable state or task-bearing runner/result registration; do not claim all workers are inert |
| Provider capabilities | Confirmed Codex adapter drops dynamic FERAL tools | Chat and managed coding can work; chat readiness does not imply central FERAL-tool readiness | Expose chat/tool/audio/vision/cancel capability flags; use compatible reviewed provider paths |
| Native voice | Confirmed bounded explicit utterance workflow | Native capture does not require sounddevice; playback ACK is device queue evidence | Preserve current workflow while adding separately tested continuous duplex/delegation |
| Bundle dependencies | Confirmed missing optional control/speech distributions | AX/Quartz and raw CDP have separate dependency paths and remain present | Declare actual installed/configured/reachable/inference/tool/voice states; provision only reviewed compatible dependencies |

Additional confirmed computer-control findings:

- CU-01: with a live executor but missing GUI manifest, actual `_execute_gated`
  dispatches directly. A registered manifest routes through the executor, preserves
  session and respects denial. Direct drag skips that inner executor. The shell
  branch checks only its first token; an inert subprocess factory received a full
  command containing an additional shell statement. No command was executed.
  Close these gaps through the existing executor and refuse unresolved authority.
- CU-02: the immutable bundle lacks PyAutoGUI/Pyperclip used by canonical pointer,
  key and Unicode typing methods. AX/Quartz and raw-CDP controls are separate.
  Presence of source cannot certify these missing-dependency paths.
- CU-03: the generic vision loop requires cloud key presence even for a prospective
  local provider; native vendor computer-tool protocol handling is incomplete.
  Reuse compatible ordinary function tools first, with truthful capability state.
- BUY-02: two inert commerce evaluations can both admit $60 against a $100 cap
  before completed spend is recorded. This is a reservation gap, not evidence of
  checkout or a real charge; current purchase helper remains a preview.
- COST-02: actual LLMProvider settlement can swallow a cap-crossing record failure
  after usage has already returned, omitting that usage. Account incurred usage
  even when a cap is exceeded; admission and settlement are distinct.
- CTX-01: background transcript/memory hooks may race an already-started realtime
  response. Source establishes a conditioning timing gap, not a measured wrong
  spoken answer. Preserve asynchronous ownership and test explicit conditioning.
- BATCH-01: single-agent batch publication waits for gather, but tool execution is
  concurrent. Multi-agent workers already publish individual tool results before
  synthesis. This is a responsiveness/design limitation, not proof that every
  task executor is serialized; do not blindly replace gather with unowned tasks.

These dispositions do not authorize real messages, spending, new privacy
permissions or service registration.

## Implementation order and exclusive ownership

The review does not authorize runtime patches. Subsequent implementation should
use small compatible changes with regression failures demonstrated before fixes.
Keep bounded loops, managed realtime refusal, uncertainty stops, physical/surface
denials, tracked-turn authority and current native lifecycle intact.

1. Shared action reliability: CRON-01/02 and computer-loop inner gates. The parent
   owns any shared executor/contracts; one routine worker owns scheduler/server
   routine code and its tests. A computer worker owns GUI/agentic implementations
   and tests, without concurrent edits to shared executor files.
2. Truthful runtime results: RT-01/02 and AG-01, with retained task/epoch/cancel
   proofs. A voice worker owns provider receive loops; ToolRunner edits integrate
   sequentially with shared dispatch changes.
3. Budget and memory correctness: atomic cost/spend reservation and source-aware
   learning. Assign distinct cost/commerce and learner/KG files; do not bypass the
   policy fixes to create background autonomy.
4. Existing TaskFlow durable detached jobs/resource lanes and opt-in service
   lifecycle; exact job acceptance before spoken promises of continued work.
5. Actual Mac computer-control, continuous voice and easy local/cloud setup
   acceptance; package only a frozen exact-source revision into a new candidate.
6. Messages receive/offer delivery, exact first-merchant checkout and iOS/glasses
   handoff; account/physical effects keep separate explicit acceptance gates.

Steps4-6 retain the existing detailed contracts in
[multitasking/install plan](MULTITASKING_AND_EASY_SETUP_PLAN.md),
[request coverage](REQUEST_COVERAGE.md), [release readiness](RELEASE_READINESS.md)
and [iOS handoff](IOS_AGENT_HANDOFF.md). Social/collaboration, avatars, health,
CLI oversight, developer extensions, subscription access, Linux and other requests
remain tracked there. Gen-UI expansion and additional Linux work remain deferred.

## Release and reporting boundary

9.40 is built from the recorded runtime source and its prior bounded native
browser/setup acceptance remains valid. This review adds no new bundle or
physical/account certification. Missing dependency paths and newly reproduced
runtime defects prevent a claim that every bundled feature works fully.
[Immutable artifact evidence](NATIVE_9_40_ACCEPTANCE.md).

Use [WORK_STATE](WORK_STATE.md) as the single current product checkpoint. It records
completed/reproduced/unverified work, owners and next acceptance gates. During an
active turn, report meaningful findings at least once per minute. At integration,
record source, tests actually run, publication state and remaining gates. Avoid
large GUI/build waves for isolated status polish; review dependency and production
reachability first, freeze source, run relevant checks once, then assemble.

No calendar completion estimate is established by this review. The supplied
engineering estimates are planning assumptions, not measured execution evidence.

## Independent execution receipts and production references

- Routine/budget review: four supplied defect reproductions and nine independent
  follow-up controls, including actual LLMProvider and TaskFlow methods, real
  isolated SQLite and inert effects. Runner exit 0, zero network attempts, unchanged
  runtime hashes. Countercontrols include hard physical/surface deny, ordinary deny
  without auto-confirm, normal scheduler reopen, persisted rollups and yielding sleep.
- Voice/memory/agent review: 34 probes and 100 relevant existing tests passed,
  nine warnings, 4.82 seconds. These include managed realtime refusal and ownership/
  cancellation guards. Probe success means defect assertions reproduced. All 12
  reviewed source files equal the packaged runtime source. No acoustic claims.
- Computer-control review: three inert actual-method checks for missing-manifest
  dispatch, registered-manifest denial/session binding and shell boundary handling;
  targeted source/bundle inventory. No physical input or packaged ABI import test.
- Parent verified all 13 supplied audit source hashes at start. Documentation HEAD
  43c1dcb5b has successful general/native/desktop/docs/version/naming CI; opt-in
  real-brain remains skipped. This does not make the reproduced defects pass.

Production references: cron startup `api/server.py:952`, direct routine
`api/server.py:1517`, resolver fallback1550 and direct call1587; scheduler catch-up
`agents/scheduler.py:1046`, callback/completion1153; budget admission
`cost/budget.py:386`, usage persistence447, LLM preflight
`agents/llm_provider.py:4498` and settlement4625; TaskFlow runner
`agents/taskflow.py:454`. All paths are under feral-core unless stated otherwise.
Computer inner gates `skills/impl/agentic_computer_use.py:405`, drag380/506,
shell543/558; canonical input imports `skills/impl/gui_computer_use.py:835`;
AX `skills/impl/macos_ax.py:949`; provider normalization
`agents/computer_use_driver.py:312`; Codex dynamic-tool limitation
`providers/codex_provider.py:609`.

Private receipts retained outside Git:
`/private/tmp/feral-review-reconciliation-20261004.json`,
`/private/tmp/feral-routine-budget-independent-review-evidence-20261004.json`,
`/private/tmp/feral-independent-voice-memory-review-20261004.json`,
`/private/tmp/feral-cu-readonly-probe-evidence-20261004.json`.
Scripts and logs remain with their corresponding private review reports. The
supplied audit JSON remains unchanged; consolidated review receipts record the
independent rerun results separately.
