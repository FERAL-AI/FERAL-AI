# Native 9.36: actual Mac acceptance

Reconciled October 4, 2026 from retained build records, actual GUI actions and
read-only SQLite receipt recovery after an interrupted inspection. This is a
bounded candidate result, not full-product or distribution acceptance.

## Artifact identity

| Field | Value |
|---|---|
| Version / build | 2026.9.36 / 2026100303 |
| Packaged source | `9a40b9ac8e7d155c16a5504a3332c1dca912ad41` |
| Native executable SHA-256 | `0917ea0f14da61056169353e3fb4accfe57e2918a7d10cf2e13aa19c2f235c1f` |
| Manifest SHA-256 | `850dc64e73ebeb7bd0341149e43d61bb785857b789afa2262083442fa7b36ba2` |
| Native input comparisons | 51, unchanged during build |
| Packaged production Python comparisons | 488, matched to source |

The cached, offline dependency stage and optimized native build completed.
Strict ad-hoc signature and bounded runtime audits passed: bundled CPython
3.11.15, SQLite 3.53.1 with FTS5, and OpenCode 1.18.10. The build took 190.63
seconds with approximately 31 GiB free beforehand and a 2 GiB new-artifact budget.
Ad-hoc verification does not establish Developer ID signing, notarization or
clean-machine installation.

Private retained evidence names: `feral-native-9-36-build-receipt-20261003.json`,
`feral-native-9-36-build-inputs-20261003.json`,
`feral-candidate-9-36-manifest.json`, `arithmetic-receipt-readback.json`,
`managed-codeword-receipt-readback.json`, `projection-before-restart.json`,
`restart-review.json`, `relaunch-1/launch.json`,
`relaunch-1/native-exit.json`, and
`restart-receipt-reconciliation-20261004.json`. These identify local records;
personal profiles and raw task transcripts are not published artifacts.

## Observed journeys

The actual app used a disposable home and preferences, the normal shipped tool
inventory, a previously cached local model, and deferred vault initialization.
No personal deployment or external account was used. Tool schemas were present;
these prompts requested no tool execution.

| Journey | Observed result | Boundary |
|---|---|---|
| Avatar / identity | FERAL and Orb selection; portrait/logo rendered | No new avatar animation or marketplace claim |
| Provider onboarding | One reviewed provider stage, connection check, finish review and connected chat | Actual local provider; no cloud login |
| Ordinary arithmetic chat | Durable completed receipt and final text `42`; 71.323 seconds | No saved-context checkpoint on this ordinary turn |
| Saved-context conversation | Durable completed receipt, remembered fixture codeword and checkpoint revision 3; 71.871 seconds | Normal tool inventory, zero asserted external actions |
| Passive health delays | Live turn reached completed terminal despite four approximately five-second probe timeouts | Does not diagnose the cause of probe latency |
| Normal Quit | Exact held host process exited with code 0 | Not a crash/recovery test |
| Reviewed same-profile restart | Restored identity, selected conversation and earlier transcript without onboarding | Prior host/backend absent before one-use reviewed launch |
| Post-restart recall | Durable completed receipt, same remembered codeword, checkpoint revision 5, `replayed=false`; 77.168 seconds | Extra `ACK` violated the requested codeword-only format |
| Restarted host exit | Exact held host process exited with code 0; both host/backend absent at reconciliation | No claim about unknown personal app processes |

Context continuity and durable turn completion therefore passed on this
candidate. Strict response-format compliance did not. Replies taking more than
70 seconds are an unresolved usability issue. Provider first-byte/finish and
event-loop-lag measurements are needed before attributing that latency to RAM,
model loading, host scheduling or request size. Later idle model residency and
swap observations do not establish causation during these turns.

## Separate source and release gates

The candidate still ran automatic skill discovery with self-learning disabled.
Discovery began after the foreground terminal, so this does not explain the
foreground latency. The later [self-learning correction](SELF_LEARNING_SWITCH_EVIDENCE.md)
passes source integration but is not in this immutable artifact.

Exact-source native, desktop, documentation, naming and version CI passed.
General CI run `37170582632` failed one managed-voice disconnect fixture, with
13,361 passed, 83 skipped and 75.45% coverage. The fixture read status immediately
after socket close and assumed a terminal receipt; retained cleanup can still
be settling at that time. A later controlled test distinguishes durable running
from terminal cancellation without weakening no-replay checks. Its correction
is outside this candidate. Web, SDK, extension, lint, syntax, architecture and
type-count jobs passed. The actual brain E2E job was skipped.

Current-candidate GUI Stop, physical microphone/speaker/barge-in, noisy-room
voice, cloud-account use, real computer/browser actions, Messages, payment,
physical glasses, encrypted-profile Keychain, migration, clean installation,
updates and signed distribution remain open. Earlier artifacts' Stop and action
results remain their own evidence. See [current work](WORK_STATE.md),
[release readiness](RELEASE_READINESS.md) and
[the new integration plan](MULTITASKING_AND_EASY_SETUP_PLAN.md).
