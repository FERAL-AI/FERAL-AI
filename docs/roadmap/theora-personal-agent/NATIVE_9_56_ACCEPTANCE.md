# Native 9.56 phone recovery acceptance

October 10, 2026. Preview 2026.9.56/build2026101005. Exact packaged runtime:
`82a6c269e0926b1b808a1274cb5cafe28cdf0b9e`.

## Source and fixture evidence

Registered phone voice startup retains a bounded ordered lane instead of blocking
control intake on provider initialization. Current socket, principal, runtime and
exact producer guards fence late startup, audio and teardown. The prior published
real model-step Chrome ownership/completed-deferred trace repair is included.
Four remote Ubuntu failures are repaired in three test fixtures, without weakening
production authorization or catalogue verification.

Frozen integration: 1,237 passed, four skipped, CI Ruff passed, zero drift. The
first typing run found two new dynamic attribute diagnostics; equivalent setattr
stores resolve both. The final affected eleven-suite gate passes 217 tests, CI
Ruff and 788 existing Mac diagnostics with zero additions/removals or source drift.
The separate 14-case real model-loop consent gate passes. Fresh remote Ubuntu CI
remains separate from these local Mac results.

## Artifact and native provenance

Canonical artifact: `desktop-native/build/FERAL Native Preview.app`.
All 504 packaged production Python files and all 70 tracked WebUI files match
committed source. Contained Python 3.11.15/SQLite 3.53.1/FTS5, SDK, WebUI v2,
OpenCode 1.18.10 and desktop dependency imports pass bounded bundle audits.
Strict deep ad-hoc signature verification passes. This is a development preview,
not notarization or clean-machine distribution acceptance.

All 54 Swift source hashes and build.sh flags match verified fresh 9.55 compilation
at 3717ad646. Reuse input signed executable SHA256:
`09268024d3522ba16075a93eaf38d838860ac7b5799bf0e01fb20b8b53819f44`.
The exact executable is copied into the inspected generated build destination and
re-signed for the new bundle. Final executable SHA256:
`35faa776e473a50d090f97c3ecd31d61480244b2f83e89c1071db38bcbee2127`.
Mach-O payload equality excluding the code signature passes with SHA256:
`de65a9f75e45c66161f9413b7d4b2d5026abe7955bd8da39a11c5930558e231f`.
This is verified native reuse, not a fresh Swift compilation or new GUI run.

Manifest: `/private/tmp/feral-candidate-9-56-manifest.json`, SHA256:
`ef0412fd52708ed9767730951902fed5ddf92a129724e2353e31b0aaabe0d7db`.
Assembly/audit/reuse evidence: `/private/tmp/feral-native-9-56-build-20261010`.
Retained source-gate evidence is linked in
[the recovery record](PHONE_VOICE_RECOVERY_20261010.md).

## Actual bundled lifecycle and retained failures

A fresh disposable unavailable local-provider profile starts the actual bundled
backend. Health succeeds in 2.31 seconds, setup status remains incomplete, closing
the launcher's stdin lifeline shuts down the owned process, and its listener
closes. Artifact hashes remain unchanged. No account keys, microphone, browser
interaction or inference request is used. Arbitrary backend egress is not
instrumented; the probe requests are loopback only.

Receipt: `/private/tmp/feral-956-backend-startup-final-evidence.json`.
The initial probe supplied the preceding signed executable hash; its identity guard
refused before process creation. That failure is retained separately. A second
invocation refused an occupied evidence path; the final run uses a fresh path.
These are verification setup failures, not observed application startup failures.

## Resources and remaining acceptance

Offline cached assembly took 32.81 seconds. Free space was 13,054,025,728 bytes before
and 12,990,926,848 bytes after: net 63,098,880 bytes within the 2 GiB estimate. The
identity-checked generated 9.55 app is replaced; known-good 9.52 remains the sole
rollback. Personal data/models/caches/useful Git history and unrelated changes
are preserved. No personal runtime or settings are changed.

The iOS development app from local `5749babc52087698de21aec96e0926650a823f37` is
signed and installed in place on the connected iPhone 17 Pro Max. Its 568 assertions,
actual ambient wire validation and device builds pass; iOS commits remain unpushed.
Live microphone/HFP, chosen-provider/browser action/result and reconnect acceptance
remain open. The prior task is cancelled with unknown action outcome, not replay
permission. Code 9 termination cause remains unconfirmed. Native GUI evidence stays
historical 9.55 evidence. Distribution and the other complete-product gates remain
in [release readiness](RELEASE_READINESS.md).
