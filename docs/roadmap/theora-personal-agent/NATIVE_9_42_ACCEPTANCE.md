# Native Mac 9.42 acceptance

October 4, 2026. This is a bounded artifact and runtime acceptance record, not
full-product or distribution certification.

## Artifact identity

- Version `2026.9.42`, build `2026100406`, arm64 macOS 13 minimum.
- Exact source `6947bbec10a4eec61ecfb835598d33448d706c29`, published in
  [draft PR310](https://github.com/FERAL-AI/FERAL-AI/pull/310).
- Native SHA256 `4a10f680302ef18356755bd7db74cebe82b53e73f1df4627fdfd4a71907dec0f`.
- Manifest SHA256 `8032ce2e1a0df4359996dd96e6a9dd48626cad9935605a47852fbdb4c4c0541d`.
- Fresh optimized compilation of all 52 Swift sources. All 495 packaged production
  Python files equal the committed source; 60 build-input hashes independently
  match. Separate inspection confirms the shipped avatar, logo and icon match
  committed source and packaged bytes, closing their omission from the build-input
  hash list.
- Bundle audit: 12,808 files, 265 Mach-O files, nine symlinks, zero reported issues.
  Bundled Python 3.11.15, SQLite 3.53.1 with FTS5 and OpenCode 1.18.10 pass their
  isolated probes. Strict ad-hoc signature verification passes.

Build completed in 202.68 seconds using cached offline staging. Free storage was
21,961,187,328 bytes before and 21,991,112,704 bytes after. Canonical 9.42 and sole
generated rollback 9.41 occupy approximately 593 MiB each. Only the inspected,
unused generated 9.40 copy was retired; its acceptance records remain. Personal
files, models, profiles and home-directory Git history were not changed.

## Executed checks

The frozen source gate passed 1,765 tests across 88 suites with two opt-in skips
and 58 warnings. Native Automation passed 51 fixture assertions plus linked model,
desktop and error checks. Full local typing reports 801 existing diagnostics with
zero normalized additions and eight removed. [Runtime behavior and limits](RUNTIME_MULTITASKING_EVIDENCE.md).

Tests using the packaged interpreter and isolated SQLite profiles verified:

- An unfinished routine remains visible as requiring reconciliation.
- Two independent budget instances admit only one of two $1 reservations against
  a $1.50 cap; an uncertain dispatched reservation remains held.
- Cancellation-resistant tool work retains its lane until exit and publishes no
  stale result.

Actual packaged backend startup reached health in 15.96 seconds in this run.
The setup-status endpoint returned HTTP 200 and correctly reported incomplete
setup. Closing the host lifeline stopped the owned child with SIGTERM and closed
its listener, without fallback termination. This is one observation, not a latency
benchmark or inference result.

The actual native app was launched with a disposable profile, disabled voice,
an unconfigured local provider and a paused inert routine with a persisted claim.
Through native Automation controls, the routine inspection displayed zero run
records and the visible warning: "Needs review: an earlier action outcome is
unresolved. Automatic replay is blocked." Screenshot inspection confirmed readable
layout and withheld raw payload/output/error content. One confirmation click
returned a stale accessibility-index error; a fresh full tree and screenshot
established the completed read and warning. Normal keyboard Quit returned host
exit 0 and closed the owned backend listener. No effect was dispatched.

Local receipts: `feral-native-9-42-build-20261004/result.json`,
`feral-candidate-9-42-manifest.json`, `feral-942-brand-input-evidence.json`,
`feral-942-packaged-method-evidence.json`, `feral-942-backend-startup-evidence.json`
and `feral-native-942-routine-ui/result.json`, under the local temporary evidence
directory. These filenames identify executed local evidence, not shipped archives.

## Open gates and rollback

Exact-source GitHub backend CI found one cloud-cost assertion using a local
Ollama fixture; 14,034 tests passed, one failed and 86 skipped, with 76.04%
coverage. The test-only correction explicitly prices cloud calls and verifies
local zero-cost inference. It passes 214 focused worker tests and the parent
1,792-test/89-suite gate, with two skips and 55 warnings. Production is unchanged.
The correction requires its own remote result. Other executed
checks passed; four conditional/opt-in checks skipped. No all-green claim applies
to `6947bbec1`, whose result remains 17 passed, four skipped and one failed.
Later corrected-source CI acceptance is separate; the unchanged production
artifact continues to identify `6947bbec1`.

Physical voice and continuous native duplex, real provider inference and
model-selected Mac actions, supported local input/speech provisioning, personal
Chrome/account tasks, Messages, payments, glasses, migration, empty-cache fresh
installation and Developer ID/notarized distribution remain open. This launch
used seeded setup state and does not prove onboarding or clean installation.
Durable detached jobs, exact scheduled grants and global resource ownership also
remain separate from bounded TaskFlow concurrency. Coding expansion remains last.

Retained 9.41 is the immediate generated rollback. Preserve unresolved receipts
and profiles across rollback; never replay uncertain effects or downgrade them
onto the older 9.40 scheduler. [Full current scope](WORK_STATE.md).
