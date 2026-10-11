# Selected macOS window capture, October 10

The native 9.60 acceptance run used background Chrome Accessibility controls,
then a desktop-region capture. The actual image showed a different foreground
application. AX interaction does not establish foreground capture identity.
Image inference was blocked before provider dispatch in that run.

The capture skill now accepts an exact CGWindowID, process ID and title from
fresh `gui_computer_use__window_list` metadata, plus an optional exact app name.
It validates identity before and after `screencapture -x -o -l` and validates the
private staged image before publishing bytes or requesting analysis. Missing,
ambiguous or changed identity returns no image. The mode does not activate the
window or fall back to the desktop. Existing desktop/region calls remain
compatible; region cannot be combined with explicit window selection.

Parent integration passes 79 focused checks, including 39 new capture cases and
existing screen, selected-scene and agentic authorization regressions. Six existing
warnings remain. Full-core CI Ruff passes. Final full-project typing retains
the 780-diagnostic Mac baseline with zero additions or removals.

Window selection is a stale-identity guard, not an atomic OS ownership lock or
browser-tab grant. Global keyboard input still requires the intended app to be
frontmost and its target field verified. Real scoped capture, selected-model image
reading and the complete saved browser workflow remain live acceptance gates.
