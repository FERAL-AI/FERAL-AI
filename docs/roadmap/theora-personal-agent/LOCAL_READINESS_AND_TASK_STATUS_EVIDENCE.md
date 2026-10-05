# Local model readiness and task status

Updated October 4, 2026. This source wave extends existing provider discovery,
preset application and TaskFlow inspection. Exact source and packaged acceptance
are recorded in [WORK_STATE](WORK_STATE.md). It creates no additional executor.

## Truthful local inventory

Ollama and LM Studio accept only a successful, well-formed model inventory. An
empty inventory clears adapter models, persisted catalog models and stale default
selection. A failed request or malformed inventory preserves the previous list
and exposes a refresh warning. Reopening the catalog and passive discovery do
not revive removed models or invoke provider I/O. Cloud fallback behavior stays
compatible.

An empty local inventory still uses the existing probe contract:
`reachable=false` with `provider returned no models`. This is not a new transport
versus usable-model capability schema. A returned model identifier also does not
prove inference, vision, tool execution or audio readiness.

Independent worker verification passes 257 tests across 12 suites, including
50 new empty/invalid inventory cases. Inert transport fixtures reproduce four
prepatch failures. No model weights, private accounts or saved user settings are
used.

## Vision preset admission

The public preset route refuses when the requested Ollama vision model cannot
be verified as installed, or its existing capability classification fails. A
manufactured bare alias does not prove that the implicit latest tag is installed.
The preset chooses a verified full tag, preferring latest only when present;
an explicitly requested full tag must match exactly. Refusal precedes provider
switching, client closure and saved configuration writes. Ordinary text presets
retain auto-detection.

Success records confirmed model presence and a model-classification capability
basis, with `inference_verified=false`. It does not certify image interpretation
or performance on this Mac. Source tests exercise the existing public route with
isolated configuration and inert inventory/capability boundaries.

The final worker gate passes 56 tests, including 18 new public-route cases.
Non-string preset model values refuse before discovery or configuration changes.
The initial parent typing gate finds two new object-type diagnostics; the explicit
string guard corrects them. Final full typing establishes no additions below.

## Task inspection envelope

Packaged 9.43 testing found that valid task status with `error=null` was labeled
as an operation failure. The correction adds explicit lookup success and keeps
a failed job's stored error inside its status data. A successful status read is
separate from a successful job or verified external action. Failed lookups retain
their refusal/error envelope.

Two actual registered-executor regressions fail before the correction and pass
after it, covering both a queued job and a failed fixture job. The worker's
12-suite gate passes 210 tests. No action is retried to obtain a status response.
The failed 9.43 packaged probe is preserved in its acceptance ledger.

9.43 remote CI also identifies three endpoint contract-inspection failures and
one timing assertion. An explicit supported-endpoint guard makes the delegated
adapter inspectable without changing central execution. The cancellation fixture
now holds an inert worker until retention is checked, then releases it and
verifies zero inserted jobs; a safely drained worker is not treated as a runtime
failure. The global manifest checker is unchanged.

## Integration and remaining gates

Parent final frozen integration passes 2,337 tests across 106 suites, including
every manifest endpoint contract, with two opt-in skips and 55 warnings in
51.51 seconds. All 1,327 Python inputs remain unchanged; digest
`b7ae5e4288ab3502b11e8ba030abc0b5968b5f95ba8a5c35abcc8354433c80a8`.
Full typing completes with 798 existing diagnostics, zero normalized additions
including multiplicity checks, and two previous object-type errors removed.
Ruff, 24 script tests, shell syntax, whitespace and documentation navigation pass.
The final task/manifest worker gate passes 477 tests. Worker counts overlap;
they are not additive evidence of product coverage. Exact-source
[Mac 9.44](NATIVE_9_44_ACCEPTANCE.md) now passes independent payload/signature
verification, actual bundled task/status and public local-readiness method
checks, and isolated backend startup/lifeline shutdown. No inference, physical
voice or full approval handling is claimed by those inert collaborators.

Small private receipts retain the initial 2,065-test source gate and its typing
failure separately from the corrected final gate. Final evidence is
`feral-local-ready-944-integration-final-20261004/{pytest.log,result.json}` and
`feral-local-ready-944-typing-final-20261004/{mypy.log,result.json,diagnostic-multiplicity.json}`.

Exact pending-approval origin transfer, durable result subscriptions, supervised
service ownership, trusted desktop targets/watch/Stop, real local vision/speech,
physical voice, clean installation and account/device integrations retain their
own acceptance gates. [Complete requirements](REQUEST_COVERAGE.md) and
[release readiness](RELEASE_READINESS.md) preserve the full scope. Coding expansion
remains last.
