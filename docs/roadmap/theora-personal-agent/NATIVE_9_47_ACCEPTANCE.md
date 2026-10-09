# Native 9.47: Home and stable endpoint acceptance

October 9, 2026. Canonical application: desktop-native/build/FERAL Native
Preview.app. Version 2026.9.47, build 2026100901, exact runtime source
`4bc4128cc74c85f6eaba08cf613b7730fc60ab76` on the release-foundation branch.
Source behavior and fixture scope: [repair evidence](END_TO_END_REPAIR_20261009.md).

## Artifact and retention

- Fresh optimized arm64/macOS13 native compilation uses 54 Swift inputs.
- All 499 packaged production Python files match committed source. All 70
  tracked WebUI files, including rebuilt Home HTML/JavaScript, match separately.
- Contained Python/SDK/OpenCode, supported desktop imports and strict deep ad-hoc
  signature checks pass. This is not Developer ID/notarized distribution.
- Native binary SHA256:
  `92801661ed3c926a607ed368c69d34fa37bfc8ce682c5dfe7eb7aa98dc425428`.
- Manifest SHA256:
  `d121794fd2dcc9e872b6f63116944773e8e389a8e19d6a771ca9e930ff9cd8e6`.
- Builder passed in 192.09 seconds. Free space: 19,622,211,584 bytes before,
  18,457,985,024 after; net change 1,164,226,560 bytes within the 2 GiB budget.
  Peak disk usage is not measured; unrelated concurrent disk activity is not
  attributed to the bundle. No personal model/profile/Git cleanup occurred.
- 9.46 is the sole generated rollback. The inspected unused 9.45 app was retired
  only after owner/version/hash/signature/process checks. Its evidence remains.
  If a future assembly fails after moving canonical, inspect the failed artifact
  and restore the verified rollback explicitly; do not launch a partial bundle.

## Executed packaged and GUI checks

The isolated bundled backend passed health in 2.12 seconds, setup HTTP200 with
incomplete setup, exact owned process-group observation, lifeline EOF shutdown
and listener closure. Artifact identity stayed unchanged. Its fixture has an
unavailable local provider, no credentials or fallback. The harness initiates
only loopback probes; arbitrary backend egress was not instrumented.

The actual signed candidate was then launched twice with a separate disposable
profile, saved network.port=43282 and neither FERAL_PORT nor FERAL_BRAIN_PORT.
Computer Use observed avatar selection first, then native setup with the saved
unconfigured provider marked not probed. Set up later opened the existing app;
Chat and Home rendered, and Home showed sourced-state caveats rather than
invented health readings. No model query, connection probe or reviewable external
lookup was invoked.

Both launches served the rebuilt WebUI entry from the same saved endpoint and
returned distinct instance headers. After normal native Quit, each host exited
0. The replacement retained Home selection and showed Local agent connected.
Final process/listener verification found both hosts and the observed backend
absent, listener closed and unchanged native/manifest hashes. These observations
verify native lifecycle, not inference readiness. The Chat readiness label does
not establish successful inference from the deliberately unavailable provider.

Small private receipts: feral-native-9-47-build-20261009/{result,bundle,core}.json,
feral-candidate-9-47-manifest.json, feral-947-backend-startup-evidence.json and
feral-oct9-native-saved-port-evidence.json. The fresh-profile host/health/exit
receipts remain under feral-oct9-native-saved-port-profile. Test processes were
stopped; generated private data and evidence are excluded from Git.

## Open acceptance

Actual Theora phone/glasses connectivity and shared browser-task handoff remain
unverified. Chrome ownership remains session scoped; a Mac-chat attachment is not
implicitly the phone session's browser. No actual iMessage transport, delivery,
read receipts, real accounts, audio, commerce or model provisioning was exercised.
Playwright phone-width geometry remains unexecuted because its browser binary
was absent. Clean-machine installation, updates, Developer ID/notarization,
Intel/Linux and physical iOS acceptance remain in [release readiness](RELEASE_READINESS.md).
