# Native 9.52: private phone task review acceptance

October 10, 2026. Canonical application: desktop-native/build/FERAL Native
Preview.app. Version 2026.9.52, build 2026101001, exact runtime source
`0138f3d24751adb00b1728adc5dfc7b086663c3e` on the PR310 feature branch.
Later documentation commits do not change this immutable runtime.

## Behavior and source checks

Task reviews now bind to the authenticated originating device before publication.
Private request/resolution cards have a current exact-device audience, with no
session-broadcast fallback. Guarded REST discovery and decisions require version 1,
the originating conversation and the full exact server card. Waiting model-action
reviews can be explicitly renewed after reopen by the same device with freshly
verified credentials. Renewal issues a new card and dispatches nothing. Existing
central execution, operator approvals and unknown-effect protections remain.
[Wire and client contract](PHONE_REVIEW_AUTHORITY_20261010.md).

Final frozen integration passes 917 tests, one skip and 325 warnings across 53
selected suites in 62.67 seconds, with zero input drift. It includes registered
HTTP middleware/routes, real disposable pairing/credentials, TaskFlow/SQLite,
central dispatch and registered HUP audience logic with simulated sockets and
inert effect/model boundaries. It is not the whole backend repository or physical
iPhone/browser/account acceptance.

Full configured Mac mypy retains 788 existing diagnostics with zero normalized
additions/removals and zero input drift. Full-core CI Ruff, authored whitespace
and public-document checks pass. The first typing run added four nullable renewal
route diagnostics; validation was corrected before final acceptance. Two legacy
tests bypassed ToolRunner construction and lacked its new private map; only their
fixture initialization was reconciled. Earlier failed receipts remain separate.

Swift and WebUI inputs are unchanged. Earlier 9.50 GUI/full-WebUI evidence remains
historical; no fresh Swift compile, WebUI build/full run or new 9.52 GUI is claimed.

## Exact artifact and retention

- All 501 packaged Python and 70 WebUI files match committed source. Runtime/
  import, dependency containment and strict deep ad-hoc signature audits pass.
- All 54 Swift inputs and flags match the preceding manifest. Verified native
  output compiled from 58ad0a025 is reused; the whole application is signed again.
  Reused pre-signing binary SHA256 is
  `02c418fc8722e596f1cf01e99aa6f07acdb39d8dd06001314387144dd0437797`.
- Final signed native SHA256 is
  `0ebe01d58f56e49691c9a15c4537c2cdcf7ded91e9c2175536ca83aba46acd67`.
  Manifest SHA256 is
  `a5c136603e95654332312e4139d750dde089404237bf5a82fe8807d532b72cf5`.
- Offline cached assembly takes 37.52 seconds. Free disk space is 16,750,440,448
  bytes before and 16,504,766,464 after, net 245,673,984 bytes within the 2 GiB
  batch budget. Peak use is unmeasured; concurrent disk changes are not attributed
  to this build.
- Canonical 9.52 and sole generated rollback 9.51 remain. Inspected obsolete
  9.50 was retired after identity, signature, ownership and process checks.
  Personal data, models, caches and useful Git history remain untouched.
- Signing remains ad-hoc arm64/macOS13. Clean download/install/update, Developer
  ID and notarization remain distribution gates; this is not a public release.

## Actual bundled runtime

Bundled Python imports eight actual packaged modules. Real disposable pairing,
full bearer verification/rotation and SQLite exercise private review checkpoint,
foreign refusal, close/reopen, fresh same-device discovery and explicit renewal.
Old and duplicate cards do not dispatch; one inert central effect is recorded and
model-step continuation completes from that receipt without repeating it. The
model and executor boundaries are inert fixtures. The creation adapter receives
trusted fixture metadata; this probe is not an installed-phone or HUP/HTTP session.
No provider inference, account, audio, message or payment is performed.

Actual bundled backend health becomes reachable in 2.14 seconds. Setup HTTP200
correctly reports incomplete setup. The owned process exits with SIGTERM after
its stdin lifeline closes; the listener closes and artifact identity is unchanged.
The disposable local provider has no credentials/fallbacks. Probe requests are
loopback only; arbitrary backend egress is not instrumented. No native GUI runs.

Private receipts include feral-phone-review-gate-zbhyo8k0/receipt.json,
feral-phone-review-typing-aw4coxzw/comparison.json,
feral-native-9-52-build-20261010/{result,bundle,core}.json,
feral-candidate-9-52-manifest.json,
feral-952-packaged-review-9_343ty7/receipt.json and
feral-952-backend-startup-evidence.json. Private profiles/receipts are excluded
from Git.

## Next acceptance gates

Job-scoped Chrome delegation is the next implementation card. iOS must retain
exact cards, negotiate the inbox version, support separate renewal and preserve
unknown-outcome responses. Physical iPhone/glasses connection, configured inference,
browser/desktop tasks, local provisioning and continuous voice remain unverified.
Messaging, checkout, proactive multitasking/shared collaboration and distribution
remain in the complete product plan. Initial pre-creation proposals are volatile;
this model-step renewal does not reconstruct them. Review isolation does not claim
general multi-user account isolation or browser permission.

The predecessor's Ubuntu PR backend fast lane failed while 17 other checks passed
and four skipped; its cause is unclassified here. Current remote CI is separate
from this local acceptance. No merge or production release is performed.
[Checkpoint](WORK_STATE.md), [complete coverage](REQUEST_COVERAGE.md),
[iOS handoff](IOS_AGENT_HANDOFF.md).
