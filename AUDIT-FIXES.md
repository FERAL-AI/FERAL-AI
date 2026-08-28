# AUDIT-FIXES

Defects found in the 2026-08-11 audit of `d15645cd4` (v2026.8.8). Each entry below carries its own evidence and its current status.

Read `CLAUDE.md` first — it documents traps that will otherwise corrupt your measurements.

---

## Working agreement

These findings were verified once, on one commit. **They are leads, not facts about your working tree.** The tree may have moved. Some may already be fixed. At least one first-pass claim in this audit was refuted only because a second agent re-ran the command.

For every item below, in this order:

1. **Re-verify.** Open the cited `file:line`. Confirm the defect is present as described. If it is not, record that in this file under the item and move on. Do not fix something you have not seen.
2. **Reproduce.** Make the failure observable — a script, a test, a log line, a `curl`. If you cannot make it fail, you do not understand it yet. Say so rather than guessing.
3. **Write the failing test first.** It must fail against current `main` for the stated reason. A test that passes before your fix proves nothing.
4. **Fix the cause, not the symptom.** If a broad `except` is hiding the error, the fix is usually both: narrow the handler *and* correct the call.
5. **Prove it.** The new test passes, `make test` still passes, and `cd feral-core && ruff check --select=E,F,W --ignore=E501,E402,F401,W291,W293 .` is clean.
6. **Check for siblings.** Every defect here is a class, not an instance. Grep for the same shape elsewhere before closing. Signature changes in particular: grep every call site.
7. **Record the outcome** in this file — `FIXED` with the commit, `NOT REPRODUCIBLE` with what you found, or `DEFERRED` with why.

Hard rules:

- One item per commit. No drive-by changes.
- Do not refactor adjacent code because it looks wrong. Note it and move on.
- Do not trust a green test suite as evidence — see trap 3 in `CLAUDE.md`.
- If a fix requires changing a public signature or wire format, stop and ask. `models/protocol.py` is consumed by four other languages.

---

## P0 — do these first

### F-01 · Scheduled federated sync has never worked

**Status:** FIXED — see commit below.

**Re-verified 2026-08-11.** Present exactly as described. `SyncEngine.sync_with_peer`
is keyword-only after `peer_id` with no `passphrase`; `sync_scheduler.py:240` passed
one; `inspect.signature(...).bind(None, "peer-1", passphrase="x")` raises
`TypeError: got an unexpected keyword argument 'passphrase'`.

**Provenance correction.** The audit attributes the removal to `ba55caf4d`.
`git show ba55caf4d -- feral-core/memory/sync.py` shows no change to `passphrase`
or to `sync_with_peer` in that commit. The parameter's removal is not evidenced
there. This does not change the fix, but the citation should not be repeated as fact.

**Decision: dropped the kwarg, did not thread it through.** The engine already has
the better value. `_handshake_and_exchange` sends the module-level
`memory.sync.SYNC_PASSPHRASE`, which `ensure_sync_passphrase()` resolves at boot
(`api/state.py:1163`) as env, then vault, then freshly generated and persisted.
The scheduler's own `_passphrase()` read `os.environ` alone, so threading it would
have sent an empty passphrase on any install whose secret lives in the vault, which
is the normal case after v2026.5.38. Restoring the parameter would have created a
second, weaker source of truth for a value the handshake already reads.

`_passphrase()` was removed with its only caller, and replaced by a comment saying
why, so nobody threads it back in.

**Done-when, all three met:** the contract test binds the scheduler's call against
the real `SyncEngine` signature; `_StubEngine`'s signature is asserted against the
real one so it cannot drift again; and `_sync_one_peer` now catches `TypeError`
separately as `internal_error`, logged with a traceback, because sharing the
`exception` bucket with peer failures is precisely what hid this.

New tests fail 4/6 against the unfixed source and pass 6/6 after.

**Siblings found — each needs its own item, not fixed here.** `mypy
--ignore-missing-imports memory/` surfaces the same `call-arg` class elsewhere.
The worst is verified:

- `skills/impl/agentic_computer_use.py:272` calls
  `LLMProvider(provider=..., model=..., api_key=...)`, but `LLMProvider.__init__`
  takes `(self)` and accepts none of them. It sits inside `except Exception`, which
  logs a warning and returns `None`, so the VLM for computer-use has never
  initialised. Same shape as F-01: wrong kwargs, swallowed by a broad handler.
- `skills/registry.py:120` and `:329` — missing positional `skill_id` for `BaseSkill`.
- `mcp/registry.py:267` — missing named argument `url` for `MCPServerConfig`.

This is the strongest available argument for S-1: the type checker finds this class
mechanically, and it is the class that hid for 40 releases.

Original finding (superseded by the record above, kept for reference):

```
feral-core/memory/sync.py:1202            def sync_with_peer(self, peer_id, *, max_attempts, connect_timeout, handshake_timeout, backoff_base)
feral-core/memory/sync_scheduler.py:240   self.engine.sync_with_peer(peer_id, passphrase=_passphrase())
feral-core/tests/test_sync_scheduler.py:43  async def sync_with_peer(self, peer_id: str, passphrase: str = "")
```

---

### F-16 · The computer-use VLM has never initialised

**Status:** FIXED — commit below.

**Re-verified.** `LLMProvider.__init__` takes `(self)`; the call passed three kwargs.
Confirmed by `inspect.signature`, and mypy reported all three as `call-arg`.

**The audit's question answered:** there is no lying test double here. Nothing under
`tests/` so much as names `_get_vlm` — the path had zero coverage. Unlike F-01, this
hid through absence rather than through a drifted stub.

**Fix.** `LLMProvider()` then `await llm.switch_provider(provider, model=, api_key=)`,
which is the real configuration API. `switch_provider` is async, so `_get_vlm` is now
async; its single caller was already inside `async def _execute_task`.

`FERAL_VLM_PROVIDER` and `FERAL_VLM_MODEL` are documented knobs that were read into
locals and then discarded with the TypeError, so neither has ever taken effect. A test
now pins both reaching the provider.

**"Not configured" vs "failed to construct" are now separate.** The missing-key case
returns `None` silently and early, because the caller already renders an actionable
503 for it. Everything after that point runs with a key present, so a failure there is
never a configuration problem, and it logs at warning with `exc_info=True` saying so.
The old code collapsed both into one silent `None`, which is why a dead capability
presented as "Set OPENAI_API_KEY" to users who had already set it. That message is
the reason this was never reported as a bug.

New tests fail 5/6 against the unfixed source and pass 6/6 after.

**Siblings:** every other `LLMProvider(` construction in the tree is zero-arg. With
this fixed, the real `call-arg` count is **zero**; the three remaining are the false
positives documented in `mypy-baseline.txt`.

Original finding follows.

```
feral-core/skills/impl/agentic_computer_use.py:272   LLMProvider(provider=..., model=..., api_key=...)
feral-core/agents/llm_provider.py:494+13             def __init__(self):
```

Independently confirmed: `LLMProvider.__init__` takes `(self)` and accepts none of those three keyword arguments. The call raises `TypeError`, which the surrounding `except Exception` at `:273` catches, logs at warning level, and converts into `return None`. The caller treats `None` as "no VLM configured", so the feature degrades silently rather than failing.

This is the same shape as F-01 — wrong kwargs, swallowed by a broad handler — but the consequence is larger: an entire capability is dead, and the log line reads like a missing API key rather than a bug.

**Done when:** the call matches the real constructor, a test asserts the VLM actually initialises, and the handler distinguishes "not configured" from "failed to construct". Check whether `agentic_computer_use` has any passing test that would have caught this; if it does, the test double is lying the way `_StubEngine` was.

---

### F-02 · Input validation lives on the client, not the server

**Status:** FIXED — see the record below. The block was cleared by an owner
decision, recorded here so it is not re-litigated:

- **Scope: every model in `models/protocol.py`, inbound and outbound.** The
  owner was shown that roughly half are outbound and that constraining those
  buys no security while costing cross-language churn, and chose all of them
  anyway. The real count is **59 models, not 53** — the audit's 53 counted
  only classes named `*Payload`, missing `FeralMessage`, `AttachmentRef`,
  `HealthReadingModel`, `HealthSeriesModel` and `HealthUpdateDataModel`.
- **Behaviour on violation: REJECT, never clamp.** The 1003 error frame at
  `api/server.py:2200` already keeps the socket alive and names the field.

**What landed.** All 59 models now carry at least one bound. Of the 236 fields
whose type can take one (`str` / `int` / `float` / `list`, excluding `bool`,
`Literal` and `dict`, which are already closed by their type), **177 are
constrained and 59 are deliberately bare**, each with the reason written at
the field. The bounds are structural only: identifier lengths, non-negative
counters and durations, the pixel range, the lat/lon domain, list caps, and a
decoded-size cap on the one base64 blob that has a documented one. No semantic
ceiling was invented.

`GlassesFramePayload` and `DeviceAnnouncePayload` mirror the node SDK
**exactly** — `device_id` 1-128, `width`/`height` 1-8192, `sequence >= 0`, a
512 KiB decoded cap, `rssi_dbm` in [-127, 20] — and no stricter, so a frame
the SDK builds can never be refused by the brain.
`tests/test_protocol_field_constraints.py` compares the two models' pydantic
metadata field by field, so the SDK docstring's "mirrors the brain" claim is
now enforced rather than asserted.

**Three bounds that looked obviously correct and are wrong.** Each is now a
passing test so nobody "completes" the sweep by adding them:

- `LocationUpdatePayload.accuracy_m` / `heading_deg` / `speed_mps` — a `ge=0`
  here would reject a large share of real iPhone fixes. CoreLocation reports
  **-1** for each when the value is unavailable.
- `GlassesStatusPayload.battery_level` — `ge=0` would reject **this model's
  own declared default of -1**, the "unknown" sentinel.
- `BiometricPayload.*` — 220 bpm and 43 C are both real readings.

**Two bounds withdrawn after checking the brain's own emitters**, before the
suite ever ran:

- `FeralMessage.session_id` was capped at 128, then raised to 1024.
  `api/routes/conversations.py:163` composes a session id as
  `f"{session_id}:{branch_name}:{uuid[:6]}"` where `branch_name` is
  unvalidated request-body text, and sub-agents append `:sub:<n>:<uuid>` per
  nesting level. 128 would have refused every frame of a branched conversation.
- `BudgetExceededPayload.call_site` lost its `min_length=1`.
  `agents/orchestrator.py:2161` builds it as
  `str(budget.get("call_site") or call_site)`, so an empty label is reachable,
  and a `ValidationError` on that path would turn a budget banner into an
  exception on the refusal path itself.

**No test broke.** The full suite went 7411 passed / 29 skipped / 0 failed
(7374 pre-existing passes, unchanged), so there was never an occasion to be
tempted into editing one. The new file fails **28 of 37** against the unfixed
source and passes 37/37 after. `ruff check --select=E,F,W --ignore=E501,E402,F401,W291,W293 .`
is clean.

**Deliberately not adopted from the SDK, with reasons:**

- `NodeRegisterPayload.node_id`'s `^[A-Za-z0-9._:-]{1,128}$` pattern. A
  character class is a stronger claim than a length bound and the Swift and
  Kotlin bridges are not known to honour it; adopting it could disconnect
  paired hardware over a character the brain has never objected to. The length
  half is adopted.
- `le=120_000` on `timeout_ms`. `agents/tool_runner.py:735` already dispatches
  30,000 ms and `hardware/mesh.py:330` computes the value from arbitrary
  seconds, so an upper bound risks refusing the brain's own commands. `ge=0`
  only.

**Blobs left uncapped, and why:**

- `VisionFramePayload.data_b64` — its cap is `VISION_MAX_FRAME_KB`
  (`api/state.py:82`), which operators tune via `FERAL_VISION_MAX_FRAME_KB`.
  A hard 512 KiB in the model would silently override that setting.
- `AudioChunkPayload.data_b64` / `TTSChunkPayload` / `AudioResponsePayload` —
  the only documented audio cap governs the HUP `audio_frame` envelope, which
  is a different type and is not in `MESSAGE_TYPES`. Applying 64 KiB here
  would reject a two-second 24 kHz pcm16 chunk (96,000 bytes).

**F-03 divergence — CLOSED by F-03.** While it was live: the model measured
`glasses_frame` `data_b64` on **decoded bytes** (512 KiB) and `api/server.py`
measured **base64 characters** against the same 512 KiB constant, so its
effective ceiling was 384 KiB and the two disagreed for every frame between
384 and 512 KiB decoded. A 400 KiB JPEG was exactly this case. F-02
deliberately did not touch `api/server.py` and left `VIDEO_FRAME_MAX_BYTES` in
`models/protocol.py` (the canonical home per CLAUDE.md) as a second copy of
the server's own literal. F-03 deleted the server's copy, imports the
canonical one, and measures decoded bytes at all six sites. The model and the
handler can no longer disagree: there is one constant and one measurement.

Original finding follows.

**Status when filed:** BLOCKED — awaiting a decision. Re-verified and reproduced; not fixed.

**Re-verified.** Exactly as described. The brain declares `device_id: str`,
`width/height: Optional[int]`, `sequence: Optional[int]`, `data_b64: str` bare. The SDK
bounds all of them and base64-decodes against the 512 KiB cap.

**Reproduced.** Constructed directly against the brain's own model:

```
device_id=''  width=-5  height=1000000000  sequence=-42
decoded payload bytes=900000        (cap is 512 KiB)
DeviceAnnouncePayload rssi_dbm=-9999
```

All accepted. The SDK docstring's claim to "mirror" the brain is false in the
direction that matters.

**The rejection path already exists and is sound.** `parse_message` validates against
`PAYLOAD_MODELS`, and `api/server.py:2200` turns a `ValidationError` into a HUP §8
error frame (1003) while keeping the socket alive. So adding constraints does not
require new plumbing; it changes what that plumbing fires on.

**Compatibility evidence gathered.** Real paired devices on this install carry UUID
`device_id`s (36 chars), so `min_length=1, max_length=128` would not affect them.

**Why this is blocked, three separate stop conditions:**

1. **Wire format, four languages.** `models/protocol.py` is the canonical schema and
   the hard rule says stop.
2. **It makes something that currently "works" start erroring.** A device sending an
   out-of-bounds value is accepted today and would receive a 1003 frame instead. That
   is the point of the fix, but it is a live behaviour change for already-paired
   hardware, and it cannot be verified from here against real glasses firmware.
3. **The scope is 53 models, not 2.** An audit of every `*Payload` class in
   `protocol.py` finds **53 with zero field constraints**, including
   `BiometricPayload`, `AudioChunkPayload`, `ExecuteCommandPayload` and
   `DeviceRegisterPayload`. The finding says to "audit the other payload types while
   you are there"; the answer is that the gap is universal, so "while you are there"
   is its own project.

**Also note:** the `data_b64` size cap overlaps F-03. Fixing it in the model and at
`server.py:3672` independently would produce two caps measuring different things
again. These two items should be decided together.

```
feral-core/models/protocol.py:838-845                        brain — no constraints
feral-nodes/python-node-sdk/src/feral_node_sdk/schemas.py:151-172   SDK — full constraints
```

The SDK docstring claims it "mirrors the brain's `GlassesFramePayload`". It does not. The SDK enforces `device_id` length 1-128, `width`/`height` 1-8192, `sequence >= 0`, and a validator that base64-decodes `data_b64` against a 512 KiB cap. The brain declares all of them bare. `DeviceAnnouncePayload` has the same inversion (SDK bounds `rssi_dbm` to [-127, 20], brain does not).

Every constraint sits in the component an attacker controls.

**Verify:** read both class bodies side by side.

**Done when:** the brain's models carry the constraints, a test posts a hostile frame (empty `device_id`, negative `sequence`, oversized payload) directly at the brain and gets a typed rejection, and the SDK's claim of mirroring is either true or the docstring is corrected. Audit the other payload types in `protocol.py` for the same gap while you are there.

---

### F-03 · Frame size cap measures base64 characters, not decoded bytes

**Status:** FIXED. Not committed: left in the working tree for review.

**Citation corrected: there are six sites, not four, and the finding missed
two of them.** The finding lists `server.py:3672` plus `:1823, :2304, :3283`
as if all four were one shape. They are two families, and two members of the
second family were never cited:

```
# Family A: cap is `VISION_MAX_FRAME_KB * 1024`, variable named frame_b64_len
feral-core/api/server.py:1823   client_session  vision_frame (webclient)
feral-core/api/server.py:2304   daemon_session  vision_frame
feral-core/api/server.py:3283   daemon_session  frame

# Family B: cap is a *_MAX_BYTES constant, measured as len(data_b64)
feral-core/api/server.py:3598   _handle_video_frame                    ← UNCITED
feral-core/api/server.py:3636   _handle_audio_frame (AUDIO_FRAME_MAX_BYTES)  ← UNCITED
feral-core/api/server.py:3672   _handle_glasses_frame                  (the cited one)
```

All six measured base64 characters against a cap whose name and log line mean
decoded bytes. All six are fixed.

(An earlier pass through this file recorded the same two families with the
letters swapped. Read the file:line lists, not the letters.)

**That earlier pass argued the `frame_b64_len` sites `:1823, :2304, :3283`
"are not the same defect" because the variable honestly says what it measures.
That is wrong and it is corrected here.** The variable is honest; the cap is
not. It is
`VISION_MAX_FRAME_KB`, sourced from the operator setting `vision.max_frame_kb`
via `FERAL_VISION_MAX_FRAME_KB`, defaulting to 512. An operator who sets 512
is budgeting 512 KiB of image, and the log line reports the number as `B`. A
character comparison silently turned that into 384 KiB, which is the same
defect with a different constant.

**Measured before the fix** (decoded size, base64 size, outcome):

```
300 KB image -> b64 400 KB | before: accepted | correct: accepted
400 KB image -> b64 533 KB | before: DROPPED  | correct: accepted
500 KB image -> b64 666 KB | before: DROPPED  | correct: accepted
```

**Second defect in the same item: 4020 was never sent.** `grep -n 4020` over
the tree found the string only inside log messages and docstrings. An
over-cap frame was dropped in silence and the device's send reported success,
which is why this was never reported as a bug: nothing anywhere told the
sender its frames were being discarded.

**The fix.**

- `models/protocol.py` gains `decoded_b64_size()` and keeps
  `VIDEO_FRAME_MAX_BYTES` as the only declaration. `api/server.py` imports
  both; its own `VIDEO_FRAME_MAX_BYTES = 512 * 1024` literal is deleted. A
  test asserts by AST that the server does not redeclare it, so the two
  copies cannot come back.
- `decoded_b64_size` computes the decoded length arithmetically rather than
  calling `b64decode`, because these checks run once per frame at camera
  frame rate and decoding would allocate a full copy of every frame purely to
  measure it. The model layer's `_decoded_size_guard` still decodes, because
  it is a validator and must also reject a blob that is not base64 at all.
  They agree on every well-formed input.
- `AUDIO_FRAME_MAX_BYTES` stays in `api/server.py` with a comment saying why:
  `audio_frame` is not in `MESSAGE_TYPES`, so no model governs it and there is
  nothing in the model layer for it to drift from. Moving it would have been
  symmetry for its own sake.
- `_handle_video_frame`, `_handle_audio_frame` and `_handle_glasses_frame`
  now return `str | None`: a rejection reason, or `None` when handled. They
  are sync and never receive the socket, so they cannot send. Returning a
  reason was chosen over making them async and threading `ws` through: it is
  the smaller change and it keeps them directly testable. Their five call
  sites in `daemon_session` await the new `_send_frame_too_large(ws, reason)`.
- Only the cap returns a reason. A missing `glasses_buffer` or a raising
  `ingest` is a brain-side problem, not a protocol violation by the daemon,
  so those still drop quietly rather than blaming the sender.

**Where 4020 is emitted, and where it is not:**

| Site | Now measures | 4020 emitted |
|---|---|---|
| `:1823` webclient `vision_frame` | decoded bytes | **no**, see below |
| `:2304` daemon `vision_frame` | decoded bytes | yes |
| `:3283` daemon `frame` | decoded bytes | yes |
| `:3598` `_handle_video_frame` | decoded bytes | yes, at both call sites (`video_frame`, `device_event`) |
| `:3636` `_handle_audio_frame` | decoded bytes | yes, at both call sites (`audio_frame`, `device_event`) |
| `:3672` `_handle_glasses_frame` | decoded bytes | yes |

`:1823` is inside `client_session`, and `ws` *is* in scope there, so this is a
judgement call rather than missing plumbing. It is left silent because that
socket has no HUP error-code channel: it speaks
`FeralMessage(type="error", payload={"text": ...})`, which
`feral-client-v2/src/pages/Chat.jsx:602` renders as an inline chat notice plus
a global toast. A browser camera loop sending over-cap frames would fire one
per frame. Rate-limiting it would be inventing plumbing this item did not ask
for. It is fixed for measurement only.

**Deviation from HUP_SPEC.md, deliberate, needs its own decision.**
`HUP_SPEC.md:925` says of 4020: "Brain closes the socket; daemon MUST
reconnect with a saner encoder bitrate." The socket is **not** closed here. A
single over-cap frame from a mis-configured encoder would drop a live voice or
vision session, and the daemon now has what it needs to correct itself. The
spec and the implementation therefore disagree on this point; reconciling them
is a wire-contract change across four SDKs and is not in scope for F-03.

**Note on `glasses_frame` specifically.** It is registered in `MESSAGE_TYPES`,
and since F-02 `GlassesFramePayload` applies the same decoded cap,
so on the daemon socket an over-cap glasses frame is refused at `parse_message`
with a 1003 `bad_payload` before the handler runs. The handler's own check is
the second line, for callers that do not go through `parse_message`. The
other five sites have no model-layer counterpart and were genuinely silent.

**Behaviour change, intended and stated.** Frames between 384 and 512 KiB
decoded were dropped and will now be accepted: that is the documented cap
working correctly. Over-cap frames that were dropped in silence now draw a
4020 error frame. Nothing that succeeds today starts failing.

**Tests.** New file `feral-core/tests/test_frame_size_cap_decoded_bytes.py`,
14 tests: **14/14 fail against the unfixed source, 14/14 pass after.** The
failures were the stated ones, not collection errors: 400 KiB frames not
buffered, over-cap frames returning `None` instead of a reason, and the socket
answering 1002 (the trailing probe) where 4020 was expected, which is the
silence made visible. Each WS test sends an unknown-type message after the
over-cap frame so the unfixed tree fails on content rather than hanging on a
`receive_json` that never returns.

**Three existing tests were changed, and this is worth flagging.** They
encoded the defect: `"x" * (VIDEO_FRAME_MAX_BYTES + 8)` is ~384 KiB decoded,
so they asserted that a legal frame gets dropped. They now build their
payloads with `_b64(cap + n)` and assert the returned reason.

```
feral-core/tests/test_hup_v1_1_brain.py                       test_video_frame_over_cap_is_dropped
feral-core/tests/test_hup_v1_1_brain.py                       test_audio_frame_over_cap_is_dropped
feral-core/tests/test_hup_glasses_frame_and_device_announce.py  test_handle_glasses_frame_rejects_oversize
```

Two stale docstrings in `tests/test_protocol_field_constraints.py` that said
the server "still measures characters" were corrected in the same change.

**Proof, real numbers.** `python -m pytest tests/ -q -p no:cacheprovider
-p no:randomly --no-cov` is **7428 passed, 29 skipped, 0 failed** (332s), run
twice with the same result. `ruff check --select=E,F,W
--ignore=E501,E402,F401,W291,W293 .` prints "All checks passed!".

One caveat about the tree, not about this fix: an intermediate full run showed
three failures in `tests/test_embedding_loop_liveness.py`, which is untracked
F-04 work sitting in this same working tree alongside a modified
`memory/embeddings.py`. They pass standalone and passed in both clean runs
above; they are timing-sensitive liveness assertions, and nothing in F-03
touches `memory/`.

**Siblings.** `decoded_b64_size` is now the only size measurement in
`api/server.py`: `grep -n "len(data_b64)\|frame_b64_len"` over the file
returns one hit, and it is inside an explanatory comment. `feral-nodes/python-node-sdk` already decoded before
comparing, so the SDK never had this bug.

Original finding follows.

**Citation corrected 2026-08-11 — the original site list was wrong twice over.** Independently re-verified:

```
# Family A — the actual defect. len(data_b64) is base64 CHARACTERS.
feral-core/api/server.py:3598   if len(data_b64) > VIDEO_FRAME_MAX_BYTES:   # _handle_video_frame   ← UNCITED
feral-core/api/server.py:3672   if len(data_b64) > VIDEO_FRAME_MAX_BYTES:   # _handle_glasses_frame
feral-core/api/server.py:3636   audio_frame family                                                   ← UNCITED

# Family B — a DIFFERENT constant, and the variable is honestly named.
feral-core/api/server.py:1823, :2304, :3283   if frame_b64_len > VISION_MAX_FRAME_KB * 1024:
```

Family B is not the same defect: `frame_b64_len` says what it measures, so there is no docstring/code contradiction to fix. Treat it separately and decide on its own merits whether a base64-length cap is what is wanted there.

`len(data_b64)` counts base64 characters while the docstring two lines above claims a decoded cap. Base64 inflates 4/3, so the effective ceiling is 384 KiB. A 400 KiB JPEG passes both SDK validators, reports a successful send, and is dropped by the brain with a log-only warning — HUP error 4020 is never returned.

**Done when:** the check measures decoded size, the caller receives an explicit protocol error rather than silence, and the docstring matches the code. Fix all four sites.

---

### F-04 · The default embedding path blocks the event loop

**Status:** open

```
feral-core/memory/embeddings.py:1219      return self._fastembed_embed(text)     # inside async def
feral-core/memory/embeddings.py:1221      return self._local_embed(text)         # inside async def
feral-core/memory/embeddings.py:1200-1202 batch variant, blocks once per text
feral-core/memory/embeddings.py:52        _detect_provider default is "auto"
```

`_embed_impl` is `async` and awaited from ordinary request paths, but both branches run synchronously on the loop thread. `_local_embed` ends in `SentenceTransformer.encode()`. `auto` resolves to exactly these two branches, so this is the default install.

Worse: `_ensure_local_model` is reachable from the same sync path and its docstring notes construction triggers a ~130 MB download. A cold first embed can stall the brain for the length of a model fetch.

**Done when:** both branches are offloaded via `asyncio.to_thread`, and a test asserts loop liveness during an embed — see the existing pattern in `feral-core/tests/perf/test_memory_latency.py`, which counts ticks of a 1 ms pulse coroutine rather than measuring wall clock.

---

### F-05 · Blocking `subprocess.run` inside async route handlers

**Status:** FIXED, uncommitted, in the working tree.

**All three cited sites re-verified present and fixed.** All three were
`subprocess.run` inside `async def`, exactly as cited.

**Citation correction: the sweep the finding asks for finds seven sites, not
three.** An AST scan for blocking calls inside `async def` across `feral-core`
(excluding `build/`, `dist/`, `tests/`) returns:

```
# Fixed — six sites.
feral-core/api/routes/apps.py:379                 subprocess.run  git clone, timeout=120   (cited)
feral-core/api/routes/apps.py:397                 shutil.rmtree   of the fresh clone       ← UNCITED
feral-core/skills/marketplace.py:245              subprocess.run  git pull,  timeout=30    (cited)
feral-core/api/routes/system_permissions.py:115   subprocess.run  open,      timeout=3     (cited)
feral-core/security/docker_sandbox.py:199         shutil.rmtree   sandbox tmpdir           ← UNCITED
feral-core/skills/impl/code_interpreter.py:448    shutil.rmtree   run dir                  ← UNCITED

# Examined and rejected — not this defect.
feral-core/skills/impl/browser_use.py:403         subprocess.Popen in async _auto_launch_chrome
```

`apps.py:397` is the one that matters most among the uncited: it is in the
*same* `async def` as the cited clone, in the `finally` block, deleting a
directory that a `git clone` has just filled, so it is thousands of `unlink()`
calls on the loop thread rather than one syscall.

`browser_use.py:403` is **not** the same defect and was deliberately left
alone. `Popen` spawns and returns immediately; it never waits for the child.
The blocking cost is `fork`/`exec` only, not the lifetime of the process, so
converting it would buy nothing. (That file is also being edited concurrently
for another item; nothing here touches it.)

**How the subprocess conversions preserve semantics.** Each is now
`asyncio.create_subprocess_exec` + `await asyncio.wait_for(proc.communicate(),
timeout=...)`, with the same timeout values, now named constants
(`GIT_CLONE_TIMEOUT_S = 120`, `GIT_PULL_TIMEOUT_S = 30`,
`OPEN_DEEPLINK_TIMEOUT_S = 3`) so the timeout path is testable in under a
second. `wait_for` only cancels the *await*, it does not touch the child, so
each timeout path kills and reaps the process before raising: without that,
every timed-out install leaks a running `git clone`. The exception raised on
timeout is still `subprocess.TimeoutExpired` with the same argv and timeout,
so callers and the resulting HTTP behaviour are unchanged.
`marketplace.py` used `check=True`, which `create_subprocess_exec` has no
equivalent for, so `subprocess.CalledProcessError` is raised by hand with the
same returncode and argv; its `str()` is what lands in the returned
`{"success": False, "error": ...}`, and that string is pinned by a test.
`system_permissions.py` used `check=False`, so the exit code is still ignored.
The three `shutil.rmtree` sites became `await asyncio.to_thread(shutil.rmtree,
..., ignore_errors=True)`, keeping `ignore_errors` where it was already set.
No endpoint changed what it returns, what it raises, or any log message.

**Tests.** New file `feral-core/tests/test_blocking_calls_loop_liveness.py`,
10 tests: **9/10 fail against the unfixed source, 10/10 pass after.** The
tenth is a stated preservation guard (the `check=True` error string) that
passes both ways on purpose. The failures are the stated ones, not collection
errors:

```
ticked only 0 times during a 0.2s git clone       apps.py clone
ticked only 0 times during a 0.2s git pull        marketplace.py pull
ticked only 0 times while waiting on `open`       system_permissions.py
rmtree ran on the event loop thread               apps.py, docker_sandbox.py, code_interpreter.py
module has no attribute GIT_CLONE_TIMEOUT_S       the three kill-the-child tests
```

Liveness is measured the way `tests/perf/test_memory_latency.py` and
`tests/test_embedding_loop_liveness.py` do it: count the ticks of a 1 ms pulse
coroutine running alongside the work, rather than timing wall clock. The
`to_thread` sites additionally assert the work ran on a different thread,
because a wrapper that awaits something trivial and then still calls the
blocking function inline would satisfy a tick count on a fast machine. The
subprocess tests put a fake `git` / `open` shell script first on `PATH`, so
they need no network and no real git while still exercising the real spawn,
argv, exit code and stderr capture; the kill tests have that script touch a
marker file *after* its sleep and assert the marker never appears.

**One existing test was changed.** `tests/test_phase11_desktop_control.py::
TestOpenSystemPermission::test_known_key_triggers_open` patched
`api.routes.system_permissions.subprocess` and asserted `subprocess.run` was
called. It asserted the mechanism, which is what this fix replaces; it now
stubs `asyncio.create_subprocess_exec` and makes the same assertions about
argv, status and body. As a side effect it no longer spawns a real `open`
against System Settings on the developer's machine.

**Proof, real numbers.** `python -m pytest tests/ -q -p no:cacheprovider
-p no:randomly --no-cov` is **7447 passed, 29 skipped, 0 failed** (325s).
`ruff check --select=E,F,W --ignore=E501,E402,F401,W291,W293 .` prints
"All checks passed!".

Original finding follows.

```
feral-core/api/routes/apps.py:379                git clone, timeout=120   ← worst
feral-core/skills/marketplace.py:245             git pull,  timeout=30
feral-core/api/routes/system_permissions.py:115  open,      timeout=3
```

The first can stall every concurrent coroutine for two minutes against a slow or hostile remote.

**Done when:** all three use `asyncio.create_subprocess_exec`. Then grep for the class: `subprocess.run`, `subprocess.Popen`, and `shutil.rmtree` reachable from any `async def`.

---

### F-06 · Unreferenced background tasks can be garbage-collected

**Status:** FIXED.

**Re-verified 2026-08-11 against `63b054aa1`.** All fourteen cited `file:line`
pairs were present and every line number was still exact, which is unusual for
this audit and worth recording: nothing in this class had drifted.

**Count correction: thirty-two, not fourteen.** Nineteen `create_task` plus a
thirteen-site sibling the finding does not name, `asyncio.ensure_future`.

An AST sweep of `feral-core` (excluding `build/`, `dist/`, `tests/`) for
`*.create_task(...)` appearing as a whole expression statement, which is exactly
"the Task was produced and immediately dropped", found five sites the audit
missed:

```
integrations/home_assistant.py:194   asyncio.get_running_loop().create_task(client.aclose())
memory/sync_scheduler.py:218         per-peer sync, cadence tick
memory/sync_scheduler.py:378         per-peer sync, heartbeat reconnect
voice/gemini_realtime.py:620         per-turn memory refresh
voice/realtime_proxy.py:1254         voice-turn hooks
```

The audit's list only covered `asyncio.create_task` and `loop.create_task`
spelled as bare names; the four it missed in `sync_scheduler.py` and the voice
proxies are those same two spellings, so that gap was enumeration, not pattern.
`home_assistant.py:194` is genuinely a different spelling
(`get_running_loop().create_task(...)`, a call receiver rather than a name).

A second sweep for the adjacent shape, `t = create_task(...)` assigned to a
local that is never read again, found **zero** sites.

**The sibling: `asyncio.ensure_future`, thirteen more sites.** Found by running
`ruff --select=RUF006`, which the CI gate does not enable. `ensure_future` on a
coroutine constructs a Task by exactly the same route and the loop references it
exactly as weakly, so it is the same defect under a different name:

```
channels/base.py:649, :675, :692   Discord gateway connect, its heartbeat, its reconnect
channels/base.py:830, :885         Slack socket-mode connect and its reconnect
agents/orchestrator.py:643         F2 auto-compaction
agents/orchestrator.py:1775, :3184 self-learning on_message (non-stream and stream)
api/server.py:1818, :1853, :2360, :2374, :3699   vision scene analysis
```

`channels/base.py:649` and `:830` matter most: they are the layer *underneath*
the three `config.py` sites the finding does cite. `start_channel("slack", ...)`
scheduled `_socket_mode` with a bare `ensure_future`, so fixing `config.py`
alone would have left the channel's actual socket collectible one frame later.
The finding's own stated symptom, "a channel that fails to start does so
silently", was reachable through both.

`agents/orchestrator.py:643` is the worst of the rest: `_run`'s `finally` is the
only thing that clears `_compaction_inflight[session_id]`, so a collected
compaction task leaves that flag stuck `True` and the session never compacts
again for the life of the process.

Thirty-two is the whole class. `RUF006` now reports one hit repo-wide, the
deliberately-unreferenced task inside the meta-test that proves the probe works.

**Mechanism, per site.** No third mechanism was invented.

- `state.register_background_task(...)` where the object can reach it:
  `api/routes/config.py` (3, the channel-startup sites), `api/state.py`
  (3, the websocket broadcasts) and `api/server.py` (5, the vision analyses).
  `api/routes/config.py` already used this registry for the proactive and
  vision toggles at `:182` and `:203`, and `api/server.py` for its own
  boot-time tasks.
- `Orchestrator._track_background_task`, the registry that file already owns,
  for its 3 `ensure_future` sites.
- An instance-level `set[asyncio.Task]` with an `add_done_callback(discard)`,
  the `memory/store.py` shape, everywhere else: `memory/sync.py`,
  `memory/sync_scheduler.py`, `agents/supervisor.py`, `cost/loop_guard.py`,
  `agents/subagent_spawner.py`, `skills/impl/browser_use.py`,
  `voice/realtime_proxy.py`, `voice/gemini_realtime.py`, and `channels/base.py`
  where the set and a `_track_bg_task` helper went on the `Channel` ABC so all
  five subclass sites share one mechanism.
- A module-level set, same discard idiom, at the two sites with no instance to
  hang state off: `services/mdns.py` (`stop_advertisement` is a module
  function) and `integrations/home_assistant.py` (`_close_later` is a
  `staticmethod`).

**The sites where retaining unconditionally would leak.**
`skills/impl/browser_use.py:254` schedules one task per CDP event per async
listener, in a receive loop that runs for the life of a browser session; the
five `api/server.py` vision analyses fire per frame on a live websocket. An
unbounded registry at either would be a real leak. The done-callback discard is
what makes it safe: the set only ever holds tasks that have not finished. The
same reasoning applies, at lower volume, to the mDNS resolves and the per-peer
syncs. No site was left unreferenced on this ground.

**Nothing deferred.** None of the thirty-two sites falls in a file another lane
holds.

**Also fixed the silent half of the config finding.** `register_background_task`
retains the task but its `set.discard` callback does not *retrieve* the
exception, so a channel that fails to start would still only surface through
asyncio's never-retrieved warning at collection time, which is the very event
that was unreliable. The three channel sites now attach a done-callback that
logs the failure with `exc_info`, so "the route said `ok: true` and the channel
never came up" produces a log line.

**The failing test is a real collection, not a set-existence check.**
`tests/test_background_task_references.py`. The probe coroutine awaits a future
that is published only through a `weakref.WeakValueDictionary`, so the task, its
frame, and that future form a cycle with no external root. `gc.collect()`
destroys it and CPython prints "Task was destroyed but it is pending!". If the
call site kept a reference the future survives, the test completes it, and the
coroutine's second half runs. Two meta-tests pin that the probe can both fail
and pass, so it cannot silently stop discriminating.

Against unfixed `63b054aa1` (run in a clean `git worktree` of HEAD so the
baseline could not be contaminated by other lanes' in-flight edits):
**5 failed, 5 passed**. The four behavioural tests each failed reporting the task
collected mid-flight:

```
test_channel_startup_task_survives_gc        POST /api/config/credentials, real handler
test_supervisor_broadcast_task_survives_gc   Supervisor._record broadcaster
test_discord_gateway_task_survives_gc        DiscordChannel.start, the ensure_future sibling
test_sync_scheduler_reconnect_task_survives_gc
```

and the AST guard failed listing all thirty-two sites. After the fix:
**10 passed**.

The channel-startup test binds the real `BrainState.register_background_task` to
its stand-in state rather than reimplementing it, so it exercises the production
registry. That is trap 3 in `CLAUDE.md` applied to this fix.

**Guard against reintroduction:** `test_no_unreferenced_create_task`, modelled
on `tests/test_double_contracts.py`. `RUF006` alone was not sufficient: it
matches only `asyncio.create_task` / `asyncio.ensure_future` spelled as an
attribute of a module named `asyncio`, so it missed eight of the nineteen
`create_task` sites (`loop.create_task`, `get_running_loop().create_task`), and
the CI gate runs `--select=E,F,W` anyway. It was, however, what surfaced the
`ensure_future` sibling, and it is worth a line in S-1: two of this audit's
findings so far were mechanically discoverable by a linter or type checker the
gate does not run.

**Proved:** `python -m pytest tests/ -q -p no:cacheprovider -p no:randomly
--no-cov` → **7448 passed, 29 skipped**, 0 failed (324s).
`ruff check --select=E,F,W --ignore=E501,E402,F401,W291,W293 .` → clean.

Original finding (kept for reference; the site list is incomplete, see above):

```
feral-core/api/routes/config.py:492, :510, :540   channel startup (Telegram / Slack / WhatsApp)
feral-core/api/state.py:790, :828, :1888          websocket broadcast
feral-core/memory/sync.py:1042 · agents/supervisor.py:452 · cost/loop_guard.py:275
agents/subagent_spawner.py:250 · services/mdns.py:351 · skills/impl/browser_use.py:254
voice/realtime_proxy.py:1668 · voice/gemini_realtime.py:843
```

`asyncio.create_task(...)` with no reference retained. CPython may collect a task held only by a weak reference. The config sites are user-facing: a channel that fails to start does so silently *and* can be collected mid-startup.

The repo already has the right pattern — `agents/orchestrator.py:210-218` and `memory/store.py:352-357` hold a `set[asyncio.Task]`, and `api/server.py` uses `state.register_background_task(...)`. These sites simply missed it.

**Done when:** every site uses the existing helper, and a ruff rule or test prevents reintroduction.

---

## P1: after the P0 set

All of F-07 through F-15 were worked in one pass. Nothing is committed; the
work is in the tree. Two items are DEFERRED because they land in files another
lane owns, and both are recorded with what the owning lane needs to do.

Proof for the whole set, run after the last change:
`python -m pytest tests/ -q -p no:cacheprovider -p no:randomly --no-cov` →
**7680 passed, 31 skipped, 0 failed** (440s), and
`ruff check --select=E,F,W --ignore=E501,E402,F401,W291,W293 .` →
**All checks passed!**. Run under `PYENV_VERSION=3.11.11` because
`.python-version` pins an interpreter that is not installed in this tree.
The same ruff line over `scripts/` and `feral-nodes/python-node-sdk/` is also
clean. `feral-client-v2`: **97 files, 607 tests, all passing**.
`feral-nodes/ts-node-sdk`: **25 passing**, `tsc --noEmit` clean.
`feral-nodes/python-node-sdk`: **47 passing**.

One caveat about the tree, not about this work: it is shared with other lanes
and moved underneath this pass. An intermediate full run showed
`tests/test_doctor_severity.py` and `tests/test_sqlite_interpreter_features.py`
failing; both passed standalone, both concern `cli/main.py`'s doctor sections
and an untracked `memory/sqlite_features.py` that another lane owns, and both
pass in the final run above. Nothing here touches those files.

---

### F-07 · Gen-UI payload cap disagrees between host and brain by 6x

**Status:** FIXED. Not committed: left in the working tree.

**Re-verified.** Present as described, at `genui/app_message_schema.py:112`
(the finding says 113) and `AppSurface.types.ts:70`. Reproduced against both
implementations with the audit's own payload:

```
{"a": "中" * 11000}
  python  json.dumps(v).encode("utf-8")   66,009  -> REFUSED
  js      JSON.stringify(payload).length  11,008  -> ACCEPTED
```

**The audit names one cause. There are three, and the second one it misses
breaks pure ASCII.**

1. `ensure_ascii=True`, as stated: six bytes per BMP character, twelve per
   astral one.
2. **`separators`.** `json.dumps` defaults to `(', ', ': ')`; `JSON.stringify`
   emits neither space. That is two bytes per key with no non-ASCII involved
   anywhere. Measured: a pure-ASCII payload of **exactly** 65,536 bytes
   (`{"a": "x" * 65528}`) measured 65,537 in Python and was refused while the
   browser accepted it. A 1000-key object was 12,780 against 10,781.
3. **Direction.** The audit frames this as the brain refusing what the browser
   allows. The reverse is the security-relevant half and it is worse: the
   browser guard, which is the half an attacker controls and whose stated job
   is to stop an iframe flooding the host channel, counted UTF-16 units. 30,000
   CJK characters is 90,008 bytes and 30,002 units, so it passed. 20,000 emoji
   is 80,008 bytes and 40,002 units, so it passed.

**Fix: both sides measure UTF-8 bytes of the compact JSON encoding**, which is
the quantity the constant is named after. `payload_size_bytes()` in Python and
`measurePayloadBytes()` in TypeScript; the validators now call them rather than
inlining a measurement.

`errors="backslashreplace"` on the Python encode is load-bearing and not
cosmetic. `json.loads('{"a": "\ud800"}')` yields a lone surrogate, and
`.encode("utf-8")` raises `UnicodeEncodeError` on one. That is a `ValueError`,
so the validator's existing handler would have reported an acceptable payload
as "not JSON-serialisable" while the browser accepted it, the same defect
re-created. JavaScript's well-formed `JSON.stringify` re-escapes lone
surrogates to six ASCII characters, which is exactly what `backslashreplace`
produces, so both land on 14 bytes.

**Deliberately not changed:** `allow_nan`. Python emits `NaN`/`Infinity` where
JavaScript emits `null`, so the two differ by 1-4 bytes per non-finite number.
A payload would need ~16k of them to matter, and a payload that arrived as JSON
cannot contain one. Tightening it would make Python refuse what the browser
accepts, which is the shape being fixed.

**Shared fixture, asserted on both sides.** The done-when asks for exactly
this. `feral-core/tests/fixtures/app_message_payload_sizes.json` holds 13 cases
with their expected byte counts and verdicts; the TypeScript test reads that
file rather than copying the numbers, because a copy is what let these drift.
A Python test fails if the TS mirror stops reading it.

**Tests.** `feral-core/tests/test_genui_payload_cap_utf8_bytes.py` (32):
**21 fail against the unfixed source, 32/32 after.** Four of the failures are
behavioural accept/reject verdicts, not size assertions.
`feral-client-v2/src/__tests__/pages/AppSurface.payloadCap.test.js` (31):
**20 fail before, 31/31 after**; three of those are the over-cap payloads the
old guard waved through.

**Not fixed, and it needs saying:** `feral-core/webui_v2/assets/index-*.js` is a
checked-in build of `feral-client-v2` and still contains the old comparison.
`scripts/build_webui_v2.sh` regenerates it and `scripts/release.py` runs that at
release time, so it is rebuilt by the existing process rather than by hand. The
fix does not reach an installed wheel until that runs.

---

### F-08 · The two node SDKs write different key filenames for the same node

**Status:** FIXED. Not committed: left in the working tree.

**Re-verified** at both cited lines. Reproduced by running both rules over the
same node ids:

```
node_id              python (before)        typescript (before)
'sensor 01'          sensor01.key           sensor_01.key
'café'               café.key               caf_.key
'!!!'                .key                   ___.key
''                   .key                   .key
'a b'                ab.key                 a_b.key
'日本語ノード'          日本語ノード.key         ______.key
```

**Scope corrections, three of them.**

- **Three algorithms, not two.** `HUP_SPEC.md` §4.1 step 5 documented the path
  as `~/.feral/node-keys/<node_id>.key` with no sanitisation at all. That is
  the document the done-when calls the source of truth, and it described a
  third behaviour neither SDK implements, the one a new SDK author would copy.
- **Both sides are many-to-one, not just Python.** The audit attributes the
  collapse to Python's dropping. TypeScript's replacement is equally
  many-to-one: `"a b"` and `"a_b"` both resolved to `a_b.key`, and every
  six-character non-ASCII node id resolved to `______.key`. Python's is worse
  in kind, not in principle: every all-punctuation id and the empty id all
  wrote to a hidden file literally named `.key`.
- **A fourth divergence the audit does not name, found by the new test rather
  than by reading.** The TypeScript regex had no `u` flag, so it matched per
  UTF-16 code unit: an astral character is a surrogate pair and became **two**
  underscores where Python's `re` produced one. `"😀"` was `__.key` against
  `.key`. Found because the shared fixture asserted the emoji case on both
  sides and my first fix passed in Python and failed in TypeScript.

**Verified not a vulnerability, and pinned so it stays that way:** `/` is
outside the allowed class in both rules, so neither SDK could ever escape the
keys directory. `'../../etc/passwd'` produced `....etcpasswd.key` and
`.._.._etc_passwd.key`. There is now a test per fixture case asserting the
resolved path's parent is the keys directory.

**One algorithm, specified in the spec.** New `HUP_SPEC.md` §4.1.1:

1. Replace every character outside `[A-Za-z0-9._:-]` with `_`, **per code
   point**. The class is exactly the brain's `NodeRegisterPayload.node_id`
   pattern and it is ASCII; the spec now says out loud not to use a
   Unicode-aware "is alphanumeric" test, which is what Python was doing.
2. If that changed nothing and the length is 1-128, the filename is that.
3. Otherwise truncate to 128, then append `-` and the first 8 hex characters of
   `sha256(node_id)`.

**Step 2 is why no paired hardware moves.** Every node id the brain accepts
takes it, so `wristband-01`, `acme:wb:001` and `a.b_c-d` keep the byte-identical
filename both SDKs already write. Only ids that were already colliding or
already disagreeing get a new name. Step 3 is what makes the mapping injective;
sanitising alone is not, which is the half that silently overwrote one node's
API key with another's.

**Checked and not affected:** the Swift (`ios-node-sdk`) and Kotlin
(`android-bridge`) SDKs do not persist keys to `~/.feral/node-keys` at all , 
they take the API key from the caller. So the audit's "two node SDKs" is right
on the language dimension. `cli.py:_cmd_run` also calls `save_key`, so the
daemon-run path was affected too, not only the pair flow.

**Tests.** `feral-nodes/spec-fixtures/node_key_filename.json` holds 15 cases
with the pre-fix output of *both* SDKs recorded alongside the canonical name,
so the compatibility guard compares against measurement rather than against a
restatement of the new rule. Both suites read that one file.
`feral-nodes/python-node-sdk/tests/test_key_filename_canonical.py` (35):
**12 fail before, 35/35 after.**
`feral-nodes/ts-node-sdk/tests/keyFilename.test.ts` (20): **19 fail before,
20/20 after**, `tsc --noEmit` clean. `ts-node-sdk` had vitest as a
devDependency and no `test` script; added one, since the test is otherwise
only reachable by typing the runner by hand.

---

### F-09 · The install smoke test cannot fail, and runs after publishing

**Status:** FIXED. Not committed: left in the working tree.

**Re-verified.** All three claims hold. Reproduced the first one directly, with
a stub `feral` on `PATH` that exits 1:

```
$ feral --help || true; python -c "..." || true; echo $?
ModuleNotFoundError: No module named 'mlx_lm'
0                                   <- the step passes

$ set -euo pipefail; feral --help; echo $?
1                                   <- what it should have done
```

**Scope: four tolerated commands, not two.** The file has two jobs,
`smoke-linux` and `smoke-macos`, and both carried all three defects. The audit
describes the shape once.

**A fourth weakness, unstated:** the version command *printed* the installed
version and never compared it, so even without `|| true` a stale cached release
would have passed.

**NOT REPRODUCIBLE, the "related and worth fixing together" half.** The audit
says `CHANGELOG.md:305` claims "CI now tests Python 3.14" while `ci.yml:82` is
`['3.11','3.12']`. The tree has moved. `ci.yml:120-132` now carries a written
rationale for *deliberately* keeping 3.14 out of the brain pytest matrix, the
3.11 lockfile pins `pillow==11.3.0` while fastembed needs `pillow>=12` on 3.14,
so that job could not install and would be red for the wrong reason, and it
points at `install-smoke.yml`, whose matrix is already `['3.11','3.12','3.14']`.
The CHANGELOG sentence is true by way of the smoke job. No change made.

**Fix, all three parts.**

- **Asserts.** Every `|| true` on a verification command is gone and every step
  is `set -euo pipefail`. The one surviving `|| true`, on `wait` after killing
  a background server, is reaping rather than verification, and the new test
  allows it by name rather than by pattern.
- **Installs what users install.** `feral-ai[all]`, matching
  `scripts/install.sh`. And because pip warns rather than fails on an unknown
  extra, requesting `[all]` proves nothing on its own, so a new
  `scripts/check_extras_installed.py` asserts every requirement the extra pulls
  in is actually present. Verified against the real distribution on this
  machine: `[all]` OK, 17 requirements; `macos-extras` correctly failed as
  undeclared (that is F-11, found by this script rather than by reading).
- **Gates the publish.** `install-smoke.yml` gained a `workflow_call` trigger
  and `publish.yml` gained an `install_smoke` job between `build` and
  `publish`, installing the wheel this run just built and blocking `publish` on
  it. It runs in parallel with `stage` rather than after, because `stage` sits
  behind a manual environment approval and there is no reason to ask a
  maintainer to approve a canary for a wheel that cannot be installed. The
  post-release PyPI lane is kept: installing from the index exercises
  index/sdist selection that the artifact path does not.

**Two traps handled, both of which would have made the gate vacuous.**

- Inside a called workflow `github.event_name` is the **caller's** event, so
  the existing `if:` (which only allowed `workflow_dispatch` and
  `workflow_run`) would have skipped every job on a tag push and the gate would
  have passed by doing nothing. The condition now includes `push`.
- The job id is `install_smoke`, not `install-smoke`: GitHub's expression
  parser reads `-` in `needs.install-smoke.result` as subtraction.

**The matrix is deliberately unconstrained**, and there is a test for it.
Applying `--constraint requirements.lock` here would reintroduce the exact
3.11-resolution conflict that shipped 2026.8.3 broken, as a green run.

The two jobs also stopped hand-rolling a weaker copy of the bundle checks and
now call `scripts/release_wheel_smoke.py`, the same asserting script the build
and canary stages already run, with `--expected-version`.

**Tests.** `feral-core/tests/test_install_smoke_gates_release.py` (8):
**5 fail against the unfixed workflows, 8/8 after.** Re-run against the
original file recovered from `git show HEAD:`, the tightened versions still
fail 4 of 5; the fifth (the lockfile guard) passes both ways because the old
file did not use the lockfile either, it guards reintroduction, and saying so
is more useful than pretending it caught something.

**Not verifiable from here, stated plainly:** GitHub Actions cannot be executed
in this environment. The YAML parses, the job graph and shell logic are
asserted structurally, and `scripts/check_extras_installed.py` was run for real
against a real installed distribution. The workflows themselves have not been
run.

---

### F-10 · `mlx-lm` and `sentence-transformers` declared by no extra

**Status:** FIXED. Not committed: left in the working tree.

**Re-verified, and the working hypothesis was wrong.** The brief said to treat
the mlx half as a dead import path. It is not dead: `agents/llm_provider.py:809`
calls `create_local_engine()`, and that factory returns `MLXEngine` whenever
`platform.system() == "Darwin" and platform.machine() == "arm64"`. So the
engine with no declared dependency is the one auto-selected on the flagship
platform. (An earlier grep of mine missed the caller; trap 1 in CLAUDE.md.)

**A second defect, unstated, and it changes the fix.** Declaring `mlx-lm` alone
would have made things worse. Verified against the real package rather than
inferred, `mlx-lm 0.31.3` installed into a scratch venv on this Apple Silicon
machine:

```
mlx_lm.generate.generate_step(prompt, model, *, max_tokens, sampler, ...)
  no `temp` parameter, no **kwargs

>>> inspect.signature(generate_step).bind(None, None, max_tokens=512, temp=0.7)
TypeError: got an unexpected keyword argument 'temp'

>>> inspect.signature(generate_step).bind(None, None, max_tokens=512,
...                                       sampler=make_sampler(temp=0.7))
OK
```

`MLXEngine.generate` passed `temp=`, which `mlx_lm.generate` forwards through
`stream_generate` into `generate_step`. Sampling moved behind
`sampler=make_sampler(temp=...)`. This is the F-01 / F-16 shape again: wrong
kwargs against a real signature.

`MLXEngine.generate_stream` had the matching problem. `stream_generate` yields
`GenerationResponse` dataclasses and the code `str()`-ed them:

```
response.text  -> 'hi'
str(response)  -> "GenerationResponse(text='hi', token=1, logprobs=None, ..."
```

so it would have streamed dataclass reprs at the user. Both are fixed to
`.text`, and the `ImportError` fallback is kept and corrected rather than
dropped.

So today the user gets "mlx-lm not installed. Run: pip install mlx-lm", which
is actionable. Installed but mis-called they would have got a `TypeError` from
inside a thread executor. Declaring the dependency and correcting the call site
are one change, not two.

**mlx half, declared.** `mlx-lm>=0.31.0,<0.32` in `[local]`, marked
`sys_platform == 'darwin' and platform_machine == 'arm64'`, mirroring the
factory's own condition exactly because `mlx` has no wheel for Intel Macs or
Linux. Marker evaluated: darwin/arm64 True, linux/x86_64 False, darwin/x86_64
False. The bound is patch-level within one minor on purpose: mlx-lm is 0.x and
has already reshaped this API inside a minor, so `<1.0` would be a meaningless
bound.

**sentence-transformers half, an extra, deliberately not a dependency.**
`pyproject.toml` carries a measured argument for fastembed (~226MB installed)
over a torch-backed sentence-transformers stack (~2.5GB), and that is why
fastembed is the default. Promoting it would contradict a written, measured
decision. But the documented `FERAL_EMBED_FALLBACK=local` mode had no install
path at all, so there is now an `embeddings-local` extra, and a test that keeps
it out of `dependencies` and `[all]` so nobody "completes" the sweep.
`sentence-transformers>=2.2,<6.0`: both API points the code uses
(`SentenceTransformer`, `encode(..., normalize_embeddings=)`) were checked in
the 2.2.0, 3.0.0, 4.0.0 and 5.7.0 artifacts rather than assumed.

`memory/embeddings.py` was **not** edited, another lane owns it, and the fix
did not require touching it.

**Tests.** `feral-core/tests/test_optional_backend_dependencies.py` (7):
**5 fail before, 6 pass + 1 skip after.** The stub mlx_lm is built from the
signature recorded off 0.31.3, and a further test re-checks that recording
against the installed package wherever `mlx_lm` is importable, the F-01
"assert the double against the real thing" pattern. It skips on Linux CI, which
is stated rather than hidden.

---

### F-11 · An install command that installs nothing

**Status:** FIXED, except the one line in a file another lane owns.

**Re-verified.** `macos-extras` is not among the declared extras. Line drift:
the site is `cli/main.py:2675`, not 2593. Confirmed mechanically rather than by
reading, with the script written for F-09:

```
$ python scripts/check_extras_installed.py feral-ai macos-extras
✗ extra 'macos-extras' is not declared by the installed distribution
  (declared: all, bedrock, browser, ... 33 of them)
```

pip does not fail on an unknown extra. It warns to stderr and installs the base
package, so the user runs the command, sees no error, and gets nothing.

**Scope: four sites, three shapes, not one site.** A sweep of every
`feral-ai[...]` string in shipped Python, shell and docs:

```
cli/main.py:2675          feral-ai[macos-extras]   extra does not exist
cli/app_commands.py:274   feral-ai[cli]            extra does not exist
cli/app_commands.py:419   feral-ai[cli]            extra does not exist
cli/main.py:899           feral-ai[all]            extra exists, cannot help
```

`[cli]` is wrong twice over: it does not exist, and the dependency it offers to
supply is `httpx`, which is a **base** dependency (`pyproject.toml:42`). A user
whose `import httpx` fails has an incomplete install; no extra can fix it and
the advice sent them in a circle. `cli/main.py:899` is the same shape with a
real extra: `uvicorn[standard]` is also a base dependency, so `[all]` was never
going to help either.

**Fix.** The three sites outside `cmd_doctor` now say the install is incomplete
and give `pip install --force-reinstall feral-ai`, which is a command that can
actually work.

**The `macos-extras` line itself is inside `cmd_doctor()`, which another lane
owns, so it was not edited.** It did not need to be: the extra is what was
missing, not the message. `macos-extras` now exists, declaring
`pyobjc-framework-EventKit` and `pyobjc-framework-Contacts` (darwin-marked) , 
the bindings `security/macos_permissions.py` imports for the Calendar /
Reminders / Contacts / Full Disk Access probes, which are declared nowhere
else. Bounds match the two PyObjC frameworks already in `dependencies`, because
PyObjC ships one release train. The doctor's printed command is now true.

**For the lane that owns `cli/main.py`:** nothing is required. If you would
rather the doctor named the packages directly, `security/macos_permissions.py:454`
and `:590` already print raw `pip install pyobjc-framework-...` hints and could
be pointed at the new extra for consistency.

**Tests.** `feral-core/tests/test_advertised_extras_exist.py` (3):
**2 fail before, 3/3 after.** It is the class guard, not a check of those four
lines: any future hint naming an undeclared extra fails, and so does any hint
offering an extra as the remedy for a base dependency.

---

### F-12 · The `[wake]` extra above Python 3.11

**Status:** FIXED (documented, guarded and made legible). The audit's statement
is imprecise in both directions and both corrections matter.

**Re-verified, and the cited line is not what the finding says.**
`pyproject.toml:308` is `"openwakeword>=0.6.0,<1.0"`. `tflite-runtime` is not a
declared dependency anywhere in the tree; it arrives transitively, and its
marker is the whole story:

```
openwakeword 0.6.0 metadata:
  tflite-runtime <3,>=2.8.0 ; platform_system == "Linux"
```

**Narrower than stated: macOS is unaffected.** The marker is Linux-only, so
`feral-ai[wake]` installs and works on macOS 3.12+. Confirmed on this machine:
`[wake]` is installed and the extras check passes. macOS being the flagship
platform is why this was never hit.

**Wider than stated: there is no upgrade path.** `tflite-runtime` publishes
wheels for cp38-cp311 and **no sdist at all**, so pip cannot even attempt a
build, and 2.14.0 is the newest release and it is from 2023. Reproduced from
this tree for all three:

```
$ pip download "tflite-runtime>=2.8.0,<3" --no-deps --only-binary=:all: \
    --python-version 3.11 --platform manylinux2014_x86_64
Saved tflite_runtime-2.14.0-cp311-cp311-manylinux2014_x86_64.whl

$ ... --python-version 3.12 ...
ERROR: No matching distribution found for tflite-runtime<3,>=2.8.0
$ ... --python-version 3.14 ...
ERROR: No matching distribution found for tflite-runtime<3,>=2.8.0
```

**"Nothing gates it" is the part with teeth, and it is now gated.**
`openwakeword` was removed from `[all]` in 2026.4.11 for exactly this reason
and nothing has stopped anyone putting it back, while `[all]` is what
`scripts/install.sh` runs and, since F-09, what the pre-publish smoke installs
on 3.11 / 3.12 / 3.14. There are now tests holding it out of both `[all]` and
`dependencies`.

**Deliberately not "fixed" by weakening the extra.** Adding a marker so the
extra resolves empty on Linux 3.12+ would turn a loud pip failure into the
F-11 silent-no-op, and stripping `openwakeword` would cost macOS users a
working feature to describe a Linux limit. Today's behaviour, pip refuses, is
correct; it was undocumented and untested.

So: the `[wake]` extra gained the written rationale the repo's convention
requires and had none, and `feral wake-test` no longer sends a Linux 3.12+ user
to a command that cannot succeed without saying why (pip's own error names
`tflite-runtime`, which means nothing to them).

**Tests.** `feral-core/tests/test_wake_extra_python_ceiling.py` (4):
**2 fail before, 4/4 after.** The other two pass both ways by design: they are
the reintroduction guards on `[all]` and `dependencies`, and that is stated in
the file.

**Noted, not fixed:** `feral-client-v2/src/pages/Settings.jsx:1885` shows
"Install feral-ai[wake] to enable." with no platform caveat. The client cannot
know the brain's interpreter without a new field on the health payload, so it
is out of scope here.

---

### F-13 · Token budget under-counts non-Latin and code-heavy content

**Status:** FIXED. Not committed: left in the working tree.

**Re-verified and measured** against the real tokenizers, `cl100k_base` and
`o200k_base`, taking the worse of the two because the router talks to 16
providers and a budget has to hold for the worst of them:

```
sample          chars    real   //4 said   ratio
english prose    1800     401        450    1.12
python code      1675     575        418    0.73
JSON             1180     540        295    0.55
Russian          2160    1041        540    0.52
Chinese          1080    1260        270    0.21
Japanese         1080    1020        270    0.26
Hebrew           1600    1651        400    0.24
emoji             800    2200        200    0.09
```

A Chinese conversation measured at a fifth of its real size, an emoji-heavy one
at a ninth. `//4` is calibrated for English prose and for nothing else.

**Scope: nine sites in five files, not the one line cited.**

```
agents/context_engine.py:188,192,197   context window budget   (cited: 197)
agents/llm_provider.py:3951            USD budget, candidate routing
agents/learner.py:171,229,230          cost ledger, _cost_guard.record
agents/proactive_engine.py:612         cost ledger, _cost_guard.record
memory/context_builder.py:42           DEFERRED, another lane owns the file
```

The five cost sites are the same defect with a different consequence: spend
against a USD budget is under-reported by the same factor rather than a request
being refused. Eight are fixed; the ninth is deferred below.

Examined and rejected as unrelated: `perception/change_detector.py:179` (byte
offset), `memory/store.py:1830,1891` (float32 blob width),
`models/protocol.py:88` (base64 decoded size).

**Fix.** New `agents/token_estimate.py`, one estimator, used by all eight.

**The asymmetry is the design.** Under-counting produces a hard failure, a
refused request, or a budget silently overshot. Over-counting produces earlier
summarisation, which costs context but keeps working. So it is tuned never to
fall below the real count, and it accepts up to ~2x over on English to get
there. Every weight was measured, not guessed:

- An alphanumeric run up to 12 characters is a word and merges near 4
  characters per token; beyond that it is a hash, UUID or base64, which
  measured 1.38 characters per token, so long runs are charged at 1.
- Cyrillic is charged less than other two-byte scripts (0.7 against 1.3)
  because the vocabularies cover it far better, Russian measured 2.07
  characters per token where Hebrew measured 0.97. That is tokenizer coverage,
  not a property of the script, and the comment says so.
- Whitespace is charged 0.3 rather than 0. It usually merges into the next
  token in Latin text, but Hebrew and Thai under-counted at 0.
- Astral characters are charged 3; emoji measured 2.75 each.

Result over 20 samples: **no under-counts, worst ratio 1.04, worst over-count
2.03x**, against `//4`'s worst of 0.09.

**A real tokenizer was considered and rejected**, with the reason recorded:
`tiktoken` is correct for OpenAI only, it is not in `requirements.lock` (it is
here solely as a transitive of `langchain-openai`), and a network count per
estimate is worse than a heuristic on a hot path.

`estimate_message_tokens` walks content blocks rather than stringifying them,
mirroring `LLMProvider._message_char_count`; stringifying a block list would
count the Python dict syntax around the text.

**Tests.** `feral-core/tests/test_token_estimate_never_undercounts.py` (47):
**3 fail before, 47/47 after.** The corpus lives in
`tests/fixtures/token_estimate_corpus.json` with real token counts recorded, so
the property is provable without tiktoken; a further test re-derives those
counts from live tiktoken wherever it is importable and says out loud that it
skips in CI. There is a test that the corpus still contains the hard cases and
one that the estimator does not over-count English absurdly, because an
estimator returning a huge number would pass everything else and summarise every
conversation on its second turn. The end-to-end case drives the real
`_prune_to_budget` with a Chinese conversation that it used to keep entirely.

**DEFERRED: `memory/context_builder.py:42`**, `(max_tokens_budget // 4) *
_CHARS_PER_TOKEN`. Another lane owns that file. It is the same class and the
fix is the same one-line swap to `agents.token_estimate`.

---

### F-14 · Desktop updater has no shipping channel

**Status:** DEFERRED, `desktop/src-tauri/` is owned by another lane. Verified
and characterised here so the owning lane does not have to redo it.

**Re-verified, exactly as cited.** `desktop/src-tauri/tauri.conf.json:82` is
`"pubkey": ""` and `:73` is `"signingIdentity": null`.

**Two things the audit does not say, and the second changes the conclusion.**

1. **`tauri-plugin-updater` is not a dependency.** `Cargo.toml` lists only
   `tauri-plugin-global-shortcut` and `tauri-plugin-autostart`. So the
   `plugins.updater` block configures a plugin that is not compiled in: the app
   has no updater at all, not merely an unsigned one. Meanwhile
   `"createUpdaterArtifacts": "v1Compatible"` still tells the bundler to emit
   update artifacts, for a plugin that is absent and a key that does not exist.
2. **This is a recorded, deliberate pre-release state, not an oversight.**
   `.github/workflows/desktop.yml` runs on `workflow_dispatch` only, uploads no
   release asset, and its header says why: "not uploaded as a release asset yet
   (needs Apple Developer ID + Windows Authenticode + Tauri updater keypair
   first, TAURI_SIGNING_PRIVATE_KEY, ..., WINDOWS_CERT_P12, WINDOWS_CERT_PW)".
   CLAUDE.md calls the desktop shell experimental.

So "no shipping channel exists" is true and is the project's own position.
The actionable residue is narrow: either drop `createUpdaterArtifacts` until
the plugin and key exist, or add the plugin and key together. Both are one-line
changes in files this lane must not touch.

---

### F-15 · `FeralBrainClient.swift` exists twice and has diverged

**Status:** FIXED. Not committed: left in the working tree.

**Re-verified.** Both copies are inside this repo, so this was actionable here:

```
feral-nodes/ios-bridge/FeralBrainClient.swift                    604 lines
feral-nodes/ios-app/Sources/FeralBridge/FeralBrainClient.swift   774 lines
```

`FeralSensorBridge.swift` is byte-identical in both, as stated (`cmp` agrees).

**The divergence is entirely one-way, which the audit does not say and which
decides the fix.** `diff -u` is 161 added lines and one removed line, and that
line is a `// MARK:` comment that was reworded. `ios-bridge/` is a strict
subset, missing three things:

- `UnifiedPairPayload` / `parsePairingPayload`. Brains at or above 2026.5.8
  emit the unified v1 QR payload; the stale copy decodes only the legacy
  `{host, port, apiKey, nodeName}` shape. **Anything built from it cannot pair
  with a current brain.**
- TLS certificate pinning via `FERAL_BRAIN_CERT_HASH`. The stale copy has no
  `didReceive challenge` handler at all.
- The `sendAudioChunk(_ data: Data)` overload.

**Nothing built `ios-bridge/`.** `ios-app/Package.swift` declares one target at
`path: "Sources/FeralBridge"`, and no manifest, project or script referenced
the other directory. `git log` shows `ios-app/Sources/FeralBridge` received
"phase 5: mobile consolidation, one iOS app, one Android app"; `ios-bridge/`
did not, and was left behind by it.

**Its only referents were documentation, and that is the damage.** Four
non-test references pointed developers at the stale copy as the reference
implementation, including `feral-nodes/android-bridge/README.md` and four
KDoc comments in `FeralBrainClient.kt` citing it with line numbers as the
cross-language contract. That is precisely the mechanism CLAUDE.md warns about:
prose spec, hand-written SDKs, drift.

**Fix.** Deleted the unbuilt subset; repointed all four documentation
references and the four KDoc line-number citations at the built path, with the
line numbers re-derived against the surviving file rather than carried over.

Removing it removes no capability: it is a strict subset of a file that is
compiled, and it is compiled by nothing.

**One existing test had to change, and it is worth flagging.**
`tests/test_hup_message_parity.py:34` read the **stale** copy as the iOS half
of its Swift/Kotlin parity contract. So the gate meant to stop the SDKs
drifting was itself checking parity against a file nothing builds, 170 lines
behind. Repointed; it passes 13/13 against the current file, so the newer copy
satisfies the same contract.

**Checked and not affected: the separate iOS app at
`~/Desktop/Theora-backend-ML`.** Inspected read-only; nothing modified, nothing
pushed, `xcodegen` never run. It vendors neither file. It carries its own
`ios/Theora/Feral/` implementation (`FeralPairingService.swift`,
`FeralPairingPayload.swift`, `FeralHUPModels.swift`, ...), and that one already
decodes the unified v1 payload, so it is current and independent.

**Nothing is required in that repo for F-15.** Worth its own item, though, and
not raised by this audit: it is a **third** hand-written Swift implementation of
the same pairing and HUP surface, so it is a standing candidate for the same
drift. S-3 (generate the protocol) is the structural answer.

**Tests.** `feral-core/tests/test_ios_bridge_single_source.py` (7):
**4 fail before, 7/7 after.** It is the class guard: any duplicated Swift source
basename under `feral-nodes` fails it, sourced from `git ls-files` so build
output cannot register as a copy, with `Package.swift` allowed by name because
one manifest per SwiftPM package is correct. It also pins the three
capabilities that made the choice of copy matter, so a future "consolidation"
cannot quietly keep the weaker file.

---

## Systemic work — after the defect list

Do these in order. Each makes the next cheaper.

**S-1 · Status: DONE (scoped) — commit below.**

`.github/workflows/ci.yml` gains a non-blocking `typecheck` job; `feral-core/mypy-baseline.txt`
is the committed baseline. **719 errors in 204 files** (mypy 1.20.2, Python 3.11).

*Citation check:* the audit says 324 errors in 103 files "at default settings". I measure
719/204 with `--ignore-missing-imports --exclude '^build/'`. Not necessarily a contradiction,
since the flags and scope differ, but 324 should not be quoted as the baseline. The reproducible
command is recorded in the baseline header.

**The exclusions are load-bearing.** Without `--exclude '^build/'` mypy does not run at all:
`Duplicate module named "agents"` / `errors prevented further checking`. Trap 1 in CLAUDE.md
degrades the tool to zero rather than to a wrong number.

**call-arg: 6 repo-wide, and only 3 are real.**

- Real, and they are F-16's: `skills/impl/agentic_computer_use.py:272` (`provider`, `model`,
  `api_key`). Fixed under F-16 rather than here, to keep one item per commit.
- False positive: `skills/registry.py:120` and `:329`. The call is `obj()` where `obj` is a
  `BaseSkill` *subclass*; mypy narrows to `type[BaseSkill]`, which does require `skill_id`,
  but every concrete subclass defines `__init__(self)` and passes it to `super()`. Verified
  in `pdf_reader.py:37`, `plan.py:101`, `code_interpreter.py:370`.
- ~~False positive: `mcp/registry.py:267`~~ **RESOLVED.** `url` has a default of `""`.
  I originally reported that the pydantic mypy plugin crashes on mypy 1.20.2 and left it
  disabled, planning 62 mechanical `Field("", ...)` -> `Field(default=...)` edits instead.
  **That was wrong.** Re-tested on the owner's prompt: the plugin loads and runs clean on
  mypy 1.20.2 with pydantic 2.13.3 across the whole tree. The earlier crash does not
  reproduce and was most likely a stale `.mypy_cache` from a run without the plugin.
  Enabling it removes this false positive, drops the baseline 719 -> 683, costs one config
  line, and makes the 62 edits unnecessary. Config now in `feral-core/mypy.ini`.

So S-1 fixed no call-arg errors directly: the only real ones belong to F-16. Saying so is more
useful than manufacturing three edits.

No `# type: ignore` was added for the false positives. CLAUDE.md records 52 existing unvalidated
suppressions; a false positive documented in the baseline is auditable, one silenced in source
is not.

Once F-16 lands, call-arg is at zero, which makes it the cheapest code to promote from warning
to failure, and it is the class that hid F-01.

Original plan text follows.

**S-1 · Turn on the type checker.** Add `mypy --ignore-missing-imports` to `ci.yml` as a **non-blocking** job, then ratchet down. Baseline is 324 errors in 103 files at default settings — reproduce it before changing anything. 111 are the `AttributeError` class (`attr-defined` + `union-attr`); 6 are `call-arg`, the class that produced F-01. Annotations already cover 92.5% of parameters, so this is configuration, not annotation work. Note the 52 existing `# type: ignore[code]` comments in source are unvalidated suppressions from an earlier unrecorded mypy run.

**S-2 · Widen the lint.** Add `BLE001` (1,452 hits), `S110` (210), `RUF013` (56), `ASYNC240` (37), `B023` (4), `B006` (1) to the ruff selection. Introduce them one rule at a time with a per-rule baseline so the build stays green. `ASYNC240` in particular would have caught F-05. Also extend lint beyond `feral-core` — six other Python trees (102 files) have none.

**S-3 · Generate the protocol instead of hand-writing it.** Export `pydantic.model_json_schema()` from `models/protocol.py` and generate the TypeScript, Swift, and Kotlin types from it. This eliminates the F-07 and F-08 class permanently and is a prerequisite for any TypeScript work. Today `HUP_SPEC.md` is prose and every SDK is a human reading it; `HUP_VERSION` is hand-written in six places across four languages, and `tests/test_hup_version_unified.py` keeps them aligned by regex-scraping TypeScript and Swift source from Python.

**S-4 · Fix distribution without leaving Python.** Ship a relocatable interpreter (`python-build-standalone` or `uv`'s managed Python) inside the Tauri bundle. This permanently removes the worst finding in the audit: `memory/embeddings.py:411-420` documents that an interpreter built without `--enable-loadable-sqlite-extensions` (pyenv's macOS default) makes `sqlite-vec` unloadable, and `embeddings.py:288` calls the resulting brute-force fallback "the DEFAULT path for a large share of installs". Product correctness currently depends on how the user compiled CPython.

---

## Known-clean — do not spend time here

Verified during the audit and found sound. Listed so nobody re-investigates.

- **The GIL is not a problem.** Zero `ThreadPoolExecutor`, `ProcessPoolExecutor`, `multiprocessing`, or `concurrent.futures` anywhere, tests included. The only two GIL references *rely* on it, for atomic deque operations at `perception/glasses_buffer.py:123`.
- **All 31 `asyncio.run()` call sites are safe.** `security/content_defense.py:601` correctly probes for a running loop and degrades.
- **No secrets are committed.** `.env` is gitignored and absent from `git log --all`; a history scan for key formats finds only test fixtures. (The untracked working-copy `.env` does hold live credentials and sits at the repo root — one `git add -f` from exposure.)
- **Dependency management is genuinely strong.** Committed lockfile with 135 pins, explicit upper bounds with written rationale, an unconstrained smoke matrix, a TestPyPI canary. Do not "improve" it without reading the inline comments in `pyproject.toml` explaining why each bound exists.
- **`genui/a2ui_protocol.py` has zero importers.** It is dead code and contradicts `genui/generator.py` on component names. Delete rather than reconcile.

---

## F-17 · Memory search: the advice was backwards, and the scan was rebuilt per query

**Status:** FIXED, uncommitted, in the working tree.

Two defects, one root: nobody had measured the path they were telling users
to escape.

### 1. The "degraded, rebuild CPython" advice was wrong on performance

`memory/embeddings.py` called the numpy path "degraded", and `feral doctor`
and the setup wizard both told the operator to rebuild their interpreter
with `--enable-loadable-sqlite-extensions` to get off it.

**sqlite-vec 0.1.9 (the pinned version) builds no ANN index.** A vec0
`MATCH` is itself a full scan, so both paths are O(n) and the choice was
never linear-versus-sublinear. Measured on this machine, top-5 over 384-dim
vectors, identical results to seven decimals:

| corpus | numpy | sqlite-vec vec0 |
|---|---|---|
| 12,000 | 0.46 ms | 7.08 ms |
| 50,000 | 2.42 ms | 10.98 - 28.53 ms |
| 100,000 | 3.97 ms | 56.99 ms |

The prescribed fix was a ~10x slowdown. The honest argument for sqlite-vec
is resident memory (numpy holds ~18 MB at 12k rows, ~154 MB at 100k), so
the rebuild instructions are kept and re-attached to that reason.

Corrected, every site found by grep (`build/lib/` is the duplicate tree from
trap 1 and was left alone):

- `memory/embeddings.py:8` module docstring, "(degraded, still works)".
- `memory/embeddings.py` `cosine_similarity_bulk`, "not an exotic degraded
  path" plus a new measured comparison table, which is now the one place the
  numbers live and everything else cites.
- `memory/embeddings.py` `_try_load_sqlite_vec`, the WARNING telling users to
  rebuild. Now INFO, reason changed to memory.
- `memory/embeddings.py` `VectorIndex` class docstring.
- `cli/main.py` `feral doctor`. Was `_warn` with the rebuild in
  "Suggested fixes:"; now `_info` with no suggested fix. It still refuses to
  green-tick, because the operator did configure a backend that is not
  running and that remains worth saying.
- `cli/setup/steps/memory.py` module docstring and both `_report_sqlite_vec`
  branches.
- `api/routes/memory.py` `_runtime_vector_state` reason string, and the
  `/internal/memory/stats` docstring. **Wire field names
  (`degraded_semantic_search`, `vector_index_degraded*`) were NOT renamed** -
  four languages consume this payload. The docstring now says the name
  oversells what the field means.
- `memory/store.py` stats: `vec_index_mode` "(degraded)" -> "(numpy scan)".
- `memory/vector_index_backends/base.py` Protocol docstring, which also
  claimed `indexed=False` fell back to "FTS5 keyword search". It does not;
  the vector leg still runs, over numpy.
- `memory/vector_index_backends/sqlite_vec.py`, two "degraded mode" lines.

`tests/test_doctor_vector_backend_truth.py` asserted the old wording and was
updated with it: it now pins that the rebuild is NOT offered as a fix, that
the instructions are still reachable, and that the row says neither
"degraded" nor "O(n) per query".

### 2. The corpus matrix was rebuilt from BLOBs on every query

`_centered_similarity` re-read every embedding and rebuilt its float32
matrix per query. None of that work depends on the query. Measured on a copy
of the real store (`~/.feral/memory.db`, 11,613 episode chunks, 384 dims;
copied with its `-wal`/`-shm`, the live file was never opened):

```
SELECT every embedding BLOB   23.3 ms
join + decode + centre         9.1 ms
------------------------------------
vector leg                    32.4 ms   every query
the mat-vec that uses the query 0.35 ms
```

**Fix.** `MemoryStore._centered_corpus` builds the centred matrix once and
keeps it, keyed on a fingerprint of the corpus. `_centered_similarity` was
split into a query-independent half (`_centered_docs`) and a one-mat-vec
scoring half (`_score_centered`), so the cached matrix is produced by the
same arithmetic in the same order as before and the scores are bit-identical
rather than merely close.

**Memory.** One matrix, never two. `docs = unit - centroid` allocated a
second full matrix and dropped the first a line later; it is now an in-place
`unit -= centroid`. The blob list and the joined buffer are released before
the caller runs. Above `_CORPUS_CACHE_MAX_BYTES` (256 MB, so 100k chunks
still fits) the matrix is used and then dropped rather than retained, so a
very large store degrades to the old cost instead of to an OOM.

**Invalidation.** `SELECT COUNT(*), MAX(rowid) FROM memory_chunks WHERE
source_table = ?`, 0.39 ms, because `idx_chunks_source` covers it. Adding
`AND embedding IS NOT NULL` makes it read every table row and costs 16 ms,
which would eat most of the saving, so the count is used to detect change
and never as a number of usable vectors. Both values are read from SQLite,
so writes from any connection or process count.

`PRAGMA data_version` was tried and **rejected**: it reports commits by
other connections on any table, and `_bump_access` commits an
`UPDATE episodes` after every single search, so it changed on essentially
every query and the cache never hit. Recording this because it is the
obvious first choice and it does not work here.

The one writer this does not catch is an in-place `UPDATE ... SET embedding`,
which only `feral memory reembed` does. It runs in its own process and
already ends by printing "Restart the brain to pick it up." That contract
predates the cache.

**Measured, before -> after, on the real-store copy** (40 queries drawn from
the store's own episode summaries, 3 repeats, `time.time` frozen so the
recency prior cannot move):

| | before | after |
|---|---|---|
| `episode_search_hybrid`, median | 45.31 ms | 8.61 ms |
| vector leg alone, warm | 32.4 ms | 1.02 ms |
| matrix assemblies / 121 queries | 121 | 1 |
| peak RSS | 174.5 MB | 137.7 MB |

Peak RSS *falls* because the old code allocated three full matrices per
query and churned them; the new code holds one 17.8 MB matrix and stops
allocating.

**Results unchanged: 36 distinct queries, 227 returned rows, zero
differences** in id, order, or `relevance_score` to 9 decimals.

**Test written first.** `tests/test_memory_corpus_matrix_cache.py` asserts
the assembly *count* and the cache *identity*, not latency, because a
latency assertion is flaky on a shared machine. Against the unfixed source
it fails 3/5 ("corpus matrix rebuilt 3 times for 3 queries"); after, 5/5.
It also pins invalidation, including an end-to-end "a new episode is
findable immediately" case, because a cache that never invalidates silently
stops returning new memories.

### Siblings

**Fixed here, same defect:** `_centered_filter`, the *indexed* path, re-read
the entire corpus and rebuilt the whole matrix on every query purely to
recompute a centroid that had not moved. It now refreshes the centre only
when the fingerprint changes, and deliberately does **not** retain the
matrix, because keeping the corpus out of RAM is the reason to run an index.
Verified with a stubbed vec0 backend over the real store: median 43.74 ->
8.28 ms, corpus assemblies 1 instead of 121, 206 rows identical, peak RSS
200.0 -> 192.6 MB (no matrix retained).

**Found, not fixed, same shape.** Each fetches every blob and rebuilds a
matrix per call. All are sub-millisecond on this store, so fixing them would
add a second and third resident matrix to save ~0.3 ms, and each needs its
own invalidation key:

- `memory/notes_legacy.py:203`, notes corpus, 386 chunks, 0.27 ms/fetch.
- `memory/knowledge_graph.py:672`, `entity_search_hybrid`, 312 entities,
  0.37 ms/fetch.
- `memory/knowledge_graph.py:992`, `_find_similar_entity`. **The one most
  likely to bite:** it is on the ingest path, not the query path, so it is
  O(entities) per entity resolved and therefore quadratic over a batch.
  Worth its own item once the entity table is larger than 312 rows.
- `memory/backends/sqlite_vec.py:172`, the pluggable-backend record scan.

**Proof:** `python -m pytest tests/ -q -p no:cacheprovider -p no:randomly
--no-cov` -> **7455 passed, 29 skipped, 0 failed** (341s). `ruff check
--select=E,F,W --ignore=E501,E402,F401,W291,W293 .` -> **All checks passed**.
Run on pyenv 3.11.11 via `PYENV_VERSION`, because `.python-version` is
pinned to an interpreter that is not installed yet by concurrent work.

---

## F-18 · The knowledge graph was dead in production: entities were never re-embedded

**Status:** FIXED (uncommitted, in tree).

Not from the audit list. Found by running the real store.

### The failure, reproduced

`~/.feral/memory.db` on this machine: `entities.embedding` held 312 rows of
6144-byte (1536-dim, OpenAI-era) blobs while `memory_chunks.embedding` held
11,999 rows of 1536-byte (384-dim, current fastembed BAAI/bge-small-en-v1.5)
blobs. `feral memory reembed` had run at some point and fixed the chunks. It
only ever knew about `memory_chunks`, so it never touched `entities`. That gap
is the whole defect.

`KnowledgeGraph.search_entities` embeds the query at 384 dims and compares it
against the stored 1536-dim vectors, so `cosine_similarity_bulk` raised
`EmbeddingDimensionMismatch` on **every** call. There was no handler at
`memory/context_builder.py:365`, so it propagated out of
`MemoryStore.search_all` into `gateway/protocol.py:373` (the `memory.search`
RPC), `agents/taskflow.py:541` (the taskflow `memory.search` step) and
`api/routes/memory.py:559`.

Measured against a copy of the real store (copied with its `-wal`/`-shm`; the
live file was never opened), running the unfixed source at `HEAD`:

```
search_entities('FERAL')     -> EmbeddingDimensionMismatch: query vector has 384
                                dims but stored vector has 1536 (6144 bytes)
... 8 of 8 queries identical
search_all('FERAL')          -> EmbeddingDimensionMismatch  (same)
... 8 of 8 queries identical
```

So one stale table took out episode, note and knowledge recall as well, and the
only trace was a single throttled ERROR line whose advice ("clear the vector
tables") named no command.

### Every table carrying vectors, and its width

Discovered by walking `sqlite_master`, not assumed. On the real store, active
provider = fastembed, 384d:

| table | column | rows with vectors | width | verdict |
|---|---|---|---|---|
| `entities` | `embedding` | 312 | 6144 B = **1536 floats** | stale, migrated |
| `memory_chunks` | `embedding` | 11,999 | 1536 B = 384 floats | current |
| `vec_chunks` | vec0 `FLOAT[384]` | 0 | 384 declared | ok |
| `vec_entities` | vec0 `FLOAT[384]` | 0 | 384 declared | ok |

Nothing else in the 54 tables holds a vector. The fts5 and vec0 shadow tables
(`entities_fts_content`, `vec_entities_vector_chunks00`, ...) are owned by their
virtual table and are excluded deliberately, with a test pinning that.

### 1. The migration is a module now, and it discovers

`memory/reembed.py`. A registry of `EmbeddingTable(table, id, text, embedding)`
says what to rewrite and from which source text (`memory_chunks.text_content`,
`entities.name`, matching what `_index_chunk` and `add_entity` actually embed).
`scan_store` then walks every table for vector-shaped columns and reports any it
cannot migrate, because a hard-coded list is exactly what caused this bug:
`reembed_store` returns `ok: False` and the CLI exits non-zero when anything is
left stale, rather than printing success over a still-broken store.

Deliberate refusals, both tested:

- A row whose source text is gone is set to NULL, not left stale. Readers filter
  on `embedding IS NOT NULL`, so NULL costs one row's recall while a stale blob
  keeps the whole table's vector leg raising.
- The migration refuses to run on a degraded provider. `EmbeddingProvider` falls
  back to a deterministic *hash* embedding, which has the right width and no
  meaning; writing it would look like a successful migration while replacing the
  store's semantics with noise.

vec0 mirrors are handled too: their dimension is baked in at CREATE time and
`CREATE VIRTUAL TABLE IF NOT EXISTS` is a silent no-op against a stale one, so a
mismatched mirror is dropped and repopulated from the migrated source column.
**Not exercised on this machine**: pyenv 3.11.11 is built without loadable
extensions, so sqlite-vec cannot load and every vec0 path is inert here. The
decision function (`vec0_declared_dim`, parsed from the DDL, which is the only
place the dimension is recorded) is unit-tested; the drop/rebuild branch is not.

**Sibling fixed:** `KnowledgeGraph._init_schema` created `vec_entities` with no
declared-dimension guard, unlike `embeddings.SQLiteVecIndex._init` which has had
one since `vec_chunks` shipped. The same provider switch would have left that
index silently unwritable (every upsert rejected and swallowed at debug level).
It now refuses a stale-dim index and names the command that rebuilds it.

### 2. The failure is no longer hideable

`search_all` was the interesting decision. Both obvious options are wrong.
Propagating is what happened: one dead tier destroyed three healthy ones at three
call sites. Wrapping it in `except Exception: return []` would be **worse**, and
this is written in the code as a comment: an empty result set from a search
function reads as "nothing matched", so a broken store looks like an empty one
and nobody investigates. That is precisely how this survived to a release.

So: per-tier isolation with a *declared* degradation. A failing tier is recorded
in `store.last_search_degradations`, mirrored to `store._vector_leg_error` (which
`/internal/memory/stats` already reports as `semantic_search: degraded`), and
logged at ERROR with the command to run. If **every** tier fails the exception
propagates, because at that point `[]` is a lie about the store's contents rather
than a reduced answer.

The KG degrades the *leg*, not the query: FTS entity hits are still returned, and
`_link_entity` (the ingest path, inside `add_entity`) returns "no link" instead of
raising, so knowledge ingest keeps working while the store waits for a migration.
Both publish through `KnowledgeGraph.vector_leg_error`. The KG's ERROR line is
throttled to once per distinct failure per instance, same shape as
`embeddings._REPORTED_DIM_MISMATCHES`, since the state stays readable in the
stats endpoint (18 log lines became 1 across 16 queries).

Every message that used to say "clear the vector tables" now names
`feral memory reembed`, and the two comments claiming "there is no re-embedding
migration in this codebase" were false as of this change and were updated.

### 3. knowledge_fts indexed the corpse

`migrate_knowledge_to_kg` ends with `ALTER TABLE knowledge RENAME TO
knowledge__deprecated`. SQLite rewrites every trigger that referenced the old
name to follow the renamed table, so `knowledge_ai` / `knowledge_ad` /
`knowledge_fts_update` all followed it, and `_init_db`'s
`CREATE TRIGGER IF NOT EXISTS` statements were then no-ops because the names were
taken. The recreated `knowledge` table got no FTS triggers at all. On the real
store: `knowledge` 0 rows, `knowledge__deprecated` 29 rows, `knowledge_fts` 29
rows of deprecated content.

That is not merely stale. `_knowledge_search_flat` joins `knowledge_fts.rowid` to
`knowledge.rowid`, and a recreated table restarts rowids at 1, so the first row
written on the legacy path (`memory.kg.unified = false`, kept for chaos/recovery)
inherits a deprecated row's search terms. Pinned by
`test_stale_index_returns_the_wrong_row`, which fails against the unfixed source
by returning a `Cairo` row for the query `cerulean`.

`MemoryStore._repair_knowledge_fts` runs at boot, is idempotent, rebinds the
three triggers to the live table and rebuilds the index from it (`knowledge_fts`
stores its own content, so the fts5 `'rebuild'` command does not apply and the
rows are replaced by hand). The deprecated rows are not destroyed, and they are
not lost either: the F1 migration ported all of them into the KG. The cause is
fixed too, so a fresh migration cannot recreate the state.

### Proof on a copy of the real store

Copied `~/.feral/memory.db` with its `-wal` and `-shm`; the live file was never
opened. `FERAL_HOME` pointed at the copy, then the exact user-facing command:

```
$ feral memory reembed check
  Provider: fastembed  (384d)
  Stored vectors:
    entities.embedding: 1536d x312   <- stale
    memory_chunks.embedding: 384d x11999
    vec_chunks (vec0 index): 384d  [ok]
    vec_entities (vec0 index): 384d  [ok]
  Dry run; nothing written. Run `feral memory reembed` to migrate.

$ feral memory reembed
    entities: 312 stale vector(s)
    entities: 64/312 ... 312/312
  entities: re-embedded 312, cleared 0 with no source text
  Every stored vector now matches the active provider.
  Restart the brain to pick it up.          (11.8s wall)
```

Entity search on that migrated copy, real queries, real rows:

```
'FERAL'            -> 'Feral' (0.748), 'feral launch' (0.666), 'FERAL brain' (0.638)
'heart rate'       -> 'heart_rate 115bpm (Bluetooth Device)' (0.631), '95-100 bpm' (0.560)
'whoop'            -> 'Whoop' (0.745), 'spo2 93% (WHOOP)' (0.562)
'cute bot'         -> 'CuteBot' (0.647), 'Telegram bot' (0.562), 'cutebot.follow_line' (0.554)
'bluetooth device' -> 'heart_rate 115bpm (Bluetooth Device)' (0.585), 'firmware' (0.515)
8 of 8 queries answered; kg.vector_leg_error = None
search_all: 8 of 8 answered (was 0 of 8), store._vector_leg_error = None
build_graph_context('heart rate') returns a populated graph section again
```

`'cute bot' -> 'CuteBot'` and `'bluetooth device' -> 'firmware'` are the vector
leg working, not FTS: on the same store *before* migration those queries return
0 and 1 lexical hits respectively.

Degradation was verified separately, fixed source against an **unmigrated** copy:
8 of 8 entity queries return FTS hits instead of raising, 8 of 8 `search_all`
calls answer, one ERROR line names `feral memory reembed`, and
`_vector_leg_error` is populated so the stats endpoint reports
`semantic_search: degraded`.

### What a user runs

```
feral memory reembed check     # reports every table and width, writes nothing
feral memory reembed           # migrates, then restart the brain
```

No feature was removed. The only behaviour change is that a tier failure inside
`search_all` now degrades and is reported instead of aborting the call, and
`feral memory reembed` exits non-zero when it cannot fully migrate.

### Tests

`tests/test_memory_reembed_all_tables.py` (11),
`tests/test_memory_stale_vector_visibility.py` (12),
`tests/test_knowledge_fts_live_table.py` (7). Against the unfixed source at
`HEAD` (a pristine copy of the tree with only these files reverted, so the other
agents' in-flight work was not disturbed): **29 failed, 1 passed**. The one pass
is `test_the_reproduction_is_faithful`, which exists to guard the fixture. After:
**30 passed**. Headline failures before the fix were on their own assertions, not
on imports: `entities were left at the old provider's dimension` (`{24: 3} !=
{8: 3}`), `EmbeddingDimensionMismatch` escaping `search_all`, and the FTS index
returning `Cairo` for `cerulean`.

**Proof:** `PYENV_VERSION=3.11.11 python3 -m pytest tests/ -q -p no:cacheprovider
-p no:randomly --no-cov` -> **7591 passed, 30 skipped, 0 failed** (443s).
`ruff check --select=E,F,W --ignore=E501,E402,F401,W291,W293 .` ->
**All checks passed**.

---

## S-4 · Distribution: the interpreter is now pinned for dev and bundled for users

**Status:** FIXED, uncommitted, in the working tree.

S-4 asked for a relocatable interpreter inside the Tauri bundle. Doing it
surfaced three defects that had to be fixed first, because the bundle
alone would not have worked and the obvious interpreter choice was wrong.

### 1. The spike's interpreter cannot boot the brain

An earlier bundling spike validated python-build-standalone **3.11.13**
on `enable_load_extension` alone and stopped there. 3.11.13 links SQLite
**3.49.1, which has no FTS5**:

```
$ .../cpython-3.11.13-.../bin/python3 -c "import sqlite3; print(sqlite3.sqlite_version); \
    sqlite3.connect(':memory:').execute('create virtual table t using fts5(x)')"
3.49.1
sqlite3.OperationalError: no such module: fts5
```

`MemoryStore` reached that error unguarded during construction:

```
  File ".../memory/store.py", line 443, in __init__
    self._init_db()
  File ".../memory/store.py", line 890, in _init_db
    conn.execute("""
sqlite3.OperationalError: no such module: fts5
```

Bundling 3.11.13 would have shipped an app that installs, launches, and
whose brain never starts. The features are independent and both must be
checked. Measured, macOS arm64:

| Interpreter | SQLite | FTS5 | loadable extensions |
|---|---|---|---|
| pyenv 3.11.11 | 3.51.0 | yes | no |
| pbs 3.11.13 | 3.49.1 | **no** | yes |
| pbs 3.11.15 | 3.53.1 | yes | yes |

3.11.15 is the pin, in `.python-pin`, shared by `make dev` and the
desktop bundle so the two cannot drift.

### 2. The FTS5 assumption was unguarded in five places

`memory/sqlite_features.py` is new: it probes both features separately,
memoises, and exposes `require_fts5()`. `MemoryStore._init_db` and
`KnowledgeGraph._init_schema` call it **before** their first statement,
not lazily at the first `CREATE VIRTUAL TABLE`, because the old ordering
committed `notes` (or `entities`, `entity_aliases`, `relations`) and their
triggers first and then died, leaving a database whose triggers referenced
a table that did not exist. Verified: after the guard, no `memory.db` is
created at all.

The resulting error names the interpreter, the SQLite version, what
breaks and the remedy, and `feral serve` already renders it without a
traceback:

```
🦝  Brain failed to start: FERAL's memory store requires SQLite FTS5, and this
interpreter does not have it.
  interpreter : .../venv1113/bin/python (Python 3.11.13)
  sqlite      : 3.49.1 (built without FTS5)
...
Fix: Use an interpreter whose SQLite has FTS5. The repo pin (see .python-pin) is
CPython 3.11.15 from python-build-standalone: ...
```

`tests/test_sqlite_interpreter_features.py` includes a structural test
that fails if any module gains a `USING fts5` without importing the
guard, so a sixth site cannot reintroduce this.

### 3. `feral doctor` checked one feature and not the other

Doctor had no FTS5 probe at all: on the 3.11.13 host it printed a green
`Python version 3.11.13` and never mentioned SQLite. The loadable-extension
state was only visible indirectly, inside `Memory vector backend`, where it
was also conflated with "sqlite-vec is not installed": a rebuild versus a
pip install.

Two rows now, with deliberately different severities, which is the point:

* `SQLite FTS5`: `_fail`, with the interpreter change in "Suggested fixes:".
* `SQLite loadable extensions`: `_info`, no suggested fix. F-17 measured
  the numpy path as the faster one, so prescribing a rebuild here is a
  ~10x slowdown. The instructions stay in the detail line, attached to
  resident memory, matching what `test_doctor_vector_backend_truth.py`
  already pins for the vector row.

### 4. The desktop app could only start the brain on the machine that built it

`desktop/src-tauri/src/main.rs` resolved feral-core from
`env!("CARGO_MANIFEST_DIR")`, which rustc expands at **compile** time, so
the shipped binary carried the build machine's absolute source path and
`.canonicalize()` returned Err everywhere else. It also spawned bare
`python3` from the user's `PATH`.

Both are now run-time resolutions (`FERAL_CORE_DIR` / bundled resource /
bounded upward walk from the executable; `FERAL_PYTHON` / bundled
interpreter / repo `.venv`), every interpreter candidate is FTS5-probed
before use, `PATH` is never consulted, and failures name every candidate
tried. `desktop/scripts/stage_bundle.sh` stages the payload and
`tauri.conf.json` ships it.

**Proof.** The built `FERAL.app` was copied to a directory with no FERAL
checkout above it and launched with `FERAL_CORE_DIR` and `FERAL_PYTHON`
unset and `PATH=/usr/bin:/bin`:

```
BRAIN_STARTED_BY_APP: True
HEALTH: {"status":"ok","version":"2026.8.8", ...}
BRAIN_COMMAND: .../isolated/FERAL.app/Contents/Resources/python/bin/python3 -m api.server
BRAIN_CWD:     .../isolated/FERAL.app/Contents/Resources/feral-core
```

Caveat recorded honestly: the test machine is also the build machine, so
this does not by itself prove the old `CARGO_MANIFEST_DIR` path would have
failed here. What it does prove is that the app resolved to its own
bundled payload and used nothing from the checkout. The compile-time
dependency is covered by `resolution_uses_only_runtime_paths` in the Rust
tests, which asserts two different runtime layouts produce two different
answers.

### 5. The pin file itself was breaking the repo

The pin was initially placed in `.python-version`. **pyenv reads that
filename**, and when it names a version pyenv does not have, pyenv's
shims do not fail, they fall through. Measured inside this repo while
such a file existed:

```
$ python3 -c "import aiosqlite"
ModuleNotFoundError: No module named 'aiosqlite'
$ python3 -c "import sys; print(sys.executable)"
/opt/homebrew/opt/python@3.14/bin/python3.14
$ ruff --version
pyenv: version `3.11.15' is not installed ...
pyenv: ruff: command not found                     # exit 127
```

It broke this session's own post-edit lint hook. Removing the file
restored both immediately (`python3` -> 3.11.11, `ruff 0.15.10`, exit 0).
The pin now lives in `.python-pin`, read only by this repo's tooling;
`.python-version` is gitignored and `make dev` refuses to run while one
exists.

### Development environment

`make dev` from a clean clone is one command and needs no pre-installed
Python: `scripts/ensure_uv.sh` supplies a uv >= 0.12 (repo-local download
into `.uv/` when the system uv is older, leaving it untouched), uv
installs 3.11.15, `.venv/` is created and `feral-core[all,dev]` installed
against `requirements.lock`. `dev-verify` now **fails** on a missing FTS5
instead of warning.

The extras were moved from `[llm,dev]` to `[all,dev]` to match CI exactly.
With `[llm,dev]`, `tests/test_doctor_severity.py` fails two tests on a
freshly built dev environment, because `feral doctor` correctly warns
"Playwright (driver lib) not installed" and that test asserts a clean
install emits no warnings. A green CI beside a red local run, caused by
the environment rather than the code, is the same class of surprise this
whole change is about. Verified that `[all,dev]` resolves on 3.11.15 with
no source builds.

Verified on a copy of the tree with no `.venv` and no `.uv`, and a `PATH`
containing only uv 0.7.20: the script fetched uv 0.12.3 locally, built
3.11.15, and the resulting environment booted a real brain
(`/health` -> `{"status":"ok",...}`).

### Files

`memory/sqlite_features.py` (new), `memory/store.py`,
`memory/knowledge_graph.py`, `cli/main.py`,
`tests/test_sqlite_interpreter_features.py` (new),
`tests/test_doctor_severity.py`, `Makefile`, `scripts/ensure_uv.sh` (new),
`.python-pin` (new), `.gitignore`, `desktop/src-tauri/src/main.rs`,
`desktop/src-tauri/tauri.conf.json`, `desktop/src-tauri/.gitignore` (new),
`desktop/scripts/stage_bundle.sh` (new), `desktop/package.json`,
`desktop/src/main.js`, `CLAUDE.md`, `README.md`, `CONTRIBUTING.md`,
`ONBOARDING.md`, `desktop/README.md`.

---

## F-18 · The tool-execution audit trail had one writer, and the traffic moved

**Status:** FIXED, uncommitted, in the working tree.

**Re-verified 2026-08-12** against a copy of the live store
(`~/.feral/memory.db`, copied to scratch; the brain may start at any time,
so nothing was written under `~/.feral`).

```
execution_log rows                     206
first row                              2026-04-23 14:24:33
last row                               2026-05-21 15:16:47
episodes rows after that date          6,348
episodes with event_type='tool'        33, dated 2026-06-30 .. 2026-08-06
```

So the trail is dead exactly as reported, and the system was not idle:
33 real tool calls executed in the gap and left an episode each.

**The cause is not a break, it is a missing connection.** `log_execution`
works. Called against a copy of the real store it inserts and reads back
correctly. The row had exactly one writer, `Orchestrator`, in its two
chat loops. Every other tool-execution path writes nothing:

| path | writes execution_log (before) |
|---|---|
| `agents/orchestrator.py` non-streaming loop | yes |
| `agents/orchestrator.py` streaming loop | yes |
| `voice/realtime_proxy.py` | no |
| `voice/gemini_realtime.py` | no |
| `mcp/server.py` (external MCP clients) | no |
| `api/routes/tools.py` | no |
| `agents/multi_agent.py` | no |
| `agents/direct_execution.py` | no |
| `agents/ui_handlers.py`, `agents/refusal_handler.py` | no |

The 33 episodes in the gap all carry `"source": "voice_realtime"`.
`git log` confirms no commit ever removed a `log_execution` call: the
writer was never there for those paths. What changed around 2026-05-21 is
which path the traffic used.

**Fix: write the row at the chokepoint, the same place the plan-mode gate
already moved to.** `SkillExecutor.execute` is the one function all nine
paths reach. New `memory/execution_audit.py` resolves the store through
`sys.modules["api.state"]` (the pattern `SkillExecutor._gate` established,
so `memory` gains no dependency on `api`) and writes the row.

`Orchestrator` keeps its own writes, because the chat path also dispatches
tools that never reach the executor: `mcp_*` tools, `daemon_*` commands,
`subagent__spawn_subagent`, and every refusal that returns before
dispatch. `claimed_by_caller()` keeps the two writers disjoint - a
ContextVar, so it survives the `asyncio.gather` fan-out the orchestrator
runs its tools through, since a Task copies the context at creation.

**Voice, MCP and multi-agent now bind a `ToolCallContext`.** They called
`SkillExecutor` with nothing bound, so `session_id` was `""`. That was
already affecting more than the audit: `SkillExecutor._gate` reads the
session from the same contextvar, so the approval gate was evaluating
every voice, MCP and multi-agent tool call against session `""`. Voice
re-checks plan mode itself, so plan mode held; `enforce_safety` did not.

**Not silent when it cannot write.** No `api.state` at all means offline
tooling (tests, CLI, an embedder) and stays quiet. `api.state` present
with no store, or an insert that raises, logs once per process at warning
naming the surface and the tool. A trail that stops with no log line is
the whole finding.

**Live proof** on a copy of the real store: 206 rows before, 207 after one
voice-bound executor call, with `session_id='voice-live-proof'`; the
claimed call wrote nothing.

New tests fail 4/13 against the unfixed source and pass 13/13 after.

**Files:** `memory/execution_audit.py` (new), `skills/executor.py`,
`agents/orchestrator.py`, `agents/multi_agent.py`,
`voice/realtime_proxy.py`, `voice/gemini_realtime.py`, `mcp/server.py`,
`tests/test_execution_audit_trail.py` (new).

---

## F-19 · A tool call whose arguments failed to parse ran with no arguments

**Status:** FIXED, uncommitted, in the working tree.

This is the cause of the reported `web_search` 61/61 failure rate, and it
is the strongest single instance of the repo's defect class found in this
pass.

```
skill_id=web_search  61 executions, 61 failures, 0 successes
every row's args     '{}'
every row's error    "Missing search query. Provide 'query' or 'q' parameter."
all 61 on            2026-05-15
anti-loop guard      fired at streaks of 5, 6 and 7
```

The skill accepts `query`, `q`, `search` or `text`. It received an empty
dict 61 times. Four sites in `agents/llm_provider.py` did
`json.loads(...) except: args = {}` with no log line: 481 (Responses API
finaliser), 1043 (chat-completions `extract_response`), 2494 (SSE
`[DONE]`), 2898 (Anthropic `message_stop`). A truncated or malformed
arguments blob became a valid-looking argument-free call, the skill
answered with its missing-field message, and the model read that as a
tool problem rather than its own output being lost, so it re-issued.

Not one bad reply from one model. The same shape on other days:
`computer_use__bash` 7 of 8 calls with empty args (2026-05-12),
`computer_use__write_file` 11 of 19 (2026-05-21),
`desktop_control__shell_command` 6 of 14 (2026-05-21).

**Fix.** One `parse_tool_arguments(raw, tool_name) -> (args, error)` used
by all four sites. A parse failure logs at warning with the tool name and
the raw string, and sets `args_error` on the tool call.
`ToolRunner._execute_tool_call_for_llm_inner` refuses such a call before
the safety gate and before dispatch, returning
`error_code="unparsable_arguments"` with a reason that says nothing was
executed and asks for the call to be re-issued. An absent or empty
arguments string is still a legitimate no-argument call and is not
flagged.

Measured, same input, before and after:

```
PRE-FIX   extract_response -> [{'id':'call_1','name':'web_search__web_search','args':{}}]
          dispatch reached the safety gate and went on to execute
          (no log line anywhere)

POST-FIX  WARNING feral.llm: tool-call arguments for web_search__web_search
            did not parse (Unterminated string starting at: line 1 column 11);
            the call would otherwise run with no arguments. raw='{"query": "unterminat'
          WARNING feral.orchestrator.tool_runner: Tool dispatch refused ...
          dispatch reached gate: []
          result: error_code='unparsable_arguments'
```

New tests: 10, all failing against the unfixed source.

**Files:** `agents/llm_provider.py`, `agents/tool_runner.py`,
`tests/test_tool_argument_parse_failure.py` (new).

---

## F-20 · An approval prompt was recorded, and treated, as a failed tool call

**Status:** FIXED, uncommitted, in the working tree.

The reported `workspace_scripts` 0/9 and `agentic_computer_use` 0/5 are
partly untrue. Of those 14 "failures", 9 are
`{"status": "pending_approval", ...}` envelopes. Nothing failed: FERAL
asked the operator to approve a call.

`tool_success = bool(result_data.get("success") or ...)` is False for an
envelope with no `success` key, so a pending approval was written to
`execution_log` as `failure` **and** fed to the no-progress guard as a
failing call. The store shows the consequence: three consecutive
`workspace_scripts__rerun` rows with identical args and three different
`request_id` values, and four near-identical
`agentic_computer_use__execute_task` rows. The model was told its call
had failed and asked again, so one approval prompt became several.

**Fix.** `memory.execution_audit.status_of` classifies
`pending_approval` as its own status; both orchestrator loops use it for
`result_status` and skip `budget.observe_tool` for a pending call. A
pinning test shows that three pending envelopes reported as failures do
trip `GUARD_STOP`, which is what the orchestrator used to do.

The genuine failures in that group are real and honest: two
`workspace_scripts__run` rows say "Sandbox required ... Docker sandbox
unavailable" (correct, actionable), and one `agentic_computer_use` row is
"No VLM available. Set OPENAI_API_KEY" - that one is F-16, already fixed.

New tests: 6.

**Files:** `agents/orchestrator.py`,
`tests/test_pending_approval_is_not_failure.py` (new).

---

## F-21 · A failed skill call was stamped HTTP 200

**Status:** FIXED, uncommitted, in the working tree.

Found while running down the reported `calendar_google` 0/3. The live
store holds it verbatim, twice, on 2026-05-21:

```json
{"success": false, "status_code": 200, "data": null,
 "error": "Unknown endpoint: upcoming_events"}
```

`SkillExecutor._execute_inner` normalised backing-implementation results
with `result.get("status_code", 200)`. Nine integration modules
(`calendar`, `spotify`, `notion`, `email`, `microsoft365`,
`home_assistant`, `google_drive`, `google_contacts`, `messaging`, plus
`health_platforms`) return `{"success": False, "error": "Unknown
endpoint: ..."}` with no status code, so every one of their failures
carried a 200. Anything branching on `status_code` rather than on
`success` reads that as fine.

The `upcoming_events` endpoint itself was renamed to `list_events` in
v2026.5.38 (commit `ef146fc3c`), so the underlying call was a real,
diagnosable failure wearing a success-shaped code.

**Fix.** Default to 200 only when `success` is true, otherwise 500. An
explicit status code from the implementation still wins in both
directions.

New tests: 14, one failing against the unfixed source (the other 13 pin
the behaviour that must not regress and the shape in the integrations).

**Files:** `skills/executor.py`,
`tests/test_failure_envelope_status_code.py` (new).

---

## F-22 · 26 skill implementations loaded inside `except ImportError: pass`

**Status:** FIXED, uncommitted, in the working tree.

**Re-verified.** `skills/impl/__init__.py` had 26 separate
`try: import skills.impl.X / except ImportError: pass` blocks. A missing
optional dependency removed the skill from the process with no log line,
no metric and no record. `feral doctor` reported nothing, the model was
never offered the tool, and the user was told FERAL could not do the
thing. `agentic_computer_use` carries the VLM path and `external_agent`
needs the ACP bridge, so either going missing is a shipped capability
disappearing from a running install.

**Fix.** One data-driven loop over `AUTOLOAD_MODULES`.
`ImportError` is logged at warning and recorded in
`FAILED_IMPLEMENTATIONS`; anything else is logged at error with a
traceback, because a module that raises at import used to take the rest
of the block with it. `load_report()` returns loaded / failed /
unreachable, and `api/state.py` logs the failed set at boot.

On this tree: 26 modules, 0 failures.

New tests: 11 (1 skipped without a booted brain).

**Files:** `skills/impl/__init__.py`, `api/state.py`,
`tests/test_skill_impl_loader_visibility.py` (new).

---

## F-23 · `image_gen` is a complete implementation nothing can call

**Status:** MADE VISIBLE, uncommitted. **Needs an owner decision - see below.**

**Re-verified.** `skills/impl/image_gen.py` is 197 lines of DALL-E 3 with
provider failover, and it registers itself with `@register_skill` on
import. No manifest names `image_gen`: 38 manifests in
`skills/manifests/`, none with that `skill_id`.
`SkillRegistry.get_skill("image_gen")` returns None, and
`SkillExecutor` looks the implementation up by the manifest's
`skill_id`, so it can never be dispatched. The code is loaded, the
capability is not, and nothing said so.

**What was fixed here is the silence, not the capability.**
`report_unreachable_implementations(registry.skills.keys())` runs at boot
once the registry is complete and logs each unreachable implementation by
name with the fix. It takes the live registry ids rather than reading the
manifest directory, because the directory alone over-reports:
`weather_current` comes from the hardcoded `WEATHER_SKILL` constant and
`browser` is built at boot by `api.state._register_browser_skill`. Both
look unreachable from the directory and are not. Measured on this tree
with the real registry: `image_gen` is the only one.

**Decision needed.** Writing `skills/manifests/image_gen.json` would turn
on a capability that has never been live, which is not a defect fix and
not mine to decide. The alternatives are (a) add the manifest and ship
image generation, (b) delete `skills/impl/image_gen.py`. Doing neither
leaves loaded code that nothing can reach, now at least announced at
boot.

---

## F-24 · `POST /api/channels/start` answered `{"ok": true}` for channels that do not exist

**Status:** FIXED, uncommitted, in the working tree.

**Re-verified.** `ChannelManager.CHANNEL_TYPES` holds four entries:
telegram, discord, slack, whatsapp. Five channel classes ship in
`channels/` and are in none of them: `feishu.py`, `matrix.py`,
`signal.py`, `voice_call.py`, `zalo.py`. `pyproject.toml` declares
`channel-matrix`, `channel-signal`, `channel-voice-call`,
`channel-feishu` and `channel-zalo` extras (lines 346-361), so an
operator has every reason to try one. `SignalChannel` documents itself as
a stub and its `send()` logs "dropping message".

`start_channel` returned None on every path - started, unknown type,
degraded, never connected - and the route answered
`{"ok": True, "channel": <type>}` regardless. Asking it to start
`signal` logged "Unknown channel type: signal" server-side and reported
success to the caller.

**Fix.** `start_channel` returns a status:
`{"started": True, ...}` or `{"started": False, "reason": ..., "detail": ...}`
with `unknown_channel_type` naming the four that are available,
`degraded` carrying the channel's own reason, and `did_not_start`. The
route maps those to 404 / 502 and returns `ok: False`. A manager that
predates the status return is tolerated rather than reported as a
failure.

New tests: 12, 11 failing against the unfixed source.

**Not fixed, and deliberately not deleted:** the five orphaned classes
(~437 lines) remain. They are a shipped shape for later contributors and
`signal.py` carries a written ship-ready checklist. The operator-visible
lie is closed; whether to wire them, or to drop them and their five
pyproject extras, is an owner decision.

**Files:** `channels/base.py`, `api/routes/channels.py`,
`tests/test_channel_start_reports_truth.py` (new).

---

## F-25 · A simulated vacuum answered exactly like a real one

**Status:** FIXED, uncommitted, in the working tree.

**Re-verified.** `hardware/mock_roomba.py` is enabled by default
(`FERAL_MOCK_ROOMBA` defaults to `"1"`), is registered into
`HardwareMesh` at boot by `api/state.py`, and is reachable at
`POST /api/hardware/mock_roomba/start`.

Its envelope was deliberately identical to
`HomeAssistantIntegration.vacuum_start`, "so the orchestrator's tool
dispatch path can use either backend interchangeably". That parity is the
feature and was also the defect: `{"success": true, "data": {"started":
true, "service": "vacuum.start"}}` is what a real vacuum returns, and no
field distinguished it. The actuator episode it writes into memory read
`mock_roomba started for vacuum.mock_roomba` - recall and the timeline
render `summary`, so a demo event was indistinguishable from a real one
in the one place a user reads.

**The mock is kept.** It now says what it is: `simulated: True` and a
`note` naming `FERAL_MOCK_ROOMBA=0` on `start`, `stop` and `status`, and
`simulated vacuum started for ... (mock_roomba)` in the episode summary.
The existing "wrong entity" truthfulness gate is unchanged. The mesh
entry was already honest (`name: "Mock Roomba (demo)"`,
`metadata.mock: True`).

`tests/test_mock_roomba.py::test_start_records_episode_via_memory`
asserted the old summary text and was updated to the new contract in the
same change.

New tests: 8.

**Files:** `hardware/mock_roomba.py`,
`api/routes/security_and_hardware.py`, `tests/test_mock_roomba.py`,
`tests/test_simulated_device_is_labelled.py` (new).

---

## F-26 · Three silent-at-debug swallows that lose device and voice history

**Status:** FIXED, uncommitted, in the working tree.

`episode_save` failures were caught and logged at `debug`, which is off
in every normal deployment, so the loss was unobservable:

```
hardware/adapters/cutebot.py:545     "CuteBot episode_save failed"
hardware/capability_skill.py:526     "%s episode_save failed"
voice/realtime_proxy.py:1670         "episode_save for voice tool call raised"
```

The third is the one that matters most: it is the only trace the 33 voice
tool calls in the F-18 gap left anywhere. All three now log at warning.

Also fixed in the same shape: `api/routes/apps.py` `open_app` carried the
comment "Caller still gets success:True for the render; say the push part
did not land instead of leaving it to be inferred", and then said so only
in the server log. The response now carries `pushed` and `push_error`, so
a client whose surface push failed stops waiting for a surface that is
not coming.

**Files:** `hardware/adapters/cutebot.py`, `hardware/capability_skill.py`,
`voice/realtime_proxy.py`, `api/routes/apps.py`.

---

## Leads checked and found sound - do not re-investigate

- **`perception_query` 0/6 is honest.** Five of the six failures are
  `{"status_code": 404, "error": "No camera is currently connected. Share
  your phone's camera from the Devices page or plug in a FERAL-HUP
  glasses daemon, then try again."}` and the sixth is a 504 naming the
  camera that timed out. Actionable, specific, correct. Nothing to fix.

- **`skills/impl/timeline_fusion.py` and `skills/impl/todo_store.py` are
  not dead.** Reported as never imported. They are: `timeline_fusion` by
  `skills/impl/notes_memory.py:29`, `agents/orchestrator.py:1565` and
  `skills/impl/external_agent.py:379`; `todo_store` by
  `skills/impl/feral_workflows.py:13`. They are helper modules, not
  skills, which is why they are absent from the autoload list. **NOT
  REPRODUCIBLE.**

- **`except <handler>: return <success-shaped>` is close to absent.** An
  AST sweep of every non-test `.py` outside `build/` and `dist/` for an
  exception handler returning `True` or a dict with `success`/`ok`/
  `started`/`enabled`/`valid` set True found 6 sites. All 6 are correct:
  `security/session_auth.py:109` and `bridges/sessions.py:57` are
  documented fail-safe defaults, `infra/supervisor.py:110` is
  `PermissionError` on `os.kill(pid, 0)` (the process does exist),
  `skills/impl/coding_tools.py:1023` is a working glob fallback, and the
  two in `cli/main.py` are owned by another agent.

- **No user-visible "not configured" message is produced from inside a
  broad handler.** 47 such strings exist; none sit in an `except` body.
  The one that did was F-16, already fixed.

---

## F-27 · "REMEMBERS", tier by tier, audit 2026-08-12

Method: every claim below was produced by a real write followed by a real
read against a **copy** of the live store (`~/.feral/memory.db` +
`-wal` + `-shm`, plus `sync_wal.db` and `baselines.db`), taken while the
brain was running. The live store was never written to.

### The four tiers, as measured

| Tier | Where it lives | Verdict |
|---|---|---|
| 1 Working memory | `MemoryStore._working`, in-RAM `dict[str, deque]`, `maxlen=50` per session, 500-session cap | WORKS in a turn; only the primary session survives a restart |
| 2 Episodes (12,300) | `episodes` + `episodes_fts` + `memory_chunks` | WORKS; 3,677 forgotten rows were unreachable |
| 3 Notes (400) | `notes` + `notes_fts` | WORKS; 397 of the 400 are duplicate health readings |
| 4 Knowledge graph (312 entities, 319 relations, 308 aliases) | `entities` / `relations` / `entity_aliases` | Structurally WORKS, semantically DEGRADED on the live store right now |

Tier 1 is consulted per turn: `build_context_for_llm_async` renders it as
`## Recent Context` (verified on a copy, working memory came back first
in the assembled prompt). It is not in SQLite. `MemoryStore
.snapshot_session` / `list_snapshots` / `get_snapshot` and the
`session_snapshots` table are a **second, dead implementation** with zero
production callers and 0 rows after four months of use; the live
mechanism is `memory/session_snapshot.py::SessionSnapshotStore` writing
`~/.feral/primary_session_thread.json` (50 working-memory entries
present), wired from `api/state.py:1516` for `primary_session_id` only.
Every non-primary session's working memory is lost on restart. Left as
found; both are outside this lane's fixes.

### `knowledge` has 0 rows and `knowledge__deprecated` has 29, NOT a silently empty tier

`memory.kg.unified` defaults to true, so `knowledge_store/query/search/
about` route to `entities` × `relations`. All 29 deprecated triples were
verified present as relations (`Alex works_on Feral`, `Alex prefers oat
milk latte`, …): 29 of 29 matched, 0 missing. `knowledge_fts` at 0 rows
is consistent, the unified search path reads `entities_fts`.

Latent trap, recorded not fixed: setting `memory.kg.unified=false`
(documented as kept for chaos/recovery) routes reads back to the empty
flat table, and all 29 facts vanish.

### The knowledge graph is degraded on the live store *today*

`entities.embedding`: 312 vectors at 1536 dims against a 384-dim
fastembed provider, plus 4 at 384. `memory_chunks` is fully migrated
(12,001 at 384, 41 NULL), so the earlier re-embed reached chunks and the
`entities` table was never migrated on this machine.

Proven on the copy, before and after `reembed_store`:

```
entity vector widths BEFORE: [(384, 4), (1536, 312)]
  search_entities('CuteBot') -> ['The CuteBot should follow the line every night at 9 p.m.',
                                 'Start CuteBot line-following routine', 'CuteBot left line sensor']
entity vector widths AFTER:  [(384, 316)]
  search_entities('CuteBot') -> ['CuteBot', 'cutebot.follow_line', 'Start CuteBot line-following routine']
```

Before the migration the CuteBot entity itself is not in its own result
set. This is an operational gap, not a code defect: `memory/reembed.py`
covers `entities` correctly and the runtime now logs the mismatch at
warning. The remedy is `feral memory reembed` against the live store,
which this lane did not run because it does not write to `~/.feral`.

**Fixed here:** the operator could not see it. `feral memory status`
reported the backend module and the encryption flag and nothing else.
It now runs the same discovery scan `reembed check` uses. Against a copy
of the real store:

```
  Embedding provider:    fastembed (384d)
  Stored vectors:        STALE, 312 at the wrong width
    entities.embedding: 1536d x312
  Semantic recall is degraded for the tables above. Fix with:  feral memory reembed
```

### Decay is not deleting anything a user would expect to keep, but "forgotten" meant "lost"

12,300 episodes, 3,677 forgotten, min `decay_factor` 0.0402 against a
0.05 threshold. Everything forgotten is 92.9-118.6 days old; the oldest
still-active row is 92.98 days. The math is exactly the documented curve
and nothing has been hard-deleted, nor can be: hard delete needs
`forgotten_at` older than `retention_days` (365) and the oldest episode
in the store is 118 days old.

The defect was recovery. Every read path filters `forgotten_at IS NULL`,
`feral memory recall` takes an episode id, and **nothing could produce
one**. 3,677 episodes (30% of the store, including 133 `user_command`
rows the user typed by hand) could be neither searched nor recalled.

Fixed: `memory/decay.py::forgotten_query` (one definition),
`MemoryDecayService.list_forgotten`, and `feral memory forgotten [text]`,
which reads the DB read-only so recovery does not require a running
brain. Against a copy of the real store, `feral memory forgotten flight`
returns 40 of 3,677 with ids, and `feral memory recall <id>` accepts
them.

### CRDT: update and delete propagation has never worked, and could not have

`sync_wal.db`: 16,184 operations, `op_type` distribution `[('insert',
16184)]`, `synced_to` distribution `[('[]', 16184)]`. Two independent
causes, both confirmed by reading every call site:

1. **No writer has ever logged a non-insert.** Every `_log_sync` /
   `_log_sync_async` call in the tree passes `"insert"`
   (`store.py:1550,2588,2637,2648`, `notes_legacy.py:51`,
   `knowledge_graph.py:402,461,477`). The three deleters on synced
   tables, `notes_legacy.delete_note`, `store.conversation_delete`,
   and the decay sweep's hard delete, logged nothing at all. The
   receiving side's `op_type == "delete"` branch in
   `SyncEngine._apply_to_memory` has been correct and unreachable the
   whole time. Consequence: a note the user deletes on one brain stays
   readable on every peer, and a hard-deleted episode is resurrected by
   any peer still holding its `insert`, because `get_changes_since`
   selects purely on HLC and there are 14,807 episode inserts with
   nothing to counter them.

2. **`SyncWAL.mark_synced` had zero callers** anywhere in the tree,
   production or test. `synced_to` was unwritable by construction, so
   "which peers have this operation?" was unanswerable and the WAL had
   no basis on which it could ever be pruned. It grows unbounded; there
   is no prune path (recorded, not fixed, 12MB today).

Fixed: the three deleters log a `delete` operation after their local
commit (never before, so a delete that failed locally is not announced);
`_handshake_and_exchange` marks the operations it shipped once the peer's
own change set comes back. `mark_synced` opened a connection per call,
which a first sync of 16,184 operations cannot afford, so
`mark_synced_many` does it in one commit, chunked at 500 to stay under
`SQLITE_MAX_VARIABLE_NUMBER`, and tolerates a corrupt `synced_to` cell
rather than losing the batch.

### Do the senses reach memory? Two of three.

* **Screen, YES.** `perception/screen_loop.py:462` calls `episode_save`.
  9,509 `screen_*` episodes in the live store; `episode_search
  ('FlightRadar24')` returns real rows.
* **Device events, YES.** `hardware/capability_skill.py:517`,
  `hardware/adapters/cutebot.py:536`, `hardware/mock_roomba.py:203`.
  280 `device_action` + 267 `robot_event` episodes, retrievable.
* **Biometrics, ONLY ON THE HTTP BATCH PATH.**
  `/api/health/ingest` (`api/routes/dashboard.py:586`) writes notes: 397
  of the 400 notes in the store are HealthKit readings, and they read
  back. The **live wearable stream does not reach memory at all**.
  `api/server.py::_handle_biometric_device_event` fans a `device_event`
  to `state.perception.update_sensors` (volatile),
  `_record_biometrics_to_baseline`, and `_record_biometrics_to_history`
 , the last two write `~/.feral/baselines.db`, a different database.
  That table holds **1,554 real samples** (1,286 heart rate, 149 SpO2,
  117 steps, sources `jw_health_glasses` / `veepoo_wristband`, spanning
  2026-06-21 to 2026-08-05) and **not one of them is in memory.db**.
  `search_all`, `feral memory query` and `build_context_for_llm` cannot
  reach a single one.

  **Not fixed here**, `_handle_biometric_device_event` is a frame
  handler owned by another lane. It needs an owner.

Two further observations, recorded not fixed, both outside this lane's
remit but inside the promise:

* The health-ingest route's comment claims it appends "the raw dict as a
  JSON tail so nothing is lost". It does not; only `content_line` is
  written, so `sampled_at_ms` and every unmapped field are dropped
  (`api/routes/dashboard.py:552-585`). A note's `created_at` is ingest
  time, not sample time.
* Health notes are not de-duplicated: 209 notes reading
  `heart_rate 115bpm (Bluetooth Device)` and 188 reading
  `spo2 93% (WHOOP)`, identical content, one per poll. Each one also
  creates two KG entities and a `says` relation via
  `notes_legacy.py:85`, which is where whole sentences enter `entities`
  as entity names.

### Tests

`tests/test_memory_remembers_audit.py`, 12 tests. Against unfixed HEAD
(`b5934eb25`, run in a clean worktree): **10 failed, 2 passed**. The two
that pass are the negative controls, a delete that changed no local row
must not be announced, and a sweep with no hard deletes must log no
delete, and they must hold both before and after. After the fixes:
**12 passed**.

Whole suite after the fixes: `python3 -m pytest tests/ -q -p
no:cacheprovider -p no:randomly --no-cov` gives **7,697 passed, 38
skipped, 0 failed** in 451s. `ruff check --select=E,F,W
--ignore=E501,E402,F401,W291,W293 .` is clean.

**Files:** `memory/notes_legacy.py`, `memory/store.py`, `memory/decay.py`,
`memory/sync.py`, `cli/memory_cmd.py`, `cli/main.py` (one entry added to
the `memory` action list so `forgotten` is reachable).

### Still open, needs an owner

1. `_handle_biometric_device_event` (`api/server.py:4114`) does not write
   to memory. 1,554 wearable samples are stranded in `baselines.db`.
2. `api/routes/dashboard.py:552-585` drops `sampled_at_ms` and every
   unmapped field despite a comment claiming it keeps them, and does not
   de-duplicate identical readings.
3. `MemoryStore.snapshot_session` / `list_snapshots` / `get_snapshot` and
   the `session_snapshots` table: zero production callers, 0 rows. Either
   wire them or delete them.
4. Working memory persists for `primary_session_id` only. Every other
   session loses it on restart.
5. `sync_wal` has no prune path and grows without bound (16,184 ops,
   12MB). `synced_to` is now written, so a prune finally has a basis.
6. `memory.kg.unified=false` routes reads to the empty flat `knowledge`
   table and hides all 29 migrated facts.

---

## F-27 · HEARS: every `audio_frame` from every device was dropped, and the rest of the audio stack reported readiness it did not have

**Status:** FIXED, uncommitted, in the working tree.

Audit of the product promise "FERAL sees, hears and remembers
everything connected to it". This is the HEARS half. Everything below
was established by running it.

### The map: every route audio can take into the brain

| # | Entry point | File:line | Verdict (before) |
|---|---|---|---|
| 1 | HUP `audio_frame` on `/v1/node` | `api/server.py:3371` -> `_handle_audio_frame:3730` | **DROPPED** |
| 2 | HUP `device_event(event_type=audio_frame)` | `api/server.py:3403` -> same handler | **DROPPED** |
| 3 | HUP `audio_chunk` on `/v1/node` | `api/server.py:3234` -> `voice/router.py:635` | REACHES, if a session is bound |
| 4 | `audio_chunk` on `/v1/session` | `api/server.py:1785` -> `voice/router.py:745` | REACHES |
| 5 | Gateway JSON-RPC `voice.audio` | `gateway/protocol.py:357` -> `voice/router.py:745` | REACHES |
| 6 | REST upload | none exists | N/A |
| 7 | Local microphone | none exists | N/A |

`api/routes/audio.py` is discovery and config only; it accepts no
audio. No `sounddevice` / `pyaudio` / capture code exists anywhere in
`feral-core`, and no ambient path exists: audio is only routable once
`bind_session_to_daemon` has run (`api/state.py:3164`), which only
`voice_session_start` and session attachment do. **FERAL never listens
without a session being started.** That is a design fact, not a defect,
and it is now the documented answer.

### 1. The `audio_frame` path was dead

`_handle_audio_frame` ended in a `getattr(audio, "ingest_frame", None)`
probe. `perception/audio_pipeline.AudioPipeline` has never defined
`ingest_frame`, so the probe was never true: every frame was
size-checked, counted, and discarded at `debug`, while the daemon's
send reported success. HUP_SPEC.md §5.4.1 said "Route to
`state.audio.ingest_frame(node_id, payload)`" - the spec named a method
nobody wrote, and the spec is prose, so nothing caught it.

It survived because of trap 3. `tests/test_hup_v1_1_brain.py`,
`tests/test_hup_v1_1_e2e.py` and
`tests/test_frame_size_cap_decoded_bytes.py` all installed a double
that *did* define `ingest_frame` (or a MagicMock, which defines
everything). Three test files proved a call production could not make.

**Fixed.** `audio_frame` now converges on the one consumer whose
transcript has somewhere to go: `VoiceRouter.handle_audio_from_node`,
the same sink `audio_chunk` uses, which owns mute, wake-word gating,
provider selection, the `transcript` frame, working memory, the
orchestrator turn and the spoken reply. The handler is `async` and
awaited (fire-and-forget would let two 20ms frames of one utterance
reach a provider out of order). `codec` maps to `encoding`. An
unroutable frame is a rate-limited `warning` naming `voice_session_start`,
not a `debug`. The three test files were corrected to the real contract.

### 2. Wiring it was not enough: the silence-gap VAD could never fire

`AudioPipeline.process_audio_chunk` appended the chunk and *then* asked
`buf.vad_triggered()`. `append` stamps `_last_chunk_time = time.time()`,
and `vad_triggered` returns `time.time() - _last_chunk_time > 1.5`, so
the check always measured a gap of zero. Measured before the fix: 10
chunks x 3000 bytes in, 30,100 bytes still resident, zero
transcriptions. Only `is_final=True` ever flushed. A browser sends
`is_final`; `audio_frame` has no such field, so device audio would have
accumulated forever even after being wired.

**Fixed.** The boundary is evaluated before the append, which is what a
silence gap actually means. Teardown of a non-empty buffer now logs at
`warning` - a stream that ends mid-utterance is still never
transcribed, and that hole is real, but it is no longer invisible.

### 3. Wake word: works, listens for the wrong phrase, cannot be turned on

openwakeword 0.6.0 and its `hey_jarvis_v0.1.onnx` are both present
(bundled in the wheel, 1.27MB) and the detector loads and runs. Three
defects around it:

- `POST /api/ambient/wake_word/toggle` does `state.wake_word.enabled =
  not ...`. `enabled` was a read-only `@property`: `AttributeError:
  property 'enabled' of 'WakeWordDetector' object has no setter`. The
  wake word could not be enabled from the API at all. Its test used a
  MagicMock, which accepts any assignment.
- `GET /api/ambient/wake_word/status` read `getattr(ww, "phrase", "hey
  feral")` against an object with no `phrase` attribute, so the default
  won unconditionally and `FERAL_WAKE_PHRASE` was never reflected.
- **The phrase FERAL reports is not the phrase it detects.**
  `FERAL_WAKE_MODEL` defaults to `hey_jarvis_v0.1`, which fires on "hey
  jarvis", while the configured phrase defaults to "hey feral". No
  FERAL-branded wake model is shipped or referenced anywhere in this
  repo. Out of the box the product told the user to say a phrase that
  could not work.

Also: with openwakeword absent (it is not in `[all]`, per F-12) the
detector drops to `_detect_energy`, whose own docstring says "not a
true wake word detector". Measured: 3200 bytes of uniform random noise,
no speech, activates it with confidence 1.00. A loudness gate opening a
microphone to STT.

**Fixed.** `enabled` is settable and loading the model on enable (the
detector boots disabled for privacy, so without that it would switch on
into the fallback regardless of what is installed). `phrase`,
`detector`, `model_phrase` and `effective_phrase` are real properties;
`effective_phrase` is `""` for the energy fallback because it matches no
phrase. A phrase/model mismatch and the energy fallback each log a
`warning` naming the remedy. `stats` and both routes carry
`detector` / `effective_phrase`. The fallback is kept, labelled, not
removed. The `except Exception: pass` around the model fetch now logs.

### 4. VAD with missing weights: honest, checked, one gap closed

`~/.feral/models` does not exist on the audit machine, so this is the
branch every install without `feral setup` takes. `load_endpointer`
returns `None`, logs at INFO, and the chained pipeline falls back to its
packet-absence timer. No crash, no pretence. **NOT A DEFECT.** The one
gap was actionability: the message named the missing file but not the
command that produces it, and the only symptom is ~2.3s of extra
latency per turn. `vad_available()` now names
`python -m voice.local_models fetch-vad`.

### 5. Local STT and TTS: "ready" for engines that could not run

`feral voice providers` reports **7/13 green**. The six that are not:
Deepgram and Groq (no credential), and whisper.cpp, faster-whisper,
Piper and Silero VAD (model never fetched). That surface is honest and
names each missing artefact. `AudioPipeline` was not, and the two
disagreed on the same machine at the same time:

- `_LocalTTS._ensure_voice` called `PiperVoice.load("en_US-lessac-medium")`.
  `PiperVoice.load` takes a *path*. It raises `FileNotFoundError: [Errno
  2] No such file or directory: 'en_US-lessac-medium.json'` on every
  machine, voice installed or not. **Local TTS through this pipeline has
  never produced one byte of audio.**
- `_LocalSTT._ensure_model` called `WhisperModel("base",
  compute_type="int8")`, which downloads mid-turn. Demonstrated
  accidentally and conclusively: running the new tests against the
  unfixed source pulled 141MB into `~/.cache/huggingface` at 22:08.
  `voice/local_models.py` exists to forbid exactly this.
- Both failures were caught and rerouted to OpenAI, breaking the rule
  `voice/local_models.py` states in its own docstring: "an operator who
  chose local engines for privacy must not be silently rerouted to a
  cloud provider." Selecting "local, for privacy" silently meant
  "OpenAI", with an `error` log as the only trace.
- Boot logged "Audio pipeline ready - STT: local/faster-whisper (base),
  TTS: local/piper (...)" for both.

**Fixed.** Both backends resolve through `voice/local_models.py` (no
mid-session download; `ModelUnavailable` carries the fetch command),
using the same load pattern as `voice/tts_providers/piper.py:210` and
`voice/stt_providers/faster_whisper_local.py:153` so the call sites
cannot drift again. Boot probes the same store the CLI reads and says
`NOT READY: <reason>` with a `warning`. The cloud reroute is preserved
but no longer silent or default: `FERAL_LOCAL_AUDIO_CLOUD_FALLBACK=1`.

### Tests

New: `tests/test_audio_frame_reaches_transcription.py` (11),
`tests/test_wake_word_honesty.py` (12),
`tests/test_local_audio_engines_are_honest.py` (6),
`tests/test_vad_missing_weights_degrades_loudly.py` (3). 32 total.
Verified failing against unfixed source: 8/11, 11/12, and 7/9 across
the last two files. The five that pass either way are deliberate
non-regression guards (`is_final` still flushes, the cloud fallback
still works when asked for, no download at VAD load, the energy
fallback still opens on loud audio).

Updated to the real contract: `tests/test_hup_v1_1_brain.py`,
`tests/test_hup_v1_1_e2e.py`, `tests/test_frame_size_cap_decoded_bytes.py`.

### Left for the owner to decide

- `AudioPipeline.process_audio_with_wake_word` (`perception/audio_pipeline.py:457`)
  is called from nowhere. It is also the only wake-word gate on that
  class, and `api/state.py:367` constructs `AudioPipeline()` with no
  detector while `state.py:1613` gives the same detector to
  `VoiceRouter`, which does the gating. Dead, but deleting it removes a
  documented capability. **Not removed.**
- A stream that stops mid-utterance and never sends another frame is
  still never transcribed. It now warns at teardown. A proper fix needs
  an idle-flush timer, which is a design change, not a defect fix.
- HUP_SPEC.md §5.4.1 still says "Route to `state.audio.ingest_frame`".
  The spec is in `feral-nodes/`, owned by another lane. It is now wrong
  in the opposite direction and should be updated to name
  `VoiceRouter.handle_audio_from_node`.

**Files:** `api/server.py`, `perception/audio_pipeline.py`,
`perception/wake_word.py`, `api/routes/ambient.py`, `voice/vad.py`,
plus the four new and three updated test files.

---

## V-01 · FERAL could not see anything a glasses device sent: the buffer had one writer and no reader

**Status:** FIXED, uncommitted, in the working tree.

`perception/context_attach.py:162` resolved the glasses buffer with

```python
return getattr(glasses_buffer, "get_glasses_buffer", lambda: None)()
```

`perception/glasses_buffer.py` never defined `get_glasses_buffer`
(`__all__` was `GlassesBuffer, GlassesFrame, KNOWN_SOURCES`). The probe
therefore fell through to `lambda: None` on every turn, and the reader
concluded "Lane 11's buffer has not merged yet". The orchestrator
(`agents/orchestrator.py:2232`) never passes `glasses_buffer=` either,
so nothing else could supply it. `state.glasses_buffer` had exactly one
writer (`api/server.py _handle_glasses_frame`) and zero readers.

This is the same shape as the audio_frame defect: a getattr probe for a
method that does not exist, whose absence is indistinguishable from
"the feature is not installed".

**Reproduced on a running brain**, not by reading. TestClient over the
real `/v1/node` socket, real `BrainState`, real `GlassesBuffer`: a
`glasses_frame` landed (`device_ids_with_frames() == ['w610-PROBE']`)
and the next `orchestrator._attach_vision_context` on a voice turn
attached no image. After the fix the same script attaches the image.

**Why the test suite did not catch it.** Every one of the 12 tests in
`tests/test_vision_context_attach.py` injects a fake buffer through the
`glasses_buffer=` keyword, and `tests/perf/test_lane08_live_traces.py`
patches `_get_glasses_buffer` itself. The one code path that runs in
production was the one path no test exercised. CLAUDE.md trap 3.

**Fix.** `perception/glasses_buffer.py` exports `get_glasses_buffer()` /
`set_glasses_buffer()`; `BrainState.__init__` registers its own
instance, so the reader resolves the same object the writer writes to,
never a second empty one. `_get_glasses_buffer` now logs at WARNING,
once, when the accessor is missing, and says what to do about it. The
module docstring in `context_attach.py` documented `push()` and
`device_ids()`, neither of which ever existed, which is how the missing
symbol stayed unnoticed; it now matches the real API.

## V-02 · Six `device_event` types passed the filter or the dispatcher and were discarded at debug

**Status:** FIXED, uncommitted, in the working tree.

`device_event` with `event_type=uv` was confirmed dead exactly as
reported: the dispatcher filter admits it (`api/server.py:3420`),
`_handle_biometric_device_event` has no `uv` branch, `sensors` stays
empty, and the reading is dropped by the `if not sensors` guard at
`logger.debug`. Every other type the device side can emit was then
checked against the extractor by driving the real `/v1/node` socket,
one `device_event` per type. Result before the fix:

| event_type | before | after |
|---|---|---|
| heart_rate, spo2 | survives | survives |
| skin_temperature | baseline only, never reached the frame | survives |
| temperature, steps, accelerometer | reached `update_sensors`, no field read it | survives |
| uv | DROPPED (filter passes, no branch) | survives |
| gyroscope, ambient_light, battery, gps, button_press | DROPPED (unknown-event branch) | survives |
| camera_frame | DROPPED (unknown-event branch) | routed to the vision buffer |
| microphone_chunk | DROPPED | still dropped, audio lane owns it |
| gesture, glasses_status, robot_telemetry, audio_frame, video_frame | survives | survives |

`camera_frame` is the HUP v1.0 image type, still valid per
HUP_SPEC.md §5.4 ("camera_frame and microphone_chunk remain valid for
v1.0.0 daemons"). It had no branch at all, so a v1.0 daemon's every
frame was discarded. It now shares `_handle_video_frame` with
`video_frame`.

`ambient_light`, `battery` and `gps` each already had a sink on the
brain side (`PerceptionFrame.ambient_light_lux`, `.battery_pct`,
`.location`); only the dispatch was missing. `button_press` is named in
HUP_SPEC.md §5.4 and was named in this function's own docstring, and
had neither a filter entry nor a branch.

`perception/fusion.py update_sensors` read only the nested
`vitals.*` / `environment.*` shapes for skin temperature and ambient
light, while the extractor emits them flat, so those readings trained
`baselines.db` and never reached the frame the LLM is shown. Both forms
are read now. `PerceptionFrame` gained `uv_index`, `steps`,
`ambient_temperature_c`, `accel_xyz`, `gyro_xyz`, and UV / steps /
ambient temperature appear in `to_system_context()`, because a value on
the frame that the context block omits is still invisible to the model.

**Both drop sites are now visible.** The "could not extract a value"
log is WARNING and names the extractable types; the unknown-event log
is WARNING, once per (node, event_type) so glasses telemetry rates
cannot flood it. Forward-compat is unchanged: unknown types are still
ignored, they are just no longer ignored silently.

## V-03 · The `frame` envelope could not answer "what do you see"

**Status:** FIXED, uncommitted, in the working tree.

`type: "frame"` is what the shipped iOS bridge sends
(`feral-nodes/ios-app/.../FeralBrainClient.swift:435`,
`sendCameraFrame`). Its brain branch (`api/server.py:3342`) pushed to
the vision buffer and updated perception, but was the only image branch
that never called `orchestrator.resolve_pending_frame` and never ran
scene analysis. `request_frame` waits on a future that only
`resolve_pending_frame` completes, so `perception_query` /
`what_do_i_see` against an iPhone could only ever run its 10 s timeout
and answer 504, which is the "honest 504" recorded earlier in this
file. The message was honest; the cause was this. Both calls added, so
the branch now matches `vision_frame` and `_handle_video_frame`.

## V-04 · Sensing and remembering are not joined (REPORTED, not fixed)

`_handle_biometric_device_event` writes three sinks: the in-RAM
perception frame, `somatic_engine`, and `baseline_engine`
(`~/.feral/baselines.db`). It contains no memory write of any kind.
The memory lane's finding is the same seam one layer up: `baselines.db`
holds 1,286 real heart-rate samples spanning 2026-06-20 to 2026-08-07
(verified on a copy: `hr` 1286, `spo2` 149, `steps` 119) and none of
them are episodes in `memory.db`. `uv` was the same defect one layer
lower: it never even reached the RAM sink. Joining the two is a
memory-lane call, not a frame-handler call, so it is recorded here and
not fixed.

## V-05 · The screen loop is not writing episodes today (MEASURED)

On a copy of the live store: 12,299 episodes total, 9,513 with a
summary starting `Screen:`. Oldest 2026-04-16 16:18, **most recent
2026-07-30 17:26**, i.e. 13 days before this audit, and none since.
The newest episode of any kind is 2026-08-07 15:19. So the prose-salvage
fix in `perception/scene.py` works when the loop runs (verified: a
probe boot with `FERAL_VISION_ENABLED=true` produced
`Scene [general] [screen_loop]: A computer screen displays the Google
homepage...` and the loop is no longer blind), but no brain with the
loop enabled has been running since 07-30. Scene provider config is
sound: `~/.feral/settings.json` has `vision.enabled=true`,
`provider=ollama`, `model=moondream`; `config/loader.py:1410` exports it
as `FERAL_VLM_PROVIDER` / `FERAL_VLM_MODEL`; `ollama` is up and
`moondream:latest` and `llava:latest` are both present locally. With no
`FERAL_VLM_PROVIDER`, `SceneAnalyzer.available` falls back to the shared
LLM, which is why the probe's frames went to OpenAI.

**Tests:** `tests/test_vision_entry_points.py`, 25 tests. Against
unfixed `HEAD` (`b5934eb25`, run in a detached worktree so no other
lane's uncommitted work was touched): **21 failed, 4 passed**. After:
**25 passed**. The 4 that passed before are the two paths that already
worked (`heart_rate`, `spo2`, `gesture`) plus
`test_glasses_frame_lands_in_the_glasses_buffer`, which is the point of
V-01: the write always worked, the read never did.

**Files:** `perception/glasses_buffer.py`, `perception/context_attach.py`,
`perception/fusion.py`, `api/server.py`, `api/state.py`,
`tests/test_vision_entry_points.py`.

**Leads checked, sound, do not re-investigate.**

- `vision_frame` on both sockets, `video_frame`, and the web client's
  `vision_frame` all reach `PerceptionFrame.vision_data_url` and the
  LLM via `to_llm_user_content`. Verified by running.
- `POST /api/uploads` stores bytes and runs no vision analysis. That is
  what it is for; it is not a perception entry point.
- `skills/impl/perception_query.py` `pick_best_camera` consults
  `state.vision_buffer` only, never `state.glasses_buffer`, so a device
  that streams `glasses_frame` alone is invisible to its fallback. Real,
  but `skills/` is owned by another lane. Not fixed here.
- `feral-nodes/ios-app` `FeralBrainClient` has no `vision_request`
  handler (`handleMessage` falls through to `default:`), so even with
  V-03 fixed a brain-initiated capture depends on the host app
  implementing it. Device-side, out of this lane.

---

# Lane: CONNECTED + ORCHESTRATION

Audited the two halves of "FERAL sees, hears and remembers everything
connected to it, whether software or hardware": does every connected
thing get in, and does the brain use it. Every verdict below was
produced by running the real object against the real data, not by
reading code. Copies of `~/.feral/memory.db`, `paired_devices.db` and
`baselines.db` were taken with `sqlite3 .backup` and driven from a
scratch `FERAL_HOME`; the live directory was not written to.

Suite before this lane: 7674 passed, 38 skipped.
Suite after: **7774 passed, 2 failed, 44 skipped**. Both failures are
untracked in-flight test files owned by the perception lanes
(`tests/test_local_audio_engines_are_honest.py` asserting on
`perception/audio_pipeline.py` wording, and
`tests/test_vision_entry_points.py` on the `perception/glasses_buffer.py`
singleton). Neither touches a file this lane changed; both fail
identically with this lane's changes reverted.
`ruff check --select=E,F,W --ignore=E501,E402,F401,W291,W293 .` -> All checks passed.

New tests: 39 across 5 files. 30 of the 32 written against a defect fail
on unmodified `b5934eb25` (verified in a detached worktree, not a
stash); the 2 that pass there are deliberate negative controls.

---

## C-01 - The brain was never told what hardware is connected

**Status:** FIXED, uncommitted, in the working tree.

`memory/node_subdevices.py` opens by naming its consumers: "the web
dashboard, native iOS UI, future MCP clients, **the orchestrator's
prompt context**". The first three read it. The fourth never did.

Live `memory.db`, `node_subdevices`: 7 rows, all `provenance=ble`,
across 6 iPhone nodes - `jw_health_glasses` (device_name `W300`,
battery 69% / 86%) on five nodes and `veepoo_wristband` (`VITRO`) on
one. `/api/devices/connected` merges them into every node row via
`_subdevices_for()`, and `GET /api/dashboard` counts them.

Driving the real `IdentityLoader` against a copy of the live database,
the entire hardware content of the assembled system prompt was:

```
## Live Perception
Connected nodes: feral-iphone-6053b3cdc4ed

Connected devices: ['feral-iphone-6053b3cdc4ed']
```

`W300`, `VITRO`, `glasses`, `wristband` and `subdevice` were all absent
from the 11,556-character prompt. A bare HUP node id is not an answer to
"are my glasses connected", and the model had no reason to believe a
peripheral existed at all, so it would not call a tool to find out.

**Fix.** `IdentityLoader.subdevice_store`, wired by the new
`Orchestrator.set_subdevice_store` from `BrainState` next to the
existing `set_calendar` / `set_mcp_client` wiring, renders a
`## Connected Hardware` block. Same shape as `set_calendar` and for the
same reason recorded there: a capability the brain owns is useless until
the prompt carries it.

Three states are stated explicitly because collapsing any two of them
lies in a different direction each time: live rows say "connected,
reporting now"; rows past their provenance heartbeat window say "not
reporting" and are **kept**, since hiding one makes "my glasses just
dropped" read identically to "you have never owned glasses"; a store
with zero rows says "No peripherals have reported", because silence
reads to a model as absence of information rather than information
about absence. A store that raises logs at warning and the prompt says
hardware status is unavailable - a silent `except` here would rebuild
the exact defect, a prompt that looks complete while carrying no
hardware truth.

Against the real database the block now renders all 7 rows with their
names, batteries, statuses and owning node, each correctly marked "not
reporting" (the brain is not running, so every `last_seen` is months
outside its 30s BLE window).

**Files:** `agents/identity_loader.py`, `agents/orchestrator.py`,
`api/state.py`, `tests/test_connected_hardware_reaches_prompt.py` (new,
9 tests, 8 failing before).

---

## C-02 - "What is connected" answered without the peripherals

**Status:** FIXED, uncommitted, in the working tree.

`self_introspection.connected_devices` is the tool an LLM calls for that
question. It read `state.device_registry` only, which holds HUP nodes -
the iPhone - and has never held what is paired behind the iPhone. So the
dashboard and the tool answered the same question with different facts,
and the tool, the one the model reads, was the one missing the hardware.

Its body also ended in `except Exception: return []`. `[]` is
simultaneously the honest answer for a machine with nothing attached, so
a registry that raised produced a confident "no devices are connected"
with no trace anywhere.

**Fix.** Each device row now carries `subdevices: [...]` from
`state.node_subdevices`, with `capability`, `name` (from
`attrs.device_name`, the only human-readable label the device has),
`status`, `live` and `provenance`. `live` is carried through
deliberately: "paired" and "currently reporting" are different claims.
The broad handler logs with a traceback and returns the reason, which
the endpoint surfaces as `data.error` so a caller can tell empty from
broken. `_connected_devices_payload` is kept as a wrapper so existing
callers are unchanged.

**Files:** `skills/impl/self_introspection.py`,
`tests/test_connected_devices_tool_reports_peripherals.py` (new,
5 tests, all 5 failing before).

---

## C-03 - Every npx MCP server reported itself installed

**Status:** FIXED, uncommitted, in the working tree.

`_check_installed` was `if cmd == "npx": return shutil.which("npx") is
not None`. `npx` is one binary that can launch any package on npm, so
this asks "is npm's runner present" and answers "the GitHub MCP server
is installed". Measured live on this machine, where exactly two MCP
packages are cached (`@modelcontextprotocol/server-filesystem` and
`server-memory`, found under `~/.npm/_npx/*/node_modules`):

```
before:  stats() -> {'known_servers': 9, 'installed': 9}
         auto_discover() -> all 9 ids, each {'installed': True}
after:   stats() -> {'known_servers': 9, 'installed': 2,
                     'launchable': 9, 'fetch_on_launch': 7, 'unavailable': 0}
         auto_discover() -> ['filesystem', 'memory']
```

`ready` was `installed and has_required_env`, so it inherited the lie.
`auto_discover`'s docstring promised it "checks npx availability and
node_modules" and only ever checked the former.

**Fix.** `_resolve_install_state` returns one of three states, because
the remedies differ: `installed` (on disk, starts with no network),
`fetch_on_launch` (npx present, package not cached - it will start,
after a download, and only with a network), `unavailable` (the launcher
itself is missing, so the fix is to install Node, not the package).
Resolution is filesystem-only, no subprocess, because this runs on every
render of the Settings page and `npm ls -g` costs hundreds of
milliseconds; it checks npx's own cache, the global prefix derived from
the `npm` binary's location, `NODE_PATH`, and the cwd.

`ready` is now `install_state != "unavailable" and has_required_env`.
Deriving it from the newly honest `installed` would grey out every
working npx server, which is a regression dressed as a fix.
`install_detail` carries the actionable sentence, e.g. "npx is available
but @modelcontextprotocol/server-github is not cached locally. The first
connect will download it ... Pre-install with: npm install -g ...".

**Note for the owner:** the brief said 12 default MCP servers.
`KNOWN_SERVERS` holds **9**.

**Files:** `mcp/registry.py`,
`tests/test_mcp_registry_reports_real_install_state.py` (new, 11 tests,
10 failing before).

---

## C-04 - A failed MCP connect threw away the only thing that explained it

**Status:** FIXED, uncommitted, in the working tree.

Driven live against a server whose npm package does not exist:

```
>>> await registry.connect_server("bogus")
{'error': "Failed to connect to MCP server 'bogus'"}
stats -> {'bogus': {'reason': 'connection failed after 4 attempts'}}
```

Thirty seconds of wall clock, four retries, and neither the return value
nor the degraded record names a cause. Meanwhile `_connect_stdio` opened
the child with `stderr=asyncio.subprocess.PIPE` and **never read it**,
so npm's own `404 Not Found` was written into a pipe that was closed and
discarded. The one artifact that explains the failure was produced and
thrown away. `reason` describes the retry loop, not the problem.

An unread PIPE is also a hazard on its own: a child that writes past the
buffer (16KB on macOS, 64KB on Linux) blocks forever on its own stderr,
so "never read it" is not a safe default even when nobody wants the text.

**Fix.** `MCPServerConnection.last_error`, always present so readers need
no `hasattr` guard, populated on every failure path - missing command
(named, with the remedy), rejected `initialize`, and no response to the
handshake - each with `_drain_stderr()` appended. The drain is bounded by
both a 4000-byte cap and a 2s timeout, and keeps the last 12 non-empty
lines because npm prefixes a dozen lines of its own noise before the one
that matters. `_mark_degraded` gained a `detail` field carrying it, and
`MCPServerRegistry.connect_server` returns `detail`, `install_state` and
`attempts`. Both lookups there are best-effort: failing to *read* a
diagnostic must never replace the diagnostic with an `AttributeError`
(it did, against the minimal stub in
`tests/test_mcp_canonical_config_and_connect.py`, which is how that
regression was caught).

**Files:** `mcp/client.py`, `mcp/registry.py`,
`tests/test_mcp_connect_failure_is_actionable.py` (new, 7 tests, all 7
failing before).

---

## C-05 - Biometrics ARE reachable from a turn. NOT REPRODUCIBLE as a defect.

**Status:** NEGATIVE RESULT. Characterization test added, no fix needed.

The memory lane established, correctly, that
`_handle_biometric_device_event` writes `~/.feral/baselines.db` (1,286
real `hr` samples spanning 2026-06-21..2026-08-07, plus 149 `spo2` and
119 `steps`) and that **none** of them are in `memory.db`, which instead
holds 209 heart-rate notes, 199 of them the same 115bpm value. The
natural conclusion is that "what was my heart rate on Tuesday" is
unanswerable. **That conclusion is wrong, and it matters.**

Verified live, end to end, against the real `baselines.db`:

```
health_data__health_history
  -> HealthAggregator.get_health_history   (endpoint resolves to get_<id>)
  -> BaselineEngine.get_samples
  -> baselines.db
=> 1286 hr entries, sources ['jw_health_glasses', 'veepoo_wristband'],
   most recent 2026-08-07 15:20:52 = 82.0 bpm
```

with real per-day detail available (2026-08-03: 217 samples, 93-119bpm;
2026-07-24: 293 samples, 75-98bpm). The provider is wired at boot,
`api/state.py:1252`, `biometric_history_provider=lambda:
self.baseline_engine`.

Routing reaches it too. `_R_HEALTH` only matches present tense - it
misses "what was my heart rate on Tuesday", "... yesterday", "how has my
heart rate been this week" - but that is harmless, because those fall
through to keyword routing which returns `health_data` as the
**confident lead** for all three. Only `_R_HEALTH`'s single-skill
shortcut is skipped.

So the sensing half and the answering half are connected, just not
through memory. The promise holds by the `health_data` route. This is
recorded rather than "fixed" because there is nothing broken here, and
because a future reader who finds the memory.db gap will otherwise draw
the same wrong conclusion.

Both halves were load-bearing and untested. A guard now pins them so the
path cannot regress into the 115bpm noise silently.

**Files:** `tests/test_biometrics_reachable_from_a_turn.py` (new, 7
tests, characterization - they pass before and after by design).

---

## Findings recorded, NOT fixed - owner decisions

### C-06 - `paired_devices.db` only ever sees browsers

61 rows, and every single one is `kind='browser'` with `capabilities`
`[]` **and `node_id` empty**. 59 `device_credentials`, all
`bearer_kind='phone_bearer'`. 1 `pending_pair_codes` row, never claimed.

The six iPhone HUP nodes that produce every `node_subdevices` row are
**not in this table at all**. So "what has paired" and "what is actually
connected" are two disjoint worlds on this install, and a user reading
the paired list sees 61 browser sessions and none of their hardware.

The HUP path is not missing - `claim_pending_code` mints
`pair_device(name, kind="hup", node_id=...)` - it has simply never been
exercised here. Note also that even that path passes no `capabilities`,
so the column would stay `[]` for real hardware too, while
`node_register` carries a capabilities list the brain already reads.

Not fixed because making nodes register as paired changes pairing and
auth semantics, which is an owner call, not a defect fix.

### C-07 - `agents/context_engine.py` is 227 lines with zero callers

The `ContextEngine` ABC, `DefaultContextEngine` (token estimation,
LLM summarisation compaction, checkpoint ring), and the
`register_context_engine` / `get_context_engine` / `set_default_engine`
registry have **no importer anywhere in production or tests**. The only
two references outside the file are a prose line in
`agents/token_estimate.py:21` and a test that reads the file as *text*
to check it uses the shared estimator. Nothing constructs it.

The orchestrator's real context path is `self.context_manager.compact`
plus `memory.compact_session`, and the real prompt assembly is
`IdentityLoader.build_system_prompt`. This is exactly the defect class
the brief named - a context builder with a branch nobody reaches - but
it is dead rather than wrong, so it is reported for an owner decision
(wire it, or delete it) rather than deleted here.

### C-08 - The gateway is live; its legacy bridge is not

`gateway/*` is real and reached: `BrainState` builds `MethodRegistry` +
`register_core_methods` at `api/state.py:1702-1703`, `api/server.py:1614`
constructs a `GatewaySession` per socket, and `api/server.py:1677`
routes `req` / `res` / `event` to it before `parse_message`. **25
methods** register (chat.send, memory.search, hardware.execute,
node.invoke, vision.frame, taskflow.*, session.*, ...). That answers the
brief's question about the envelope that is not in `MESSAGE_TYPES`: it is
the typed gateway RPC and it is live.

`GatewaySession._handle_legacy` is not. It only runs for a `type` that
is *not* req/res/event, and `api/server.py` sends only those three to
the gateway, so it is unreachable from the live server. Two of its eight
mappings also point at methods that were never registered:
`device_register -> device.register` and `vision_query -> vision.query`.
Reported, not fixed: inventing those two handlers is a feature decision.


---

## D-01 - Connected devices: a device could never read as disconnected

**Status:** FIXED, in the tree, not committed. Owner report, verbatim:
devices that were connected and then disconnected still show as
connected; the same device appears many times; reconnecting is not
easy; a phone shows up as a browser connection he never made; glasses
connected to an iPhone should be a sub-device of that iPhone.

Five separate defects, all re-verified against the live install at
`~/.feral` (copies, never the live files).

### What was measured

```
$ sqlite3 memory.db "SELECT count(*), count(DISTINCT node_id) FROM node_subdevices;"
7|6
$ sqlite3 paired_devices.db "SELECT kind, count(*) FROM paired_devices GROUP BY kind;"
browser|61
$ sqlite3 paired_devices.db "SELECT count(*), sum(claimed_at IS NOT NULL), sum(node_id != '') FROM paired_devices;"
61|18|0
```

Correction to the brief: the 7 sub-device rows are spread across **six**
distinct `feral-iphone-*` node ids, not five. Six of the seven rows are
`jw_health_glasses`.

### D-01a - Disconnect had no representation at all

Not "the dot was wrong": there was no disconnected state to render. The
`except WebSocketDisconnect` teardown at `api/server.py:3533` pops
`state.daemons`, calls `hardware_mesh.on_node_disconnected` (which runs
`DeviceRegistry.unregister_device`), and unregisters from the capability
registry. A phone that drops does not become disconnected, it stops
existing. `/api/devices/connected` then returns `{"devices": []}`, the
`connected_devices` tool reads the emptied `device_registry` and answers
"nothing is connected", and the topology renders "Awaiting node". Absence
reads as "you never owned a device", so the last thing the owner ever saw
for that phone was a green pulsing dot.

`DeviceTopology.jsx:161`'s hardcoded `<StatusDot tone="live" pulse />`
and `_describe_device`'s hardcoded `"status": "connected"` were true only
because those lists held open sockets and nothing else. Both are now
bound to a real flag.

**Fix.** New `api/device_view.py` joins the live daemon set against
`memory/node_subdevices.py` (the only store that outlives a socket) and
emits one tree that every surface reads. `/api/devices/connected` keeps
`devices[]` selection-bound to open sockets (contract unchanged, existing
tests still pass) and adds `offline[]` plus `heartbeat_window_s`.

### D-01b - The heartbeat window: 30 s, derived not invented

`HUP_SPEC.md` keepalive row: `node_heartbeat` every `heartbeat_ms`
(default 10000), brain MAY close with `4004 stale_heartbeat` after 3x
that interval. `models/protocol.py:1028` and `api/server.py:2326` both
carry the 10000 default. 3 x 10 s = **30 s**, which is also what
`memory/node_subdevices.LIVENESS_WINDOWS["ble"]` already used, so the
phone and the BLE peripherals behind it derate on the same clock and no
surface can show "phone offline, glasses live". Sub-devices keep their
provenance windows (ble 30 / host 60 / cloud 300) because a cloud-synced
account genuinely reports on a slower cadence; each row carries its own
`liveness_window_s` so nothing has to guess. A test asserts the constant
against `NodeAckPayload.heartbeat_ms` so the two cannot drift.

### D-01c - Why one physical device appears six times

Established, not guessed: the store is keyed `(node_id, capability)` and
the iOS companion mints an install-scoped node id
(`feral-nodes/ios-app/Sources/FeralBridge/FeralBrainClient.swift:179`
shows the SDK's own `feral-iphone-` prefix). Six installs produced six
node ids for one phone, so the same W300 glasses wrote six rows.

**Not fixed at source, and deliberately so.** Making the node id stable
across reinstalls changes pairing identity, and the iOS app that mints
these ids is not in this tree. **Owner decision required:** derive the
node id from `identifierForVendor` (stable per vendor per device until
every app of that vendor is deleted) or from a Keychain-persisted UUID
(survives reinstall). Recommend the Keychain UUID; `identifierForVendor`
still rotates on full uninstall.

Presentation is fixed instead, non-destructively. `node_family()` strips
a trailing install nonce (>=6 hex or >=4 digits, never the last segment)
so `feral-iphone-*` collapse to one entry; offline installs fold into the
live node of the same family, or into the family's most recent member.
Two live nodes are never merged. `group_subdevice_rows()` collapses
repeated observations of one peripheral, splitting on distinct non-empty
`attrs.device_name` so a W300 and a W610 never merge. Nothing is deleted:
`also_known_as` and `also_seen_via` name every collapsed id.

Measured on a copy of the live `memory.db`: 7 rows / 6 node ids becomes
**1 phone with 2 peripherals**, `jw_health_glasses (W300)` carrying
`observations=6, also_seen_via=5`.

### D-01d - The browser rows are real rows, but they are not browsers

The 61 `kind='browser'` rows are an artefact of two things.

1. `GET /api/devices/pair/url` and `/pair/qr` both called
   `store.pair_device(name, kind="browser")`. `kind` was stamped at
   TOKEN-ISSUE time, when the brain cannot know what will scan the QR.
   Opening the pair screen recorded a browser pairing. **43 of the 61
   rows were never claimed by anything** - they are pairing codes, not
   devices.
2. `mark_claimed` wrote only `claimed_at` and `last_seen`. The /pair page
   POSTs `kind: "browser_node_v2"`, which is the TRANSPORT the page
   speaks, not what the device IS; an iPhone in Safari sends exactly that
   string. So the remaining 18 claimed rows kept saying "browser", and
   `node_id` stayed empty on all 61, meaning no pairing could ever be
   joined to a node that connected.

**Fix at source.** Both mint `kind="pending"` now. `Pair.jsx` sends
`platform` (its user agent) and `node_id` (the stable
`browserNodeId()` from `BrowserNode.js`, exported for this).
`mark_claimed(token, kind=, platform=, node_id=)` resolves the real kind
via `kind_from_platform` and records it, downgrading a known kind never
(only `""`, `"pending"` and `"browser_node_v2"` are replaceable). A
claim with no identity behaves exactly as before.

**No history deleted.** `describe_pairing_row` adds derived
`is_device` / `label` / `explain`. Against the live DB the 61 rows now
read as **18 devices ("Browser") and 43 "Pairing code (unclaimed)"**.
The 18 are genuine: the v2 web client IS a node (it streams camera and
mic). Legacy rows keep `kind='browser'` because they carry no `platform`
and inventing one would be a guess.

### D-01e - Reconnect: the brain cannot, and now says so

Established rather than assumed. There is no outbound path to a node
that is not holding a WebSocket: `~/.feral/data/push_tokens.db` does not
exist, so `channels/push.py` has zero registered APNs/FCM tokens and no
configured credentials, and the pairing handshake is phone-initiated.
`reconnect.brain_can_initiate` is a field, always `False`, so no surface
can render a button that does nothing. The steps the brain emits are the
two real ones: reopen the app (the iOS client retries 10 times with
backoff), and if it does not return, re-pair because
`DEFAULT_TTL_SECONDS` is 24 h.

**Open, needs an owner call:** a 24 h pair-token TTL means a phone left
off for a day must re-scan a QR. That is most of "reconnecting is not
easy". The runtime `phone_bearer` already lasts 30 days
(`DEFAULT_PHONE_BEARER_TTL_SECONDS`), so raising the pair TTL to match,
or having `verify_device`'s sliding window cover the app's normal
lifecycle, is a security tradeoff rather than a defect fix.

### Proof

New tests, both written first and run against the unfixed tree:

* `tests/test_disconnected_devices_are_visible.py` - **14 failed / 0
  passed before, 14 passed after.** Seeded with the owner's exact 7 rows.
* `tests/test_pair_row_records_the_device_not_the_browser.py` - **7
  failed / 1 passed before, 8 passed after** (the 1 is the
  backward-compat claim path, which had to pass both sides).
* `feral-client-v2/src/__tests__/DeviceTopology.disconnect.test.jsx` -
  **5 failed / 2 passed before, 7 passed after.**

Two existing tests asserted the old pair-token behaviour and were
updated with the reason inline, not silently:
`tests/test_pair_flows.py:127` and
`tests/test_demo_mobile_ambient_smoke.py:68` (`kind` "browser" ->
"pending").

Full runs:

```
cd feral-core && ruff check --select=E,F,W --ignore=E501,E402,F401,W291,W293 .
  All checks passed!
cd feral-client-v2 && npx vitest run
  Test Files  98 passed (98)      Tests  614 passed (614)
cd feral-core && PYENV_VERSION=3.11.11 python3 -m pytest tests/ -q -p no:cacheprovider -p no:randomly --no-cov
  6 failed, 7805 passed, 31 skipped in 427.43s
```

### The 6 remaining failures are another lane's, and the cause is named

`tests/test_phone_bearer_allowlist_route_coherence.py` (3) and
`tests/test_phone_bearer_http_auth.py` (3), all `401 != 200`. Not mine,
and proved so rather than asserted:

* They fail with **both** of this lane's new test files removed
  (`--ignore` on each): still 6 failed.
* Reproduced by the minimal pair
  `pytest tests/test_execution_audit_trail.py tests/test_phone_bearer_http_auth.py`
  -> 3 failed. Neither file, nor `memory/execution_audit.py`, nor the
  `APIKeyMiddleware`, is touched by this lane.
* On a pristine `git worktree` at HEAD that pair **passes**. Overlaying
  every one of this lane's changed source files onto it: still passes.
  Overlaying the perception lane's uncommitted `tests/conftest.py`
  (its new `_reset_glasses_buffer_registration` autouse fixture):
  **fails, 3 failed.**

Mechanism, instrumented: `test_execution_audit_trail.py::
test_offline_tooling_stays_silent` does
`monkeypatch.delitem(sys.modules, "api.state")`. With the new autouse
fixture in play, `api.state` is re-imported while that entry is gone, so
a second module object is created and bound as the `state` attribute of
the `api` package. monkeypatch restores `sys.modules["api.state"]` at
teardown but not the package attribute, leaving them permanently
desynchronised:

```
--- clean ---
PROBE1 file: .../feral-core/api/state.py id: 4501583920
PROBE1 in sys.modules: True
--- after test_offline_tooling_stays_silent ---
PROBE1 file: .../feral-core/api/state.py id: 4530809856
PROBE1 in sys.modules: False
```

Every later `monkeypatch.setattr("api.state.state", mock)` then patches
the wrong module object, so `APIKeyMiddleware`'s per-request
`from api.state import state` reads the real BrainState, finds no
matching phone bearer, and 401s. The fix belongs to whoever owns that
conftest fixture: either restore the `api.state` package attribute in the
delitem test, or stop importing `api.state` from an autouse fixture.
