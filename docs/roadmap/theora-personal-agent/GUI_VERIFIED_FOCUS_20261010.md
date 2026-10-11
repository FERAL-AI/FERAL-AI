# Verified macOS application and window focus

Window focus previously reported success after activation exited zero, even if
another application stayed foreground or the specific window raise failed.
The repair binds a uniquely observed CG window to an actual AX handle, requests
application/window focus, then verifies fresh foreground PID and that same focused
handle. Accepted requests alone do not establish success.

CG and AX titles may differ while multiple Chrome windows share identical bounds.
An optional exact AX title from fresh window inspection resolves this only with
a unique observed prefix relationship on both inventories and matching bounds.
Ambiguity, replacement, permission refusal and cancellation do not produce a
successful receipt. Window enumeration separates app foreground from actual
window focus and marks unavailable observations explicitly.

Parent integration passes109 focused checks, including36 new cases, with six
existing warnings. Full-core CI Ruff passes. Full-project typing retains779
existing diagnostics, adds none and removes one. Numeric AX integration retains
its121-check/14-live-skip evidence. Physical repaired focus and completed browser
workflow remain open for native9.62 acceptance. A successful focus receipt is a
point-in-time observation; input still needs fresh foreground/field verification.
