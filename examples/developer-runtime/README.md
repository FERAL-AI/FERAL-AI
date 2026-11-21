# Build a local FERAL extension

This example adds two bounded integers using FERAL's existing skill package,
manifest, registry and policy dispatcher. It uses authored Python, no provider,
account, network backend, device or generated source. The walkthrough installs
into a new temporary profile, exercises the existing reload route and dispatcher,
then uninstalls. It never installs into your running app.

The walkthrough exposed and now regression-tests a repaired exact-one-call approval
handoff between ToolRunner and SkillExecutor. The executor independently rechecks
current policy using a trusted, task-bound, single-use admission. A foreign task,
changed action, reused receipt, failed policy gate or revoked lease cannot use that
review. See the dated developer evidence for precise verification and limits.

## Prerequisites and executable verification

Use a checkout with the repository's pinned `.venv` already prepared by `make dev`.
The existing runtime supports Python 3.11+; this walkthrough uses the dependencies
installed for `feral-core` (including FastAPI, httpx and Pydantic). It starts no
runtime lifespan, model, scheduler, HTTP listener or GUI. Run without Python `-O`.

From the `ASOS` repository root:

```sh
.venv/bin/python examples/developer-runtime/walkthrough.py
.venv/bin/python -m pytest examples/developer-runtime/tests -q --no-cov -p no:randomly
```

`FERAL_HOME` and `FERAL_DATA_HOME` are set to newly created disposable directories
before runtime imports. Strict autonomy and bound tool-call identity are selected
for this example. Existing profiles and their settings are not loaded. On success,
the script prints a JSON verification summary; every required condition is asserted
and a failed condition exits nonzero. The temporary profile is removed on exit.

This is a fresh-process local embedding and ASGI route integration using your
checkout and installed dependencies. It is not clean-clone installation acceptance,
a running-server SDK journey, remote authentication testing or app-store publishing.

## Package files and contracts

- [`read_only_math/manifest.json`](read_only_math/manifest.json) declares the stable
  skill ID `developer_math`, endpoint `calculate`, integer arguments and no external
  permissions. Its `PYTHON` method and empty URL select the existing implementation
  path. No `plugin://` or `internal://` transport is invented.
- [`read_only_math/impl.py`](read_only_math/impl.py) implements `BaseSkill.execute`.
  It accepts only two integers between -1000000 and 1000000 and returns the standard
  `{success, status_code, data, error}` envelope. It does not evaluate expressions.
  The bound caller session is read from `skills.call_context`, not from user arguments.
- [`walkthrough.py`](walkthrough.py) uses `SkillPackage` and `SkillValidator`, then
  `install_package` with an explicit disposable destination. It registers through
  the actual `POST /api/skills/reload` route and verifies the dynamic implementation.
  Its embedding adapter provides real registry, executor, ToolRunner, ApprovalManager
  and TrustLedger objects; the model and notification UI are not required.

Package validation checks declared structure and selected static patterns. It is
not a sandbox or a proof that arbitrary extension code is safe. A Python package's
module is imported during registration and its implementation runs in-process.
Review the source before installing a package. The low-level installer can replace
an existing directory; this example deliberately requires a new disposable target.

## Review, invocation and removal

The tool name is `developer_math__calculate`. An example request is:

```json
{"name":"developer_math__calculate","args":{"a":13,"b":29},"id":"a-fresh-call-id"}
```

Call `ToolRunner.execute_tool_call_for_llm` with the trusted caller session and the
registry's current tools. It binds identity, applies policy and validates parameters;
do not call `impl.execute` directly to bypass those layers. A third-party declaration
of `read_only_hint` does not grant first-party authority. The example explicitly
requires user review and verifies the pending tool name, arguments and session.

For its controlled local review, the harness first denies a request and verifies
zero implementation calls. A fresh review is accepted using the existing
`approve_pending(..., session_id=..., exact_once=True)` method. The returned internal
one-call receipt is passed to the existing dispatcher. This simulates an operator
review in an embedding; an SDK caller must use its deployment's supported approval
surface, never construct this internal receipt from JSON. Foreign-session review
and receipt reuse must fail. The calculation must return 42 exactly once.

The remaining checks cover missing arguments, bounds, and removal via the existing
`MarketplaceClient.uninstall`. Removal must delete the package and live manifest;
an already reviewed queued request must then fail without executing. The current
implementation cache may retain an object after uninstall, so object absence is
not the removal guarantee: the registry and dispatcher must refuse new execution.

To build a real extension, choose your own stable ID, version and narrow argument
contract; use the supported installation/review surfaces of the running deployment.
External effects require appropriate permissions, account setup, policy checks,
outcome verification and uncertain-outcome reconciliation. This example grants none.
