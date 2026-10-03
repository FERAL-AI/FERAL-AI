# Type release gate and distribution evidence

October 2, 2026. This bounded repair preserves runtime behavior and the committed
812-error type baseline. Source inspection, fixture tests, type discovery and
distribution acceptance are separate evidence classes.

## Exact published-source CI

Published source `d2158fc6a88586aa443f5adf24e471ee7a90d29f` completed
[CI37086090592](https://github.com/FERAL-AI/FERAL-AI/actions/runs/37086090592)
successfully. Its backend PR lane passed **12,654 tests,83 skipped,592 warnings
in841.15 seconds**, with **74.77% coverage**, above the unchanged50% floor.
The same installed-brain job passed112 generic Python SDK tests. Web coverage,
stubbed-API Playwright, generic Node and device SDKs, extension, registry, Ruff,
architecture, syntax and bundled-asset checks passed. The
[native workflow](https://github.com/FERAL-AI/FERAL-AI/actions/runs/37086090563)
and docs/naming/version checks passed. Live-brain browser and main-branch Linux
matrix jobs were skipped. These results do not operate a GUI or certify external
accounts, physical devices, installation or signing.

The explicitly non-blocking
[mypy job111096592064](https://github.com/FERAL-AI/FERAL-AI/actions/runs/37086090592/job/111096592064)
failed with **818 errors in234 files,1,264 checked** against812. Timestamp-stripped
diagnostics were compared by `(file, exact message/error code, multiplicity)`,
ignoring shifted line numbers. There were **22 added and16 removed occurrences**,
net+6. This does not attribute every changed message to the latest commit; the
health-platform message, for example, replaced another error at the same count.
The workflow's printed per-file comparison includes notes; error-only normalized
diagnostics are used here.

## Bounded implementation

- `hardware/mesh.py`: the authorization map names its actual `HardwareReview`
  values; the lazily installed controller is explicitly optional. Exact owner,
  generation, authorization consumption, transport and outcome checks are unchanged.
- `agents/multi_agent.py`: accumulated call/result dictionaries match the existing
  `WorkerResult` contracts. The working tool list explicitly includes the absent
  value returned by the existing iteration filter and accepted by query routing.
  No new conversion or dispatch behavior is introduced.
- `skills/impl/places.py`: the private connection cache names its actual optional
  `MCPServerConnection`, imported only for typing. Connection creation, reuse,
  location permission, source attribution and failure behavior are unchanged.

These changes remove seven attributable diagnostics in three reserved files.
No explicit Any annotation, cast, ignore, suppression, configuration alteration
or baseline increase was added. Existing dictionary contracts are retained.

## Targeted verification

From `feral-core`, with the pinned interpreter and disposable home/data/audit:

```sh
env FERAL_HOME=/private/tmp/feral-release-types-home \
  FERAL_DATA_HOME=/private/tmp/feral-release-types-home \
  FERAL_AUDIT_LOG_PATH=/private/tmp/feral-release-types-home/audit.log \
  ../.venv/bin/python -m pytest \
  tests/test_hardware_mesh.py tests/test_hardware_mesh_device_announce.py \
  tests/test_reviewed_hardware_dispatch.py tests/test_reviewed_hardware_integration.py \
  tests/test_multi_agent.py tests/test_multi_agent_gates.py \
  tests/test_multi_agent_identity_and_budget.py tests/test_multi_agent_preserves_images.py \
  tests/test_workers.py tests/test_places_skill.py \
  -q --tb=short --no-cov -p no:randomly --timeout=60
../.venv/bin/python -m mypy -p hardware.mesh -p agents.multi_agent \
  -p skills.impl.places --follow-imports=silent --no-incremental
../.venv/bin/python -m ruff check hardware/mesh.py agents/multi_agent.py \
  skills/impl/places.py --select=E,F,W --ignore=E501,E402,F401,W291,W293
```

**172 tests passed,seven warnings in3.09 seconds.** Warnings include existing
dependency/lifespan notices, an aiosqlite cleanup-thread warning and restored
test environment leakage. Fixtures exercise exact hardware ownership and denial,
transport/readback, multi-agent policy/budget/image behavior, and permission-bound
attributed places results. No real hardware, Google request or model inference ran.
Ruff passed. Focused mypy reports three pre-existing baseline diagnostics:
hardware adapter incompatibility, hardware optional-parameter default and
multi-agent optional bus default. It is not a whole-file type-clean claim.

## Frozen integrated-source measurement

After all backend workers froze their production and test files, the configured
whole-core command ran under mypy 1.20.2 and Pydantic 2.13.4 on this macOS host:

```sh
../.venv/bin/python -m mypy . \
  --cache-dir=/private/tmp/feral-release-frozen-mypy-cache-20261002
```

The fresh-cache run completed in **13.87 seconds**, reporting **811 errors in
233 files, 1,269 source files checked**. Its diagnostic exit code was 1. The
local count satisfies the unchanged count ratchet, 811 <= 812; this is not a
published Linux CI result. Configuration and the tracked baseline were not
modified.

Before/after manifests cover all non-generated, non-hidden Python `.py`/`.pyi`
inputs plus type/dependency configuration and the tracked baseline within
`feral-core`, excluding build, dist, caches and node_modules. Both manifests
contain **1,273 files** and have the identical aggregate SHA-256:

`dab3b76d07ad35fd83f90b6f4b5f87a55dbaf3bf3ff090d75a1f918205e6029a`

Error-only normalized comparison by `(file, exact message/error code,
multiplicity)`, ignoring line shifts, found **zero added and seven removed
diagnostics versus the exact d215 CI job's 818**. The seven removals are exactly
the bounded annotation repairs above. Archive, interrupted-context recovery and
this wave's release additions introduced no new diagnostics in that comparison.

Against the tracked 812 baseline, **15 added and 16 removed occurrences remain**,
net -1. The remaining added occurrences are five in `agents/orchestrator.py`,
one each in `agents/proactive_engine.py`, `api/state.py`, `config/loader.py`,
`integrations/health_platforms.py` and `security/vault.py`, three across proactive
freshness tests, one in the memory-writing test and one in the voice-header test.
The health-platform message replaces a prior diagnostic. These messages already
exist in the d215 job and are not erased by a lower aggregate count. Existing
vault diagnostics also remain; the result is not a whole-core type-clean claim.

Local evidence is retained at
`/private/tmp/feral-release-frozen-mypy-20261002.log`, its `.before.json` and
`.after.json` source manifests, and
`/private/tmp/feral-release-frozen-mypy-comparison-20261002.json`. Source and
baseline remained frozen throughout the run. Exact-source remote integration
CI and actual app acceptance are separate remaining checks.

### Final archive error-path integration

After temporary-file creation failures were converted to typed archive refusals
and two tests were added, a second frozen fresh-cache measurement completed in
13.67 seconds: **811 errors in233 files,1,269 checked**, diagnostic exit1.
The baseline remained812. Before/after manifests matched across1,273 inputs:
`05f734ffd8effc15a3cc579b8e70c2a18e028deb6b4e73a9049eaac596e9e487`.
Normalized comparison again found zero added/seven removed versus d215 and
15 added/16 removed versus the tracked baseline. Evidence uses the prefix
`/private/tmp/feral-completion-final-mypy-20261002`; its comparison is retained
at `/private/tmp/feral-completion-final-mypy-comparison-20261002.json`.
The final24-suite backend integration passed570 tests/19 warnings in18.99s.
This is local current-source evidence; remote Linux CI remains separate.

## Distribution observations

Read-only checks on this host reported **6.9GiB available** before the slice.
No cleanup was performed. Packaging must repeat the capacity check.
`security find-identity -v -p codesigning` reported **zero valid identities
accessible to that invocation**; no certificate names or credential values were
enumerated in the evidence. `notarytool` is installed. The current native build
script signs ad hoc, and the existing preview bundle reports an ad-hoc signature
with no TeamIdentifier. This does not prove that remote signing credentials are
unavailable; their authorization/availability was not inspected.

Developer-ID signing/notarization, signed vault operation, clean-machine install,
migration/update/rollback and physical account/device acceptance remain separate
release gates. No binary was rebuilt, distributed or published by this card.
