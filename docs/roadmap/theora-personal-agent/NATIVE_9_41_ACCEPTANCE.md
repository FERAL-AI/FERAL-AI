# Mac 9.41 runtime reliability candidate

Historical artifact record. After [9.42 acceptance](NATIVE_9_42_ACCEPTANCE.md),
9.41 is retained as the sole generated rollback at
`/private/tmp/feral-candidate-9-41-preserved.app`; canonical build path now holds
9.42. The inspected unused 9.40 generated copy was retired. The original checks
and retention decisions below describe the 9.41 build checkpoint.

October 4, 2026. This candidate contains the bounded
[runtime reliability fixes](RUNTIME_RELIABILITY_EVIDENCE.md). It is not full
product or signed-distribution acceptance.

## Exact artifact

- Version/build: `2026.9.41` / `2026100405`.
- Runtime source: `a3f99db02718ab620a579e4bd9ebfd2b4f4c637f`.
- Canonical local artifact: `desktop-native/build/FERAL Native Preview.app`.
- Native SHA256: `8d414d82176a682db484f17afb8826209ac2f314459a05d02c30df34f2d9dc17`.
- Private manifest: `/private/tmp/feral-candidate-9-41-manifest.json`.
  SHA256: `005ef6f44b588f322a19390d18ecfd2e4e74b4e24550c5f8ef5b105e723a21b8`.
- All 494 packaged production Python files equal the committed source.
- Native executable reuse requires all 52 Swift inputs and build script/compiler
  flags to equal verified 9.40. These checks passed. Original optimized compilation
  source remains `fc4e86faf048b556b0b4400b80ec7a7fa8e7ebd8`; 9.40 runtime source
  `250787b2d...` is recorded separately. This is not a fresh Swift compilation.

## Executed checks

- Offline cached staging and assembly completed in 27.28 seconds. Strict ad-hoc
  signature verification passed before and after payload audits.
- Payload audit: 12,806 files, 265 Mach-O files, nine symlinks, zero findings.
  Python 3.11.15 and SQLite 3.53.1 run inside the bundle with FTS5; standard-library
  and base-prefix containment pass. Bundled OpenCode is 1.18.10; coding feature
  expansion is not part of this wave.
- The app's own isolated interpreter imports the new serializer and scheduler
  from the packaged core. Six falsy data values preserve their exact type and
  failure/uncertainty receipt; a named fixture password is redacted.
- An isolated SQLite claim is committed and reopened. Startup catch-up and normal
  polling dispatch zero callbacks for the unfinished occurrence. No external
  effect or OS crash is simulated by this method probe.
- The actual packaged backend launcher starts against a disposable profile with
  no credentials or reachable model. Health becomes reachable in 2.36 seconds;
  setup status returns HTTP 200 and incomplete setup. Closing the host lifeline
  stops the exact owned backend with SIGTERM and closes its loopback listener.
  This is backend startup/lifeline evidence, not native GUI acceptance.
- Source gate: 1,405 passed, two opt-in skips, 54 warnings across 72 suites, with
  all 1,317 Python inputs unchanged. Counts overlap with focused worker checks.
- Ruff passes; full local mypy still reports 809 errors in 233 files. CI and
  platform-specific baseline acceptance are recorded separately in WORK_STATE.

Private machine receipts: `/private/tmp/feral-native-9-41-build-20261004/`,
`/private/tmp/feral-941-packaged-method-evidence.json`,
`/private/tmp/feral-941-backend-startup-evidence.json`.

## Retention and rollback

Measured 24 GiB free before and after; planned new payload/copies were bounded at
2 GiB. The current candidate and one rollback copy are retained. Generated 9.39
was retired only after exact version/build/native hash, signature, path/ownership
and process-in-use checks. Immutable 9.40 is retained at
`/private/tmp/feral-candidate-9-40-preserved.app` with its earlier acceptance ledger.

Do not attach 9.40 to a 9.41 profile with unresolved occurrence claims. Older
scheduler code ignores the new journal and can replay those due slots. Keep
rollback isolated, or stop/disable and reconcile affected routines before a
downgrade. Callback return is not proof of a verified external effect. The new
occurrence journal still needs explicit native status/reconciliation presentation.

## Remaining acceptance

No new native GUI journey, model inference, continuous voice, physical control,
personal account, Messages, purchase, glasses, sleep/wake, migration or clean-user
installation was exercised in this wave. The [9.40 GUI evidence](NATIVE_9_40_ACCEPTANCE.md)
belongs to that immutable predecessor; unchanged Swift inputs do not establish
new runtime-dependent GUI outcomes.

Control/speech dependency provisioning, provider capabilities, persistent cost
reservations, responsive realtime intake, durable scheduled approvals and jobs,
external-effect reconciliation, Developer ID/notarization and downloaded-app
Gatekeeper acceptance remain open. See [current checkpoint](WORK_STATE.md) and
[full release gates](RELEASE_READINESS.md).
