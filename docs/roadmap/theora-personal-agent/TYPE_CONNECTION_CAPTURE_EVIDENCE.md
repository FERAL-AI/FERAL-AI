# Type card: captured connector tables across awaited teardown

Date: 2026-10-02. Exclusive files are `feral-core/api/routes/mcp.py`,
`feral-core/api/routes/channels.py`,
`feral-core/tests/test_native_integration_lifecycle_routes.py` and this evidence.
Parent owns publication, global type measurement and shared release checkpoints.
No native/staged resource, other core module or previously frozen card changed.

## Source-backed contract

MCP disconnect and channel stop first capture the runtime manager, its current
dictionary and the requested connection/listener. They await teardown of only
that capture. A failed or unverifiable close returns HTTP 502 and an uncertain
runtime outcome, retaining registration. Successful captured teardown followed
by manager/table/entry replacement returns HTTP 409 with a partial captured
outcome; it must not remove or stop the replacement. Only unchanged exact
identities permit runtime registration removal. Saved configuration and
credentials remain outside this operation's scope.

Both routes now shape-check a raw manager table, then bind the narrowed
dictionary to the stable captured local used across await. This retains the
same dictionary object rather than copying it or re-reading a current table
after teardown. The postawait manager, table and entry identity fences are
unchanged, as are validation, owned task/process draining, failure redaction
and honest partial-outcome responses. No permission or remote-close assurance
was added; the route still cannot verify a remote session acknowledged close.

## Additional actual registered-route tests

Eight barrier-controlled ASGI cases pause the captured close, then replace the
manager or its table after the route enters its awaited teardown. They exercise
both MCP and channels, with successful close and explicit close failure.

Tests require the successful old close to produce 409 with the correct captured
partial-outcome flag; failures must produce 502/uncertain without exposing the
private fixture exception. In every case the fresh registered entry remains
present and live, has zero close/stop calls, and MCP replacement configuration
is preserved. The old table retains its captured entry, demonstrating that
neither old nor replacement registration was blindly deleted after a changed
owner. Existing same-table entry replacement, captured-child reaping,
HTTP-close uncertainty, owned-task draining, missing/unsupported manager and
invalid-identifier cases remain intact.

No account, remote connector, listener, service process or real MCP child was
started. The controlled routes use the actual registered production router
functions with disposable profile state. These fixtures do not establish live
connector/account or main-server authentication acceptance.

## Checks run

From `feral-core`:

```bash
env FERAL_HOME=/private/tmp/feral-type-connection-home FERAL_DATA_HOME=/private/tmp/feral-type-connection-home FERAL_AUDIT_LOG_PATH=/private/tmp/feral-type-connection-home/audit.log ../.venv/bin/python -m pytest tests/test_native_integration_lifecycle_routes.py tests/test_mcp_routes.py tests/test_channels.py -q --tb=short --no-cov -p no:randomly --timeout=60
../.venv/bin/python -m mypy -m api.routes.mcp -m api.routes.channels --follow-imports=silent --no-incremental
../.venv/bin/python -m ruff check api/routes/mcp.py api/routes/channels.py tests/test_native_integration_lifecycle_routes.py --select=E,F,W --ignore=E501,E402,F401,W291,W293
```

- **33 passed, 6 warnings in 1.44s** across the three suites.
- Focused mypy: **six before, two after**, under unchanged configuration.
  MCP's two captured-table diagnostics are removed. Channels' two captured-table
  diagnostics are removed. Its older WhatsApp vault Optional and environment
  string Optional errors remain, now at lines 144/149; checker exits one.
- Exact Ruff gate and scoped `git diff --check`: passed.

Private logs: `/private/tmp/feral-type-connection-before.log`,
`/private/tmp/feral-type-connection-after.log` and
`/private/tmp/feral-type-connection-tests.log`.

## Remaining gates

No new Any annotations, casts, ignores, type-baseline changes or broadened
interfaces. The focused checker suppresses followed-import diagnostics; no
global count, Ubuntu ratchet result or full-suite acceptance for the new source
is claimed. The older WhatsApp typing is separate debt and was not changed.
Parent must integrate frozen worker source before global measurement and
publication. Native 9.30 and live connector outcomes remain separate evidence.
