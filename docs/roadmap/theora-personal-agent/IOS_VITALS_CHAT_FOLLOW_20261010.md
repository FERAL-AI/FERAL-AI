# iOS callback freshness and idle chat following

The signed development app is installed in place from local source
`cdc2bda729d26f2acc8c8347830106ad2a0448d9`. Exactly six reviewed paths were
committed: the Objective-C BLE bridge header/implementation, glasses relay,
VoiceAgentView and two persistent regression scripts. The existing project,
vendor SDK, vendor metadata/untracked files and unrelated source were preserved.
No app data reset, SDK replacement, project regeneration or remote publication
was performed.

## Resulting behavior

A temperature-only SDK callback previously forwarded the cached heart rate again
with a new timestamp. The bridge now preserves separate per-metric callback
receipt epochs before asynchronous delivery. The relay validates freshness and
deduplicates receipt epochs instead of values, so a genuinely new callback with
the same BPM still forwards. It sends sample_ts and sdk_callback_receipt
provenance; missing, invalid, future and stale receipt epochs never fall back to
send time. These are callback receipt times, not proven sensor acquisition times.

Disconnect, timeout-disconnect and pairing removal advance a synchronized monotonic
generation, invalidate old receipt evidence and clear old cached values on the
callback queue. Queued Objective-C callbacks and Swift Tasks retain their exact
generation; callbacks queued before disconnect cannot forward after reconnect,
including when wall time moves backward. A late cache clear preserves a valid new
generation receipt. Retired maximum generations fail closed. The existing cached
callback API, subscriber registry and hardware start/sync flags are preserved.

The chat follower previously discarded the last content update if it arrived
while its scroll view was decelerating. A bounded retry follows the final rendered
update after deceleration, with coalescing and exact attached-view/generation
checks. Deliberate scroll-up and detached views retire following; an explicit
latest request can resume it. The retry budget is two seconds, not acceptance of
arbitrarily long deceleration. The already integrated selected-agent menu is
unchanged; screenshots of an earlier build do not prove current physical UI.

## Verification actually performed

The parent reproduced both the original temperature-only cache restamp and the
withdrawn proposal's queued-Task-after-reconnect failure. The amended actual
selected Objective-C callbacks and Swift subscription/handler/encoder, feeding
the existing Mac decoder, pass13 synthetic callback rows and10 dated frames.
Coverage includes constant-valued new HR, one-shot HR, wearing checks, invalid
epochs, stale queues, reconnect/backward-clock fencing, BLE-disconnected Tasks,
late clear and retired-generation overflow.

The actual selected Swift follower passes10 assertions; the original source
compiles then fails the final-growth continuation case. Tests include scroll-up
with a stale binding, explicit latest, replacement view, redundant geometry and
retry expiry. Historical BP/HRV wire checks retain21 Python/10 Swift assertions.
All three actual-source fixtures pass before the single full signed device build;
strict deep codesign verification and in-place device installation pass.

App executable SHA-256:
`c6e7a2507b7f2f91603437e2529b649a3665f5024eb744458f36392f238e16ed`.
Debug dylib SHA-256:
`e28bcdeffb1577c66cd452497913f4bad1ad7277baf157dda2dd540a13616f43`.
Free disk was16,955,977,728 bytes before and17,078,538,240 after, within the
2GiB incremental budget using the existing device DerivedData.

## Remaining acceptance

Physical UIKit/KVO/gesture behavior, genuinely new supported sensor callbacks,
HFP duplex voice, interruption, ambient rejection and concurrent task results
remain unverified while the phone UI is unavailable. The hardware stream restart
boundary and DeviceCoordinator's legacy cached-snapshot recorder are separate
work. The Mac canonical biometric table does not yet retain receipt provenance;
history transfer, metric timestamp presentation and personal baseline acceptance
remain separate required work. Unsupported sensors must remain unavailable.
These checks do not establish complete product or public release readiness.
