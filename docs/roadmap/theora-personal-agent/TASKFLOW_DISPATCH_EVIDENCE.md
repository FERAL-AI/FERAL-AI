# CORE-03A: workflow dispatch and truthful pending reviews

Date: 2026-10-02. Base at final verification: `3ecb95bbea0f3bea9b67b331341928e673a6acbe`, plus this card's working-tree changes and concurrent owned integration changes. Publication is the parent's responsibility. This evidence does not declare release readiness.

## Source findings and implementation

The previous `skill.invoke` called an implementation directly after checking only explicit policy deny. Resolver errors were swallowed. That omitted the existing dispatcher, approval handling, session attribution, plan mode, dispatch validation and lease checkpoints. `SkillRegistry.get_skill()` returns an executable implementation; manifests actually live in `.skills`. No alternate authority store or dispatcher was added.

- `agents/taskflow.py` now requires the existing orchestrator ToolRunner. Missing authority fails closed. Policy errors never fall back to direct execution. Safe built-in steps retain their existing behavior.
- Skill calls bind the owning conversation, `taskflow` surface and stable `taskflow:<flow_id>:<step_id>` call ID. An unbound flow receives its own dedicated taskflow session.
- ToolRunner's existing synchronous policy preflight creates the existing pending review. TaskFlow persists exact request ID, session, tool and argument terms in its existing step result, and attaches flow/step identity to that existing review **before** publishing its notification. A user may approve during that awaited notification: the durable step already exists and gates execution.
- `agents/orchestrator.py` claims the exact waiting step before consuming approval, checks pause/cancellation again after UI notification, awaits the actual central dispatcher result, then persists it before sending completion UI. The callback advances a successful step once and never dispatches work itself. A late notification failure cannot erase an already saved completed result.
- `agents/tool_runner.py` can mint an internal one-call approval receipt through `approve_pending(exact_once=True)`. It binds issuer, session, tool and deep-copied arguments, consumes once, and travels as a Python keyword, never a model/JSON argument. Workflow approval does not create a standing tool/session grant. Current deny, plan-mode and native lease checks remain active. Existing central dispatch checkpoints also reject a workflow receipt when its flow has stopped or the supervisor has paused.
- `api/state.py` supplies TaskFlow the existing supervisor instance. There is no new pause system.
- Cancellation retains the cancelled flow, cancels owned work, prevents subsequent steps and removes pending reviews. A potentially committed action is retained as `outcome_unknown`, not replayed. A declared read-only result already returned successfully remains a completed step while its flow stays cancelled and does not advance.
- Restart only resets declared safe reads and safe built-in steps. Running writes, model calls and other potential effects become `outcome_unknown` with no timer. Pending review terms survive in SQLite; losing the in-memory approval manager never recreates a review or dispatches the action automatically. Manual resume cannot resolve either a lost review or an uncertain effect. Terminal resume no longer re-enters the non-reentrant SQLite lock.

The observed native chat defect was separate: an action was correctly pending while the model's next round claimed it had written the file. Both normal and streaming tool loops now stop after a batch containing pending reviews and return deterministic approval-status prose. Mixed batches refer only to gated actions. The wording uses “was sent for your approval” and “current status” because immediate approval can complete concurrently before the original pending result returns. Ordered tool results, history and earlier successful calls remain intact.

## What was tested

Real local SQLite, actual ToolRunner policy/validation/context methods and actual orchestrator approval-resolution methods run against disposable profiles. The executor and provider/UI notifications are controlled fixtures. No user account, purchase, message, browser checkout, hardware command or real external engine was used.

The focused cases cover safe execution and validation, denied/paused/planned dispatch, missing dispatcher, resolver failure, wrong session, changed review terms, expiration, explicit denial, one-call consumption, forged JSON receipts, changed tool/args, changed subsequent workflows, revoked native lease, immediate approval inside notifier, pause/cancel after claim, pause at the central post-validation checkpoint, cancellation during resolution notification, cancellation during dispatch, late UI failure, terminal resume, safe-read versus effectful recovery, and reopening the SQLite database with lost pending or claimed reviews.

The normal and streaming loops run with a fixture provider that would falsely say `I have written ACCEPTANCE_ONLY` on its second round. Pending, mixed-success and multiple-pending batches stop after one provider round and preserve the exact ordered tool history. This is a regression against the observed final-response path, not a claim that all model prose is constrained: streaming preambles emitted before tool dispatch remain model-generated.

## Reproduction

Run from `feral-core` with the pinned interpreter. Isolation must be installed before pytest collection because API imports construct local state.

```sh
../.venv/bin/python -c 'import os, tempfile, pytest; profile = tempfile.TemporaryDirectory(prefix="feral-taskflow-final-"); os.environ["FERAL_HOME"] = profile.name; os.environ["FERAL_DATA_HOME"] = profile.name; code = pytest.main(["tests/test_tool_runner_exact_approval.py", "tests/test_pending_approval_response.py", "tests/test_taskflow_dispatch_policy.py", "tests/test_taskflow.py", "tests/test_taskflow_compose.py", "tests/test_routine_executor.py", "tests/test_permissions_routines.py", "tests/test_feral_workflows_skill.py", "tests/test_background_task_skill.py", "tests/test_approvals_api.py", "tests/test_a9_execution_approvals.py", "tests/test_approval_reaches_the_wearer.py", "tests/test_background_task_references.py", "tests/test_agent_turn_lease.py", "tests/test_plan_mode.py", "tests/test_tool_runner_call_context.py", "tests/test_pending_approval_is_not_failure.py", "tests/test_stream_nonstream_parity.py", "tests/test_turn_attribution.py", "tests/test_stream_usage_billing.py", "tests/test_chat_turn_failure_wire.py", "tests/test_orchestrator.py", "-q", "--no-cov", "-p", "no:randomly"]); profile.cleanup(); raise SystemExit(code)'

../.venv/bin/python -m ruff check agents/taskflow.py agents/orchestrator.py agents/tool_runner.py api/state.py tests/test_taskflow.py tests/test_taskflow_dispatch_policy.py tests/test_tool_runner_exact_approval.py tests/test_pending_approval_response.py --select E,F,W --ignore E501,E402,F401,W291,W293
```

Final result: **299 passed, 7 warnings in 17.14 seconds**, exit 0. Warnings concern existing Pydantic field naming, FastAPI lifespan deprecations, Starlette httpx deprecation and the test environment guard restoring configuration exports. Targeted Ruff passed. `git diff --check` passed for owned tracked source; new test/documentation whitespace was checked separately.

During development, an unisolated collection attempt failed with SQLite unable to open database; the isolated harness succeeded. Early fixtures assumed `get_skill()` returned a manifest and used nonexistent endpoint IDs, and a third-party safe declaration was correctly clamped by real policy. Fixtures were corrected to real registered first-party endpoint IDs without weakening production policy. The first pending-response stream test exposed use of buffered-only attribution variables; corrected streaming behavior passed its actual turn-loop tests.

## Remaining boundaries

This is at-most-once dispatch for the reviewed workflow step in the tested process, plus conservative crash recovery. It cannot guarantee exactly-once external effects if an account service commits and the process dies before storing a receipt. `tool_outcome_verified` is preserved only when the actual executor supplied it. Conservative unknown results may include some errors that occurred before any effect.

There is intentionally no new reconciliation API or approval database. Reopening a lost pending review is safe but blocked; a future recovery card must reconcile actual external state, expose expired/lost review status and allow an explicitly reviewed new attempt. Existing generic non-workflow session grants, earned autonomy and operator-selected loose mode retain their semantics. This internal receipt is not a multi-user authentication boundary against arbitrary trusted in-process plugins. The intentional call-context kill switch remains effective.

Pause/cancellation checks prevent work at existing dispatch checkpoints. They cannot retract an already committed remote effect or stop a non-cooperative executor after it starts. Tool-specific receipts and cancellable execution contracts are still required for payments, messages and account actions. HTTP GET is deliberately excluded from automatic restart replay: the method alone cannot prove absent server-side effects. Existing non-skill built-in policy is outside this bounded dispatcher card. Real native acceptance and external-engine acceptance are tracked separately by their owning workers.
