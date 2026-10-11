# Focused health, ambient, device and memory verification

Publication note: checkout paths are portable placeholders. Set `EVIDENCE_ROOT` to a new disposable directory outside personal/app data before running the commands below, for example `export EVIDENCE_ROOT="$(mktemp -d)"`. Evidence filenames identify historical local outputs, not shipped archives or fresh reruns. `<theora-ios-checkout>` denotes the separate Theora iOS repository.

Date: September 30, 2026. Read-only application source review plus selected existing tests. No application source edits, hardware access, iOS builds, clinical validation or provider/model calls were performed. Test LLMs and cloud health contributors are fake/mocked; embeddings use the hash provider. This verifies software behavior in the selected test harness, not production readiness.

## Exact command and result

Working directory: `<checkout>/ASOS/feral-core`

```bash
FERAL_HOME=${EVIDENCE_ROOT:?}/theora-health-verification-20260930 FERAL_DATA_HOME=${EVIDENCE_ROOT:?}/theora-health-verification-20260930/data FERAL_EMBED_PROVIDER=hash ../.venv/bin/python -m pytest tests/test_health_ingest_route.py tests/test_health_summary_live_wearable.py tests/test_biometric_freshness.py tests/test_baseline_rejects_sensor_dropouts.py tests/test_biometric_history_covers_every_vital.py tests/test_ambient_transcript_frame.py tests/test_ambient_conversation_recall.py tests/test_node_subdevices.py tests/test_memory_recent_summary.py -q --no-cov -p no:randomly --tb=short
```

Result: **113 passed, 15 warnings in 11.49 seconds**, process exit code 0. No skipped or failed tests were reported. No escalation was needed.

## What the focused cases establish

- Health ingestion route registration and phone-bearer allowlist coherence; memory-save calls with readable values/tags; malformed envelopes and samples. Route tests use a minimal FastAPI app and mocked memory. They do not establish real HealthKit permission or end-to-end iPhone uploads.
- Health summary access to glasses/wristband observations when cloud contributors are absent; source naming; keeping cloud resting values when present; provider exceptions handled. Contributors are mocked/local callables.
- Biometric freshness/source priority and protection from invalid baseline inputs, including NaN/infinity and sensor-dropout ranges. Plausibility filters are software checks, not sensor accuracy or medical safety validation.
- Historical coverage across biometric fields.
- Ambient handler persistence-before-ACK, duplicate/resumable delivery behavior, conversation recall/schema agreement and commitments filtering using temporary SQLite and fake model/socket collaborators. These tests call handler/storage paths; they are not a live WebSocket/iOS/device test.
- Node-subdevice SQLite persistence, updates/removals, provenance-specific liveness and stale transitions.
- Recent conversation summaries across sessions, ordered prompt inputs, cache/no-content/error behavior with a fake model.

## Material source finding despite passing tests

`integrations/health_platforms.py:1006-1012` promotes a fresh live wearable `heart_rate` value into `summary["resting_hr"]` when no cloud resting value exists. `tests/test_health_summary_live_wearable.py` explicitly expects that fallback. A current HR reading is not established as a resting HR measurement by freshness alone; test success preserves this semantic conflation rather than disproving it.

Before product release, specify current-versus-resting semantics and acceptance fixtures. Preserve current HR with its source/timestamp, and leave resting HR unavailable unless a documented rest-qualified algorithm/source supports it. This is a planning correction recommendation, not an implemented fix.

## Warnings and limitations

- Ambient tests generated `PytestUnhandledThreadExceptionWarning` from aiosqlite worker threads attempting callbacks after an event loop closed, plus `PytestUnraisableExceptionWarning` involving unfinished `MemoryStore.episode_save`. This suggests test/background-task cleanup or lifecycle issues requiring focused diagnosis before presenting ambient lifecycle verification as clean.
- The environment guard reports `FERAL_HOME` left changed by 35 tests and restored afterward. Isolation prevented use of the normal FERAL home, but these cases have cleanup hygiene debt.
- Other warnings concern Starlette/FastAPI deprecations and a Pydantic schema-name shadow.
- No clinical correctness, sensor accuracy, ECG/raw-PPG hardware availability, real BLE background collection, voice quality, real network relay, cloud provider or full cross-device memory synchronization claim follows from these 113 passes.
- Hash embeddings do not verify semantic retrieval quality. No production accounts, personal health data, API keys or vendor secrets were inspected.

## Follow-up bounded fix, authorized by parent

Independently instantiated the real `HealthAggregator` with a local provider
returning glasses HR=145. Before the fix, its summary returned resting_hr=145,
and `build_health_update` emitted both Heart Rate and Resting Heart Rate at 145.
After the fix, resting_hr is null and the HUP frame only contains Heart Rate.

The regression-first run of the live wearable/history tests produced 4 failures
and 12 passes, all failures demonstrating unqualified data in the resting field.
Removed the live-sample and historical-minimum summary promotions. Explicit
trend estimates remain available; Whoop/Oura-reported resting data is retained.
Updated manifest wording and meaningful summary/HUP regressions. No schema or
public signature change; no iOS changes.

Post-fix command, same core working directory:

```bash
FERAL_HOME=${EVIDENCE_ROOT:?}/theora-health-fix-20260930 FERAL_DATA_HOME=${EVIDENCE_ROOT:?}/theora-health-fix-20260930/data FERAL_EMBED_PROVIDER=hash ../.venv/bin/python -m pytest tests/test_health_summary_live_wearable.py tests/test_glasses_vitals_history.py tests/test_health_data_manifest.py tests/test_biometric_freshness.py tests/test_baseline_rejects_sensor_dropouts.py tests/test_hr_pipeline_demo_fixes.py tests/test_whoop_durable_sync.py -q --no-cov -p no:randomly --tb=short
```

Result: **110 passed, 7 warnings in 1.22 seconds**, exit code 0. Warnings are
deprecations/schema shadowing and restored FERAL_HEALTH_CLOUD_RETENTION_DAYS
environment leakage from two tests; this selection did not run ambient tests.

```bash
../.venv/bin/ruff check --select=E,F,W --ignore=E501,E402,F401,W291,W293 integrations/health_platforms.py tests/test_health_summary_live_wearable.py tests/test_glasses_vitals_history.py
```

Result: All checks passed.

Consumer path: health_summary tool calls (`HealthAggregator.execute`), direct
summary consumers, and canonical HUP output built by `build_health_update`,
returned/broadcast by `api/routes/dashboard.py` at `/api/health/frame`.
`integrations/health_canonical.py` labels the old phantom metric "Resting Heart
Rate". Existing raw HR and source reporting remain intact.

Ambient warning diagnosis remained read-only: `test_ambient_transcript_frame.py`
has a synchronous fixture returning state/tasks without draining registered
tasks or closing the real MemoryStore. That is a strong teardown explanation,
not a proven production lifecycle defect. No broad cleanup was attempted.

## Full-suite BP test failure follow-up

The full-suite failure at `test_blood_pressure_end_to_end.py:257` was a
test-owned event-loop dependency, not an observed production BP defect.
The single case initially passed in isolation. Explicitly clearing the current
loop before pytest reproduced the full-suite failure at `get_event_loop()`:

```bash
FERAL_HOME=${EVIDENCE_ROOT:?}/theora-bp-fix-20260930 FERAL_DATA_HOME=${EVIDENCE_ROOT:?}/theora-bp-fix-20260930/data FERAL_EMBED_PROVIDER=hash ../.venv/bin/python -c 'import asyncio,pytest; asyncio.run(asyncio.sleep(0)); raise SystemExit(pytest.main(["tests/test_blood_pressure_end_to_end.py::TestReachesTheDurableStoreForReal::test_the_health_history_tool_returns_it_with_the_product_name","-q","--no-cov","-p","no:randomly","--tb=short"]))'
```

Before test fix: 1 failed, 5 warnings in 1.20s; after fix: 1 passed, 5 warnings
in 1.39s. Converted that case to pytest-managed async/await and used the real
HealthAggregator constructor instead of bypassing initialization with __new__.
No production BP code changed.

```bash
FERAL_HOME=${EVIDENCE_ROOT:?}/theora-bp-fix-20260930 FERAL_DATA_HOME=${EVIDENCE_ROOT:?}/theora-bp-fix-20260930/data FERAL_EMBED_PROVIDER=hash ../.venv/bin/python -m pytest tests/test_blood_pressure_end_to_end.py tests/test_health_summary_live_wearable.py tests/test_glasses_vitals_history.py tests/test_whoop_durable_sync.py -q --no-cov -p no:randomly --tb=short
```

Result: 79 passed, 7 warnings in 1.69s. The changed BP test file also passes
the repository Ruff rule set. A full-suite process which collected the old test
before this edit still needs an affected-case rerun; root was notified.
