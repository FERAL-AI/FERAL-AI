# Mac 9.43 exact-source acceptance

Updated October 4, 2026. Historical artifact record. [9.44](NATIVE_9_44_ACCEPTANCE.md)
is canonical; immutable 9.43 is the sole generated rollback and its failed status
probe remains below. Inspected unused 9.42 was retired after the new assembly.
[9.42 acceptance](NATIVE_9_42_ACCEPTANCE.md) preserves its actual native warning/Quit
evidence. Retention figures below describe the 9.43 assembly checkpoint. No
personal deployment or model/profile data was removed.

## Identity and assembly

- Version `2026.9.43`, build `2026100407`, arm64/macOS 13 minimum.
- Runtime source `0604ddf970a70c0e116f7418787299bd04e4edd2`, published in
  [draft PR310](https://github.com/FERAL-AI/FERAL-AI/pull/310).
- Native binary SHA256
  `0b31937ad7e35c5e9eba2245042470be0c9f35989e695a51932798c0c68b4b8c`.
- Manifest SHA256
  `79d58b9ec5b6692baa197115864dd3c15a460000a802b694eb678f5285c1506d`.
- All 496 packaged production Python files match the exact committed source.
  The bounded audit records 12,934 entries, 265 Mach-O files, nine contained
  symlinks and zero issues. Strict deep ad-hoc signature verification passes.
- All 65 assembly inputs match Git and stay unchanged. The 52 Swift inputs and
  compiler script are identical to freshly compiled 9.42. Assembly reuses that
  verified optimized binary and signs the new bundle; no new Swift compilation
  is claimed. Compile provenance remains source `6947bbec1`.
- Mac staging acquires the nine additional dependencies pinned by the unchanged
  lock and includes the existing desktop extra. All 11 supported desktop imports
  pass both staging and bundled Python acceptance. Python 3.11.15, SQLite 3.53.1
  with FTS5 and pinned OpenCode 1.18.10 remain contained in the bundle.

The canonical app is `desktop-native/build/FERAL Native Preview.app`. Its size is
594 MiB. The sole rollback is 593 MiB. Free space starts at 26,951,151,616 bytes
and ends at 26,872,213,504 bytes in the assembly receipt; net growth is
78,938,112 bytes. Assembly takes 38.50 seconds. The 2 GiB resource check measures
net growth after inspected retention cleanup, not peak disk usage. Dependency
imports prove readiness only; no input, screenshot, clipboard or permission
operation is part of this acceptance.

## Source and runtime checks

Frozen parent integration passes 1,962 tests across 100 suites with two opt-in
skips and 55 warnings in 51.55 seconds. All 1,325 Python inputs stay unchanged;
digest `241e37798db9e41638ae1debf19e9bc26c6b47d6890ca93fdd610f3420be1891`.
Full local mypy completes with 800 existing errors, zero normalized additions
including diagnostic multiplicities and one optional-return error removed. Ruff,
24 script tests, shell syntax, documentation navigation and publication wording
checks pass. Independent TASK review passes 86 scoped tests with network
connections prohibited; independent INSTALL review passes 24 script tests.

Actual bundled backend startup uses an isolated empty provider/profile fixture.
Health becomes reachable in 2.07 seconds and setup returns HTTP 200 with
`setup_complete=false`. Closing the host lifeline stops the owned child with
SIGTERM and closes its listener; fallback termination is not used. This is one
startup observation, not an inference or latency benchmark.

Packaged task-handoff verification ran with this bundle's own interpreter and
isolated TaskFlow/MemoryStore. Creation, exact-call replay and changed-term
refusal reached the actual registered executor. The first probe had a fixture
envelope assertion mismatch; its failure evidence is retained. The corrected
probe then found a real adapter defect: successful scoped status reads include
`error=null`, which the Python backing mistakenly treats as an operation failure.
Its public envelope reports `success=false` despite valid status/origin data.
The full handoff acceptance is therefore not passed. Two source regressions
reproduce the issue before its narrow correction for the next build. 9.43 remains
immutable; it is not edited in place or claimed fully accepted.
No new native GUI acceptance is claimed by unchanged Swift input equality.

## Behavior and remaining gates

The package contains in-process background task transport, atomic durable
exact-call creation, origin-scoped inspection and preserved execution surfaces;
passive provider discovery stays offline on a cold cache. The detailed source
contract is in [installation/task handoff evidence](INSTALL_AND_TASK_HANDOFF_EVIDENCE.md).
Accepted/stored is distinct from action authorization, execution, outcome and
result delivery. Default approval-origin transfer, input revision, subscriptions
and service ownership remain separate cards.

The preceding corrected source `e35dd9af9` has 18 successful remote checks and
four conditional skips. Source `0604ddf97` finishes with 17 successful checks,
four conditional skips and one failed backend check: three delegated-endpoint
contract-inspection failures and a cancellation fixture timing assertion. Actual
registered routing remains reachable. These corrections require a new source
and remote result; prior checks are not assigned to this candidate.

Selected-window desktop ownership/watch/Stop, real model-chosen desktop action,
continuous physical voice, fresh-user installation, missing local model/speech
assets, personal accounts, Messages delivery, payments, glasses and signed
distribution are not certified here. No new microphone, capture, personal account,
message, payment, model download or service registration was performed.

Small private receipts are retained under the local task evidence directory:
`feral-native-9-43-build-20261004/{inputs,result,bundle,core}.json`, its stage/native
logs, `feral-candidate-9-43-manifest.json`, and
`feral-943-backend-startup-evidence.json`. These names are evidence locations, not
shipped runtime data. The full scope and next cards remain in
[WORK_STATE](WORK_STATE.md) and [release readiness](RELEASE_READINESS.md).
