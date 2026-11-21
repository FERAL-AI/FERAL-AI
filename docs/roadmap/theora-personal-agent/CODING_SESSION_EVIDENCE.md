# Coding session continuity evidence

October 2, 2026. CODE-01A implements isolation in the existing external-agent
skill. Parent integration owns REST/native/web identity propagation, publication
and the final source checkpoint. This evidence concerns the working-tree change
over `6b368ccf7`, not a freshly rebuilt native app or a published binary.

## Implemented behavior

- `ExternalAgentSkill` reads `skills.call_context.current_context().session_id`,
  which the existing ToolRunner binds around skill dispatch. Model-supplied
  `conversation_id` must match that identity: conflict returns 409; an ID supplied
  without bound context returns 400. An argument cannot manufacture ownership.
- Automatic continuity matches exact caller, agent and workspace. Two concurrent
  or interleaved chats in the same workspace use distinct coding handles; a
  no-handle follow-up reuses only its own live or persisted session.
- Run, permission response and close check both live and indexed ownership before
  prompting, answering, cancelling, forgetting, recording or spawning. Foreign
  live, dead and persisted handles return 403. An old unscoped handle is never
  silently assigned to a scoped chat.
- Unbound direct local callers retain legacy continuity among unscoped records
  only. They cannot pick scoped records through the index's wildcard fallback.
  Existing explicit-close and fresh-session behavior remains covered.
- Live/resumable session lists expose handles in the caller's exact scope.
  Deliberately shared long-lived activity episodes remain shared within the
  existing single-user memory store. This is conversation continuity isolation,
  not a new cross-user or hosted authentication boundary.
- Agent death and persisted-pointer recovery retain the same scoped handle.
  Supported ACP resume and unsupported-resume cold restart are reported honestly;
  the latter reuses the existing episodic summary briefing.
- Owned fixtures now await `MemoryStore.aclose()` and registry shutdown before
  their event loop exits. Synchronous `close()` did not drain pooled aiosqlite
  workers and caused teardown warnings in the initial run.

No index schema or separate memory database was added. No existing private
profile, account login, approval policy or personal coding session was migrated.

## Actually tested

Pinned Python under the repository `.venv`, macOS, disposable runtime home,
disposable continuity indexes, SQLite stores, workspaces and ACP subprocesses.
The subprocesses implement deterministic fixture agents, **not real OpenCode,
Codex or Claude inference**. Their real pipes, permissions and process death
exercise the existing ACP implementation rather than mocking registry behavior.

From `feral-core`, the final command was:

```sh
../.venv/bin/python -c 'import os, tempfile, pytest; profile = tempfile.TemporaryDirectory(prefix="feral-coding-isolation-"); os.environ["FERAL_HOME"] = profile.name; os.environ["FERAL_DATA_HOME"] = profile.name; code = pytest.main(["tests/test_external_agent_skill.py", "tests/test_external_agent_memory.py", "tests/test_call_context.py", "tests/test_tool_runner_call_context.py", "-q", "--no-cov", "-p", "no:randomly"]); profile.cleanup(); raise SystemExit(code)'
```

**135 passed, 6 warnings, 15.28 seconds, exit 0.** Tests cover concurrent and
interleaved A/B calls; conflicting and matching supplied identity; foreign
live/dead/persisted run and close; foreign permission response; owner death and
restart recovery; resume versus cold briefing; shared activity with scoped handle
lists; legacy non-adoption; and context disabled behavior. Existing call-context,
ToolRunner, digest, permission, cancellation, empty-turn and continuity tests
also passed.

Remaining warnings are the existing Pydantic schema-name warning, FastAPI
startup/shutdown deprecations and the environment guard's fixture-variable
warning. Variables were restored by the guard. No aiosqlite worker-after-loop
warnings occurred in this final run. The initial 134-test run passed with those
teardown warnings; it predates fixture shutdown fixes and the concurrent case.

```sh
../.venv/bin/python -m ruff check --select=E,F,W --ignore=E501,E402,F401,W291,W293 skills/impl/external_agent.py tests/test_external_agent_skill.py tests/test_external_agent_memory.py
```

**Passed, exit 0.** Diff whitespace check also passed.

## Remaining integration and acceptance

- Parent integrated optional `conversation_id` on local Coding REST task,
  permission/cancel bodies and overview/poll/activity queries. Both live and
  indexed handles must match before read, approval, cancellation or reattachment.
  Scoped requests refuse when tool-call context is disabled. Invalid identity
  values are rejected before dispatch. The authenticated local client supplies
  this identity; it is not a model argument or multi-owner authentication scheme.
- Dedicated native/web Coding workspaces remain explicitly unbound and usable;
  they do not inherit whichever ordinary chat happens to be active. Chat tool
  dispatch already binds its owning conversation through ToolRunner. A future
  client deliberately using scoped REST must supply the same identity on every
  follow-up, review, poll and cancellation.
- Parent combined command (with explicit disposable process home/data):
  `../.venv/bin/python -m pytest tests/test_external_agent_skill.py tests/test_external_agent_memory.py tests/test_call_context.py tests/test_tool_runner_call_context.py tests/test_coding_api.py -q --no-cov -p no:randomly`
  **162 passed, 6 existing warnings, 31.40 seconds**. Real ACP pipes cover
  interleaved A/B plus legacy REST sessions, wrong-scope actions, persisted
  reattachment/forgetting refusals, disabled-context and malformed scopes.
- Earlier parent runs failed: the first omitted process-home isolation and hit
  a collection-time database-open error; the next found five missing-fixture
  setup errors when this API test followed the collected skill test. Explicit
  disposable storage and importing the fixture functions by name fixed those
  respective verification issues; no assertion was relaxed. The successful
  result above includes those formerly failing cases.
- `FERAL_TOOL_CALL_CONTEXT=off` intentionally makes `current_context()` unbound.
  Tests confirm it does not permit a scoped handle; supplied scoped identity is
  then refused and no-handle calls use legacy unscoped continuity. The toggle and
  context module are not an unbypassable authorization boundary. Existing local
  operator authentication, ToolRunner approvals and environment jailing remain
  necessary; multi-owner hosted use remains unsupported.
- Matching no-handle continuity does not infer that an interrupted action is safe
  to replay. A still-running turn returns conflict. ACP resume acceptance is not
  proof that the external engine restored its history or completed a tool effect.
- Real installed engine edit/test/deny/cancel, native multi-chat journeys and
  restart acceptance on the newly assembled candidate remain separate gates.
- This scoped skill/API change does not alter the REST coding workspace's global
  provider/workspace administration, persistent approval-grant scopes or contracts of
  standalone CLI observation. Those require their own reviewed cards.

See [execution plan](EXECUTION_PLAN.md) CODE-01 and
[work checkpoint](WORK_STATE.md) for integration ownership and publication state.
