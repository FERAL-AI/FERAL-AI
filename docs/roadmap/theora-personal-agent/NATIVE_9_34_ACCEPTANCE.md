# Native 2026.9.34 packaged acceptance

October 3, 2026. This records actual packaged checks, separate from source
fixtures, GUI acceptance and release qualification.

## Immutable candidate

| Identity | Verified value |
|---|---|
| Version / build | `2026.9.34` / `2026100301` |
| Runtime source | `4eded179e2060c226739511fc53f5a8bde0eaa16` |
| Native executable SHA-256 | `2dac68710b2d730db7555b85fa66254a56b34f04707df8919f8e6457329c22e2` |
| Manifest SHA-256 | `aaf50df1d841c61e00f351dc8abe6fa9d612962cf3b40f77ca6b52af3765f230` |
| Compiled native source inputs | 51 files matched the published source |
| Packaged production Python | 487 files matched the published source |

Optimized native assembly and strict deep ad-hoc signature verification passed.
The bounded bundle audit passed with 13,104 files, 265 Mach-O objects, nine internal
links and zero reported issues. Bundled CPython 3.11.15, SQLite 3.53.1/FTS5,
OpenCode 1.18.10 and SDK imports passed their staging checks. Ad-hoc signing does
not establish Developer-ID signing, notarization or clean-machine installation.

## Actual offline archive and reader round-trip: passed

The actual bundled Python and archive CLI operated on fresh synthetic profiles.
No app, server, model, personal profile, OS preferences domain or account was
opened by this probe. It verified:

- Backup with a canonical native preference attachment and fresh-root restore.
- Actual readers recovered conversation messages, custom title, pin, note,
  knowledge fact, wiki page, settings and an explicit capability denial.
- The saved context's generation, revision, attempt and history remained exact.
- Native preferences and the relative avatar retained their digests.
- Wrong-primary preference input and occupied restore destination were refused.
- Original profile and packaged source were preserved. Restore reported
  `preferences_applied=false` and `runtime_started=false`.
- Post-run strict signature, executable hashes and all 487 source comparisons
  passed again.

The successful private evidence directory is identified by suffix `rs1qp3kx`;
its final receipt records harness SHA-256
`d8b06cf6dcdbe0d3301cee3e1d5e11dcf1d657a51d2e3d73d82967b3105f7350`.
The generated archive was 435,253 bytes across six entries. No synthetic database
or generated archive is checked into the repository.

The first probe, suffix `udk_bnrd`, failed during fixture seeding before archive
dispatch. Its harness incorrectly expected the checkpoint mutation result to
be `READY`; the actual contract returns `APPLIED` with a `READY` record. A new
harness revision checks both fields correctly. The failed evidence and original
harness remain preserved; production checkpoint code was unchanged.

## Current independent gates

- Actual local-model Stop/recovery/restart acceptance has no completed receipt
  for this candidate yet. The initially reported active run had not launched:
  its worker was waiting for command approval. A new resource-bounded probe is
  prepared with the existing acceptance deadlines and isolated resources;
  actual launch and completion must be recorded separately.
- Computer Use inventory now succeeds after its earlier native-pipe failure.
  Isolated actual GUI journeys can resume. File-picker/preferences-application
  and microphone/speaker acceptance still require completed results; tool
  availability alone is not app acceptance.
- Managed saved-context voice remains refused pending its tracked-turn adapter.
- Candidate-source native and Linux CI failed. Newer source `f41c204fe` passes
  general CI and the corrected Linux desktop package workflow. Its native Process
  fixture still times out at30s, now with evidence that the interpreter and child
  launched before readiness stalled. This does not establish a listening server
  or the root cause. Further diagnosis is a Mac-first workstream.
- Cloud-account actions, physical glasses, iOS, Linux GUI, migration of encrypted
  profiles, distribution signing and full update/rollback need separate evidence.

See [the work checkpoint](WORK_STATE.md), [archive source evidence](NATIVE_PROFILE_ARCHIVE_EVIDENCE.md)
and [release readiness](RELEASE_READINESS.md).
