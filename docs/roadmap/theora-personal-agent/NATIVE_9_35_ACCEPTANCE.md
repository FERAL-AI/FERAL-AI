# Native 9.35 acceptance checkpoint

Updated October 3, 2026. Candidate source is
`12cc62c480422a3d844eada816d9cdf29c79fbef`, version 2026.9.35,
build 2026100302. These results apply to this immutable candidate; subsequent
source changes require their own acceptance.

## Package and automated verification

Assembly completed. All 51 native build inputs match the source commit before and
after compilation; all488 packaged production Python files match that commit.
The bounded bundle audit reports 13,108 files, 265 Mach-O files, 9 symlinks and no
issues. Isolated runtime probes confirm bundled Python 3.11.15, SQLite 3.53.1,
FTS5 and OpenCode 1.18.10. Strict deep ad-hoc signature verification passes.
This is not release signing, notarization or clean-machine installation.

Executable SHA256:
`73ff92317cfcb71cc30ec28d76e48ba79a4c6e63b01815b6393480ac5105c319`.
Manifest SHA256:
`fcf6d0d29c9a49e114655ab51bcb39de7edfcb8170da3d15525389e28aa63de7`.

The final frozen backend wave passes 471 checks and adds no typing diagnostics
against its retained baseline. Native production typecheck and linked model
checks pass, including voice ownership, context fencing, Stop and single-stage
onboarding. [Voice evidence](MANAGED_CHAINED_VOICE_INTEGRATION_EVIDENCE.md),
[onboarding evidence](MAC_SINGLE_STAGE_ONBOARDING_EVIDENCE.md).
[Native CI](https://github.com/FERAL-AI/FERAL-AI/actions/runs/37166186400)
passes on the exact published source, including 28 process-ownership assertions.
The general CI backend-coverage job was still running at the recorded snapshot;
this document does not claim its conclusion.

## Actual isolated Mac journey

A fresh synthetic profile used normal shipped tools and an existing local model
service. No model service was created, changed or stopped. Offline caches, null
keyring and deferred vault initialization isolated this acceptance from personal
accounts and data.

- The supplied portrait and logo render; FERAL and Orb selection are accessible.
- Avatar/name Continue opens one reviewed provider stage. Provider activation,
  readback and reviewed completion reach the connected main app without the
  preceding duplicate onboarding flow.
- A normal arithmetic prompt generated its correct answer in the transcript.
  The native view also displayed an incomplete fragment and a cancelled/stopped
  status after health timeouts and a WebSocket disconnect/reconnect. This is a
  failed whole-turn acceptance, not a reliable-chat pass. The exact durable
  receipt is cancelled/unknown with empty final text. Source/log reconciliation
  establishes optional skill discovery awaiting after the main answer, before
  manager terminal commitment. The disconnect cancelled that still-owned wait.
  A scoped sequencing repair is active and is not part of this candidate.
- The app remained connected after recovery and exited normally with host code 0.
  No new crash was observed in this bounded journey.

Critical host disk exhaustion occurred during this run. It blocked profile
creation initially and later caused GUI image and SQLite diagnostic failures.
Health delays and the misleading stopped state were observed, but disk exhaustion
alone has not been established as their complete cause. The resource blocker was
removed before further acceptance work.

## Remaining gates and retained evidence

Reconcile the actual tracked turn and repair connection-loss presentation or
ownership behavior established by that receipt. Repeat normal tool-enabled chat,
recovery and restart with adequate headroom. Actual microphone/speaker, cloud
accounts, provider reliability, migration, clean installation and signed
distribution remain open. No real messages, purchases or device captures ran.

Retained private evidence includes the 9.35 manifest, bundle/core audit, 51-input
snapshot, fresh-profile preparation/launch and native/backend logs. The preceding
9.34 candidate is retained. Obsolete generated 9.32/9.33 app copies were removed;
their manifests, logs, profiles and historical results remain.
