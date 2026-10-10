# Native 9.53: phone recovery and Chrome consent checks

October 10, 2026. Version 2026.9.53/build2026101002 contains exact runtime
`fd64e38f8893025c5f2c51550f4882139c34e3ca`. This source is published on the
PR310 feature branch. The artifact passed package checks but failed expanded
Browser layout acceptance; it is not an accepted replacement for 9.52.

## Verified scope

Frozen affected-path integration passes 1,147 tests/four skips across 64 suites
in 71.89 seconds. All 1,506 retained core/SDK inputs match this checkout. Core CI
Ruff passes; configured Mac typing retains 788 baseline diagnostics with zero
additions/removals. Native typechecking and 164 focused browser checks pass.
Earlier SDK snapshot omissions and a nullable publisher typing diagnostic remain
in the private evidence history; final integration corrected both.

Three independent current-source spotchecks verify that a runtime-issued Chrome
review token rejects a forged conversation (403), accepts its owner (200), and
refuses reuse (409). A task without Chrome consent saves its exact next call and
waits. Grant confirmation dispatches nothing; explicit Resume continues the
saved call. A mutation still requires exact action approval and runs once; a
saved read also runs once without another provider call. These use inert model,
wire and effect boundaries, not a personal Chrome session or real inference.

The package has 504 Python and 70 WebUI files matching committed source, fresh
optimized compilation of 54 Swift inputs, contained Python/OpenCode dependencies
and strict deep ad-hoc signatures. Native SHA256:
`8b5af254cae0797a25c9691299297e31d60159c4e3d8fabd121a27580c4bef54`.
Manifest SHA256:
`bf86188cbbf606d668b767cc0050acceeb9fca26dafe5117be66fdb0342112dd`.
Offline build elapsed 163.03 seconds. Free space was 13,078,167,552 bytes before
and 12,991,447,040 after, within the 2 GiB net-growth budget. Concurrent disk
changes and peak use are not measured. Generated 9.52 is the retained rollback.

## Actual native GUI and failure

The exact signed application starts with a disposable profile, isolated native
preferences, an unavailable local provider and no model download. Avatar setup,
provider setup, Set up later, receipt-ready Chat and local-backend connection
work. Home renders its local state. Collapsed Browser renders normally and
exposes the new one-task-step permission disclosure.

Expanding that disclosure with no attached Chrome or eligible tasks reproducibly
moves visible controls and the sidebar above the window. Accessibility retains
their descriptions, but a Home row click fails as offscreen. Native Navigate >
Home restores usable rendering; returning to collapsed Browser also works. This
is a layout failure, not evidence of a backend crash or a permission bypass.
Normal Command-Q exits the native process with code 0. The sandbox refused the
listener-closure socket probe; listener closure is not claimed by this GUI run.

The next candidate must bound the preview region and keep expanded controls
scrollable, then repeat the actual expanded-screen check. Do not edit this signed
artifact in place or relabel accessibility-only observations as visual success.

Remote native-contracts also fails the existing NativeSelectableTextTests safety
check: three new Browser metadata rows use unsafe SwiftUI text selection. The
same failure is reproduced locally; the correction uses the established native
selectable-text component. Intermediate 9.54 packages the layout-only correction
at fee9e31bf, with fresh compilation and matching package checks, but is not GUI
accepted. The final 9.55 batch includes both corrections and repeats GUI checks.
The full local public-doc checker separately reports three preexisting model-number references in ignored local notes. It is not a
passing unfiltered local gate; those notes are not changed or published. The
tracked-publication check and final artifact are recorded in the 9.55 ledger.

Private evidence includes feral-oct10-root-integration/combined-evidence,
feral-oct10-root-browser-tests, feral-native-9-53-build-20261010,
feral-candidate-9-53-manifest.json and feral-native-9-53-gui-20261010. Private
profiles, tokens and receipts remain excluded from Git.

## Client and remaining boundaries

Independent source review confirms iOS commits f513f8d, ab516b5, adaaf69 and
aa77855 implement persisted canonical input IDs, read-only reconnect queries,
exact review cards and separate renewal. Retained 539 Foundation assertions and
a compiled Swift/registered ASGI/pairing/SQLite round trip pass: one stored task,
one inert model call and zero prompt resends. These commits are local and unpushed
at this review; the updated phone app is uninstalled. Approval uncertainty is
in memory; durable inputs/receipts survive restart and cards are fetched again.

Physical phone/provider/Chrome/audio/glasses operation, positive native Chrome
grant UI, Messages, checkout, continuous voice, provisioning and signed clean
distribution remain separate gates. No account, microphone, message, payment or
physical-device action is performed by this acceptance.

[Contract](PHONE_TASK_CHROME_CONTRACT_20261010.md),
[checkpoint](WORK_STATE.md), [iOS handoff](IOS_AGENT_HANDOFF.md).
