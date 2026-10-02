# DATA01C runtime context activation

October 2, 2026. Worker implementation on published base
`634595c7194aedaf6eb9b8c91cbacb87fedea4bd`; parent ingress, boot installation and
native gating are integrated separately. This evidence describes worker source
and isolated acceptance. The staged 9.29 application uses the committed base and
does not yet contain this unpublished activation wave.

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

## Implemented attachment contract

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

`chat.capabilities` remains read-only. Additive readiness metadata includes:
`context_checkpoint_versions`, `context_ready`, `context_managed` (an explicit
boolean even for refused attachments without local opt-in), `context_state`, and, only when
ready, `context_checkpoint` with version, exact SID, generation, revision,
`durable`, `initialized` and explicit omission counts. No raw history, internal
writer attempt, opaque attachment token or credentials appear on the wire.
Omission metadata is immutable internally and copied for wire serialization.
Only the exact query string `1` selects checkpoint version one.
Current policy and system prompts are rebuilt from current configuration.
Checkpoint generation/revision grants no authority or effect-success claim.

The initial typed refusal is sticky for the exact attachment: an absent ledger
cannot downgrade a UI-only/unavailable managed attachment to legacy. Read-only
polling cannot initialize, restore or repair it to READY. The exact-token gateway
guard prevents opt-out through ordinary/tracked send, UI action, manual mutation
or voice activation/audio. Requested-but-missing readiness refuses before working
memory or a provider call. Disabled voice and read-only status/abort remain
available. Gateway tests exercise the real request/response dispatcher and strict
capability booleans.

Cleanup unregisters an exact opaque token, then rechecks attachments and registered
held/queued writers under the same stable SID lock. Queued writer registration
protects the unlocked coroutine wake-up interval; no held/queued lock is evicted.
Managed history, working rows, images and session metadata may clear after the
last surface, while SQLite context survives. Explicit legacy cleanup preserves
the old learner behavior and rechecks both attachments and queued writers after
the awaited learner callback. Current installed legacy passthrough applies only to a
truly unknown SID; existing checkpoints cannot opt out.

Native must retain drafts and attachments and perform zero presend saves or task
submissions until strict readiness evidence. Read-only task-status reconciliation
must remain available when context is unavailable or pending. A cached initial
READY claim cannot substitute for checking the current committed fence.

## Writer closure inventory

| Writer | Required activation boundary |
| --- | --- |
| Tracked, legacy web and device text preparation | Existing SID scope before working-memory prelude; await checkpoint commit before terminal/context-ready |
| Command wrappers and tool approvals | Same coordinator; preserve exact task/session/approval identity and unknown-effect semantics |
| UI confirmation, generic tool/app action and command fallback | Whole helper fenced before consuming confirmation or executing effects; exact same-task owner reuse and one command handoff |
| Primary greeting and primary JSON hydration | No silent plaintext or UI import; explicit legacy/migration gate |
| Voice router and transcript hooks | Explicit managed-thread refusal before provider/transcript effects; original legacy bodies preserved |
| Realtime voice tool summary | Managed OpenAI/Gemini tool handlers refuse before execution; codec authority is not broadened |
| Automatic/manual compaction | Managed auto-compaction skipped and manual compaction refused; legacy swap compares captured prefix, not only length |
| Manual snapshot, restore, reset, branch and deletion | Managed source/target refusal before mutation or auto-source persistence; legacy bodies remain locked |
| Session handoff | Managed source/target refusal before queue, transfer or notification |
| Disconnect, stale eviction and images | Exact attachment identity/refcounts plus same SID lock; recheck current attachments; never pop held/queued locks |

Production realtime currently writes a working-memory tool summary shaped as
`role:tool`, `tool`, `result_summary`, while the strict storage codec accepts
working user/assistant text. This gap requires trusted writer normalization or
explicit refusal before managed realtime voice is advertised. This card selects
refusal. Full managed voice/handoff/manual operations require follow-up trusted
writer/fencing work; their refusal is a bounded activation gate, not feature parity.

Tool approval resolution now consumes grants and executes within the exact pending
request's SID fence. Success awaits checkpoint commit. Registered HTTP routes
return redacted typed 409 before effects or 503 with outcome unknown after an
unverified commit; both specify retry_safe false. No old tool/request is replayed.
The real resolver tests inspect IN_PROGRESS before a controlled fake effect.

Source review found `ui_handlers` confirm/call/app skill paths execute ToolRunner
before the command fallback and `_send_text` updates turn/audit metadata.
`Orchestrator.handle_ui_event` now fences the whole helper before these effects.
It reuses only an existing exact-task/SID owner; inherited child-task context is
not ownership. The fallback may enter one command within the same scope. No new
lock or general reentrant execution is introduced. Actual confirm/call helper
tests inspect pending context before their controlled effect; unverified commit
leaves pending context and refuses repetition without replaying the effect.

## Ownership and acceptance

Parent owns server/state/runtime-ingress/chat-turn integration and native gating.
Worker-owned production paths are `memory/store.py`,
`agents/runtime_context_checkpoint.py`, `agents/orchestrator.py`,
`gateway/protocol.py`, `api/routes/conversations.py`, `api/routes/memory.py`,
`api/routes/approvals.py`, `agents/session_handoff.py`, `voice/router.py`,
`voice/realtime_proxy.py`, `voice/gemini_realtime.py`; also activation tests and this
evidence. Published DATA01A/B tests, SDKs and staged resources were preserved.
Parent owns publication and the integrated frozen acceptance result.

Required acceptance includes ready-before-presend, atomic empty-init versus UI
save races, cancelled/failed attachment with no pending turn, exact SID and
identity cleanup, two simultaneous sockets, zero opt-out bypass, current policy
rebuild, next actual provider request after process restart, no old tool replay,
late compaction/delete fencing, all managed writer integration or truthful
refusal, and capability/readiness failure with zero user task submission.

Parent reports boot installation after Orchestrator.set_llm and before channels
and legacy primary hydration; primary lookup failure cannot choose plaintext
fallback. Its main/device attachment, cleanup and native gates need their own
integrated acceptance. Primary JSON migration/privacy remains deferred; no
personal file is cleaned or migrated. Managed learner/identity maintenance,
voice/handoff/compaction/deletion, SDK restart negotiation, encrypted-memory product
policy, physical devices, Linux/CI and rebuilt native acceptance remain gates.
UI full-blob races/tombstones and 500-row retention remain CORE02: save acknowledges
input count, not exact retained readback. UI metadata never becomes model authority.
Storage and controlled orchestration tests alone do not pass those gates.

## Verification

The initial two-case scaffolding on the DATA01B source passed 2 tests. After the
worker implementation and queued-writer repair, the 17-suite regression passed
**305 tests, 9 warnings in 9.88s**, before the final gateway metadata/guard changes.
That command included activation/lifecycle/storage, gateway/handoff/voice/realtime,
approval/compaction/conversation, stream parity and durable turn receipt suites.
After final gateway changes, actual gateway/activation fixtures passed
**45 tests, 5 warnings in 2.58s**. After the final learner recheck and whole UI helper
scope closure, the frozen five-suite regression passed **122 tests, 6 warnings in
2.79s**. The parent full integrated run must supersede the intermediate broad
results. Final worker command:

```bash
cd feral-core
env FERAL_HOME=/private/tmp/feral-data01c-ui-final2-home FERAL_DATA_HOME=/private/tmp/feral-data01c-ui-final2-home ../.venv/bin/python -m pytest tests/test_runtime_context_activation.py tests/test_gateway.py tests/test_app_action_dispatch.py tests/test_apps_e2e.py tests/test_api_apps.py -q --no-cov -p no:randomly --timeout=60
```

Ruff passed all 12 owned Python paths with repository CI selections
`--select=E,F,W --ignore=E501,E402,F401,W291,W293`; scoped diff check passed.
Package-named mypy with silent followed imports reports **66 existing errors on
both committed baseline and current source**, zero added/removed diagnostics
compared by file/message/multiplicity rather than shifted lines. This is measured
macOS no-regression evidence, not a clean global mypy or Ubuntu claim. Existing
counts: orchestrator 34, store 17, gateway 8, OpenAI realtime 3, router 2, Gemini 1,
memory route 1; coordinator, approval/conversation routes and handoff have none.

Implementation failures were corrected and rerun: a quota test caught lock
allocation before quota refusal; the original reservation ordering was restored.
A new HTTP fixture called a keyword-only snapshot method positionally; only the
fixture was corrected. Helper extraction initially added three narrowing errors;
checked local references removed them. Parent ingress tests found a refused token
downgrading to legacy, fixed by sticky exact-token refusal. Resource review found
queued-writer eviction in the unlocked wake-up interval; registrations and a
controlled regression closed it before the 305-test run. Final review found the
learner could await while a new writer queued; a post-await writer recheck and
regression closed it. The new UI owner-reuse test initially expected one revision
increment, corrected to inspect the actual pending +1 and committed +2 CAS
sequence; its rerun passed. Earlier failing runs are not acceptance.

Warnings are existing Pydantic/FastAPI/Starlette deprecations, two existing
realtime GA AsyncMock warnings and fixture environment-restoration warnings.
Fixtures use disposable FERAL_HOME/DATA_HOME and SQLite with fake providers and
explicit fake effects. No real model, account, payment or personal profile is
contacted. No full-app release, native/device or CI claim follows from this slice.
