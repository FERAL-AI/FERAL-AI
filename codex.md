# Working on FERAL with Codex

Read [AGENTS.md](AGENTS.md) first. It is the automatically discovered repository
instruction file and owns the shared contributor rules. [CLAUDE.md](CLAUDE.md)
provides the existing architecture and pinned-environment details.

On a resumed or fresh session, read
[WORK_STATE](docs/roadmap/theora-personal-agent/WORK_STATE.md),
[RESUME_WORK](docs/roadmap/theora-personal-agent/RESUME_WORK.md) and
[EXECUTION_PLAN](docs/roadmap/theora-personal-agent/EXECUTION_PLAN.md). Verify Git,
CI, dirty edits and worker claims before continuing the next ready cards. The
read-only `scripts/feral_work_status.py` helper reports checkout/resources/docs;
it does not recover processes or automatically grant permissions.

Use the existing FERAL repository and control plane. Track implementation and
acceptance in [release readiness](docs/roadmap/theora-personal-agent/RELEASE_READINESS.md),
and native coverage in [feature parity](desktop-native/FEATURE_PARITY.md).

When delegating, assign exclusive file ownership. When reporting, name what
changed, the checks actually run, the resulting commit or branch, and the remaining
release gates. Do not substitute a proposed demonstration for a working product.
During active work, give concise progress updates at least every minute. At each
integration checkpoint, state what is published, what remains local, what is
blocked and what comes next. Follow the public-publication wording rules in
AGENTS.md; technical records must not reproduce private conversations.
