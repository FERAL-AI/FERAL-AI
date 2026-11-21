# Native task, layout and context integration

October 2, 2026. Parent integration on published base
`cd1571ef94aa2fe244023d56ba468f7ba3266700` plus this reviewed source wave.
The checkpoint commit identifies its source; candidate identity and actual GUI
results are recorded separately. This is not a release certification.

## Integrated behavior

- Native chat negotiates durable whole-turn support before sending, saves the
  exact request marker before dispatch, correlates progress, stops only the
  accepted turn, and reconciles uncertain outcomes without replay. Durable
  processing completion is distinct from external effects and read/seen.
- Selectable text measures a copied cell instead of mutating an attached field
  during layout. Source invariants and small actual Security/copy probes pass;
  the full9.28 failure remains separate.
- Native Forge supports actual proposals, statistics and reviewed unapproved
  drafting, with fresh target checks and truthful uncertain outcomes. Backend
  atomic creation and durable generation recovery remain open.
- Explicit interval automations bypass the natural-language schedule parser.
  Action text containing “every email” cannot change the reviewed interval.
- An internal, inactive-until-installed runtime checkpoint lifecycle restores
  trusted model context after restart and awaits its commit before terminal
  processing success. Production activation, all-writer fencing and legacy
  migration are the next slice. UI transcripts are never trusted context.

## Frozen backend verification

All production owners held these backend files during this run. From
`feral-core`:

```sh
env FERAL_HOME=/private/tmp/feral-context-native-integration-home \
  FERAL_DATA_HOME=/private/tmp/feral-context-native-integration-home \
  ../.venv/bin/python -m pytest \
  tests/test_runtime_context_lifecycle.py tests/test_runtime_session_checkpoints.py \
  tests/test_chat_turn_progress_identity.py tests/test_structured_automation_api.py \
  tests/test_stream_nonstream_parity.py tests/test_orchestrator.py \
  tests/test_orchestrator_deep.py tests/test_chat_turn_receipts.py \
  tests/test_chat_turn_abort.py tests/test_chat_turn_failure_wire.py \
  tests/test_timeline_episode_source.py \
  tests/test_scheduler_unparseable_cron_disables.py \
  -q --no-cov -p no:randomly --timeout=60
```

Exit0: **235 passed,7 warnings in9.38s**. Separate-process lifecycle tests send
restored context into the next controlled provider request and prove historical
tools are not executed again. The actual router/SQLite automation tests do not
start a scheduler callback. Warnings are retained in the individual evidence.

Targeted owner-progress verification passed32 tests after correcting a new
annotation NameError; its initial failed run remains in
[progress evidence](CHAT_PROGRESS_IDENTITY_EVIDENCE.md). Edited backend Ruff
and `git diff --check` passed. Two activation baseline tests passed separately;
they test refusal/read-only behavior, not implemented production activation.

## Native and candidate gates

The frozen full native runner exited0: all36 feature suites, linked25 model
fixture groups, desktop39 assertions and error5 assertions passed. Forge86 and
Workflow70 assertions are included. Command: `bash test_features.sh` from
`desktop-native`, log `/private/tmp/feral-native-wave3-fixtures-20261002.log`.
Production `bash build.sh --typecheck` exited0 on the same frozen Swift source,
log `/private/tmp/feral-native-wave3-typecheck-20261002.log`. The first fixture
and typecheck attempts failed because
the sandbox blocked Xcode's SwiftUI macro compiler. The properly escalated rerun
is separate; no source or assertion was weakened to bypass that failure.

Actual child-lifeline, bundle-auditor and source-equality regression fixtures
passed18 tests in1.27s, exit0, from repository root:
`.venv/bin/python -m pytest desktop-native/test_native_lifeline.py
scripts/tests/test_check_native_bundle.py scripts/tests/test_check_packaged_core.py
-q --no-cov -p no:randomly`. These test the checkers/lifecycle mechanism;
the next assembled bundle must still be inspected and operated separately.

The next assembly version is **2026.9.29/build2026100203**. It must be built from
committed frozen source, pass resource equality and bundle audit, then undergo
actual isolated Security/rich-chat/three-turn/Stop/status/quit/relaunch acceptance.
The version change alone does not establish a built or functioning app.

## Detailed evidence and remaining boundaries

[Native chat](NATIVE_CHAT_RECEIPTS_EVIDENCE.md),
[layout](NATIVE_SELECTABLE_LAYOUT_EVIDENCE.md),
[Forge](NATIVE_FORGE_PARITY_EVIDENCE.md),
[structured automation](STRUCTURED_AUTOMATION_EVIDENCE.md),
[inactive lifecycle](RUNTIME_CONTEXT_LIFECYCLE_EVIDENCE.md), and
[next activation contract](RUNTIME_CONTEXT_ACTIVATION_EVIDENCE.md) record tests,
failure cases, exact source identities and deferred acceptance.

The immutable [9.28 report](NATIVE_9_28_ACCEPTANCE.md) remains failed. No real
account, external message, payment, private-data reset, merge or release occurred.
Full concurrent document saves, lifetime/shared memory, voice, physical devices,
signing/notarization, upgrades, Linux, messaging/commerce and social use retain
their explicit gates in [release readiness](RELEASE_READINESS.md) and
[all requirements](REQUEST_COVERAGE.md).
