# Mac 9.45 exact-source acceptance

Updated October 5, 2026. This records bounded source, artifact and packaged-method
acceptance. [Behavior and limitations](TASK_APPROVAL_AND_SELECTED_VISION_EVIDENCE.md)
and [current remaining work](WORK_STATE.md) distinguish full-product gates.

## Identity and assembly

- Version `2026.9.45`, build `2026100501`, arm64/macOS 13 minimum.
- Exact build source `3cfe733b4f94ae18dd1755d13b7b5fbd7a34be5a`, published in
  [draft PR310](https://github.com/FERAL-AI/FERAL-AI/pull/310). Runtime changes are
  `99f94dda1`; subsequent checkpoint commits change documentation only.
- Native SHA256 `fb9141bdaacd9b1e5cd9a728add4243f75a68e9aeb83ea7769d839e0697cbe93`.
- Manifest SHA256 `79746a5e62a0e068aba58d4fa3c83a8feb308afff6e0e336585eca464dc52802`.
- Fresh optimized compilation uses all 52 committed Swift inputs and unchanged
  compiler flags. All 68 assembly inputs match Git. The native source change
  relative to 9.44 is confined to Oversight approval scope and availability.
- All 496 packaged production Python files match the exact commit. The audit
  records 12,934 entries, 265 Mach-O files, nine contained symlinks and zero issues.
  Strict deep ad-hoc signature verification passes.
- Cached, offline staging contains Python 3.11.15, SQLite 3.53.1 with FTS5, the
  Python SDK and OpenCode 1.18.10. Eleven supported desktop dependency imports
  pass; this is not permission, capture or physical control acceptance.

Independent read-only review repeats source/input/brand/launcher comparisons,
fresh compilation provenance, contained runtime/import checks and signatures of
both the candidate and rollback. Its receipt passes without repository edits.

The canonical app is `desktop-native/build/FERAL Native Preview.app`; the sole
generated rollback is verified 9.44. Only the inspected, unused generated 9.43
copy is retired. Its historical manifests and genuine failed status-probe
evidence remain. Personal Git, models and profiles are untouched.

Assembly takes 181.49 seconds, starting with 26,331,430,912 bytes free and ending
with 26,259,369,984 bytes. Net growth is 72,060,928 bytes after retention cleanup.
The 10 GiB initial, 5 GiB final and 2 GiB artifact-budget checks pass. Peak disk
usage is not measured.

## Source and remote checks

The retained frozen source gate passes 2,724 tests across 124 suites with two
skips and 67 warnings. On resume, all 1,329 Python inputs still match digest
`51a73308c52207842d4acc8085a2b2350037b920f7e905f4abdb678e0de0a14f`; all 55
native/harness inputs also match their passed ledger. No unchanged source suite
is rerun merely to attach it to a documentation commit.

Full local typing retains 798 existing diagnostics with zero normalized additions
or removals. Native Oversight passes 27 groups, linked model 28, desktop 39,
error presentation five, and production typecheck. Ruff, whitespace and docs
navigation pass. Initial compatibility failures remain separate evidence.

Exact build head `3cfe733b4` has 18 successful remote checks, four conditional
skips and zero failures. Backend fast lane, native contracts and Linux bundle
checks pass. Real-brain e2e, macOS distribution build and main-only/matrix checks
are skipped; these do not establish live-provider or Mac distribution acceptance.

## Executed packaged methods

The app's own isolated interpreter loads actual packaged ChatTurnManager,
ToolRunner, SkillExecutor, MemoryStore, TaskFlow and public approval routes.
Approval after the original turn closes creates one queued metadata task with
the original request, turn, call and surface. Repeating the same approval refuses.
A later same-owner conversational approval retains original attribution. Neither
path grants ongoing tool permission.

An authentic stale tracked request remains visible and deny-only beside a valid
legacy request. Stale approval refuses; denial creates no additional flow rows.
The two approved fixture jobs remain queued: no task supervisor, inference or
external action runs. Owned workers, database handles and clients drain/close.

Selected vision uses the shared production multimodal adapter with inert local
HTTP transport. Passive selection makes no requests. A capacity read plus one
mocked image inference preserves exact endpoint, model and image input. A later
capacity refusal sends no inference. Both paths preserve the primary chat
context observation/window and client; owned temporary clients close. This is
wire and ownership acceptance, not real vision inference.

The method receipt records zero network and subprocess attempts, exact artifact
pins and loaded-module paths inside the bundle. Private receipt:
`feral-945-approval-vision-packaged-evidence.json`.

## Backend lifecycle

The exact bundled launcher starts an owned backend with a disposable profile,
no provider credentials/fallbacks and an unavailable local model. Health is
reachable in one 1.91-second observation; setup responds HTTP 200 and remains
correctly incomplete. Closing the launcher lifeline stops the owned backend
with SIGTERM, without fallback termination, and closes its loopback listener.
Manifest/native/launcher/server hashes remain unchanged. No inference is initiated;
arbitrary backend egress is not instrumented by this startup check.

The first sandbox run fails on loopback binding before creating a profile or
child. That permission failure is retained separately from the successful
approved run; it is not an app crash. Receipts are
`feral-945-backend-startup-evidence.json` and
`feral-945-backend-startup-evidence-unsandboxed.json`.

## Remaining acceptance

Actual native approval-screen acceptance is blocked by Computer Use's native
pipe startup failure. Fixtures and compilation are not a GUI result. Real model
inference, desktop target/watch/Stop, continuous physical voice, Messages,
commerce, accounts, glasses, fresh installation, migration, signing/notarization
and distribution remain separate gates. Task-result subscriptions and durable
pending approval restoration remain incomplete.

Assembly and independent artifact receipts are
`feral-native-9-45-build-20261005/{result.json,bundle.json,core.json}` and
`feral-945-independent-artifact-evidence.json`. They are small private evidence,
not shipped assets or consumer setup dependencies.
