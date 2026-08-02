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
- **The env jail breaks subscription logins.** Claude Code stores auth in
  `~/.claude`; a jailed agent gets a fresh HOME and must be API-key
  authenticated. Escape hatch is `external_agents.env_jail: false`.

### Yours
- **Rotate the OpenAI key** committed in Theora's git history
  (`ios/Theora/Configuration.swift:36`, in history since `546e0e7`).
- **Publish gates.** Both releases went straight to PyPI; I claimed they would
  pause for approval and they did not. That is a GitHub environment setting.

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
