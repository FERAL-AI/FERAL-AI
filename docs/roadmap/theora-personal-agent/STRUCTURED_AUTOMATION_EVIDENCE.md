# Explicit interval scheduling

October 2, 2026. Follow-up to the published
[scheduled automation slice](NATIVE_WORKFLOW_PARITY_EVIDENCE.md), on base
`cd1571ef94aa2fe244023d56ba468f7ba3266700`.

## Behavior

The native form should accept ordinary instructions such as “Review every unread
email” or “Summarize my weekly plan” without letting those words change the
chosen interval. `POST /api/automations` now supports an exact structured body:

```json
{"interval_minutes":60,"action":"Review every unread email","session_id":"selected-thread"}
```

The interval is an actual integer from1 through10080; booleans, floating-point
numbers and numeric strings are refused. Action text is nonempty, at most8000
UTF-8 bytes and free of control characters except newline/tab. Session identity
is exact and nonempty, at most1024 characters, with no surrounding whitespace
or controls. Unknown/mixed fields and incomplete structured requests fail422
before any write; they cannot fall back to legacy schedule parsing.

The existing CronService stores the exact CUSTOM recurring job using the chosen
interval. It retains the existing three-field action payload for dispatcher
compatibility. The composed description/action text is historical natural-language
task data; the interval is never inferred from that text. Acknowledgement adds
`creation_mode: explicit_interval`. Legacy `{text, session_id}` callers retain
their existing parser and response contract. Storage calls run off the async
request thread using the existing thread-safe scheduler; no new scheduler exists.

The native form sends only those three structured fields and requires the
explicit-interval acknowledgement plus the existing exact detail/inventory
readback. It no longer forbids every/daily/weekly in the task. An older runtime
that lacks this body contract cannot silently create a parsed schedule; no
legacy-body fallback is attempted. The form describes the repeated task in
plain language, with the actual background action shown in review.

## Verification and limits

Parent actual registered-router/disposable-SQLite tests plus existing timeline
and scheduler regressions: **61 passed,6 warnings in1.55s**. No server lifespan,
callback, provider, account or task execution ran. Tests prove action words do
not invoke the parser, exact persisted intervals/payloads, inventory/delete,
validation without writes, legacy compatibility, and redacted storage failure
with one attempt/no automatic retry. Edited Python Ruff passed.

Parent frozen combined backend run: **233 passed,7 warnings in7.33s**, including
runtime checkpoint lifecycle/storage, trusted progress, ordinary/stream
orchestration, receipts/cancellation, structured scheduling and existing timeline/
scheduler checks. Warnings are existing deprecations and restored environment
leakage; none were suppressed.

```sh
cd feral-core
env FERAL_HOME=/private/tmp/feral-structured-automation-home \
  FERAL_DATA_HOME=/private/tmp/feral-structured-automation-home \
  ../.venv/bin/python -m pytest tests/test_structured_automation_api.py \
  tests/test_timeline_episode_source.py \
  tests/test_scheduler_unparseable_cron_disables.py \
  -q --no-cov -p no:randomly --timeout=60
```

The later frozen backend integration passed235 tests/7 warnings in9.38s after
the direct-owner progress follow-up. The full native fixture runner passed the
updated **70 Workflow assertions**, all sibling suites and linked25/desktop39/
error5 checks. Actual assembled UI acceptance remains pending. The earlier67
assertions belong to the prior parser-restricted slice. Exact shared commands
are in [integration evidence](NATIVE_CONTEXT_INTEGRATION_EVIDENCE.md).

This adds schedule storage, not an outcome receipt, immediate execution,
backend idempotency or conditional-write CAS. Already running work is not undone
by deletion. Existing policy/dispatch limitations and cross-client preflight
races remain described in the prior evidence and
[automation audit](NATIVE_AUTOMATION_CONTRACT_PLAN.md).
