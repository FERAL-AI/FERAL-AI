# Native 9.48: shared conversation and runtime acceptance

October 9, 2026. Canonical application: desktop-native/build/FERAL Native
Preview.app. Version 2026.9.48, build 2026100902, exact published runtime source
`24a44c6ba42de9c3fbba9d597f3e441cf740b1f1`, draft PR310. Documentation commits
following this source do not change the immutable runtime.

## Artifact and retention

- Fresh optimized arm64/macOS13 compilation uses 54 Swift inputs.
- All 500 packaged production Python files and 70 tracked WebUI files match
  committed source. Contained Python/SDK/OpenCode, supported desktop imports,
  runtime and strict deep ad-hoc signature audits pass.
- Native binary SHA256:
  `f5028912bfd3d4e298292091674bc989e7a0c2fefb860d06fc56b77cedda732f`.
- Manifest SHA256:
  `fd1781cd08d6af88da41ec38fc8e7a663470f1f6453944553da43d7aa26f85e8`.
- Cached offline staging and assembly pass in 191.43 seconds. Free space is
  15,567,773,696 bytes before and 15,494,037,504 after: net 73,736,192 bytes,
  within the 2 GiB wave budget. Peak usage is not measured; concurrent disk
  activity is not attributed to this build.
- Canonical 9.48 plus sole generated rollback 9.47 are retained. The inspected
  obsolete 9.46 copy was retired only after identity/signature/process checks.
  Personal profiles, models, caches and Git history were preserved.
- This is ad-hoc signing, not Developer ID/notarized clean-install distribution.
  A failed future build must be inspected and the verified rollback explicitly
  restored; do not launch a partial bundle or modify this signed app in place.

## Frozen source verification

The final source wave passes 4,734 tests, 22 skips and 571 warnings across 232
selected suites in 130.66 seconds with zero source drift. This is the focused
integration selection, not the complete repository suite or a real account task.
Configured Mac mypy reports 796 diagnostics versus pre-wave 799: zero normalized
additions and three removals. The Linux baseline is unchanged. CI-rule Ruff passes.

Provider/API/alias checks pass 89 tests; native setup/provider checks pass 89/66
assertions. Shared-conversation checks pass 29 linked model groups, 118 Browser
checks and production typecheck. Installed-Chrome Playwright passes 108 tests,
with 63 intentional real-backend opt-in skips; 47 Home/navigation Vitest checks
and the unchanged production JS/CSS build pass. Earlier failed invocations and
counterexamples remain retained separately.

Included runtime changes and their limits are recorded in
[model-task approval evidence](TASK_MODEL_APPROVAL_EVIDENCE_20261009.md),
[workflow origin policy](TASKFLOW_ORIGIN_POLICY_EVIDENCE_20261009.md),
[phone integration](IOS_PHONE_INTEGRATION_20261009.md) and
[shared-conversation behavior](SHARED_THEORA_CONVERSATION_20261009.md).
HUP send counts mean socket acceptance, not message delivery or read confirmation.

## Actual packaged backend and native GUI

The bundled backend reaches health in 2.29 seconds, returns setup HTTP200 with
incomplete setup, and passes owned-process/lifeline shutdown and listener closure.
Its unavailable local-only provider has no credentials or fallback. Probe requests
are loopback only; arbitrary backend egress is not instrumented.

Computer Use operated the actual candidate with a disposable profile. The first
launch shows the FERAL logo, avatar choice and saved provider configuration.
An explicitly reviewed negative local provider probe reports unreachable and
states that no successful model response is claimed. Set up later opens Chat;
Home renders its sourced-state caveats.

The isolated backend then stores two distinct two-record fixture conversations,
including stable IDs, reasoning and tool metadata marked not_executed. Selecting
the separate conversation shows only its records. In Browser, cancelling the
shared Theora conversation review preserves that selection and history. A fresh
review and confirmation select the canonical primary session. Chat shows only
shared records; independent backend readback confirms both original histories
and rich metadata are unchanged, with no separate records copied into shared
context. No Chrome attachment or browser action authority is granted by switching.

Normal native Quit exits 0. A second launch using the same profile restores the
shared conversation, avatar and saved endpoint, with a distinct runtime instance.
Independent readback again confirms both histories. The second normal Quit also
exits 0. Final checks confirm both hosts and observed backends absent, saved port
43283 closed, no port overrides and unchanged artifact/manifest identity.

The post-Quit capture API returned ScreenCaptureKit error -3811 on the second
exit. This is retained as a capture failure; the host exit receipt is 0 and does
not establish an application crash. GUI observations and automated process/hash/
readback checks are separate evidence, not proof of real model task execution.

Private receipts: feral-native-9-48-build-20261009/{result,bundle,core}.json,
feral-candidate-9-48-manifest.json, feral-948-backend-startup-evidence.json,
feral-oct9-native-shared-browser-evidence.json and both conversation-readback
receipts under feral-oct9-native-shared-browser-profile. Private fixtures and
profiles are excluded from Git. All acceptance-owned hosts are stopped.

## Remaining acceptance and next cards

The explicit negative probe is truthful, but its passive cached row remains
unprobed. Chat's Verified chat ready label refers to context/admission and does
not prove model inference readiness; readiness presentation needs correction.
No model response was requested. Ollama's inspected inventory is empty, and no
local model or speech assets were downloaded by this wave.

Originating-chat/phone background approval projection and resolution remain
separate from the repaired HUP sender. Job-scoped Chrome delegation needs an
explicit task/resource permit; the physical Chrome owner must remain intact.
Physical Theora connectivity, pairing/LAN/ATS, model-selected browser/desktop tasks,
continuous microphone/speaker behavior, iMessage delivery/read status, accounts,
checkout, clean installation/update and signed distribution remain unverified.
The separate iOS agent's local build and source tests are not a device acceptance.
See [release readiness](RELEASE_READINESS.md) for the complete feature gates.

At runtime source 24a44c6ba, remote native, Linux bundle, browser E2E, WebUI,
SDK, lint/docs/version and typing checks pass. The broad backend PR test job is
still pending at this checkpoint; opt-in/main-only checks are skipped.
