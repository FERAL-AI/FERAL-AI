# Native 9.40 acceptance ledger

Updated October 4, 2026. Saved-local-provider source verification passes;
`2026.9.40/build2026100404` is not yet packaged or certified by this ledger.
The predecessor is recorded in [9.39 acceptance](NATIVE_9_39_ACCEPTANCE.md).

## Scope

The catalogue must probe the local endpoint actually selected by the runtime at
boot and activation, including no-key local activation. Ollama's native endpoints
must remove only the terminal OpenAI `/v1` suffix and preserve proxy prefixes.
Inactive scoped provider overrides remain intact. Repeated unchanged binding
must retain its cache; a changed binding invalidates it.

A probe finishing against an adapter replaced during the request must not publish
readiness or restore stale model inventory. It returns a typed configuration-change
refusal; the client reviews again rather than automatically retrying.

The original wrong destinations were reproduced in observed-request fixtures.
The actual 9.39 local catalogue contained the pinned model, while its native probe
used another endpoint. Positive native probe acceptance therefore requires the
backend correction; a parser fixture alone cannot close this gate.

## Packaging and actual gates

Reuse of 9.39's optimized native executable is allowed only with verified artifact
and manifest identity, all 52 compiled Swift inputs unchanged, and identical build
script/compiler flags. Reused compile provenance must be recorded explicitly.
The new backend still requires committed-source equality, bundled-runtime and
strict ad-hoc signature audits. Otherwise, compile fresh.

Keep canonical plus one preceding app, at least 10 GiB free before packaging and
at most 2 GiB of new artifacts. The fresh GUI uses an owned synthetic Chrome,
isolated default-path discovery and no personal accounts or model inference.
Require positive saved-provider probe, exact selected-chat approval, observed page
outcome, Stop/disconnect and normal Quit with exit 0 and closed listeners.

Real personal Chrome permission, whole-desktop sharing, independent concurrent
browser resources, audio/cloud accounts/glasses/Messages/commerce, migration,
clean installation and signed distribution remain separate release gates.

## Frozen source verification

- Integrated backend: 1,801 passed across 68 suites; three opt-in skips and 229
  warnings; 43.19 seconds. All 1,308 Python inputs unchanged, digest
  `9099e667c936591ca2f0321ca0d378245260d0f9d99441dc1f89e0ce76ade7de`.
- Focused observed-request/restart fixtures: 17 passed. Related provider suites:
  140 passed, overlapping those focused cases. Original three failures retained.
- Ruff and independent source review pass. Full mypy remains809 errors in233files
  with zero normalized additions/removals; it is not type-clean.
- Swift inputs are unchanged from the39 source tested by Onboarding73,
  Oversight16 groups, Browser115, linked model28 groups, desktop39 and errors5.

Private evidence: `/private/tmp/feral-chrome-native-940-integration-20261004-tests-se_sw9rz/receipt.json`,
`/private/tmp/feral-native-940-typing-delta.json` and
`/private/tmp/feral-local-probe-endpoint-{baseline,final,related}.log`.
The actual BrainState bootstrap tests use persisted disposable settings and scoped
constructor environment, stopping before credentials; HTTP is controlled. No
real inference or accounts were exercised. Pre-existing empty-model bootstrap
selection from a provider-only stale cache is outside this correction; native
acceptance selects an explicit cached model.
