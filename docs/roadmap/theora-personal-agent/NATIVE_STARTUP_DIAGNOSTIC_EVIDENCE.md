# Native startup diagnostics

October 3, 2026. This change adds bounded startup observations and corrects the
disposable Process fixture's app-info contract. The remote startup failure remains
unexplained; local passing results do not close remote acceptance.

## Observed failure and resulting behavior

Native CI [37134269877](https://github.com/FERAL-AI/FERAL-AI/actions/runs/37134269877)
timed out at the unchanged 30-second fixture deadline. Retained evidence shows
the native host entered its first launch, the production Python launcher ran,
and the synthetic server spawned its child. The process receipt is written
before HTTP server construction, so it does not establish listener readiness.
There was no observation distinguishing `Process.run()` returning from the
first native health request completing.

The missing initial `health:starting` event has a separate source explanation:
the coordinator emits during `prepare()`, before `BrainRuntime` assigns the
returned owner. Its exact-owner event guard drops that event. Its absence is
not evidence of a blocked actor or failed process launch.

`BrainRuntime` now records fixed, owner-fenced phases for launch entry/return,
initial health request entry/response/error, and coordinator verification
entry/return. Codes are allowlisted and attempts are bounded. Logs contain no
URL, credential, response body, task content or raw exception detail. An ATS
transport refusal has an explicit diagnostic code. Request/retry timing,
startup cancellation, ownership checks and execution authority are unchanged;
this change does not add a resource timeout.

The fixture records bounded server construction and health GET/write phases.
An independent passive observer verifies the listener's synthetic instance,
backend/child identity and live owned process group. It sends only `/health`
requests, never agent tasks or lifecycle commands. Observations do not grant
runtime readiness or replace the original ownership assertions. Evidence export
remains allowlisted and capped at 64 KiB per file.

The actual app's assembler declares
`NSAppTransportSecurity: {NSAllowsLocalNetworking: true}`; the old fixture omitted
it. The fixture now reads and validates that literal policy without executing
assembly, includes it in `Info.plist`, and records the assembler source hash.
This confirms and repairs metadata drift. It does not establish ATS as the cause:
the unchanged fixture also passed locally before this correction. The observed
remote/local Python version difference likewise has no established causal link.

## Frozen implementation identity

| File | SHA-256 |
| --- | --- |
| `desktop-native/BrainRuntime.swift` | `18ee6c6f391dc75ea9bd1e8a54eb6fd740168dc4c300367bc09e93ba95930c7c` |
| `desktop-native/acceptance/archive_runtime_owner_fixture.py` | `96134b295a606793a976566ab56eb26627ccf0746a720debf3a5cfdb5baef7d1` |
| `desktop-native/tests/NativeArchiveRuntimeOwnershipTests.swift` | `040523148ab0b4b926ed7739c78d81da1d674ef1869782742af938f6c630c05a` |

Only those three implementation/test paths changed. Immutable candidate 9.34,
its resources and signature were not modified.

## Executed validation

Commands below ran from `desktop-native/` with the pinned repository interpreter.

```sh
../.venv/bin/python acceptance/archive_runtime_owner_fixture.py \
  --evidence-output /private/tmp/feral-native-startup-final-evidence-20261003
../.venv/bin/python -m unittest -v test_native_lifeline.py
../.venv/bin/python -m ruff check --select E,F,W --ignore E501 \
  acceptance/archive_runtime_owner_fixture.py
xcrun swiftc -swift-version 5 -warnings-as-errors \
  -target arm64-apple-macosx13.0 \
  -module-cache-path /private/tmp/theora-native-feature-tests-cache \
  -parse-as-library NativeRuntimeHealthFeature.swift \
  tests/NativeRuntimeHealthFeatureTests.swift \
  -o /private/tmp/feral-native-runtime-health-diagnostic-tests
/private/tmp/feral-native-runtime-health-diagnostic-tests
```

- Final genuine Process fixture: **28 assertions passed**, retaining its
  30-second execution deadline. Both independent health observations verified
  exact ownership; both backend/child groups were absent after completion.
  Root: `/private/tmp/feral-native-archive-owner-zmiwm12v`.
- Runtime-health regressions: **62 assertions passed**. Existing actual child
  lifeline tests: **2 passed**. Ruff, source/generated-server syntax, policy
  positive/three refusal checks and whitespace checks passed.
- Unchanged baseline: **28 passed**, preserved separately at
  `/private/tmp/feral-native-startup-baseline-evidence-20261003`.
- Before the final diagnostic-code refinement, an intentionally shortened
  `--execution-timeout 0.3` run failed as expected and retained phases, server
  GET observations and a failure receipt. Backend and child were absent afterward.
  Evidence: `/private/tmp/feral-native-startup-timeout-evidence-20261003`.
  That historical run used BrainRuntime SHA-256
  `9ff3f39074bf2b836edcec27d4da81c14082db886c34278eec975fcce605051e`.

These checks use a tiny unsigned bundle and a synthetic health-only Python
server through the unchanged production launcher. They do not verify the full
backend, accounts, models, GUI, installation, signing or notarization. Next-head
remote CI must identify the stalled boundary or pass the same bounded fixture.
