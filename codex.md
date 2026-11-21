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

Keep work batches bounded by concrete failures and acceptance checks. Reuse
completed source reviews and retained results when their relevant inputs are
unchanged. Give workers concise file ownership and avoid repeating full history
or assigning overlapping audits. Run focused regressions during implementation;
broaden testing when integration or an unresolved risk requires it. Package a
coherent verified wave instead of rebuilding after every small edit. At the end
of each batch, checkpoint and report completed behavior, current artifact/source,
unverified boundaries and the next ready work. Do not imply work continues after
a deliberate pause or that a passing fixture proves physical-device operation.

Resource and retention rules are in AGENTS.md. Before a build wave, record source,
free disk space, estimated new artifacts, exclusive ownership and the acceptance
command. Keep one active status summary in WORK_STATE; mark preceding summaries
as historical. Update the current artifact, verified outcomes, failures, remaining
work and next assignments together so documentation cannot imply that a stale
candidate contains later source changes. Resource failures belong in the evidence
ledger and must not be reported as application failures without diagnosis.
