# macOS AX depth-limit verification

Publication note: checkout paths are portable placeholders. Set `EVIDENCE_ROOT` to a new disposable directory outside personal/app data before running the commands below, for example `export EVIDENCE_ROOT="$(mktemp -d)"`. Evidence filenames identify historical local outputs, not shipped archives or fresh reruns. `<theora-ios-checkout>` denotes the separate Theora iOS repository.

2026-09-30. Read-only Finder accessibility snapshots; no actions/clicks, provider calls, or application source changes. Only `ASOS/feral-core/tests/test_macos_ax.py` changed.

The full-suite report had a live depth test observing timeout instead of max_depth. Exact isolated test initially skipped in sandbox because Accessibility was not granted to that process. The same command with approved escalation passed: **1 passed, 5 warnings in 10.70s**. This does not reproduce a consistent product defect.

Source inspection found that baseline requested `MAX_TIMEOUT_S=30`, while the second depth-bounded snapshot used default timeout 8s. Added a deterministic endpoint-level test with a 15-node chain and simulated one-second-per-node descriptions. It uses actual snapshot/collect/walk/header logic, mocks only app/AX bindings and module clock, and does not sleep:

- 30s full walk completes, visits 15 nodes, no limits.
- Default 8s bounded walk visits 8 nodes and honestly reports `timeout` plus `WALK STOPPED EARLY (timeout)`.
- 30s bounded walk visits 14 nodes and honestly reports `max_depth` plus corresponding header.

Live test now gives both snapshots the same maximum timeout. Its success/max_depth/header assertions are retained; no additional timeout skip was added to the bounded result. This corrects a timing-assumption collision and strengthens deterministic coverage without changing runtime behavior. Synthetic node depth comes from structured `node.depth`, because formatted tree indentation intentionally clamps at 12.

Working directory: `<checkout>/ASOS/feral-core`.

```sh
FERAL_HOME=${EVIDENCE_ROOT:?}/theora-commerce-probe-home FERAL_DATA_HOME=${EVIDENCE_ROOT:?}/theora-commerce-probe-home/data FERAL_EMBED_PROVIDER=hash ../.venv/bin/python -m pytest tests/test_macos_ax.py -q --no-cov -p no:randomly --tb=short -rs
```

Result after correction: **62 passed, 14 skipped, 5 warnings in 1.11s**. All skips are pre-existing live tests gated on sandbox Accessibility grant.

```sh
FERAL_HOME=${EVIDENCE_ROOT:?}/theora-commerce-probe-home FERAL_DATA_HOME=${EVIDENCE_ROOT:?}/theora-commerce-probe-home/data FERAL_EMBED_PROVIDER=hash ../.venv/bin/python -m pytest tests/test_macos_ax.py::test_a_depth_limit_is_reported_not_hidden -q --no-cov -p no:randomly --tb=short -rs
```

Same read-only command executed with approved escalation after correction: **1 passed, 5 warnings in 10.65s**. Warnings are existing Pydantic field naming and FastAPI lifespan deprecations.

`../.venv/bin/python -m ruff check tests/test_macos_ax.py` and `git diff --check` passed. No claim of comprehensive accessibility control testing or safe financial computer use follows from this bounded depth-reporting check.
