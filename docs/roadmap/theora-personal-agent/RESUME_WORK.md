# Running and resuming FERAL completion work

Updated October 2, 2026. This is the operating procedure for the existing FERAL
completion effort. Product scope is in [EXECUTION_PLAN.md](EXECUTION_PLAN.md);
the current checkpoint and next assignments are in [WORK_STATE.md](WORK_STATE.md).

## Configure automatic review

The installed CLI's `codex --help` and `codex resume --help` both advertise
`--approve-for-me`. It routes eligible access requests through automatic review
while retaining workspace-write boundaries. Automatic review is distinct from
Full access; it cannot override managed requirements or guarantee every request
will be approved. Computer Use app-level prompts can still require the user.
[Official automatic-review documentation](https://learn.chatgpt.com/docs/sandboxing/auto-review).

In the ChatGPT desktop app, enable **Auto-review** under Settings → General →
Permissions, then select **Approve for me** beneath the composer in this chat.
Enabling the option alone does not change the current chat's mode. In the CLI,
use `/permissions` to select it. This repository does not change global Codex
configuration or the current managed permission profile.
[Official permission controls](https://learn.chatgpt.com/docs/permission-modes).

For a new CLI session in the actual repository:

```sh
codex --approve-for-me -C /path/to/FERAL-AI
```

Replace `/path/to/FERAL-AI` with the actual ASOS checkout, not its enclosing
workspace or home directory.

The equivalent documented settings, if the user chooses to persist them in their
own Codex configuration, are:

```toml
sandbox_mode = "workspace-write"
approval_policy = "on-request"
approvals_reviewer = "auto_review"
```

Do not switch to approval policy `never` expecting automatic review: that removes
the interactive requests the reviewer handles. Do not alter an organization
policy or install an unattended approval bypass. Approved engineering work
includes repository fixes/tests/documentation and reviewable commits/pushes;
real messages, recording, purchases and production releases retain their
separate authorization boundaries.

## Recover an existing session

Use the session picker, including sessions whose saved working directory was the
enclosing workspace rather than ASOS:

```sh
codex resume --all --approve-for-me -C /path/to/FERAL-AI
```

Choose this FERAL task. Once a saved session ID or name is known, it can be passed
to `codex resume`. `--last` selects the most recent matching session, which may be
the wrong task when several projects are active. Use the picker rather than
guessing. These commands were checked against installed help; no second model
session was launched to test restoration during this task.
[Official resume behavior](https://learn.chatgpt.com/docs/developer-commands).

Closing a window is different from deleting stored chat history. If history has
been cleared/deleted, or this is a fresh checkout/session, reconstruct work from
the repository instead of assuming the old conversation or subagents survived.
Source commits and these files survive independently of chat history. Uncommitted
changes survive only if the checkout is retained; therefore checkpoint regularly.

Start the new session with this instruction:

> Continue FERAL completion in ASOS. Read AGENTS.md and codex.md, then
> docs/roadmap/theora-personal-agent/WORK_STATE.md, RESUME_WORK.md and
> EXECUTION_PLAN.md. Verify the checkout, branch, dirty files, current CI and
> candidate identity. Reconcile interrupted work before resuming. Use a parent
> integrator and up to three workers with exclusive file ownership. Execute the
> next ready cards, test actual outcomes, update the checkpoint, and commit/push
> coherent changes. Preserve existing functionality and personal data. Do not
> restart the project or relabel unverified work as complete.

## First actions after interruption

1. Confirm `git rev-parse --show-toplevel`, current branch, HEAD and origin. Never
   run Git operations in the enclosing home repository. Read the dirty path list
   and preserve unrelated edits. The status helper below performs read-only checks.
2. Read the last checkpoint and compare its source SHA to current Git and remote
   PR/CI state. Passing evidence belongs to its tested source/candidate, not
   automatically to the latest checkout. Record stale or missing evidence.
3. Reconcile worker claims and running processes. Persisted worker names are
   historical records, not proof of live workers or ownership locks. Recover
   unfinished diffs before assigning those files again. Do not blindly rerun
   interrupted commands with external effects or kill unknown processes.
4. Select ready task cards with all prerequisites available. An account/device
   dependency must not block unrelated local implementation or fixtures. Assign
   exclusive files, acceptance commands and integration order before spawning.
5. Integrate workers sequentially, freeze sources for candidate/full-suite checks,
   run appropriate tests, update the state/evidence, review explicit staged paths,
   commit and push. Do not merge or publish a release without its own gates.

Read-only recovery check from ASOS:

```sh
.venv/bin/python scripts/feral_work_status.py
.venv/bin/python scripts/feral_work_status.py --json
```

The helper is a checkout/resource/document check. It does not authenticate to
GitHub, infer CI success, inspect private memory or prove a model session resumed.

## Execution and checkpoint cadence

This environment exposes **four agent slots total: parent plus three workers**.
Use independent workstreams; do not assign multiple workers the same source files.
The parent owns shared contracts, WORK_STATE, integration, Git publication and
final acceptance. Give workers a bounded card rather than the entire product.

The task lifecycle is: **ready → active → implemented → verified → published**.
Use **blocked** only with a concrete unmet dependency and next action. An
implementation with only fixtures stays distinct from real GUI/account/device
acceptance. A published commit can still have pending CI. Keep failure evidence.

Update WORK_STATE at each task integration, before publishing, before a deliberate
pause and whenever interrupted work changes the next safe action. Record card,
owner/files, source SHA, commands/results, candidate identity, outstanding tests,
blocker and next action. Commit the checkpoint with each coherent change; do not
depend on a final SessionEnd hook that might never run during a crash.

For sustained iteration, Codex supports `/goal` with a concrete outcome and
definition of done. A goal retains the same access limits. Local work requires
the machine/runtime to remain available; do not assume a closed app, stopped
runtime or sleeping/offline Mac keeps running these workers. Keep the local app
open and use the supported prevent-sleep setting when appropriate. Cloud tasks
can cover compatible source work, while native Mac GUI/Keychain/glasses acceptance
still needs its host and hardware. This task has not installed a background
daemon, started a goal or configured cloud execution.
[Official long-running work](https://learn.chatgpt.com/docs/long-running-work),
[local task availability](https://learn.chatgpt.com/docs/dots/tasks-and-memory).
