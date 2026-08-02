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
**DONE, after being found broken.** Plan mode did not hold: the gate lived in
`ToolRunner` while `SkillExecutor.execute` has seven callers, only two of which
go through it. A live brain in plan mode with autonomy `strict` created a
reminder with no refusal and no approval prompt. Gate moved to the executor
chokepoint. Todo tracker is one endpoint on `feral_workflows`, not a fifth
overlapping concept.

**Still open:** no live run has yet observed a plan-mode dispatch refusal.

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

### Mine, not started
- **Checkpoint UI.** REST and CLI only; nothing in the web UI lists or reverts a
  checkpoint, and a refusal is not shown.
- **`checkpoints.py:395`** returns `dry_run: true` on a refused revert, so a UI
  cannot tell a refusal from a preview. Fix before building UI on it.
- **Env-leak guard test.** Test isolation broke twice today from settings writing
  to `os.environ`. A test that fails the build on a leak would have caught both.
- **Live verification of hermes and the two Node shims.**

### Needs your decision
- **`SkillExecutor._gate` fails open** when no ToolRunner is reachable, so tests
  and the CLI work. Fail-closed is more honest and more disruptive.
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
