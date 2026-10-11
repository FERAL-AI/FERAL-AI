# Native 9.51: phone task ownership and cloud connection acceptance

October 9, 2026. Canonical application: desktop-native/build/FERAL Native
Preview.app. Version 2026.9.51, build 2026100905, exact runtime source
`901c72f1075eab7ca59550e35142be6cecb524ff` on the feature branch for draft PR310.
Later documentation commits do not alter this immutable runtime.

## Behavior and source checks

Private authenticated phone origin now survives direct/reviewed TaskFlow creation
and backing-skill status/list discovery. Native shared-session routing ownership
is retained. Stored provenance is not permission to approve a review or use Chrome.
[Phone task contract](PHONE_TASKFLOW_ORIGIN_20261009.md).

Supported active cloud catalogue connections now match the actual resolved runtime
endpoint and credential after startup and Settings/key activation. Publication is
identity-fenced; failed activation preserves the preceding catalogue connection.
No passive HTTP discovery is added. [Cloud contract](ACTIVE_CLOUD_CATALOG_20261009.md).

Final frozen affected-path integration passes 685 tests, 200 warnings across 36
suites in 39.97 seconds, with zero input drift. Full configured Mac mypy has 788
existing diagnostics: zero added and eight removed versus 9.50. Full-core CI Ruff
and authored whitespace pass. Earlier intermediate test results and the initial
typing failure remain separate evidence, not final passing results.

Native Swift and WebUI sources are unchanged. Their preceding 9.50 GUI and full
WebUI evidence is retained; no new 9.51 GUI, full WebUI run or fresh Swift compile
is claimed. These affected-path checks are not the entire backend repository.

## Exact artifact and resource use

- All 501 packaged production Python files and 70 tracked WebUI files match the
  committed source. Runtime/import, dependency containment and strict deep ad-hoc
  signature audits pass.
- All 54 native Swift inputs and build flags match the verified preceding build.
  Assembly reuses that native output, compiled from source 58ad0a025, and signs
  the application again. Reused pre-signing binary SHA256 is
  `64e2853f31013e7e29e3050954dcf2d556dd307c952e2fb910dcbe555b86b29e`.
- Final signed native binary SHA256 is
  `02c418fc8722e596f1cf01e99aa6f07acdb39d8dd06001314387144dd0437797`.
  Manifest SHA256 is
  `3e95e18f561bf8c175bb28126632891f9391e2d8074befd2a3eedf31f4d5852b`.
- Offline cached assembly takes 39.25 seconds. Free disk space is 15,332,806,656
  bytes before and 15,255,949,312 after: net 76,857,344 bytes, within the 2 GiB
  batch budget. Peak usage is unmeasured; concurrent disk changes are not
  attributed to the build.
- Canonical 9.51 and sole generated rollback 9.50 are retained. The inspected
  obsolete 9.49 copy was retired after signature, identity and process checks.
  Personal data, models, caches and useful Git history are preserved.
- Signing remains ad-hoc arm64/macOS13. Developer ID, notarization, downloaded
  clean-machine installation and release publication remain separate gates.

## Actual packaged runtime

Bundled Python imports six actual packaged modules. Disposable real pairing and
SQLite exercise private tracked receipt replay after reopen, one inert effect,
foreign-source refusal, public identity exclusion and revoked bearer rejection.
Actual TaskFlow methods preserve private ownership and exact deduplication across
reopen; a foreign principal cannot discover that flow. The direct creation guard
is a fixture, not a physical phone or registered HUP end-to-end session.
The actual cloud catalogue method binds an explicit fixture endpoint/key and
retains its adapter on repeat, without requesting HTTP. Device approvals remain
unavailable. No model, account, job execution or physical device is exercised.

The actual bundled backend reaches owned health in 2.08 seconds, returns setup
HTTP200 with incomplete setup, and exits with SIGTERM after its stdin lifeline
closes. The local listener closes; native/manifest identity remains unchanged.
This uses a disposable unavailable local provider with no keys or fallbacks.
Probe requests are loopback only; arbitrary backend egress is not instrumented.
No native GUI, microphone, speaker, payment or message is exercised.

The first lifecycle invocation refused an incorrect expected hash before launch:
the pre-signing hash was supplied instead of the new signed output hash. The next
sandboxed invocation verified identity but lacked process/listener permissions.
Both failure receipts are retained; the accepted run uses the exact final manifest
and signed hash with the required test permissions.

Private evidence includes feral-native-9-51-build-20261009/{result,bundle,core}.json,
feral-candidate-9-51-manifest.json, feral-951-packaged-input-gq3nwa19/receipt.json,
feral-951-backend-startup-evidence.json, feral-phone-cloud-gate-kza9vtq3/receipt.json
and feral-phone-cloud-typing-0tq36qyp/comparison.json. Private profiles and receipts
are excluded from Git.

## Remaining acceptance

Next is private phone review publication/discovery/resolution and later model-step
renewal, followed by coordinated job-scoped Chrome delegation. Paired tool-review
resolution remains unavailable; the Mac operator inbox retains existing behavior.
Real configured inference, physical phone/glasses/browser tasks and audio remain
unverified. Local provisioning, continuous voice, messaging, commerce, multi-user
collaboration, clean installation/update and signed distribution stay in the
full release plan. [Current checkpoint](WORK_STATE.md),
[full request coverage](REQUEST_COVERAGE.md).
