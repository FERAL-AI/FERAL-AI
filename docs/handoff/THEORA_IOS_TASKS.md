# Theora iOS: handoff

Verified against the code on 2026-08-01 in `~/Desktop/Theora-backend-ML/ios`
and cross-checked against `feral-core`. Line numbers are from that state.

**Supersedes `THEORA_IOS_VOICE_TASKS.md`**, which was written before the full
read and covered voice only.

Run git from **inside** `~/Desktop/Theora-backend-ML`. `/Users/mahmoudomar` is
itself a git repo and shadows Desktop, so `git status` from above lies. The
real state: on `main`, in sync with origin, HEAD `6581a75`, **no Swift files
modified**.

---

## P0-1. Rotate the OpenAI key and delete the hardcoded fallback

`ios/Theora/Configuration.swift:36` contains a live `sk-proj-...` key.

Verified: the file is **tracked**, the key is **present in HEAD**, and it was
introduced in commit **`546e0e7`**, so it is in git history. Rotating alone is
not sufficient.

`Theora/Secrets.xcconfig` holds the same key and *is* correctly gitignored. The
`.gitignore` is working; the hardcoded fallback defeats it.

Two side effects worth fixing at the same time:
- `hasOpenAIKey` (`Configuration.swift:39-41`) can never return false, so every
  "API key not configured" error path is unreachable
  (`OpenAIRealtimeManager.swift:809, 1297, 3448`).
- `OpenAIRealtimeManager.swift:1303-1305` prints the key's first 15 and last 4
  characters to console on every connect.

The Gemini key is handled correctly by comparison (env, plist, empty string, no
fallback) at `Configuration.swift:10-14`. Copy that shape.

---

## P0-2. Replace the single-slot vitals callback with an owner-keyed registry

**This is why FERAL's health relay works or does not work depending on which
screen the user is on.**

`BLE/BLEBridgeObjC.m:18` declares exactly one callback property, and
`:1362-1368` guards subscription with a single BOOL:

```objc
- (void)subscribeToRealTimeVitals:(RealTimeVitalsCallback)callback {
    if (self.isSubscribedToVitals) {
        return;                      // silently discards the new callback
    }
    self.realTimeVitalsCallback = callback;
    self.isSubscribedToVitals = YES;
```

Four consumers subscribe (Swift renames the selector to
`subscribe(toRealTimeVitals:)`, which is why a naive grep misses three of them):

| Consumer | Call site |
|---|---|
| `HealthMonitorService` | `Services/HealthMonitorService.swift:112` |
| `DashboardView` VM | `Views/Dashboard/DashboardView.swift:2304` |
| `FeralGlassesRelay` | `Feral/FeralGlassesRelay.swift:122` |
| `BackgroundVoiceService` | `Services/BackgroundVoiceService.swift:241` (dead code) |

Consequences, all verified:

1. **First subscriber wins, the rest are silent no-ops.** `MainTabView` is the
   root container, so `HealthMonitorService` almost always registers first.
2. **The relay's re-assertion strategy does not work.** Its header comment
   (`FeralGlassesRelay.swift:8-11`) says it re-asserts every 15s "so a transient
   owner change does not permanently steal the stream". It does re-call, but the
   call hits the early return and changes nothing. **The stated design is not
   what the code does.**
3. **Any consumer's unsubscribe kills the stream for everyone.**
   `unsubscribeFromRealTimeVitals` (`:1426-1445`) nils the shared callback *and*
   sends `jwRealTimeHeartRateAction:NO` to the device.
   `DashboardView.stopMonitoring()` calls it on `.onDisappear`.
4. **A FERAL `read_heart_rate` action can stop streaming vitals for every other
   consumer and fail to restore them**, because it calls
   `startRealTimeHR`/`stopRealTimeHR` (`FeralGlassesRelay.swift:256-259`) and
   then re-subscribes, which is a no-op if the slot is held.
5. Local bookkeeping drifts: `isVitalsSubscribed = true` is set unconditionally
   at `:121` regardless of whether the ObjC call took effect, so `stop()` can
   tear down another consumer's subscription.

Fix: keep a dictionary of `owner -> callback`, fan out to all of them, and only
send the hardware stop when the last owner unsubscribes.

---

## P1-1. Parse the chained voice frames

`FeralBrainConversationManager.swift:175` hardcodes `voiceMode: .openaiRealtime`.
`.chained` exists in the enum (`FeralHUPModels.swift:184`) and is never passed.

`FeralInboundParser.parse` (`FeralHUPModels.swift:326-419`) decodes exactly:
`node_ack`, `hup_action_request` (plus undocumented aliases `command`,
`execute`, `hup_execute`), `error`, `node_bye`, `chat_response`,
`text_response`, `transcript`, `audio_response`, `voice_status`. Everything else
hits `default: return .other(type:)` at `:416`.

**Missing: `tts_chunk`, `voice_state`, `voice_cancel`** (zero occurrences
tree-wide). `audio_chunk` is outbound-only, which is correct by design.

Exact payloads, read from the brain:

`audio_chunk` (`voice/chained_pipeline.py:951`)
```json
{ "type": "audio_chunk",
  "payload": { "data_b64": "...", "chunk_index": 0, "is_final": false,
               "encoding": "mp3", "sample_rate": 24000 } }
```
Read `encoding` and `sample_rate` rather than assuming; encoding is moving to PCM16.

`voice_state` (`voice/chained_pipeline.py:916`)
```json
{ "type": "voice_state",
  "payload": { "state": "listening|thinking|speaking|idle",
               "mode": "chained", "error": "optional" } }
```

`voice_cancel` (`voice/chained_pipeline.py:892`)
```json
{ "type": "voice_cancel",
  "payload": { "reason": "...", "mode": "chained", "drop_pending_audio": true } }
```

`tts_chunk` (`voice/router.py:1683`) is the fallback-path variant of
`audio_chunk`; treat it the same.

**`tts_chunk` is not only the chained path.** It is also the realtime *fallback*
emitter, reached when a realtime provider degrades mid-session. So today, if
OpenAI Realtime fails over, the app goes silent with no error shown, because
`voice_status` is decoded but the audio frames are not. Fixing this fixes a
silent-failure mode you have today, independent of whether you ever use chained.

Note the app sends `voice_interrupt` for barge-in (`FeralConnectionManager.swift:502`),
not `voice_cancel`. Verify the brain accepts that; it does today.

---

## P1-2. Enable engine-level AEC on the FERAL voice path

**Theora's own voice path already does this correctly** —
`OpenAIRealtimeManager.swift:2733` calls `setVoiceProcessingEnabled(true)` on
the engine input node, correctly ordered *before* reading the input format.

**The FERAL path does not.** `SharedVoiceAudioEngine.startCapture()` (`:131-180`)
creates its own `AVAudioEngine`, takes `engine.inputNode` at `:137`, and reads
the format at `:138` with no voice-processing enable in between. It sets only
session-level `mode: .voiceChat`.

Those are different things. Session `.voiceChat` selects an audio mode;
`setVoiceProcessingEnabled(true)` installs the Voice-Processing I/O unit on that
specific engine's input node. On glasses HFP, where speaker and mic share one
SCO link, the FERAL path captures FERAL's own TTS with no engine-level
cancellation.

`SharedVoiceAudioEngine.swift:9-13` claims it "mirrors
`OpenAIRealtimeManager.configureAudioSession`". True of the session config,
false of the engine config. Fix the code and the comment.

---

## P1-3. Mute, and send it to the brain

Current mute (`FeralBrainConversationManager.swift:227-230`) sets a local flag
and the capture task drops chunks (`:191-194`). Chunk indices only increment on
actual sends, so the sequence stays contiguous, which is right. But nothing
reaches the brain, and the comment at `:105` says "not muted output" while mute
is capture-side only. Comment and code disagree.

The brain now has a `voice_mute` frame and a server-side mute ledger:

```json
{ "type": "voice_mute",
  "payload": { "stream_id": "<voice session id>", "muted": true } }
```

Brain behaviour: stops ingress server-side as well as whatever the client does,
survives reconnect (failing safe toward muted), does not stop synthesis. The
stream id must be the one from `voice_session_start`, because the brain derives
`session_id = stream_id or f"voice-{node_id}"`.

Re-send the current mute state after reconnect, or the ledger and the client
disagree.

---

## P2. Smaller, verified

- **Playback drain is a 600ms guess.** `isSpeaking` is cleared by a fixed timer
  (`FeralBrainConversationManager.swift:122-130`) rather than an actual drain
  callback. `OpenAIRealtimeManager` does this properly with rendered-frame
  accounting; copy that.
- **No on-device turn detection on the FERAL path.** The phone streams
  continuously from mic-on to mic-off; `stopListening()` sends an empty final
  chunk (`:216-222`). The brain now has server-side Silero VAD, so this is
  workable, but there is no local endpointing at all.
- **`sessionId` is never rotated**, not even by `clearHistory()` (`:270-274`).
  Starting a new chat clears the local transcript while the brain keeps the
  server-side context.
- **SpO2 is never streamed.** `FeralHUP.EventType.spo2` is declared
  (`FeralHUPModels.swift:62`) and never emitted, so the brain's
  `vitals_trend.spo2_*` is always null from the glasses. It is only available
  on demand via the `read_spo2` action.
- **`event_type: "uv"` is relayed and dropped.** `FeralGlassesRelay.swift:178`
  emits it; the brain has no dispatch branch. Either add the branch brain-side
  or stop sending it. The relay's own comment already flags this honestly.
- **`veepoo_wristband` is advertised as a capability**
  (`FeralHUPModels.swift:31`) with no implementation in the relay.
- **Ack timeout hardcodes `5.0`** at `FeralConnectionManager.swift:240` while
  the log message interpolates `self.ackTimeout` at `:244`. They agree today
  and will diverge silently.

---

## Project hygiene, worth an hour

- **Two `.xcodeproj` bundles, and the one named after the app is a husk.**
  `ios/Theora.xcodeproj/` contains only `project.xcworkspace/` with **no
  `project.pbxproj`**, so it cannot be opened or built. `ios/w.xcodeproj/` is
  the real one and is current (100 Swift refs, 42 `Feral` matches). Anyone
  opening this repo picks the wrong one. Either regenerate with `xcodegen`
  (`project.yml` has `name: Theora`) or delete the husk and rename `w`.
- **~7,700 LOC dead, about 13% of the tree.** `AssistantView` +
  `VoiceAssistantManager` (923), `BackgroundVoiceService` (512),
  `GeminiLiveManager` (1,837, reachable only via two `clearHistory()` calls but
  load-bearing because it declares `ConversationMessage` and `MessageRole`),
  vendored `FMDB` (~4,000, excluded from the build and never imported), six
  unreachable `TheoraAPIClient` methods including `sendVoiceConversation`.
- **Auth tokens are in `UserDefaults`** with a comment at
  `Managers/AuthManager.swift:402` saying "Store in Keychain for production" on
  the line above the `UserDefaults` write. `FeralCredentialStore.swift:90-97`
  already does it correctly with `kSecAttrAccessibleAfterFirstUnlock`; copy it.
- **A fabricated bearer token.** `AuthManager.swift:538-541` returns
  `"local_\(userId)"` when refresh fails, which ships as
  `Authorization: Bearer local_<uuid>` and fails server-side with a confusing
  error instead of "session expired". This contradicts the file's own explicit
  "NO FALLBACK" stance.

---

## Do NOT adopt WebRTC

Recorded so it is not relitigated. Theora's own analysis already rejected it
(`docs/voice-agent-sota.md:13`): WebRTC owns its audio device module and fights
the manual HFP/SCO route control glasses require, and
`SharedVoiceAudioEngine.swift:80-125` is exactly that control.

Independent research agrees for different reasons: FERAL already carries Opus so
the bandwidth win is moot, the one published production A/B found WebRTC
*slower* (1,920ms vs 2,060ms median), iOS gives AEC from one line regardless of
transport, and a local-first brain behind a home NAT wants an overlay network
rather than a TURN relay someone has to host forever.

Revisit only if video from the glasses becomes a requirement.

---

## Context you may not have

- **Gemini already works from the phone.** The phone never talks to Gemini;
  `GeminiRealtimeProxy` runs in the brain (`voice/gemini_realtime.py:23`) and
  holds the socket. Provider selection is a brain-side setting.
- **The brain now walks an ordered realtime fallback chain** that terminates at
  local chained, and it is surface-aware: `phone`, `ios`, `iphone`, `ipad`,
  `glasses`, `watch` all default to realtime-first
  (`voice/router.py:62`). So the "iOS must be fast" requirement is policy, not
  something the app has to enforce.
- **The brain's realtime default moved to the mini tier** on cost
  (roughly a third the price per audio token in both directions).
- **`W610/QCSDKDemo/.../VoiceAgent/`** contains a complete chained voice agent
  for glasses (WhisperKit, Apple Speech, Kokoro, an orchestrator, Moshi, a
  Gemini Live client). Worth reading before building anything similar. It also
  contains **two live API keys in `Config.swift:23,29`**; that directory is
  untracked so they are not in git history, unlike P0-1.

---

# Coding-agent picker (added 2026-08-01)

FERAL can now drive external coding agents. This section is what the Theora
app needs to put a picker in the chat UI next to Wellness / Claude Code /
FERAL Brain. Everything below is read from the shipped code.

## What exists brain-side

`feral-core/bridges/` spawns a coding agent as a subprocess and speaks ACP
(Agent Client Protocol, JSON-RPC 2.0 over newline-delimited JSON on stdio).
Verified against real opencode 1.18.10: 1026 streamed events in one run, a real
tool call, permission requests answered both ways, and a file written through
FERAL's own handler.

Four agents, from `bridges/catalog.py:61-92`:

| `agent_id` | `native_acp` | Needs |
|---|---|---|
| `opencode` | true | nothing, single binary, FERAL can install it |
| `claude_code` | false | Node + `@zed-industries/claude-code-acp` |
| `codex` | false | Node + `@agentclientprotocol/codex-acp` |
| `hermes` | true | an existing source checkout, FERAL will not install it |

**Neither Claude Code nor Codex speaks ACP natively.** Verified locally:
`claude` 2.1.220 has no `acp` subcommand, and Codex has an open request for one.
Both are driven through Zed-maintained Node shims, which are themselves the ACP
agent, so the bridge drives them unchanged. That is why `native_acp` is on the
wire: the picker should show whether an agent is a one-step or two-step install.

## The skill surface

Skill id `external_agent`, four endpoints. These are ordinary FERAL tools, so
they reach the phone through the existing `chat_request` / `chat_response`
path. **No new HUP frame is required for v1.**

```
list_agents()
  -> { default_agent, agents: [ { agent_id, display_name, available,
                                  native_acp, binary, install_hint } ] }

run_task(prompt, agent_id?, workspace_dir?, session_handle?, wait_seconds?)
  -> { status, session_handle, agent_id, workspace_dir,
       events[], tool_calls[], text, permission_request? }

respond_permission(request_id, decision, wait_seconds?)
  -> same shape as run_task, plus { answered: { request_id, decision } }

close_session(session_handle, cancel_first?)
  -> { handle, closed }
```

`list_agents` populates the picker. `available` is a real check (binary on PATH
or at the recorded absolute path), so an agent the user has not installed
renders greyed with `install_hint` rather than failing on first use.

## The one behaviour that will surprise you

**`run_task` returns on the first permission question, not at the end of the
turn.** FERAL's tool loop is one call at a time, so a blocking `run_task` could
never be unblocked by the `respond_permission` that unblocks it. The flow is:

1. `run_task` -> `status` indicates a pending permission, `permission_request`
   carries the id and the options
2. UI shows the question, user picks
3. `respond_permission(request_id, decision)` -> the turn continues and returns
   the same shape
4. Repeat if another arrives. Real runs produced **two sequential permission
   requests inside one turn**, so do not assume one.

Decisions use ACP's own vocabulary: `allow_once`, `allow_always`,
`reject_once`. There is no auto-allow anywhere: no broker, broker raised,
timeout, no allow-shaped option, and cancelled session all resolve to
rejection. Grants are namespaced `external_agent:<tool>` so an external
agent's `bash` can never grant FERAL's `bash`.

## What the picker should probably do

- Call `list_agents` on open, render `display_name`, grey out `available:false`
  with `install_hint`.
- Keep `session_handle` per conversation so follow-up turns continue the same
  agent session instead of starting cold.
- Render `tool_calls[]` as they stream, the same way the existing tool chips do.
- Treat a permission question as a blocking modal. The user must answer before
  the turn finishes, and rejecting is a normal outcome, not an error.
- `close_session` on thread switch or when the user picks a different agent.

## Not built yet, do not design against it

- **No HTTP route for permissions.** The approve/deny path today is the
  `respond_permission` tool. If the picker wants a REST button instead, that
  route needs adding brain-side first; say so and it will be.
- **Cross-agent memory is not done.** Each agent session is independent. FERAL
  does not yet summarize what an agent did into its own memory, so asking FERAL
  "what did Claude change earlier" will not work. That work is next; this
  section will be updated when it lands rather than promised here.
- **hermes is untested.** Its code path exists and it speaks ACP, but only
  opencode was exercised against a real binary.
