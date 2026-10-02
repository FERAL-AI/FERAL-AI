# DATA01C runtime context activation

October 2, 2026. Work in progress. Production storage/lifecycle sources remain
frozen for the parent's DATA01B publication. This document records the activation
contract and controlled regressions; it does not claim production activation.

## Confirmed ordering problem

Native `APIModel.beginRequest` persists its submitted user row before sending
`text_command`. The first DATA01B writer refuses a missing checkpoint when the
UI conversation is nonempty. That refusal is necessary: UI rows and request
markers cannot establish trusted model context. Attachment must establish and
await a ready empty server checkpoint before native is allowed to presave its
first submitted row.

`test_presaved_request_marker_is_not_trusted_empty_context` reproduces the
existing refusal with a disposable SQLite database and a fake provider. It
asserts zero provider requests, no new checkpoint, and no imported model history.
`test_ready_read_is_read_only_and_never_imports_ui` checks that reading the
existing ready checkpoint does not copy subsequently saved UI content.

## Proposed attachment contract

Authenticated sockets may request `context_checkpoint_version=1`. Raw exact SID
validation rejects malformed identities without trimming. Omitted opt-in retains
legacy behavior only for an untouched SID: an existing checkpoint row cannot
escape lifecycle protection through a false or absent client flag.

The coordinator reserves an opaque attachment identity before waiting for the
existing exact-SID orchestration lock. Reservations are bounded to 128 globally
and eight per exact SID. Cancellation removes the exact reservation; no held or
queued lock is evicted. Under that lock it either validates and
restores READY context or initializes absent trusted empty context. The storage
initializer uses one transaction to check absent checkpoint, absent/empty UI,
row and byte quotas, then commit an empty READY checkpoint. No task, pending turn,
request marker import, or transcript import occurs. Existing pending, deleted,
corrupt, unsupported or otherwise conflicting records fail closed.

`chat.capabilities` remains read-only. Additive readiness metadata is proposed:
`context_checkpoint_versions`, `context_ready`, `context_state`, and, only when
ready, `context_checkpoint` with version, exact SID, generation, revision,
`durable`, `initialized` and explicit omission counts. No raw history, internal
writer attempt, opaque attachment token or credentials appear on the wire.
Omission metadata is immutable internally and copied for wire serialization.
Only the exact query string `1` selects checkpoint version one.
Current policy and system prompts are rebuilt from current configuration.
Checkpoint generation/revision grants no authority or effect-success claim.

Native must retain drafts and attachments and perform zero presend saves or task
submissions until strict readiness evidence. Read-only task-status reconciliation
must remain available when context is unavailable or pending. A cached initial
READY claim cannot substitute for checking the current committed fence.

## Writer closure inventory

| Writer | Required activation boundary |
| --- | --- |
| Tracked, legacy web and device text preparation | Existing SID scope before working-memory prelude; await checkpoint commit before terminal/context-ready |
| Command wrappers and tool approvals | Same coordinator; preserve exact task/session/approval identity and unknown-effect semantics |
| Primary greeting and primary JSON hydration | No silent plaintext or UI import; explicit legacy/migration gate |
| Voice router and transcript hooks | One trusted history/working scope, or explicit managed-thread refusal |
| Realtime voice tool summary | Normalize the actual trusted writer shape; never expand codec authority from arbitrary records |
| Automatic/manual compaction | Compare captured checkpoint fence/revision under lock before replacing context |
| Manual snapshot, restore, reset, branch and deletion | Server-created checkpoint provenance, strict CAS/tombstones, or explicit managed-path refusal |
| Session handoff | Exact source/target locks and checkpoint semantics, or explicit managed-target refusal |
| Disconnect, stale eviction and images | Exact attachment identity/refcounts plus same SID lock; recheck current attachments; never pop held/queued locks |

Production realtime currently writes a working-memory tool summary shaped as
`role:tool`, `tool`, `result_summary`, while the strict storage codec accepts
working user/assistant text. This gap requires trusted writer normalization or
explicit refusal before managed realtime voice is advertised.

## Ownership and acceptance

Parent owns server/state/chat-turn integration and native gating. Worker-owned
production edits will begin only after publication and explicit ownership grant.
The planned worker paths cover storage initialization, coordinator/orchestrator,
gateway guards, conversation/manual-compaction routes, handoff and voice writers.

Required acceptance includes ready-before-presend, atomic empty-init versus UI
save races, cancelled/failed attachment with no pending turn, exact SID and
identity cleanup, two simultaneous sockets, zero opt-out bypass, current policy
rebuild, next actual provider request after process restart, no old tool replay,
late compaction/delete fencing, all managed writer integration or truthful
refusal, and capability/readiness failure with zero user task submission.

Primary JSON migration, unsupported legacy/manual restore, full voice/handoff
continuity, physical devices and rebuilt native acceptance remain explicit gates.
Storage and controlled orchestration tests alone do not pass those gates.

## Verification

On the existing frozen DATA01B source, the new two-case scaffolding passed:
**2 passed, 5 warnings in 1.10s**. Ruff passed for the new test file. Command:

```bash
cd feral-core
env FERAL_HOME=/private/tmp/feral-data01c-scaffold-home FERAL_DATA_HOME=/private/tmp/feral-data01c-scaffold-home ../.venv/bin/python -m pytest tests/test_runtime_context_activation.py -q --no-cov -p no:randomly --timeout=60
```

Warnings are the existing Pydantic field-name and FastAPI lifespan deprecations.
The fixtures use a fake provider and disposable SQLite; no real model or account
is contacted. Production activation is not enabled. These tests establish the
old refusal and read-only boundary, not the proposed attachment implementation.
