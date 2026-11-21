# Manifest approval precedence

Date: 2026-10-02. Scope: central strict-mode tool authorization. This is
controlled backend evidence, not native, external-service, or release acceptance.

## Confirmed defect

The real safety resolver classified a registered `__read` endpoint with
`requires_user_approval=True` and `read_only_hint=True` as `CONFIRM`. The strict
tool-runner branch nevertheless consulted only its lenient read classification.
The original controlled test dispatched through a real `SkillRegistry`,
`ToolRunner`, and `SkillExecutor`: it returned a successful constant result and
called the local handler once instead of returning `pending_approval`.

The first pre-fix run stopped on that actual assertion failure: one failed test
in 1.25 seconds. No account, network connector, or external effect was involved.

## Narrow change

`agents/tool_runner.py` now requires approval in strict mode when the resolved
policy is `CONFIRM` or the tool is not read-only. Resolved review policy therefore
cannot be overridden by a read hint, a read-like endpoint name, or contradictory
safe-tier metadata. The resolver already gave the correct answer and was unchanged.

Denial still precedes approval handling. Hybrid earned autonomy, explicit loose
mode, standing operator grants, exact owner/action-bound receipts, plan mode,
and executor admission retain their existing policies. This change does not make
all read tools require approval or redefine operator autonomy settings.

## Verification

The dedicated suite uses real registered manifests and production central
dispatch with a deterministic local `BaseSkill`. Cases cover custom and trusted
skill identifiers, explicit approval plus read hints, contradictory safe-tier
metadata, explicit confirm tier, strict/hybrid behavior, trusted read-safe AUTO,
untrusted hints, plan mode, model-supplied approval flags, standing-session scope,
foreign approvals, exact one-use dispatch, reuse rejection, and a policy change
to DENY after review. Successful owner-approved execution returns the actual
constant result; pending/refused calls assert zero handler invocations.

An intermediate test run exposed two incorrect assumptions in the new fixtures:
`get_pending` has no session keyword, and an exact receipt permits one dispatch
attempt, including a mismatched attempt. The tests now inspect the stored owner
and check owner success separately from foreign rejection and consumed-receipt
rejection. Production receipt behavior was preserved.

Final command, from `feral-core` using a disposable runtime home and audit path:

```sh
../.venv/bin/python -m pytest \
  tests/test_manifest_approval_precedence.py \
  tests/security/test_safety_resolver.py \
  tests/test_tool_runner.py \
  tests/test_tool_runner_exact_approval.py \
  tests/test_tool_runner_executor_admission.py \
  tests/test_plan_mode.py \
  tests/test_earned_autonomy.py \
  tests/test_mcp_tools_are_not_auto_approved.py \
  tests/test_p0_security_clamps_and_gates.py \
  -q --tb=short --no-cov -p no:randomly --timeout=60
```

Result: **324 passed, 7 warnings in 3.05 seconds**, exit 0. Existing warning
categories include Pydantic schema naming, FastAPI lifecycle deprecations,
Starlette test-client deprecation, and the suite's environment-restoration report.
The final log is `/private/tmp/feral-manifest-review-regression-final.log`.

Ruff on the changed production file and dedicated test passed with the repository
`E,F,W` selection and existing ignored codes. `git diff --check` passed.

Focused package-named mypy checked `agents.tool_runner` and the new test module
with `--follow-imports=silent --no-incremental`. Both current source and the
committed source supplied via `--shadow-file` report the same two preexisting
optional-provider attribute errors (`chat` and `extract_response`); their line
numbers shift by the added comment. The new tests add no diagnosed type errors.
No baseline, cast, `Any`, or suppression was added. Global type reconciliation
and remote CI belong to the integration checkpoint.

## Remaining acceptance

The earlier whole-backend result predates this patch and must not be presented
as testing it. Combined source checks and exact-source remote CI must validate
the integrated changes before merge or release. Publishing the review branch
starts that remote verification. Packaged native resources were not changed.
