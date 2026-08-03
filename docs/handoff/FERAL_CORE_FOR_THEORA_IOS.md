# feral-core, for the agent building Theora iOS

Orientation, not a reference. The goal is that you can decide **where a
change belongs** without reading 68k lines. Where a real spec already
exists this points at it rather than paraphrasing it, because a
paraphrase goes stale and then lies.

Written 2026-08-02 against HUP v1.3.0.

---

## The one paragraph version

The **brain** (`feral-core/`, FastAPI + Python) owns all state, all LLM
calls, all skills and all persistence. **Nodes** (Theora glasses, the
iOS app, the Mac daemon) own sensors and I/O and hold no durable truth.
They talk over one WebSocket using HUP, a typed JSON frame protocol.
The iOS app is a node. It should be thin: capture, render, relay. If you
find yourself wanting to store or decide something on the phone, that is
usually a sign the brain is missing a frame.

---

## Where things live

| Path | What it is |
|---|---|
| `feral-core/api/server.py` | HTTP + WebSocket entrypoint. The three sockets are `/v1/session` (clients), `/v1/node` (devices), `/sync` (federation). |
| `feral-core/models/protocol.py` | **The wire contract.** `HUP_VERSION`, every payload model, and `MESSAGE_TYPES` mapping frame name to model. |
| `feral-core/agents/` | Orchestrator, multi-agent loop, tool runner, plan mode. |
| `feral-core/skills/` | Capabilities. `manifests/*.json` declare them, `impl/` implements them, `executor.py` runs them. |
| `feral-core/voice/` | STT, TTS, realtime proxies, the fallback router. |
| `feral-core/hardware/` | Device manifests and the mesh/protocol layer. |
| `feral-core/security/` | Safety policy, vault, env jail, sandbox. |
| `feral-client-v2/` | The web client. Useful as a **reference implementation** of the frames, since it already handles most of them. |
| `feral-nodes/HUP_SPEC.md` | The protocol spec. Read this before writing frame code. |

---

## The protocol, and the only rule that matters

`models/protocol.py` is authoritative. `MESSAGE_TYPES` is the complete
list of frame names and their payload models. As of v1.3.0 there are 53,
including the ones you already decode plus `health_update`,
`sensor_telemetry`, `sensor_batch`, `glasses_status`, `vision_frame`,
`permission_request`, `refusal` and `budget_exceeded`.

**The rule: never invent a frame on the device.** If iOS needs something
the brain does not send, the fix is a payload model in `protocol.py` and
a brain-side emitter, not a side channel or a parsed prose reply. That
is not a style preference. It already went wrong once: Theora's client
decoded nine frame types and had no health frame, so Whoop and glasses
data reached the app only as English sentences inside a chat reply,
which an app cannot render as a card or a chart. `health_update` exists
now because of that.

**Unknown frames must be ignored, not fatal.** The brain adds frames in
minor versions and an older app has to survive them.

---

## What you can rely on the brain for

Do not reimplement these on the phone.

- **Persistence.** Biometrics land in `biometric_samples` with a 400-day
  horizon for synced sources (Whoop) while live sensors prune at 35 days.
- **Health data.** `health_update` carries `{node_id, event_type, data,
  ts}` where `event_type` is `health_summary` (current values) or
  `vitals_trend` (a dated series). Both use the same canonical reading
  shape, so one renderer handles both. Payload details are in
  `THEORA_IOS_TASKS.md`.
- **Tool safety.** Plan mode, autonomy tiers and approvals are enforced
  in `skills/executor.py`, at the chokepoint every caller passes
  through. The device does not get a vote and should not try to.
- **Refusals.** A declined tool arrives as a tool result carrying
  `error_code` (`plan_mode_blocked`, `policy_denied`,
  `pending_approval`). Render these as a held boundary, not as an error.
  The web client's `ToolCallCard.jsx` is the reference: it keys off the
  code, never off the message text, because the text is user-facing copy
  that changes.

---

## Ambient recording, since that is your piece

The agreed shape, so the halves meet:

1. Glasses capture audio.
2. iOS relays it to the brain. It does not transcribe and does not
   decide what is interesting.
3. The brain runs STT **locally** with whisper.cpp. Verified on Apple
   Silicon at 109-162 ms per utterance (0.022x realtime), so this is not
   aspirational.
4. The transcript never leaves the machine. The configured LLM
   summarises text only.

The privacy property is the point of the design: raw audio and the
verbatim transcript stay local. If a change would send either to a cloud
endpoint, it breaks the feature's reason for existing, so raise it
rather than working around it.

`api/routes/ambient.py` currently serves the ambient *surface* (briefing,
next event, snapshot, wind-down, wake-word). It is not the recording
pipeline. The recording brain side is not built yet and is mine.

---

## Voice, briefly

There is an ordered provider chain that terminates at a fully local
chained path, and it is surface-aware. `phone`, `ios`, `iphone`, `ipad`,
`glasses` and `watch` default to **realtime-first** because latency is
the product on a wearable; desktop and web default to local-first.

Send the right `surface` and you get the right default. Do not hardcode
a provider on the device.

Mute is a brain-side ledger that survives reconnect and fails safe
toward muted. iOS has not implemented its half yet; that task is in
`THEORA_IOS_TASKS.md`.

---

## Auth

`APIKeyMiddleware` in `api/server.py` protects everything except an
explicit open-path list. Node sockets register with `node_register` and
get `node_ack`. Read `_OPEN_PATHS` / `_OPEN_PATH_PREFIXES` before
assuming an endpoint is reachable unauthenticated.

---

## Conventions that will save you a review round

- **No em dashes** anywhere: code, comments, commit messages, docs.
- **Never `git add -A`.** Stage explicit paths. A previous session
  committed a `.regress_home/` containing real credentials that way.
- **Never touch `~/.feral` in a test.** Point `FERAL_HOME` at a temp dir.
- **Never use real API keys in tests.** Scrub `OPENAI_API_KEY` and
  `ANTHROPIC_API_KEY` from child environments.
- The suite runs under `pytest-randomly`, so test order changes every
  run. If something passes alone and fails in the suite, suspect shared
  state, not flakiness. `tests/conftest.py` has one list,
  `_SHARED_STATE_RESETTERS`, naming every global that must be reset.

---

## When you need something from the brain

Open a task in `docs/handoff/` describing the frame you need and the
payload shape, rather than inventing a workaround on the device. The two
existing task docs (`THEORA_IOS_TASKS.md`,
`FERAL_COMPANION_IOS_TASKS.md`) are the format.

`docs/handoff/WORKLOG.md` tracks what is asked versus done across this
whole effort, including known-open items and corrections. Read it before
assuming something is finished.
