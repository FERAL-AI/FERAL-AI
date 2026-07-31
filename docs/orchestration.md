# Orchestration

How FERAL takes a request from anywhere (chat, voice, channel, HUP node,
proactive engine, cron, digital twin) and turns it into a response —
with audit, parallelism, and per-session safety.

## One seat over everything

Every entry point the user can reach goes through the `Supervisor` before
it touches the `Orchestrator`. The Supervisor is a thin wrapper that
records every call (source, kind, actor, decision, latency) into a
SQLite audit log and respects a global kill switch. Commit 2 of this
plan widened it to cover four orchestrator methods: `handle_command`,
`handle_command_stream`, `handle_ui_event`, and `handle_daemon_result`.

```mermaid
flowchart TB
  Web[Web chat]
  Node[HUP node mic/camera]
  Voice[Voice router]
  Channels[Telegram / Slack / Discord / WhatsApp]
  Cron[CronService routines]
  Proactive[ProactiveEngine alerts]
  UIEvent[ui_event from GenUI apps]
  Twin[Digital Twin execute]

  Web --> Sup
  Node --> Sup
  Voice --> Sup
  Channels --> Sup
  Cron --> Sup
  UIEvent --> Sup
  Twin --> Sup
  Proactive -. record automation .-> Sup

  Sup[Supervisor]
  Sup --> Audit[(supervisor_events SQLite)]
  Sup --> Orch[Orchestrator]
  Sup --> Gate["Policy gate (Twin / custom rules)"]
  Orch --> Memory[(4-tier memory)]
  Orch --> Skills[ToolRunner + SkillExecutor]
  Orch --> GenUI[GenUI render]
```

Source tagging is honest: cron-driven turns record `source="cron"`, the
proactive engine records `source="proactive"`, chat turns record
`source="web"`, HUP nodes record `source="node"`, and the twin records
`source="twin"`. You can filter the audit log by source at
`GET /api/supervisor/events?source=cron`.

## A single chat turn

Every call to `handle_command` now runs inside a **per-session async
lock**. Two concurrent turns on the same `session_id` queue — they
share `conversation_history` and the outgoing tool_call ordering, so
interleaving them would corrupt both. Turns across different sessions
run fully in parallel.

```mermaid
sequenceDiagram
  participant User
  participant Sup as Supervisor
  participant Orch as Orchestrator
  participant Lock as session lock
  participant LLM
  participant ToolA
  participant ToolB
  participant ToolC

  User->>Sup: handle_command(sess, text)
  Sup->>Orch: wrapped call
  Orch->>Lock: acquire(sess)
  Lock-->>Orch: granted
  Orch->>LLM: chat(messages, tools)
  LLM-->>Orch: three tool_calls

  par parallel dispatch (FERAL_MAX_PARALLEL_TOOLS=6)
    Orch->>ToolA: execute
    Orch->>ToolB: execute
    Orch->>ToolC: execute
  end

  ToolA-->>Orch: result_a
  ToolB-->>Orch: result_b
  ToolC-->>Orch: result_c

  Note over Orch: rebuild history<br/>in tool_calls order<br/>so LLM sees pairing
  Orch->>LLM: chat(history + tool_results)
  LLM-->>Orch: final text
  Orch->>Lock: release(sess)
  Orch-->>Sup: response
  Sup->>Sup: insert audit row
  Sup-->>User: text
```

Before Stage 1 of this plan, the `for tc in tool_calls: await ...` loop
executed N tools in series — a single turn that needed
`web_search + weather + calendar + memory_query` took ~4x as long as
the slowest tool. Now it completes in `max(tool_i)` wall-clock, bounded
only by `FERAL_MAX_PARALLEL_TOOLS` (default 6). Set it to `1` to
restore strict sequential behaviour for debugging.

## Transcript write-back

`conversation_history[session_id]` holds the **full transcript** for a
session, bounded only by `_conversation_max_per_session` (200 rows).
The window the LLM sees is a *view*: `ContextManager.compact` recomputes
it on every request and it is never stored back. An earlier version
assigned the compacted window over the stored list, which made
truncation permanent and cumulative.

Every turn opens a record in `_begin_turn` and closes it from a
`finally` in `_finalize_turn`, so no early return — refusal fallback,
cost cap, LLM exception, multi-agent hand-off, stream error — can skip
the assistant row. `_send_text` records the prose it emits against the
in-flight turn, so a path that answers without reaching the LLM loop
still records what it said.

Live voice bypasses `handle_command` entirely, so
`note_voice_user_turn` and `note_voice_assistant_turn` write the two
sides of a spoken exchange into that same transcript, under the same
per-session lock. F2 auto-compaction takes the lock too.

The invariant all of this protects: **the model must never receive two
consecutive user messages.** Anthropic's Messages API is stateless and
expects alternating turns; handed `user, user` the model correctly
concludes it never spoke and says so. `ContextManager` coalesces any
consecutive user rows that still reach it, as a last line of defence.

The window is measured in **turns**, not raw rows: the newest
`max_turns` (12) user turns, whole tool round-trips included, shrunk
oldest-first when they exceed the token budget
(`FERAL_CONTEXT_WINDOW_TOKENS`, default 128000). It never starts after
the newest assistant message. The old 15-raw-row window meant one
assistant turn carrying six parallel tool calls consumed seven slots,
so a tool-heavy session retained two turns out of six.

## Spawning subagents

The agent can still spin parallel subagents on demand via the
`subagent__spawn_subagent` tool. This path is older than the per-turn
parallel dispatch and uses its own `asyncio.gather` + `Semaphore` in
[`feral-core/agents/tool_runner.py`](../feral-core/agents/tool_runner.py).

```mermaid
sequenceDiagram
  participant Orch as Orchestrator
  participant TR as ToolRunner
  participant Sem as Semaphore(max_workers)
  participant S1 as subagent-1
  participant S2 as subagent-2
  participant S3 as subagent-3

  Orch->>TR: spawn_subagents(tasks=[t1, t2, t3])
  par gather
    TR->>Sem: acquire
    Sem-->>TR: ok
    TR->>S1: run(t1)
    TR->>Sem: acquire
    Sem-->>TR: ok
    TR->>S2: run(t2)
    TR->>Sem: acquire
    Sem-->>TR: ok
    TR->>S3: run(t3)
  end
  S1-->>TR: result_1
  S2-->>TR: result_2
  S3-->>TR: result_3
  TR-->>Orch: merged results
```

Each subagent shares the parent's skill registry and memory, but
gets a scoped session id and a bounded iteration count so it cannot
loop forever.

## What that looks like in practice

- **Fast multi-tool turn.** "What's my weather, my next meeting, and did
  I have a note about that PR?" → three skills fire in parallel instead
  of sequentially.
- **Safe concurrent channels.** Telegram and Slack DMs arriving a second
  apart for the same user queue on the per-session lock. Different
  users on the same channel fan out fully.
- **Honest cron audit.** Scheduled morning briefings no longer impersonate
  a web-chat turn in the oversight log. Filter
  `/oversight?source=cron` to see every routine that fired.
- **Proactive automations in the log.** Every `set_scene` / breathing
  exercise / notification from `ProactiveEngine._execute_automation`
  lands as `source="proactive"` with `actor="system"`. If the kill
  switch flips, nothing fires.

## Further reading

- [feral-core/agents/orchestrator.py](../feral-core/agents/orchestrator.py) — the main loop. Look for `_handle_command_impl` + the `asyncio.gather` block around line 720.
- [feral-core/agents/supervisor.py](../feral-core/agents/supervisor.py) — `wrap`, `_wrap_call`, `record`. Four entry points listed explicitly.
- [feral-core/agents/tool_runner.py](../feral-core/agents/tool_runner.py) — subagent parallel execution with semaphore.
- [feral-core/api/routes/supervisor.py](../feral-core/api/routes/supervisor.py) — the `/oversight` REST surface.
- [docs/roadmap/oversight.md](roadmap/oversight.md) — what's next for the policy gate + retention + anomaly alerts.
