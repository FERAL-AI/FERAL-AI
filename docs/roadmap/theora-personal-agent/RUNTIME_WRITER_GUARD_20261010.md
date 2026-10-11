# Early cooperating runtime writer exclusion

A server could previously initialize profile metadata, stores, credentials and
API keys before its listen bind failed. A second server using a different port
could avoid that collision entirely. Native backend, API import and CLI serve/
foreground-start entrypoints now acquire a shared kernel advisory lease before
backend initialization. A healthy CLI client connection remains read-only.

The lease covers distinct canonical config/data roots and the existing legacy
API-key root, acquired in deterministic order. Owned directories and stable
private regular lock inodes are checked. Nonblocking opening refuses malformed
FIFO/symlink/directory/hardlink artifacts before store admission. Process-local
reentrance shares one owner; exact tokens cannot release another lease. Forked
children close inherited descriptors without unlocking the parent's open file
description. Descriptors are close-on-exec. Runtime entrypoints retain ownership
through shutdown persistence until process exit; the kernel releases it on crash.
Busy admission does not kill a process, reset a profile or replay a task.

Parent integration verifies all three existing source baselines and five payload
hashes. Thirty-eight parent checks pass: 27 real kernel/subprocess/startup checks
and 11 existing endpoint contracts, with six existing warnings. Coverage includes
different ports, alias/overlapping roots, simultaneous admission, interrupted
rollback, SIGTERM/SIGKILL recovery, fork/reentrance, unsafe identities and early
API/native/CLI refusal. Full-core CI Ruff passes. Full Mac typing retains exactly
779 diagnostics, with zero additions/removals after line-number normalization.

This excludes cooperating backend runtime writers. Older apps, direct api.state
importers and offline setup/key/migration/archive utilities do not participate.
Native prechild log/directory creation and service-install artifacts remain outside
the lease. No universal zero-filesystem-write or offline-writer claim is made.
Old runtimes must be retired through their reviewed lifecycle before adoption.
Actual packaged startup/duplicate denial/normal Quit and ordinary persisted-profile
relaunch remain required candidate acceptance.
