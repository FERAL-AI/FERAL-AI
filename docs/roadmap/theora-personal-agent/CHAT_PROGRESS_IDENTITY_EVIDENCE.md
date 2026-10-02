# Trusted chat progress identity

October 2, 2026. Parent integration on published base
`66c7cd500d7ec353c68da97f44b99d6201af9abe` plus the working changes described
here. This is source and disposable integration evidence, not packaged-app
acceptance or a receipt for external effects.

## Change and boundary

The existing `ChatTurnManager` now places the accepted request UUID in its
task-local `TurnAudit`. `BrainState.send_to_session` attaches
`payload.chat_turn = {contract_version: 1, request_id, turn_id}` only when that
exact session has a live trusted audit. Skill/permission `payload.request_id`
remains unchanged. The source message is copied; caller-provided nested turn
identity is replaced by the trusted audit for an active turn.

This lets a native client distinguish provider/tool progress from the exact
accepted whole turn. Progress never establishes whole-turn completion. The
existing durable terminal receipt and exact-owner cancellation contracts remain
authoritative. The sender still uses the existing session/channel routing;
this change does not add cross-user authorization or multicast delivery.

Unrelated sessions, unaudited legacy work and closed turns receive no added
identity. Concurrent task-local audits cannot tag another session's output.
The native adoption is being verified separately; this hook alone does not
certify that app behavior.

## Actual verification

Two new tests use the actual manager, disposable SQLite `MemoryStore` and actual
`BrainState.send_to_session` method with recording transport sinks. They verify
trusted identity, retained permission identity, immutable input, concurrent A/B
isolation, unrelated-session output and output after the audit closes.

Parent frozen combined run: **165 passed, 8 warnings in 5.80 seconds**. It includes
the checkpoint storage, memory/pool/snapshot, receipt/abort/failure-wire and
delivery suites. Warnings include existing model/FastAPI deprecations, one
older memory test's event-loop-closed worker warning, and restored receipt-limit
environment leakage. The result is not warning-free. The earlier command used
two nonexistent delivery filenames and collected no tests; the corrected command
below was discovered from the actual repository and completed.

```sh
cd feral-core
env FERAL_HOME=/private/tmp/feral-data01-integrated-home \
  FERAL_DATA_HOME=/private/tmp/feral-data01-integrated-home \
  ../.venv/bin/python -m pytest tests/test_runtime_session_checkpoints.py \
  tests/test_chat_turn_progress_identity.py tests/test_memory.py \
  tests/test_memory_pool_release.py tests/test_session_snapshot_trailing_edge.py \
  tests/test_chat_turn_receipts.py tests/test_chat_turn_abort.py \
  tests/test_chat_turn_failure_wire.py tests/test_delivery_is_not_silent.py \
  tests/test_channel_send_reports_delivery.py \
  -q --no-cov -p no:randomly --timeout=60
```

Focused `agents/chat_turns.py` mypy and edited-file Ruff passed before this
combined run. Full repository CI and type ratchet belong to the resulting
published commit; neither is inferred from this local result. Native fixtures,
actual rebuilt app acceptance, durable runtime writer restoration and effects
remain their own gates.
