# Read-only type ratchet triage, October 2, 2026

Full backend source/tests were frozen during this audit. Parent's source manifest
has 1,261 Python files and digest
`23fed780ce1e9eef2d1a6af6c2fc2470e295e51ac44a5780b7073d0344523b2f`.
Published comparison source is `634595c7194aedaf6eb9b8c91cbacb87fedea4bd`.
This audit changes only this new evidence file; no core, SDK, native, dependency,
baseline, cached source or personal data was edited. No repair is implemented by
this document.

## Measured result

Using the existing Python environment and unchanged repository `mypy.ini`:

```bash
cd feral-core
../.venv/bin/python -m mypy . --cache-dir=/private/tmp/feral-type-triage-20261002-cache > /private/tmp/feral-type-triage-20261002-current.log
```

This is the full repository ratchet discovery command, not a selected-file alias
that would remap packages. Mypy **1.20.2**, macOS, reported **847 errors in 238
files; 1,261 source files checked**, with a normal final summary and exit 1.
The existing pydantic plugin and missing-import/exclusion policy were preserved.
Disk had 5.2 GiB free before measurement; this used a fresh temporary cache without
copying dependencies or model data. The committed Ubuntu baseline has **812
diagnostic occurrences**, so the local total is **35 above baseline**. This does
not establish the next Ubuntu total or a passing type ratchet.

Parent supplied the verified timestamp-stripped Ubuntu diagnostics for
[run 37071282138, job 111051062407](https://github.com/FERAL-AI/FERAL-AI/actions/runs/37071282138/job/111051062407),
source 634595c: **848 errors in 238 files; 1,257 checked**. The raw job log was
also fetched read-only after the sandbox network attempt failed. The local
source includes four additional ingress/runtime test files. Error comparisons
use `(file, exact message including error code, multiplicity)` and omit shifted
line numbers and timestamp prefixes; notes and summary are not error occurrences.

| Exact message change versus the last Ubuntu source | Occurrences |
| --- | ---: |
| NEW `agents/tool_genesis.py`: sqlite3.connect receives str or None instead of a path | +2 |
| REMOVED `api/server.py`: missing return statement | -1 |
| REMOVED `api/server.py`: Awaitable[str or None] passed to Awaitable[None] spawn helper | -1 |
| REMOVED `api/server.py`: Awaitable[str or None] passed to create_task requiring a coroutine/generator | -1 |
| All other file/message/multiplicity entries | Identical |

The three server repairs are measured in the full graph, consistent with the
parent's narrower package comparison. The two Forge errors are attributable to
new helper methods in this working wave. Therefore neither 848 nor 847 is all
unchanged debt. An expected total of 845 after repairing two errors is only a
hypothesis until another full measurement and the exact-source Ubuntu job.

No default-config error diagnostics occur in the new runtime coordinator,
`api/runtime_context.py`, activation/lifecycle/storage/ingress tests, or changed
chat-turn test fixture. Most pytest functions are unannotated: the default mypy
run explicitly does not check untyped function bodies. This zero-diagnostic
statement is not a claim that every assertion or fixture body was type checked.
Separately, DATA01C's 11-package focused baseline/current comparison measured
66 existing errors each, zero introduced/removed; the full graph above is the
authoritative current local ratchet result.

## Per-file changes from the committed 812 baseline

The following counts are computed from diagnostic lines. Counts alone do not
attribute every message to a recent change. Comparing normalized messages across
the entire baseline/current pair yields **45 added and 10 removed occurrences**,
net +35. Unchanged per-file totals are omitted below, not from the measurement.

| File | Baseline | Current local | Delta |
| --- | ---: | ---: | ---: |
| agents/multi_agent.py | 1 | 4 | +3 |
| agents/orchestrator.py | 29 | 34 | +5 |
| agents/proactive_engine.py | 2 | 3 | +1 |
| agents/tool_genesis.py | 1 | 3 | +2 |
| agents/ui_handlers.py | 1 | 6 | +5 |
| api/routes/channels.py | 2 | 4 | +2 |
| api/routes/mcp.py | 0 | 2 | +2 |
| api/routes/security_and_hardware.py | 3 | 0 | -3 |
| api/server.py | 19 | 15 | -4 |
| api/state.py | 1 | 2 | +1 |
| bridges/acp.py | 2 | 4 | +2 |
| config/loader.py | 2 | 3 | +1 |
| hardware/mesh.py | 2 | 5 | +3 |
| security/vault.py | 1 | 2 | +1 |
| skills/impl/places.py | 0 | 1 | +1 |
| tests/test_failover_endpoint_routing.py | 0 | 10 | +10 |
| tests/test_hr_pipeline_demo_fixes.py | 0 | 1 | +1 |
| tests/test_manifest_trigger_conditions.py | 0 | 1 | +1 |
| tests/test_proactive_freshness_gate.py | 0 | 1 | +1 |
| tests/test_the_agent_can_write_memory.py | 0 | 1 | +1 |
| tests/test_voice_realtime_headers.py | 6 | 5 | -1 |

Storage remains at 17 errors and checkpoint/approval/conversation code added no
new normalized diagnostic. The high-count older modules include orchestrator
34, llm_provider 23, sync protocol tests 21 and store 17. Their totals are not a
reason to raise the baseline or hide errors.

## Executable repair cards after the full test freeze

Reserve exclusive files, then implement and measure. No Any, casts, ignores,
blanket constructor widening or baseline regeneration is proposed.

### TYPE01G: new Forge worker database-path narrowing, first priority

Exclusive production file: `agents/tool_genesis.py`. Relevant existing tests:
`tests/test_tool_genesis.py`, `tests/test_tool_genesis_atomic_creation.py`.
Parent has reserved this source for the repair after the running backend suite.

Current line 374 `_stored_target_exists` and line 394 `_insert_new_generated`
pass optional `self._db_path` directly to sqlite3.connect. Their callers check
the path before asyncio.to_thread, but that narrowing is not a contract inside
the worker helper. The newly added methods cause both current-wave errors.
Other persisted helpers already contain local missing-path guards.

Smallest repair: capture `db_path = self._db_path` in each synchronous worker,
explicitly refuse a missing path before any connection, and connect only with
that narrowed immutable local. Keep no-database public creation behavior in the
caller; never substitute an empty path, in-memory database or personal default.
Missing path must not publish a persisted proposal as if its commit succeeded.
An alternative required-string helper argument would be precise, but would also
change existing worker monkeypatch fixture signatures; the local capture avoids
that unrelated churn. Preserve insert-only transaction, readback verification,
duplicate guards, cancellation and uncertain-commit semantics.

Acceptance:

```bash
env FERAL_HOME=/private/tmp/feral-type01g-home FERAL_DATA_HOME=/private/tmp/feral-type01g-home ../.venv/bin/python -m pytest tests/test_tool_genesis.py tests/test_tool_genesis_atomic_creation.py -q --no-cov -p no:randomly --timeout=60
../.venv/bin/python -m mypy -p agents.tool_genesis --follow-imports=silent --no-incremental
```

The existing sandbox import-assignment diagnostic at current line 464 predates
this wave; the acceptance must say whether it remains. Public no-database mode,
two-engine same-target creation, cancellation, commit/readback loss and no live
publication are already covered by the atomic creation fixtures. Do not claim a
global reduction until the complete graph is rerun.

### TYPE01H: app confirmation numeric validation, five older attributed errors

Exclusive source: `agents/ui_handlers.py`; existing acceptance:
`tests/test_app_action_dispatch.py`, `tests/test_apps_e2e.py`,
`tests/test_runtime_context_activation.py`.

Current lines 73, 74 and 78 have five diagnostic occurrences: `isfinite` and
numeric comparisons/subtraction operate on values returned by pending.get,
inferred as Any or None. Existing code requires exact built-in int/float types,
finite timestamps, a bounded lifetime and nonexpired exact owner confirmation.
Move invalid-type refusal before numeric operations, with explicit isinstance
narrowing plus the existing exact-type check so bool/subclasses do not acquire
consent. Then retain finite/lifetime/expiry checks unchanged. Do not coerce missing
or string timestamps, consume foreign requests, or weaken manifest/action
revalidation. Preserve the final existing optional daemon SID error as separate
debt if not assigned. Run the three suites and package mypy, inspecting invalid,
expired, foreign, repeated and queued-context confirmations.

### TYPE01I: provider-error fixtures, ten older test errors

Exclusive file: `tests/test_failover_endpoint_routing.py`. Current lines 524-541
pass None to collaborators annotated as required, assign a mock provider to an
inferred None-only attribute, replace three methods directly, and inspect a mock
recorder through the production Callable type.

Keep retained recorder/provider variables and inject them through real supported
fixture setup; use scoped monkeypatch for method replacement. Supply actual
disposable collaborators instead of broadening production types merely for the
test. Preserve provider-error frames, absent assistant history and stream/failover
endpoint assertions. Run this suite and package mypy. Resolving fixture typing
must not quietly remove the no-final-answer/no-success checks or contact a model.

### TYPE01J: ACP same-call permission scope, two older added errors

Exclusive source: `bridges/acp.py`; relevant existing tests:
`tests/test_acp_permission_context.py`, `tests/test_bridges_acp_client.py`.

Current `_permission_context` line 610 looks up sessions with optional unvalidated
params.get(sessionId); line 617 creates an untyped heterogeneous snapshot. Validate
sessionId as a nonempty string before the lookup, and type the actual captured
rawInput/locations mapping. Do not infer authority from another session, completed
call, title or changed scope. Preserve the two older subprocess pipe-optional
diagnostics at 434/435 unless their lifecycle is separately assigned; do not
download/start OpenCode for this type slice. Run both controlled bridge suites and
package mypy, including missing/wrong session IDs and same-call changed scope.

## Further triage required

Subsequent parent repair implemented TYPE01G with local guarded database paths.
The full unchanged-config rerun measured **845 errors in 238 files, 1,261 checked**
in `/private/tmp/feral-wave4-typefixed-mypy-20261002.log`. Targeted Forge checks
passed 43 tests. This confirms the local two-error reduction; the original 847
audit above remains historical evidence, and the 812 baseline is unchanged.

The remaining changed-file debt needs source-specific cards: multi-agent tool
list/result typing, hardware/session transport, channel routes, config/provider
annotations and older small test fixtures. For example, the two MCP disconnect
errors at current lines 59/63 lose optional dictionary narrowing during the
post-await identity check; a later card must preserve the captured connection
replacement/failure behavior rather than insert an unchecked mutation. The
stream delivery annotation and SDUI delivery proxy in orchestrator are also
older errors: these require a precise sender protocol matching production bool
receipts and legacy None adapters, not a guessed generic Any.

Parent owns shared checkpoints, Git publication and the next remote CI result.
The baseline remains unchanged at 812. The required backend suite was still
running when this read-only report was written; its test evidence is separate.
