# Proposed Forge generation operation and recovery card

Date: 2026-10-02. **Read-only audit and implementation proposal. Nothing in this
plan is implemented or tested.** No production or test source was edited, and
no compilation, generation or account operation was run for this audit.
Native Forge's implemented fixture evidence is a separate document.

## Confirmed source findings

- `agents/tool_genesis.py` stores both sequence and intent records under a bare
  12-hex signature. Intent hashing considers only the first 200 Python code
  points. Same-prefix intents can overwrite one another; intent and sequence
  records also share this signature namespace despite differing tool ID prefixes.
- Both generation methods assign `_generated[sig]` before persistence.
  `_persist_generated` uses `INSERT OR REPLACE`. A database failure can leave an
  in-memory draft even though generation reports failure. There is no atomic
  target reservation before the awaited provider call, so two callers can both
  incur model work and race their writes.
- SQLite saves full generated code, but does not persist approved or
  requires_approval flags. Reload constructs defaults: previously approved
  records can reappear as pending. Existing records have no full-intent digest,
  request identity, revision, original owner or trustworthy stored approval state.
- The propose/generate routes call the engine directly. Approve directly marks
  approval and promotes files; execute directly calls the sandbox helper.
  These route functions do not traverse ToolRunner's plan/safety/lease dispatch.
  Global API middleware is transport authentication, not that dispatcher.
- `_on_capability_gap` also directly proposes; loose mode directly approves and
  promotes. Generation hardening limited to REST would leave that writer open.
  Its strict-mode workspace-script branch is a separate existing path.
- The shared LLM is already wired to the configured CostBudget in `api/state.py`.
  Do not create another cost authority or retry a model call after uncertain billing.
- Existing TaskFlow `skill.invoke` already checks pause, plan mode and full
  ToolRunner policy. It persists exact approval/step terms before notifying,
  consumes the existing one-call approval execution, and preserves uncertain
  effects across restart. TaskFlow creation currently has no request dedupe key.
- `skills.call_context` is a correctness aid with an explicit kill switch, not
  an unbypassable authorization boundary. A JSON operation ID must never grant
  permission or be treated as a trusted dispatcher identity.

## Recommended smallest coherent shipping card

**FORGE-02: durable, create-only draft operations through existing TaskFlow and
registered skill dispatch.** Limit it to proposing and generating drafts. Do
not add generated-code execution, arbitrary replacement, a payment authority,
an approval database or another history/dispatcher stack.

Use one existing TaskFlow containing one registered `skill.invoke` for draft
generation. Add bounded optional idempotent creation to TaskFlow's existing
SQLite store: canonical client request UUID, trusted local owner/session scope,
namespace `forge.draft`, complete exact-terms SHA-256 and a target reservation.
Creation of the request mapping, flow and target reservation must commit in one
transaction before the scheduler can see the flow. Task/step/request IDs then
remain stable through deferred approval, cancellation and reconnect.

The target reservation must reflect the engine's actual shared signature key,
not merely its displayed tool ID. Reserve globally within the deployment,
because the generated registry is currently global, not per conversation.
Two different scoped callers must not reserve the same underlying row and
overwrite it. Same UUID plus different terms is a conflict, not a new job.
Exact duplicates return the existing operation/status without another flow,
approval request or provider call. A different UUID for a reserved/existing
target is a conflict. Keep compact request/target tombstones when bounding
retained task detail; deletion must not silently restore replayability.

In the engine's existing database, replace generation-time upsert with atomic
create-only insertion. Check/reserve before provider work and check again at
commit. Existing tools, including known pending tools, cannot be replaced.
Never update in-memory generated inventory before the database transaction
commits. Persist creation operation reference, full-input digest, revision and
actual approval state alongside new records. Use the existing SQLite file,
not a separate Forge data store. Move blocking database work off async handlers
using the repository's existing threading/SQLite conventions.

TaskFlow and generated-tool databases cannot be committed as one ordinary
transaction. Treat that boundary honestly: if a draft commits but the TaskFlow
result does not, restart leaves the flow uncertain. A read-only reconciliation
can match the committed generated row's exact operation/terms reference and
establish that a draft was recorded. It never repeats the provider call or
infers code execution. Old/incomplete records cannot supply that proof.

Registered draft endpoints should describe their mutation/model-cost semantics
in the manifest and use existing configured policy, plan mode, supervisor and
agent-generation lease checks. A native review does not become `confirm:true`
or a standing grant. When policy asks for approval, reuse the existing TaskFlow
approval path and identity binding before notification. Preserve the user's
autonomy settings. No draft endpoint implicitly approves or promotes code.

Route both REST generation and capability-gap generation through this path.
Until that conversion is complete, enforce the create-only engine invariant
for every legacy method as well: a legacy call cannot remain an alternate
upsert writer. Legacy wrappers may return a typed conflict or existing exact
receipt; they must not pretend a conflicted draft was newly generated.

## Proposed exclusive source ownership

No reservation is currently active. The parent must assign these paths before
implementation and coordinate any other writer.

| Owner slice | Proposed exclusive production paths | Focused tests |
| --- | --- | --- |
| Engine/storage worker | `feral-core/agents/tool_genesis.py` | Existing `tests/test_tool_genesis.py`; new `tests/test_forge_atomic_drafts.py` |
| Draft API/skill worker | `feral-core/api/routes/tool_genesis.py`; new `skills/impl/tool_genesis.py`; new `skills/manifests/tool_genesis.json` | New `tests/test_forge_operation_api.py`; new `tests/test_forge_draft_skill.py` |
| Parent shared integration | `feral-core/agents/taskflow.py`; narrow `api/state.py` wiring and `agents/orchestrator.py` capability-gap routing | Existing `tests/test_taskflow_dispatch_policy.py`; new `tests/test_taskflow_request_dedupe.py`; exact capability-gap regressions |

The ToolRunner currently logs the first 200 characters of all arguments. If
the new registered draft call includes the intent for exact approval display,
that logging path needs a narrowly coordinated privacy change in
`agents/tool_runner.py`, with a no-intent-in-normal-log regression. Request and
digest IDs are diagnostics; the user's task text is not. This is a dependency,
not permission to edit that shared file during another frozen wave.

## Proposed additive client contract

The existing generation endpoints gain a negotiated operation version and
canonical request UUID. The server supplies stable operation/flow identity.
Add one read-only status route under the existing Tool Genesis router, scoped
to the trusted caller and bounded lookup. It reports queued, awaiting_approval,
running, draft_recorded, refused, cancelled or outcome_unknown, plus separate
current draft existence/approval state. Processing completion is not code
execution; a deleted draft does not erase its operation receipt.

Legacy bodies need an explicit compatibility strategy: server-minted request
IDs alone do not give the caller replay-safe correlation, though global target
reservation still prevents duplicate generation. Do not activate the new
queued/approval response contract behind old native/web clients that only
accept immediate `{success, tool_id, preview}`. Negotiate support before any
new client sends a generation task, then update both clients in a later,
separately owned slice. Status reads never recreate or resume generation.

## Required meaningful verification

Use real SQLite with disposable profiles and two independent engine/store
instances, plus controlled async provider barriers. No real provider, generated
code execution, file promotion or account effects are needed.

1. Same request/terms interleaved through two callers: one durable flow, one
   policy request and one provider invocation. Same request/different terms
   conflicts before model work. Wrong-session status/approve/cancel cannot
   access another operation.
2. Two different requests for one target, including Unicode same-first-200
   intents and an intent/sequence signature alias: at most one generation,
   no overwrite of pending, approved or registered records.
3. Pause, plan mode, deny, confirm, lease revocation and cancellation before
   first scheduling prevent provider work. Immediate approval during notifier
   await sees already persisted exact operation/step terms and executes once.
4. Death after claim, during provider work, after code receipt, after draft
   commit and before flow result commit: restart never replays. A committed
   exact operation-tagged draft can be reconciled read-only; absent drafts and
   legacy rows do not prove a provider call did not occur.
5. Persistence/commit failure never publishes an in-memory-only draft as
   confirmed. Lost HTTP reply followed by duplicate submit returns the same
   receipt without another provider call or draft.
6. Approval mutation and generation reservation race: neither can replace or
   silently reset the other's state. Reject/delete does not discard dedupe
   tombstones. Quota and expiry failures are actionable and effect-free.
7. Migration preserves code and identifiers. Rows lacking historical approval
   proof are labelled legacy_unknown, never silently inferred approved or
   pending; existing loaded skills are not removed. Malformed/duplicate
   legacy inventory fails mutations closed without deleting user records.
8. Existing headless/REST/capability-gap callers and public response fixtures
   remain deliberately compatible. Normal logs contain no intent or code.

## Deliberate boundaries

Approval/promotion/execute routes remain a separately visible policy integration
gap until converted. Do not label all Forge operations centrally gated merely
because generation is fixed. Likewise, this proposed draft card does not claim
full generated-code review from a truncated preview, rollback of model billing,
distributed multi-user registry isolation or atomic replacement support.

The first implementation checkpoint can land engine create-only storage and
dedupe primitives with tests, but the shipping gate is the complete dispatcher,
client-negotiation and read-only recovery integration. This audit assigns no
completion percentage and changes no runtime behavior.
