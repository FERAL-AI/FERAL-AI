# Native selectable text repair evidence

This follows the frozen [9.26 populated acceptance](NATIVE_POPULATED_ACCEPTANCE.md). It does not change that candidate's failures or certify a new packaged app. Production changes described here require a separately assembled candidate and repeated actual native acceptance.

## Confirmed failure

On this Mac (macOS 27.0.1, build 26A434), immutable 2026.9.26/build2026100117 crashed during accessibility inspection. The original executable SHA256 remains `3dbfbbd63af7206bd99d81065e6f8a3f239bf0eb4bd3e31584f6c2cbdae7a0e3`.

- Original PID 48746: `/Users/mahmoudomar/Library/Logs/DiagnosticReports/feral-native-2026-10-02-083330.ips`, capture 08:32:27; SIGSEGV 11, excessive recursion / stack exhaustion. Repeated SwiftUI `AccessibilityNode.resolvedRole`, `labelExposedAs`, `labelsToResolve`, `accessibilityLabel` and AppKit accessibility attribute resolution.
- Relaunched PID 57319: owned observer recorded exit -11 after expanding Conversation connection and AX inspection. Subsequent report `feral-native-2026-10-02-084140.ips` exists.
- A standalone native probe without backend, model or personal profile also reproduced the failure: explicit-label mode PID 64040, exit -11, report `feral-ax-probe-2026-10-02-085533.ips`. Its first frames contain the same SwiftUI label-resolution chain. Adding `.accessibilityLabel` to selectable SwiftUI Text is therefore insufficient.

## Actual A/B work

The dedicated probe uses a NavigationSplitView, rich/short synthetic conversations, LazyVStack, connection and metadata disclosures, and a composer. These are actual CUA/native interactions, separate from mocked fixture tests.

| Mode | Actual result |
| --- | --- |
| Simple SwiftUI selectable disclosure, PID 61775 | Expanded and AX inspection succeeded; normal quit exit 0. Insufficient alone to reproduce the failure. |
| Rich SwiftUI selectable layout, PID 63148 | Rich→short and connection expansion remained AX-readable, but screenshot body was blank; normal quit exit 0. |
| Plain SwiftUI text, PID 63359 | Same bounded switch/disclosure sequence survived; screenshot also blank; normal quit exit 0. |
| Explicit-label SwiftUI selectable text, PID 64040 | SIGSEGV 11 during initial inspection; matching AX recursion report. |
| AppKit wrapping selectable field, PID 65377 | Connection and metadata expansion, rich→short, AX inspection and visible screenshot succeeded; normal quit exit 0. |
| Shared production AppKit component, PID 68022 | Rich→short, expanded connection, actual text selection and Cmd-C/Cmd-V into synthetic composer returned the exact selected synthetic message; normal quit exit 0. |
| Current component with Markdown/link fixture, PID 72226 | Actual AX and screenshot show bold, italic, inline mono, fenced code, wrapping text. Copy code pasted exact `let synthetic = true` into composer. Clicking public Markdown link opened Example Domain in Chrome, verified native URL `example.com/`. Both owned public test tabs were closed; normal probe quit exit 0. |

Blank screenshots remain an unresolved capture/render discrepancy: both selectable and plain modes showed it, and one subsequent capture returned ScreenCaptureKit error -3811. They do not independently prove an app layout defect or establish that the AppKit change fixes all blank views.

Local actual CUA PNGs (not committed):

- `/private/tmp/feral-native-populated-20261002/ax-selectable-blank.png`
- `/private/tmp/feral-native-populated-20261002/ax-plain-after-switch.png`
- `/private/tmp/feral-native-populated-20261002/ax-appkit-copy.png`
- `/private/tmp/feral-native-populated-20261002/ax-appkit-markdown.png`

## Bounded production change

- `NativeRichText.swift`: shared `NativeSelectableText` uses selectable, noneditable AppKit NSTextField with dynamic wrap measurement, horizontal unwrapped code, explicit rendered accessibility label, and Copy text context menu. Markdown conversion retains bold/italic/inline-code fonts and link attributes; allowsEditingTextAttributes supports actual clickable native links. Existing Copy code remains.
- `NativeViews.swift`: connection recovery status, message metadata and delivery failure use the shared control.
- `NativeRichChatFeature.swift`: user text, recorded/live reasoning, event/tool inspection and folder review displays use the shared control. Secondary captions preserve secondary label color.
- The immutable 9.26 packaged app was not modified, rebuilt or restaged.

## Reproducible checks

From `ASOS/desktop-native`:

```sh
bash test_features.sh RichChat Conversation SessionRecovery
bash build.sh --typecheck
```

Both completed exit 0. Named fixture groups: 15 RichChat groups, 35 conversation assertions, 42 session recovery assertions. Linked checks: 20 mocked NativeModel groups, 39 desktop assertions, 5 error presentation assertions. Existing Swift 5 async NSLock warnings in test source remain. These are source/fixture checks, not a packaged GUI pass.

From `ASOS`, compile `tests/NativeSelectableTextTests.swift` with `NativeRichText.swift`, Swift 5, arm64 macOS 13 target. The executable passed read-only selection, narrow/wide wrap sizing, unwrapped code width, secondary color and Markdown bold/italic/link retention checks. The probe compiles `acceptance/SelectableTextAccessibilityProbe.swift` with the same production component. `acceptance/ax_probe_launcher.py` packages/launches only a disposable named probe and records its exit. It has no backend or provider access.

## Remaining coverage

After the narrow patch, direct inventory found **172** remaining `textSelection(.enabled)` expression occurrences across **28** production files. Multiple expressions can share one line; line counts undercount this inventory. Remaining paths include Oversight, Providers, Security, Operations, Memory, Devices/Hardware, skills/apps, voice, identity, reviews and other NativeViews panels. They require migration or actual evidence that their AX paths are safe. The chat mitigation is not an app-wide crash-fix claim.

New packaged acceptance must repeat the original crash path, conversation persistence/switching, message copy/link behavior, cancellation, harmless review denial/allow outcomes and clean quit/child cleanup. Backend fallback and context failures recorded in the original report remain separate and must be verified against the new integrated backend. No personal account, payment, messaging or OS permission flow was exercised here.

## Parent fixture registration and integration

Parent added SelectableText to the default `test_features.sh` inventory so the
new component assertions run in native CI. The first registered invocation
failed in macOS Bash's `set -u` handling of an empty source array. Appending the
main/test source before expanding the array fixed that runner defect. The final
registered `bash test_features.sh SelectableText` exited0 and passed the component,
linked20 model groups, desktop39 and error5 checks. This source/fixture result
does not replace rebuilt-app GUI acceptance. Production candidate version is
2026.9.27/build2026100201, pending assembly from the committed combined source.
