# FERAL completion checkpoint

Updated October 2, 2026. Parent/integrator owns this file. Reconcile it with actual
Git, CI and processes after resuming; it is a checkpoint, not a live process lock.
See [resume procedure](RESUME_WORK.md), [execution plan](EXECUTION_PLAN.md) and
[all user requirements](REQUEST_COVERAGE.md).

## Objective and standing scope

Finish the existing FERAL system as a dependable native app and open-source
developer platform, preserving its runtime, memory, tools, clients and data.
Then integrate the requested receipts/concurrent sessions, voice multitasking,
CLI oversight, proactive assistance, phone/glasses continuity, messaging,
commerce, collaboration and Linux capabilities through existing contracts.

The user requested autonomous engineering, parallel workers, tests, documentation
and GitHub publication. Continue authorized reversible work without redundant
confirmation. Sandbox/managed requirements still apply. Auto-review must be
selected by the user/runtime; these files cannot grant filesystem access or
authorize real purchases/messages, personal-data reset, merge or release.
The new Instinct feature remains at the explicitly requested research checkpoint
until go; existing-app completion work can continue independently.

## Repository and published work

| Field | Checkpoint |
|---|---|
| Checkout | ASOS inside the user's thoera-mac workspace; verify actual root |
| Origin | `https://github.com/FERAL-AI/FERAL-AI.git` |
| Working branch | `feat/native-product-release-foundation-20261001` |
| Published review | [Draft PR #310](https://github.com/FERAL-AI/FERAL-AI/pull/310), open/unmerged |
| Most recent verified publication before this workflow change | `c7693fb2f39ab6c23ae90fc3c88a3af14ea06ea5` |
| Prior correction | `2754ce677`: wait for stale-heart-rate metadata without weakening assertions |
| Unrelated local edit | `AUDIT-FIXES.md`; preserve and exclude unless separately reviewed |
| Disk observation | 8.1 GiB available when checked; recheck before parallel compilation/packaging |

The checkpoint's own commit cannot name its future hash. Read current HEAD from
Git/helper; evidence below explicitly names the source it tested. Do not update
every historic source identifier to HEAD or regenerate a baseline to make a gate
appear green.

## Evidence at the last checked source

Source **c7693fb2f**, [CI run 37021517741](https://github.com/FERAL-AI/FERAL-AI/actions/runs/37021517741):

- Corrected-source web build/coverage and Playwright **passed remotely**. Browser
  specs stub API responses; live-brain workflow was skipped.
- Ruff, architecture, syntax, asset coherence, node SDKs, registry and extension
  checks **passed**. Generic SDKs under `sdk/python` and `sdk/node` are separate
  from the node SDK checks.
- Docs, naming and version workflows **passed**.
- Backend Ubuntu/Python 3.11 PR fast lane **passed: 12,127 tests / 83 skipped,
  73.95% coverage**. Parent re-queried the completed job and its actual log.
  This is the PR lane; the separate Linux matrix was skipped.
- [Native run 37021517971](https://github.com/FERAL-AI/FERAL-AI/actions/runs/37021517971)
  **passed** production typecheck, feature/linked/desktop checks, real child
  lifeline tests and bundle-auditor fixtures. This job does not operate the app GUI.
- Non-blocking mypy **failed: 859 errors in 244 files, 1,240 checked**, versus the
  recorded 812-error ratchet. The 47-count rise requires attributable-regression
  triage; it must not be called wholly unchanged debt. Use matching Ubuntu/Python
  conditions; the logs forbid regenerating this baseline on macOS.

Packaged Mac artifact inspected directly, without a new launch: **2026.9.26 /
2026100117**, `desktop-native/build/FERAL Native Preview.app`; executable SHA-256
`3dbfbbd63af7206bd99d81065e6f8a3f239bf0eb4bd3e31584f6c2cbdae7a0e3`.
Historical 9.26 avatar-onboarding/quit/relaunch passed. Earlier actual reply/import/
coding outcomes belong to their earlier candidates. This artifact is not certified
as containing every later source commit or completing the populated-profile matrix.

## Current workflow-setup wave

These worker records describe this session's assignments. Before reusing files,
check whether the workers are live, done or interrupted.

| Worker | Owned scope | Status at checkpoint |
|---|---|---|
| Parent | AGENTS/codex, RESUME_WORK/WORK_STATE, helper CI step and publication | Reviewed integration, actual helper and targeted checks passed; publication checkpoint being committed |
| work_recovery_helper | `scripts/feral_work_status.py`, its dedicated tests | Complete: 10 isolated tests, Ruff and actual human/JSON modes passed |
| current_ci_reconcile | Read-only GitHub/candidate inspection | Complete; evidence above received |
| next_wave_contracts | Read-only exact modules/tests/worker boundaries | Complete; exact assignments/commands below received |

No new payment, message, account connection, background daemon or global
permissions configuration has been performed. The status helper must not be
described as an autonomous execution supervisor.

## Next ready implementation wave

Reserve explicit files before spawning. The parent coordinates dependencies and
fixes; these are planned assignments, not claims that workers have started them.

| Card / worker | Concrete deliverable | Exit evidence |
|---|---|---|
| NATIVE-01A/03A / A | Immutable candidate plus populated disposable profile: conversations/rich records/reviews/deny/cancel/exit/relaunch. Own a dedicated evidence doc and disposable probes, no production Swift until a defect is reproduced | Actual GUI/action evidence tied to candidate/source, focused regression tests for defects; preserve personal installation |
| CODE-01A / B | Own `skills/impl/external_agent.py` and existing external-agent skill/memory tests under feral-core. Bind continuity to trusted caller context, reject foreign scoped handles, document legacy unbound semantics | Interleaved same-workspace A/B, conflicting supplied identity, live/persisted foreign handles and engine reattachment; existing ACP/digest reused |
| DEV-01A / C | Own `sdk/python/feral_sdk/client.py`, its README and new dedicated SDK HTTP tests. Add supplied credentials, correct registered routes and distinguish HTTP/application failures | Registered API contract tests, denial/malformed/timeout behavior; no automatic confirmation or retry. WS and Node are separate follow-on slices |
| TYPE-01 / parent follow-on | Classify the mypy ratchet increase by source/environment; repair attributable regressions | Matching-source Ubuntu check; do not blindly raise baseline |

SDK findings from inspected source include `/api/health` versus registered
`/health`, missing constructor authentication, and an invocation URL not registered
by the current server; actual invocation is POST `/api/tools/execute` with
`skill_id`, `endpoint`, `args`, optional `session_id` and explicit `confirm`.
An HTTP 200 can contain an application failure; preserve that distinction.
Confirm contracts before editing. ToolRunner already binds session context;
external_agent currently ignores it when choosing continuity. CODE-01A fixes that
bounded gap. Parent-coordinated CODE-01B separately owns coding REST/native/web
identity propagation, preserving the Coding page's independent workspace semantics.

Existing targeted commands, from `feral-core` with disposable test storage:

```sh
../.venv/bin/python -m pytest tests/test_external_agent_skill.py tests/test_external_agent_memory.py tests/test_call_context.py tests/test_tool_runner_call_context.py -q --no-cov -p no:randomly
```

From `desktop-native`, after any needed compiler permission:

```sh
bash test_features.sh Conversation RichChat ChatTools Attachment SessionRecovery
bash test_features.sh Oversight Security Operations RuntimeHealth
```

These also run linked-model/desktop/error checks and remain fixture evidence.
From ASOS, `.venv/bin/python -m pytest sdk/python/tests -q --no-cov -p no:randomly`
is the planned command **after DEV-01A adds that suite**; it does not currently
exist. Actual GUI isolation must explicitly set temporary runtime home/port and
distinct native preferences, and use CUA. Merely opening the default preview
does not establish an isolated profile or populated-profile acceptance.

After this wave: workflow authority/recovery, durable receipts, memory/migration,
voice coordination and exact-device/iOS contracts. Messaging/Link adapters follow
the requested research review boundary. Social/Linux/avatars and developer
distribution work remain explicit cards; do not drop them from scope.

## External acceptance and decision dependencies

Signing/notarization credentials, named permitted provider/account identities,
real mic/audio, physical iPhone/glasses/background matrix, clean-machine upgrades
and a Linux host matrix must be checked. They are declared dependencies, not proof
that access is unavailable. GitHub reported default-branch dependency alerts; inspect
affected packages/applicability before declaring them fixed or irrelevant.

## Update record format

For each integrated card append: date; card and owner; exclusive files; source SHA;
exact verification command/exit/result and evidence class; candidate/hash where
relevant; remaining acceptance; commit/PR; next action. Mark implemented,
verified and published independently. Preserve failed runs and stale claims in a
dated record instead of overwriting them as if they had passed.

## October 2 workflow setup verification

- Parent reviewed the helper and ran its 10 isolated tests successfully, exact
  targeted Ruff successfully, and actual human/JSON status modes successfully.
  Helper reported the expected checkout/origin, HEAD, dirty paths, 8.1 GiB free
  and all recovery documents present. It does not read file contents/history,
  check remote CI or recover workers.
- Added the same standard-library helper tests to the existing CI syntax job.
  Remote acceptance of that new step belongs to the subsequent commit/run;
  this local result does not establish it has run on GitHub yet.
- Parent checked CI YAML parsing/step placement, diff whitespace, repository
  naming, 135 tracked shipped-document leakage targets and changed-document
  local links successfully. The unfiltered leakage scan's three pre-existing
  ignored handoff files remain outside publication; they were not changed.
- Resume/approval flags were checked against installed CLI help and official
  fetched OpenAI docs. No second session, goal, daemon, hook or global config
  change was started. The current managed permission profile remains unchanged.
