# Scheduled CI maintenance — November 14, 2026

Extended workflows run weekly with manual dispatch retained. Required pull-request
checks remain enabled and retain their job names and tests.

| Workflow | Scheduled time (UTC) |
| --- | --- |
| Voice soak | Monday 04:13 |
| Sync chaos | Tuesday 05:17 |
| Real-brain browser e2e | Wednesday 04:37 |
| Provider research | Thursday 09:23 |

Live-channel soaks require a manual opt-in and configured staging-service secrets.
Voice duration remains 60 minutes per protocol on the weekly run. Manual durations
are bounded by the existing job timeouts. Soak and chaos logs retain seven days;
retention changes apply to new artifacts, not previously uploaded ones.

## Failure repairs

The [November 14 sync-chaos run](https://github.com/FERAL-AI/FERAL-AI/actions/runs/43592071770)
reported unawaited `apply_remote_changes` calls followed by divergent WAL state.
The CLI now awaits both reconciliation directions and closes async memory pools
before checking persisted state. The final failure gate remains enabled.

The [November 14 real-brain browser run](https://github.com/FERAL-AI/FERAL-AI/actions/runs/43590800260)
failed during Chromium installation, before the app or specs started. Its locked
Playwright 1.59.1 does not support the Ubuntu 26.04 image selected by
`ubuntu-latest`. Both browser workflows now pin Ubuntu 24.04, supported by the
[locked Playwright dependency table](https://github.com/microsoft/playwright/blob/v1.59.1/packages/playwright-core/src/server/registry/nativeDeps.ts).

## Verification checkpoint

- Two new chaos CLI regressions failed before the repair and pass afterwards.
- The affected chaos/recovery gate passes eight tests, with five existing warnings.
- The exact five-iteration CLI command converges to 40 keys in 10.91 seconds using
  temporary SQLite state and offline hash embeddings, with no app service or model.
- Workflow YAML parses with duplicate-key rejection; required CI jobs are preserved.
- Changed Python files pass CI-rule Ruff; authored whitespace checks pass.
- Ubuntu browser installation and full browser execution require GitHub-hosted
  acceptance. Static validation does not establish those outcomes.

The repair was prepared from `452a012557d06274063b72546433257b4694d611` in an isolated
checkout. Deployment, physical-device and billing changes are outside this repair.

## Activation checkpoint

On November 14 the four previous daily workflows were verified
`disabled_manually`, while `.github/workflows/ci.yml` remained active. This stops
the old scheduled executions without suppressing normal push/PR checks. Workflow
disablement also suspends their manual and labelled-PR entry points.

The weekly file changes must be merged before those four workflows are re-enabled.
After merge, enable `soak-nightly.yml`, `sync-chaos-nightly.yml`,
`v2-real-brain-e2e.yml` and `provider-research.yml` through the Actions interface or
`gh workflow enable <filename> --repo FERAL-AI/FERAL-AI`. File names are retained to
preserve existing links and workflow identities.
