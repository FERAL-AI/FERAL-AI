# Explicit saved-context recovery after interruption

## Behavior and boundary

An interrupted managed writer leaves its durable checkpoint `in_progress`.
Ordinary continuation remains refused until an explicit recovery acknowledgement.
Recovery restores the privately retained **last committed** model history and
working memory, adds a deterministic notice that the interrupted actions have
unknown outcomes, and rotates generation and attempt identity at revision + 1.
The interrupted task is never replayed. UI conversation rows and durable task
receipts are not rewritten or imported as model context. Unfinished volatile
model rows are not promoted to a completed checkpoint.

Existing pending reviews remain inspectable and explicitly deniable. Managed
reviews are stamped with the trusted runtime generation; an old or unstamped
review cannot execute after recovery. A fresh request requires a fresh review.
This does not create approval, a standing grant, or evidence that an action
succeeded. Current policy, plan mode, lease and exact-owner checks remain in use.

The existing SQLite transaction performs exact-fence CAS, validates the retained
codec, checks byte quotas, verifies the written record before commit, and returns
only after commit. Same-process active/queued writers and accepted tracked turns
refuse recovery. Existing SID locks serialize recovery and new-turn admission.
Late saves using the old generation are rejected. Cancelled recovery surfaces
leave a strongly retained settlement task; no receipt certifies an uncommitted
mutation. Runtime/store replacement during awaited reads or publication refuses
an old-owner response.

## Additive authenticated WebSocket contract

The existing `/v1/session` authentication selects the GatewaySession identity.
Recovery cannot select another thread through request parameters.

`chat.capabilities` advertises `context_recovery_versions: [1]` when supported.
For an interrupted checkpoint, it returns:

```json
{
  "context_recovery": {
    "contract_version": 1,
    "session_id": "selected-thread",
    "generation": "canonical-uuid",
    "revision": 4,
    "attempt_id": "canonical-uuid",
    "state": "in_progress",
    "durable": true,
    "requires_unknown_effects_acknowledgement": true
  }
}
```

Call `session.context.recover` with exactly `contract_version`, `session_id`,
`generation`, `revision`, `attempt_id`, and `acknowledge_unknown_effects: true`.
Version/revision reject booleans and floats; identities are exact, bounded and
canonical. Extra fields are refused. A successful response contains:

```json
{
  "contract_version": 1,
  "session_id": "selected-thread",
  "status": "recovered",
  "context_ready": true,
  "durable": true,
  "replayed": false,
  "action_outcome": "unknown",
  "context_checkpoint": {
    "contract_version": 1,
    "session_id": "selected-thread",
    "generation": "new-canonical-uuid",
    "revision": 5,
    "attempt_id": "new-canonical-uuid",
    "durable": true,
    "initialized": false,
    "omissions": {"history_rows": 0, "system_rows": 0, "images": 0, "working_rows": 0}
  }
}
```

READY capabilities retain the established omission of `attempt_id`; independent
readback matches the exact SID, generation and revision. Unready readiness keeps
`fence=None`; its separate `recovery_fence` is only a recovery preview.

A lost response is reconciled by explicit read-only
`session.context.recoveryStatus`, with the exact original five fence/version
fields and no acknowledgement field. Deterministic UUIDv5 target identities
bind that reviewed old fence to its recovery result without a second ledger.
This identity is data, never authentication or execution authority. Status never
repeats the mutation or the task. It returns `status` as `recovered`,
`not_recovered`, or `superseded`, plus `recovered`, `context_ready`, `durable`,
`replayed:false`, and `action_outcome:unknown`. Only the exact READY target includes
`context_checkpoint`. A later revision is `superseded` and cannot certify the
original ready fence. `not_recovered` is returned only when the durable record is
still the exact original `in_progress` fence; a changed non-target generation is
`superseded`, because current absence of the target cannot prove that recovery
never occurred. Status can repair an interrupted volatile publication of
that exact committed target, without changing SQLite.

Refusals use the existing Gateway error envelope with bounded codes
`context_recovery_invalid`, `context_recovery_session_mismatch`,
`context_recovery_busy`, `context_recovery_conflict`,
`context_recovery_unavailable`, or `context_recovery_<checkpoint-state>`.
`details` retains `retry_safe:false`, `effects_may_have_occurred`, and explicit
`action_outcome`. No context bytes or private exception messages are returned.

## Verification

New `tests/test_runtime_context_recovery.py` uses actual MemoryStore SQLite,
checkpoint coordinators, orchestrator methods and registered ASGI WebSocket/RPC
dispatch with controlled providers. It verifies cancellation/recovery/new-turn
admission; retained committed context; unchanged UI rows; first-turn interruption;
restart; exact stale/foreign/CAS and cross-store collisions; queued/live writers;
a writer arriving during an awaited read; active tracked work; retained cancelled
surface settlement; lost-reply status; superseded status; readback rollback;
quota/deleted/corrupt/future/absent/ready refusal; redaction; unauthenticated remote
refusal; stale and fresh reviews; and runtime/store replacement races.

Pending tool reviews currently live in the existing volatile ToolRunner store.
The restart review negative fixture injects a captured real pending record into
a restarted ToolRunner to verify stale-generation refusal; it does **not** claim
that pending approvals have acquired durable storage.

Run from `feral-core`, with the pinned repository interpreter and disposable
`FERAL_HOME=/private/tmp/feral-saved-context-recovery/home` and
`FERAL_DATA_HOME=/private/tmp/feral-saved-context-recovery/data`:

```sh
../.venv/bin/python -m pytest tests/test_runtime_context_recovery.py tests/test_runtime_context_lifecycle.py tests/test_runtime_context_activation.py tests/test_runtime_context_ingress.py tests/test_runtime_session_checkpoints.py tests/test_tool_runner.py tests/test_tool_runner_exact_approval.py tests/test_tool_runner_executor_admission.py tests/test_tool_runner_call_context.py tests/test_agent_turn_lease.py tests/test_approvals_api.py tests/test_chat_turn_receipts.py tests/test_chat_turn_abort.py tests/test_chat_turn_progress_identity.py tests/test_pending_approval_response.py tests/test_taskflow_dispatch_policy.py -p no:randomly --no-cov -q
../.venv/bin/ruff check agents/runtime_context_checkpoint.py agents/chat_turns.py gateway/protocol.py agents/tool_runner.py agents/orchestrator.py memory/store.py tests/test_runtime_context_recovery.py
../.venv/bin/python -m mypy --follow-imports=skip --no-incremental agents/runtime_context_checkpoint.py agents/chat_turns.py gateway/protocol.py agents/tool_runner.py agents/orchestrator.py memory/store.py
```

Frozen-source results after the status refinement: **417 passed**, 19 warnings,
in 17.47 seconds across the sixteen listed suites, including **42 new recovery
tests**. The sequential two-recovery regression verifies that an earlier target
reports `superseded` after a later generation, with zero durable mutation.
Targeted Ruff and
`git diff --check` pass. Six-file mypy exits 1 with **45 existing diagnostics**;
the same command against exact `d2158fc6a` baseline sources produces the same
45 normalized module/message diagnostics: **0 added, 0 removed**. This targeted
comparison uses `--follow-imports=skip`; full-graph type verification remains a
separate integration check. Logs are in `/private/tmp/feral-saved-context-recovery-final-refinement.log`,
`/private/tmp/feral-saved-context-recovery-mypy-baseline.log`, and
`/private/tmp/feral-saved-context-recovery-mypy-refinement.log`. The prior
416-test run remains in `/private/tmp/feral-saved-context-recovery-frozen.log`. These
fixtures perform no real account, browser, purchase, message or model-download
operation. They do not certify the native packaged application or full repository
type graph.

## Remaining limits

- Local writer exclusion and durable CAS cannot stop an external operation
  already issued by another process. Its outcome remains unknown; no rollback is
  claimed. Operating multiple live runtimes against one database needs a separate
  process ownership/lease contract before it can claim exclusive execution.
- Recovery restores the last committed model view, not unfinished tool rounds.
  The original conversation UI and action receipts remain the inspection sources.
- Historical codec omission counts may be recalculated when adding the notice;
  no image bytes, policy, grants or transcript imports are restored.
- Oversized or unsupported/corrupt/deleted checkpoints remain refused. This is
  an interruption recovery operation, not an arbitrary reset/import endpoint.
- An acknowledgement or context-ready fence certifies saved context only. It
  does not mean that the interrupted task was delivered, read, executed or paid.
