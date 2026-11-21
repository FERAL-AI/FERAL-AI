# Native numeric values and fresh foreground evidence

The9.61 live Chrome task stopped after string AX writes to native date segments
were rejected. The setter converted every requested value to a string, including
controls that expose a native numeric AXValue. Fixture counterexamples reproduce
the type rejection and incorrect string-only readback comparison.

An exact resolved AXIncrementor now receives the finite numeric type it actually
exposes. Invalid, nonfinite or out-of-range integer input is refused before the
write. Numeric readback requires actual numeric equality; a formatted text
lookalike does not verify the write. Text controls and whole date fields retain
text handling. Secure and stale references refuse before value reads or writes.
No date format, action support or input permission is invented.

Foreground reporting now observes the fresh system-wide AXFocusedApplication
PID and resolves that exact running application. Unavailable observations return
unknown rather than cached app state or window z-order. This is application
evidence at query time, not exact-window focus or keyboard authorization.

Parent integration passes121 focused AX checks with14 live checks skipped and
five existing warnings. Full-project typing retains780 baseline diagnostics with
zero additions/removals at this integration point. Actual Chrome numeric writes,
verified window focus and the saved workflow remain live acceptance gates.
These changes do not establish that cached NSWorkspace state caused the9.61
physical contradiction. Scene OCR/query guidance also requests verbatim case and
punctuation after the live separator mismatch;48 existing scene checks pass,
while actual exact visual recognition remains unproved.
