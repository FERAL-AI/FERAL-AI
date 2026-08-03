# Worklog: what was asked, what is done, what is not

Single source of truth for this stream of work. Updated as things land.
Last updated 2026-08-01.

Status vocabulary, used strictly:
- **DONE** shipped and verified, with the evidence named
- **BUILT** code exists and tests pass, never run against a live brain
- **PARTIAL** some of it works, the rest is named below
- **NOT STARTED** no code

---

## Shipped releases

| Version | What |
|---|---|
| v2026.8.1 | setup correctness, coding-harness reliability, voice rebuild, plan mode |
| v2026.8.2 | external coding agents, cross-agent memory, command hardening |

Both live on PyPI. Suite at time of writing: **6595 passed, 35 skipped**.

---

## Requests and status

### 1. Coding harness reliability
**DONE.** Six fallback edit matchers (strictest first, ambiguity is a hard
failure), read-before-edit staleness guard, content-addressed checkpoints with
revert, post-edit diagnostics with baseline diffing. Evidence: `edit_matchers.py`,
`file_state.py`, `checkpoints.py`, `diagnostics.py`, 156 tests.

### 2. Plan mode and todo tracker
**DONE, verified against a live brain.** Plan mode did not hold: the gate lived
in `ToolRunner` while `SkillExecutor.execute` has seven callers, only two of
which go through it. A live brain in plan mode with autonomy `strict` created a
reminder with no refusal and no approval prompt. Gate moved to the executor
chokepoint. Todo tracker is one endpoint on `feral_workflows`, not a fifth
overlapping concept.

**Two further bugs the live run found, both now fixed:**

1. `api/routes/tools.py` never bound `ToolCallContext`, so every call arrived at
   the executor with `session_id: ""` and plan mode could not match the session.
   The route already accepted `session_id` in the body and dropped it. The
   `pending_approval` payload now carries `session_id: "live-plan-test"` where it
   previously carried `""`, which is the direct evidence the binding works.

2. Plan mode blocked read-only tools too, while the refusal text instructed the
   agent to "finish investigating with read-only tools". `is_plan_safe_tool`
   requires an explicit `read_only_hint: true` and fails closed without it, which
   is the correct posture, but only **50 of 183 endpoints (27%)** declared it.
   Eighteen genuinely non-mutating endpoints were annotated after reading each
   one, taking coverage to 68. This was a manifest data gap, not a gate defect,
   so the gate logic is unchanged.

`web_actions__search_and_compare` was deliberately **left blocked**: it drives a
real browser through the user's live session and navigates pages, so it is not
declarable as incapable of side effects.

Live probe matrix, brain on 9414 with `FERAL_AUTONOMY=strict`:

| Call | In plan mode | Expected |
|---|---|---|
| `feral_reminders__list` and 3 other reads | allowed, `success=True` | allowed |
| `feral_reminders__create` | `plan_mode_blocked` | blocked |
| `notes_memory__save_note` | `plan_mode_blocked` | blocked |
| `feral_workflows__todo_write` (escape hatch) | allowed | allowed |

After the two blocked creates, `feral_reminders__list` returned
`{"items": [], "count": 0}`: the refusals wrote nothing. Leaving plan mode as
`actor=user` and repeating the create returned `pending_approval`, which is
strict autonomy working, not a failure.

### 3. opencode and hermes as an optional install
**DONE for opencode, BUILT for hermes.** ACP bridge in `bridges/`, driven
against a real opencode 1.18.10 binary: 1026 streamed events, a real tool call,
permissions answered both ways, a file written through our handler. `feral setup`
installs a pinned opencode with `--no-modify-path`.

**hermes is untested against a real binary.** Its code path exists.

### 4. Claude Code and Codex
**BUILT, untested.** Neither speaks ACP natively (`claude` 2.1.220 has no `acp`
subcommand; Codex has an open request). Both work through Zed-maintained Node
shims. Those shims are **not installed here**, so the launch path has never run.

### 5. CLI setup, investigated and live-tested
**DONE.** The default install was broken: Ollama's base URL lacked `/v1`, so a
fresh brain booted, reported `LLM: ready`, and 404'd every turn. The wizard also
could not be completed by pressing enter. Both fixed, wizard walks all 14 steps.

### 6. Local STT and TTS
**DONE, verified with real audio.** whisper.cpp 109-162 ms (0.022x realtime),
faster-whisper 241-285 ms, Piper 630 ms synthesis. Real chained turn: **1221 ms
to first audio with Piper**, 2319 ms with macOS `say`.

**Correction to earlier advice:** I told you to drop Piper. That was wrong.
Piper is ~3x faster than `say`. Piper 1.4.2 works on macOS; 1.5.0 and 1.6.0
abort with a hardcoded CI espeak path, and `pyproject` would have installed
1.6.0. Now pinned `<1.5` on darwin.

### 7. WebRTC
**DONE, answered: do not adopt.** Theora's own docs already rejected it (WebRTC
owns the audio device module and fights the manual HFP/SCO route control glasses
need). Independent research agrees: FERAL already carries Opus, the one published
production A/B found WebRTC slower, iOS gives AEC from one line regardless of
transport, and a local-first brain wants an overlay network rather than a TURN
relay you host forever.

### 8. Provider choice with fallback to local
**DONE.** Ordered chain terminating at local chained. Surface-aware:
`phone`, `ios`, `iphone`, `ipad`, `glasses`, `watch` default to realtime-first,
desktop and web to local-first.

### 9. Voice failure diagnosis
**DONE.** Twelve causes with concrete next steps. Never fabricates a diagnosis,
and never reports a silent cloud fallback as success for someone who chose local
for privacy. That last one was a real bug: `_pick_fallback_provider` returned
OpenAI's `/audio/speech` whenever any OpenAI key existed.

### 10. Mute
**PARTIAL.** Brain ledger and web client done. Survives reconnect, fails safe
toward muted. **iOS not done**, specified in `THEORA_IOS_TASKS.md`.

### 11. Whoop, and Whoop reaching Theora
**DONE brain-side.** Whoop was live-fetched into a transient dict, so no history
existed. Now mirrored into `biometric_samples` with a 400-day horizon while live
sensors still prune at exactly 35 days. New `health_update` frame, payload
documented in `THEORA_IOS_TASKS.md`.

**Not done:** Oura sync. Oura's OAuth was broken (client built without the
manager) and is now fixed, but only Whoop syncs.

### 12. Smoother integrations
**DONE.** Whoop could not be connected at all: Authorize opened a JSON popup
rather than the consent screen. Skill API keys were a dead end (UI wrote them,
nothing read them). Home Assistant had no URL field. The "connected" badge meant
"a token string exists" because probes never ran; there is now a sweeper and a
real refresh route, and unverified rows say "stored, unverified".

### 13. Cross-agent memory
**DONE.** One episode per agent turn in the existing memory store, surfacing
through `notes_memory__fused_timeline`. Keeps one entry per tool call, files
touched, and **permissions including refusals**, which the event stream never
contains. Prefers `session/resume` over `session/load`, because load replays the
whole conversation and would make the next turn's summary claim the previous
turn's work.

### 14. Ambient recording
**NOT STARTED, by agreement.** You are building the iOS capture side. Architecture
agreed: glasses capture, iOS relays, brain does STT locally with whisper.cpp,
transcript never leaves the machine, the configured LLM summarizes the text.

**Prerequisite:** whisper.cpp is now verified with real audio, which this depends on.

### 15. qm investigation
**DONE.** Real, not hype, but ~70% is the wrong shape (cloud multi-tenant vs
local-first). Took four things: the recursive shell unwrapper, a ReDoS-safe regex
compiler, the security classifier prompt, and env jails.

### 16. Docs
**DONE.** Three guides plus `THEORA_IOS_TASKS.md` and
`FERAL_COMPANION_IOS_TASKS.md`.

---

## Open, and who owns it

### Memory, made to work A-Z (v2026.8.3)

Driven by one instruction: memory is the product's strongest feature, so exercise
it rather than reason about it. Everything below was found by actually installing
the optional packages and running them, and none of it was visible before.

**Embeddings are free and local by default.** `fastembed` +
`BAAI/bge-small-en-v1.5` (384-dim, matches the existing `LOCAL_DIM`, no index
migration, no torch) now ship in core `dependencies`, with a wizard step that
installs, warms the model, and verifies semantically rather than checking that an
import succeeded. Measured here: 0.42s cold including model load, 17ms warm.

Previously the mere presence of `OPENAI_API_KEY` selected paid OpenAI
embeddings, so a key exported for chat silently billed every memory write.
`FERAL_EMBED_PROVIDER` now gates that: `auto` (default) never selects a paid
provider, `openai` is an explicit opt-in.

**Retrieval proven end to end**, with near-zero lexical overlap so only the
vector leg can satisfy it:

| query | top hit |
|---|---|
| "coffee machine upkeep" | "The espresso machine needs descaling every two months." |
| "how does the eyewear send sound?" | "Theora glasses stream audio over BLE to the iPhone." |

**Qdrant was broken in four ways**, all hidden because its tests skip unless the
package is installed and it never is:
- `memory/backends/qdrant.py` called `client.search()`, removed from
  qdrant-client (gone in 1.18). Every query raised `AttributeError`.
- The vector-index adapter passed raw chunk ids to Qdrant, which requires a UUID
  or uint64. FERAL's look like `note_984acdb8_c0`, so every write failed. The
  sibling module already solved this with `_to_qdrant_id`; the adapter never got
  it.
- Search returned Qdrant's internal UUID rather than the caller's chunk id, so a
  hit could not be mapped back.
- All of it was silent: `search` caught everything at `logger.debug` and returned
  `[]`, so a completely broken backend looked like "no results".

Both adapter tests also called async methods without awaiting, asserting on a
coroutine object. Chroma was equally unexercised but turned out correct.

**The numpy fallback was ~11x slower than it needed to be.** It scanned
candidates in a Python per-row loop (`blob_to_vec` + `cosine_similarity`) where
one blocked matmul would do. Measured 384-dim: 273ms to 23ms at 100k rows, 27ms
to 2.7ms at 10k, results identical within 1e-6. Converted at five call sites.
This matters more than sqlite-vec, because it is the path that actually runs
whenever the extension cannot load.

**sqlite-vec cannot load on many macOS installs**, and the old diagnostic blamed
the wrong thing. pyenv builds CPython without loadable SQLite extension support,
so `pip install sqlite-vec` succeeds and the extension still never loads. FERAL
now says exactly that, with the rebuild flag, instead of "not installed".

### Streaming defaults ON, and that exposed an unbilled path

Two sources of truth disagreed: `agents/orchestrator.py:306` defaulted
`FERAL_STREAMING` to `"true"` while `config/loader.py:246` defaulted the setting
to `False` and exported `FERAL_STREAMING=false`. Git history shows the runtime
literal was flipped to streaming-first in `256be0fcd` and the settings side was
never updated, a four-day-old omission. Now one `DEFAULT_STREAMING = True` that
both sides import, so they cannot drift again. Env override still wins.

Side effect worth noting: the chained voice pipeline taps `stream_delta` to
speak sentence 1 while the model writes sentence 2, so with streaming
effectively off, every chained-voice turn was taking the slow fallback.

**Flipping it surfaced a real gap: streamed turns were never billed.** The
chat-completions SSE route never sent `stream_options.include_usage` and the
Anthropic route discarded `message_delta` usage outright, so nothing reached
`cost_events` and a configured cap could not trip. Both now record once per turn.
An endpoint that rejects `stream_options` is retried once without it and
remembered, so the turn survives where usage cannot be captured, and nothing is
ever estimated. Proven by moving a real `CostBudget` from under to over cap and
watching `check_and_reserve` flip.

That fix also closed a latent crash: `chunk.get("choices", [{}])[0]` raised
`IndexError` on a usage-only chunk, which would have fired on every OpenAI turn
the moment we asked for usage.

**Still open here:** live-provider verification of `stream_options` support is
outstanding (all of it is mocked SSE), Anthropic prompt-cache tokens are not
billed on either path because `cost/pricing.py` has no cache rates, and
`FERAL_MULTI_AGENT` has the identical two-defaults defect in the opposite
direction.

### `feral doctor` was printing install commands that install the wrong thing

Every extras hint in the command was mangled by Rich, which parsed the bracket
as a style tag: `pip install 'feral-ai[embeddings]'` rendered as
`pip install 'feral-ai'`. Same for `[browser]`, `[stt]`, `[tts]`,
`[memory-chroma]`, `[memory-qdrant]`, `[macos-extras]`. Fixed once at the render
point so a future probe cannot reintroduce it by writing a bracket in a detail
string. The same latent bug exists in `cli/setup/steps/voice_preflight.py` and is
NOT fixed.

### Follow-ups after v2026.8.3

**`FERAL_MULTI_AGENT` drift closed, and the runtime literal was the stale side.**
The mirror image of streaming. `d447a2c87` introduced the gate defaulting
`"true"`; `4df5fc1cc` flipped it to `"false"` reasoning that "multi-agent was
enabled by default, bypassing all tool use"; then `c7ac82c20` added the whole
settings side ON (`features.multi_agent: True`, exported True, dashboard
defaulting True) and never touched the runtime literal. Disagreeing since April.

Settings win at boot because `export_as_env` publishes before the orchestrator
reads, so shipped behaviour has been ON for four months and the codebase was
built around it: the plan-mode and approval gates added to the worker path on
2026-08-01 were justified explicitly by "multi_agent defaults True, so this is
the primary text chat path". The April rationale no longer describes the code.
Now one `DEFAULT_MULTI_AGENT = True` referenced by both readers.

**Blast radius:** none on a real install, since the loader already exported
`true`. It DOES change loader-less processes (embedded use, direct unit-test
construction) from single-agent to multi-agent, which skips `_route_prompt`
semantic routing, `_ensure_core_skills`, mitosis routing, `_force_tool_for_query`
and MCP tool injection on that branch.

**`stream_options.include_usage` verified against the live OpenAI API.**
`scripts/verify_stream_usage_live.py` exists for exactly this, since mocked SSE
proves our parsing and cannot prove a vendor still honours the opt-in. One real
call returned `prompt_tokens: 14, completion_tokens: 1, total_tokens: 15`, so
streamed turns on the default provider genuinely bill.

**Still unverified:** groq, deepseek, openrouter and anthropic, because no keys
for them are set here. Run `python3 scripts/verify_stream_usage_live.py --all`
with those keys present. A provider that silently ignores the opt-in reports
nothing and FERAL records nothing rather than estimating, so a cost cap on that
provider is advisory until checked.

**Anthropic prompt-cache tokens are billed, and a much larger bug turned up
underneath.** The cache gap was real: writes cost 1.25x base input and reads
0.1x (5-minute TTL, per
`https://platform.claude.com/docs/en/about-claude/pricing`), and neither was
counted, so a cache-heavy turn under-counted by **5x** on a representative shape
($0.015 recorded against $0.075 actual).

Only the 5-minute write rate is stored. FERAL never sends `cache_control.ttl`
anywhere, so the 1-hour rate is unreachable and billing at its 2x would
over-charge by 60%. If that ever changes, the 1h writes will under-bill.

**The larger find: non-streaming Anthropic turns were billing $0 for
everything**, input and output included, not just cache.
`_normalize_anthropic_response` never emitted a `usage` key at all (verified
against HEAD: the function does not contain the word), so `_budget_record`
received nothing on every non-streamed Anthropic call. Both normalizers now
carry a usage block through.

Also corrected: `claude-sonnet-4-6` carried Sonnet **5**'s introductory cache-read
rate. All 15 Anthropic catalog entries now re-derive exactly from the published
multipliers, checked independently.

**Not billing cache tokens, named:** every non-Anthropic provider (OpenAI's
cached-input discount is a different shape, no write charge and a different read
ratio, and was not invented), unknown models falling through to the pricing
fallback, and 1-hour-TTL writes if they are ever sent.

**Caveat worth knowing:** `cost_events` has no cache columns, so cache tokens are
folded in as dollar-equivalent base-input tokens. The dollars are exact but
`cost_events.prompt_tokens` is now a billing-equivalent count rather than the raw
wire number. Confirmed nothing outside tests SELECTs that column. A faithful
token-level record needs a `record_usage` signature change and a schema
migration in `cost/budget.py`.

**Still unverified:** no live Anthropic call was made, so the assumed shape of
`cache_creation_input_tokens` / `cache_read_input_tokens` on `message_start`, and
reconciliation against a real invoice line, remain open.

**`requirements.lock` was stale in both directions.** Missing `fastembed`,
`sqlite-vec`, `py-rust-stemmers`, `mmh3`, and still pinning `piper-tts`, which
`pyproject` deliberately keeps out of `[all]` for being GPL-3.0-or-later. So the
lock carried a package the project intentionally excludes. Regenerated: 7 added,
2 removed, **zero existing pins changed version**. Note for next time:
pip-tools 7.6.0 cannot run against pip 26 (`stdlib_pkgs` was removed from
`pip._internal.utils.compat`), so it needs a throwaway venv holding `pip<25.3`.

**CI was downloading a 130MB model mid-suite.** Once `fastembed` became a core
dependency, `importorskip("fastembed")` stopped skipping and `TestRealModel`
began fetching the model inside the runner. It did not complete, the provider
degraded to hash vectors, both scores came back `0.000`, and the suite went from
under 4 minutes to **35**. Now gated on the model already being cached, with the
download opt-in behind `FERAL_TEST_DOWNLOAD_MODELS=1`.

**16 more instances of the Rich bracket bug**, in `voice_preflight`, `audio` and
`memory` setup steps. Checked rather than assumed: the occurrences in
`cli/main.py` outside `cmd_doctor`, in `cli/app_commands.py` and in
`cli/memory_cmd.py` go through plain `print()`, which does not parse markup, so
they were correct already and left alone.

### CI on main was red, and it was a real inconsistency

`tests/perf/test_lane08_live_traces.py::test_trace_2_stream_nonstream_parity`
had been failing in CI. Not flaky, and not caused by this work (it fails
identically against the pre-change conftest).

Streaming has two different defaults depending on which you reach first:
`agents/orchestrator.py:306` reads `os.environ.get("FERAL_STREAMING", "true")`
while `config/loader.py:246` defaults the setting to `False` and line 1280
exports `FERAL_STREAMING=false` from it. On a fresh `FERAL_HOME` streaming is
therefore OFF, `handle_command_stream` emits no `stream_delta` frames, and the
parity test compares an empty assembled string against a real non-streamed
reply.

It passed only when an earlier test had left `FERAL_STREAMING` set, which is
why a full local suite went green while CI's isolated perf job failed. The test
now sets the feature it is testing. `tests/perf/` is 7 passed in isolation.

**The underlying inconsistency is still there** and is worth a decision: the
code default says streaming is on, the settings default says it is off.

### The six dialing tests are fixed, and the cause was not what it looked like

Neither file was "a test that forgot to stub". Both were hiding something:

- `switch_provider()` itself fires a genuine `POST /chat/completions`
  (`agents/llm_provider.py:2770`), a deliberate round-trip availability probe
  added in v2026.5.23 so it can report a model as actually reachable.
  `_probe_chat_availability`'s broad `except Exception` swallowed the network
  guard's error, which is exactly why a strict-mode run still showed "passed".
  Now wired to a `MockTransport` that records the dialed host, so the tests
  additionally assert the request went to `api.groq.com` / `api.deepseek.com` /
  `openrouter.ai`. That proves more than before, not less.

- `test_ambient_api.py::_make_mock_state()` built a bare `MagicMock` and never
  set `s.vault`. `api/routes/ambient.py:86` checks
  `hasattr(state, "vault") and state.vault`, and on a MagicMock both the
  attribute and `retrieve(...)` auto-vivify as truthy, so **the "no API key"
  test was never testing the no-key path**. Fixed by setting `s.vault = None`,
  which is the truthful state for a brain that never wired a vault.

Verified: 46 passed with zero `[network guard]` output, in plain mode, strict
mode, and strict mode with a key present.

You asked for this to stop being a problem rather than be patched case by
case. What the evidence turned up:

- **The root cause of the whole "no key configured" family was one line.**
  `security/vault.py` holds a process-wide `_vault` singleton, keys resolve
  through the **vault first** and the process env second, and `reset_vault()`
  existed for tests but nothing called it between them. So a test that seeded
  the vault made every later test believe a key existed regardless of what env
  it cleared. That is six separate failures across shuffled runs, all one bug.
  It was invisible to a scan for mutable module globals because a lazily
  initialised singleton starts life as `None`.

- **`tests/conftest.py` now has one list**, `_SHARED_STATE_RESETTERS`, naming
  every global reset between tests, with a companion comment naming what is
  deliberately NOT reset and why. The rule that matters: reset what is filled
  at **runtime**, never what is filled at **import**. Clearing a provider
  registry populated by import-time decorators empties it for the rest of the
  session.

- **Some tests do reach external hosts. The number is small, and an earlier
  version of this file said 112, which was wrong.**

  The 112 came from my own broken measurement: the guard hooks
  `httpx.Client.send`, which runs for `httpx.MockTransport` too, so every
  correctly-stubbed test counted as a network attempt. Six
  `test_whoop_durable_sync` tests were reported as calling
  `api.prod.whoop.com` when all six were already wired to a `MockTransport`.
  The guard now ignores Mock/ASGI/WSGI transports.

  Real, verified: `test_llm_provider.py` and `test_ambient_api.py` attempt 6
  calls (`api.deepseek.com`, `api.groq.com`, `openrouter.ai`,
  `api.openweathermap.org`).

  **How to check this correctly, because the obvious way is wrong.** Running
  with `FERAL_STRICT_TEST_NETWORK=1` and seeing everything pass does NOT mean
  nothing dialed out. Those two files report `46 passed` AND 6 recorded
  attempts in the same run, because the provider-probe code catches the
  guard's exception in a broad `except`. Read the `[network guard]` report at
  the end of the run, never the pass/fail count.

  `memory/embeddings.py:801` posting to the live embeddings endpoint on the
  normal code path is a separate and more serious question, because that is
  production code rather than a test: with a reachable key it makes billable
  calls. Three shuffled runs each failed a different memory/KG test on
  `httpcore.ReadTimeout`, which is what a live call looks like on a loaded
  machine. **Still to confirm** whether the memory tests reach it through a
  stub or for real, now that the MockTransport false positive is out of the
  measurement.

- **The env-leak guard warns, it does not fail the build, and that is
  deliberate.** Measured across the full suite: 532 tests leave `FERAL_HOME`
  changed, 181 `OPENAI_API_KEY`, ~70 each across the `FERAL_*` settings keys.
  That is not 532 sloppy tests. `ConfigLoader.update_settings` publishes
  settings into `os.environ` on purpose so a live toggle reaches env-only
  readers without a restart, so any test exercising it "leaks" while behaving
  correctly. Blocking on that would fail the build for working production
  behaviour. `FERAL_STRICT_ENV_LEAKS=1` makes it blocking for cleaning up one
  area at a time. The honest permanent fix is settings that do not write to the
  process environment, which is a design change and is not done.

- **`pytest-randomly` is why "the suite passed" was a weak claim.** Order
  changes every run. CI should pin a seed and additionally run one shuffled
  job, so a regression is reproducible and a new order-dependency still gets
  caught. Not wired into CI yet.

### Mine, done since
- **Refusals now render as refusals.** The gap was narrower than "refusals are
  not shown": the client already had a refusal renderer, but only
  `agents/supervisor.py` ever emitted the frame that drives it. A per-tool
  refusal came back as a tool result with `success: False` and rendered as a red
  "failed" card, identical to a crash, so plan mode looked like a broken FERAL
  rather than a boundary being held.

  Three gates refuse with three different envelopes (`plan_mode_blocked` carries
  a code; a policy deny sets `PermissionOutcome::Deny`; strict autonomy sets
  `pending_approval`). Rather than rewrite three envelopes the LLM also reads,
  `Orchestrator._refusal_code` normalises them at the single point that builds
  the client frame, and `ToolResultPayload.error_code` carries the result. The
  card gets an amber "refused" state, keyed off the code and never off the error
  prose. The set is closed: an unrecognised code stays a loud failure, so a
  future gate cannot be silently softened into a friendly amber card.

- **The voice transcript ordering singleton leaked between tests.**
  `TRANSCRIPT_ORDER` is process-wide by design and hands out a per-session `seq`
  starting at 0. Tests reuse ids like `sess-web`, so whichever ran second got
  `seq == 1`. Now reset by an autouse fixture.

- **The integration probe cache leaked between tests.**
  `integrations/_probe_status` holds a process-local dict of the last probe
  result per provider, and `connected` on the Calendar and Email integrations
  reads it. A test marking "google" reachable made every later test believe
  Google was connected regardless of the env it cleared, so both
  `test_init_no_credentials` cases failed with `assert True is False` under a
  shuffled order. The module already exposed `clear()` for tests and nothing
  called it; there is now an autouse fixture that clears it before and after
  every test, which closes the class rather than the two symptoms.

- **Two order-dependent voice tests.** `test_realtime_no_key_emits_degraded_voice_status`
  and `test_no_openai_key_at_all_keeps_the_legacy_whisper_degrade` both assert
  behaviour "when no OpenAI key is configured" but only cleared the env var.
  Keys resolve through the **vault first** (`get_active_key`), env second, so
  when a shuffled order put a vault-seeding test ahead of them they picked up a
  stray `sk-test`, reached a real handshake, and failed on "Incorrect API key".
  They now stub the vault too. Pre-existing, unrelated to this work, and only
  visible because the order changed.

- **`checkpoints.py`** was worse than recorded. A refusal did not merely *look*
  like a preview, it was byte-identical to one: the drift check ran before the
  `dry_run` check, so previewing a drifted turn returned the refusal envelope.
  Not even `success` separated them, which the old pinning test asserted without
  noticing what it proved. A dry run applies nothing, so it is now answered
  first, and a refusal carries `refused: true` with
  `error_code: "revert_refused_drift"`.

### Mine, not started
- **Checkpoint UI.** REST and CLI only; nothing in the web UI lists or reverts a
  checkpoint. The envelope it needs is now honest, which was the blocker.
- **Env-leak guard test.** Test isolation broke twice from settings writing to
  `os.environ`. A test that fails the build on a leak would have caught both.
  Related and now understood: **`pytest-randomly` is installed, so every run
  uses a different order.** "The suite passed" has therefore been a weaker
  statement than it looked, and a green run does not prove the next one is
  green. Two voice tests were caught by this and fixed (see below), but a
  deterministic seed in CI plus one shuffled run is the real answer.
- **Live verification of hermes and the two Node shims.**

### Needs your decision
- **`SkillExecutor._gate` fails open** when no ToolRunner is reachable.
  **Recommendation: keep it open, and I have made the case observable.** I traced
  every production caller: `direct_execution` is imported only by
  `orchestrator.py`; `mcp/server.py` refuses to execute unless `api.state` wired
  its executor, and the standalone `python -m mcp` path never wires one. So no
  production path reaches the executor without a live orchestrator, and
  fail-closed would break tests and the CLI for no security gain. What was
  genuinely wrong is that the skip was silent: it now logs a warning and
  increments `feral_executor_ungated_total` when `api.state` is loaded but no
  runner is reachable, which is the combination that should be impossible.
- **The env jail vs subscription logins: solved, needs your sign-off on the
  directory location.** You asked whether a client could log in with their own
  Claude / Codex subscription and still reach the external agent. They can, and
  it does not require turning the jail off.

  Both vendors support relocating their entire config directory: Claude Code
  reads `CLAUDE_CONFIG_DIR`, Codex reads `CODEX_HOME`. Passing the *operator's*
  value through would be wrong, since it points straight back at their real
  config and undoes the replaced HOME in one variable, which is why the jail
  refuses it. What is safe is pointing those variables at a directory FERAL
  owns, `~/.feral/agent-credentials/{claude,codex}`, that the operator logs
  into once:

      CLAUDE_CONFIG_DIR=~/.feral/agent-credentials/claude claude /login
      CODEX_HOME=~/.feral/agent-credentials/codex codex login

  After that a jailed agent authenticates with the subscription and still
  cannot read `~/.claude`, `~/.ssh`, `~/.aws` or shell history. Strictly better
  than both previous options. Implemented in `security/env_jail.py`
  (`subscription_credential_env`), on by default, and it only activates when
  the directory exists so nothing changes for anyone who has not logged in.

  **macOS caveat, verified:** Claude Code prefers the Keychain over the file,
  so on macOS the token may not land in the directory. Two documented ways out:
  copy it once with `security find-generic-password -a "$USER" -s "Claude
  Code-credentials" -w > <dir>/.credentials.json`, or mint a long-lived token
  with `claude setup-token` (confirmed present in 2.1.220) and pass it as
  `ANTHROPIC_AUTH_TOKEN`, which needs no directory at all.

  **Not yet verified end to end**, because it needs your subscription: I have
  not logged a real Claude or Codex session into that directory and driven an
  agent through the jail. That is one of the three things you said you would
  test.

### Yours
- **Rotate the OpenAI key** committed in Theora's git history. **Done, you
  rotated it.**
- **Publish gates. I had this wrong twice over.** I said both releases went
  straight to PyPI. In fact **2026.8.2 never reached PyPI at all**: PyPI's
  latest is 2026.8.1. The tag and the GitHub release exist, which is why it
  looks shipped.

  Cause: the release workflow's TestPyPI canary. The wheel and sdist uploaded
  to TestPyPI cleanly at 21:44:57, then the canary polled TestPyPI's simple
  index 12 times over ~2 minutes, never saw it, failed, and that skipped the
  real publish job. The version **is** on TestPyPI now, so the artifact was
  always fine and only the gate was impatient. Widened to ~11 minutes with
  backoff, and the failure message now says whether the upload succeeded and
  tells you to re-run the job rather than re-cut the release.

---

## Corrections I have had to make

Recorded so the pattern is visible rather than repeated.

1. Said Piper should be dropped. It is ~3x faster than `say`.
2. Said `autonomy_mode` was a dead key. It reached the ToolRunner; only the
   shell gate was stale.
3. Said the publish gates would pause for approval. They did not.
4. Said `session_id` was in scope in the barge-in branch. It was not.
5. Said the Wave 2 worktrees were branched from the wave-1 branch. They were not.
6. Reported "7 passed" on a security fix from one file while main was red.
7. Called `bind_context(ToolCallContext(...))` from the assumed signature instead
   of reading it. It is keyword-only. Took the live brain down with a 500.
8. Checked JSON round-trip fidelity on one manifest, then rewrote nine. The other
   eight were formatted differently and got reflowed, turning an 18-line change
   into 1223 insertions and 721 deletions. Reverted and done surgically instead.

Seven and eight are the same failure as one through six: verifying one case and
generalising, rather than checking the case in front of me.
