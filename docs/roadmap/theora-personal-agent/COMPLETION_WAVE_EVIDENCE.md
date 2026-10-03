# Native daily-use completion wave

October 2, 2026. Source `2d6839ab49cc570959cc3b9057181b0b6c3eba3e` is
published in [draft PR #310](https://github.com/FERAL-AI/FERAL-AI/pull/310).
This report distinguishes source integration, remote CI and the assembled
candidate. It records completed checks,
not a production-release declaration or a delivery forecast.

## Changes in this wave

| Area | Implemented behavior | Evidence and remaining boundary |
|---|---|---|
| Interrupted saved context | Explicit recovery restores the last committed context without replaying the interrupted task. It binds the selected session, generation, revision and attempt, rotates authority and refuses active/queued writers or stale reviews. Uncertain earlier effects remain uncertain. Native review/status handling is integrated. | Actual SQLite and WebSocket fixtures exercise recovery, conflicts, restart, stale approvals and late writers. Current packaged GUI acceptance remains pending. |
| Secure cloud setup | Provider onboarding exposes separate reviewed fresh-vault setup and existing-vault unlock, followed by the existing provider-key save flow. Initialization and read-only unlock use the same coordinator/storage. | VaultSetup31, onboarding34 and existing-vault43 native assertions passed. Backend checks use actual encrypted disk operations with a fake atomic OS adapter. Actual signed Keychain behavior and cloud inference remain unverified. |
| Release acceptance | Fresh initialization requires a version/bundle/team/contract-bound receipt in a verified Developer-ID app. Environment or API arguments cannot enable it; ad-hoc builds remain closed. | Fixture signature/receipt and actual production route/binding checks passed. No production receipt was issued. A valid signature is not proof that Keychain behavior was tested. |
| Offline profile archive | Bounded, hash-checked cold backup and restore preserve config/data layout and private file modes. Restore targets must be nonexistent; failures do not overwrite existing profiles. | Disposable fixtures cover committed SQLite WAL, ciphertext and refusal/rollback cases. This is byte continuity, not automatic active-profile migration, cross-machine key recovery or a native backup interface. |
| Type repairs | Hardware review/controller, worker result/tool lists and the cached places connection have annotations matching their existing runtime contracts. | Seven attributable diagnostics removed; the committed812 baseline is unchanged. Existing type errors remain. |

See [recovery source tests](../../../feral-core/tests/test_runtime_context_recovery.py),
[cloud onboarding evidence](CLOUD_ONBOARDING_EVIDENCE.md),
[profile archive evidence](PROFILE_ARCHIVE_EVIDENCE.md) and
[type release evidence](TYPE_RELEASE_GATE_EVIDENCE.md).

## Integrated backend check

The parent ran the frozen combined backend integration with the pinned interpreter
and disposable FERAL_HOME/FERAL_DATA_HOME under an isolated temporary profile:

**570 tests passed, 19 warnings in 18.99 seconds**, exit 0. This includes runtime
checkpoint recovery/lifecycle/activation/ingress/storage, central authorization and
exact approvals, executor admission and task identity, tracked-turn cancellation,
pending-review behavior, workflow dispatch, profile archive and vault/release
boundaries. It used `pytest -q --no-cov -p no:randomly --timeout=60`.

The final result is retained in
`/private/tmp/feral-completion-backend-final-20261002.log`.
It is a controlled integration check, not a whole-project coverage result. It did
not perform real cloud login, Keychain writes, external messages, purchases,
physical-device actions or native GUI operation.

This final run includes the profile-archive correction that converts temporary-file
creation failure into a typed refusal, with two missing-destination API/CLI tests.
The earlier 568-test run remains historical evidence for the preceding source.

## Frozen type result

The configured fresh-cache whole-core mypy measurement reported **811 diagnostics
in 233 files, 1,269 source files checked**. Diagnostic exit was 1; the unchanged
aggregate count ratchet is satisfied because 811 <= 812. The current PR's Ubuntu
mypy job also passed with the same normalized diagnostics. Existing errors
remain; an aggregate ratchet pass is not zero type errors.

Before/after input manifests contained 1,273 files and matched aggregate SHA-256
`05f734ffd8effc15a3cc579b8e70c2a18e028deb6b4e73a9049eaac596e9e487`.
Normalized exact-message/multiplicity comparison found zero added and seven
removed diagnostics versus the published d215 result of 818. Compared with the
tracked baseline, 15 added and 16 removed occurrences remain, net -1. The lower
aggregate count does not erase those remaining messages. Configuration and the
tracked baseline were not changed.

This final fresh-cache measurement took 13.67 seconds and includes the archive
error-path correction. Logs and before/after manifests use the prefix
`/private/tmp/feral-completion-final-mypy-20261002`.
[Type evidence](TYPE_RELEASE_GATE_EVIDENCE.md) preserves the earlier measurement,
comparison method and remaining diagnostics.

## Prior published-source CI

The full remote backend result belongs to
`d2158fc6a88586aa443f5adf24e471ee7a90d29f`, **before this wave's implementation**:
**12,654 passed,83 skipped,592 warnings,74.77% coverage** in841.15 seconds.
The unchanged required coverage floor is50%. Generic Python SDK112, required web,
Node/device SDK, extension, registry, syntax/Ruff/architecture and native workflow
checks passed on that source. Its non-blocking mypy job still reported818 against812.
[Exact prior-source CI](https://github.com/FERAL-AI/FERAL-AI/actions/runs/37086090592),
[prior-source native checks](https://github.com/FERAL-AI/FERAL-AI/actions/runs/37086090563).

These historical results do not cover the new recovery, cloud setup, archive or
type changes. Current-source verification is recorded separately below. Skipped
live browser/Linux jobs and earlier GUI failures remain separate evidence.

## Current PR verification

[CI 37095062677](https://github.com/FERAL-AI/FERAL-AI/actions/runs/37095062677)
and [native 37095062634](https://github.com/FERAL-AI/FERAL-AI/actions/runs/37095062634)
belong to PR head `2d6839ab49cc570959cc3b9057181b0b6c3eba3e`.
The workflow checkout is synthetic PR merge
`2d8a28713f7dd936ded739067e9823bef2b8b2c5`, combining that head with main
`452a012557d06274063b72546433257b4694d611`.

Confirmed terminal results: native workflow success; mypy ratchet success with
811 diagnostics; web coverage 1,358 tests in 171 files, 65.5% statements and
68.79% lines; API-stubbed Playwright 107 passed. Python/Node SDK, extension,
registry, Ruff/syntax/architecture, assets, naming/version and documentation
checks passed. Live-brain and main-only Linux matrix jobs were skipped.

The full backend job **failed**: 12,749 passed, 1 failed, 83 skipped,
599 warnings in 891.75 seconds; coverage 74.88% exceeds the unchanged 50% floor.
The sole failure is the authenticated GET-route sweep: the newly registered
optional vault-initialization status returned 503 when its controller was absent.
The sweep observed 143 routes, 142 answers, one failure and zero timeouts. This
is an attributable status-contract regression; overall CI is not green.
A source correction must report passive unavailability without granting setup
authority and retain mutation refusal. Its new-head checks remain separate.

## Native artifact and remaining acceptance

The preserved 9.30/build2026100204 app contains source
`dd69c7bf5175517966a363591fa8137782358535`; it does not contain this working wave.
Its earlier real packaged-backend replies, memory restart and cancellation checks
remain valid for that artifact. They cannot certify a subsequently assembled app.

Final production native typecheck and the selected ContextCheckpoint, VaultSetup,
Vault, OnboardingSetup, ChatTurn, Conversation, ChatTools and SessionRecovery
runner passed, including **26 linked-model groups**, 39 desktop assertions and
5 error-presentation assertions. All 87 native source/test inputs stayed unchanged
during verification; manifest digest:
`c6c5c4cacb8dcad971d59313cb4ff336d60c4cbbed3d9c91cfe6e54d74122911`.
Logs: `/private/tmp/feral-native-9-31-final-tests.log` and
`/private/tmp/feral-native-9-31-final-typecheck.log`.

Production recovery uses a bounded private file journal, written and fsynced
before dispatch. Restart cannot treat a ready backend as proof that an unresolved
recovery was checked. Exact read-only status and independent capability readback
must precede matching journal cleanup. Filesystem fixtures passed corruption,
symlink/FIFO/hard-link, mode/size and nonblocking-lock refusal. The linked model
also verifies that a delayed old send failure cannot cancel a newer readback
deadline; the independent timeout actually expires. The journal coordinates
cooperating app instances; it is not signed storage or protection against a
hostile same-user filesystem writer. Duplicate JSON keys are not explicitly
rejected by the decoder.

A new **2026.9.31/build2026100205** candidate contains the published source above.
All 484 production Python files match that commit. Build, strict ad-hoc signature,
bounded bundle/runtime audit and packaged-source checks passed. The audit found
13,095 files, 265 Mach-O objects and 9 internal symlinks; bundled Python 3.11.15,
SQLite 3.53.1/FTS5 and OpenCode 1.18.10 passed their runtime probes.

Executable SHA-256:
`ccf2fa65bec6a0caf4bd88c9cd69515533ec82e91908006f11c808502b31fedb`.
[Candidate acceptance](NATIVE_9_31_ACCEPTANCE.md) records actual outcomes,
including failed attempts. Current GUI acceptance is blocked by Computer Use
native-pipe startup failure. Developer-ID signing/notarization,
clean-machine installation/update/rollback, populated-profile migration, real
provider/audio and enabled account/device acceptance remain independent gates.
There was no merge, release or public binary distribution in this evidence card.

The following frozen source wave corrects that CI status failure, intentional
empty-browser startup preservation and native voice admission/origin checks.
Combined386 backend tests, native voice37/configuration45/vault33 plus linked26,
production typecheck and the unchanged811 type ratchet passed locally.
See [hardening evidence](NATIVE_CONTRACT_HARDENING_EVIDENCE.md). The preserved
9.31 artifact and its accepted recovery journey do not contain those later fixes;
9.32 assembly and corrected-head CI remain separate acceptance steps.

Gen-UI expansion is deferred. Coding-engine expansion follows dependable setup,
chat/recovery, voice and data continuity. Existing coding capabilities remain
preserved; a wider ecosystem is not a prerequisite for finishing a supported
single-user Mac app. The remaining app work is tracked by concrete journeys and
release gates in [the execution plan](EXECUTION_PLAN.md), rather than inferred
from test totals, source-file counts or a calendar prediction.
