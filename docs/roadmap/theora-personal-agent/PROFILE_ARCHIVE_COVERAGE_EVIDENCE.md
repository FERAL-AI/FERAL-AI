# Offline profile archive coverage

October 2, 2026. This extends bounded format-1 offline archives; it does not
silently move storage or activate a restored profile.

## Reproduced defect and correction

Without FERAL_HOME, the config loader selects XDG roots while several real
runtime stores still use `~/.feral`. A disposable CapabilityGrantStore test
confirmed a denied camera grant in that omitted root. An archive of only XDG
folders could omit the denial; an empty replacement grant store would allow it.

Default selection now checks the legacy runtime root before copying and again
before exclusive publication. An uncovered nonempty root refuses the archive.
An absent or empty uncovered root is accepted; symlink, nondirectory,
uninspectable or changing roots fail closed. Empty FERAL_HOME is refused for
default selection because existing readers disagree between current-directory
and fallback-home behavior. No omitted payload is read and no whole-home
expansion occurs. Supplying both roots explicitly retains partial-snapshot
semantics. Receipts say `coverage: selected_roots_only` and disclose excluded
native preferences. Archive format 1 remains compatible.

Native BrainRuntime selects FERAL_HOME explicitly. The existing loader uses
that same root for config and data, independently of FERAL_DATA_HOME; archiving
it once includes nested data without changing existing reader paths.

## Verification

The frozen worker archive suite passed **52 tests** with FERAL_STRICT_ENV_LEAKS=1,
zero environment-leak diagnostics. Combined archive/config-loader/runtime
checkpoint/sandbox regression passed **159 tests**, six existing warnings.
Targeted Ruff and mypy passed. Actual existing readers in disposable fixtures
verified settings, workspace grants, denied capability grants, notes, pinned
conversation metadata, knowledge, wiki pages and a READY context fence after
restore. Refusals preserved original source bytes and published no archive;
the actual CLI refusal exited 2. These are real-reader fixtures, not personal
deployment migration or native GUI acceptance.

Local log: `/private/tmp/feral-data01-archive-related-final.log`.

## Limits

Offline quiescence is still a caller precondition, not a daemon detector or
cross-process lease. Native UserDefaults, OS keys and credential portability
are excluded. No mixed-layout merge, automatic activation or native preference
migration is implemented. The complete ZIP is not encrypted. Directory checks
do not constitute a hostile same-user filesystem isolation boundary.
