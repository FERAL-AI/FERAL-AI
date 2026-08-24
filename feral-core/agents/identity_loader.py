"""
Identity and system-prompt construction for the FERAL orchestrator.

Loads agent personality from ~/.feral/ files (IDENTITY.yaml, USER.md,
SOUL.md, MEMORY.md) and assembles the full system prompt injected into
every LLM conversation.
"""

from __future__ import annotations

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


def current_time_context() -> str:
    """Concise, always-present 'now' line for the system prompt.

    The brain previously had no reliable sense of the current local time
    or the user's timezone, so it kept asking and mis-scheduled jobs.
    This injects the host's local wall-clock + IANA timezone into every
    turn's preamble. The timezone is DERIVED from the host (see
    ``config.loader.local_timezone_name``), never hardcoded. Best-effort:
    any failure falls back to UTC rather than blocking the prompt.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from config.loader import local_timezone_name

    try:
        tz_name = local_timezone_name()
        now = datetime.now(ZoneInfo(tz_name))
    except Exception:
        from datetime import timezone as _tz
        tz_name = "UTC"
        now = datetime.now(_tz.utc)

    # e.g. "Wednesday, June 24 2026, 3:15 PM PDT". ``%-I`` (no zero pad)
    # is glibc/BSD-only; fall back to the portable ``%I`` elsewhere.
    try:
        stamp = now.strftime("%A, %B %d %Y, %-I:%M %p %Z").strip()
    except ValueError:
        stamp = now.strftime("%A, %B %d %Y, %I:%M %p %Z").strip()
    offset = now.strftime("%z")
    return (
        "## Current Time\n"
        f"Current local time: {stamp} (timezone {tz_name}, UTC offset {offset}).\n"
        "Use THIS as 'now' for scheduling and relative-time reasoning. When the\n"
        "user gives a clock time without a date (e.g. '3:01 PM', 'at 5pm'),\n"
        "interpret it in this local timezone — do NOT ask the user for their\n"
        "timezone, and do NOT assume UTC. When creating routines/reminders, the\n"
        "scheduler already defaults to this timezone; only pass an explicit\n"
        "tz_name if the user names a different one.\n"
    )


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
        # Optional NodeSubdeviceStore, wired via
        # `Orchestrator.set_subdevice_store` from BrainState. Without it
        # the prompt's only hardware line was
        # `Connected devices: ['feral-iphone-6053b3cdc4ed']`, a bare HUP
        # node id. The peripherals BEHIND that node (the W300 glasses
        # and the VITRO wristband, 7 rows with provenance=ble on the
        # audited install) were rendered on the dashboard and in
        # /api/devices/connected but never reached the model, so
        # "are my glasses connected" had no grounded answer.
        # `memory/node_subdevices.py` names "the orchestrator's prompt
        # context" as a consumer of this store; this attribute is what
        # finally makes that true.
        self.subdevice_store = None
        # Optional zero-arg callable returning the node ids that are
        # holding a HUP WebSocket right now. Wired alongside the store
        # by `Orchestrator.set_subdevice_store`. Without it the prompt
        # can say a peripheral is or is not reporting but cannot say
        # whether the phone carrying it is connected, and "glasses not
        # reporting" reads very differently depending on whether the
        # phone is in the user's hand or switched off.
        self.live_node_ids = None

    @staticmethod
    def _agent_name_from_settings() -> str:
        """Agent name persisted at ``settings.identity.agent_name``, if any.

        Only consulted when no IDENTITY.yaml exists. Best-effort: a
        missing or unreadable settings file yields an empty string and
        the caller keeps the shipped default name.
        """
        try:
            import json

            path = feral_home() / "settings.json"
            if not path.is_file():
                return ""
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                return ""
            name = ((data.get("identity") or {}) or {}).get("agent_name") or ""
            return str(name).strip()
        except Exception:
            return ""

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
            # No IDENTITY.yaml. Installs set up by the modular wizard
            # before v2026.7.31 only ever recorded the operator's chosen
            # agent name at ``settings.identity.agent_name``, which
            # nothing read: naming the agent Jarvis still produced "You
            # are FERAL". The wizard now writes IDENTITY.yaml, but
            # existing installs already have the name only in settings,
            # so honour it here rather than making them re-run setup.
            name = self._agent_name_from_settings() or "FERAL"
            parts.append(
                f"You are {name}, a personal AI operating system.\n"
                "You run locally on the user's devices (phone, laptop, wearables, smart home).\n"
                "You are warm, helpful, and genuinely interested in making the user's life easier.\n"
                "You're privacy-first: everything stays on-device unless the user says otherwise.\n"
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
        plan_mode: bool = False,
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
            plan_mode: True while the session is in plan mode. Adds the
                plan-mode block near the top of the prompt. Callers are
                expected to have already pruned ``skills`` / ``full_catalog``
                to their plan-safe endpoints; this flag only controls the
                prose, it does not filter anything itself.
        """
        identity = identity_text if identity_text is not None else self.load_identity()

        # Plan mode goes FIRST, ahead of the tool-selection block, because
        # that block is an aggressive "if a tool exists, call it" instruction
        # and would otherwise read as a licence to act. This one says the
        # opposite for this turn, so it has to win.
        plan_mode_header = ""
        if plan_mode:
            try:
                from agents.plan_mode import plan_mode_prompt_block
                plan_mode_header = plan_mode_prompt_block() + "\n"
            except Exception:
                logger.debug("plan-mode prompt block unavailable", exc_info=True)

        # The static header is structured so the most-violated disciplines
        # (tool-selection, grounded recall, honest execution) land BEFORE
        # any dynamic context. Anthropic-class models honour authority
        # signals at the top of the prompt; budget-priced models honour
        # the last instruction. Putting these blocks first AND echoing the
        # critical ones near the end (Execution Bias) covers both shapes.
        prompt = plan_mode_header + (
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
            "   **Robot/CuteBot/QtBot LED lights** (headlight/underglow color, off, red,\n"
            "   green, blue) → call `cutebot__set_lights`. NEVER use `smart_home_hue` for\n"
            "   the desk robot — Hue/HA is for Philips bridge and Home Assistant entities\n"
            "   only.\n"
            "4. **Sending messages** (Telegram/Slack/Discord/WhatsApp) → call\n"
            "   `messaging_channels__send`. Resolve handles via\n"
            "   `messaging_channels__resolve_chat_id` first if only an @handle is given.\n"
            "5. **Calendar / reminders / health** → call the matching tool, even if a\n"
            "   today's-events preview block is present in this prompt — that block\n"
            "   is a hint, not the authoritative answer for 'do I have anything\n"
            "   Friday' type questions.\n"
            "6. **Scheduled / recurring actions** ('every day at 5pm', 'every night\n"
            "   at 9', 'every weekday', 'daily', 'every 30 minutes', or a clock time\n"
            "   like 'at 3:01pm' paired with an action) → call\n"
            "   `feral_routines__create`. There is NO 'background task' alternative\n"
            "   and no separate automation engine to defer to. `feral_routines` IS\n"
            "   FERAL's scheduling primitive: it persists a real cron job that fires\n"
            "   at the requested time and dispatches the chosen action (skill\n"
            "   endpoint, free-text prompt, or workflow). NEVER claim FERAL has no\n"
            "   built-in automation feature and NEVER invent a 'workaround\n"
            "   background task' — that is a hallucination, not honesty. If the user\n"
            "   already said the schedule on an earlier turn and the current turn\n"
            "   only fills in the action ('just make it spin'), combine them and\n"
            "   call `feral_routines__create` immediately — do not ask the schedule\n"
            "   question again. For a device action without a confirmer at fire\n"
            "   time, pass `auto_confirm: true`.\n"
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
            "## Ambient Conversation Capture\n"
            "The operator's glasses record real conversations. The phone transcribes\n"
            "them on device and syncs them to you, where they are summarized into\n"
            "`ambient_conversation` episodes carrying the summary, who was present, the\n"
            "topics, and any commitment the operator made OUT LOUD. This is a shipped\n"
            "feature that is running right now, not a roadmap item, and not something\n"
            "you need to be told is enabled.\n"
            "You do NOT know from introspection whether anything has been recorded.\n"
            "That fact lives in a store you can only reach with a tool. So when asked\n"
            "'is there any conversation recorded', 'what did I discuss with X', 'what\n"
            "did I say I'd do', or anything else about spoken conversations:\n"
            "LOOK, don't introspect. Call `notes_memory__list_conversations`,\n"
            "`notes_memory__search_conversations`, or\n"
            "`notes_memory__conversation_commitments` FIRST, then answer from what came\n"
            "back. `total: 0` is the only thing that entitles you to say nothing was\n"
            "recorded. A conversation with `status: 'pending'` HAS been recorded and is\n"
            "still being summarized; saying 'no conversations' about it is wrong.\n"
            "Never answer 'I don't record audio' or 'I only hear what's typed here'.\n"
            "That is a false claim about your own capability, and it is the specific\n"
            "failure this block exists to stop: asked whether any conversation was\n"
            "recorded, an earlier build answered 'I don't have any ambient audio\n"
            "recording active' while holding four transcripts with commitments already\n"
            "extracted from one of them. It had not searched. It had guessed about\n"
            "itself.\n"
            "\n"
            "## Execution & Honesty\n"
            "- ACT when the user asks you to act, in the same turn, by calling a real\n"
            "  tool. Never describe steps for the user to perform themselves when a tool\n"
            "  exists for it. Never say 'I can't' or 'I'm unable to' when a tool exists.\n"
            "- DON'T fake readiness. If a required setup step is missing, state the\n"
            "  exact blocker and the setup step needed — don't pretend the action ran.\n"
            "- Local files → use `coding_tools__write_file`, `coding_tools__read_file`,\n"
            "  `coding_tools__edit_file`. Don't write files via shell `echo`, heredocs,\n"
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

        # Always-present "now" so the brain never asks the user for their
        # timezone and never mis-schedules against UTC.
        try:
            prompt += f"\n{current_time_context()}"
        except Exception:
            logger.debug("current_time_context failed", exc_info=True)

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
            "- **coding_tools__write_file / read_file / edit_file**: create, inspect,\n"
            "  and update local files through the filesystem policy.\n"
            "- **coding_tools__bash**: open files, run commands, verify results. It\n"
            "  runs on the host inside a granted folder; outside every grant it\n"
            "  returns `permission_needed` instead of executing.\n"
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

        # Connected hardware — the peripherals behind those nodes.
        # `frame.connected_nodes` above is a list of HUP node ids and
        # stops there; the BLE/cloud/host sub-devices each node owns
        # live in NodeSubdeviceStore and had no path into the prompt.
        hardware_section = self._build_connected_hardware_section()
        if hardware_section:
            prompt += f"\n{hardware_section}\n"

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
            "- For scheduled / recurring device actions ('every day at 5pm', 'nightly at 9pm',\n"
            "  'every night', or a clock time paired with an action), CALL `feral_routines__create`\n"
            "  immediately — there is no 'background task' workaround. For scheduled device motion\n"
            "  without a human confirmer at fire time, pass `auto_confirm: true`.\n"
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

    def _build_connected_hardware_section(self) -> str:
        """Render `## Connected Hardware` from the sub-device truth store.

        Returns "" when no store is wired (a brain built without one,
        and every existing test double), so this can never invent a
        block out of nothing.

        Three states, each stated explicitly, because collapsing any two
        of them is how a user gets lied to:

        * rows present and inside their heartbeat window -> "connected,
          reporting now".
        * rows present but past the window -> "not reporting". Hiding a
          stale row would make "my glasses just dropped" read
          identically to "you have never owned glasses".
        * store present, zero rows -> "No peripherals have reported".
          Emitting nothing here reads to the model as absence of
          information rather than information about absence, and the
          model then guesses.

        A store that raises is reported in the prompt as unavailable AND
        logged at warning. Swallowing it silently would rebuild the
        original defect: a prompt that looks complete while carrying no
        hardware truth at all.

        Structure. The rows are grouped by the node that owns them and
        by physical peripheral before rendering, because the flat list
        this used to emit was actively misleading on the audited
        install: 7 rows, of which 6 were the SAME pair of glasses seen
        through 6 install-scoped `feral-iphone-*` node ids. The model
        read six pairs of glasses and six phones. Glasses and the
        wristband reach the brain THROUGH the phone, so they are
        indented under it; a peripheral that is nobody's child is a
        claim this brain cannot support.
        """
        store = self.subdevice_store
        if store is None:
            return ""

        try:
            rows = list(store.list_all())
        except Exception as exc:
            logger.warning(
                "node_subdevices.list_all failed; the prompt cannot describe "
                "connected hardware this turn: %s", exc, exc_info=True,
            )
            return (
                "## Connected Hardware\n"
                "Hardware status unavailable this turn (the sub-device store "
                "could not be read). Do not claim anything is or is not "
                "connected; call a device tool or tell the user the status "
                "could not be read.\n"
            )

        if not rows:
            return (
                "## Connected Hardware\n"
                "No peripherals have reported to this brain. Any HUP nodes "
                "listed above are connected, but nothing is paired behind "
                "them.\n"
            )

        from api.device_view import build_device_view

        # getattr, not attribute access: existing tests build this class
        # with `IdentityLoader.__new__` and set only `subdevice_store`,
        # so a hard attribute read would turn a missing optional into an
        # AttributeError that blanks the whole hardware block.
        provider = getattr(self, "live_node_ids", None)
        live_ids: list[str] = []
        if callable(provider):
            try:
                live_ids = [str(n) for n in (provider() or [])]
            except Exception as exc:
                # Degrade to "no node is confirmed live" rather than
                # guessing. Claiming a phone is connected on the
                # strength of a failed lookup is the exact defect being
                # fixed here.
                logger.warning(
                    "live node lookup failed; the prompt will describe every "
                    "node as not reporting: %s", exc, exc_info=True,
                )
        view = build_device_view(
            live_nodes=[{"node_id": nid} for nid in live_ids],
            subdevice_rows=rows,
        )

        lines = ["## Connected Hardware"]
        # Live nodes first: the model weights early lines more heavily,
        # and a long tail of months-old installs would otherwise bury
        # the phone the user is actually holding.
        for node in view["devices"] + view["offline"]:
            lines.append(self._hardware_node_line(node))
            for sub in node["subdevices"]:
                lines.append(self._hardware_peripheral_line(sub))

        lines.append(
            "Peripherals are indented under the node they reach the brain "
            "through. The glasses and wristband speak BLE to the phone, not "
            "to this brain. Report these verbatim when asked what is "
            "connected. Anything marked \"not reporting\" or \"disconnected\" "
            "is paired but silent. Say that, do not call it connected and do "
            "not omit it. The brain cannot reconnect a node itself; only the "
            "device can start that."
        )
        return "\n".join(lines) + "\n"

    @staticmethod
    def _age_text(age_s) -> str:
        """Human age for a last-seen stamp, or "" when there is none."""
        if not isinstance(age_s, (int, float)) or age_s <= 0:
            return ""
        if age_s < 90:
            return f"{int(age_s)} s ago"
        if age_s < 5400:
            return f"{int(age_s / 60)} min ago"
        if age_s < 172800:
            return f"{int(age_s / 3600)} h ago"
        return f"{int(age_s / 86400)} days ago"

    @classmethod
    def _hardware_node_line(cls, node: dict) -> str:
        node_id = str(node.get("node_id") or "unknown")
        node_type = str(node.get("type") or "node")
        if node.get("connected"):
            state_text = "connected, reporting now"
        else:
            age = cls._age_text(node.get("last_seen_age_s"))
            state_text = "disconnected" + (f", last seen {age}" if age else "")
        line = f"- {node_type} {node_id}: {state_text}"
        # Earlier installs of the same physical device. Named rather
        # than hidden so the model can answer "why do I see this id in
        # my old logs" without the user having to ask twice.
        others = node.get("also_known_as") or []
        if others:
            line += (
                f"; same physical device as {len(others)} earlier install"
                f"{'s' if len(others) != 1 else ''} ({', '.join(others)})"
            )
        return line

    @classmethod
    def _hardware_peripheral_line(cls, sub: dict) -> str:
        capability = str(sub.get("capability") or "unknown")
        detail = capability
        label = str(sub.get("name") or "").strip()
        if label:
            detail += f" ({label})"
        if sub.get("live"):
            state_text = "connected, reporting now"
        else:
            age = cls._age_text(sub.get("last_seen_age_s"))
            state_text = "not reporting (outside its heartbeat window)"
            if age:
                state_text += f", last seen {age}"
        extras = []
        status = str(sub.get("status") or "").strip()
        if status:
            extras.append(f"status={status}")
        attrs = sub.get("attrs") if isinstance(sub.get("attrs"), dict) else {}
        battery = attrs.get("battery_pct", attrs.get("battery_level"))
        if isinstance(battery, (int, float)):
            extras.append(f"battery={int(battery)}%")
        line = f"  - {detail}: {state_text}"
        if extras:
            line += "; " + ", ".join(extras)
        return line

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
