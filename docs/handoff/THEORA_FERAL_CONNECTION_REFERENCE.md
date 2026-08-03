# Theora <-> FERAL: connection reference and ambient contract

For the agent working on the Theora iOS app. Everything in the "exists"
sections was read out of the running code on 2026-08-03 against HUP
v1.3.0, not from memory. Everything in the "does not exist" sections is
labelled as such, so you do not build against an endpoint that is not
there.

Read `FERAL_CORE_FOR_THEORA_IOS.md` first for the map. This is the wire
detail.

---

## 0. The one thing to check before you start

**`docs/handoff/WORKLOG.md`** tracks what is actually done versus
claimed, including corrections. If something here contradicts it, the
worklog is newer.

**Version matters.** PyPI `feral-ai==2026.8.3` does NOT contain the
latest `main`. Three commits landed after the tag, including a fix where
non-streaming Anthropic turns billed $0. If you are testing against a
pip-installed brain, you are on `2026.8.3`. Against a git checkout, you
are ahead of it.

---

## 1. Transport

Three WebSockets on the brain (`feral-core/api/server.py`):

| Path | Who connects |
|---|---|
| `/v1/session` | clients (web, phone chat) |
| `/v1/node` | devices (Theora glasses, iOS as a node, Mac daemon) |
| `/sync` | federation between brains |

The iOS app is a **node**. Use `/v1/node`.

Every frame is the same envelope (`models/protocol.py`):

```json
{
  "msg_id": "uuid",
  "session_id": "",
  "timestamp_ms": 1234567890123,
  "hop": "client" | "brain" | "daemon" | "skill",
  "type": "<frame name>",
  "payload": { }
}
```

`models/protocol.py` is authoritative. `MESSAGE_TYPES` in that file is
the complete list of 53 frame names mapped to their payload models. If
this document and that file disagree, the file wins.

**Unknown frames must be ignored, never fatal.** The brain adds frames in
minor versions and an older app has to survive them.

### Auth

`APIKeyMiddleware` protects everything except an explicit open-path list.
Phones authenticate with a `phone_bearer` minted during pairing and
accepted on the WS subprotocol (`verify_phone_bearer`, server.py:1234).
Read `_OPEN_PATHS` / `_OPEN_PATH_PREFIXES` before assuming any endpoint
is reachable unauthenticated.

### Handshake

Send `node_register`, receive `node_ack`.

`node_register` payload:
`node_id, node_type, os, platform, manufacturer, model,
firmware_version, capabilities[], skills[]`

`node_ack` payload:
`node_id, session_token, hup_version, heartbeat_ms, server_time,
capabilities, granted_capabilities, denied_capabilities`

**Read `granted_capabilities` and `denied_capabilities`.** They are not
decoration: the brain can refuse a capability you asked for, and acting
as though you have it is how a feature half-works.

---

## 2. Frames that EXIST today, verified

Exact field lists, dumped from the pydantic models.

### Device to brain

```
audio_chunk        encoding, sample_rate, channels, chunk_index,
                   is_final, data_b64
glasses_frame      device_id, timestamp, encoding, data_b64,
                   width, height, source, sequence
vision_frame       (see MESSAGE_TYPES)
sensor_telemetry   node_id, sensor, data, timestamp, source
sensor_batch       node_id, readings, timestamp, source
glasses_status     node_id, glasses_connected, battery_level,
                   glasses_model
biometric          heart_rate_bpm, spo2_pct, accel_xyz, temperature_c,
                   uv_index, gps, inferred_state
device_register    device_id, device_type, name, sensors,
                   firmware_version, battery_pct
voice_session_start stream_id, sample_rate, channels, language_hint,
                   mode, interrupt_policy, camera_linked
```

### Brain to device

```
health_update      node_id, event_type, ts, data
                   event_type is "health_summary" | "vitals_trend"
                   data: sources, window_days, note, readings, series
transcript         text, is_partial, confidence, role, item_id,
                   previous_item_id, seq
tool_start / tool_result / stream_delta / text_response /
refusal / permission_request / budget_exceeded / error / node_bye
```

### Two that are easy to get wrong

**`health_update`** is the mirror of the daemon's `device_event`: same
`{node_id, event_type, data, ts}` vocabulary, opposite direction. It
exists because Theora's client decoded nine frame types and had no health
frame, so Whoop and glasses data only ever arrived as English prose
inside a chat reply, which an app cannot render as a card. Payload detail
is in `THEORA_IOS_TASKS.md`.

**`tool_result` now carries `error_code`.** A refused tool call is not a
crash: `plan_mode_blocked`, `policy_denied`, `pending_approval`. Render
those as a held boundary, not an error. Key off the code, never the
message text, which is user-facing copy that changes.
`feral-client-v2/src/components/ToolCallCard.jsx` is the reference
implementation.

---

## 3. Ambient recording

### What exists: nothing brain-side

Checked, not assumed. There is no ambient recording pipeline, no
transcript store, no ambient session model. `grep` for
`ambient_record|ambient_session|AmbientRecord` returns nothing.

`api/routes/ambient.py` **is not this**. It serves the ambient *surface*
(briefing, next event, snapshot, wind-down, wake word), a
glanceable-information API. Two different features share the word.

`audio_chunk` exists but is wired to the **voice** path: on `/v1/session`
it goes to the Gemini proxy or `voice_router.handle_audio_from_client`,
which is turn-based conversation. Sending continuous ambient audio down
that frame today would feed the assistant, not a recorder.

**So: do not build iOS against an ambient endpoint. It is not there.**

### The agreed architecture

1. Glasses capture audio.
2. iOS relays it. It does not transcribe, and does not decide what is
   interesting.
3. The brain runs STT **locally** with whisper.cpp. Measured on Apple
   Silicon: 109-162 ms per utterance, 0.022x realtime. Not aspirational.
4. The transcript never leaves the machine. Only the configured LLM
   summarises, and only text.

The privacy property is the whole point. If a change would send raw audio
or the verbatim transcript to a cloud endpoint, it breaks the reason the
feature exists. Raise it rather than working around it.

### Division of labour

| Piece | Owner | State |
|---|---|---|
| Glasses capture, BLE to phone | Theora iOS | yours |
| iOS relay to brain | Theora iOS | yours, blocked on the frame below |
| New HUP frame for ambient audio | brain | **not built** |
| Ambient session lifecycle, storage | brain | **not built** |
| Local whisper.cpp STT | brain | engine verified, pipeline not built |
| Summarisation | brain | **not built** |

### What the brain needs to add, so you can plan against it

Not built yet. Proposed shape, deliberately mirroring `audio_chunk` so
the encoder work on your side is reusable:

```
ambient_start    node_id, session_hint, sample_rate, channels, encoding
ambient_chunk    node_id, ambient_id, chunk_index, is_final, data_b64
ambient_stop     node_id, ambient_id, reason
```

and brain to device:

```
ambient_status   ambient_id, state ("recording"|"stopped"|"refused"),
                 reason, seconds_captured
```

**Treat this as a proposal, not an API.** If you need it to differ, say
so before either side builds. What matters is that ambient audio gets its
own frame rather than reusing `audio_chunk`, because that frame already
means "talk to the assistant".

### Consent and control, which are not optional here

Continuous capture needs, at minimum:
- an explicit start that the user performs, never an automatic one
- a visible indicator whenever capture is live
- a stop that takes effect immediately and is confirmed by the brain
- mute that fails safe toward muted (the brain already has a mute ledger
  that survives reconnect; the iOS half is in `THEORA_IOS_TASKS.md` and
  is not done)

If glasses capture audio of people who did not consent, that is a legal
question in many jurisdictions, not just a product one. Worth settling
before the pipeline exists rather than after.

---

## 4. Whoop and health

Brain-side is done. Whoop was previously fetched into a transient dict so
no history existed; it is now mirrored into `biometric_samples` with a
400-day horizon, while live sensors still prune at 35 days. Delivery to
the app is the `health_update` frame above.

Not done: Oura sync. Its OAuth was broken (client built without the
manager) and is fixed, but only Whoop actually syncs.

---

## 5. Voice

An ordered provider chain terminating at a fully local chained path, and
it is surface-aware. `phone`, `ios`, `iphone`, `ipad`, `glasses`, `watch`
default to **realtime-first** because latency is the product on a
wearable. Desktop and web default to local-first.

Send the right `surface` and you get the right default. Do not hardcode a
provider on the device.

Streaming is now ON by default (v2026.8.3+). The chained voice pipeline
taps `stream_delta` to start speaking sentence 1 while the model is still
writing sentence 2, so if you suppress those frames you lose that.

---

## 6. Conventions that will save a review round

- **No em dashes** anywhere: code, comments, commit messages, docs.
- **Never `git add -A`.** Stage explicit paths. A previous session
  committed a directory containing real credentials that way.
- **Never touch `~/.feral`** in a test. Point `FERAL_HOME` at a temp dir.
- **Never use real API keys in tests.** Scrub `OPENAI_API_KEY` and
  `ANTHROPIC_API_KEY` from child environments.
- The suite runs under `pytest-randomly`, so order changes every run. If
  something passes alone and fails in the suite, suspect shared state.
  `tests/conftest.py` has one list, `_SHARED_STATE_RESETTERS`, naming
  every global that must be reset.
- A test that silently downloads a model is not a unit test. One did, and
  it took CI from 4 minutes to 35.

---

## 7. When you need something from the brain

Open a task doc in `docs/handoff/` describing the frame you need and its
payload shape, rather than inventing a workaround on the device.
`THEORA_IOS_TASKS.md` and `FERAL_COMPANION_IOS_TASKS.md` are the format.

The rule that matters: **never invent a frame on the device.** If iOS
needs something the brain does not send, the fix is a payload model in
`protocol.py` plus a brain-side emitter, not a side channel and not
parsing prose out of a chat reply.
