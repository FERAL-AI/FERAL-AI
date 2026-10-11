# Tracked live-backend web acceptance

Actual full run passed **61 tests in19.2 minutes**, exit zero, against immutable
`7f818da08139952b1698644469e7016563512cd5`. This source predates the later shell
hydration correction and local attempt/preference work; the result is not silently
applied to those changes.

The isolated snapshot came from tracked Git source, built before freezing. All
1127 entries retained SHA-256, inode, size, mode, mtime and ctime. Startup273 and
shutdown349 core module origins were verified, with zero checkout-origin
violations. Pinned browser and FFmpeg hashes stayed unchanged.

The run exercised744 clicks,29 hard-load WebSocket receipts and51 distinct
registered REST paths, with zero control throws, silent failures or unclickable
controls. Chat/Settings navigation passed without ENOSPC or AudioContext errors.
Its owned server/test processes and listener were independently verified absent
after graceful SIGTERM, without forced kill.

## Limits

- Five controls produced no observable DOM change: Copied, an already-empty New
  task, and three already-active publication tabs. Source inspection identifies
  possible idempotence/detector limits; this does not independently verify
  clipboard contents or every effect.
- Ten Skills controls exceeded the existing45-control route cap and were not
  walked. Other intentionally excluded/unsafe controls retain their exclusions.
- Three injected-error scenarios were excluded by the recorded command. This is
  not an unrestricted sweep of every application action.
- Empty/disposable backend fixtures, not personal accounts or physical devices,
  were used. This is browser acceptance, not Mac GUI/audio acceptance.

The keyboard-only skip link was excluded from the pointer walk by a narrow
selector/style check and independently verified with actual Tab→Enter→main focus.
The shutdown observer records module provenance after normal Uvicorn shutdown
and before signal restoration; application handlers and effects were unchanged.

Evidence root:
`/private/tmp/feral-live-web-full-final-20261002-ojnp9bpu`, containing
`validation-summary.json`, `final-receipt.json`, Playwright log/report and
independent cleanup/provenance receipts. Exact command and file hashes are in
the receipt. No production source changed during this worker assignment.

Earlier results remain preserved: a60-pass run with failed copied-source
integrity, a tracked60-pass/1-fail run with actual ENOSPC and incomplete shutdown
provenance, and a focused4-pass corrected-observer run. The later pass does not
establish the cause of earlier source removal or disk pressure. Available disk
was3.27GB initially,2.52GB minimum and3.07GB after this successful run.
