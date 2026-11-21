# Native saved-context integration evidence

Recorded October 2 local time (October 3 UTC); final source/fixture verification includes the full 37-feature run, scope-label rerun and final post-readback correction run, distinguished below. This is source and isolated fixture evidence. It is not packaged GUI acceptance, provider inference, or certification of every task-entry screen. The immutable 2026.9.29/build2026100203 artifact and its acceptance report remain unchanged.

## Implemented source behavior

- Explicit **New chat with saved context** in Chat and Conversations, with disclosure before creating a text-only chat. Standard New conversation and existing threads retain legacy behavior. There is no convert-existing-thread action.
- Fresh full UUID identity and installation-scoped pending creation intent are retained before attaching. Only opted-in or known-managed connections send `context_checkpoint_version=1` on the existing session WebSocket.
- Runtime context capability and whole-turn receipt capability are separate requirements. Initial UI creation, prompt presave and prompt submission require verified context READY. The opt-in route uses atomic `create_if_missing:true` and empty-record readback; it never supplies UI messages as runtime context or overwrites a nonempty winning record.
- Parsing binds exact session/request/connection and validates version, strict Boolean/integer types, durable flag, canonical UUID generation, positive revision and the actual codec omission keys: `history_rows`, `system_rows`, `images`, `working_rows`. Generation change or revision regression blocks dispatch. A positive managed flag remains sticky even if the rest of the capability is malformed. Actual refused attachment can report managed=false and is not falsely reported as READY.
- Managed mode and interrupted creation identity use the explicitly selected preference suite, scoped to the verified primary installation. Malformed mode preferences are refused without silently resetting them.
- A managed terminal starts a read-only capability refresh before the next task can be submitted. Cancelled/in-progress context is not falsely promoted to ready. Previously verified display rows and request outcomes remain savable; readonly request status and exact Stop retain their existing contracts.
- Voice and unsupported saved-point operations are unavailable in managed chats. Conversation tools invalidate old reviews when readiness changes, without preventing passive inspection. Managed-thread deletion is disabled where mode is known; backend remains authoritative for other clients.

Changed native source: `APIModel.swift`, `NativeViews.swift`, `NativeConversationFeature.swift`, `NativeChatToolsFeature.swift`, new `NativeContextCheckpointFeature.swift`. Test changes: `NativeModelTests.swift`, `tests/NativeChatToolsFeatureTests.swift`, new `tests/NativeContextCheckpointFeatureTests.swift`. Parent registered build/test manifests. Existing Conversation tests were unchanged.

## Executed checks

From `ASOS/desktop-native`:

```sh
bash build.sh --typecheck
bash test_features.sh ContextCheckpoint Conversation ChatTools
```

Production typecheck exited 0. Final registered runner exited 0:

| Check | Actual result | Scope |
| --- | --- | --- |
| ContextCheckpoint | Passed | Strict wire, stale identity, readiness fences, refused/managed modes, revision/generation checks, isolated preference persistence and malformed-preference preservation |
| Conversation | 35 assertions passed | Existing mocked HTTP history/readback behavior |
| ChatTools | 11 groups passed | Existing mutations plus context restrictions, stale review refusal and passive inspection without POST |
| Linked NativeModel | 25 groups passed | Includes managed READY, presave barrier, cancelled in-progress refresh, new-only UUID creation, atomic empty readback, no UI import and known-mode downgrade refusal |
| Desktop experience | 39 assertions passed | Existing standalone desktop state fixtures |
| Error presentation | 5 assertions passed | Display-only summaries |

The first linked compile failed because the new test capability helper lacked `@MainActor`; only that test annotation was corrected. The final runner passed. Eight existing Swift-5 NSLock async-context warnings remain in the fixture harness; production typecheck emitted no errors.

Direct standalone ContextCheckpoint, Conversation and ChatTools compiles and fixture executions also exited 0 before the registered run. A syntax-only parse of owned Swift and `git diff --check -- desktop-native` passed. All HTTP/WS model tests used mocked transports; no app, real model, credentials, account, OS permission or external delivery was exercised in this card.

## Read-only audit findings that motivated the follow-up

The following are read-only source findings, not live endpoint tests. Native action gates were subsequently implemented as described below; backend ownership findings remain unchanged. No assumption is made that every HTTP method named GET is passive.

| Surface | Actual scope and endpoints | Boundary identified before the follow-up |
| --- | --- | --- |
| Specialists | Persona/custom registration and feedback are global metadata (`POST /api/agents/spawn`, `/api/agents/feedback`). Proposal spawn is a global provider call. Native model does not use the view's session ID. | Selected-thread readiness does not gate proposal generation. View-only clearing of review on thread change does not fence a task already awaiting fresh inventory. |
| Workflows | Pack instantiate omits SID and backend defaults `pack-{id}`. Native flow/routine drafts send blank SID; TaskFlow execution falls back `taskflow-{flowID}`. Structured interval automation alone supplies selected SID. Resume, compilation and scheduler-starting routine list can begin work. | There is no universal checkpoint guard on these CRUD routes. Context readiness changes do not invalidate the model's issued reviews or guard after awaited preflight. Global inventories can include other owners. |
| Automation triggers | Geofences/inbound/outgoing are global stores. Inbound receive sends text to the first active backend session, not native selected SID. Outgoing test sends real HTTPS. Detail/run inspection is readonly; trigger inventory may initialize a store. | Selected SID is not used by the model. No context epoch on held reviews/preflight. Deterministic inbound/geofence ownership needs a separate backend contract; a native label cannot fix first-session routing. |
| Connections | Only handoff uses selected SID. Access mode, Funnel, pairing, device capabilities and sync are global operations. Pair-link creation is an effectful GET. Export is readonly. | Handoff needs native managed/unready gating and policy-epoch fencing. Backend handoff manager already uses `legacy_context_mutation` to reject managed source/target, but other global settings must not be mislabelled as chat-context actions. |

Inspected definitions: native four feature models/views, `api/routes/agent_mitosis.py`, `taskflows.py`, `personas.py`, `routines.py`, `intents.py`, `timeline.py`, `webhooks.py`, `outgoing_webhooks.py`, `handoff.py`; TaskFlow and session-handoff execution. The routine list explicitly calls scheduler `ensure_running`; loading it can restart scheduled work. TaskFlow skills bind actual stored/fallback SID; LLM steps enter orchestrator using that SID. CRUD acceptance alone does not verify saved-context execution outcomes.

## Action-specific native follow-up

Approved follow-up owns the four feature models/views and their tests, `NativeViews.swift`, and the shared pure context-policy feature/tests. `APIModel.swift`, backend routes, SDK and packaged candidate were unchanged by this follow-up.

The UI supplies exact selected SID, chat connection epoch, task readiness and managed mode. Each model carries an independent action-policy revision in issued reviews. Policy changes invalidate held reviews without clearing readonly inventories. Review/confirmation paths update policy before acting; effectful requests after awaited preflight recheck the exact revision. A request already dispatched cannot be undone by a later policy change; stale outcomes are not promoted to current-context success and no automatic retry is added.

- Specialists: global persona/custom registration and feedback remain available. Global provider proposal generation requires readiness; that readiness is not a provider permission grant. Global scope is stated in review and receipt.
- Workflows: new queued work, resume, plan compilation, scheduled creation and the scheduler-starting routine-list GET require readiness. Detail inspection, cancel, pause, delete and manual completion remain recovery/metadata controls. Dedicated/global background owner semantics are retained, including existing pack/default sessions. No existing row or new route was silently rebound to selected chat saved context. Reviews now identify action-specific scope: pack default, blank flow/routine fallback, actual inventory owner when provided, selected-SID structured automation, or global scheduler/intent metadata. Receipts state returned owner data or verified structured-automation ownership; omitted owner fields remain explicitly unconfirmed.
- Automation: creation/arming and real outgoing test require readiness. Reviewed inventory, exact routine/run detail and trigger deletion remain available. Global scope and inbound first-active-session behavior remain disclosed; native gating does not repair backend trigger routing.
- Connections: handoff requires a ready standard chat and the exact selected source, and is unavailable for managed context. Global access, pairing, capability and sync controls retain existing review. Readiness is not an OS/device permission or grant. Pair-link creation is correctly treated as effectful despite being GET.

Executed selected run: ContextCheckpoint passed, Specialists 39 assertions, Workflow 74, Automation 38. Connections initially aborted because the new test cases were accidentally placed after the mock session's existing teardown (`Task created in a session that has been invalidated`). This was a fixture sequencing error, not a packaged app crash; teardown was moved after the new cases. Corrected Connections runner exited 0: 37 assertions, linked Model 25 groups, Desktop 39, Error 5. Initial direct Specialist compile also omitted its pre-existing `NativePlainTextEditor` dependency; registered runner includes it and passed without changing production source for that omission.

Production `bash build.sh --typecheck` exited 0 on identical frozen production source. Final tests additionally distinguish ready-managed versus unready-standard handoff and blocked outgoing-test refusal versus successful global trigger deletion. The complete frozen pre-label 37-feature registered matrix exited 0, including ContextCheckpoint, Specialists 39 assertions, Workflow 74, Automation 40, Connections 37, linked Model 25 groups, Desktop 39 and Error 5. This full-matrix result belongs to the source before the narrow Workflow scope wording/readback refinement below. All tests use mocked HTTP/WS; no Tailscale, location sample, real webhook, provider generation, scheduled job, credential or external transmission was exercised.

## Narrow scope-label refinement

After the full 37-feature pass, only Workflow production source and its fixture were refined. No endpoint, request body, session routing or ownership was changed. Source-backed distinctions were checked against pack instantiate in `api/routes/personas.py`, flow creation/execution in `api/routes/taskflows.py` and `agents/taskflow.py`, routine serialization/creation in `api/routes/routines.py` and fallback execution in `api/server.py`.

- Pack requests omit SID and disclose the existing `pack-{workflowID}` default. Blank native flow/routine drafts disclose `taskflow-{new flow ID}` or `routine-{new routine ID}`. These expected defaults are not fabricated receipt confirmation.
- Existing/returned records identify the actual `session_id` when present. Blank returned SID is reported as blank with the source-backed dedicated fallback. Missing SID stays **Owner not confirmed by this response**. Returning an identifier is not asserted to be an independent durable readback.
- Structured interval automation already verifies exact selected SID through routine detail and inventory. Its receipt now says **Readback verified selected conversation**. Deletion distinguishes verified preflight owner from verified absence. Global scheduler/intent actions retain explicit global scope.

Affected registered command: `bash test_features.sh Workflow`. Workflow passed **80 assertions**, including six new scope cases: exact selected automation owner, missing pack/flow/routine owner, returned blank flow fallback and returned independent flow owner. The affected registered runner exited 0: Workflow 80 assertions, linked Model 25 groups, Desktop 39 and Error 5. Final production `bash build.sh --typecheck` exited 0 on the frozen refined source; `git diff --check -- desktop-native` also passed. The linked fixture harness retained its eight existing Swift-5 NSLock async-context warnings; production typecheck emitted no errors. The earlier full 37-feature pass is retained separately and is not relabelled as a rerun on this final wording refinement.

## Final post-readback receipt correction

Parent review then found four late-readback publication gaps after the prior freeze. The candidate was not assembled. Only the four action-feature sources and their tests were temporarily unfrozen; APIModel, context parser, backend, SDK and candidate resources were unchanged.

- Specialists now recheck exact action policy after the awaited specialist-list readback and before assigning its receipt.
- Workflows recheck after the final awaited inventory refresh and before assigning success.
- Connections recheck after refresh and capability reload before assigning success. Both awaits have dedicated regression paths.
- Automation holds receipt text locally until its final policy check and clears any uncommitted receipt on error. A stale readback no longer leaves success text beside its uncertain-outcome error.

The new URLProtocol fixtures commit one mocked effect, hold only a later GET/readback response, change the selected-context policy while that response is pending, and then release it. They verify no early receipt, false final success, cleared receipt and exactly one effect. Specialist/Workflow/Automation also verify that the consumed review cannot replay. Existing global metadata/recovery controls and preflight refusal tests remain in these same suites. These are actual mocked transport interleavings, not GUI, real effects or backend universal gating.

Affected registered command: `bash test_features.sh Agent Workflow Automation Connections`. Individual suites passed Specialists **42 assertions**, Workflow **83**, Automation **43**, Connections **41**. Final registered runner exited 0, including linked Model **25 groups**, Desktop **39**, Error **5**. Final `bash build.sh --typecheck` exited 0 on this latest frozen production source, and the whitespace check passed. Eight existing Swift-5 NSLock async warnings remain in the linked fixture harness; no production typecheck errors were emitted. The earlier complete 37-feature run and final scope-label run above remain evidence of their earlier frozen source, not claims of a rerun on this latest correction.

## Limits and next acceptance

No claim of universal backend/API task gating, complete persistent-life memory, deterministic inbound routing, managed voice/handoff, or actual new-feature GUI acceptance. The new native action gates cover the explicitly audited action sets; future task-entry surfaces must adopt their own policy contract. A create/readback cannot atomically prevent all later changes by another client. Preferences are mode hints; runtime capability/checkpoint is authoritative. Pending setup reuses its exact identity; automatic replay or converting existing UI messages is forbidden.

After the final combined native checks pass on frozen source, publish the native source/evidence. Parent then builds a new candidate from an exact committed revision. Actual isolated acceptance must verify the disclosure, legacy preservation, creation/readback, managed restart continuity, real local replies, blocked interrupted context, readonly status/Stop, thread switching and owned-process cleanup. The prior 9.29 GUI result cannot substitute for that acceptance.
