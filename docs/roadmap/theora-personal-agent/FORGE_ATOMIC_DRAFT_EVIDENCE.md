# FORGE02A: create-only draft storage

Date: 2026-10-02. Implemented against checkpoint
`634595c7194aedaf6eb9b8c91cbacb87fedea4bd`; worker verification only, pending parent
integration. This closes destructive generation-time replacement in the existing
Tool Genesis engine. It does not implement the proposed durable operation and
central policy dispatch card in [FORGE_OPERATION_RECOVERY_PLAN.md](FORGE_OPERATION_RECOVERY_PLAN.md).

## Implemented contract

- Both legacy creation entry points, `generate_tool` and `propose_from_intent`,
  refuse an existing live signature, existing live tool ID, or persisted signature
  or tool ID before calling the provider. Intent IDs retain their existing
  first-200-Python-code-points MD5 construction. A hash collision is a refusal,
  never replacement. The shared intent/sequence storage namespace is preserved.
- One fixed engine-wide async lock serializes generation, including distinct
  targets and queued callers. No per-target lock cache grows or evicts held locks.
  Cancelling a queued caller or a blocked provider releases only that caller's
  participation. This is live-instance exclusion, not a durable reservation.
- Persisted creation uses the existing `generated_tools` SQLite table without a
  schema change. An offloaded `BEGIN IMMEDIATE` transaction checks both identities
  and performs a plain create-only `INSERT`. Commit and an exact readback of all
  ten existing persisted fields precede `_generated` publication. No await occurs
  between the final live conflict check and publication.
- Storage lookup failure, failed insert, lost commit acknowledgement, or mismatched
  readback returns the existing unsuccessful result (`None`); it does not publish
  a live draft or claim creation success. Cancellation propagates. Logs added by
  this change include the generated identity rather than task text or code.
- In-memory-only engines retain their legacy non-durable behavior with create-only
  live publication. Existing provider calls, shared provider CostBudget handling,
  AST checks, return shapes, approval, promotion, and usage persistence are retained.
  A new draft is pending approval. Creating a draft never approves, promotes or
  executes its generated code.

## Actual verification

Only disposable SQLite databases and controlled providers were used. Generated
fixture source raises if executed; separate approval, promotion and execution
sentinels also reject calls. No external model call, account, purchase, message,
GUI operation, app assembly, staging, or Git mutation was performed by this worker.

From `feral-core`:

```sh
../.venv/bin/python -m pytest tests/test_tool_genesis.py tests/test_tool_genesis_atomic_creation.py -p no:randomly -q --no-cov
../.venv/bin/ruff check --select E,F,W --ignore E501,E402,F401,W291,W293 agents/tool_genesis.py tests/test_tool_genesis.py tests/test_tool_genesis_atomic_creation.py
../.venv/bin/python /private/tmp/feral-forge-route-probe.py
```

Results: **43 passed**, 5 existing warnings, 1.49 seconds; Ruff exit 0. Existing
`test_tool_genesis.py` assertions were not changed. Coverage includes Unicode
same-prefix collisions, approved live identity retention, intent/sequence shared
signatures, stale independent engines, partial live records, foreign-signature
tool-ID collisions, controlled concurrent providers, queued cancellation,
provider cancellation, actual SQLite commit/readback fault injection, worker-thread
IO and publication order, and cancelled callers whose SQLite worker later commits.

The independent-engine race fixture confirms two provider calls are possible,
with exactly one committed/published draft and no destructive replacement. A
subsequent attempt against that committed row refuses before another provider call.
Actual SQLite fault injection commits a row before raising an acknowledgement
error, and separately substitutes a mismatched readback; both leave `_generated`
empty while preserving the committed row. Cancellation during a paused SQLite
worker likewise does not imply rollback or live success.

The probe mounts the actual registered Forge router on an isolated ASGI app,
uses the actual engine and disposable SQLite, and confirms two pending drafts,
400/800-character preview/readback, statistics, malformed/unknown input refusal,
and uninitialized-engine behavior. Approval, promotion and execution are forbidden
by probe sentinels. This is actual-source integration with a controlled provider,
not real-model or installed-app acceptance.

Output logs:

- `/private/tmp/feral-forge-atomic-tests-20261002.log`
- `/private/tmp/feral-forge-atomic-route-contract-20261002.log`

An earlier implementation's cancelled-waiter fixture failed because a weak lock
cache retained a completed task's lock reference. It was replaced with the fixed
engine-wide lock; the final suite includes cancelled and distinct-target waiters.

## Remaining boundaries and next acceptance gates

Parent integration narrowed the optional database path inside both synchronous
SQLite helpers with a captured local and an explicit missing-path refusal. This
removed the two newly introduced type errors. The corrected targeted rerun passed
**43 tests, 5 warnings in 1.67s**; full local mypy measured **845 errors**, still
above baseline 812. An earlier targeted run passed all 43 cases but failed the
global coverage floor because --no-cov was omitted; it is not a green check.

- Independent engines/processes can both call a paid provider before either
  commits. Atomic creation prevents replacement, but does not prevent duplicate
  provider billing. There is no durable generation reservation or operation receipt.
- Cancelling `asyncio.to_thread` does not stop a running SQLite worker. It may
  commit after cancellation, with no live publication. The engine lock is released
  when its caller is cancelled; a retry racing the unfinished worker can call the
  provider again. Once the committed row exists it fences later creation. This
  card adds no automatic retry or claim of exactly-once generation/rollback.
- The legacy `None` result cannot distinguish conflict, failed provider, or
  uncertain storage outcome. A persisted but unpublished draft becomes visible
  on engine reload; do not infer that a missing live preview means no commit.
  Durable read-only operation reconciliation is the separate follow-up.
- Approval flags are still absent from the historical SQLite schema. Existing
  approval/usage metadata persistence remains unchanged, including its legacy
  update implementation. This card does not make historical approval durable,
  migrate old grants, or fix cross-instance metadata-update races.
- Legacy REST and capability-gap paths still require the separate central policy
  dispatch conversion. Create-only storage is not authorization. Existing approval
  and promotion behavior is not certified by these draft-only fixtures.
- Parent integration must run the combined frozen source checks. Installed native
  acceptance, real-provider acceptance and release readiness remain separate gates.
