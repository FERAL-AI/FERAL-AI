# Native 9.49: readiness and device authority acceptance

October 9, 2026. Canonical application: desktop-native/build/FERAL Native
Preview.app. Version 2026.9.49, build 2026100903, exact published runtime source
`58ad0a0252a87230a5d08552b6ab316ce221451c` in draft PR310. Later documentation
commits do not alter this immutable runtime.

## Artifact and retention

- Fresh optimized arm64/macOS13 compilation uses 54 Swift inputs.
- All 501 packaged production Python files and 70 tracked WebUI files match
  committed source. Runtime, supported desktop imports, Python/SDK/OpenCode
  containment and strict deep ad-hoc signatures pass.
- Native binary SHA256:
  `cb31865625d206af6dc7cc64e1a5279fae33b54117da66052645cb3304cd9da6`.
- Manifest SHA256:
  `29f59fd1ca166979a48440f4dab7e5ec65eef790adf846c6e35e66c693f6eeea`.
- Cached offline staging/assembly passes in 262.34 seconds. Free disk space is
  16,471,724,032 bytes before and 16,375,496,704 after: net 96,227,328 bytes,
  within the 2 GiB wave budget. Peak usage is unmeasured; concurrent disk changes
  are not attributed to the build.
- Canonical 9.49 plus sole generated rollback 9.48 are retained. The inspected
  obsolete 9.47 copy was retired after identity/process/signature checks.
  Small prior manifests and acceptance receipts remain. Personal profiles,
  models, caches and useful Git history are preserved.
- Signing is ad-hoc, not Developer ID/notarized clean-install distribution.
  Do not modify the signed app in place or launch a partial future assembly.

## Source verification

Frozen integration passes 4,766 tests, 22 skips and 571 warnings across 234
selected suites in 159.66 seconds with no source drift. This is the focused
integration gate, not the full repository suite. Nine approval-focused suites
pass 207 tests. A test-only imported-fixture lint adjustment is followed by
14 passing node authority cases; production sources are unchanged. Pinned Mac
mypy retains 796 existing diagnostics with zero normalized additions/removals
versus 9.48 and unchanged source hashes. The Linux baseline is untouched.
CI-rule lint and version coherence pass.

Native Providers passes 75 assertions across 21 groups; onboarding passes
100 assertions; the linked model passes 29 groups and production typecheck
passes all 54 sources. Independent review confirms generation/cancellation
fences protect explicit probe history. No inference/network operation occurs in
these fixtures. Earlier compiler/lint failures remain separately recorded.

[Device authority behavior and limits](DEVICE_APPROVAL_AUTHORITY_EVIDENCE_20261009.md)
and [native readiness presentation](NATIVE_READINESS_PRESENTATION_20261009.md)
record the exact scope. Previous shared-conversation GUI/readback acceptance
belongs to [9.48](NATIVE_9_48_ACCEPTANCE.md); that journey was not repeated here.

## Actual bundled backend and native app

The bundled backend reaches health in 2.12 seconds, returns setup HTTP200 with
incomplete setup, and passes owned process-group/lifeline shutdown and listener
closure with unchanged artifact identity. Its unavailable local-only provider has
no credentials or fallbacks. Probe requests are loopback only; arbitrary backend
egress is not instrumented.

Computer Use operates a new disposable profile with saved port 43284 and no
port override. Avatar/name setup and the exact saved local provider render.
An explicitly reviewed negative probe shows Last explicit probe: unreachable,
with model inference separately unverified. Passive setup refresh retains that
history. Set up later opens Chat, whose status now describes receipt connectivity
without claiming model availability.

AI Providers starts with no view-local history, as intended. Its independently
reviewed negative probe shows unreachable; Refresh saved status retains that
history and updates the backend cached health row to cached probe failed. Editing
the draft model clears the explicit history. The draft is restored without a
save, and an independent settings read confirms the original model and endpoint
remain unchanged. Home renders sourced-state caveats; no calendar/account lookup
or health inference is requested. The settings screenshot still exposes legacy
runtime-available wording and low-contrast action styling for later UX review;
these do not certify working model inference.

First normal Quit exits 0. A second launch restores Home/avatar/name and the same
saved port under a distinct runtime instance. Chat again displays receipt-only
readiness. Second normal Quit exits 0. Final checks find both hosts and observed
backends absent, the saved listener closed and native/manifest identity unchanged.
The post-Quit capture API reports App quit / noWindowsAvailable, consistent with
the exit receipts, not an application crash. GUI observations are distinct from
process/hash/socket checks and source regressions.

Private receipts: feral-native-9-49-build-20261009/{result,bundle,core}.json,
feral-candidate-9-49-manifest.json, feral-949-backend-startup-evidence.json,
feral-oct9-native-readiness-evidence.json and both launch/health/exit receipts
under feral-oct9-native-readiness-profile. Private fixture profiles are excluded
from Git. All acceptance-owned hosts are stopped.

## Remaining gates

Paired REST tool-review inspection/resolution and node-origin tool-review replies
are explicitly unavailable until server-authenticated per-request device provenance
is persisted. Profile operator approvals remain usable. Existing session broadcasts
can still carry review metadata; origin-authorized publication/private delivery is
not established. Generated-skill approval identity remains a separate contract.

Next: negotiate tracked phone turns with the existing ChatTurnManager, persist
private origin authority through existing TaskFlow receipts/reviews, then enable
matching phone approval projection/resolution. Explicit job-scoped Chrome
permission follows separately; shared conversation selection grants no action
permission. Do not restore old grants or replay unknown effects.

No real model response, physical phone/glasses, mic/speaker, account, iMessage,
checkout or model download was exercised. Actual provider/model choice plus spend/
download limits and the installed phone endpoint/error are still needed. Local
model/speech provisioning, continuous voice, full browser/desktop tasks, clean
installation/update and signed distribution remain in the full release plan.
At exact runtime 58ad, 17 remote checks pass, four conditional/main-only checks
skip and broad backend PR tests remain pending at this checkpoint. This is not
full-product or distribution acceptance.
