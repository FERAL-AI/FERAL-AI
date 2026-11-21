# Native offline profile archive controls

October 3, 2026. Extends the existing profile archive CLI and canonical native
preference readers. Settings now offers backup and restore of the currently
verified installation, with review before the app stops its captured runtime.
Restored files use a new folder and fresh preference suite; applying preferences
requires a second confirmation. The restored copy is not selected or started.

## Behavior and ownership

The scope comes from the launcher's actual config/data roots, verified primary
installation and the app's actual preference suite. It requires inactive chat,
voice, coding and context-creation operations. The visible conversation must have
an exact saved acknowledgement before an archive command is dispatched.

`BrainRuntime.stop(ifOwnedBy:)` refuses a different or busy lifecycle owner.
Completed quiescence records the exact retired Process identity; a nil owner
alone is not proof, and a new launch invalidates the old record. Review scope,
primary, preferences, roots, archive and destination are checked around awaits.
The user separately confirms all other writers are stopped: this app does not
claim a system-wide writer lease.

The CLI runs through the app's bundled Python with explicit arguments, bounded
streams and deadline, without a shell or inherited credentials. The app retains
its operation task and cancels/settles it during shutdown. A malformed receipt,
cancellation or uncertain post-dispatch result leaves destination files available
for inspection and blocks replay. Existing roots/domains are not replaced.

Settings exposes progress, review cancellation and exact receipt details. After
a settled operation, a separate action can restart the original profile. That
action does not select the restored copy or repeat earlier tasks. Unknown archive
outcomes do not enable this restart action.

Native review reproduced a retained feature-client write during shutdown. The
shared local-origin epoch gate now refuses those old clients before writes and
after late reads, including same-address runtime replacement. Only the exact
final conversation-save path bypasses host admission. This does not cancel an
already-dispatched effect or create a cross-process writer lease.
[Admission evidence](NATIVE_ACTION_ADMISSION_EVIDENCE.md).

## Verified checks and limits

- **280 controller assertions passed**, including actual standalone Python CLI
  same-root/nested/split backup and restore, canonical preference-reader readback,
  source/owner/destination drift, receipt strictness, one-use reviews, concurrency,
  output bounds, timeout and cancellation of real subprocesses. Disposable roots
  and defaults were used; the interpreter was the repository `.venv`, not the
  signed app's bundled runtime.
- Integrated runner passed280 archive,71 preference,87 layout,55 voice and45
  voice-configuration assertions, plus26 mocked model groups,39 desktop and5
  error-presentation assertions. Existing Swift5 fixture lock warnings remain.
- Complete production typechecking passed after Settings/model integration and
  again after the admission-guard refinement.
- Final shared-guard integrated runner passes34 gate,42 Agent,83 Workflow,80
  Integration, Configuration checks,280 archive and55 voice assertions, plus27
  mocked model groups/39 desktop/5 error checks. Actual final-save acknowledgement
  remains available while coding/grant/provider/settings dispatch stays refused.
- The CLI imports with Python `-S`, without third-party site packages. Native CI
  creates an isolated stdlib interpreter for these archive fixtures.
- **28 genuine Process-backed assertions passed** with real `BrainRuntime`,
  profile layout, health coordinator and the exact production launcher. Wrong
  and stale owner stops refused; exact stops removed backend/child PIDs and
  listeners; a replacement invalidated the old quiescence record. Evidence:
  `/private/tmp/feral-native-archive-owner-izlcxdbt/evidence.json`, with source,
  interpreter and executable hashes in `manifest.json`. The health-only server
  is synthetic and no GUI was launched. NativeModel save orchestration and
  file-picker interaction are separate acceptance, not certified by this test.

Actual Mac GUI/file-picker acceptance, packaged-runtime archive execution,
restored-profile activation, encrypted-profile migration, signed distribution and
upgrade/rollback remain separate gates. This UI restores backups with the native
snapshot attachment for the currently verified installation. Legacy archives
remain supported by the CLI; the UI does not invent absent native preferences.
No accounts, personal profiles, OS-key reset, real payments or messages were used.
