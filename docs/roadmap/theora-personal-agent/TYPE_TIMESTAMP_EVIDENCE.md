# TYPE01H: strict app-confirmation timestamps

Date: 2026-10-02. Starting checkout observed as
`dd69c7bf5175517966a363591fa8137782358535`; this card changes only
`feral-core/agents/ui_handlers.py`, `feral-core/tests/test_app_action_dispatch.py`
and this evidence file. Parent owns Git publication and the shared checkpoint.
The staged/native 9.30 artifact was not changed or tested by this worker.

## Actual source contract and change

The version-one app confirmation path consumes only an exact owner's pending
request before any await. It revalidates the recorded action/manifest before
dispatch, emits an explicit decision, and never treats dispatch acceptance as
verified tool success. Foreign responses cannot consume the request; replay
after owner consumption cannot execute again.

The timestamp contract already required exact built-in int/float values,
finite values, creation at or before current time, a positive lifetime no
longer than 300 seconds, and expiry strictly later than current time. However,
`pending.get` values were inferred as Any or None, and exact `type(...)`
membership alone did not narrow them for the checker.

`agents/ui_handlers.py:72` now rejects values using explicit isinstance
narrowing **and** preserves the exact built-in type membership check. Bool and
numeric subclasses remain invalid; strings, missing values and malformed
values are not coerced. The existing finite/time/lifetime/expiry logic follows
only after narrowing.

The pinned interpreter also confirmed `math.isfinite(10 ** 1000)` raises
OverflowError while converting a built-in int to float. That specific exception
now returns an ordinary error decision rather than escaping the handler after
consuming consent. There is no broad catch, hidden retry or restored pending
request. Oversized values dispatch no app command or tool action.

## Verification performed

Tests use real disposable AppRegistry/SQLite state and controlled orchestrator
effects. The additional cases test both timestamp fields with strings, bools,
int/float subclasses and oversized built-in integers. A fixed time-zero window
means bools/subclasses would otherwise represent a valid live window, making
their strict type rejection observable. Each malformed case preserves a
foreign owner's pending request, then consumes the legitimate response once,
returns an error and performs zero effects on replay.

Boundary tests preserve the accepted exact 300-second mixed int/float window,
reject future creation, excessive/empty/reversed lifetime and a nonfinite
difference between individually finite floats, and treat equality with expiry
as expired. Existing malformed, expired, rejected, foreign-owner, replay,
manifest/action drift, delivery-loss and managed-context tests remain intact.

Commands from `feral-core`:

```bash
env FERAL_HOME=/private/tmp/feral-type-timestamp-home FERAL_DATA_HOME=/private/tmp/feral-type-timestamp-home FERAL_AUDIT_LOG_PATH=/private/tmp/feral-type-timestamp-home/audit.log ../.venv/bin/python -m pytest tests/test_app_action_dispatch.py tests/test_apps_e2e.py tests/test_runtime_context_activation.py -q --tb=short --no-cov -p no:randomly --timeout=60
../.venv/bin/python -m mypy -m agents.ui_handlers --follow-imports=silent --no-incremental
../.venv/bin/python -m ruff check agents/ui_handlers.py tests/test_app_action_dispatch.py --select=E,F,W --ignore=E501,E402,F401,W291,W293
```

Results:

- **107 passed, 5 warnings in 3.71s** across the three suites.
- Focused mypy before: six errors, including five timestamp diagnostics.
- Focused mypy after: one unchanged older diagnostic at current line 585,
  `session_id: str = None` in the separate daemon helper. The five timestamp
  diagnostics are removed; the module is not claimed completely type-clean.
- Ruff and scoped `git diff --check` passed.

Private logs: `/private/tmp/feral-type-timestamp-tests.log`,
`/private/tmp/feral-type-timestamp-before.log` and
`/private/tmp/feral-type-timestamp-after.log`.

## Limits and remaining gates

No new Any annotations, casts, ignores or baseline changes were introduced.
The optional daemon SID annotation is separate older debt, outside this card.
The focused checker suppresses followed-import diagnostics; no new global
diagnostic count or Ubuntu ratchet result is claimed. These checks follow the
previous frozen full-suite run and do not substitute for acceptance of the
new publication. No real account, external effect, personal profile, native
build, staging or GUI operation was performed.
