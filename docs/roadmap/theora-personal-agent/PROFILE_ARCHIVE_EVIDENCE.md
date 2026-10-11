# Offline profile archive foundation

October 2, 2026. Implementation is limited to `config/profile_archive.py`, its
disposable-fixture tests and this evidence. No active profile, vault, native UI,
server route, shared CLI or migration code was opened or changed by this card.

## Contract

`create_archive(destination, offline=True, config_root=..., data_root=...)`
produces a completed version-1 ZIP with a manifest, hashes, sizes, private modes
and root aliases. A private temporary file is flushed, source identities and
hashes are checked again, and an exclusive hard link publishes the final name.
An incomplete folder copy is not a completed archive. Existing output files are
never replaced. Failed snapshot attempts do not publish an archive.

`restore_archive(archive, offline=True, config_root=..., data_root=...)` validates
the full inventory and file contents before creating destinations. Destinations
must be **nonexistent**, and same-root versus split-root layout must match.
Files are created exclusively. A failed restore rolls back only newly created
roots whose device/inode identities match those created by this invocation.
Restore returns counts and flags; it never starts a process, selects an active
profile, applies migrations, reactivates grants or imports memory through an API.

The caller must stop **every writer** to both roots first. `offline=True` and
`--offline` are explicit caller confirmations, not a process-quiescence detector.
A stale PID file or a quiet UI does not establish that condition. Changed file,
directory or archive identities are refused, but this is not a substitute for
stopping writers or protection against a hostile concurrent filesystem owner.

Default backup roots come from the existing `config.loader.feral_home()` and
`feral_data_home()` helpers. `FERAL_HOME` sets both. Otherwise configuration uses
`XDG_CONFIG_HOME/feral` and data uses `XDG_DATA_HOME/feral`, each falling back to
`~/.feral`. These helpers do **not** read `FERAL_DATA_HOME`. Explicit roots are
available for fixtures and offline operators. Restore always requires explicit
roots. Aliased roots are archived once; nested roots are refused.

All regular files within those roots are included, without a new selective
export list. This covers configuration and identities, memory databases and
their committed WAL/SHM files, saved conversations/checkpoints/receipts,
encrypted artifacts, custom extensions and empty directories. Files referenced
outside these roots, external model caches and OS-managed keys are not included.
Runtime state files are preserved as bytes; restore does not trust or activate
their stale process, pairing or endpoint identities.

## Bounds and refusals

Default bounds are 20,000 inventory entries including roots/directories, 1 GiB
of uncompressed file contents, 256 MiB per file, and a 4 MiB manifest. Paths are
canonical relative POSIX paths under `config/` or `data/`, at most 32 components
and 4,096 UTF-8 bytes. Operators can explicitly supply positive integer
`ArchiveLimits` through the Python API; the standalone CLI uses these defaults.

Source/ZIP symlinks, nonregular entries, encrypted ZIP entries, duplicate ZIP
members or JSON keys, traversal, inconsistent root mappings, file/directory
conflicts, unlisted contents, incorrect hashes/sizes and excessive inventories
are refused. Archive containers must also be bounded regular files opened with
`O_NOFOLLOW`. Restoration verifies integrity again while copying and refuses a
changed archive. Ordinary files restore with mode 0600, executable files with
0700, and directories with 0700. Original permissive modes are not restored.

The ZIP itself is private (0600) but **not encrypted as a whole**. Source
plaintext remains plaintext in the archive; encrypted source bytes remain
encrypted bytes. The manifest contains relative filenames and hashes. It is
neither a cryptographic signature nor proof of who produced the archive.
Receipt output contains counts and fixed flags, not profile content.

## Use after stopping all writers

From `feral-core`, using the installed runtime:

```sh
python -m config.profile_archive backup /private/tmp/my-profile.zip --offline
python -m config.profile_archive restore /private/tmp/my-profile.zip --offline \
  --config-root /private/tmp/new-profile --data-root /private/tmp/new-profile
```

The example restore assumes an archive whose roots were aliased. A split-root
archive requires two separate nonexistent destinations. Parent directories must
already exist. Module failures print a refusal and exit 2; success prints a JSON
receipt. No global CLI command or native backup button was added.

## Verified evidence

All tests use temporary fixture roots. No actual user profile or OS credential
was inspected, archived or restored.

```sh
../.venv/bin/python -m pytest tests/test_profile_archive.py \
  -q --tb=short --no-cov -p no:randomly --timeout=60
../.venv/bin/python -m mypy -p config.profile_archive \
  --follow-imports=silent --no-incremental
../.venv/bin/ruff check config/profile_archive.py tests/test_profile_archive.py
```

**34 tests passed, one existing Pydantic schema-shadowing warning, in 2.54
seconds.** Scoped mypy and Ruff passed. Tests cover same/split roots,
configuration/ciphertext/custom extensions, committed SQLite WAL readback and
`PRAGMA integrity_check = ok`, explicit offline refusal, size bounds,
nonregular sources, malformed archives, duplicates, archive changes, partial
restore rollback, no overwrite, executable modes and standalone CLI round trips
without private-content output. They caught and fixed a restore identity variable
shadowing defect before freeze.

The parent subsequently added two missing-output-directory refusal tests and
wrapped temporary-file creation errors in `ProfileArchiveError`. The final
integrated24-suite run includes these36 archive cases:570 total tests passed,
19 warnings in18.99s. The standalone CLI returns2 without a traceback or an
incomplete archive when the destination parent is unavailable.

## Remaining release boundaries

This is offline byte continuity, not active-profile migration or cross-machine
credential recovery. Vault ciphertext may require the original OS master key or
a separately established recovery path. Neither OS keys nor a portability
promise are exported. Cloning sync/pairing identities onto another running
machine is not supported by this card. Profile compatibility, unlock, migration,
activation and end-to-end app acceptance must be checked separately.

Publication avoids ordinary overwrite and validates a cold snapshot, but parent
directories are not fsynced and a two-root restore is not a power-loss atomic
transaction. Recovery from interrupted restore, signed archives, archive-level
encryption, native UI integration and automatic retention remain future cards.
Final integrated-source tests and remote CI are owned by the release coordinator;
the scoped results above do not claim a finished release.
