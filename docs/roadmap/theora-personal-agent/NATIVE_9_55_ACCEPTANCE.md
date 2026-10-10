# Native 9.55: bounded Chrome task controls and phone recovery

October 10, 2026. Canonical application: desktop-native/build/FERAL Native
Preview.app. Version 2026.9.55/build2026101004 contains exact runtime source
`3717ad646a676301879933bba9d042dc8a1f2c41` on the PR310 feature branch.
Later checkpoint documentation does not change this immutable runtime.

## Behavior and source verification

Phone receipt recovery and exact-step Chrome consent are retained from published
fd64e38f8. Authenticated reconnect queries recorded receipts without resending the
goal. A task lacking Chrome consent saves its exact next call and waits. Reviewed
browser permission is short-lived and bound to the actual current step, owner,
tab and tools. Grant confirmation dispatches nothing; explicit Workflows Resume
continues the saved call. Mutations still require separate exact action approval.
Private review renewal and unknown-effect protections remain intact.
[Contract](PHONE_TASK_CHROME_CONTRACT_20261010.md).

Browser controls now scroll within a bounded detail viewport. The live/empty
preview has finite height, and an empty tool inventory creates no scroll region.
Three metadata rows use the established AppKit selectable-text component.
Full native typechecking, a fresh focused Browser compilation, 164 checks and
the native selector safety test pass after both corrections. No cursor coordinate
mapping or authorization logic changes in these native corrections.

Retained backend integration passes 1,147 tests/four skips across 64 suites.
All 1,506 recorded inputs match current core/SDK source; none changed in the
layout/text corrections. Core CI Ruff passes. Configured Mac typing retains 788
baseline diagnostics with zero additions/removals. Three independent current-
source spotchecks verify forged-owner/reused-token refusal, exact saved-call
Resume, separate mutation approval, and one effect without another provider call.
These use isolated pairing, SQLite and inert model/effect boundaries.

The unchanged actual bundled-method probe verifies same-device credential
rotation, reopen, explicit renewal, foreign/old/duplicate card refusal, one inert
effect and receipt-based continuation without replay. Its ten module hashes
match current source and the exact final package. It is retained evidence from
9.53, not a new physical phone/provider/browser run.

## Exact package, retention and resources

- All 504 Python and 70 WebUI files match committed source. Fresh optimized
  compilation covers 54 Swift inputs. Runtime/import, dependency containment
  and strict deep ad-hoc signature checks pass.
- Native SHA256:
  `09268024d3522ba16075a93eaf38d838860ac7b5799bf0e01fb20b8b53819f44`.
  Manifest SHA256:
  `9339c86f40508a1304b1c309ea732ec0129ca939cbd1ab3ca227340f27ccc37b`.
- Offline build elapsed 172.91 seconds. Free space was 12,970,913,792 bytes
  before and 12,893,335,552 after, within the 2 GiB net-growth budget. Peak use
  and concurrent disk changes are unmeasured.
- Canonical 9.55 and known-good generated rollback 9.52 remain. Failed-layout
  9.53 and intermediate layout-only 9.54 are retired after exact identity,
  ownership, strict signature and process checks. Their small manifests/logs
  remain. Personal data, models, caches and useful Git history are preserved.
- Signing is ad-hoc arm64/macOS13. Developer ID/notarization and fresh downloaded
  installation/update acceptance remain distribution gates.

## Actual native GUI and lifecycle

The exact package starts with a disposable profile and isolated native
preferences, an unavailable local provider and no model download. Avatar/provider
setup, Set up later and receipt-ready Chat work. Expanded Browser controls render
visibly without displacing the sidebar. Expiration changes from five minutes to
one minute without issuing a grant. Scrolling the Browser detail does not move
the sidebar; Home remains clickable and renders actual local state. Returning
to Browser, expanding again and collapsing work. Missing Chrome/task review
controls correctly remain disabled. The test window has no overflowing populated
tool list; small-window and populated positive-grant GUI acceptance are not claimed.

Actual loopback health returns HTTP200 while the native process is alive. Normal
Command-Q exits0, stops the isolated-profile processes and closes the listener.
The native hash is unchanged afterward. No personal Chrome attachment, inference,
account, microphone, message, purchase or physical device is exercised.

Private evidence includes feral-native-9-55-build-20261010/{result,bundle,core}.json,
feral-candidate-9-55-manifest.json, feral-native-9-55-gui-20261010,
feral-browser-selector-fix-receipt-20261010.json,
feral-oct10-root-integration/combined-evidence and
feral-953-packaged-review-xtyp4zao/receipt.json. Private profiles and credentials
are excluded from Git.

## Failure history, client status and next acceptance

Actual expanded Browser failure in 9.53 and the remote/local selector-test
failure remain documented in [the earlier ledger](NATIVE_9_53_ACCEPTANCE.md).
The selector failure is reproduced before correction and passes afterward.
The full local public-doc checker reports three references in preexisting ignored
local notes. The same checker restricted to 135 tracked publication targets
passes with zero violations; the ignored notes are neither changed nor published.
New remote CI is separate from these local checks; no all-green CI claim is made.

Independent iOS review confirms local aa77855 contains persisted canonical input
IDs, read-only reconnect status queries, exact cards and separate renewal. The
retained 539 Foundation assertions and compiled Swift/registered ASGI/pairing/
SQLite round trip pass: one stored task, one inert model call, zero prompt resend.
The remote iOS branch remains at 7d51197 during review and the updated app is
uninstalled. Durable task inputs/receipts survive restart; approval uncertainty
is in memory and cards are fetched again. iOS publication/installation belongs
to its agent, and no iOS source is edited here.

Next: install that client, confirm the chosen model and phone-to-Mac network,
then verify authenticated phone task intake, Chrome consent, explicit Resume,
separate action review, one observed result and reconnect recovery on the actual
phone. Positive native grant UI, real inference, desktop/Chrome operation, audio,
glasses, provisioning, Messages, checkout, continuous voice, shared collaboration
and clean distribution remain in the complete product plan. Initial volatile
pre-creation proposals are not reconstructible by model-step renewal. No merge
or production release is performed.

[Checkpoint](WORK_STATE.md), [complete coverage](REQUEST_COVERAGE.md),
[iOS handoff](IOS_AGENT_HANDOFF.md).
