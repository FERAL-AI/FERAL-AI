# DATA-01 / CORE-02: durable model thread plan

October 2, 2026. Read-only audit against the existing FERAL runtime and native
client, after checkpoint `4e19f07f8603196fd73ecdafd0e127dbad865e69`.
This document is a proposal, not implemented or verified durability. Production
edits remain deferred until the parent commits/stages the current frozen wave.

Implementation follow-up: [DATA01A storage evidence](RUNTIME_CHECKPOINT_STORAGE_EVIDENCE.md)
now covers versioned SQLite/CAS/codec primitives and their tests. Runtime
writer, restoration and cleanup integration below remains proposed; the storage
slice alone does not provide durable model continuity.

## Outcome and current evidence

Reopening an exact saved thread should restore its server-generated model
context, not only its visible messages. It must never replay a request, execute
historical tool calls or restore historical permission authority.

Source inspected:

| Source | Confirmed behavior |
|---|---|
| `api/server.py`: `client_session`, `_prepare_chat_turn_context`, `_submit_tracked_chat_turn` | Query-selected session identity binds commands. Preparation pushes/refines a new user prompt; it does not restore durable history. Last nonprimary detach clears working memory and calls disconnect cleanup. |
| `agents/orchestrator.py`: command wrappers, `_finalize_turn`, disconnect/eviction | Stream/nonstream commands hold a per-session lock. Finalization is synchronous, runs in `finally`, and snapshots only the primary thread. Disconnect and eviction remove history, locks and image tables without the same lock. |
| `agents/orchestrator.py`: compaction and `_append_voice_row` | Compaction snapshots/swaps under the session lock but reconciles by length, not context generation. Voice appends under that lock and persists episodes separately. Neither supplies a durable per-thread runtime checkpoint. |
| `memory/store.py`: conversation and snapshot methods | Conversation blobs are saved UI records. SQL snapshots persist raw `history[-200:]` and working memory without a format/revision/fence contract. Working memory has a 50-row cap; live orchestrator history has a 200-row cap. |
| `api/routes/conversations.py`: branch, restore, delete, save | Manual branch/restore writes runtime arrays directly without active-turn/revision protection. UI save is a whole-blob upsert; a stale local save can recreate a deleted UI row. |
| `api/state.py`, `memory/session_snapshot.py` | Only the primary session has automatic JSON snapshot restore. The file contains raw history/working JSON, outside the database encryption envelope. |
| `memory/at_rest.py`, `memory/sync.py` | Existing encryption covers memory/sync databases while stopped; runtime SQLite and retained operator backups are plaintext. The sync allowlist includes UI conversations, not runtime session checkpoints. |
| `desktop-native/APIModel.swift`, `NativeSessionRecoveryFeature.swift` | Opening a saved conversation restores visible records and connects its exact SID, explicitly without injecting UI records into model context. Live transcript recovery uses position markers, not durable message identities. |
| `agents/chat_turns.py`, `gateway/protocol.py` | CORE-04 receipts provide durable acceptance/processing status and read-only `chat.status`. Receipts are not model transcript snapshots or permission to retry an effect. |

Existing `/api/checkpoints` routes concern file-write restoration backed by
`skills/checkpoints.py`; they are unrelated to conversation context.

The completed DEV-01C SDK tests separately verified live sequential provider
context and concurrent thread isolation. This audit ran no new durability,
encryption, migration or native restoration tests.

## Recommended first backend card: DATA-01A

Add a versioned `runtime_session_checkpoints` table in the existing `memory.db`.
Keep it separate from `conversations`, manual saved points and the primary JSON
file. Do not add it to federated sync, UI autosave or an arbitrary HTTP
history-write surface.

A bounded row is keyed by the **exact single-operator SID** and contains:

- Format version, monotonic revision, runtime/context generation and phase
  `ready`, `in_progress` or a deletion fence.
- Last safely committed canonical model history and working-memory payload.
- Writer attempt UUID, last committed turn identity, byte/row counts and update
  time; metadata is context provenance, not execution authority.
- Explicit truncation/image-omission information and any unresolved checkpoint
  state needed to avoid presenting an older checkpoint as current.

Store methods return typed read/claim/CAS/commit results. A stale writer must fail
rather than replace a newer generation/revision. Use one existing session lock
for initialization, execution, restoration and cleanup; do not delete a lock
while it is held or has queued users, creating two locks for the same SID.

Before any new turn mutates context or begins execution, durably fence the row
as `in_progress` under that lock. After synchronous `_finalize_turn` has assembled
the canonical history, the async command wrapper awaits bounded checkpoint
commit before releasing the lock. Preserve the original exception/cancellation
and truthful CORE-04 outcome when checkpointing fails. Do not turn completed
external effects into a retry suggestion or assert checkpoint success from a
background task merely being scheduled.

A crash or failed commit leaves a detectable incomplete fence. Restoration must
report reconciliation required, not silently run a new prompt against an older
checkpoint. Existing turn/effect receipts remain the reconciliation source; the
checkpoint never resumes queued work. Missing, malformed, oversized,
unsupported-version, locked, conflicting or deleted context has an explicit
state. A legacy nonempty UI conversation without runtime context cannot be
silently imported or claimed as a restored conversation.

Restore on an authenticated query-bound attachment before preparation/refinement
can append a new user row. Register the attachment first, take the session lock,
and recheck the attachment/current generation so an older socket's cleanup
cannot wipe the restored session. If current RAM context is already authoritative,
do not overwrite it from disk. Command and voice entrypoints also ensure context
under the same lock; restoration cannot be exclusive to desktop WebSockets.

Checkpoint voice appends and compaction swaps too. Compaction must fence its
out-of-lock summary by generation/revision, preserving concurrent arrivals only
within that unchanged generation. Disconnect/eviction must recheck active turns,
attachments and the latest commit under the stable lock before clearing RAM.
Skip active/locked eviction candidates. Branch/manual restore must use this same
lifecycle and expected revision/generation; no direct history replacement bypass.
An explicit branch to a new target must not overwrite an existing target.

Deleting a saved conversation fences its runtime context generation, preventing
an in-flight checkpoint writer from resurrecting it. This does not undo actions
or erase all episodes, facts, approvals and turn receipts. UI autosave CAS and
local UI deletion tombstones remain a separate follow-up contract: checkpoint
isolation prevents their stale blobs from overwriting model context.

## Validation, privacy and authority

Start with the current 200 history / 50 working-row limits, additional bounded
byte/storage/SID quotas, and strict format validation. Trim complete user/tool
exchange groups rather than naive tails. Validate ordered assistant call
announcements and matching tool outputs. Historical unresolved/orphan groups
must be omitted with an explicit marker, never dispatched on restore.

Persist only server-generated model data. UI metadata, imported/synced
conversation records and arbitrary client-supplied system/tool rows never become
a checkpoint. Rebuild the current system prompt, provider, execution surface and
policy from current configuration. Do not restore approval caches, plan grants,
execution leases, credentials, pending actions, sockets, device/voice state or
paused execution. Derive conversational subject from the restored transcript,
rather than treating persisted routing state as authority.

Tool images live in separate ephemeral tables and disappear on disconnect.
Initially report that those old images are unavailable after restore; never
claim complete visual memory or automatically recapture camera/screen content.
Multimodal user data needs an explicit bounded binary-retention policy before
full visual continuation is claimed.

Database checkpoints inherit the existing stopped-database encryption workflow;
this is not always-encrypted SQLite. Locked/tampered memory must not fall back to
an empty/stale plaintext store for restored context. Checkpoints must remain
outside sync to avoid accepting remote payloads as trusted runtime context.

The existing primary JSON snapshot is a real additional plaintext content
artifact. Do not create more sidecars. A primary migration/privacy gate must
validate the exact known primary SID, canonical legacy runtime shape, bounds and
current checkpoint absence, commit/read back the SQL result, then deliberately
retire the old writer/artifact. Do not guess provenance from a saved UI thread or
silently overwrite a newer SQL checkpoint. Artifact retention/cleanup needs an
explicit implementation contract; this audit did not migrate or delete files.

## Wire and native split

Propose passive `GET /api/sessions/{sid}/context` plus authenticated bound
WebSocket/gateway context readiness. Return version, exact SID, runtime/context
generation, revision and `new`, `ready`, `restored`, `unavailable` or
`reconciliation_required`, with omission flags. Do not expose raw private model
context just to verify readiness. Canonical SID validation must be shared before
attachment/lookup, not silently normalize ownership with `.strip()`.

Keep `GET /api/conversations/{id}` as UI data. Native opening/boot must wait for
matching `socketGeneration` and `conversationRevision` readiness before enabling
Send or claiming context restored. Never fall back from an exact selected SID to
recent or primary history. Saved messages stay visible when context is unavailable.
Live transcript merging remains a separate display operation. Use a new thread
for deliberately fresh work; an interrupted task is never replayed automatically.

Proposed exclusive ownership after publication:

| Backend DATA-01A | Native CORE-02A, after stable wire |
|---|---|
| New `memory/runtime_session_checkpoint.py` codec/types | `desktop-native/APIModel.swift` |
| `memory/store.py` schema/CAS/fences | `desktop-native/NativeSessionRecoveryFeature.swift` |
| `agents/orchestrator.py` lifecycle and awaited commits | `desktop-native/NativeChatToolsFeature.swift` for reviewed restore/branch revision |
| `api/server.py` bound activation/readiness | Relevant native model/recovery/chat-tools tests |
| `api/routes/sessions.py` passive metadata | Native fixture and actual app acceptance after integration |
| `api/routes/conversations.py` manual restore/branch/delete fencing | No backend/shared-contract edits by native worker |
| `api/state.py` lifecycle/primary migration wiring | |
| New backend checkpoint/lifecycle tests and evidence | |

The parent currently owns store/type and identity-workspace changes. Do not
assign overlapping production files until those edits are integrated and the
current source freeze is released. SDK DEV-01C remains frozen and alive-only;
explicit durable SDK resume requires a later agreed contract.

## Exact proposed acceptance

All mutating checks use disposable homes/databases, controlled providers and
in-memory vault wrappers; never a personal deployment or real keychain.

1. Execute two nonprimary turns through the registered route, close all sockets,
   destroy/recreate store and orchestrator, activate the exact SID, and record the
   next provider request containing preceding user/assistant rows and legal tool
   pairs. Assert zero historical tool executions during restoration.
2. Restore simultaneous A/B threads; provider context, working rows, generation,
   status and receipts stay exact-session isolated, with no primary fallback.
3. Race detach/reconnect with active and queued turns; no second session lock,
   cleared live context, double execution or stale checkpoint overwrite.
4. Interrupt after the durable in-progress fence and simulate failed commit/disk
   error. Reopen reports reconciliation required and sends **zero new provider
   prompts/tool calls** until that state is explicitly resolved.
5. Race stale/equal-length compaction with restore/reset; old generation/revision
   cannot replace the newer transcript. Completed concurrent arrivals survive.
6. Delete context during a pending writer; its stale commit cannot resurrect the
   checkpoint. A stale UI autosave cannot turn that blob into model context.
7. Reject unsupported versions, wrong SIDs, corrupt/oversized payloads and
   client/UI/sync metadata. Report capped/orphan-group/image omissions honestly.
8. Revoke a currently required tool grant; historically approved transcript data
   cannot authorize the next invocation. No pending operation/lease resumes.
9. Verify checkpoint coverage in an encrypted stopped DB; locked/tampered restore
   refuses without plaintext fallback. Verify primary migration exact readback,
   conflict refusal and no newly written plaintext snapshot sidecar.
10. Native boot/open/new chat waits for exact context readiness; stale or foreign
    generation events cannot change selection or enable Send. Missing legacy
    context retains visible messages with truthful status. Inspect the actual app.

Relevant regression sources include `test_shared_session_lifecycle.py`,
`test_session_eviction_by_staleness.py`, `test_primary_session_id.py`,
`test_session_snapshot_trailing_edge.py`, `test_stream_nonstream_parity.py`,
`test_compaction_does_not_block_next_turn.py`, `test_transcript_survives_compaction.py`,
`test_tool_result_images.py`, conversation creation/thread/multimodal tests,
`test_native_deferred_vault.py`, `test_memory_encrypt.py`,
`test_sync_wal_at_rest.py`, CORE-04 receipt/abort tests and generic SDK thread tests.
Run focused acceptance, then the parent-owned combined regression on frozen
sources. Local fixtures do not replace Ubuntu/Node 22 CI or actual app acceptance.

## Deferred release gates

Backend persistence is not implemented by this document. Native readiness,
legacy-primary migration/privacy, manual restore fencing and writer coverage must
pass before advertising durable conversation continuation. UI autosave revision
and deletion recovery, binary visual continuity, explicit durable SDK resume,
complete user-data erasure, multi-user owner ACLs, Linux/iOS continuity and
clean-machine install acceptance remain separate gates. Selecting a SID in the
currently supported single-user installation is not a multi-user identity or a
new permission grant.
