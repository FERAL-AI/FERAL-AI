# Theora iOS: voice work handoff

Everything here was verified against the code on 2026-08-01, in
`~/Desktop/Theora-backend-ML/ios/Theora` and `feral-core`. Line numbers
are from that state and may drift.

Context: the Theora app is the main iOS surface. Its chat UI offers
Wellness and FERAL Brain, and FERAL Brain uses FERAL's voice system.
Device is the **W300** glasses (`Ble-Demo-iOS/` is the Wo-Smart / JWBle
vendor SDK).

The FERAL integration is real and good: a hand-written HUP v1.3.0 client
in `ios/Theora/Feral/` (7 files, ~2,170 LOC), wired at
`TheoraApp.swift:56`. Field naming matches the brain exactly. None of
the tasks below are rewrites; they are gaps.

---

## Task 1: parse the chained voice frames (highest value)

**The problem.** `FeralBrainConversationManager.swift:175` hardcodes
`voiceMode: .openaiRealtime`. `.chained` exists in the enum
(`FeralHUPModels.swift:184`) and is never passed. If you pass it today,
the phone streams audio, the brain transcribes, thinks and synthesizes
correctly, **and you hear nothing**, because the two modes emit
different frame types and the parser only knows one.

- Realtime and Gemini emit `audio_response`.
- Chained emits `audio_chunk`, `voice_state`, `voice_cancel`, and the
  router additionally emits `tts_chunk` on the fallback path.
- `FeralHUPModels.swift:411-413` sends everything else to
  `default: return .other(type:)`, logged at
  `FeralConnectionManager.swift:315` as "ignoring unsupported frame type".

**Exact payloads**, read from the brain:

`audio_chunk` (`voice/chained_pipeline.py:951`)
```json
{ "type": "audio_chunk",
  "payload": { "data_b64": "...", "chunk_index": 0, "is_final": false,
               "encoding": "mp3", "sample_rate": 24000 } }
```
Note `encoding` is currently `"mp3"` and is moving to PCM16; read the
field rather than assuming either. `sample_rate` is authoritative.

`voice_state` (`voice/chained_pipeline.py:916`)
```json
{ "type": "voice_state",
  "payload": { "state": "listening|thinking|speaking|idle",
               "mode": "chained", "error": "optional" } }
```

`voice_cancel` (`voice/chained_pipeline.py:892`)
```json
{ "type": "voice_cancel",
  "payload": { "reason": "...", "mode": "chained",
               "drop_pending_audio": true } }
```

`tts_chunk` (`voice/router.py:933`) is the fallback-path variant; treat
it as `audio_chunk`.

**What to do.** Add the four cases to the `FeralHUPModels` decoder,
route decoded audio into `SharedVoiceAudioEngine`, honour
`drop_pending_audio` by flushing the playback queue, and drive the UI
state from `voice_state` rather than inferring it.

**Why it matters.** It is the only thing making realtime mandatory. Once
chained works, provider choice becomes a policy decision instead of a
constraint, and the brain's ordered fallback chain (realtime, then
another realtime, then local) becomes reachable from the phone.

---

## Task 2: measure a real turn

**There is no latency instrumentation anywhere in the iOS voice code.**
`grep -n "latency|elapsed|duration_ms" ios/Theora/Voice/*.swift` returns
nothing. The ~800ms-1.3s figure in `docs/voice-agent-sota.md:33` is an
estimate from third-party numbers, not a measurement of this app.

Instrument both paths with the same two timestamps so they are
comparable:

- **t0**: server VAD reports speech stopped. Realtime: the
  `input_audio_buffer.speech_stopped` event. Chained: `voice_state`
  going to `thinking`.
- **t1**: first audio frame rendered, not received. Realtime: first
  `response.audio.delta` scheduled on the player node. Chained: first
  `audio_chunk` scheduled.

Log p50 and p95 per path per session. Until this exists, nobody can say
whether realtime is worth its cost, in either direction.

---

## Task 3: verify echo cancellation

`SharedVoiceAudioEngine.swift` sets `.playAndRecord` with `.voiceChat`
and does careful manual HFP/SCO route acquisition (`:69-125`), which is
correct and should be preserved.

What to check: whether `setVoiceProcessingEnabled(true)` is called on the
`AVAudioEngine` input node. Session-level `.voiceChat` and engine-level
voice-processing IO are not the same thing, and without the latter the
engine graph may not be getting Apple's AEC. This is a test, not an
assumption: record while the assistant speaks and look for the assistant
in the captured buffer.

If AEC is absent, barge-in cannot work reliably regardless of transport.

---

## Task 4: mute

The brain now has a `voice_mute` frame and a session-level mute ledger.
Send from the phone:

```json
{ "type": "voice_mute",
  "payload": { "stream_id": "<the voice session id>", "muted": true } }
```

Brain behaviour: mute stops capture ingress server-side as well as
whatever the client does, survives a reconnect (failing safe toward
muted), and does not stop synthesis (that is `voice_interrupt`).
`voice_status` frames now carry a `muted` field; treat its absence as
"older brain", not as unmute.

Mirror this in the UI so it cannot show "listening" while muted.

---

## Task 5: do NOT adopt WebRTC

Recorded here so it is not relitigated. Theora's own analysis already
rejected it (`docs/voice-agent-sota.md:13`): WebRTC owns its audio device
module and fights the manual HFP/SCO route control glasses require, and
`SharedVoiceAudioEngine.swift:80-125` is exactly that control.

Independent research agrees for different reasons: FERAL already carries
Opus so the bandwidth win is moot, the one published production A/B found
WebRTC *slower*, iOS gives AEC from one line regardless of transport, and
a local-first brain behind a home NAT wants an overlay network rather
than a TURN relay someone has to host forever.

Revisit only if video from the glasses becomes a requirement.

---

## Not tasks, but worth knowing

- **Gemini works from the phone already.** The phone never talks to
  Gemini; `GeminiRealtimeProxy` runs in the brain
  (`voice/gemini_realtime.py:23`) and holds the socket. Selecting Gemini
  is a brain-side setting.
- **UV is relayed and dropped.** `FeralGlassesRelay.swift:174` emits
  `event_type: "uv"`; the brain has no dispatch branch. Either add the
  branch brain-side or stop sending it.
- **SpO2 is never streamed**, only exposed as a `read_spo2` action, so
  the brain's `vitals_trend.spo2_*` is always null from the glasses.
- **`W610/QCSDKDemo/.../VoiceAgent/` contains a complete chained voice
  agent** for glasses (WhisperKit, Apple Speech, Kokoro, an orchestrator,
  Moshi, a Gemini Live client). Worth reading before building anything
  similar. It also contains **two live API keys in `Config.swift:23,29`**
  that should be rotated; the directory is untracked so they are not in
  git history.
