"""
Identity and system-prompt construction for the FERAL orchestrator.

Loads agent personality from ~/.feral/ files (IDENTITY.yaml, USER.md,
SOUL.md, MEMORY.md) and assembles the full system prompt injected into
every LLM conversation.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import TYPE_CHECKING

from config.loader import feral_home

if TYPE_CHECKING:
    from memory.store import MemoryStore
    from models.skill_manifest import SkillManifest
    from perception.fusion import PerceptionFrame
    from perception.somatic import SomaticEngine

logger = logging.getLogger("feral.orchestrator.identity")

# Bounded in-memory ring of every `## Memory` block we assembled during a
# session. Small (20 entries) so the /api/memory/context endpoint can prove
# to the user that multi-memory really does fire per turn. We keep this on
# the class (not a global) but flatten it to a module-level ring for quick
# cross-session retrieval by the inspector.
_SNAPSHOT_RING_MAX = 50
_memory_snapshots: deque[dict] = deque(maxlen=_SNAPSHOT_RING_MAX)


def record_memory_snapshot(entry: dict) -> None:
    """Append a rendered memory-context snapshot to the inspector ring."""
    _memory_snapshots.append(entry)


def recent_memory_snapshots(limit: int = 20) -> list[dict]:
    """Return the latest memory-context snapshots, newest first."""
    ordered = list(_memory_snapshots)
    ordered.reverse()
    return ordered[:limit]


def clear_memory_snapshots() -> None:
    """Drop every cached snapshot — used by tests."""
    _memory_snapshots.clear()


class IdentityLoader:
    """Loads agent identity files and builds the LLM system prompt."""

    def __init__(
        self,
        memory: "MemoryStore | None" = None,
        somatic_engine: "SomaticEngine | None" = None,
        calendar=None,
    ):
        self.memory = memory
        # Audit-r9 fix: optional calendar handle (wired via
        # `Orchestrator.set_calendar`) so the system prompt can carry
        # an authoritative "## Today's Events" block. Without this, the
        # LLM only sees calendar data when the routing layer happens
        # to add `calendar_google` to the active skills set — which is
        # how iOS chat ended up "having no idea" about events the
        # operator created on the web tab.
        self.calendar = calendar
        self.somatic_engine: SomaticEngine | None = somatic_engine

    def load_identity(self) -> str:
        """Load agent identity from ~/.feral/ files: IDENTITY.yaml, USER.md, SOUL.md, MEMORY.md."""
        home = feral_home()
        parts: list[str] = []

        # 1. IDENTITY.yaml — agent name, personality, rules
        for p in (home / "identity.yaml", home / "identity.yml", home / "IDENTITY.yaml"):
            if p.exists():
                try:
                    import yaml
                    with open(p) as f:
                        data = yaml.safe_load(f) or {}
                    name = data.get("name", "FERAL")
                    tagline = data.get("tagline", "")
                    personality = data.get("personality", "")
                    rules = data.get("rules", [])
                    greeting_style = data.get("greeting_style", "")

                    parts.append(f"You are {name}.")
                    if tagline:
                        parts.append(tagline)
                    if personality:
                        parts.append(f"\n## Personality\n{personality}")
                    if rules:
                        parts.append("\n## Rules\n" + "\n".join(f"- {r}" for r in rules))
                    if greeting_style:
                        parts.append(f"\n## Communication Style\n{greeting_style}")
                    break
                except Exception as e:
                    logger.warning(f"Failed to load identity: {e}")

        if not parts:
            parts.append(
                "You are FERAL, a personal AI operating system.\n"
                "You run locally on the user's devices — phone, laptop, wearables, smart home.\n"
                "You are warm, helpful, and genuinely interested in making the user's life easier.\n"
                "You're privacy-first — everything stays on-device unless the user says otherwise.\n"
                "You learn the user's preferences over time and get better at anticipating their needs.\n"
                "You have personality: you can be witty, ask thoughtful questions, and suggest creative ideas.\n"
                "When given a task, you think about related things the user might want and offer them proactively."
            )

        # 2. USER.md — who the user is
        user_md = home / "USER.md"
        if user_md.exists():
            try:
                content = user_md.read_text().strip()
                if content and content != "# About Me\n\nTell your agent about yourself here.":
                    parts.append(f"\n## About the User\n{content}")
            except Exception:
                pass

        # 3. SOUL.md — deeper personality / behavioral notes
        soul_md = home / "SOUL.md"
        soul_loaded = False
        if soul_md.exists():
            try:
                content = soul_md.read_text().strip()
                if content:
                    parts.append(f"\n## Soul\n{content}")
                    soul_loaded = True
            except Exception:
                pass
        if not soul_loaded:
            parts.append(
                "\n## Default Personality\n"
                "- Be warm and conversational — you're a companion, not a command line.\n"
                "- When multiple approaches exist, ask the user which they prefer.\n"
                "- Proactively suggest related actions after completing a task.\n"
                "- Encourage the user to explore: custom skills, workflows, automations.\n"
                "- If the user seems stuck, offer concrete ideas rather than waiting.\n"
                "- Never flatly refuse — say what you CAN do and offer the closest alternative."
            )

        # 4. MEMORY.md — persistent long-term knowledge the user has given
        memory_md = home / "MEMORY.md"
        if memory_md.exists():
            try:
                content = memory_md.read_text().strip()
                if content:
                    parts.append(f"\n## Long-Term Memory\n{content}")
            except Exception:
                pass

        # 5. AboutMeStore — structured self-model from chat/baseline/user.
        # Injected after IDENTITY/USER/SOUL/MEMORY so free-form prose stays
        # dominant; structured facts act as sharp disambiguators.
        try:
            from api.state import state as _state
            store = getattr(_state, "about_me", None)
            if store is not None:
                chunk = store.system_prompt_chunk()
                if chunk:
                    parts.append(f"\n{chunk}")
        except Exception as exc:
            logger.debug("AboutMeStore unavailable in identity_loader: %s", exc)

        return "\n".join(parts)

    async def build_system_prompt(
        self,
        frame: "PerceptionFrame",
        skills: list["SkillManifest"],
        session_id: str = "",
        identity_text: str | None = None,
        full_catalog: list["SkillManifest"] | None = None,
        memory_filter: str = "",
        query: str = "",
    ) -> str:
        """Assemble the full system prompt for an LLM conversation turn.

        Args:
            identity_text: Pre-loaded identity string.  When *None* the loader
                calls :meth:`load_identity` itself.  The orchestrator passes
                the result of its own ``_load_identity()`` so that test patches
                on the orchestrator are honoured.
            full_catalog: Every registered skill, used to emit the "Available
                (full catalog)" block so the model never claims a skill does
                not exist. When None, only the active list is shown.
            query: The user's current utterance. Threaded into the memory
                context builder so knowledge-graph + episode search fire per
                turn. Empty string = legacy behaviour (working memory + recent
                episodes only).
        """
        identity = identity_text if identity_text is not None else self.load_identity()

        # The static header is structured so the most-violated disciplines
        # (tool-selection, grounded recall, honest execution) land BEFORE
        # any dynamic context. Anthropic-class models honour authority
        # signals at the top of the prompt; budget-priced models honour
        # the last instruction. Putting these blocks first AND echoing the
        # critical ones near the end (Execution Bias) covers both shapes.
        prompt = (
            "## Tool-Selection Discipline\n"
            "Tools are FERAL's senses and hands. Calling the right tool beats parametric\n"
            "guessing every time. Before answering, ask: is there a tool that would\n"
            "produce the ground truth here? If yes, CALL IT. The user's question is the\n"
            "specification; the tool is the source of truth.\n"
            "\n"
            "Hard rules — do not violate:\n"
            "1. **Personal recall, history, 'what did I…'** → call `notes_memory__fused_timeline`\n"
            "   FIRST, then synthesise from its result. Examples that REQUIRE the call:\n"
            "   'what did I do yesterday', 'summarize my morning', 'last Tuesday',\n"
            "   'what was I working on this week', 'recap my day', 'what happened today',\n"
            "   'earlier today', 'draft my standup', 'what did we discuss'.\n"
            "   The `## Memory` block below is a lossy working-set hint, NOT the full\n"
            "   window — it omits dates, durations, and most episodes. Answering from it\n"
            "   alone produces hallucinated specifics. The fused timeline merges episodes,\n"
            "   notes, knowledge, calendar, and health into one ordered card.\n"
            "2. **Current external info** ('latest', 'today's news', 'price of', 'who won',\n"
            "   anything time-sensitive) → call `web_search`. Do not answer from training\n"
            "   data for time-sensitive questions; you don't know what is current.\n"
            "3. **Acting on the local machine** (open app, write file, run command, click,\n"
            "   browse, control home device) → call the matching local tool listed below.\n"
            "   Never describe what the user should do themselves when a tool exists.\n"
            "4. **Sending messages** (Telegram/Slack/Discord/WhatsApp) → call\n"
            "   `messaging_channels__send`. Resolve handles via\n"
            "   `messaging_channels__resolve_chat_id` first if only an @handle is given.\n"
            "5. **Calendar / reminders / health** → call the matching tool, even if a\n"
            "   today's-events preview block is present in this prompt — that block\n"
            "   is a hint, not the authoritative answer for 'do I have anything\n"
            "   Friday' type questions.\n"
            "\n"
            "When a tool returns nothing or errors, SAY SO honestly with the specific\n"
            "blocker — never paper over it with parametric guessing.\n"
            "\n"
            "## Grounded Memory Synthesis\n"
            "When you DO call a memory or timeline tool, ground every claim in its\n"
            "result. Cite specifics — the actual project, the actual time, the actual\n"
            "person — drawn from returned entries. If the result is empty, reply with\n"
            "'no entries in <window>' and offer to widen the window or check a related\n"
            "source. Do NOT say 'I don't have access to that' or 'I can't see your\n"
            "history' when the data IS available — that's a lie about your capability.\n"
            "If the user asks for a window the tool doesn't cover, say which window IS\n"
            "covered and offer the closest one.\n"
            "\n"
            "## Execution & Honesty\n"
            "- ACT when the user asks you to act, in the same turn, by calling a real\n"
            "  tool. Never describe steps for the user to perform themselves when a tool\n"
            "  exists for it. Never say 'I can't' or 'I'm unable to' when a tool exists.\n"
            "- DON'T fake readiness. If a required setup step is missing, state the\n"
            "  exact blocker and the setup step needed — don't pretend the action ran.\n"
            "- Local files → use `computer_use__write_file`, `computer_use__read_file`,\n"
            "  `computer_use__edit_file`. Don't write files via shell `echo`, heredocs,\n"
            "  or python one-liners when the file tool can do it directly.\n"
            "- After a destructive or substantive action, verify with a follow-up tool\n"
            "  call (read the file back, list devices, etc.) when verification is cheap.\n"
            "- If a file tool returns `permission_needed`, tell the user the folder\n"
            "  requires access and stop retrying until the grant succeeds.\n"
            "- If you truly lack a specialised skill and the task is safe to extend,\n"
            "  call `system_settings__create_skill` to generate one.\n"
            "- Local-first and sovereign: everything runs on the user's hardware unless\n"
            "  they routed a request to a remote provider. Don't overclaim ('I synced\n"
            "  to the cloud') and don't underclaim ('I can't help with that') — be\n"
            "  literal about what you did and didn't do.\n"
            "\n"
            "## Agentic Planning\n"
            "For multi-step requests ('research X, save the top 3 to a note, summarise'\n"
            "or 'draft my standup from yesterday's work'), decompose BEFORE acting:\n"
            "1. Name the steps internally (1-3 lines is enough).\n"
            "2. Call the FIRST tool. Do not narrate the plan instead of executing.\n"
            "3. After each tool result, decide: continue, branch, or finish. If a step\n"
            "   fails, surface the specific error and try the next-best tool — don't\n"
            "   loop on the same failing call.\n"
            "4. End with a tight synthesis grounded in tool outputs, not the plan.\n"
            "Do not produce a plan-only answer when the user asked for the result;\n"
            "produce the result.\n"
        )

        if identity:
            prompt += f"\n## Identity\n{identity}\n"

        prompt += (
            "\n## Tone & Companionship\n"
            "- Warm, conversational, occasionally playful — you're a personal AI\n"
            "  companion, not a sterile chatbot.\n"
            "- When multiple approaches exist and the choice is consequential, ask the\n"
            "  user which they prefer; otherwise pick the obviously-best one and act.\n"
            "- Proactively suggest one related action after completing a task\n"
            "  (\"Done — want me to also pin this to your wiki?\"). One suggestion, not five.\n"
            "- If the user seems stuck, offer concrete options rather than waiting.\n"
            "- Answer questions directly in plain language. No JSON dumps, no raw UI\n"
            "  markup, no five-paragraph preambles before the answer.\n"
            "\n## Local Computer & Browser Control\n"
            "Prefer deterministic tools before GUI fallback:\n"
            "- **computer_use__write_file / read_file / edit_file** — create, inspect,\n"
            "  and update local files through the filesystem policy.\n"
            "- **computer_use__bash** — open files, run commands, verify results once\n"
            "  filesystem grants and sandbox policy allow it.\n"
            "- **desktop_control__open_app** — launch or focus an app via AppleScript.\n"
            "- **desktop_automation__click_screen / type_text / key_combo / scroll /\n"
            "  get_cursor_position** — low-level GUI primitives, only when needed.\n"
            "- **browser__navigate / click / type_text / screenshot / evaluate** —\n"
            "  drive an in-process browser for web tasks.\n"
            "- **notes_memory** — FERAL's internal memory store (NOT filesystem files).\n"
            "  Use `notes_memory__fused_timeline` for temporal recall (see Tool-Selection\n"
            "  Discipline above); use `notes_memory__search` for keyword/topic recall;\n"
            "  use `notes_memory__save` to persist a fact the user told you to remember.\n"
            "- **system_settings__read/update_user_profile / read/update_agent_personality\n"
            "  / read_settings / update_setting** — change identity, persona, or config.\n"
            "- **system_settings__create_skill** — generate + register a NEW skill on the\n"
            "  fly when the user asks you to 'learn' something or do something no\n"
            "  current skill covers.\n"
            "- **agentic_computer_use__execute_task** — autonomous vision-action loop for\n"
            "  multi-step GUI workflows (forms, navigation, sequences). For single\n"
            "  simple actions, prefer `desktop_control` / `desktop_automation`.\n"
        )

        # Perception Context
        perception_context = frame.to_system_context()
        if perception_context and perception_context != "No sensor data available.":
            prompt += f"\n## Live Perception\n{perception_context}\n"

        # Audit-r9 fix — Today's Events / Reminders preload.
        #
        # Without this block, the LLM had no automatic awareness of
        # calendar items or reminders. It only learned about them when
        # the routing layer happened to add `calendar_google` /
        # `feral_reminders` to the active skill set AND the model
        # decided to call a lookup tool. That made cross-surface
        # awareness fragile: an event created in the web chat (which
        # mints a fresh `uuid4()` session) was completely invisible
        # to the iOS chat (which uses `phone-{node_id}`) because
        # neither working memory nor the system prompt carried it.
        #
        # We now read the next ~5 upcoming items synchronously at
        # prompt-build time. Best-effort — every failure path is
        # swallowed so a calendar OAuth glitch can never block chat.
        events_section = self._build_events_section()
        if events_section:
            prompt += f"\n{events_section}\n"

        # Memory Context — a specialist-scoped memory_filter narrows the
        # surfaced episodes + recent actions so cross-domain leakage
        # (journaling thoughts bleeding into a coding turn, etc.) stops.
        #
        # We prefer the async builder so the knowledge graph `build_graph_context`
        # path fires on every turn the user asked a real question. If no event
        # loop is running (e.g. a sync caller or test), we fall back to the
        # sync builder. Either way, the user's query is threaded through so
        # `context_builder` actually searches KG + episodes instead of quietly
        # guarding both behind `if query:`.
        memory_context = ""
        if self.memory and session_id:
            started = time.monotonic()
            memory_context = await self._build_memory_context(
                session_id=session_id,
                query=query or "",
                memory_filter=memory_filter or "",
            )
            if memory_context:
                prompt += f"\n## Memory\n{memory_context}\n"

            record_memory_snapshot({
                "session_id": session_id,
                "query": (query or "")[:240],
                "memory_filter": memory_filter or "",
                "memory_context": memory_context,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "ts": time.time(),
            })

        # Prose Tooling catalog (active + full). Replaces the terse
        # "Relevant skills: ..." line with a detailed enumeration so
        # the LLM can see which tools are live AND which exist at all.
        try:
            from agents.self_model import build_tooling_catalog, build_ui_route_map, build_runtime_line
            tooling_block = build_tooling_catalog(
                active=skills or [],
                full=full_catalog or skills or [],
            )
            if tooling_block:
                prompt += f"\n{tooling_block}\n"
            prompt += f"\n{build_ui_route_map()}\n"
            prompt_runtime_line = build_runtime_line(frame)
        except Exception as exc:
            logger.debug("self_model unavailable in identity_loader: %s", exc)
            if skills:
                prompt += "\nRelevant skills: " + ", ".join(s.brand.name for s in skills) + "\n"
            prompt_runtime_line = None

        # Connected nodes
        if frame.connected_nodes:
            prompt += f"\nConnected devices: {frame.connected_nodes}\n"

        # Somatic context — body-state adaptive behaviour
        if self.somatic_engine and session_id:
            somatic_section = self.somatic_engine.build_system_prompt_section(session_id)
            if somatic_section:
                prompt += f"\n{somatic_section}\n"

        # Live messaging-channel awareness + execution bias.
        prompt += self._messaging_channels_section()

        prompt += (
            "\n## Execution Bias (final reminder)\n"
            "- If the user asks you to DO work, DO it in the same turn by calling a real tool.\n"
            "- For personal-recall / 'what did I…' questions, CALL `notes_memory__fused_timeline`\n"
            "  before drafting an answer. The `## Memory` block above is a hint, not the source\n"
            "  of truth. Answering from it alone fabricates specifics.\n"
            "- NEVER describe what the user should do themselves when a tool for it exists.\n"
            "- NEVER say 'I can't', 'I'm unable to', or 'I don't have access' when a tool exists\n"
            "  for that action. Honest unavailability sounds like 'no <X> tool is wired right\n"
            "  now — to fix this, <specific setup step>'.\n"
            "- For messaging, `messaging_channels__send` IS your direct line to Telegram, Slack,\n"
            "  Discord, and WhatsApp. Call it — never tell the user to open the app or paste into the API.\n"
            "- Never use shell/curl to send messages on those channels; `messaging_channels__send` handles routing.\n"
            "- If a tool call fails, report the SPECIFIC error and try again or pick the next best tool.\n"
            "  Don't loop on the same failing call — branch.\n"
            "- After a tool returns, ground your reply in its actual output — cite specifics, not\n"
            "  general impressions. One short confirmation sentence is enough for simple actions.\n"
        )

        # Runtime line — last so models biased to "recent context" see it.
        if prompt_runtime_line:
            prompt += f"\n{prompt_runtime_line}\n"

        return prompt

    async def _build_memory_context(
        self,
        session_id: str,
        query: str,
        memory_filter: str,
    ) -> str:
        """Assemble `## Memory` content using the async KG-aware builder.

        Since v2026.5.33 MemoryStore is async-native; this coroutine
        awaits the builder directly without the prior asyncio.run /
        sync-bridge dance.
        """
        if not self.memory:
            return ""

        async_builder = getattr(self.memory, "build_context_for_llm_async", None)
        if async_builder is None:
            async_builder = getattr(self.memory, "build_context_for_llm", None)
        if async_builder is None:
            return ""

        try:
            return await async_builder(
                session_id,
                query=query,
                max_tokens_budget=800,
                memory_filter=memory_filter,
            )
        except Exception as exc:
            logger.debug("Memory context builder failed: %s", exc)
            return ""

    def _build_events_section(self) -> str:
        """Render `## Today's Events` + `## Reminders` blocks.

        Pulls from:
        * `self.calendar` — wired by `Orchestrator.set_calendar` via
          `BrainState.calendar`. Same `CalendarIntegration` instance
          the proactive engine uses.
        * `~/.feral/data/reminders.json` — first-party FERAL reminders
          skill store. Read directly so we don't have to round-trip
          through the skill registry on every prompt build.

        Returns "" when there's no data to show. Never raises — a
        calendar / file glitch must not block chat.
        """
        sections: list[str] = []

        # Calendar (Google Calendar via CalendarIntegration).
        if self.calendar is not None:
            try:
                exec_ = getattr(self.calendar, "execute", None)
                if callable(exec_):
                    raw = None
                    # `CalendarIntegration.execute` is a coroutine in
                    # the live brain; tolerate both sync stubs (tests)
                    # and async callers via `asyncio.get_event_loop`
                    # detection. From the prompt-build context we are
                    # synchronous, so we run the coroutine to
                    # completion ONLY if there is no current loop;
                    # otherwise we fall back to the cached "next event"
                    # if the integration exposes one.
                    import asyncio as _aio
                    import inspect as _inspect
                    result = exec_("list_events", {"days_ahead": 1})
                    if _inspect.iscoroutine(result):
                        try:
                            _aio.get_running_loop()
                            # We are inside an async caller — the
                            # synchronous prompt builder cannot await
                            # here. Drop the coroutine and prefer the
                            # cached next-event below.
                            result.close()
                            cached = getattr(self.calendar, "_cached_next_event", None)
                            if isinstance(cached, dict):
                                raw = {"data": {"events": [cached]}}
                        except RuntimeError:
                            raw = _aio.run(result)
                    else:
                        raw = result

                    events: list[dict] = []
                    if isinstance(raw, dict):
                        # CalendarIntegration returns
                        # `{"success": True, "data": {"events": [...]}}`.
                        # Tolerate older shapes (`{"events": [...]}`)
                        # too — same defensive read the timeline route
                        # should be doing.
                        data = raw.get("data") or {}
                        events = data.get("events") if isinstance(data, dict) else None
                        if not events:
                            events = raw.get("events") or []
                    if events:
                        sections.append("## Today's Events")
                        for ev in list(events)[:5]:
                            title = ev.get("title") or ev.get("summary") or "(untitled)"
                            start = ev.get("start") or ev.get("when") or ""
                            location = ev.get("location") or ""
                            line = f"- {title}"
                            if start:
                                line += f" — {start}"
                            if location:
                                line += f" @ {location}"
                            sections.append(line)
            except Exception as exc:
                logger.debug("identity_loader calendar block skipped: %s", exc)

        # FERAL Reminders (first-party reminders.json).
        try:
            import json as _json
            from pathlib import Path as _Path
            try:
                from config.loader import feral_home as _feral_home
                home = _feral_home()
            except Exception:
                home = _Path.home() / ".feral"
            reminders_path = home / "data" / "reminders.json"
            if reminders_path.is_file():
                raw = _json.loads(reminders_path.read_text())
                items = raw if isinstance(raw, list) else raw.get("reminders", [])
                if items:
                    sections.append("\n## Reminders")
                    for r in list(items)[:5]:
                        if not isinstance(r, dict):
                            continue
                        title = r.get("title") or r.get("text") or "(reminder)"
                        when = r.get("when") or r.get("due") or ""
                        line = f"- {title}"
                        if when:
                            line += f" — {when}"
                        sections.append(line)
        except Exception as exc:
            logger.debug("identity_loader reminders block skipped: %s", exc)

        return "\n".join(sections)

    def _messaging_channels_section(self) -> str:
        """Inject the live list of configured messaging channels and how to address them.

        Builds the tool-discovery block whose description PROVES the agent can
        send, so it cannot truthfully say 'I can't'.
        """
        try:
            from api.state import state as _state
            cm = getattr(_state, "channel_manager", None)
            if not cm:
                return ""
            rows = []
            for ctype, ch in cm.channels.items():
                label = ctype
                bot = getattr(ch, "_bot_username", None)
                if bot:
                    label = f"{ctype} (@{bot})"
                rows.append(label)
            if not rows:
                return (
                    "\n## Messaging Channels\n"
                    "No messaging channels are currently connected. If the user asks you to send a\n"
                    "message on Telegram/Slack/Discord/WhatsApp, call `messaging_channels__list_channels`\n"
                    "to confirm, then tell them to add credentials in Settings → Channels or re-run\n"
                    "`feral setup`.\n"
                )
            channel_list = ", ".join(rows)
            return (
                "\n## Messaging Channels (live)\n"
                f"Configured and running: {channel_list}.\n"
                "To send a message, call `messaging_channels__send` with:\n"
                "  channel=<telegram|slack|discord|whatsapp>, to=<chat_id or @handle>, text=<content>.\n"
                "If the user only gave an @handle on Telegram, call `messaging_channels__resolve_chat_id`\n"
                "first. Only then call send. Do NOT say you can't — these channels are ready.\n"
            )
        except Exception:
            return ""
