# Bundled runtime update protection

October 3, 2026. The desktop package must update as a complete artifact, because
its bundled Python, application source, web assets and native executable are
verified together. An in-place pip upgrade would invalidate that relationship.

## Reproduced defect and resulting behavior

The actual existing update-command source, loaded under a synthetic Mac bundle
layout with a mocked runner, attempted `Resources/python -m pip install --upgrade
feral-ai`. No real network request, package installation or restart occurred.

The correction classifies the loaded command, interpreter and Python prefixes
before installation metadata, index checks or service inspection. Coherent native
and Tauri Mac resources, staged Tauri resources and the exact current Linux
resource layout are refused for pip self-upgrade. Identified mixed or redirected
bundle paths are also refused. Ordinary wheel, editable and independent virtual
environments keep their existing behavior, including a virtual environment based
on a bundled interpreter.

Both update and `--check` explain the whole-app replacement requirement and
explicitly say that no index comparison, installation or restart was performed.
This protection does not implement automatic download, signed artifact selection,
transactional replacement, profile migration or rollback.

## Verification

- **63 tests passed** across the new structural/effect-refusal cases and existing
  CLI update suite, using an explicit disposable profile. The first sandboxed run
  could not inspect its own process through `ps`; the authorized rerun passed.
- Targeted Ruff and whitespace checks passed.
- Scoped typing retained one existing heterogeneous process-dictionary diagnostic;
  comparison to the preceding source found no added diagnostic. The earlier full
  808-diagnostic result does not certify this new source; exact-head CI remains next.
- Read-only introspection of immutable9.34 confirmed its actual loaded command
  and interpreter/prefix/base-prefix match the native bundle structure. The package
  remains unchanged and does **not** contain this later guard.

Source SHA-256: `d09d6e4432b4b9a598f858cef3b9b7fc5ebb50eb3c52667453f0436f7c7c0f80`.
Test SHA-256: `8bf1dcd91a2de7576b3f4c85847c74fec83bcf1303f6d6edfef585e6e093872e`.

## Native verification diagnostics

The separate native CI Process fixture timed out at its30-second bound on
published4eded source. Its original output did not retain the blocked phase;
the cause remains unconfirmed. A narrow test-harness repair retains phase
checkpoints, child logs, streams and failure receipts without changing production
runtime code or increasing the deadline. The genuine Process test passes28
assertions locally. Deliberately shortened deadlines preserve failed receipts.

CI always uploads only allowlisted synthetic evidence, capped at64 KiB per file
with seven-day retention. App/interpreter/profile trees are excluded. The next
remote run must identify or close the timeout; local success alone does not do so.

See [the current checkpoint](WORK_STATE.md) and
[the exact packaged candidate](NATIVE_9_34_ACCEPTANCE.md).
