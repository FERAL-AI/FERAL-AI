# DATA01A runtime checkpoint storage

October 2, 2026. Storage primitives on published base
`66c7cd500d7ec353c68da97f44b99d6201af9abe`, plus the four owned files below.
This is not a claim of model context continuity in the running app.

## Scope and ownership

- `feral-core/memory/runtime_session_checkpoint.py`: internal codec and immutable
  typed context, fence, record, result, limits and status definitions.
- `feral-core/memory/store.py`: additive schema and checkpoint methods only.
- `feral-core/tests/test_runtime_session_checkpoints.py`: disposable SQLite and
  codec tests.
- This evidence file.

No native app, packaged resource, SDK, orchestrator, route or state changes belong
to this card. Existing task receipt methods remain unchanged. Schema observation
version is eight; boot adds the new table without replacing existing tables.

## Frozen internal contract

`CheckpointFence(session_id, generation, revision, attempt_id)` validates exact
nonempty SID, at most 1,024 characters with no Unicode control characters or
leading/trailing whitespace, canonical UUID generation/attempt and a positive
SQLite-compatible revision. Leading/trailing whitespace follows CORE04 and the
generic SDKs: reject without trimming, preserving internal spaces as exact
identity. Unicode category Cc rejection is preserved and is stricter than the
current transport's ASCII-control/DEL rule. The fence is an internal
concurrency token, not an owner grant.

All methods return `CheckpointResult(status, record?)`. A record has a fence,
state, timestamp and optional immutable `CheckpointContext`. Read exposes context
only for a validated **ready** record. Getter lists are independent parsed copies.

| Method | Contract |
| --- | --- |
| `runtime_checkpoint_read(session_id)` | Exact SID. Absent, ready, in_progress, deleted, corrupt or unsupported; no fallback to primary/UI context. |
| `runtime_checkpoint_begin(session_id, *, attempt_id, expected=None)` | Absent needs no expected fence. Ready needs its exact fence and a new attempt. Generation stays stable, revision advances, state becomes in_progress. |
| `runtime_checkpoint_commit(fence, context)` | Exact pending generation/revision/attempt required. Validates codec and byte quotas before committing ready with the next revision. |
| `runtime_checkpoint_delete(fence)` | Exact ready/pending fence required. Clears payload, advances revision and leaves a deleted tombstone. |

Mutation success is `applied`; CAS mismatch is `conflict`. Other explicit results
include `quota`, `invalid`, `absent`, `in_progress`, `deleted`, `corrupt` and
`unsupported`. Identity/limits validation raises redacted
`CheckpointValidationError`; SQL failures propagate after rollback. Nothing here
executes a task or claims an external effect succeeded.

The transaction is `BEGIN IMMEDIATE`: read, comparison, quota decision and write
are serialized across connections and independent stores. Retried stale commits
cannot overwrite ready data or resurrect deletion. Pending records cannot be
automatically begun again. A rejected save leaves the exact pending fence intact;
the prior safe bytes, if any, remain private, not restorable as ready context.
On restart, in_progress remains an explicit unfinished state. Resolving that
state requires a later reviewed lifecycle/reconciliation contract.

## Codec, quotas and privacy

Format one contains only normalized model history, working text and omission
counts. `encode_context(history, working, *, limits, tool_image_call_ids)` is
internal, intended for trusted runtime writers. Shape validation alone cannot
prove server provenance; keeping it off public transcript/import/sync routes is
the authority boundary. Do not expose arbitrary checkpoint setters.

History supports user/assistant text and complete OpenAI assistant tool-call /
tool-result groups. Calls must have IDs and function name/arguments; duplicate
or orphan results and incomplete groups are omitted with explicit counts. Row
trimming preserves a contiguous suffix of indivisible tool groups. Historical
tool calls are data and must never be dispatched during restore.

Image blocks retain a text marker saying the image is unavailable, with no URL,
binary or base64 persisted. The later writer must pass referenced out-of-band
tool image IDs from the existing image cache; these results receive the same
marker. The codec cannot discover unrelated side-cache state on its own.

Current routing/system rows are omitted. A server consolidation summary with the
existing raw-turn watermark is demoted to clearly historical assistant data;
its watermark is dropped. Current prompts/policy must be rebuilt by the runtime.
This field recognition is not proof that arbitrary incoming summaries are trusted.
Unknown history/working metadata and root fields are rejected. No UI title,
surface, grants, approval, owner or sync authority is imported. Existing server
greeting `content` and voice working `source` shapes normalize to text; source
metadata is dropped.

Production ordinary tool rounds (`agents/orchestrator.py` assistant construction
near line 3,779) copy tool calls but add content only when parsed text is truthy.
Streaming construction near line 4,519 also omits empty text. Provider
`content:null` therefore becomes an assistant history row with omitted content;
the codec accepts that actual writer shape and normalizes it to empty text.
An explicitly persisted null is refused, not silently repaired. A controlled
actual-Orchestrator test uses the actual provider parser and verifies a null
provider response followed by a tool result and final answer encodes correctly.

Transport caveat: the existing WebSocket entry point still trims its query SID
before gateway binding (`api/server.py` near line 2,429). That preexisting
normalization is outside this storage-only slice; later runtime integration must
reject an invalid raw query identity before binding rather than alias it.

Defaults and hard maxima are 200 history rows, 50 working rows, 512 KiB per encoded
context, 64 MiB combined context payloads and 1,000 SID rows. Smaller constructor
`runtime_checkpoint_limits` are supported without changing existing callers.
Row quota includes deleted, pending and corrupt rows; there is no automatic
eviction. Total-byte accounting uses actual UTF-8 SQLite BLOB lengths, not trusted
declared sizes. Checks and writes occur in the same transaction. These quotas
bound context payloads and metadata through SID length/count; they are not an
exact bound on physical SQLite/WAL allocation or unrelated memory tables.

Storage lives inside the existing `memory.db`; no context JSON sidecar is added.
The table is excluded from `SyncEngine._SYNC_ALLOWED_TABLES`. It inherits the
existing operator-selected whole-file at-rest encryption envelope. This does not
introduce always-on encryption: SQLite plaintext exists while the runtime runs,
as documented by `memory.at_rest`. The existing primary JSON privacy/migration
gap remains deferred; this card does not delete or migrate an operator's file.

## Actual checks

Tests use disposable paths and explicit disposable FERAL_HOME/FERAL_DATA_HOME.
The encryption test supplies an in-memory 32-byte fixture key through the existing
envelope, never an OS keychain/account. It encrypts this new table, checks the
fixture canary is absent from ciphertext and reopens the decrypted database.

Coverage includes real SQLite close/reopen, exact-SID isolation, immutable caller
copies, stable generation / changing revision and attempt, stale saves/deletes,
pending restart with no auto-resume, permanent tombstones, concurrent independent
store CAS/row/byte quotas, retained safe bytes after quota refusal, real SQL write
failure rollback, cancellation held before commit, corrupt/future formats, strict
metadata, tool group pairing, row caps, working caps, image omission, compaction
summary demotion and unchanged UI/receipt separation.

Prior storage-source combined check: **121 passed / 11 warnings in 7.88s**,
before the later identity and actual-writer tests.
Warnings include existing model/FastAPI deprecations, receipt-limit environment
restoration, and event-loop-closed worker warnings surfaced by the older memory
and pool-release suites. They are reported rather than suppressed. Ruff passed;
codec mypy passed. The equivalent CI skill-import boundary scan found 32 nonempty
skill implementation files and zero violations; AST inspection confirms the new
codec imports standard-library modules only.
Prior isolated new suite: **49 passed / 5 warnings in 1.57s**, only existing
model/FastAPI warnings, no worker-thread warnings in this run.
Final identity/writer correction on frozen owned sources: **62 passed / 5 warnings
in 2.00s** with the isolated checkpoint command, Ruff passed and codec mypy passed.
New cases cover leading/trailing ASCII and nonbreaking whitespace, Unicode C1
controls, exact internal-space identity, the actual buffered history writer with
provider null content, and explicit persisted-null refusal. The 121-test combined
result above predates this correction; parent combined verification is separate.

```sh
cd feral-core
env FERAL_HOME=/private/tmp/feral-data01a-home \
  FERAL_DATA_HOME=/private/tmp/feral-data01a-home \
  ../.venv/bin/python -m pytest tests/test_runtime_session_checkpoints.py \
  tests/test_memory.py tests/test_memory_pool_release.py \
  tests/test_session_snapshot_trailing_edge.py tests/test_chat_turn_receipts.py \
  tests/test_chat_turn_abort.py tests/test_chat_turn_failure_wire.py \
  -q --no-cov -p no:randomly --timeout=60
../.venv/bin/python -m ruff check --select=E,F,W \
  --ignore=E501,E402,F401,W291,W293 memory/runtime_session_checkpoint.py \
  memory/store.py tests/test_runtime_session_checkpoints.py
../.venv/bin/python -m mypy memory/runtime_session_checkpoint.py \
  --follow-imports=silent --no-incremental
../.venv/bin/python -m mypy memory/runtime_session_checkpoint.py memory/store.py \
  --follow-imports=silent --no-incremental
```

Local codec mypy passed. Store comparison at the published base and with this
slice both reports the same 17 existing diagnostics; no new checkpoint diagnostic
or suppression/baseline increase was added. The full repository type gate remains
separate. CI architecture source restricts imports from skill implementations;
this card edits no skill implementation and the codec imports stdlib only.

Initial failures were test setup mistakes: importing a class-level sync allowlist
as a module symbol and using a 33-byte fixture key instead of 32. They were
corrected. A command naming a nonexistent test file collected no tests and was
replaced with the actual failure-wire suite. One intermediate run was invalidated
by strengthening a corruption fixture after it started; final evidence requires
a complete rerun on unchanged owned sources.

## Next gates

No runtime currently calls these methods. Before claiming saved model-context
continuity, integrate all exact-SID writers under the session lock, fence before
preparation/action, await commit before a ready receipt, restore only validated
ready context before the next prompt, and coordinate voice, compaction, approved
continuations, eviction, disconnect, delete and manual restore. Rebuild policy
and current surfaces; never derive grants from this context. Model-provider tests
must inspect the actual next request after reopen/restart and prove no old tools
execute. Interrupted state needs explicit reconciliation. Native saved-thread
adoption, primary JSON migration, app rebuild, cold-start and release acceptance
remain separate cards.
