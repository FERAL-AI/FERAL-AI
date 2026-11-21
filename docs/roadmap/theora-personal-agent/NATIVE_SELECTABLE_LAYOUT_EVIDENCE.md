# Selectable text layout investigation

Source repair frozen after bounded checks and synthetic GUI acceptance. Original
9.28 bundle remains unchanged and failed. Full packaged acceptance of the next
candidate is still required; the small probes did not reproduce its full crash.

Actual9.28 PID27774 crashed SIGTRAP at11:14:09.406-0700 after selecting Permissions
and cost. IPS asiBacktraces show NSWindow._postWindowNeedsUpdateConstraints through
SwiftUI invalidation and NSTextField content-size constraints. Scoped OS log
11:14:08.082 identifies NativeSelectableTextField.fittingSize(width:field:)
calling NSTextField.invalidateIntrinsicContentSize during sizeThatFits. Window
constraint update count197/198/199 exceeded its193 limit. OS exception reports
NSGenericException with redacted reason; privacy redaction was preserved.

Source inspection: fittingSize wrote live field.preferredMaxLayoutWidth while
measuring. Repair measures a copied NSTextFieldCell without changing the live
field, guards unchanged configuration values, and handles non-finite proposals.
A private field flag clears prior attributed links when reused as plain text,
including when its visible string is unchanged. Markdown spans/links, exact AX
labels, fonts/colors, line limits and native text selection remain supported.

New synthetic aids: SelectableLayoutProbe.swift, SelectableMeasurementProbe.swift
and selectable_layout_launcher.py.
They use no backend, account, default profile or private data, and a distinct
ad-hoc-signed disposable app. Original and measured-copy variants share the
Permissions and cost tab, workspace-path layout, wrapping Unicode and a harmless
copy-verification field. The security-original launcher mode renders the actual
NativeSecurityFeatureView against five synthetic GET-only loopback fixtures.
Its name selects the production renderer, not a particular committed source;
compile source identity must be recorded when interpreting its result.

## Verification performed

- The original production fittingSize measured the same synthetic field at240
  and720width. SelectableMeasurementProbe's purity assertion failed exit133,
  explicitly reporting that sizeThatFits changed the live intrinsic-size input.
  The same probe after repair passed exit0: preferred width stayed0/0/0;
  narrow/wide heights340/102. No attached view frame/string/attributes mutation.
- Added real regression assertions in NativeSelectableTextTests for repeated
  widths, unchanged preferred width/frame/attributed text, nil/infinity/NaN/zero/
  negative proposals, long emoji/Arabic/CJK/combining Unicode wrapping, and
  rich-to-plain reuse clearing prior link attributes. Existing code-width,
  bold/italic/link/style/lineLimit/AX assertions passed.
- Original small custom probe44513 and actual Security fixture probe45778 survived
  tab/width/section transitions and normal Quit exit0. They did not reproduce the
  full9.28 geometry/state crash. Keep that limit visible.
- Fixed actual Security probe48677 passed Credential vault > Permissions and cost,
  Folders > Cost caps > Folders, AX and screenshot; normal Quit exit0. No grant,
  policy or spending-cap submission. Initial right-click then CmdC did not verify
  copied text; its unsubmitted input was cleared without saving. Do not call that
  attempt a Copy pass.
- Fixed custom probe49622 and final lifecycle subclass probe51306 passed wide>
  narrow Unicode layout, full AX and normal Quit exit0. Actual triple-left-click
  selected the exact synthetic project path; CmdC, focusing Copy verification and
  CmdV produced the exact path in AX. Output checking was limited to the synthetic
  match. Screenshots show wrapping and copied path. Actual clickable Markdown
  links were not retested in this wave; attribute retention is fixture evidence.
- Final focused runner exited0: SelectableText, linked native25groups,
  desktop39assertions and error5assertions. Linked evidence is provisional because
  other workers changed different production files during this wave; parent will
  rerun with all source owners frozen. Do not reuse it as the integrated gate.
- First production typecheck failed with “NativeCapabilitiesFeature.swift was
  modified during the build”; a later typecheck exited0. Both are provisional
  until the parent's final all-source freeze. New Python launcher py_compile/Ruff
  and scoped whitespace check exited0.

Commands used from desktop-native: xcrun swiftc -parse-as-library NativeRichText.swift
with SelectableMeasurementProbe.swift or NativeSecurityFeature.swift plus
SelectableLayoutProbe.swift, framework SwiftUI/AppKit; bash test_features.sh
SelectableText; bash build.sh --typecheck. Launchers ran from ASOS with original
and security-original modes. All probe apps are ad-hoc signed in the disposable
root and have no real FERAL backend. The fixture server is loopback and stops at
probe exit. No actual accounts, purchases, messages or OS permissions.

Local evidence under /private/tmp/feral-native-populated-20261002:
layout-fixed-security.png, layout-fixed-copy.png, layout-fixed-narrow.png,
layout-final-narrow-copy.png and probe exit/log receipts. No raw private OS logs or
IPS committed. Full9.28 failed acceptance stays frozen separately; its early
deadline, unexplained exit0, incomplete Allow and pending-status presentation
remain open until the next immutable candidate is tested.
