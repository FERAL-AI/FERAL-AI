# Phone browser task cancellation and audio recovery

October 10, 2026. Physical-device evidence supersedes the earlier fixture-only
checkpoint. Pairing, voice transport and opening a page were observed, but the
subsequent page-control request did not produce a verified outcome. No completed
phone-to-browser acceptance is claimed.

## Observed sequence

Private iPhone console output and screenshots were reconciled with the local
brain's stderr and read-only SQLite receipt metadata. Raw recordings, personal
transcripts, device identifiers, addresses and browser contents are excluded.

- Initial connection candidates were stale or unauthorized. Pairing subsequently
  succeeded and the phone received `node_ack`.
- The brain's realtime voice session executed `desktop_control__open_url` and
  completed its response. This opens a URL; it does not establish control of the
  browser window selected by the user.
- The subsequent typed page-control request entered the orchestrator at 14:34:22
  local time. An accessibility snapshot and browser-memory lookup ran. A
  `browser__navigate` attempt initially failed to connect to CDP, then launched
  managed Chrome and connected through CDP and Playwright.
- At 14:34:48 the phone connection was replaced. The corresponding stored
  terminal receipt has `processing_outcome=cancelled`, `action_outcome=unknown`,
  `durable=true`, no approval requests and empty final prose. Effects had begun;
  neither completion nor safe replay can be inferred.
- The iPhone log contains 5,684 `auou/vpio/appl` render failures. The first
  precedes Brain playback startup. The brain nevertheless received successive
  PCM chunks during the first voice session. This proves transport continued;
  it does not establish audio quality or audible playback.
- An independent ambient payload failed validation because a signed deviation
  was encoded as confidence. Its repeated rejection is not evidence that the
  browser request itself was rejected.
- The console ends with code 9. Without a termination report or confirmation
  of a manual debugger stop, the reason for that termination is unknown.

No Chrome consent wait appears in the contemporaneous task database. A consent
pause must not be substituted for the observed cancelled receipt.

## Runtime identity limits

The port-9090 CLI log reports package version 2026.9.7. Its installation is
editable against this checkout, using pyenv Python 3.11.11. That package label
cannot identify the loaded source commit or establish that an older codebase
was used. The separately verified Mac 9.55 bundle remains unchanged. No service,
profile, account, model preference or saved task was restarted or altered during
this investigation. No port-9090 listener remained at inspection time.

## iOS owner handoff

Read-only review of committed iOS `aa77855` identifies these concrete defects:

1. `SharedVoiceAudioEngine` stops capture on format notification instead of
   rebuilding its converter. The consuming task can finish while listening UI
   remains active.
2. `SharedMicTap` observes configuration changes from all engines and rebuilds
   capture without checking the originating engine.
3. The capture watchdog checks cumulative arrivals only once. It misses a stall
   after initial successful capture and lacks a cancellable engine-generation
   fence.
4. Switching assistants stops current OpenAI audio without revoking future
   capture/playback ownership. Late audio, continuous-mode completion and delayed
   recovery paths can reassert audio after selection changes.
5. Brain playback retains unusable engine references after startup failure and
   lacks explicit recovery when playback stops after a configuration change.
6. Direct OpenAI connection loss can leave processing state set after abandoning
   the response. The Computer target previously disappeared on disconnect,
   concealing the task-recovery state.

Concurrent iOS edits now address converter rebuilds, recurring fenced capture
monitoring, engine-notification filtering, OpenAI audio ownership, failed-playback
cleanup and visible Computer disconnect state. These are in-progress source
changes owned by the iOS agent; this investigation makes no installation or
hardware-validation claim for them. Preserve that agent's dirty files.

Before installation, verify capture-stream failure clears listening state,
late playback cannot revive an old owner, stopped playback can recover, and all
delayed callbacks reject a replaced generation. Keep audio-session activation
off the UI-critical path. Apple documents that a hardware format change stops
and uninitializes the engine, requiring connections to be reestablished where
formats change: [AVAudioEngineConfigurationChange](https://developer.apple.com/documentation/foundation/nsnotification/name-swift.struct/avaudioengineconfigurationchange).

Read-only review of the in-progress changes still finds failed converter rebuild
finishing the capture stream without clearing the manager's listening state;
retained stopped playback silently discarding later chunks; and direct OpenAI
connection loss retaining abandoned processing state. The new Brain `endSession`
cleanup is not yet reached by the view's explicit End path. The buffer-progress
watchdog addresses stalled capture, not render errors that coexist with continuous
buffer delivery. These call sites require full target compilation and focused
failure-path verification after the iOS owner's edits stabilize.

Ambient confidence must retain its documented 0–1 meaning. Do not reinterpret
signed physiological deviation as confidence or relax backend validation to
accept it. Verify the queued transcript can be rebuilt with valid optional
metadata and acknowledged without losing its text.

## Independent backend model-loop repair

Real model-loop tests exposed a defect hidden by the previous command-runner
fixtures: parallel child tasks inherited the task's context but lacked the exact
owning-task authority required by its selected-Chrome scope. TaskFlow model-step
tools now run sequentially on their owning step task; ordinary chat retains its
existing parallel execution.

An exact Chrome consent pause closes started, undispatched tool traces with an
existing `tool_result` error explaining the permission and explicit Resume wait.
The same wait is re-raised, preserving its persisted checkpoint and zero-effect
pending call. Earlier completed calls in a mixed batch publish their actual
results before a later pause. They are not relabelled as undispatched or replayed
on Resume. All trace delivery retains the existing execution-session transport;
this introduces no shared-origin or foreign-device publication path.

This source repair addresses independently reproduced defects. It does not change
the incident's stored cancelled/unknown outcome, certify phone voice recovery or
update the immutable Mac 9.55 bundle.

Validation: 14 new real-loop regressions cover buffered/streaming waits, multiple
calls, mixed completed/deferred calls, exact saved-call Resume without replay,
execution-session transport and delivery failure. The worker's five-suite gate
passes 88 tests. Parent integration passes 90 tests across the new regressions,
phone status/intake and chat receipt/abort suites. Full-core CI Ruff, whitespace
and affected public-doc checks pass. All tests use disposable profiles and inert
providers/CDP; no physical audio, account or browser interaction is exercised.

## Required physical acceptance

Identify installed iOS source and Mac runtime first. Use one explicit harmless
page interaction with an observable result, then verify its terminal receipt and
spoken/text answer. Compare iPhone-only and glasses HFP capture/playback while
recording buffer progress, output progress and route identity. Exercise route
changes, assistant selection, interruptions and reconnects. A replaced socket
must display/reconcile the stored cancelled or uncertain result without resending
the original request. If code 9 was not a manual stop, inspect the iOS termination
report before assigning a crash cause.

Duplicate Objective-C SDK classes and vendor database/restoration warnings remain
separate iOS packaging issues. They are not established causes of this task's
cancellation or process termination.
