# Runtime reliability implementation

October 4, 2026. This extends the existing runtime after the
[independent review](RUNTIME_REVIEW_20261004.md). It does not replace the executor,
memory store, scheduler, voice protocol or native host.

## Behavior and compatibility

- Scheduled skill actions use the central ToolRunner and SkillExecutor with
  session, cron surface and persisted run-attempt identity. Policy evaluation or
  run-record failure prevents dispatch. DENY remains final. CONFIRM cannot use a
  payload boolean, loose mode or standing interactive permission as an exact
  scheduled grant, including at the final executor recheck. Durable scheduled
  approval remains a separate incomplete contract.
- Scheduler occurrence claims commit before the callback with a unique job/due
  identity. Unfinished or uncertain claims never expire into replay. Known callback
  return permits bookkeeping-only recovery, with schedule/count and recovery marker
  in one transaction. Both startup catch-up and normal polling consult the journal
  before lateness/rearming. Callback return is not verified external success;
  errors caught inside the server callback still need action-receipt propagation.
- Agentic computer actions use registered central dispatch, including capture.
  Missing authority prevents capture and model setup. Outer task approval does
  not approve different inner actions. Unsupported drag and shell commands return
  unavailable; supported literal application activation uses its registered tool.
  A refused, pending or uncertain action stops the bounded loop.
- OpenAI and Gemini realtime output retains the executor receipt, falsy data,
  errors, uncertainty and approval correlation instead of selecting data alone.
  Output is bounded and named credential/card fields and recognized credential/PAN
  text are redacted. This cannot identify every secret in arbitrary prose.
  Receive-loop concurrency and physical voice acceptance remain open.
- Already-incurred model usage is committed before a cap-crossing warning.
  Events and rollups commit atomically; a failed write rolls back both and leaves
  the published cache unchanged. Cancellation at transaction admission rolls back
  before ledger reuse. Clock-window rollover does not retain expired cached spend.
  Concurrent settlement against an initialized
  shared ledger reads the persisted total. Configured caps refuse model admission
  when accounting is unavailable; explicitly disabled/unlimited behavior remains.
  This does not implement concurrent pre-dispatch cost reservations. Simultaneous
  initial WAL/schema setup is not covered by the settlement change.
- ToolRunner children return incomplete for empty output or tool-only iteration
  exhaustion, retaining attempted tool counts and without automatic effect replay.
  Completed identifies a produced final answer; verified external outcomes still
  require their own tool/action receipts. Existing aggregate dispatch success is
  separate from the number of completed children.
- Learning selects committed managed-checkpoint user rows, excludes generated,
  tool, ambient and explicitly provisional input, and protects no-model and failed-
  model heuristic fallback. Untrusted extracted subjects cannot resolve through
  aliases/embedding linking into the operator entity. Named third-party relations
  remain supported. New relation provenance uses the existing source_origin field;
  legacy ABSENT sessions retain explicit operator-ingress behavior. Full authenticated
  message/owner stamping and provenance through checkpoint/merged relations/sync
  remain open. AgentWorker retrieval receives the current query at its existing
  300-token budget; no unmeasured budget increase is introduced.

## Frozen combined verification

The independent cards are implemented. Focused source checks precede the parent
frozen combined gate recorded below; their overlapping counts must not be summed
into a product-coverage percentage.

- Parent combined gate: 1,405 passed, two opt-in skips, 54 warnings across 72
  affected and shared-boundary suites in 29.20 seconds. Live/network-opt-in tests
  were excluded. Disposable process-level profile, hash embeddings and null
  keyring prevent personal deployment/account/model use.
- All 1,317 Python production/test inputs were unchanged before/after the run:
  source digest `fd7571150505b742900ae8b11e13ed83277c524fa565b1fdab6e08e356314d06`.
  Private machine receipt: `/private/tmp/feral-reliability-integration-final-20261004/result.json`.
- Independent review caught and reproduced normal-poll recovery ordering,
  cancelled SQLite BEGIN cleanup and denied operator-alias mutation. Regressions
  now cover both scheduler entry points, cancellation after BEGIN/commit, and
  operator aliases/mentions/timestamps remaining unchanged after denied extraction.
- Warnings include existing schema/FastAPI, numerical/fixture and environment-
  restoration diagnostics. The initial combined cancellation fixture failed
  because it replaced aiosqlite's context-manager interface; its scoped fault
  injection was corrected and the complete frozen gate rerun. Initial process
  isolation and first-WAL concurrency fixture failures remain test-harness limits,
  not additional app acceptance evidence.
- Source verification alone claims no physical/account outcome. Subsequent
  [9.41 packaging](NATIVE_9_41_ACCEPTANCE.md) incorporates these fixes with separate
  payload and backend startup evidence. Remote CI belongs to its exact published
  commit and remains a separate gate.
- Repository Ruff check passes. Full local mypy completes with 809 errors in
  233 files across 1,317 inputs; this is an outstanding repository type-check
  limitation, not a type-clean acceptance result. The platform-specific public
  baseline is unchanged.
- Mintlify navigation passes for all 69 pages. The publication-only leakage scan
  passes for tracked documents and this new ledger. The unfiltered working-tree
  scan still flags three ignored historical local documents; they are not staged
  or shipped and remain untouched.

## CI reconciliation

Subsequent remote CI identified one unchanged liveness assertion expecting a
routine to execute after its run-identity insert fails. The failure reproduced
locally. The corrected regression checks zero untracked dispatch and successful
progress of a later independent routine after bookkeeping recovers. Production
runtime and CI configuration remain unchanged. The complete reconciled local
gate passes 1,418 tests across 73 suites with two skips and 61 warnings in
40.11 seconds; all 1,317 Python inputs remain frozen with digest
`41db6cbc4ce20409129588df55d26857a552e1086f3dd5989b9efb54e7379943`.
Receipt: `/private/tmp/feral-reliability-integration-941-reconciled-20261004/result.json`.
The preceding remote run passed 13,911 other tests and 75.89% coverage with
86 skips. New remote acceptance is pending; these counts are not summed.

## Remaining dependencies

1. Persistent input/output/audio cost reservations and settlement/reconciliation
   across retries, failover and cancellation; checkout quote reservations.
2. Responsive bounded realtime intake with epoch/publication/cancellation fences;
   continuous same-session voice and physical headset/speaker/noise tests.
3. Exact durable grants and external-effect uncertainty propagation, detached jobs,
   bounded resource concurrency and opt-in single-writer background lifecycle.
4. Installed control/speech dependencies, guided local/cloud model provisioning,
   truthful provider capabilities and actual model-driven Mac acceptance.
5. Message receive/dedup/delivery, offer cards, authorized merchant checkout and
   receipts, glasses/iOS handoff, proactivity and cross-owner social permissions.
6. Native parity, fresh installation, migration/update recovery, signing and
   developer documentation. Coding/OpenCode feature expansion follows the product
   reliability and everyday-use work. Mac leads; Linux expansion/Gen-UI stay deferred.

The retained 9.40 app predates this source wave; 9.41 incorporates it. No account, audio, message,
purchase, download, service registration or GUI acceptance is implied by these
regressions. [Full feature state](WORK_STATE.md),
[release gates](RELEASE_READINESS.md) retain the complete scope.
