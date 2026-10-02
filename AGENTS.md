# FERAL contributor and agent rules

## Scope and sources of truth

For continuing FERAL completion, read `codex.md`, then
`docs/roadmap/theora-personal-agent/WORK_STATE.md`, `RESUME_WORK.md` and
`EXECUTION_PLAN.md` before assigning work. Compare the checkpoint to actual Git,
CI and candidate identity; do not assume chat history or previous workers survived.

Work in this repository, not the enclosing home-directory Git repository. Confirm
`git rev-parse --show-toplevel` before Git operations. Read `CLAUDE.md` for the
existing runtime, interpreter and development conventions. Nested instructions
apply to their own subtree; vendored OpenCode has its own instructions.

FERAL is a local-first, open-source runtime. Extend the existing control plane,
memory, policy gates and headless contracts. Preserve existing working clients
while native replacements gain verified parity. Do not silently change a user's
deployment, accounts, saved data or autonomy settings.

## Execution and privacy

- Route actions through the central authorization and tool execution path.
  Channel, voice, proactive and external-agent entry points must preserve owner,
  session, task and approval identity. A missing grant must not become consent.
- Keep credential values, payment card data, personal profiles, databases and
  private task content out of source control, model transcripts and normal logs.
- Distinguish accepted, approved, executed and outcome-verified states. A queued
  handoff, successful script exit or completed model turn is not an outcome receipt.
- Do not replay spending, messaging or other external effects after an uncertain
  response. Reconcile first. Review the exact action and renew approval when its
  material terms change.
- Users choose their providers and autonomy policies. Make those choices explicit,
  inspectable and revocable; do not hardcode hidden permissions.

## Implementation and verification

- Inspect real interfaces before editing. Preserve concurrent work. Give parallel
  workers distinct file ownership and integrate their changes sequentially.
- The user requested parallel workers for the FERAL completion effort. Use a
  parent integrator and up to three workers for independent ready cards when
  available. The parent owns shared contracts, checkpoint and Git publication;
  workers receive exclusive files and concrete acceptance commands. Reconcile
  stale claims after interruption before reassigning those files.
- Keep blocking work out of async handlers and retain background task references.
  Use typed errors and contextual, redacted diagnostics instead of silent failure.
- Test the failure and cancellation paths as well as success. Use isolated profiles
  and disposable workspaces; never point mutating tests at a personal deployment.
- Run relevant backend/native/web checks and the applicable CI commands in
  `CLAUDE.md`. Build and inspect the actual app when changing native layout.
- Freeze the sources under test during full-suite and candidate verification.
  Edits or formatting during a run invalidate affected results and can break
  source-inspection fixtures. Integrate workers before starting those checks.
- Check available disk space before packaging or large parallel compilations.
  Reuse build caches and bound retained generated app copies. Cleanup needs exact
  inspected paths; preserve profiles, models, source, Git history and test evidence.
- Report source inspection, fixture tests, real integration checks and physical
  device tests separately. Never claim unperformed tests or production readiness.
- Update public documentation and the release-readiness evidence with behavior
  changes. An unfinished feature needs an explicit limitation and acceptance gate.
- Update WORK_STATE at task integration, before publication and deliberate pause.
  Record tested source/candidate, result, remaining acceptance and next action.
  Keep implemented, verified and published states separate; persist coherent
  commits rather than relying on chat memory or a shutdown hook.

## Git and releases

- Stage explicit reviewed paths. Exclude ignored internal dossiers, credentials,
  runtime homes, downloaded dependencies and generated app bundles.
- Review the staged diff, whitespace and sensitive-data exposure before committing.
  Commit coherent changes with their tests and documentation.
- Fetch before publication. Publish reviewable branches to the existing repository;
  do not force-push, merge or release as a substitute for review and acceptance.
- A passing local build is not signing, notarization, clean-machine installation,
  Linux acceptance or iOS acceptance. Keep those gates visible.

Current product and release work is tracked in
`docs/roadmap/theora-personal-agent/RELEASE_READINESS.md`. Native capability coverage
and dated evidence are in `desktop-native/FEATURE_PARITY.md`.
