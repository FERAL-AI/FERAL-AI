# AUDIT-FIXES

Defects found in the 2026-08-11 audit of `d15645cd4` (v2026.8.8). Full report with evidence and methodology: https://claude.ai/code/artifact/7e82aa6b-86ce-4677-9af1-42f018e750bf

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

**Status:** open

```
feral-core/api/routes/apps.py:379                git clone, timeout=120   ← worst
feral-core/skills/marketplace.py:245             git pull,  timeout=30
feral-core/api/routes/system_permissions.py:115  open,      timeout=3
```

The first can stall every concurrent coroutine for two minutes against a slow or hostile remote.

**Done when:** all three use `asyncio.create_subprocess_exec`. Then grep for the class: `subprocess.run`, `subprocess.Popen`, and `shutil.rmtree` reachable from any `async def`.

---

### F-06 · Unreferenced background tasks can be garbage-collected

**Status:** open

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

## P1 — after the P0 set

### F-07 · Gen-UI payload cap disagrees between host and brain by 6x

```
feral-core/genui/app_message_schema.py:113-119        json.dumps(v).encode("utf-8") → len
feral-client-v2/src/pages/AppSurface.types.ts:69-70   JSON.stringify(payload).length
```

Both use `MAX_PAYLOAD_BYTES = 64 * 1024`. Python's `json.dumps` defaults to `ensure_ascii=True`, emitting `\uXXXX` escapes — six ASCII bytes per BMP character, twelve per emoji. JavaScript counts UTF-16 code units.

Measured: `{"a": "中" * 11000}` is 66,009 bytes to Python (rejected) and 11,008 units to JavaScript (accepted). Any payload above ~10,900 non-ASCII characters passes the browser-side security guard and is refused by the brain.

**Done when:** both sides measure the same quantity, and a shared fixture of non-ASCII payloads is asserted in both test suites.

### F-08 · The two node SDKs write different key filenames for the same node

```
feral-nodes/python-node-sdk/src/feral_node_sdk/pairing.py:28   drops invalid chars
feral-nodes/ts-node-sdk/src/pairing.ts:17                      replaces them with "_"
```

Both write to `~/.feral/node-keys/<safe>.key`, so this is a live collision. `"sensor 01"` becomes `sensor01.key` in one and `sensor_01.key` in the other; a node paired through one SDK silently re-pairs under the other. Python's `str.isalnum()` is Unicode-aware and the TS class is ASCII-only, so `café` yields `café.key` vs `caf_.key`. Python's drop-based collapse is also many-to-one, so two node ids can share a key file.

**Done when:** one algorithm, specified in `HUP_SPEC.md`, with the same fixture table tested on both sides.

### F-09 · The install smoke test cannot fail, and runs after publishing

```
.github/workflows/install-smoke.yml    feral --help || true
                                       python -c "…" || true
                                       on: workflow_run: [Release], types: [completed]
```

Both verification commands end in `|| true`, so a wheel whose `feral` entry point raises on import passes. It installs `feral-ai` only, never an extra, while `scripts/install.sh` installs `feral-ai[all]`. And it runs *after* the release workflow, so a failure reports a bad release rather than preventing one.

Related and worth fixing together: `CHANGELOG.md:305` claims "CI now tests Python 3.14", but `ci.yml:82` is `['3.11','3.12']`. `requirements.lock` is a Python 3.11 artifact pinning `pillow==11.3.0`, so it structurally cannot catch the marker-dependent conflict that shipped `2026.8.3` broken.

**Done when:** the smoke job asserts rather than tolerates, installs `[all]`, and gates the publish instead of following it.

### F-10 through F-15 · Smaller confirmed items

| ID | Defect | Location |
|---|---|---|
| F-10 | `mlx-lm` and `sentence-transformers` imported at runtime, declared in neither `dependencies` nor any of the 33 extras | `agents/local_inference.py`, `memory/embeddings.py:1445` |
| F-11 | `pip install 'feral-ai[macos-extras]'` is printed to users; that extra does not exist, so pip installs nothing | `cli/main.py:2593` |
| F-12 | `[wake]` pulls `tflite-runtime`, which has no wheel for Python 3.12+. Nothing gates it | `pyproject.toml:308` |
| F-13 | Token budget uses `len(str(content)) // 4`, under-counting non-Latin and code-heavy content; can overflow provider limits | `agents/context_engine.py:197` |
| F-14 | Desktop updater configured with `"pubkey": ""` and `"signingIdentity": null` — no shipping channel exists | `desktop/src-tauri/tauri.conf.json` |
| F-15 | `FeralBrainClient.swift` exists in two directories and has diverged; `FeralSensorBridge.swift` is byte-identical in both | `feral-nodes/ios-bridge/` vs `ios-app/Sources/FeralBridge/` |

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
- False positive: `mcp/registry.py:267`. `url` has a default of `""`; the model constructs
  fine without it. Cause is that the pydantic mypy plugin **crashes** on mypy 1.20.2
  (`AttributeError: module 'mypy.expandtype' has no attribute 'ExpandTypeVisitor'`), so
  `Field()` defaults are not modelled. Plugin deliberately left off.

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
