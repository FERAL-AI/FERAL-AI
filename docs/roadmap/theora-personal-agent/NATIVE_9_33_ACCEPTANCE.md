# Native 9.33 packaged-backend acceptance

October 2, 2026. This records the assembled artifact, not every later checkout edit.

| Identity | Value |
|---|---|
| Version / build | `2026.9.33` / `2026100207` |
| Bundle | `ai.feral.native.preview` |
| Runtime source | `7f818da08139952b1698644469e7016563512cd5` |
| Native executable SHA256 | `c54b8ec05159ccf236bd27e5dd79e927539cc13d7ffe102d192cc2316801058e` |
| Local manifest | `/private/tmp/feral-candidate-9-33-manifest.json` |
| Manifest SHA256 | `c0ce2093f8b0be11b373ea87bd9f3b592f4bc58f3dbe10845150f58be0608064` |
| Artifact | `desktop-native/build/FERAL Native Preview.app` |

Staging and optimized Swift build passed. Strict deep ad-hoc signature verified.
The bounded audit found13,097 files,265 Mach-O binaries and nine internal links,
with no reported issues. Bundled Python3.11.15, SQLite3.53.1/FTS5 and OpenCode1.18.10
passed runtime probes. All485 production Python files match the frozen source.
Post-journey executable hash, strict signature and all485 source comparisons passed.

## Actual journey

Fresh disposable attempt3 passed, without increasing the existing100-second
readiness deadline:
`/private/tmp/feral-native-9-33-recovery-20261002-attempt-3/final-result.json`.

The bundled backend accepted an actual synthetic local-model turn. Stop left its
durable context IN_PROGRESS; foreign and stale recovery attempts were refused.
Exact reviewed recovery rotated authority without replaying the cancelled input.
Read-only status and independent checkpoint readback agreed. The existing local
model recalled the committed fact after recovery, then again after owned-process
restart. The saved UI projection remained unchanged. Cancelled input was not
restored. Four durable terminal receipts and zero tool executions were recorded.

Both boots initially exposed zero tools from43 disposable empty installed skill
fixtures. This separately tests empty-manifest preservation; it is not tool-enabled
acceptance. The explicit authenticated browser inventory fixture remains separate.
The model was existing `theora-coding-ba1007eb4404ce93:latest` on11436/v1 with no
fallbacks or downloads. No personal deployment, account or OS key was used.

Owned backend PIDs83036 and83369 stopped with SIGTERM/exit-15, without forced kill;
PID and listener absence were checked. Harness identity, manifest and candidate
identity were recorded and rechecked before restart and completion.

## Preserved failures

- Attempt1 was sandbox-denied before an owned backend launch. Its record remains.
- Attempt2 completed Stop/recovery/actual model recall, then restart exceeded100s
  readiness. Both owned processes stopped cleanly. The cause was not established.
  Attempt3 used the same app, source, harness and deadline with a fresh profile;
  its pass does not erase that failure or prove its cause.
- Initial sandboxed compilation could not launch Swift macros. The approved
  build passed; the denied log remains separate from compiler/source defects.

The machine had substantial disk/swap pressure during adjacent checks. A separate
browser run reported an actual ENOSPC write error. That does not establish the
cause of this artifact's earlier readiness timeout or previous app crashes.

## Limits and next gates

This is actual packaged-backend text/recovery acceptance. It does not test native
GUI interaction, microphones, speakers, provider accounts, purchases, glasses or
iOS. Computer Use native-pipe startup still fails. The signature is ad-hoc, not
Developer-ID/notarization, and no clean-machine install/update was performed.

Exact7f native/web/SDK/type CI passed; backend CI failed three outdated fixtures
and one test setup race. Corrections are published in `86a8b54ed273642da40776e75609f5a45a197924`;
their targeted132 plus ambient16 tests pass, with full new-head CI pending.
Those changes affect tests/documentation and are excluded from this immutable
artifact. Native attempt correlation and preference archive additions remain
the following implementation wave, not capabilities certified by9.33.

Earlier9.32 remains preserved at `/private/tmp/feral-candidate-9-32-preserved.app`.
Do not overwrite9.33 before preserving its exact tested identity.
