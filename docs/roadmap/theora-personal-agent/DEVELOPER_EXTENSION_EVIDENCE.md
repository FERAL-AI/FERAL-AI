# DEV01D: executable local extension and exact executor review

Date: 2026-10-02. Based on checkpoint
`634595c7194aedaf6eb9b8c91cbacb87fedea4bd`; implementation and worker verification,
pending parent integration. No Git publication or app assembly by this worker.

## Delivered extension walkthrough

[examples/developer-runtime/README.md](../../../examples/developer-runtime/README.md)
documents an authored, read-only integer-calculation package and executable
fresh-process walkthrough. It uses the existing `SkillManifest`, `SkillPackage`,
`SkillValidator`, `install_package`, `SkillRegistry`, registered skills reload
route, `ToolRunner`, `SkillExecutor`, approval manager and marketplace removal.
There is no new dispatcher, SDK transport, registry or approval store.

The walkthrough sets `FERAL_HOME` and `FERAL_DATA_HOME` to temporary directories
before runtime imports. No running service, lifespan, account, model, device,
personal profile or external task is used. The route is mounted on an isolated
ASGI app. It is actual-source local integration with a minimal embedding adapter,
not a full running-server SDK journey, clean-clone installation or remote-auth
acceptance. The existing pinned environment was used; dependencies were not fetched.

Verified conditions:

- Package schema/static validation succeeds. Explicit disposable installation and
  the actual reload route acknowledge registration; a loaded implementation is
  checked separately from that acknowledgement.
- Pending review preserves caller session, tool and exact arguments. Foreign
  sessions cannot approve or deny that review. Denial produces zero calculation
  executions and the denied request cannot later be approved.
- A fresh exact one-call review produces `13 + 29 = 42`, with the bound caller
  session in its result, exactly once. Reusing the internal receipt fails. There
  is no standing approval.
- Reviewed requests with missing required arguments or out-of-bounds values fail
  before a successful calculation. The implementation does not evaluate expressions
  or execute user-supplied code.
- Actual marketplace removal deletes the installed directory and live manifest.
  A previously reviewed queued invocation cannot execute afterward. The legacy
  implementation cache can retain the object; that object is not central dispatch
  authority, and the example checks actual registration/dispatch refusal.

## Real defect discovered and repaired

The first actual-executor run failed: ToolRunner consumed the exact one-call review,
then SkillExecutor's defence-in-depth gate requested another approval instead of
executing. Stub executors in earlier exact-review fixtures did not expose this.
The new actual-executor matrix also reproduced a second failure: the executor
caught boundary lease revocation and continued, allowing its controlled calculation
implementation to run despite revocation. No external effect occurred in this test.

The parent explicitly granted the narrow repair in
`feral-core/agents/tool_runner.py` and `feral-core/skills/executor.py`.

- ToolRunner issues a private, one-use executor admission immediately around its
  validated executor dispatch. It binds the actual issuing runner, exact review,
  caller session, tool, arguments, trusted surface and dispatching asyncio task.
  It is not a JSON field or standing grant. The outer exact review remains consumed.
- The actual executor uses the declared `enforce_executor_safety` hook when present,
  with the older embedding gate as fallback. It does not mistake a dynamic mock's
  phantom attribute for a new declared hook.
- Matching admission is consumed once at executor entry. The existing safety
  resolver still runs, including current deny and dispatch-lease checks. Plan mode
  remains first. Policy rechecks use the bound caller surface. A failed gate now
  propagates instead of continuing into execution, including lease revocation.
- Context inheritance does not grant admission to a child asyncio task. Reentry,
  concurrent receipt reuse, changed session/surface/action, and JSON review flags
  cannot use it. No safety-used flag is reset, and no loose-mode workaround grants
  execution. Changed/default-filled arguments that differ from reviewed terms
  remain a refusal rather than silently widening approval.

## Commands and results

From repository root:

```sh
.venv/bin/python examples/developer-runtime/walkthrough.py
.venv/bin/python -m pytest examples/developer-runtime/tests -p no:randomly -q --no-cov
.venv/bin/ruff check --select E,F,W --ignore E501,E402,F401,W291,W293 feral-core/agents/tool_runner.py feral-core/skills/executor.py feral-core/tests/test_tool_runner_executor_admission.py examples/developer-runtime
```

Walkthrough exit **0**, one verified calculation, denial count **0**, all required
review/validation/removal conditions true. Fresh-process example test: **1 passed**.
Ruff exit **0**.

From `feral-core`:

```sh
../.venv/bin/python -m pytest tests/test_tool_runner_executor_admission.py tests/test_tool_runner_exact_approval.py tests/test_tool_runner.py tests/test_tool_runner_call_context.py tests/test_agent_turn_lease.py tests/test_taskflow_dispatch_policy.py tests/test_p0_security_clamps_and_gates.py tests/security/test_safety_resolver.py tests/test_executor_tool_timeout.py tests/test_manifest_param_defaults.py tests/test_checkpoints_actions.py tests/test_a9_execution_approvals.py tests/test_tool_runner_schema.py tests/test_tool_runner_validator_invalidation.py -p no:randomly -q --no-cov
```

Final result: **341 passed**, 6 warnings, 5.21 seconds. Existing test assertions were
unchanged. The warnings include existing Pydantic/FastAPI warnings and the test
environment leak guard, which restored modified environment values. The corrected
initial actual-executor regression run, before the production repair, had **4 failed,
6 passed**; its failures include the double gate and boundary lease revocation.
An earlier fixture setup used a non-existent `BrainState.tool_runner` attribute;
it was corrected to the existing `state.orchestrator.tool_runner` lookup before
that baseline comparison. No baseline failure is presented as a passing check.

Logs:

- `/private/tmp/feral-developer-walkthrough-20261002.log`
- `/private/tmp/feral-developer-example-tests-20261002.log`
- `/private/tmp/feral-executor-admission-before-20261002.log`
- `/private/tmp/feral-executor-admission-regression-20261002.log`

## Separate public SDK authoring blockers

Actual disposable probe:

```sh
.venv/bin/python /private/tmp/feral-sdk-plugin-authoring-probe.py
```

Exit **0** verifies the observed gap, not successful plugin installation. Using the
actual Python SDK `FeralPlugin` and `feral_tool`, the generated manifest passes
`SkillPackage` schema loading but `SkillValidator` reports its `internal://` URL.
Placing the authored `FeralPlugin` subclass in `impl.py` yields a successful
`reload_skill_detail` acknowledgement and registered manifest, while `get_skill`
and `get_implementation` both return `None`. The loader discovers `BaseSkill`
subclasses; a bare SDK `FeralPlugin` is not one. Its handler was never executed.
Log: `/private/tmp/feral-sdk-plugin-authoring-probe-20261002.log`.

The current Python SDK README's bare-module installation instructions therefore
need a separate adapter/registration and validation contract repair. Explicit
`register_instance` is a different embedding path; it was not substituted to claim
that bare-module installation worked. No public SDK file was edited here.

Node SDK `definePlugin` still emits `POST plugin://...` by source inspection. No
such handler transport was found in the current executor's supported dispatch
lanes. A Node execution probe was not run, and this example does not certify that
path. It uses the verified Python `BaseSkill` package with an empty endpoint URL.

## Limits and integration gate

Static package validation is not a security sandbox. Installed Python modules
run in-process and import source during reload; users must review/trust authored
packages. This example is not proof that malicious same-process extension code
is contained. The low-level installer can replace existing packages; the example
requires a fresh disposable target rather than exercising replacement.

Existing offline executor behavior when no runtime runner exists is preserved.
The example wires the actual runner and tests its policy path; it does not assert
that arbitrary direct Python callers or absent-runner embeddings are authorized.
No publisher signatures, app UI bundles, update consent, remote authentication,
process sandbox, third-party accounts, installed app, Linux clean-machine or iOS
journey is certified. Parent integration must freeze and run combined runtime/SDK
checks before publication. Forge atomic creation files remain frozen and unchanged.
