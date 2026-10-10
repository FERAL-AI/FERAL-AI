# Native 9.50: paired input and setup acceptance

October 9, 2026. Canonical application: desktop-native/build/FERAL Native
Preview.app. Version 2026.9.50, build 2026100904, exact published runtime source
`5f488ba8a3e0012c6e02a145ca1c4bcb16cb3e91` on the feature branch for draft PR310.
Later documentation commits do not alter this immutable runtime.

## Artifact and retention

- All 501 packaged production Python files and 70 tracked WebUI files match
  committed source. Runtime/import, Python/SDK/OpenCode containment and strict
  deep ad-hoc signature checks pass.
- All 54 Swift input hashes and native build flags match the verified preceding
  build. Assembly reuses its native binary; this wave does not claim a fresh
  Swift compilation. The assembled application is signed again as a whole.
- Native binary SHA256:
  `64e2853f31013e7e29e3050954dcf2d556dd307c952e2fb910dcbe555b86b29e`.
- Manifest SHA256:
  `74a0b7636533e5331cec7527f8a302c41fd5c2294e89643fb909dff8fafcc804`.
- Cached offline staging/assembly takes 30.89 seconds. Free disk space is
  15,422,464,000 bytes before and 15,328,325,632 after: net 94,138,368 bytes,
  within the 2 GiB wave budget. Peak usage is unmeasured; concurrent disk changes
  are not attributed to the build.
- Canonical 9.50 and sole generated rollback 9.49 are retained. The inspected
  obsolete 9.48 copy was retired after identity/process/signature checks.
  Small earlier receipts remain; personal profiles, models, caches and useful
  Git history are preserved.
- Signing is ad-hoc arm64/macOS13. Developer ID, notarization and downloaded
  clean-machine distribution are separate gates. Do not edit this signed app.

## Source verification

Frozen backend integration passes 4,967 tests, 22 skips and 682 warnings across
251 selected suites in 192.66 seconds, with no source drift. These are selected
integration suites, not the entire backend repository. They include actual
registered node routes, private receipt migration/reopen, paired credentials,
revocation races and public ToolRunner dispatch with inert effect boundaries.

Full WebUI Vitest passes 1,409 tests across 174 files in 186.13 seconds, with all
367 inventoried inputs unchanged. Focused setup/pairing/provider parity passes
48 tests; the WebUI rebuild and model-picker contract also pass. A first isolated
snapshot run lacked sibling repository inputs and failed; the accepted full run
uses the actual checkout with its required sibling inputs. Earlier failures remain
recorded rather than being counted as passed results.

Full pinned Mac mypy retains 796 existing diagnostics with zero normalized
additions/removals and unchanged input hashes. A new callback annotation defect
found in the first comparison was corrected before the final frozen gate. The
Linux baseline is untouched. Full-core CI Ruff, HUP naming and version coherence
pass. Authored-file whitespace and tracked public-document checks pass; generated
highlight grammar string literals retain their semantic whitespace.

[Paired input contract](PHONE_TRACKED_INPUT_20261009.md) and
[setup catalogue behavior](WEB_SETUP_CATALOG_20261009.md) describe behavior and
remaining limits. No phone-owned approval permission is restored by this wave.

## Actual bundled runtime and app

The bundled backend reaches owned health in 1.90 seconds, returns setup HTTP200
with incomplete setup, and passes lifeline/process-group shutdown and listener
closure with unchanged identity. The unavailable local fixture has no credentials
or fallbacks. Arbitrary backend egress is not instrumented.

A separate probe imports four actual packaged modules using bundled Python. Real
disposable pairing and SQLite exercise a private tracked receipt, one inert
effect, close/reopen exact replay without another effect, foreign-source refusal
and no device identity on the public receipt. Revoked orphan bearer authentication
is denied; device-owned approvals remain unavailable. This is packaged-method
acceptance, not a physical iPhone or real model-selected task.

Computer Use operates a new disposable native profile with saved port 43286 and
no port override. Avatar/name setup and the saved local provider render. Set up
later opens Chat with receipt-only connection readiness. Home renders with source
and health freshness caveats. No calendar lookup or health inference is requested.

Chrome opens one dedicated local tab served by this actual packaged backend.
Welcome and AI Provider setup render. Unsupported adapters have disabled Select
controls; unprobed reachability stays unknown. The explicit local catalogue probe
reports unreachable, separately from inference/tools/voice. Passive Refresh keeps
that negative history. The owned tab is closed afterward. Browser API access was
unavailable, so these checks use native Chrome UI. Welcome Continue activation
through accessibility did not advance; selecting the AI Provider tab did. This
observation does not establish whether pointer activation has the same issue.

Both normal Quits exit 0. Replacement launch restores Home/avatar/name and the
same saved endpoint under a distinct runtime instance; Chat again describes
receipt connectivity without claiming model inference. Final checks find both
hosts and the backend recorded in the retained replacement log absent, with the
listener closed and native/manifest hashes unchanged. The native runtime log is
recreated on relaunch, so it does not retain the first backend PID for that final
PID check. All acceptance-owned native hosts are stopped.

Private receipts include feral-native-9-50-build-20261009/{result,bundle,core}.json,
feral-candidate-9-50-manifest.json, feral-950-backend-startup-evidence.json,
feral-950-packaged-input-r_i0bwbp/receipt.json, feral-oct9-native-950-evidence.json
and both launch/health/exit receipts under feral-oct9-native-950-profile. Profiles
and private receipts are excluded from Git.

## Remaining gates

Persist private phone provenance through every TaskFlow/review and make review
publication, discovery and resolution specific to its verified origin. Existing
session broadcasts are not claimed private. Paired REST/node approvals remain
unavailable; the local operator inbox remains usable. Direct phone TaskFlow
handoff and model-step provenance still require this next contract.

Add explicit job-scoped Chrome permission across executor, selected CDP and
review projection without replacing physical ownership or replaying uncertain
effects. A shared conversation does not grant browser permission.

Custom cloud endpoint binding can differ between runtime settings and catalogue
probes after restart. Native action contrast and legacy runtime-available summary
wording remain UX follow-ups. Local model/speech provisioning, continuous voice,
physical phone/glasses, messaging, commerce, clean installation/update and signed
distribution remain in the release plan. No model inference, account, microphone,
speaker, message, payment or model download was exercised here. Current-source
remote CI is separate from the preceding 18-success/four-skip checkpoint.
