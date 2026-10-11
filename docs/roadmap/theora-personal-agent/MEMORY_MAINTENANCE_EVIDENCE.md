# MEM-01A: asynchronous identity maintenance

October 2, 2026. Actual native9.27 acceptance logged an unawaited
`MemoryStore.episode_recent` coroutine at disconnect. Source inspection found
both branches of `IdentityWorkspace.maintenance_cycle` treating that asynchronous
query as a list. No personal profile or real model was used in the reproduction.

The four new tests initially failed against real disposable SQLite, with the
unawaited-coroutine warning reproduced. The repair awaits both existing queries;
it preserves caller session filtering, existing provider availability checks and
the existing identity memory destination. It does not introduce a new memory
store, change autonomy or certify the quality of inferred facts.

Run from `feral-core`:

```sh
FERAL_HOME=/private/tmp/feral-identity-maintenance-20261002/home FERAL_DATA_HOME=/private/tmp/feral-identity-maintenance-20261002/data ../.venv/bin/python -m pytest tests/test_identity_maintenance_async.py tests/test_identity.py tests/test_episode_recent_filters.py tests/test_learner.py tests/test_memory_recovery.py -q --no-cov -p no:randomly --tb=short
```

**25 passed / five warnings in2.76s**. Tests use actual SQLite episode storage
and an injected model, checking scoped versus explicitly global reads, empty
history, unavailable model and unchanged existing memory on provider failure.
No cloud request, real semantic inference, background scheduling or OS memory
lifecycle was tested. Broader receipt/memory check: **51 passed / seven warnings
in4.41s**, including `test_chat_turn_receipts.py` and `test_chat_turn_abort.py`.

Parent also repaired two new receipt-store type diagnostics measured by Ubuntu
at source4e19f07f8: reject an absent capacity row before accepting a task, and
materialize fetched recovery rows before counting. Focused mypy reports no
diagnostic in these new receipt methods, but **19 diagnostics remain across
store/workspace** in the local check, including a missing local YAML stub.
The full Ubuntu ratchet remains failed at850 versus812; no baseline was changed.

Real memory quality, correction/provenance, concurrent identity-file writes,
durable per-thread model checkpoints, deletion/export/restore and physical
device continuity remain MEM-01/DATA-01 acceptance gates.
