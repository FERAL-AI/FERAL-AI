# Native accessibility follow-up

October 2, 2026. Source repair follows the confirmed 9.27 acceptance failure.
The old candidate and [9.27 evidence](NATIVE_9_27_ACCEPTANCE.md) remain immutable.
No backend, SDK, APIModel, app assembly or default user preferences were changed
by this repair. Actual next packaged candidate acceptance is still required.

## Reproduction before migration

The 9.27 crash occurred when New conversation switched from an inspected saved
conversation while a supervisor_paused diagnostic remained in the global error
banner. Source inspection showed that newConversation clears chatError but not
model.error. Thus the existing selectable global error banner remains exposed.
This is a correlated view path, not proof that retaining an error itself causes
the crash.

`acceptance/NewConversationAccessibilityProbe.swift` isolates the navigation,
error Label and selectable exact diagnostic with synthetic strings only. It has
no backend, model, account or network request. Original mode survived the New
conversation transition; opening Error details and reading AX then crashed.

- Original probe PID1052, held observer exit-11. Apple report
  `feral-new-conversation-probe-2026-10-02-101835.ips`, capture
  `2026-10-02 10:17:48.8292 -0700`, EXC_BAD_ACCESS/SIGSEGV,
  KERN_PROTECTION_FAILURE in the stack guard region. Main-thread frames repeat
  SwiftUI AccessibilityNode.accessibilityLabel, labelsToResolve, labelExposedAs,
  and AppKit accessibility override calls, matching the packaged crash class.
- First AppKit probe PID2017 exited-9 before UI. Its separate Apple report
  `feral-new-conversation-probe-2026-10-02-101757.ips` explicitly says
  SIGKILL (Code Signature Invalid), Taskgated Invalid Signature. It is a temporary
  probe signing failure, not evidence about AppKit selection. Launcher now signs
  and verifies its bounded temporary app after copying its executable/plist.
- Signed AppKit probe PID2535 passed actual New conversation and Error details
  disclosure AX reads. Selecting exact supervisor_paused text, Copy and paste
  into the probe composer reproduced that exact value. Normal Cmd-Q exit0.
  Screenshot: `/private/tmp/feral-native-populated-20261002/new-conversation-appkit-copy.png`.
- Final styled component probe PID14818 repeated that path after migration with
  the production-style callout/red/two-line summary and caption/monospaced exact
  detail. AX exposed Error: supervisor_paused and Exact error details:
  supervisor_paused, selection copied the exact value, and normal exit0 was
  observed. Screenshot visually checked:
  `/private/tmp/feral-native-populated-20261002/new-conversation-final-styled-copy.png`.
- Sanitized original stack receipt:
  `/private/tmp/feral-native-populated-20261002/new-conversation-original-stack.json`.

## Repair and selection inventory

The production shared NativeSelectableText component uses read-only selectable
NSTextField with explicit accessibility values and Copy text context menu.
NativeErrorBanner now uses an icon plus that AppKit text instead of selectable
SwiftUI Label; exact error details use the same component. Errors are not hidden
to avoid the reproduction. APIModel.swift is untouched.

All **172** remaining SwiftUI textSelection(.enabled) expressions across **28**
production files were replaced. Four selectable Label notices retain their icons
and readable text as AppKit fields. Two container-scoped selections became
individually selectable child fields. The remaining expressions were text displays.
New explicit AppKit font styles preserve caption/callout/title/monospaced choices;
component color, line limits and accessibility labels are applied before layout
wrappers. JSON, diagnostics and dynamic strings remain verbatim. Existing rich
Markdown spans and links use the existing attributed-text path.

| Original production file | Expressions migrated |
| --- | ---: |
| NativeAgentFeature | 6 |
| NativeAmbientFeature | 10 |
| NativeAppConfirmationFeature | 4 |
| NativeAppSurfaceFeature | 5 |
| NativeCapabilitiesFeature | 11 |
| NativeChatToolsFeature | 11 |
| NativeConnectionsFeature | 5 |
| NativeConversationFeature | 5 |
| NativeDesktopExperience | 4 |
| NativeHardwareFeature | 7 |
| NativeHealthHistoryFeature | 3 |
| NativeIdentityFeature | 8 |
| NativeIntegrationFeature | 2 |
| NativeKnowledgeFeature | 8 |
| NativeMemoryContextFeature | 6 |
| NativeMemoryFeature | 8 |
| NativeOnboardingSetupFeature | 3 |
| NativeOperationsFeature | 13 |
| NativeOversightFeature | 8 |
| NativeProviderRoutingFeature | 8 |
| NativeProvidersFeature | 6 |
| NativeReviewSummary | 2 |
| NativeSecurityFeature | 5 |
| NativeVaultFeature | 4 |
| NativeViews | 9 |
| NativeVoiceConfigurationFeature | 5 |
| NativeVoiceFeature | 1 |
| NativeWorkflowFeature | 5 |

Current production inventory has **zero** textSelection(.enabled) expressions.
The intentionally unsafe original-mode acceptance probes retain their reproducer.
The new component fixture includes a guard against reintroducing that modifier
in production Swift files, alongside direct read-only selection, wrapping,
monospaced sizing, title/caption fonts, line limits, exact AX label and verbatim
diagnostic tests. Existing Markdown bold/italic/link attribute tests remain.

## Verification state

- Final frozen production `bash build.sh --typecheck`: **passed, exit0**.
  An earlier run was invalidated by an edit during compilation and is not counted.
- Final frozen selected native fixture runner: **passed, exit0**. SelectableText,
  Conversation (35 assertions), RichChat (15 groups), ChatTools (10 groups),
  Attachment (23 assertions), SessionRecovery (42 assertions), Oversight
  (10 groups), Security (42 assertions), Operations (9 groups), RuntimeHealth
  (62 assertions), linked model (20 groups), desktop experience (39 assertions)
  and error presentation (5 assertions) completed. Existing Swift5 async NSLock
  warnings remain in mock fixtures. No source edits during this final run.
  Full local log:
  `/private/tmp/feral-native-populated-20261002/native-accessibility-followup-fixtures.log`.
- Initial selected fixture run passed its component/feature and linked model
  groups, then failed compiling standalone DesktopExperience because the harness
  did not include its new shared NativeRichText dependency. Coordinator corrected
  the compile source list. The initial exit1 remains a harness failure; the final
  runner is a separate result, not a relabeling of that failed run.
- Scoped production inventory check and git diff whitespace check passed.
- New launcher Python syntax and pinned Ruff checks passed.
- Actual A/B probe selection, copy and clean quit above are GUI evidence.
  Feature HTTP mocks and component tests do not certify every page visually.

Source/probe/doc are frozen for coordinator integration. No probe or native app
process remains running. The old9.27 candidate was not rebuilt or restaged.

Commands from desktop-native:

```bash
bash build.sh --typecheck
bash test_features.sh SelectableText Conversation RichChat ChatTools Attachment SessionRecovery Oversight Security Operations RuntimeHealth
xcrun swiftc -swift-version 5 -target arm64-apple-macosx13.0 \
  -module-cache-path /private/tmp/theora-native-swift-cache -parse-as-library \
  NativeRichText.swift NativeErrorPresentation.swift \
  acceptance/NewConversationAccessibilityProbe.swift \
  -o /private/tmp/feral-native-populated-20261002/feral-new-conversation-probe
```

From ASOS, launch only the bounded synthetic probe:

```bash
python3 desktop-native/acceptance/new_conversation_ax_launcher.py original
python3 desktop-native/acceptance/new_conversation_ax_launcher.py appkit
```

Both modes are held until actual process exit is recorded. Original-mode crashes
are expected reproducer evidence; do not use that mode with personal data.

## Next packaged acceptance gates

After coordinator source commit, exact staging/audit and READY for 9.28, verify
actual New conversation following pause/resume with a retained error, exact
error disclosure, rich/short switching, recovered metadata, selection/Copy/link,
review Allow/Deny outcomes and quit/child cleanup in the same disposable profile.
Read-only expanded representative migrated pages must be inspected for sizing,
font/color and accessibility regressions. No general crash-free or app-wide
visual claim is made from the small A/B probe. Broader operational acceptance,
model budget behavior, signing/notarization, installability, Linux and physical
devices remain independent release gates.
