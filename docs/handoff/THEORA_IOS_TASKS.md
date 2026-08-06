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

---

# Health frame (added 2026-08-01, v2026.8.2)

Theora's HUP client decodes nine frame types and none of them carry health data,
so the only way Whoop or glasses history reached the app was as English prose in
a chat reply. There is now a real frame.

## The frame

Type `health_update`. Envelope is the **exact mirror of `device_event`**
(HUP_SPEC 5.4), so no new vocabulary: `{node_id, event_type, ts, data}`.
Registered in `models/protocol.py` as `HealthUpdatePayload`.

`event_type` is `health_summary` or `vitals_trend`. **Both carry the same
reading shape, so one renderer handles both.**

```json
{
  "hup_version": "1.3.0",
  "type": "health_update",
  "ts": 1785391964.235,
  "payload": {
    "node_id": "feral-iphone-1",
    "event_type": "health_summary",
    "ts": 1785391964.235,
    "data": {
      "sources": ["whoop"],
      "window_days": 0,
      "note": "",
      "readings": [
        {"metric": "recovery_score", "label": "Recovery", "value": 66.0,
         "unit": "%", "precision": 0, "category": "recovery",
         "source": "whoop", "ts": 1785391964.235},
        {"metric": "resting_hr", "label": "Resting Heart Rate", "value": 54.0,
         "unit": "bpm", "precision": 0, "category": "vitals",
         "source": "whoop", "ts": 1785391964.235},
        {"metric": "hrv", "label": "HRV", "value": 78.5,
         "unit": "ms", "precision": 0, "category": "vitals",
         "source": "whoop", "ts": 1785391964.235},
        {"metric": "sleep_hours", "label": "Sleep", "value": 8.0,
         "unit": "h", "precision": 2, "category": "sleep",
         "source": "whoop", "ts": 1785391964.235},
        {"metric": "strain", "label": "Strain", "value": 12.4,
         "unit": "", "precision": 1, "category": "activity",
         "source": "whoop", "ts": 1785391964.235}
      ],
      "series": []
    }
  }
}
```

`vitals_trend` uses the same envelope with `readings` empty and `series`
carrying charts:

```json
"series": [
  {"metric": "recovery_score", "label": "Recovery", "unit": "%",
   "precision": 0, "category": "recovery", "source": "whoop",
   "points": [{"ts": 1785219164.235, "value": 64.0},
              {"ts": 1785391964.235, "value": 66.0}]}
]
```

## Three things that will bite you if you skip them

1. **`precision` is a display hint only.** `value` keeps the source's own
   precision. Round at render time, never on store. A bug in the brain did
   exactly this and persisted HRV 78.5 ms as 78.0 ms, permanently.
2. **`source` ids are carried verbatim and never translated.** Whoop is
   `whoop`, the glasses are `jw_health_glasses`. The relay already sends
   `jw_health_glasses` (`FeralHUPModels.swift:25`), so this matches. Do not
   introduce a `glasses` alias; a rename here would be a sixth vocabulary.
3. **`points[].date`** (`YYYY-MM-DD`) is present only when the producer had a
   day label. Do not require it.

## Two ways to get it

- **Push**, over the node socket you already hold. Add `health_update` to
  `FeralInboundParser.parse` alongside the other cases.
- **Pull**, `GET /api/health/frame?event_type=&days=&push=`. Now on the
  phone-bearer GET allowlist, so the existing pairing bearer works. It returns
  the frame and also pushes it to every connected node, each copy stamped with
  that node's own `node_id`.

## Caveats, stated rather than discovered later

- **No real Whoop response was recorded.** Fixtures are synthetic, written
  against the documented v1 field names and the parsing already in
  `health_platforms.py`. If the live API differs from what that client assumes,
  the fixtures inherit the same error.
- **Oura is not synced.** Only Whoop. Oura's client exists and its OAuth was
  just fixed, but the sync is Whoop-only.
- Whoop history is mirrored for 400 days. Live sensor samples still prune at 35.

---

# Pairing and remote access (added 2026-08-05)

Read this before touching anything under `ios/Theora/Feral/`. Some of it
has already been done for you, in your repo, and redoing it will create
conflicts.

## What happened while you were gone

Your session died mid-sentence on 2026-08-05 at about 07:00 UTC when the
laptop lost power. Its last recorded commitment was, verbatim:

> I'll wire it the moment you tell me it's in.

That was about `/api/wiki/ingest/text` not being on the phone-bearer
allowlist, which blocked ambient transcripts from reaching the brain.
**It is in.** It shipped as `cd1f61b7a` on 2026-08-05. See the design
conflict at the bottom before you build against it, though.

Separately, a new user installed FERAL, scanned a pairing QR, and got an
infinite "Connecting..." spinner. That turned out to be four independent
defects stacked, two brain-side and two app-side. All four are now fixed.

## What has already been done IN YOUR REPO. Do not redo it.

Branch `fix/feral-pairing-2026-08`, commit `558b7fd`, three files:

| File | Change |
|---|---|
| `ios/Theora/Info.plist` | Added `NSLocalNetworkUsageDescription`, `NSBonjourServices` (`_feral._tcp`), and `NSAppTransportSecurity` with `NSAllowsLocalNetworking`. Deliberately NOT `NSAllowsArbitraryLoads`. |
| `ios/Theora/Feral/FeralConnectionManager.swift` | `maxReconnectAttempts = 6` and a terminal `.error` state; `teardownSocket` bumps `generation`; `autoConnectIfConfigured` resets `reconnectAttempt`. |
| `ios/Theora/Feral/FeralPairingPayload.swift` | `parsePairLink` decodes the brain's new `p=` identity blob, so `brainId` and `name` survive a QR scan. |

The Info.plist keys were the hard blocker. Before them the app could not
reach a LAN brain at all: iOS 14+ needs `NSLocalNetworkUsageDescription`
to even present the local-network prompt, and cleartext `ws://` to a
private address needs an ATS exception. Neither existed. No brain-side
configuration could have fixed that.

Your other 48 uncommitted entries on `main` are untouched. I committed
only those three files, on a separate branch, and pushed nothing.

## What changed on the brain that you need to know

All on branch `fix/pairing-access-2026-08` in the ASOS repo, **not yet
released**. Spelling out what that means in practice, because it is easy
to read the list below as describing the brain you can actually probe:
the brain running on your machine came back up on released code after the
reboot, so **none of items 1 to 5 are live right now.** A live probe of
`/api/devices/pair/url` returns `?t=<token>` with no `p=` at all, and the
1001 frame is not emitted. Anything you build against them cannot be
exercised end to end until that branch ships. Verify with a probe before
concluding your code is wrong.

1. **`/v1/node` now sends an HUP error frame (code 1001, name
   `unauthorized`) before `ws.close(4003)`.** Previously a rejected
   credential produced a successful upgrade followed by a bare close,
   which is indistinguishable from a dropped network. Your
   `FeralConnectionManager` already had an `err.code == 1001` branch that
   set a terminal state; it was dead code because nothing ever emitted
   1001. It is live now. Your own `docs/theora-feral-findings.md` called
   this out: treat "connected then immediately closed" as an auth
   failure, not a transport failure.
2. **The pair URL carries `&p=<base64url-json>`** holding `brain_id`,
   `mode`, `expires`, `device_id`, `name`. The QR encodes only the URL
   string, so before this every field except the token was structurally
   undeliverable to anything that scanned it. Absent or malformed `p=` is
   not an error; older brains do not emit it and the token alone pairs.
3. **`POST /api/config/update` now returns 400** for
   `access.pairing_mode` and `network.bind_host`. The new writer is
   `POST /api/access/mode` with `{"mode": "..."}`. If any iOS code writes
   those keys, it breaks.
4. **`device_event` with `event_type: "uv"` is now accepted.** The
   glasses relay emits it and the brain was dropping every reading at
   debug level. No app change needed; it just works now.
5. **A brain configured for LAN but still bound to loopback now refuses
   to emit a QR**, returning 409 with remediation text, instead of
   handing out an address nothing listens on. Expect to see that 409 in
   testing. It is the fix, not a regression.

## What is left for you

From the approved plan's iOS section. Numbering is the plan's.

- **S3, multi-endpoint credential store.** `FeralCredentialStore` holds a
  single `feral_brain_url` String; re-pairing overwrites it and there is
  no LAN/remote pair. Replace with a JSON array of
  `{kind, url, priority}` keyed by `brain_id`, reading the old key once
  as a migration source. This is a prerequisite for S5.
- **S5, candidate racing.** Try endpoints in priority order with a 3s
  connect timeout each. Sequential, not parallel, for battery.
- **S7, render the connection state.** `reconnectAttempt` is `@Published`
  and rendered nowhere. `BLEConnectionManager` already does this well;
  copy its `"Reconnecting (n/10)..."` shape.
- **S8, do not dismiss the pair sheet on start.** `FeralPairBrainView`
  calls `dismiss()` immediately after `manager.connect(...)` without
  awaiting the result, so a pairing that will never connect reports
  success. Keep the sheet up until the first `node_ack` or a terminal
  failure. It also discards the `@discardableResult` from
  `FeralPairingService.complete`.
- **S10, Bonjour discovery.** `NWBrowser` on `_feral._tcp`. The plist key
  is already declared, and mDNS is **live now**: `dns-sd -B _feral._tcp`
  returns real instances. But the TXT record carries only `version`,
  `name`, and `hostname` (`services/mdns.py:115-119`). **There is no
  `brain_id` in it.** So discovery works and acting on a discovery does
  not: you cannot match a discovered service back to a stored credential
  by brain id, which is exactly what an S3 brain-id-keyed store wants.
  Hold S10 until the TXT records carry `brain_id`, or it is discovery you
  cannot act on.
- **S11, register the `feral://` URL scheme.** `CFBundleURLTypes` exists
  in the plist already for Google Sign-In. Add a second dict to the
  existing array; do not create a second `CFBundleURLTypes` key.
- **S12, SPKI pinning.** Gated on the brain shipping a `tls_pin` field.
  It does not yet. Do not build against it.

## Verify all of this yourself before believing it

This document was written on 2026-08-05 and will rot. Check, do not
assume:

```bash
# Did the brain-side work actually land, and is it released?
cd /Users/mahmoudomar/Desktop/thoera-mac/ASOS
git log --oneline origin/main..fix/pairing-access-2026-08
git tag --sort=-creatordate | head -3

# What does a pair payload actually look like right now?
curl -s localhost:9090/api/devices/pair/url?name=probe | python3 -m json.tool

# What is already committed in your own repo?
cd /Users/mahmoudomar/Desktop/Theora-backend-ML
git log --oneline main..fix/feral-pairing-2026-08
git diff main...fix/feral-pairing-2026-08 --stat
```

For anything about brain behaviour, read the source in
`ASOS/feral-core`, not this file. `api/routes/devices.py` is the pair
payload, `api/server.py` is the `/v1/node` handler and the auth
middleware. Exclude `feral-core/build/lib/**` from every grep: it is a
stale mirror of nearly every source file and reading it will tell you
confident lies.

## Traps that will cost you an hour each

- **Never run `xcodegen generate`.** `ios/project.yml` declares
  `name: Theora`, so it emits `Theora.xcodeproj` and rewrites the
  scheme's `ReferencedContainer`. Somebody already did this once and
  hand-reverted it; those reverts are sitting uncommitted in
  `project.pbxproj` and `Theora.xcscheme`. Regenerating buries them.
  `project.yml` has also diverged from the pbxproj (`DEVELOPMENT_TEAM`,
  `CODE_SIGN_IDENTITY`, three extra framework search paths) and should be
  treated as stale documentation, not source of truth.
- **Build `ios/w.xcodeproj`, never `ios/Theora.xcodeproj`.** The latter
  has no `project.pbxproj` and cannot open.
- **The simulator can never work.** `RTKOTASDK` and `VeepooBleSDK` ship
  device slices only. The only verification available without hardware:

  ```bash
  cd /Users/mahmoudomar/Desktop/Theora-backend-ML/ios
  xcodebuild build -project w.xcodeproj -scheme Theora \
    -configuration Debug -destination 'generic/platform=iOS' \
    CODE_SIGNING_ALLOWED=NO
  ```

  For plist-only edits, `plutil -lint ios/Theora/Info.plist` is instant.
- **`Info.plist` is hand-maintained, not generated.** `project.yml` has
  no `info:` stanza, `GENERATE_INFOPLIST_FILE` is NO, and there are no
  `INFOPLIST_KEY_*` build settings. Edit the plist directly.
- **`FeralGlassesRelay.swift` lines 88 and 122 have uncommitted changes**
  from the vitals-owner refactor. Do not clobber them. Every other file
  under `Feral/` and `Views/Feral/` is at HEAD apart from the two I
  committed.
- **Do not push.** Per this repo's `CLAUDE.md`, iOS work is local only.
- **No em dashes** anywhere in code, comments, or commit messages.

## Genuinely needs hardware, and cannot be resolved by reading

- The Local Network permission prompt: fresh install, physical device,
  iOS 17 and 18. The Simulator does not model Local Network privacy.
- ~~Whether ATS blocks cleartext to an RFC1918 literal~~ **Answered, and
  the original framing here was backwards.** Per Apple DTS, ATS is *not
  applied at all* to requests targeting an IP address on iOS 10 and
  later. `NSAllowsLocalNetworking` exists for unqualified host names and
  `.local` names, not as cover for literals. So `ws://192.168.x.x` is
  not an ATS question, candidate priority does not need to change, and
  `CANDIDATE_ORDER` stays `lan` first.

  Both plist additions still earn their place: `NSAllowsLocalNetworking`
  covers the `.local` names Bonjour hands back in S10, and
  `NSLocalNetworkUsageDescription` is a different mechanism entirely.

- **Local Network privacy on current iOS, which is the real open
  question.** ATS and Local Network privacy are separate. Whether the
  permission prompt appears and the connection completes on iOS 26 is
  unverified, and there is at least one report of cleartext local-network
  failures specific to iOS 26.5 on physical devices
  (firebase-ios-sdk#16406). Treat the ATS answer as settled enough to
  design against and still confirm this half on a device.
- AP/client isolation, the hotel-wifi case the whole remote tier exists
  for. The brain cannot detect it and says so honestly in its diagnostic.

## Do not trust `p.brain_id` for a security decision

The `p=` blob in the pair URL is **not signed**. Anyone can generate a QR
code containing any `brain_id` they like, and nothing in it proves the QR
came from the brain it names.

That is fine for what it does today: a display label, and a hint about
which stored endpoint set to try first. It stops being fine the moment it
gates anything. In particular, S3 (multi-endpoint credential store keyed
by `brain_id`) must not use it to answer "is this the same brain I paired
with before, so may I reuse or overwrite its credentials?" A forged QR
would answer yes.

The real proof of identity is the pairing exchange itself: the token is
minted by the brain and verified against it over the network. Key on
that, not on a self-declared string in a QR.

If a verifiable brain identity is genuinely needed on the app side, say
so and it can be signed with the Ed25519 key that Stage 2 introduces for
the relay. That is not built yet. Until it is, treat `brain_id` as
untrusted input.

## One open design conflict, yours to settle

Your own `docs/theora-feral-findings.md:70` evaluated
`/api/wiki/ingest/text` for ambient transcripts and **rejected** it as
"document-shaped, no speaker or timestamps", proposing a
`transcript_ingest` HUP frame instead carrying `session_id`, ordered
`segments[{text, started_at, ended_at, speaker, confidence}]`,
`source: "ios.ambient"`, and `is_final`.

That endpoint is now reachable from the phone. But the objection was
about **shape**, not availability, and it still stands. So "the allowlist
landed" does not settle the question. Decide explicitly:

- ship against `/api/wiki/ingest/text` now and accept losing speaker and
  timestamps, or
- ask for the `transcript_ingest` frame brain-side and wait.

The seam on your side is already built and waiting either way:
`ambient_segments.summarized` is written on insert, indexed, and read by
nothing. That index exists for exactly one purpose, selecting segments
not yet shipped upstream.
