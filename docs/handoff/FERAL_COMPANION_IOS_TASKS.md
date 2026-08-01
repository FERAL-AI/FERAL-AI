# FERAL Companion iOS: handoff

Verified against the code on 2026-08-01 in `~/Desktop/feral-companion-ios`
(HEAD `12598ac`) and cross-checked against `feral-core`.

This app is the **operator console for a local brain**, not the main product
surface. Four tabs: Chat, Context, Devices, Settings. `ContextView` renders the
brain's live perception state rather than user health, which is the clearest
signal of what it is for. The Theora app is the main iOS app and has its own
handoff at `THEORA_IOS_TASKS.md`.

Quality note up front, because it matters for how you approach this: the audio
layer, the Bluetooth truthfulness contract, the pairing/Keychain path, and the
test suite are genuinely good work. The recurring failure is **finished
components with no door** rather than sloppy code.

---

## P0. The `peripheral_bridge_register` loop is open

Registration works. The brain builds a `BridgedPeripheralAdapter` per device,
renders a fleet card, and derives safety tiers. **No brain-initiated action can
ever succeed**, because the manifest capability ids and the adapter action names
have an **empty intersection**.

Declared in `App/Sources/State/PeripheralManifests.swift`:
`read_heart_rate`, `read_spo2`, `read_temperature`, `play_audio`, `vibrate`,
`capture_photo`, `set_led`.

Answered by the adapters:

| Adapter | `canHandleAction` names | cite |
|---|---|---|
| `JWBleAdapterWired` (theora-w300) | `health_measure`, `get_heart_rate`, `get_spo2`, `get_temperature`, `get_uv_level`, `get_steps`, `display_text` | `Adapters/JWBleAdapterWired.swift:100-107` |
| `VeepooAdapterWired` (veepoo-band) | `health_measure`, `get_heart_rate`, `get_spo2`, `buzz` | `Adapters/VeepooAdapterWired.swift:62-66` |
| `QCSDKAdapter` (w610-open) | `display_hud`, `capture_frame`, `start_recording`, `stop_recording` | `Sources/FeralNodeSDK/Adapters/QCSDKAdapter.swift:98` |
| `HealthKitAdapter` | `health_measure`, `get_heart_rate`, `get_spo2`, `get_steps` | `Adapters/HealthKitAdapter.swift:160` |

Every invocation falls through to `FeralNode.swift:506-512` and returns
`"no adapter handles action: <name>"`.

The brain sends the capability id verbatim as `payload["name"]`
(`hardware/mesh.py:323-331`), so **the manifest is the contract**. Pick one
vocabulary and make both sides use it.

Second half of the same bug: **there is no `device_id` routing.** Nothing reads
`params["device_id"]`, so even after the names match, `theora-w300` and
`w610-open` are indistinguishable at dispatch.

Also: `w610-open` is registered unconditionally
(`PeripheralManifests.swift:22`) while `QCSDK.framework` is absent from the
build, so `DeviceStore.swift:247` reports it `.unsupported`. The brain renders a
fleet card for hardware this build cannot drive.

---

## P0-2. `AudioPlayback.teardown()` has zero call sites

Two live consequences, not just dead weight.

**The audio session never deactivates.** `teardown()` is the only caller of
`W300AudioBridge.deactivate(for: .playback)` (`Audio/AudioPlayback.swift:139`),
so `playbackActive` latches `true` after the first TTS chunk. The guard at
`Audio/W300AudioBridge.swift:221`:

```swift
guard !captureActive, !playbackActive else { return }
```

never passes, so `setActive(false, options: [.notifyOthersOnDeactivation])` is
never reached. The session holds the HFP/SCO route for the process lifetime and
Music never resumes. This is exactly the leak
`GLASSES_AUDIO_AGENT_PROMPT.md` §5 warns about and that
`W300AudioBridge.swift:213-215` claims to implement.

**The microphone can go permanently dead.** `VoiceMuteController.isMuted` is
`userMuted || autoMuted` (`Audio/VoiceMuteController.swift:43`). `autoMuted` is
raised on the first TTS chunk (`AudioPlayback.swift:88-91`) and lowered only by
`stoppedPlayingTTS()`, whose callers are `stopAndDrain()`, `teardown()` (zero
call sites), and `settleIfFinished()` which requires `finalReceived == true`.
`stopAndDrain()` is reachable only from `case "speech_started"`
(`Brain/BrainClient.swift:597`).

So if a TTS stream starts and the socket drops before `is_final: true`,
`autoMuted` stays true for the rest of the process. The mic is silently dead and
the UI shows nothing, because the button binds only to `userMuted`.

---

## P1. Voice frames: only `audio_response` is handled

`BrainClient.handleInbound` (`Brain/BrainClient.swift:493-665`) decodes
`node_ack`, `chat_response`, `text_response`, `transcript`, `audio_response`
(`:579`), `speech_started`, `error`, `genui_push`, `sdui_patch`. Everything else
hits `default: break` at `:661`.

**Not handled:** `audio_chunk`, `tts_chunk`, `voice_state`, `voice_cancel`,
`voice_status`, `voice_mute`. Zero occurrences of any of them in Swift.

Two consequences:

1. **Chained voice mode produces no audible output.** The app hardcodes
   `voiceMode: .openaiRealtime` (`BrainClient.swift:435`) so it normally lands
   on `audio_response` and works.
2. **But `tts_chunk` is also the realtime fallback emitter**
   (`voice/router.py:1655-1690`), reached when a realtime provider degrades
   mid-session. So today, a realtime failover makes this app go silent **with no
   error surfaced**, because `voice_status` is dropped too. That is a live
   silent-failure mode independent of chained mode.

Payload shapes are identical to those in `THEORA_IOS_TASKS.md` P1-1.

---

## P1-2. Mute does not reach the brain

`VoiceMuteController` (67 lines) discards captured frames inside the engine tap
(`Audio/AudioCapture.swift:114-116`) and keeps the engine warm, which is the
right local behaviour. But it sends nothing on the wire, so from the brain's
side the user is simply silent.

The brain now has `voice_mute` and a server-side ledger. What is needed:

1. `FeralNode.sendVoiceMute(streamId:muted:)` emitting
   `{"type": "voice_mute", "payload": {"stream_id": ..., "muted": ...}}`. Use
   `BrainClient.voiceStreamId` (`:87`, minted at `:432`); the brain derives
   `session_id = stream_id or f"voice-{node_id}"`, so the ids already align.
2. Call it from `toggleUserMute()`. **Do not propagate `autoMuted`** — that is
   local TTS ducking and should not perturb the brain's barge-in handling.
3. Re-send state after reconnect. `HUPWebSocket`'s reconnect path only re-sends
   `node_register` (`FeralNode.swift:80-85`).
4. **Fix the `autoMuted` latch first** (P0-2), or the client will push a stale
   muted state into the ledger.

---

## P1-3. `setVoiceProcessingEnabled` is absent from the entire repo

Verified: `grep -rn "setVoiceProcessingEnabled\|VoiceProcessing" --include="*.swift" .`
returns nothing. Echo cancellation relies on `mode: .voiceChat` plus the TTS
auto-mute, which `VoiceMuteController.swift:30-33` describes as
"belt-and-suspenders".

For contrast, the Theora app **does** call it, at
`OpenAIRealtimeManager.swift:2733`, ordered before the format read. Session mode
and engine-level Voice-Processing I/O are different things. Add it on
`AudioCapture`'s input node, before `input.outputFormat(forBus: 0)` at
`Audio/AudioCapture.swift:106`.

---

## P2. Six components with no door, ~1,400 LOC

Listed because deleting or wiring them is a decision someone should make
deliberately.

1. **`Views/HealthView.swift` (231 L)** — complete Vitals dashboard, zero
   external references. `RootView.swift:25-30` renders `ContextView` in that
   slot, while `RootView.swift:3` still says "Four tabs: Chat, Health, Devices,
   Settings". Downstream: **`HealthStore` has no UI reader at all**, yet
   `HealthKitAdapter` and `JWBleAdapterWired` still fan out writes to it on
   every poll, including elaborate `sampleAt` staleness machinery nothing shows.
2. **`Adapters/CameraGlassesAdapter.swift` (245 L)** — complete 1fps
   `glasses_frame` streamer, instantiated only in tests. Its doc comment claims
   Settings surfaces a "Demo mode" toggle; `SettingsView` has no such toggle.
3. **`BLE/BLEPeripheralScanner.swift` (211 L)** — same shape. Its toggle
   `feral.ble.peripheral_share` exists nowhere in the UI, so it is permanently
   false.
4. **`Sources/FeralNodeSDK/Adapters/BLEPeripheralScannerAdapter.swift` (458 L)**
   — SDK twin with 260 lines of dedicated tests and no production
   instantiation.
5. **The manifest/adapter gap** (P0).
6. **`AudioPlayback.teardown()`** (P0-2).

---

## P2-2. Smaller, verified

- **The shared-thread fix is defeated by its own history store.**
  `BrainClient.swift:63-83` explains at length that `chatSessionId` defaults to
  `""` so the brain resolves it to `primary_session_id`, giving phone and web
  one thread. But `bindHistory`, called unconditionally at
  `AppEnvironment.swift:43`, immediately overwrites it with
  `store.currentSessionId` (`BrainClient.swift:141`), and `ChatHistoryStore.init`
  mints a fresh UUID on first launch (`ChatHistoryStore.swift:62`). The
  documented default never reaches the wire. It may self-correct via the
  `chat_response` echo handler (`:515-517`), but the first request of a fresh
  install partitions the phone into its own thread.
- **The vendored SDK has drifted from its own policy.** `docs/SDK_SYNC.md:39`
  says "Never modify `Sources/FeralNodeSDK/` files in this repo directly." A
  diff against the canonical tree shows two added files, a whole added `Skills/`
  directory, and four modified files. `SDK_SYNC.md:15` records version `0.2.0`
  while `Sources/FeralNodeSDK/Info.swift:4` says `0.3.0`, and the recorded sync
  SHA is ~3 months stale.
- **`JWBleAdapterWired.handleAction`'s `display_text` case discards its
  argument** (`_ = text`, `:155`) and returns `success: true` with a note. That
  reports success for a no-op.
- **`AudioCapture.start()` uses `MainActor.assumeIsolated`** (`:45`), which will
  trap if ever called off the main actor. Safe today (only caller is
  `@MainActor`), latent otherwise.
- **Stray `FMDB/` at repo root**, untracked duplicate of `Vendor/FMDB/`. The
  build only references the `Vendor/` copy.

---

## Docs that are stale, in the app's favour

`GLASSES_AUDIO_AGENT_PROMPT.md` is untracked and predates commit `c1bd6da` by
about 54 minutes. Its headline "critical fix" (remove A2DP) was already applied
and the code went further. Where doc and code disagree on `.duckOthers` and the
sample-rate/IO hints, **the code is right and carries reasoned justification**
(`W300AudioBridge.swift:14-16`, `:27-31`). Update the doc, do not the code.

`README.md:56-65` lists `HealthKitAdapter` and `JWBleAdapter` as "Planned"; both
are fully implemented. `README.md:128` references `docs/DEMOS.md`, which does
not exist. `docs/PROGRESS.md` lists Phases 3-8 as pending; all are shipped.

---

## Build note

The `.xcodeproj` is gitignored and generated by xcodegen from `project.yml` plus
`project.vendor.generated.yml`. Run `scripts/bootstrap.sh`.

Proprietary vendor frameworks (JWBle, RTK*, VeepooBleSDK, JL_BLEKit, ...) are
**present on this machine but not committed**, so `#if canImport(JWBle)` is live
here and dead on a fresh clone. `QCSDK.framework` is absent entirely.

`scripts/generate-vendor-yml.sh` correctly detects static archives via `lipo` +
`file` and sets `embed: false` for them, because embedding runs `bitcode_strip`
which hard-fails on static archives. Do not "simplify" that.
