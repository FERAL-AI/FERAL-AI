# Runtime multitasking and admission

October 4, 2026. This builds on the existing
[reliability checkpoint](RUNTIME_RELIABILITY_EVIDENCE.md), preserving the central
executor, managed-turn ownership, exact approvals and occurrence journal.

## Behavior under review

- OpenAI and Gemini receive loops delegate tool work to bounded retained queues.
  One effect lane keeps tool execution ordered while intake can process interruption
  and replacement. Epoch, session and voice-attempt checks fence dispatch and
  publication. Cancellation requests do not prove an external action stopped;
  a cancellation-resistant action retains its lane until it exits. Refusal
  publication is bounded separately and cannot dispatch tools.
- Model calls use persistent per-attempt reservations immediately before wire
  dispatch. SQLite admission counts recorded spend plus unresolved holds across
  instances. Retries and fallback models need separate holds. Undispatched holds
  can release; dispatched calls with uncertain usage require reconciliation and
  never expire into free budget. Known text prices include prompt/tool overhead
  and bounded output. Capped unpriced cloud or multimodal inputs refuse admission;
  explicit local compute has a zero vendor-billing basis. This does not install
  models, verify inference, meter realtime audio or reserve checkout quotes.
  Existing learner/proactive/extraction loop guards still use advisory preflight
  and separately recorded estimates, and some shared-provider calls retain the
  default chat site. Those subsystem attribution/duplicate-estimate paths and
  direct scene-analysis calls are not migrated by this wave. Exact per-subsystem
  accounting remains an explicit follow-up.
- TaskFlow retains up to four independent flow tasks. Intrinsic local work and
  registered, proven notes-memory reads can progress while a slow effect waits.
  Browser, device, HTTP, model and unknown skills share one conservative workflow
  resource lane. Persisted claims, exact review and restart uncertainty remain.
  Queue scans advance across bounded pages; cancellation targets the selected
  flow and shutdown drains owned work. This is single-host workflow concurrency,
  not global locking of every immediate chat or external automation.
- Routine inspection and the jobs aggregator expose claimed, unresolved and
  bookkeeping-pending occurrences, including disabled or older routines outside
  the ordinary queue window. Reads never start the scheduler. Native inspection
  validates the status/occurrence binding and shows unresolved actions as needing
  review. Callback return does not become an externally verified outcome. This
  adds visibility; it does not authorize replay or implement reconciliation.
  Deletion refuses unfinished occurrence records at the scheduler boundary;
  HTTP and skill callers receive an explicit conflict instead of hiding the
  retained action. Pausing future scheduling remains available.

## Acceptance checkpoint

The first frozen combined gate passes 1,765 tests across 88 suites with two
opt-in skips and 62 warnings in 81.68 seconds. All 1,322 Python inputs remained
unchanged, digest
`18f70c79078b1dfa86d1a3aac5e2036d9f7b4517394d90643d2968e85774fd04`.
Receipt: `/private/tmp/feral-multitasking-integration-20261004/result.json`.
Warnings include existing schema/FastAPI, numerical and fixture environment
restoration diagnostics. Live/network-opt-in tests were excluded; the process
used a disposable profile, hash embeddings and null keyring.

The final frozen gate, after optional-row/usage guards and a type-preserving
decorator, passes 1,765 tests across the same 88 suites with two skips and
58 warnings in 121.30 seconds. All 1,322 Python inputs remain unchanged, digest
`ed6966e83739beca13cd4954a9f7a881133374df40226accb45df36c81741221`.
Receipt: `/private/tmp/feral-multitasking-integration-final-20261004/result.json`.
Full local typing completes with 801 errors in 233 files. Compared with the
809-error preceding baseline, there are zero normalized additions, including
diagnostic multiplicities; eight optional-usage errors are removed by the guards.
This is not a type-clean repository. The public platform-specific baseline is
unchanged. Repository Ruff passes.

Native Automation passes 51 fixture assertions, followed by 28 linked model
groups, 39 desktop assertions and five error-presentation assertions. Independent
review reproduced deletion hiding an unresolved action; the scheduler deletion
fence and HTTP/skill conflict regression address it. Worker and independent
test counts overlap this combined gate and are not summed.

The current immutable 9.41 app contains the preceding reliability changes,
not this wave. A fresh optimized 9.42 native compile is planned with exact
committed inputs, offline cached staging, at least 10 GiB free and at most
2 GiB new artifacts. Keep 9.41 as the sole generated rollback and retire only
the inspected, unused 9.40 copy. Packaging and actual GUI acceptance remain
pending until recorded separately.

No physical voice, personal account, message, payment, model download or device
outcome is implied. Continuous native duplex, durable detached jobs and background
service ownership, exact scheduled grants, supported computer-control provisioning,
real Mac task acceptance and clean signed installation remain separate gates.
Coding/OpenCode expansion follows everyday Mac runtime work.

[Current allocation and full feature state](WORK_STATE.md),
[release gates](RELEASE_READINESS.md), [requirements](REQUEST_COVERAGE.md).
