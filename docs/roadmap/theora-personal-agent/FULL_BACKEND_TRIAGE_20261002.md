# Failed full backend run triage, October 2, 2026

The required macOS backend run ended with exit 1 and **no valid final pytest
summary or coverage result**. Its log is
`/private/tmp/feral-wave4-full-backend-20261002.log`. Parent confirmed all 1,261
source files stayed unchanged during the run, manifest digest
`23fed780ce1e9eef2d1a6af6c2fc2470e295e51ac44a5780b7073d0344523b2f`.
This report separates fresh reproductions from hypotheses; the failed run is
not release acceptance.

## Confirmed environment and harness facts

Disk had **4.8 GiB free** when inspected after the run. No ENOSPC diagnostic is
present in the preserved log. This does not prove every resource was sufficient,
but disk exhaustion is not the observed explanation.

The first socket-test failure, browser recording's stub CDP server, reproduces
in a fresh default-sandbox process: `site.start()` raises PermissionError while
binding an ephemeral localhost port. The same test and CLI piped-input local
server test both pass after a legitimate escalated rerun: **2 passed, 7 warnings
in 2.46s**. This verifies sandbox restrictions for those failures. It does not
classify every later network or GUI failure as environmental.

The full run's final coverage traceback uses a `_NoExtConn` fake lacking
create_function; the source of that fake is the SQLite build-feature probe test.
The subsequent pytest temporary-directory cleanup still calls the grant-pruning
test's patched Path.exists, which raises an intentional host-unavailable error.
These are retained test doubles, not a real SQLite limitation or unavailable
filesystem. Coverage finalization then hides the ordinary accumulated test
traceback summary. Both test doubles pass in fresh isolation with normal teardown:

- No coverage: **2 passed, 5 warnings in 1.14s**.
- Coverage with `--cov=.` and temporary coverage file: **2 passed, 5 warnings in
  7.31s**.
- Late fixture groups (settings cache, SPA collisions, SQLite features, grant
  pruning) in a fresh no-coverage process: **71 passed, 7 warnings in 17.04s**.

Therefore those suites do not intrinsically leak their mocks in the minimal
process. The original full-suite order/teardown origin is still **unlocated**.
No failing test was removed or assertion weakened to claim a passing full run.
A narrow experiment using module-targeted `--cov=memory.sqlite_features` failed
collection with a NumPy duplicate-module load; switching to the actual
repository coverage target `--cov=.` produced the passing minimal reproduction.
That unsuccessful collection is not acceptance.

The same interpreter has RLIMIT_NOFILE **soft 256**, hard
**9223372036854775807** and a fresh process reports four descriptors in /dev/fd.
The failed full process had already exited before investigation, so its peak
descriptor count is unavailable. No EMFILE traceback is preserved. Descriptor
exhaustion is a plausible condition to instrument, **not a confirmed cause**.

There are 11,486 PASSED, 60 SKIPPED, 536 FAILED and 527 ERROR progress status lines
in the failed log. These are only progress-line counts: teardown can report an
extra status for a node, two collection skips are separate, and no final result
was emitted. They must not be represented as an accepted test summary.

## Confirmed attributable regression and fixture isolation

The primary transcript boot-hydration test fails in a fresh process because it
calls a real BrainState method with a SimpleNamespace fake that lacks the newly
required `_primary_context_uses_checkpoints` helper. The exact failure is an
AttributeError before hydration, independently of the sandbox/resource cascade.
Parent owns its repair and reports binding the actual guard to the legacy fake,
with primary/ingress/approval regressions checked separately. This worker did not
change state, the guard or that fixture.

The config and integration modules mark themselves no_auto_feral_home and strip
all FERAL environment variables. That also removed the command's disposable home
and audit override. Fresh credential migration then attempted a real operator
audit write; the sandbox refused it and the loader returned empty credentials.
This is a test isolation failure, not authorization to escalate the personal
write. No personal audit operation was escalated.

Parent granted these three configuration fixture files for repair:

- `tests/test_config.py`
- `tests/test_config_loader.py`
- `tests/test_integration.py`

After their ambient environment reset, fixtures now set FERAL_HOME to the same
disposable `.feral` root used by each test's ConfigLoader.user_home/credential
file, and set its audit path there. They do not precreate that root because the
existing test fixture creates it. Provider/default and migration assertions stay
unchanged. The XDG-data fallback case explicitly removes the home override before
testing absent-override precedence; its expected XDG assertion stays unchanged.
The existing fake OS-keychain fixture remains in use.

Frozen checks after those narrow repairs:

```bash
cd feral-core
env FERAL_HOME=/private/tmp/feral-full-triage-config2-home FERAL_DATA_HOME=/private/tmp/feral-full-triage-config2-home ../.venv/bin/python -m pytest tests/test_config.py tests/test_config_loader.py -q --tb=short --no-cov -p no:randomly --timeout=60
env FERAL_HOME=/private/tmp/feral-full-triage-integration-home FERAL_DATA_HOME=/private/tmp/feral-full-triage-integration-home ../.venv/bin/python -m pytest tests/test_integration.py -q --tb=short --no-cov -p no:randomly --timeout=60
```

Results: **37 passed, 6 warnings in 1.40s** and **24 passed, 6 warnings in 1.90s**,
respectively. Ruff and scoped diff check passed. No production logic was edited
for these failures. No personal profile, real keychain or account was connected.

## Additional fresh failure classification

Doctor Tailscale, Glass Brain events and turn-attribution suites in a fresh
no-coverage process yielded **63 passed, 3 failed, 7 warnings in 10.47s**.
All attribution cases pass fresh, so their earlier full-run failures are not
intrinsic to this isolated source slice.

Two doctor tests assert the entire report lacks Suggested fixes. The real macOS
Accessibility/ScreenCapture permission probe adds unrelated fixes, contradicting
their fixture's intention to isolate Tailscale. The existing doctor fixture pins
other unrelated probes but leaves OS permission state live. This needs an explicit
fixture pin for that unrelated probe or a properly scoped assertion; no OS grant
or production warning suppression is proposed.

The Glass Brain voice-start fixture constructs RealtimeProxy with `__new__`,
seeding many fields manually but omitting `_orchestrator`. The new managed voice
guard accesses that collaborator, producing an AttributeError. Parent authorized
a fixture-only repair in `tests/test_glassbrain_events.py`: initialize the actual
constructor's optional collaborator to `None`. Existing start/stop assertions and
production managed-context guards stay unchanged.

```bash
cd feral-core
env FERAL_HOME=/private/tmp/feral-full-triage-voice2-home FERAL_DATA_HOME=/private/tmp/feral-full-triage-voice2-home ../.venv/bin/python -m pytest tests/test_glassbrain_events.py tests/test_turn_attribution.py tests/test_runtime_context_activation.py -q --tb=short --no-cov -p no:randomly --timeout=60
```

Result: **86 passed, 6 warnings in 2.49s**, including real managed-context
activation/refusal coverage. Ruff on all four repaired fixture files and scoped
diff check passed. Doctor sources/tests remain read-only; their unrelated live
permission probe needs a separately reviewed unit-fixture isolation repair.

## Constraints for the next complete run

1. Finish and freeze the reviewed narrow regressions, record a fresh source hash,
   and preserve the failed full log. Parent owns source publication.
2. Run fresh fail-fast diagnostic slices with short/full traceback output before
   another 12,000-test accumulation. Default sandbox cannot bind test localhost
   servers; escalate legitimate isolated loopback tests at the first confirmed
   restriction, preserving outbound-call guards and fake keychain isolation.
3. Keep test homes, databases and audit paths disposable after every environment
   cleaner. Do not solve an isolation error by granting access to operator data.
4. Instrument descriptor counts and fixture teardown failures during the full
   rerun. A larger child-process soft descriptor limit may avoid a known macOS
   ceiling, but does not establish or fix a source leak; record its exact value.
5. Preserve coverage for the required final acceptance. No-coverage reproductions
   explain failures but cannot substitute for the coverage threshold or a normal
   final pytest summary. Keep the coverage database in a disposable location.
6. Physical macOS permission-dependent tests need their own explicit acceptance
   conditions. Never equate Linux CI, loopback escalation or app build success
   with granted Accessibility/ScreenCapture or physical-glasses validation.

The full failure is a mix of confirmed sandbox constraints, an attributable
fixture regression, older isolation defects and an unresolved late teardown
cascade. It is not evidence that hundreds of independent runtime features broke,
and it is not evidence that they all work. Parent must record the next complete
frozen local/remote result before claiming release acceptance.
