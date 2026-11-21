# Python SDK HTTP contract evidence

DEV-01A, October 2, 2026. Worker-owned implementation in
`sdk/python/feral_sdk/client.py`, its README and
`sdk/python/tests/test_client_http.py`. Base source before this slice:
`6b368ccf79c8b581bf2f705ed23e97095899f49d`; the results below apply to these
working-tree changes. Parent integration/publication supplies the final commit
and remote CI identity in [WORK_STATE.md](WORK_STATE.md).

## Behavior repaired

- Health and skill enumeration use the registered `/health` and `/skills`
  routes with JSON Accept negotiation. The prior `/api/health` and `/api/skills`
  requests did not address the registered handlers.
- Skill invocation uses POST `/api/tools/execute`, carrying `skill_id`,
  `endpoint`, `args`, an explicit Boolean `confirm` (default false) and optional
  supplied `session_id`. It preserves the existing REST policy/context path.
- `create_note()` uses the existing `notes_memory/save_note` skill through that
  same path; `/api/notes` was not registered. It returns the tool envelope;
  successful note data is in `data`.
- Caller-supplied `bearer_token` is available for HTTP; no discovery, persistence,
  permission expansion or credential logging was added. Invalid empty/multiline
  credentials are rejected before creating a connection.
- Every HTTP convenience method raises status errors before parsing JSON.
  Timeout/connection/cancellation errors propagate without automatic replay;
  redirects are not followed. Invalid JSON and wrong object/list shapes raise
  bounded `ValueError` diagnostics instead of reporting empty/success data.
- An HTTP-200 tool failure remains its application envelope, distinct from an
  HTTP failure. Callers must inspect `success`, `status_code`, `error` and any
  policy metadata. The client never turns review-required into approval.

## Source confirmed

The tests inspect the actual `api.server.app` registration and exercise the real
`health_page_or_json`, `skills_page_or_json`, tools router, memory search router,
conversation listing router and APIKeyMiddleware in an in-process ASGI app.
The server startup lifespan is not run. `FERAL_HOME`, `FERAL_DATA_HOME` and the
test credential are disposable. No account, external website or personal store
is accessed.

The recording executor proves the SDK reaches the registered tool surface and
that the real route binds the supplied session with surface `http_api`.
Real safety resolution refuses CONFIRM-tier calls with application 412 until
the test explicitly supplies true, refuses DENY-tier calls even with true, and
returns application 404 for an unknown skill. These refusal cases do not dispatch.
The recording executor is a fixture; these tests do not certify real arbitrary
skill execution or an actual note persisted to disk.

## Checks actually run

From the ASOS root:

```sh
.venv/bin/python -m pytest sdk/python/tests -q --no-cov -p no:randomly
.venv/bin/python -m ruff check --select=E,F,W --ignore=E501,E402,F401,W291,W293 sdk/python/feral_sdk/client.py sdk/python/tests
git diff --check -- sdk/python/feral_sdk/client.py sdk/python/README.md
```

Results: **25 tests passed**, targeted Ruff passed, tracked-diff whitespace passed.
Five warnings come from existing imported server models/lifespan decorators.
Cases include registered successful reads/invocation, actual remote valid/missing/
wrong bearer behavior, unknown/deny/review-required application results, exact
request shape/session binding, HTTP status failures for every convenience method,
HTML/null/array/string malformed responses, malformed record lists, timeout and
connection failure propagation, explicit approval/argument validation, redirect
refusal, cancellation without replay and debug-level credential-log absence.

An earlier 23-case run passed. Expanding the registered-route checks initially
produced one fixture failure: the disposable conversation-page stub omitted
`limit` and `offset`. Those fields were restored to match the actual route/store
contract; the corrected 25-case run passed. No server production change was made
to accommodate the stub.

## Remaining acceptance

- WebSocket chat authentication, greeting/session negotiation and stream parsing
  are a separate follow-on slice and remain unverified. `bearer_token` in this
  change applies HTTP only. Node/plugin/device SDKs are separate interfaces.
- The generic SDK suite is not automatically covered by existing node SDK CI.
  The parent must add an explicit generic SDK CI step/job before claiming remote
  coverage for this slice.
- A full started Brain, real socket/TLS/proxy deployment, installed SDK wheel,
  real credential scopes, real tool execution and clean-clone developer example
  remain acceptance gates. In-process contracts and injected transport errors
  are not those external integration results.
- `search_memory()` retains its existing results-list API; full tier-degradation
  metadata is available only from the raw search envelope and is documented.

See [SDK usage and failure semantics](../../../sdk/python/README.md) and the
[execution plan](EXECUTION_PLAN.md).
