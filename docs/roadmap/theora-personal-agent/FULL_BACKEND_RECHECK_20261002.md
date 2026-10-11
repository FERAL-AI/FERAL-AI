# Backend failure recheck and temporary observer — 2026-10-02

## Scope and source

Fresh fail-fast diagnostics after publication of core `90b75587a` and generic
SDK `205f468a2b783e847acfe810e2e753d3785f89ad`. No full-suite run was started by
this worker. The four previously repaired fixture files and all production
sources stayed frozen. Parent later authorized one additional fixture-only
change in `tests/test_doctor_tailscale.py` after the first observed runs ended.

Every slice used a separate disposable FERAL_HOME/FERAL_DATA_HOME and temporary
COVERAGE_FILE. Coverage instrumentation remained enabled (`--cov=.` from repo
configuration); `--cov-fail-under=0` was used only because a selected diagnostic
slice cannot establish the whole project's 50% floor. These runs do **not**
replace required full coverage acceptance. Flags were `-q -x --tb=short
-p no:randomly --timeout=60`. Existing outbound-call/fake-keychain fixtures stayed
enabled; no account, OS-permission grant or personal profile was connected.

## Measured results

| Fresh slice | Sandbox result | Legitimate outside-sandbox result |
|---|---|---|
| `test_retrieval_correctness.py`, `test_state_mock_getattr_guard.py` | 29 passed, 8 warnings, 20.20s | Not needed |
| `test_mcp_http_transport.py` | First test fails at temporary `127.0.0.1:0` bind with PermissionError | 8 passed, 7 warnings, 7.97s |
| `test_relay_client.py` | 4 passed then own test-port bind raises PermissionError | 20 passed, 8 warnings, 7.81s |
| Browser stub-CDP socket test and both CLI piped-install consent tests | First browser bind raises PermissionError | 3 passed, 7 warnings, 8.92s |
| Config, config-loader, integration, Glass Brain, primary-transcript, SQLite-interpreter-feature and workspace-grant-pruning suites | 124 passed, 12 warnings, 26.37s | Not needed |
| CLI update `TestItAimsAtTheRunningInterpreter` | 2 passed then own-process interpreter lookup returns unavailable | 6 passed, 5 warnings, 6.89s |
| Tailscale doctor suite plus denied-TCC Settings-deeplink test, after authorized fixture pin | 24 passed, 6 warnings, 12.13s | Not needed |

For CLI update, source `cli/update_command.py:145` uses macOS
`ps -p <own-pid> -o comm=` and returns None on any unavailable lookup. The
sandbox test records None, not a surfaced PermissionError. Passing the identical
class outside the sandbox supports an environment restriction for that slice;
the evidence does not identify the swallowed subprocess error precisely.

Coverage finalization and ordinary pytest summaries completed for every passing
slice. Retrieval's previous full-run failures and the two globally patched
SQLite/Path tests do not reproduce in these fresh processes. The original
full-run order-dependent contamination remains unlocated; this does not prove
all failures in that run share one cause.

Private logs are `/private/tmp/feral-recheck-{retrieval,mcp,mcp-escalated,relay,
relay-escalated,socket,socket-escalated,fixtures,cli,cli-escalated,doctor}.log`.
Corresponding coverage data stayed under `/private/tmp`.

## Doctor fixture isolation repair

`doctor_home` already stubs unrelated providers, pair origin and vector backend
to test Tailscale severity independently. It now also stubs
`security.macos_permissions.all_gui_permission_statuses` using actual TCCStatus
rows for Accessibility and Screen Recording with deterministic `granted`
results. This changes the unit fixture, never the real OS grant or production
doctor reporting. Both original assertions that unrelated Suggested fixes are
absent remain intact. The separate denied-TCC fixture still verifies actual
Settings deeplink rendering and passes in the same run.

Ruff (`E,F,W` with repository gate exclusions) and scoped `git diff --check`
passed for the fixture. This is the only repository code/test edit in this card.

## Temporary descriptor and teardown observer

Reviewed worker-created helper: `/private/tmp/feral_recheck_observer.py`.
It is an opt-in pytest plugin, not a production or committed dependency. It:

- records inherited RLIMIT_NOFILE and descriptor counts at test teardown;
- records whether Path.exists and sqlite3.connect match initial process
  references after teardown, without changing or restoring either;
- writes ordinary failure phase/node ID/traceback when the report is created,
  before end-of-session coverage summary can crash;
- records internal errors, session exit status and maximum sampled count.

Output is private append-only JSONL opened with mode 0600. It never dumps
environment variables, credentials or descriptor targets. It caches its own
write/list-directory functions so unrelated test mocks do not deliberately
replace its observer. It does not force monkeypatch cleanup, stop coverage,
change fixture assertions, run garbage collection, raise resource limits or
otherwise repair the measured process. A single held log descriptor adds
observer overhead; enumeration itself can add one descriptor. The measured
maximum is a sample lower bound, not a continuous peak or a leak diagnosis.

| Observed slice | Start descriptors | Maximum sampled | End descriptors | Patched tracked functions after teardown |
|---|---:|---:|---:|---|
| Relay, 20 tests | 11 | 17 | 13 | None |
| Browser/CLI sockets, 3 tests | 11 | 18 | 17 | None |
| Fixture and global-mock recheck, 124 tests | 11 | 50 | 23 | None |

All three inherited soft limit **256**, hard limit **9223372036854775807**.
Their observed report failures/internal errors were zero. The sandbox CLI
failure demonstrates that the observer saves the ordinary assertion traceback
before finalization. These bounded slices do not measure the failed full
process's descriptor count and do not exclude accumulation across 12,000 tests.

## Proposed parent-owned full diagnostic

After parent freezes source and records its digest, reuse the required full
suite command with only this diagnostic plugin and private output added. From
`feral-core`, an explicit proposal is:

```bash
env FERAL_HOME=/private/tmp/feral-full-recheck-home FERAL_DATA_HOME=/private/tmp/feral-full-recheck-home FERAL_AUDIT_LOG_PATH=/private/tmp/feral-full-recheck-home/audit.log COVERAGE_FILE=/private/tmp/feral-full-recheck.coverage PYTHONPATH=/private/tmp FERAL_TEST_OBSERVER_LOG=/private/tmp/feral-full-recheck-observer.jsonl ../.venv/bin/python -m pytest tests/ -v --tb=short -p no:randomly -p feral_recheck_observer --timeout=60 > /private/tmp/feral-full-recheck.log 2>&1
```

Parent must run this outside the sandbox for legitimate localhost bindings,
keeping disposable-home and account/network guards. No coverage-floor override
is proposed for the full run. Preserve default/explicitly recorded resource
limits on the first observed run; a changed child-process limit would be a
separate measured environment change, not a source leak fix. Use fresh output
names if repeating, since the observer appends.

If ordinary failures or teardown corruption recur, the JSONL report sequence
identifies the first failing phase and first surviving tracked mock before the
terminal plugin fails. Stop and inspect that bounded ordering segment rather
than attributing hundreds of later errors to disk or descriptors without a
trace. Actual macOS GUI entitlement-dependent tests may still need separate
environment reporting; these loopback rechecks do not grant or validate TCC.

No full-suite success, coverage-floor acceptance, native acceptance or remote
CI status is claimed here.

## Subsequent remote fixture failure and voice ownership regression checks

Parent subsequently reported the completed Ubuntu backend run at
`205f468a2b783e847acfe810e2e753d3785f89ad`: 12,540 passed, one failed, 83 skipped,
74.66% coverage. This worker did not independently retrieve that job's log.
The sole reported failure was
`test_voice_disconnect_teardown.py::test_web_disconnect_stops_voice`; the local
full diagnostic had not started when that result arrived.

Source inspection confirmed `VoiceRouter.nodes_bound_to_session` returns the
list of nodes mapped to the exact session, with `[]` for no bindings.
`api/runtime_context.py::close_surface` stops web-session voice only after an
exact owned-socket check and a known empty binding list; a bound phone or failed
ownership lookup must preserve its voice. The old fixture returned an implicit
MagicMock for this newly inspected method, so it could not establish no owners.

Parent authorized only `tests/test_voice_disconnect_teardown.py` plus this
evidence append. The fixture now returns the legitimate empty list explicitly.
Existing stop/disconnect assertions and production guard remain unchanged.
Two added registered-WebSocket tests verify that closing a web socket preserves
an actual VoiceRouter phone binding and that a failed binding lookup cannot
authorize teardown. This covers ownership safety rather than weakening the
guard to accommodate an undeclared mock.

```bash
cd feral-core
env FERAL_HOME=/private/tmp/feral-recheck-voice-teardown-home FERAL_DATA_HOME=/private/tmp/feral-recheck-voice-teardown-home COVERAGE_FILE=/private/tmp/feral-recheck-voice-teardown.coverage PYTHONPATH=/private/tmp FERAL_TEST_OBSERVER_LOG=/private/tmp/feral-recheck-voice-teardown-observer.jsonl ../.venv/bin/python -m pytest tests/test_voice_disconnect_teardown.py tests/test_runtime_context_activation.py tests/test_runtime_context_lifecycle.py tests/test_runtime_context_ingress.py -q -x --tb=short -p no:randomly -p feral_recheck_observer --timeout=60 --cov-fail-under=0
```

Local result: **108 passed, 42 warnings in 14.38s**, with coverage enabled,
ordinary final summary present and diagnostic-only floor override. Ruff and
scoped diff check passed. Source is frozen after these checks for parent-owned
publication/full-run verification. No production file or previously repaired
fixture was edited in this follow-up.

## Required full recheck in progress

Parent started the required PR-equivalent whole backend run after both fixture
repairs. The core is frozen:1,261 Python files, manifest digest
`39a8b055528037f4a4652d67f9372a32c4871b3fed10791be3333ae14ca7d1d8`.
The command explicitly retains coverage and its50% floor, excludes `tests/perf`,
and uses the CI60-second per-test thread timeout. It runs outside the sandbox
because localhost bind/process restrictions were previously reproduced there;
the runtime/audit homes and outputs remain disposable private temporary paths.

```bash
env FERAL_HOME=/private/tmp/feral-full-recheck-home FERAL_DATA_HOME=/private/tmp/feral-full-recheck-home FERAL_AUDIT_LOG_PATH=/private/tmp/feral-full-recheck-home/audit.log COVERAGE_FILE=/private/tmp/feral-full-recheck.coverage PYTHONPATH=/private/tmp FERAL_TEST_OBSERVER_LOG=/private/tmp/feral-full-recheck-observer.jsonl ../.venv/bin/python -m pytest tests/ -v --tb=short -p no:randomly -p feral_recheck_observer --cov --cov-report=term-missing --cov-fail-under=50 --ignore=tests/perf --timeout=60 --timeout-method=thread > /private/tmp/feral-full-recheck.log 2>&1
```

No completed full result is claimed at this checkpoint. Parent must compare the
manifest after completion and record the actual final summary, coverage,
descriptor/mock observer findings and any first causal failure separately.

## Completed required run

The run above completed with exit0: **12,576 passed,50 skipped,574 warnings in
818.52s; coverage75.03%**, above the unchanged50% floor. Parent recomputed all
1,261 manifest hashes after completion: **changed=[]**. The exact tested core
is in commit407d158b2 and native integrationdd69c7bf5, whose core files are
identical. Subsequent core cards require their own validation.

The read-only observer recorded12,624 teardown reports, no failed-phase or
internalerror events and no surviving tracked `Path.exists`/`sqlite3.connect`
replacement. Peak sampled descriptors148 against inherited soft limit256;
final sample48. Sampling is not a proof of every transient resource peak, but
there was no observed exhaustion or coverage-finalizer crash. The earlier
cascade did not recur after isolated-home fixtures and legitimate outside-sandbox
socket/process execution; its exact earlier leak origin remains unproven.

This is full local macOS/Python3.11 PR-equivalent backend acceptance, excluding
performance tests. It does not certify Ubuntu, GUI, real voice/accounts/devices,
all platform permission states, or distribution. The50 local skips differ from
the83 Ubuntu skips; counts must not be merged. Logs/observer/private test homes
remain outside Git. The prior failed run is retained separately.

The subsequent exact-source Ubuntu3.11 PR job also passed ondd69c7bf5:
[job111084390650](https://github.com/FERAL-AI/FERAL-AI/actions/runs/37082003338/job/111084390650),
**12,543 passed,83 skipped,592 warnings in702.14s; coverage74.66%**. Parent
retrieved its terminal log. Generic Python SDK112 passed, Node standalone81
passed and the installed-runtime Node step1 passed with0 skips. Native source
CI37082003388 also passed; actual GUI acceptance is a separate blocked gate.
Nonblocking mypy still reports845 against unchanged812 baseline.
