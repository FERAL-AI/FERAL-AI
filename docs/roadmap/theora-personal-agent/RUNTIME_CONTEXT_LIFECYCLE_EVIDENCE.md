# DATA01B first runtime lifecycle slice

October 2, 2026. Published storage base
`cd1571ef94aa2fe244023d56ba468f7ba3266700`, plus four owned lifecycle files.
**Production activation is deliberately absent.** This slice proves the trusted
command lifecycle in isolated fixtures, not saved-thread continuity in the app.
Completing production writer/cleanup integration is the next mandatory card.

## Changes and internal contract

Owned files: new `agents/runtime_context_checkpoint.py`, narrow
`agents/orchestrator.py`, new `tests/test_runtime_context_lifecycle.py` and this
evidence. Published storage files, server, state, routes, voice, handoff, SDKs,
native code and staged app resources are not edited.

`Orchestrator.install_runtime_context_checkpoints(store)` is an internal explicit
installer, never called by current BrainState. It refuses duplicate installation
or installation during an active command. The inactive default preserves current
runtime behavior and minimal headless wrapper instances.

`RuntimeContextCoordinator` receives the existing orchestration SID-lock getter,
history mapping and image callbacks. There is no second lock map. Installed
ordinary/stream command wrappers use `command_scope`; current synchronous
finalization still assembles the history, then scope exit **awaits** the SQLite
commit before returning/releasing the lock. Optional installed cleanup preserves
lock identities rather than dropping a held/queued lock. This does not make
current cleanup/history mutation race-safe; those paths remain mandatory work.

`write_scope(SID, command_handoff=True)` is the later entry-point integration hook:
it restores and fences before preparation and permits exactly one inner command
on that exact task/SID. A second/recursive command or nested writer is refused.
Child tasks inheriting a ContextVar do not inherit ownership; they wait for the
existing SID lock. Independent SIDs remain concurrent. Attempted SID retention is
bounded by the store session quota before allocating a new lock; failed identities
retain their slot, with no automatic eviction/reset.

Ready checkpoints restore model history and working text before a new prompt,
and clear ephemeral tool-image cache. Existing current RAM is preserved only
when its cached exact fence still matches storage. Unmanaged nonempty RAM,
nonempty legacy UI records with no runtime checkpoint, changed ready revisions,
pending/deleted/corrupt/future checkpoints are explicit refusals. UI records are
read only to detect unavailable legacy context and never imported as history.

The guard calls begin before its writer, encodes bounded server context after
finalization, then awaits commit. It verifies exact ready phase, encoded context,
SID, generation, next revision and attempt in the typed commit result. An applied
status with a mismatched/unfinished record is not success. Historical tool image
IDs are passed to the codec so restored data reports those images unavailable.

`RuntimeContextError` has a bounded code, `effects_may_have_occurred` and
`retry_safe=False`. Post-processing commit/quota/deletion failures are not
retry-safe and may have uncertain effects. Cancellation/original writer failures
propagate without committing a ready checkpoint; pending fences require explicit
reconciliation, not request replay. Opaque SQL/encoding commit failures are
redacted as unverified context. Existing caller error presentation must be
integrated before production activation; no new retry suggestion is introduced.

## Authoritative ordering and tests

Required later production ordering:

1. Validate raw exact SID and authenticate/bind the caller.
2. Existing durable chat acceptance may be emitted; it is not context readiness.
3. Take the existing SID scope, restore validated ready context, durably begin.
4. Prepare/refine/push working input inside that scope, then run orchestration.
5. Synchronous finalization assembles canonical rows; await checkpoint commit.
6. Runner returns, existing CORE04 terminal receipt commits, terminal emits.

The actual existing ChatTurnManager is tested with a held checkpoint commit:
no terminal receipt/event exists while it is pending. Successful commit leads to
processing completed with action outcome not asserted. Failed commit after a
begun command leads to outcome_unknown/action unknown and a pending checkpoint.
This verifies the internal ordering; the production server prelude has not moved
inside the scope yet.

Tests use real disposable SQLite, the actual Orchestrator history writer and
controlled providers/tools. Ordinary and stream tests reopen the database and
construct a fresh Orchestrator, then inspect actual outgoing provider messages
for prior user/assistant context plus the new prompt. Separate subprocesses prove
actual process restart: the first executes one controlled fixture tool, the
second receives its historical tool result and executes zero old tools.
Subprocess homes are disposable; keyring wrappers and vault access are replaced
with in-memory fixtures before orchestration imports. No account, network model
request, OS keychain, personal profile or real tool effect is involved.

Additional coverage: simultaneous A/B isolation, ready-before-return ordering,
explicit preparation handoff, child-task lock ownership, cancellation during the
provider and while commit waits, SQL commit failure, total-byte/session quota,
deletion racing processing, changed ready revision, corrupted/future/pending
state, legacy UI/unmanaged RAM refusal and false applied-result rejection.

Final frozen-source combined check: **106 passed / 7 warnings in 7.91s**,
including all 24 new lifecycle cases and existing stream/nonstream, orchestration
and receipt/abort regressions. Ruff and focused lifecycle mypy passed. Warnings
are existing model/FastAPI/Starlette deprecations and environment restoration
from older fixtures; no warning suppression was added.

```sh
cd feral-core
env FERAL_HOME=/private/tmp/feral-data01b-home \
  FERAL_DATA_HOME=/private/tmp/feral-data01b-home \
  ../.venv/bin/python -m pytest tests/test_runtime_context_lifecycle.py \
  tests/test_stream_nonstream_parity.py tests/test_orchestrator.py \
  tests/test_orchestrator_deep.py tests/test_chat_turn_receipts.py \
  tests/test_chat_turn_abort.py -q --no-cov -p no:randomly --timeout=60
../.venv/bin/python -m ruff check --select=E,F,W \
  --ignore=E501,E402,F401,W291,W293 agents/runtime_context_checkpoint.py \
  agents/orchestrator.py tests/test_runtime_context_lifecycle.py
../.venv/bin/python -m mypy agents/runtime_context_checkpoint.py \
  --follow-imports=silent --no-incremental
```

The new lifecycle module passed focused mypy. An exact-module-name baseline
check of the published Orchestrator and current Orchestrator both reports 34
existing diagnostics with no new diagnostic messages. An earlier standalone
temporary-file baseline had two extra class-identity artifacts; the correct
package-name check replaced that comparison. No suppression/baseline increase.

Initial integration regression found two old queued-cancellation fixtures that
construct minimal Orchestrator instances via `__new__`: they lacked the new
optional instance attribute, preventing their active body from entering. A typed
class-level inactive default restores that existing wrapper contract without
weakening the tests. Initial lifecycle mypy found two optional-context alias
narrowing errors; explicit local narrowing corrected them. All test/source edits
finished before the final frozen run.

## Mandatory production closure

Do not install this coordinator in BrainState until these actual writer and
cleanup paths adopt the same lifecycle or explicitly refuse unsupported mutations:

- `api/server.py`: raw query SID rejection, tracked/legacy web and HUP preparation,
  phone chat_request working prelude, greeting, working-clear and detach cleanup.
- `api/state.py`: attach/detach identity and primary JSON hydration/writer migration.
- `agents/orchestrator.py`: approval continuation, realtime voice append, auto
  compaction revision fencing, disconnect, eviction and cache lifecycle.
- `gateway/protocol.py`: reset, compact, snapshot source, branch and restore.
- `api/routes/conversations.py`: branch/restore/delete; arbitrary UI saves remain
  display records, never runtime authority.
- `api/routes/memory.py`: manual compact directly replaces model history today.
- `agents/session_handoff.py`: cross-session working replacement needs ordered
  source/target protection and no overwrite of a live target.
- `voice/router.py`, `voice/realtime_proxy.py`, `voice/gemini_realtime.py`: working
  writes currently precede their history/command hooks and realtime tool effects
  may bypass text orchestration. Voice context injection/restore needs its own
  verified provider path as well as durable history.
- Parent CORE04/error presentation must communicate unavailable/pending context
  without a retry-safe message for already executed uncertain effects.

Primary JSON privacy/migration, trusted manual snapshot provenance, UI autosave
CAS/delete protection, full visual memory and interrupted-context reconciliation
remain explicit gates. Native selected-thread behavior, rebuilt packaged runtime,
clean restart and release acceptance require later integration evidence. No
current app/backend activation, provider-profile change or new cloud service is
claimed by these tests.

CORE02 UI retention remains separate: conversation_save retains the latest 500
rows while its message_count reports input length. A count acknowledgement does
not prove exact retained-row readback; normal single-writer presend still retains
the latest request in that suffix. Full-blob races/deletion tombstones remain
unresolved. Runtime checkpoints never import that UI blob as model authority.
