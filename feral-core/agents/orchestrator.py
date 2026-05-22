"""
FERAL Orchestrator — The Agentic Brain (v0.4.1)
==================================================
The core OS loop. Receives fused multimodal perception →
matches skills → calls LLM with tools → executes → generates UI →
logs execution → updates memory → responds with voice + visuals.

v0.4.1:
  - Split into focused modules: ToolRunner, ContextManager,
    RefusalHandler, IdentityLoader.  Orchestrator delegates to each.
v0.4.0:
  - Self-learning agent (knowledge extraction, session summarization)
  - Execution-log-aware skill routing with penalty scores
  - Streaming LLM responses (token-by-token text + SDUI patches)
  - Gesture-aware context injection
"""

from __future__ import annotations
import asyncio
import json
import logging
import os
import re
import time
from typing import Any, Optional, Callable, Awaitable, TYPE_CHECKING
from uuid import uuid4

from fastapi import WebSocket

from models.protocol import (
    FeralMessage,
    SDUIPayload,
    ToolResultPayload,
    ToolStartPayload,
    VisionRequestPayload,
)
from models.skill_manifest import SkillManifest
from skills.registry import SkillRegistry
from skills.executor import SkillExecutor
from agents.llm_provider import LLMProvider
from agents.genui_generator import GenUIGenerator
from perception.fusion import PerceptionEngine, PerceptionFrame

# Sub-modules — orchestrator delegates to these focused classes
from agents.tool_runner import ToolRunner
from security.dangerous_tools import resolve_surface_from_context
from agents.context_manager import ContextManager
from agents.refusal_handler import RefusalHandler
from agents.identity_loader import IdentityLoader
from agents.tool_display import friendly_tool_label
from agents.direct_execution import (
    direct_execute as helper_direct_execute,
    extract_args_from_text as helper_extract_args_from_text,
    handle_daemon_direct as helper_handle_daemon_direct,
    handle_memory_direct as helper_handle_memory_direct,
)
from agents.ui_handlers import (
    handle_daemon_result as helper_handle_daemon_result,
    handle_permission_response as helper_handle_permission_response,
    handle_ui_event as helper_handle_ui_event,
    send_permission_request as helper_send_permission_request,
)
from agents.response_delivery import (
    send_text as helper_send_text,
    try_genui_for_result as helper_try_genui_for_result,
    try_send_sdui as helper_try_send_sdui,
)

if TYPE_CHECKING:
    from api.server import VisionBuffer
    from memory.store import MemoryStore
    from agents.learner import Learner
    from agents.multi_agent import MultiAgentOrchestrator

logger = logging.getLogger("feral.orchestrator")


class Orchestrator:
    """
    The core agentic loop — fully wired to perception, memory, and safety.

    Heavy lifting is delegated to:
      - ToolRunner      – tool dispatch, safety, anti-loop, subagents
      - ContextManager   – conversation history compaction
      - RefusalHandler   – LLM refusal detection and fallback execution
      - IdentityLoader   – ~/.feral/ identity files → system prompt
    """

    # Class-level constants kept on Orchestrator for backward compat
    ALWAYS_INCLUDE_SKILLS = {
        # Core OS / desktop surface
        "desktop_control",
        "computer_use",
        "browser",
        "desktop_automation",
        "screen_capture",
        "system_settings",
        "agentic_computer_use",
        # Messaging / comms — always reachable so the agent never claims it "can't send"
        "messaging_channels",
        # Never-say-no escape hatches + self-knowledge
        "workspace_scripts",
        "self_introspection",
        # Memory + search
        "notes_memory",
        "web_search",
    }

    def __init__(
        self,
        skill_registry: SkillRegistry,
        send_to_client: Callable[[str, FeralMessage], Awaitable[None]],
        daemons: dict[str, WebSocket],
        memory: "MemoryStore" = None,
        vision_buffer: "VisionBuffer" = None,
        perception: PerceptionEngine = None,
        learner: "Learner" = None,
        taskflows=None,
        approval_manager=None,
    ):
        self.skills = skill_registry
        self.send = send_to_client
        self.daemons = daemons
        # Phase 5 (audit-r10 overhaul) — capability registry for
        # capability-aware action dispatch. Populated by BrainState
        # after construction via `set_capability_registry`. None-safe
        # so unit tests that build a bare Orchestrator keep working.
        self.capability_registry = None
        self.memory = memory
        self.vision_buffer = vision_buffer
        self.perception = perception or PerceptionEngine()
        self.learner = learner
        self.taskflows = taskflows

        # Components — use shared LLM if provided
        self.llm = None  # set via set_llm() from BrainState
        self.executor = SkillExecutor(daemons=daemons)
        self.genui = GenUIGenerator()
        self._mcp_client = None
        self._somatic_engine = None  # set via set_somatic_engine() from BrainState
        self._tool_genesis = None    # set via set_tool_genesis() from BrainState
        self._mitosis_engine = None  # set via set_mitosis_engine() from BrainState

        # Delegate sub-modules
        self.tool_runner = ToolRunner(self, approval_manager=approval_manager)
        self.context_manager = ContextManager(max_messages=15)
        self.refusal_handler = RefusalHandler(self)
        self.identity_loader = IdentityLoader(memory=memory)

        # State
        self.biometric_state: dict[str, dict] = {}
        self.conversation_history: dict[str, list[dict]] = {}
        self._conversation_max_per_session = 200
        self._conversation_max_sessions = 500
        # Per-session async lock. Two concurrent turns on the SAME session
        # used to race on `conversation_history` + the outgoing tool
        # ordering. Different sessions still run fully parallel — only
        # turns on the same session are serialised.
        self._session_locks: dict[str, asyncio.Lock] = {}
        # Per-session execution surface, populated from handle_command's
        # context dict. Threaded into ToolRunner.enforce_safety so
        # surface deny-lists fire on the actual invocation surface
        # instead of the historical "websocket" default.
        self._session_surfaces: dict[str, str] = {}
        # F2 — turns-since-last-compaction counter per session.
        # ``_maybe_auto_compact`` increments after every full turn and
        # fires ``memory.compact_session`` once it crosses
        # ``settings.memory.compaction.turns_threshold`` (default 20),
        # at which point the counter resets. ``_compaction_inflight``
        # gates against overlapping compactions on the same session.
        self._turns_since_compaction: dict[str, int] = {}
        self._compaction_inflight: dict[str, bool] = {}
        # Audit-r11 — Bug 1 (double bubble on iOS): when the phone
        # ``/v1/node chat_request`` handler is about to send its own
        # synchronous ``chat_response`` we set
        # ``_text_response_suppressed[session_id] = True`` for the
        # duration of the turn. ``response_delivery.send_text`` checks
        # this flag and skips the broadcast ``text_response`` so the
        # phone doesn't render the same assistant reply twice. Cleared
        # in a ``finally`` in the chat_request branch.
        self._text_response_suppressed: dict[str, bool] = {}
        # Audit-r11 — Bug 3 (silent voice fallback). Late-bound from
        # ``api/state.py:_attach_voice_router_to_orchestrator`` because
        # the router is built AFTER the orchestrator. Used by
        # ``response_delivery.send_text`` to drive whisper TTS chunks
        # when the realtime provider has died on this session.
        self.voice_router = None
        self._pending_daemon_results: dict[str, asyncio.Future] = {}
        self._pending_frame_futures: dict[str, asyncio.Future] = {}
        self._pending_confirmations: dict[str, dict] = {}
        self._pending_permission_requests: dict[str, dict] = {}
        self._fallback_learning_state: dict[str, dict] = {}
        self._auto_learn_threshold = 3
        # Paused thoughts keyed by session_id. When a consciousness
        # entity of kind=thought is resumed, its text is pre-threaded
        # into the next turn's system-level context so the LLM sees
        # the half-formed sentence BEFORE the user's new input. This
        # is the "I started saying X, came back next morning, continue
        # that same sentence" contract.
        self._paused_thoughts: dict[str, list[dict]] = {}
        self._auto_learn_window_seconds = 1800
        self._auto_learn_cooldown_seconds = 3600
        self._session_finalized: set[str] = set()

        # Multi-agent
        self._multi_agent_enabled = os.environ.get("FERAL_MULTI_AGENT", "false").lower() in ("true", "1", "yes")
        self._multi_agent: Optional["MultiAgentOrchestrator"] = None

        # Vision config
        self._vision_enabled = os.environ.get("FERAL_VISION_ENABLED", "").lower() in ("true", "1", "yes")

        # Proactive loop config
        self._proactive_enabled = os.environ.get("FERAL_PROACTIVE", "").lower() in ("true", "1", "yes")
        self._last_proactive_check: dict[str, float] = {}
        self._proactive_cooldown = 60.0

        # Streaming config
        self._streaming_enabled = os.environ.get("FERAL_STREAMING", "true").lower() in ("true", "1", "yes")
        try:
            self._max_iterations = max(1, min(int(os.environ.get("FERAL_MAX_ITERATIONS", "20")), 40))
        except ValueError:
            self._max_iterations = 20

        self.executor.load_vault_from_env()

    # ─────────────────────────────────────────────
    # Wiring helpers (called by BrainState / server)
    # ─────────────────────────────────────────────

    def set_llm(self, llm: LLMProvider):
        """Set the shared LLM provider — avoids duplicate connections."""
        self.llm = llm
        if self._multi_agent_enabled:
            self._init_multi_agent()

    def set_capability_registry(self, registry) -> None:
        """Phase 5 (audit-r10) — inject the brain's CapabilityRegistry.

        Called from `BrainState.init()` after both the orchestrator
        and registry exist. The registry tracks which `phone.*` /
        `glasses.*` action names connected nodes currently publish
        (via `node_register.skills` per Phase 4) so
        `ToolRunner.execute_capability_action(...)` can route or fail
        truthfully instead of blindly sending an HUP action into the
        void.
        """
        self.capability_registry = registry

    def set_session_snapshot_hook(self, hook) -> None:
        """Phase 3 (audit-r10) — register a no-arg callable that the
        orchestrator invokes after each successful turn whose
        session_id matches the brain's `primary_session_id`. Hook is
        responsible for persisting the current primary thread to disk
        (see `BrainState.snapshot_primary_thread`). Debouncing and
        error handling live in the hook so the orchestrator stays
        agnostic of the persistence layer.
        """
        self._session_snapshot_hook = hook

    def _maybe_snapshot_primary(self, session_id: str) -> None:
        """Best-effort persistence after a turn. Never raises.

        Called from both `_handle_command_impl` and
        `_handle_command_stream_impl` at completion so a crash mid-
        chat at worst loses the in-flight turn — last completed turn
        is durable.
        """
        hook = getattr(self, "_session_snapshot_hook", None)
        if hook is None:
            return
        primary = getattr(self, "_primary_session_id_resolver", None)
        # Resolver may be wired by BrainState; otherwise we can read
        # `api.state.state.primary_session_id` defensively.
        try:
            if primary is not None:
                primary_id = primary() if callable(primary) else primary
            else:
                from api.state import state as _state
                primary_id = getattr(_state, "primary_session_id", "")
        except Exception:
            primary_id = ""
        if not primary_id or session_id != primary_id:
            return
        try:
            hook()  # BrainState.snapshot_primary_thread()
        except Exception as exc:
            logger.debug("snapshot hook raised: %s", exc)

    def register_paused_thought(self, *, session_id: str, thought_id: str, text: str) -> None:
        """Queue a paused-thought fragment so the next turn re-threads it.

        Called by ``/api/consciousness/resume`` when the user resumes a
        kind=thought entity. On the next ``handle_command`` for this
        session the orchestrator prepends a synthetic assistant message
        quoting the paused fragment to the LLM history so the model
        continues the same thread rather than starting cold.

        Idempotent on ``thought_id`` — resuming the same thought twice
        won't duplicate the re-thread.
        """
        if not session_id or not text:
            return
        bucket = self._paused_thoughts.setdefault(session_id, [])
        if any(t.get("id") == thought_id for t in bucket):
            return
        bucket.append({"id": thought_id, "text": text})
        logger.info(
            "[%s] registered paused thought for re-thread on next turn (id=%s, %d chars)",
            session_id[:8] if len(session_id) >= 8 else session_id,
            thought_id[:8], len(text),
        )

    def drain_paused_thoughts(self, session_id: str) -> list[dict]:
        """Pop and return any paused thoughts for this session.

        Called by ``handle_command`` before building the LLM history.
        Once drained the thoughts are gone — this is intentional. If a
        turn doesn't actually re-thread them (user ignored the resume),
        the ConsciousnessStore still holds the canonical record.
        """
        return self._paused_thoughts.pop(session_id, []) or []

    def set_vault(self, vault):
        """Wire the BlindVault into the skill executor for secure key injection."""
        self.executor.set_blind_vault(vault)

    def set_mcp_client(self, mcp_client):
        """Wire the MCP client so its tools are available to the LLM."""
        self._mcp_client = mcp_client

    def set_genui_engine(self, engine):
        """Wire the shared GenUI engine so tool-result SDUI uses the server's LLM."""
        self._genui_engine = engine

    def set_somatic_engine(self, somatic_engine):
        """Wire the SomaticEngine so the identity loader can inject body-state context."""
        self._somatic_engine = somatic_engine
        self.identity_loader.somatic_engine = somatic_engine

    def set_calendar(self, calendar):
        """Wire the CalendarIntegration so the system prompt includes
        upcoming events/reminders.

        Operator report 2026-05-10: "I created an event on the FERAL
        webUI locally and then I asked the chat on the iOS app but it
        has no idea." Audit-r9 root cause (subagent #cd995a59): the
        system prompt does NOT auto-inject calendar / reminders. The
        LLM only knows about events when a calendar tool fires, AND
        the tool only fires when `_route_prompt(query)` happens to
        route the query into the `calendar_google` skill — fragile
        keyword matching. The result: phone chat asks "do I have
        anything today?" and the LLM answers from working memory
        only (which is partitioned by `session_id`, so it never
        contains events created on the web tab).

        Fix: wire `state.calendar` into IdentityLoader so the prompt
        always carries a "## Today's Events" block with the next ~5
        upcoming items, regardless of which `session_id` is asking.
        Same approach the proactive engine already uses
        (`agents/proactive_engine.py:953-961`).
        """
        self.identity_loader.calendar = calendar

    def set_tool_genesis(self, tool_genesis):
        """Wire the ToolGenesisEngine so the orchestrator records tool-call patterns."""
        self._tool_genesis = tool_genesis

    def set_mitosis_engine(self, mitosis_engine):
        """Wire the AgentMitosisEngine so the orchestrator observes interaction patterns."""
        self._mitosis_engine = mitosis_engine

    def _init_multi_agent(self):
        """Lazy-init the multi-agent orchestrator once LLM is available."""
        try:
            from agents.multi_agent import MultiAgentOrchestrator
            self._multi_agent = MultiAgentOrchestrator(
                llm=self.llm,
                skill_registry=self.skills,
                skill_executor=self.executor,
                memory=self.memory,
                perception=self.perception,
                send_to_client=self.send,
            )
            logger.info("Multi-agent orchestrator initialized with workers: %s", list(self._multi_agent._workers.keys()))
        except Exception as e:
            logger.warning(f"Multi-agent init failed, falling back to single-agent: {e}")
            self._multi_agent_enabled = False

    @property
    def runtime_status(self) -> dict:
        return {
            "multi_agent_enabled": self._multi_agent_enabled,
            "multi_agent_ready": self._multi_agent is not None,
            "active_subagents": self.tool_runner._active_subagent_tasks,
            "pending_confirmations": len(self._pending_confirmations),
        }

    # ─────────────────────────────────────────────
    # W17 — Subagent spawn (additive)
    # See docs/OPENCLAW_LESSONS.md §10 W17. Single additive method;
    # behaviour of every existing handler is unchanged. Spawning is
    # gated by ``agents.subagent_policy`` and audited via the supervisor.
    # ─────────────────────────────────────────────

    async def spawn_subsession(
        self,
        parent_session_id: str,
        kind: str,
        *,
        scope_key: str,
        model_override: Optional[str] = None,
    ) -> str:
        """Spawn a child subsession of *parent_session_id* (W17).

        Delegates to :func:`agents.subagent_spawner.spawn_subsession`.
        Raises :class:`agents.subagent_spawner.SubagentNotAllowed` when
        the policy denies this (parent_kind, child_kind) pair; the deny
        is logged to the supervisor with ``decision="denied"``.
        """
        from agents.subagent_spawner import (
            register_parent_kind,
            spawn_subsession as _spawn,
        )
        register_parent_kind(parent_session_id, "orchestrator")
        return await _spawn(
            parent_session_id,
            kind,
            scope_key=scope_key,
            model_override=model_override,
        )

    def _w17_cancel_subsessions_nowait(self, parent_session_id: str) -> None:
        """Sync hook called from the session-lock teardown path (W17).

        All-children-tied by default — every subagent registered under
        *parent_session_id* is cancelled. Call sites that need to keep
        a sibling alive must spawn it under a different parent_id or
        cancel a specific scope before the teardown fires.
        """
        try:
            from agents.subagent_spawner import get_registry
            get_registry().cancel_all_children_nowait(parent_session_id)
        except Exception as exc:
            logger.debug("W17 subagent teardown skipped: %s", exc)

    # ─────────────────────────────────────────────
    # Backward-compat delegation methods
    # (tests / internal code may call these directly)
    # ─────────────────────────────────────────────

    def _compact_context(self, history: list[dict]) -> list[dict]:
        return self.context_manager.compact(history)

    def _maybe_auto_compact(self, session_id: str) -> None:
        """F2 — increment the per-session turn counter and schedule a
        real ``compact_session`` once it crosses the configured
        ``turns_threshold``.

        Called from the post-turn save sites in ``handle_command``
        and ``_handle_command_stream_impl``. Runs in fire-and-forget
        mode so the user-visible turn isn't blocked on compaction.
        Idempotent against overlapping invocations via
        ``_compaction_inflight``.
        """
        try:
            from config.loader import load_settings
            settings = load_settings()
            compaction_cfg = (
                (settings.get("memory") or {}).get("compaction") or {}
            )
        except Exception:
            return
        if not compaction_cfg.get("enabled", True):
            return
        threshold = int(compaction_cfg.get("turns_threshold", 20))
        if threshold <= 0:
            return

        self._turns_since_compaction[session_id] = (
            self._turns_since_compaction.get(session_id, 0) + 1
        )
        if self._turns_since_compaction[session_id] < threshold:
            return
        if self._compaction_inflight.get(session_id):
            return
        if not self.memory:
            return

        async def _run() -> None:
            self._compaction_inflight[session_id] = True
            try:
                history = list(self.conversation_history.get(session_id, []))
                if not history:
                    return
                result = await self.memory.compact_session(
                    session_id, history, llm=self.llm,
                )
                if result.get("compacted") and result.get("history"):
                    self.conversation_history[session_id] = result["history"]
                self._turns_since_compaction[session_id] = 0
                logger.info(
                    "F2 auto-compact: session=%s episode_id=%s entities=%s",
                    session_id,
                    result.get("episode_id"),
                    result.get("key_entities", []),
                )
            except Exception as exc:
                logger.warning("F2 auto-compact failed: %s", exc)
            finally:
                self._compaction_inflight[session_id] = False

        try:
            asyncio.ensure_future(_run())
        except RuntimeError:
            pass

    def _is_refusal_text(self, text: str) -> bool:
        return self.refusal_handler.is_refusal(text)

    @staticmethod
    def _skill_endpoint_in_set(skill: "SkillManifest", allowed: set[str]) -> bool:
        sid = getattr(skill, "skill_id", "")
        for ep in getattr(skill, "endpoints", []) or []:
            qualified = f"{sid}__{getattr(ep, 'id', '')}"
            if qualified in allowed:
                return True
        return False

    @staticmethod
    def _build_specialist_system_prompt(specialist, base_system_prompt: str) -> str:
        """Wrap the base system prompt with a specialist persona block + tool restriction notice."""
        tools_line = ", ".join(specialist.tool_permissions or []) or "(no specific tools)"
        persona = (
            f"## Specialist Mode: {specialist.name}\n"
            f"{specialist.description}\n\n"
            f"{specialist.system_prompt.strip()}\n\n"
            f"You are currently operating as the **{specialist.name}** specialist. "
            f"Stay within this domain. Allowed tools: {tools_line}."
        )
        return f"{persona}\n\n---\n\n{base_system_prompt}"

    async def _on_capability_gap(
        self,
        session_id: str,
        text: str,
        relevant_skills: list["SkillManifest"],
    ) -> Optional[dict]:
        """Autonomy-tiered handling when no existing tool fits the user's intent.

        - strict   → write a throwaway script via ``workspace_scripts__run`` and
          return stdout. Our workspace-scoped exec escape hatch.
        - hybrid   → ask Tool Genesis to draft a proposal, surface it in Settings
          → Proposed Skills. Reply to the user that a draft is pending approval.
        - loose    → draft + auto-promote silently. Next turn the new skill is
          reachable; the agent retries transparently.

        Returns a dict describing what happened, or ``None`` when the gap
        handler declined (e.g. tool_genesis is not initialized in strict mode
        fallback paths).
        """
        # v2026.5.26 — prefer the live ToolRunner state (the runtime
        # source of truth); fall back to persisted settings.json under
        # ``security.autonomy_mode`` via ``ConfigLoader.get`` (the
        # pre-fix code used ``get_setting`` which doesn't exist, so it
        # silently always fell to "hybrid").
        mode = "hybrid"
        try:
            live = getattr(self.tool_runner, "autonomy_mode", "")
            if live:
                mode = str(live).lower()
            else:
                from api.state import state as _state
                cfg = getattr(_state, "config", None)
                if cfg:
                    getter = getattr(cfg, "get_setting", None) or getattr(cfg, "get", None)
                    if getter:
                        try:
                            val = getter("security", "autonomy_mode")
                        except TypeError:
                            val = getter("autonomy_mode")
                        if val:
                            mode = str(val).lower()
        except Exception:
            pass

        if mode == "strict":
            impl = self.skills.get_skill("workspace_scripts")
            if impl is None:
                return {"mode": mode, "handled": False, "reason": "workspace_scripts unavailable"}
            code = (
                "import os, sys\n"
                "print('FERAL workspace_scripts strict-mode stub. '\n"
                "      'This handler expected an LLM-generated script — '\n"
                "      'the planner should pass it explicitly next turn.')\n"
            )
            result = await impl.execute("run", {"language": "python", "code": code, "name": "strict_gap_probe"}, {})
            return {"mode": mode, "handled": True, "stdout": (result.get("data") or {}).get("stdout"), "script": result}

        if self._tool_genesis is None:
            return {"mode": mode, "handled": False, "reason": "tool_genesis not initialized"}

        try:
            tool_id = await self._tool_genesis.propose_from_intent(text)
        except Exception as exc:
            logger.warning("propose_from_intent failed: %s", exc)
            return {"mode": mode, "handled": False, "reason": f"propose_failed: {exc}"}
        if not tool_id:
            return {"mode": mode, "handled": False, "reason": "proposal_generation_failed"}

        if mode == "loose":
            self._tool_genesis.approve_tool(tool_id)
            promote_result = self._tool_genesis.promote(tool_id, skill_registry=self.skills)
            return {"mode": mode, "handled": True, "promoted": promote_result.get("promoted"), "tool_id": tool_id}

        # hybrid
        try:
            await self._send_text(
                session_id,
                "I don't have a built-in skill for that. I drafted a new one — open "
                "Settings → Proposed Skills to review and approve it, and I'll wire "
                "it up live.",
            )
        except Exception:
            pass
        return {"mode": mode, "handled": True, "tool_id": tool_id, "pending_approval": True}

    async def _build_system_prompt(
        self,
        frame: PerceptionFrame,
        skills: list[SkillManifest],
        session_id: str = "",
        memory_filter: str = "",
        query: str = "",
    ) -> str:
        full_catalog: list[SkillManifest] = []
        try:
            full_catalog = list(self.skills.skills.values())
        except Exception:
            pass
        return await self.identity_loader.build_system_prompt(
            frame,
            skills,
            session_id,
            identity_text=self._load_identity(),
            full_catalog=full_catalog,
            memory_filter=memory_filter,
            query=query,
        )

    def _load_identity(self) -> str:
        return self.identity_loader.load_identity()

    def _classify_safety(self, tool_name: str, args: dict) -> str:
        return self.tool_runner.classify_safety(tool_name, args)

    def _enforce_safety(self, tool_name: str, args: dict) -> Optional[dict]:
        return self.tool_runner.enforce_safety(tool_name, args)

    async def _execute_tool_call_for_llm(self, session_id: str, tool_call: dict, available_skills: list[SkillManifest]) -> dict:
        return await self.tool_runner.execute_tool_call_for_llm(session_id, tool_call, available_skills)

    async def _execute_tool_call(self, session_id: str, tool_call: dict, available_skills: list[SkillManifest]):
        return await self.tool_runner.execute_tool_call(session_id, tool_call, available_skills)

    async def _execute_daemon_command(self, session_id: str, node_id: str, action: str, args: dict):
        return await self.tool_runner.execute_daemon_command(session_id, node_id, action, args)

    async def _spawn_subagents_for_task(self, session_id: str, args: dict) -> dict:
        return await self.tool_runner.spawn_subagents(session_id, args)

    async def _execute_action_intent_fallback(self, session_id: str, text: str, available_skills: list[SkillManifest]) -> bool:
        return await self.refusal_handler.execute_action_intent_fallback(session_id, text, available_skills)

    @staticmethod
    def _tool_signature(tool_name: str, args: dict) -> str:
        return ToolRunner.tool_signature(tool_name, args)

    def _register_tool_attempt(self, session_id: str, tool_name: str, args: dict) -> int:
        return self.tool_runner.register_tool_attempt(session_id, tool_name, args)

    @staticmethod
    def _anti_loop_guidance(tool_name: str, streak: int) -> str:
        return ToolRunner.anti_loop_guidance(tool_name, streak)

    def _query_implies_action(self, text: str) -> bool:
        return self.refusal_handler.query_implies_action(text)

    def _action_text_is_destructive(self, text: str) -> bool:
        return self.refusal_handler.action_text_is_destructive(text)

    @staticmethod
    def _extract_first_url(text: str) -> str:
        return RefusalHandler.extract_first_url(text)

    def _extract_open_app_name(self, text: str) -> str:
        return self.refusal_handler.extract_open_app_name(text)

    def _build_action_intent_tool_call(self, text: str) -> Optional[dict]:
        return self.refusal_handler.build_action_intent_tool_call(text)

    @staticmethod
    def _summarize_action_result(tool_call: dict, result_data: dict) -> str:
        return RefusalHandler.summarize_action_result(tool_call, result_data)

    @staticmethod
    def _is_reject_execution_ack(user_text: str) -> bool:
        if not user_text:
            return False
        normalized = user_text.strip().lower()
        normalized = normalized.rstrip("!?.,;:").strip()
        return normalized in {
            "no",
            "nope",
            "nah",
            "cancel",
            "stop",
            "don't",
            "dont",
            "do not",
            "reject",
            "deny",
        }

    async def _execute_approved_pending_tool(
        self,
        session_id: str,
        *,
        request_id: str,
        tool_name: str,
        args: dict,
    ) -> dict:
        """Execute a previously-approved pending tool call."""
        self.tool_runner.grant_session_approval(tool_name, session_id)
        tool_call = {
            "name": tool_name,
            "args": args or {},
            "id": request_id,
        }
        await self._emit_tool_start(session_id, tool_call)
        t_start = time.time()
        result_data = await self._execute_tool_call_for_llm(session_id, tool_call, [])
        latency_ms = (time.time() - t_start) * 1000
        await self._emit_tool_result(session_id, tool_call, result_data, latency_ms)
        await self._try_genui_for_result(session_id, tool_call, result_data)
        summary = self._summarize_action_result(tool_call, result_data)
        await self._send_text(session_id, summary)
        if self.memory:
            self.memory.working_push(
                session_id,
                {"role": "assistant", "text": summary[:300]},
            )
        return {
            "status": "approved",
            "request_id": request_id,
            "session_id": session_id,
            "tool_name": tool_name,
            "args": args or {},
            "summary": summary,
            "result": result_data,
        }

    async def resolve_tool_approval_request(
        self,
        request_id: str,
        *,
        approved: bool,
        session_id: str | None = None,
        actor: str = "api",
    ) -> dict:
        """Resolve a pending tool approval request by id.

        Returns a status payload:
          * ``{"status": "not_found"}``
          * ``{"status": "session_mismatch", ...}``
          * ``{"status": "rejected", ...}``
          * ``{"status": "approved", ...}`` (includes execution summary/result)
        """
        pending = self.tool_runner.get_pending(request_id)
        if not pending:
            return {"status": "not_found", "request_id": request_id}

        pending_session = str(pending.get("session_id", "") or "")
        effective_session = str(session_id or pending_session)
        if effective_session != pending_session:
            return {
                "status": "session_mismatch",
                "request_id": request_id,
                "session_id": effective_session,
                "pending_session_id": pending_session,
            }

        tool_name = str(pending.get("tool_name", "") or "")
        args = pending.get("args") or {}
        if not tool_name:
            self.tool_runner.deny_pending(request_id, session_id=effective_session)
            return {
                "status": "not_found",
                "request_id": request_id,
                "session_id": effective_session,
            }

        if not approved:
            denied = self.tool_runner.deny_pending(request_id, session_id=effective_session)
            if denied is None:
                return {"status": "not_found", "request_id": request_id}
            await self._send_text(effective_session, f"Cancelled `{tool_name}`.")
            return {
                "status": "rejected",
                "request_id": request_id,
                "session_id": effective_session,
                "tool_name": tool_name,
                "resolved_by": actor,
            }

        accepted = self.tool_runner.approve_pending(
            request_id,
            session_id=effective_session,
        )
        if accepted is None:
            return {"status": "not_found", "request_id": request_id}
        return await self._execute_approved_pending_tool(
            effective_session,
            request_id=request_id,
            tool_name=tool_name,
            args=args,
        )

    async def _maybe_handle_pending_tool_approval_text(
        self,
        session_id: str,
        text: str,
    ) -> bool:
        """Consume plain-text yes/no replies for pending tool approvals.

        The v2 UI can render explicit approval cards, but users also
        frequently type short acknowledgements ("approved", "no").
        When a pending tool approval exists for this session, bind those
        short replies directly to that pending request so the tool run
        proceeds (or is cancelled) instead of generating a fresh
        approval-loop request.
        """
        pending = self.tool_runner.latest_pending_for_session(session_id)
        if not pending:
            return False

        if self.refusal_handler.is_ack_execution(text):
            req_id = str(pending.get("request_id", "") or "")
            if not req_id:
                return False
            outcome = await self.resolve_tool_approval_request(
                req_id,
                approved=True,
                session_id=session_id,
                actor="chat_text",
            )
            return outcome.get("status") == "approved"

        if self._is_reject_execution_ack(text):
            req_id = str(pending.get("request_id", "") or "")
            if not req_id:
                return False
            outcome = await self.resolve_tool_approval_request(
                req_id,
                approved=False,
                session_id=session_id,
                actor="chat_text",
            )
            return outcome.get("status") == "rejected"

        return False

    @staticmethod
    def _capability_key(text: str) -> str:
        return RefusalHandler.capability_key(text)

    # ─────────────────────────────────────────────
    # Specialist Routing (Agent Mitosis)
    # ─────────────────────────────────────────────

    def route_to_specialist(self, query: str) -> Optional[dict]:
        """Check if a mitosis specialist should handle this query.

        Returns {"agent_id": ..., "system_prompt": ...} or None.
        """
        if not self._mitosis_engine:
            return None
        agent_id = self._mitosis_engine.match_specialist(query)
        if not agent_id:
            return None
        specialist = self._mitosis_engine.get_specialist(agent_id)
        if not specialist:
            return None
        logger.info("Routing to specialist %s for query: %s", agent_id, query[:60])
        return {
            "agent_id": specialist.agent_id,
            "system_prompt": specialist.system_prompt,
            "name": specialist.name,
        }

    # ─────────────────────────────────────────────
    # Brain Event Bus (Glass Brain visualization)
    # ─────────────────────────────────────────────

    async def _emit_brain_event(self, session_id: str, event_type: str, data: dict):
        """Emit a brain event to the session for Glass Brain visualization."""
        try:
            msg = FeralMessage(type="brain_event", payload={"event": event_type, **data})
            await self.send(session_id, msg)
        except Exception:
            pass

    async def _emit_tool_start(self, session_id: str, tool_call: dict) -> None:
        """Notify the UI a tool call is starting (chip affordance)."""
        try:
            name = str(tool_call.get("name", "tool"))
            parts = name.split("__", 1)
            skill_id = parts[0] if len(parts) == 2 else name
            endpoint_id = parts[1] if len(parts) == 2 else ""
            preview = ""
            try:
                args = tool_call.get("args") or {}
                if isinstance(args, dict) and args:
                    preview = json.dumps(args, default=str)[:160]
            except Exception:
                preview = ""
            await self.send(session_id, FeralMessage(
                session_id=session_id, hop="brain", type="tool_start",
                payload=ToolStartPayload(
                    tool=name,
                    call_id=str(tool_call.get("id", "")),
                    skill_id=skill_id,
                    endpoint_id=endpoint_id,
                    args_preview=preview,
                    display_name=friendly_tool_label(
                        name,
                        skill_id=skill_id,
                        endpoint_id=endpoint_id,
                    ),
                ).model_dump(),
            ))
        except Exception:
            pass

    async def _emit_tool_result(
        self,
        session_id: str,
        tool_call: dict,
        result_data: dict,
        latency_ms: float,
    ) -> None:
        """Notify the UI a tool call has finished (clears the chip)."""
        try:
            success = bool(
                (isinstance(result_data, dict) and (result_data.get("success") or result_data.get("status") == "command_sent_to_hardware_daemon"))
            )
            err = ""
            if isinstance(result_data, dict):
                err = str(result_data.get("error") or "")[:240]
            await self.send(session_id, FeralMessage(
                session_id=session_id, hop="brain", type="tool_result",
                payload=ToolResultPayload(
                    tool=str(tool_call.get("name", "tool")),
                    call_id=str(tool_call.get("id", "")),
                    success=success,
                    error=err,
                    latency_ms=float(latency_ms or 0.0),
                ).model_dump(),
            ))
        except Exception:
            pass

    # ─────────────────────────────────────────────
    # Core Command Handler
    # ─────────────────────────────────────────────

    def update_biometric(self, session_id: str, biometric: dict):
        self.biometric_state[session_id] = biometric

    def _get_session_lock(self, session_id: str) -> asyncio.Lock:
        """Return (creating if needed) the per-session async lock.

        Two concurrent `handle_command*` calls for the SAME session_id
        must serialise — they share ``conversation_history`` and the
        LLM tool-call ordering. Calls for DIFFERENT session_ids run
        fully in parallel; this lock only blocks intra-session.
        """
        lock = self._session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_id] = lock
        return lock

    # ─────────────────────────────────────────────
    # Fire-and-forget episode save (Lane 08 WS1)
    # ─────────────────────────────────────────────
    #
    # ``MemoryStore.episode_save`` is async-safe (Wave 2 Lane 05 moved
    # the SQLite write to a worker thread + the AboutMe extractor to a
    # background task), but the orchestrator used to ``await`` it on
    # the hot path. That made the user-visible time-to-first-token a
    # function of WAL contention / disk fsync, not LLM latency.
    #
    # ``_save_episode_async`` schedules the save as a background task
    # and returns immediately. Failures are logged + counted on
    # ``feral_episode_save_fail_total`` so an operator can spot a
    # broken memory layer without staring at the hot path.

    def _save_episode_async(
        self,
        *,
        session_id: str,
        event_type: str,
        summary: str,
        detail: str = "",
        importance: float | None = None,
    ) -> Optional[asyncio.Task]:
        """Schedule ``memory.episode_save`` as a fire-and-forget task.

        Returns the scheduled ``asyncio.Task`` so tests can await on it
        deterministically; production callers ignore the return value.
        ``None`` when ``self.memory`` is unwired.
        """
        if not self.memory:
            return None

        kwargs: dict = {
            "session_id": session_id,
            "event_type": event_type,
            "summary": summary,
            "detail": detail,
        }
        if importance is not None:
            kwargs["importance"] = importance

        async def _runner() -> None:
            try:
                await self.memory.episode_save(**kwargs)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "episode_save failed (session=%s event=%s): %s",
                    session_id[:8] if len(session_id) >= 8 else session_id,
                    event_type,
                    exc,
                )
                try:
                    from observability.metrics import increment as _inc
                    _inc(
                        "feral_episode_save_fail_total",
                        attributes={"event_type": event_type},
                    )
                except Exception:
                    pass

        try:
            return asyncio.create_task(_runner())
        except RuntimeError:
            # No running loop — extremely rare on the orchestrator
            # hot path, but defensively swallow so the caller can
            # proceed instead of crashing the turn.
            return None

    async def handle_command(self, session_id: str, text: str, context: Optional[dict] = None):
        """Process a user command through the full agentic pipeline.

        Thin wrapper that acquires a per-session lock so two concurrent
        turns on the same session cannot race on ``conversation_history``
        or interleave tool_call ordering. Different sessions proceed
        fully in parallel.
        """
        try:
            async with self._get_session_lock(session_id):
                return await self._handle_command_impl(session_id, text, context)
        finally:
            # W17: tear down subagents tied to this parent session.
            # Lock release stays synchronous; cancellation is fire-and-forget.
            self._w17_cancel_subsessions_nowait(session_id)

    def _stamp_session_surface(self, session_id: str, context: Optional[dict]) -> str:
        """Resolve and persist the execution surface for ``session_id``.

        Called at the head of every handle_command path so deeper tool
        execution can read back the surface via
        ``ToolRunner._resolve_surface_for_session`` instead of always
        falling through to the websocket default.
        """
        surface = resolve_surface_from_context(context)
        self._session_surfaces[session_id] = surface
        return surface

    async def _handle_command_impl(self, session_id: str, text: str, context: Optional[dict] = None):
        """Real body of handle_command. Guarded by the session lock above."""
        logger.info(f"[{session_id[:8]}] Command: {text}")
        self._session_finalized.discard(session_id)
        self._stamp_session_surface(session_id, context)

        if await self._maybe_handle_pending_tool_approval_text(session_id, text):
            return

        if self.taskflows and isinstance(context, dict):
            taskflow_spec = context.get("taskflow")
            if isinstance(taskflow_spec, dict):
                steps = taskflow_spec.get("steps", [])
                if isinstance(steps, list) and steps:
                    flow = self.taskflows.create_flow(
                        session_id=session_id,
                        title=taskflow_spec.get("title", text[:80] or "Background TaskFlow"),
                        steps=steps,
                        context=taskflow_spec.get("context", {"prompt": text}),
                    )
                    ack = f"Started TaskFlow {flow['id']} with {len(steps)} step(s)."
                    await self._send_text(session_id, ack)
                    if self.memory:
                        self.memory.working_push(session_id, {"role": "assistant", "text": ack})
                    return

        if self._somatic_engine:
            self._somatic_engine.update_interaction(session_id, len(text))

        # WS1 — episode_save is fire-and-forget. Hot path returns
        # before SQLite WAL commit / AboutMe extraction completes.
        self._save_episode_async(
            session_id=session_id,
            event_type="user_command",
            summary=text[:200],
            detail=json.dumps(context or {}),
        )

        context_data = context or {}
        vision_fast_path = context_data.get("channel") == "vision_ask"

        # Multi-agent path
        if (
            not vision_fast_path
            and self._multi_agent_enabled
            and self._multi_agent
            and self.llm
            and self.llm.available
        ):
            source = context_data.get("source", "")
            if source != "proactive":
                try:
                    response_text = await self._multi_agent.run(session_id, text, context)
                    if response_text:
                        await self._try_send_sdui(session_id, response_text)
                        if self.memory:
                            self.memory.working_push(session_id, {"role": "assistant", "text": response_text[:300]})
                        if self.learner:
                            asyncio.ensure_future(self.learner.on_message(session_id, "user", text))
                        return
                except Exception as e:
                    logger.warning(f"Multi-agent failed, falling back to single-agent: {e}")

        # Step 1: Semantic Tool Routing
        relevant_skills = await self._route_prompt(text)

        if relevant_skills:
            logger.info(f"  Matched: {[s.brand.name for s in relevant_skills]}")

        if not self.llm.available:
            await self._direct_execute(session_id, text, relevant_skills)
            return

        # Full Agentic Mode — inject core skills only for LLM tool routing
        relevant_skills = self._ensure_core_skills(relevant_skills)

        # Agent Mitosis routing — if a specialist claims this domain, swap in
        # its prompt + narrow tool permissions for this turn.
        specialist = None
        try:
            if self._mitosis_engine and hasattr(self._mitosis_engine, "route_to_specialist"):
                specialist = self._mitosis_engine.route_to_specialist(text, session_id)
        except Exception as exc:
            logger.debug("mitosis routing skipped: %s", exc)

        if specialist:
            allowed = set(specialist.tool_permissions or [])
            narrowed = [s for s in relevant_skills if s.skill_id in allowed or self._skill_endpoint_in_set(s, allowed)]
            if narrowed:
                relevant_skills = self._ensure_core_skills(narrowed)
            logger.info("[%s] routed to specialist %s", session_id[:8], specialist.agent_id)

        tools = self.skills.get_tools_for_skills(relevant_skills)

        if self._mcp_client:
            mcp_tools = self._mcp_client.to_llm_tool_definitions()
            if mcp_tools:
                tools = (tools or []) + mcp_tools

        perception_frame = self.perception.get_frame(session_id)
        # When a specialist is routing this turn, thread its memory_filter
        # into context retrieval so cross-domain memory leaks stop. Empty
        # string = generalist turn (no filtering, legacy behaviour).
        active_memory_filter = (specialist.memory_filter if specialist else "") or ""
        system_prompt = await self._build_system_prompt(
            perception_frame,
            relevant_skills,
            session_id,
            memory_filter=active_memory_filter,
            query=text or "",
        )
        if specialist:
            system_prompt = self._build_specialist_system_prompt(specialist, system_prompt)

        if session_id not in self.conversation_history:
            self.conversation_history[session_id] = []

        # Re-thread any paused thoughts registered via
        # /api/consciousness/resume (kind=thought). The fragments are
        # pre-pended as synthetic assistant messages so the LLM sees
        # "I was mid-sentence saying: X" before the user's new input.
        for paused in self.drain_paused_thoughts(session_id):
            fragment = paused.get("text") or ""
            if not fragment:
                continue
            self.conversation_history[session_id].append({
                "role": "assistant",
                "content": f"[RESUMED THOUGHT] {fragment}",
            })

        user_content = perception_frame.to_llm_user_content(text)
        user_message = {"role": "user", "content": user_content}
        self.conversation_history[session_id].append(user_message)

        history = self._compact_context(self.conversation_history[session_id].copy())

        max_iterations = self._max_iterations
        refusal_retry_used = False
        reasoning_retry_count = 0
        empty_retry_used = False
        pending_retry_addition: Optional[str] = None
        # Never-stall: if the user turn is a short ack, inject the fast-path
        # instruction into the very first call (before the model replies).
        if self.refusal_handler.is_ack_execution(text):
            pending_retry_addition = self.refusal_handler.ACK_EXECUTION_FAST_PATH_INSTRUCTION
        sent_response = False
        for _ in range(max_iterations):
            effective_system_prompt = system_prompt
            if pending_retry_addition:
                effective_system_prompt = (
                    f"{system_prompt}\n\n[RETRY_STEER]\n{pending_retry_addition}"
                )
                pending_retry_addition = None
            messages = [
                {"role": "system", "content": effective_system_prompt},
                *history,
            ]

            try:
                model_name = getattr(self.llm, 'model_name', 'llm')
                await self._emit_brain_event(session_id, "llm_call", {"model": model_name})
                response = await self.llm.chat_with_failover(messages=messages, tools=tools if tools else None)
                text_content, tool_calls = self.llm.extract_response(response)

                # Never-stall: empty response — no text, no tool calls.
                if not text_content and not tool_calls and not empty_retry_used:
                    empty_retry_used = True
                    logger.warning("[%s] Empty response; prompt-addition retry", session_id[:8])
                    pending_retry_addition = self.refusal_handler.EMPTY_RESPONSE_RETRY_INSTRUCTION
                    continue

                # Reasoning-only: provider returned reasoning trace but no visible output.
                if self.refusal_handler.is_reasoning_only(response) and reasoning_retry_count < 2:
                    reasoning_retry_count += 1
                    logger.warning(
                        "[%s] Reasoning-only response (retry %d); prompt-addition steer",
                        session_id[:8], reasoning_retry_count,
                    )
                    pending_retry_addition = self.refusal_handler.REASONING_ONLY_RETRY_INSTRUCTION
                    continue

                plan_only_trigger = (
                    text_content
                    and not tool_calls
                    and self._query_implies_action(text)
                    and self.refusal_handler.is_plan_only(text_content)
                )
                if text_content and not tool_calls and (
                    self._is_refusal_text(text_content) or plan_only_trigger
                ):
                    if not refusal_retry_used:
                        refusal_retry_used = True
                        logger.warning(
                            "[%s] %s detected; forcing act-now retry (prompt-addition)",
                            session_id[:8],
                            "Plan-only" if plan_only_trigger else "Refusal",
                        )
                        pending_retry_addition = self.refusal_handler.planning_only_retry_instruction(text)
                        continue
                    logger.warning(
                        "[%s] Refusal/plan-only persisted after retry; falling back to direct execution",
                        session_id[:8],
                    )
                    handled = await self._execute_action_intent_fallback(session_id, text, relevant_skills)
                    if not handled:
                        gap_result = await self._on_capability_gap(session_id, text, relevant_skills)
                        if gap_result and gap_result.get("handled"):
                            logger.info(
                                "[%s] capability_gap handled via autonomy=%s",
                                session_id[:8], gap_result.get("mode"),
                            )
                            return
                        await self._direct_execute(session_id, text, relevant_skills)
                    return

                assistant_msg = {"role": "assistant"}
                if text_content:
                    assistant_msg["content"] = text_content

                if "choices" in response and response["choices"]:
                    raw_msg = response["choices"][0].get("message", {})
                    if raw_msg.get("tool_calls"):
                        assistant_msg["tool_calls"] = raw_msg["tool_calls"]

                history.append(assistant_msg)

            except Exception as e:
                logger.error(f"LLM failed: {e}")
                await self._direct_execute(session_id, text, relevant_skills)
                return

            if tool_calls:
                # Parallel dispatch of every tool the LLM asked for in one
                # turn. Results ARE interleaved in wall-clock but we rebuild
                # ``history`` in the original ``tool_calls`` order so the
                # next LLM turn sees tool_call_id → result in sequence (the
                # OpenAI API requires that order for tool messages).
                #
                # Cap concurrency with ``FERAL_MAX_PARALLEL_TOOLS`` (default 6).
                # Set to 1 to fall back to strict sequential execution.
                parallel_cap = max(1, int(os.environ.get("FERAL_MAX_PARALLEL_TOOLS", "6")))
                sem = asyncio.Semaphore(parallel_cap)

                async def _run_tool(tc: dict) -> dict:
                    async with sem:
                        await self._emit_tool_start(session_id, tc)
                        t_start = time.time()
                        result_data = await self._execute_tool_call_for_llm(session_id, tc, relevant_skills)
                        latency_ms = (time.time() - t_start) * 1000
                        await self._emit_tool_result(session_id, tc, result_data, latency_ms)
                        return {
                            "tc": tc,
                            "result": result_data,
                            "latency_ms": latency_ms,
                        }

                tool_outputs = await asyncio.gather(*[_run_tool(tc) for tc in tool_calls])

                for tool_output in tool_outputs:
                    tc = tool_output["tc"]
                    result_data = tool_output["result"]
                    latency_ms = tool_output["latency_ms"]

                    tool_success = bool(result_data.get("success") or result_data.get("status") == "command_sent_to_hardware_daemon")
                    await self._emit_brain_event(session_id, "tool_exec", {"tool": tc["name"], "success": tool_success})

                    if self._tool_genesis:
                        self._tool_genesis.record_tool_call(session_id, tc["name"], tc.get("args", {}))

                    if self.memory:
                        parts = tc["name"].split("__", 1)
                        skill_id = parts[0] if len(parts) == 2 else tc["name"]
                        endpoint_id = parts[1] if len(parts) == 2 else ""
                        await self.memory.log_execution(
                            session_id=session_id,
                            skill_id=skill_id,
                            endpoint_id=endpoint_id,
                            args=tc.get("args", {}),
                            result_status="success" if tool_success else "failure",
                            result_summary=json.dumps(result_data)[:300],
                            latency_ms=latency_ms,
                        )

                    history.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "name": tc["name"],
                        "content": json.dumps(result_data, default=str)[:2000]
                    })
                    anti_loop_guidance = result_data.get("_anti_loop_guidance")
                    if anti_loop_guidance:
                        history.append({"role": "system", "content": anti_loop_guidance})

                    if (
                        tc["name"].startswith("messaging_channels__send")
                        and bool(result_data.get("success"))
                    ):
                        history.append({
                            "role": "system",
                            "content": (
                                "The message was delivered successfully via the channel's API. "
                                "Do NOT describe what the user should do, and do NOT re-send. "
                                "Reply with ONE short confirmation sentence (e.g. 'Sent.' or "
                                "'Delivered to @handle on Telegram.')."
                            ),
                        })

                if self._mitosis_engine:
                    tools_used = [tc["name"] for tc in tool_calls]
                    self._mitosis_engine.observe_interaction(session_id, text, tools_used)
            elif text_content:
                if self.memory:
                    self.memory.working_push(session_id, {"role": "assistant", "text": text_content[:300]})
                    await self._emit_brain_event(session_id, "memory_write", {"type": "episodic"})

                await self._send_text(session_id, text_content)
                sent_response = True
                break
            else:
                break

        if not sent_response:
            await self._send_text(session_id, "I processed your request but have nothing to report.")

        self.conversation_history[session_id] = history[-self._conversation_max_per_session:]
        self._evict_stale_sessions()

        if self.learner:
            asyncio.ensure_future(self.learner.on_message(session_id, "user", text))

        # Phase 3 (audit-r10) — persist primary thread snapshot so the
        # operator's last 50 turns survive brain restart.
        self._maybe_snapshot_primary(session_id)
        # F2 — fire compaction when this session crosses
        # ``turns_threshold`` since its last compaction (async, no
        # block).
        self._maybe_auto_compact(session_id)

    async def handle_command_stream(self, session_id: str, text: str, context: Optional[dict] = None):
        """Streaming variant of handle_command with a per-session lock."""
        try:
            async with self._get_session_lock(session_id):
                return await self._handle_command_stream_impl(session_id, text, context)
        finally:
            # W17: tear down subagents tied to this parent session.
            self._w17_cancel_subsessions_nowait(session_id)

    async def _handle_command_stream_impl(self, session_id: str, text: str, context: Optional[dict] = None):
        """
        Streaming variant of handle_command. Sends text deltas in real-time
        so the client gets token-by-token output.
        Falls back to non-streaming if LLM doesn't support it.
        """
        self._stamp_session_surface(session_id, context)
        if await self._maybe_handle_pending_tool_approval_text(session_id, text):
            return

        if not self._streaming_enabled or not self.llm.available:
            await self.handle_command(session_id, text, context)
            return

        if self._somatic_engine:
            self._somatic_engine.update_interaction(session_id, len(text))

        # WS1 — episode_save fire-and-forget on the stream path too.
        # See ``_save_episode_async`` docstring.
        self._save_episode_async(
            session_id=session_id,
            event_type="user_command",
            summary=text[:200],
            detail=json.dumps(context or {}),
        )

        relevant_skills = await self._route_prompt(text)
        relevant_skills = self._ensure_core_skills(relevant_skills)

        specialist = None
        try:
            if self._mitosis_engine and hasattr(self._mitosis_engine, "route_to_specialist"):
                specialist = self._mitosis_engine.route_to_specialist(text, session_id)
        except Exception as exc:
            logger.debug("mitosis routing skipped (stream): %s", exc)
        if specialist:
            allowed = set(specialist.tool_permissions or [])
            narrowed = [s for s in relevant_skills if s.skill_id in allowed or self._skill_endpoint_in_set(s, allowed)]
            if narrowed:
                relevant_skills = self._ensure_core_skills(narrowed)
            logger.info("[%s] stream routed to specialist %s", session_id[:8], specialist.agent_id)

        tools = self.skills.get_tools_for_skills(relevant_skills)

        if self._mcp_client:
            mcp_tools = self._mcp_client.to_llm_tool_definitions()
            if mcp_tools:
                tools = (tools or []) + mcp_tools

        perception_frame = self.perception.get_frame(session_id)
        # When a specialist is routing this turn, thread its memory_filter
        # into context retrieval so cross-domain memory leaks stop. Empty
        # string = generalist turn (no filtering, legacy behaviour).
        active_memory_filter = (specialist.memory_filter if specialist else "") or ""
        system_prompt = await self._build_system_prompt(
            perception_frame,
            relevant_skills,
            session_id,
            memory_filter=active_memory_filter,
            query=text or "",
        )
        if specialist:
            system_prompt = self._build_specialist_system_prompt(specialist, system_prompt)

        if session_id not in self.conversation_history:
            self.conversation_history[session_id] = []

        user_content = perception_frame.to_llm_user_content(text)
        self.conversation_history[session_id].append({"role": "user", "content": user_content})
        history = self._compact_context(self.conversation_history[session_id].copy())
        from models.protocol import StreamDeltaPayload

        got_final_text = False
        any_tool_ran = False
        refusal_retry_used = False
        empty_retry_used = False
        pending_retry_addition: Optional[str] = None
        if self.refusal_handler.is_ack_execution(text):
            pending_retry_addition = self.refusal_handler.ACK_EXECUTION_FAST_PATH_INSTRUCTION
        for _ in range(self._max_iterations):
            effective_system_prompt = system_prompt
            if pending_retry_addition:
                effective_system_prompt = (
                    f"{system_prompt}\n\n[RETRY_STEER]\n{pending_retry_addition}"
                )
                pending_retry_addition = None
            messages = [{"role": "system", "content": effective_system_prompt}, *history]
            stream_id = str(uuid4())[:8]
            accumulated_text = ""
            streamed_text = False
            tool_calls_received = []

            try:
                stream_model = getattr(self.llm, 'model_name', 'llm')
                await self._emit_brain_event(session_id, "llm_call", {"model": stream_model})
                async for delta in self.llm.chat_stream(messages=messages, tools=tools if tools else None):
                    if delta["type"] == "text_delta":
                        piece = delta.get("content", "")
                        if not piece:
                            continue
                        streamed_text = True
                        accumulated_text += piece
                        await self.send(session_id, FeralMessage(
                            session_id=session_id, hop="brain", type="stream_delta",
                            payload=StreamDeltaPayload(
                                delta=piece, stream_id=stream_id, is_final=False,
                            ).model_dump(),
                        ))
                    elif delta["type"] == "tool_call_delta":
                        tc = delta.get("tool_call") or {}
                        if tc:
                            tool_calls_received.append(tc)
                    elif delta["type"] == "done":
                        if streamed_text:
                            await self.send(session_id, FeralMessage(
                                session_id=session_id, hop="brain", type="stream_delta",
                                payload=StreamDeltaPayload(
                                    delta="", stream_id=stream_id, is_final=True,
                                ).model_dump(),
                            ))
                    elif delta["type"] == "error":
                        await self._send_text(session_id, f"Stream error: {delta.get('content', 'unknown')}")
                        return
            except Exception as e:
                logger.error(f"Streaming failed, falling back: {e}")
                # The stream path already appended this turn's user
                # row to ``conversation_history``. ``handle_command``
                # will append it again, duplicating the turn. Drop
                # the trailing user row here so the non-stream
                # fallback re-adds exactly one copy.
                try:
                    hist = self.conversation_history.get(session_id) or []
                    if hist and hist[-1].get("role") == "user":
                        hist.pop()
                except Exception:
                    pass
                await self.handle_command(session_id, text, context)
                return

            normalized_tool_calls = [
                tc for tc in tool_calls_received
                if isinstance(tc, dict) and tc.get("name")
            ]

            # Never-stall: empty response — no visible text, no tool calls.
            if not accumulated_text and not normalized_tool_calls and not empty_retry_used:
                empty_retry_used = True
                logger.warning("[%s] Streaming empty response; prompt-addition retry", session_id[:8])
                pending_retry_addition = self.refusal_handler.EMPTY_RESPONSE_RETRY_INSTRUCTION
                continue

            stream_plan_only = (
                accumulated_text
                and not normalized_tool_calls
                and self._query_implies_action(text)
                and self.refusal_handler.is_plan_only(accumulated_text)
            )
            if accumulated_text and not normalized_tool_calls and (
                self._is_refusal_text(accumulated_text) or stream_plan_only
            ):
                if not refusal_retry_used:
                    refusal_retry_used = True
                    logger.warning(
                        "[%s] Streaming %s detected; forcing act-now retry (prompt-addition)",
                        session_id[:8],
                        "plan-only" if stream_plan_only else "refusal",
                    )
                    pending_retry_addition = self.refusal_handler.planning_only_retry_instruction(text)
                    continue
                logger.warning(
                    "[%s] Streaming refusal/plan-only persisted after retry; falling back to direct execution",
                    session_id[:8],
                )
                handled = await self._execute_action_intent_fallback(session_id, text, relevant_skills)
                if not handled:
                    gap_result = await self._on_capability_gap(session_id, text, relevant_skills)
                    if gap_result and gap_result.get("handled"):
                        logger.info(
                            "[%s] (stream) capability_gap handled via autonomy=%s",
                            session_id[:8], gap_result.get("mode"),
                        )
                        return
                    await self._direct_execute(session_id, text, relevant_skills)
                return

            if accumulated_text or normalized_tool_calls:
                assistant_msg = {"role": "assistant"}
                if accumulated_text:
                    assistant_msg["content"] = accumulated_text
                if normalized_tool_calls:
                    assistant_msg["tool_calls"] = [
                        {
                            "id": tc.get("id", str(uuid4())[:8]),
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": json.dumps(tc.get("args", {})),
                            },
                        }
                        for tc in normalized_tool_calls
                    ]
                history.append(assistant_msg)

            if normalized_tool_calls:
                any_tool_ran = True
                for tc in normalized_tool_calls:
                    await self._emit_tool_start(session_id, tc)
                    t_start = time.time()
                    result_data = await self._execute_tool_call_for_llm(session_id, tc, relevant_skills)
                    latency_ms = (time.time() - t_start) * 1000

                    stream_tool_success = bool(result_data.get("success") or result_data.get("status") == "command_sent_to_hardware_daemon")
                    await self._emit_tool_result(session_id, tc, result_data, latency_ms)
                    await self._emit_brain_event(session_id, "tool_exec", {"tool": tc["name"], "success": stream_tool_success})

                    if self._tool_genesis:
                        self._tool_genesis.record_tool_call(session_id, tc["name"], tc.get("args", {}))

                    if self.memory:
                        parts = tc["name"].split("__", 1)
                        skill_id = parts[0] if len(parts) == 2 else tc["name"]
                        endpoint_id = parts[1] if len(parts) == 2 else ""
                        await self.memory.log_execution(
                            session_id=session_id, skill_id=skill_id,
                            endpoint_id=endpoint_id, args=tc.get("args", {}),
                            result_status="success" if stream_tool_success else "failure",
                            result_summary=json.dumps(result_data)[:300],
                            latency_ms=latency_ms,
                        )
                    history.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", str(uuid4())[:8]),
                        "name": tc["name"],
                        "content": json.dumps(result_data, default=str)[:2000],
                    })
                    anti_loop_guidance = result_data.get("_anti_loop_guidance")
                    if anti_loop_guidance:
                        history.append({"role": "system", "content": anti_loop_guidance})

                    await self._try_genui_for_result(session_id, tc, result_data)

                    if (
                        tc["name"].startswith("messaging_channels__send")
                        and bool(result_data.get("success"))
                    ):
                        history.append({
                            "role": "system",
                            "content": (
                                "The message was delivered successfully via the channel's API. "
                                "Do NOT describe what the user should do, and do NOT re-send. "
                                "Reply with ONE short confirmation sentence (e.g. 'Sent.' or "
                                "'Delivered to @handle on Telegram.')."
                            ),
                        })

                if self._mitosis_engine:
                    tools_used = [tc["name"] for tc in normalized_tool_calls]
                    self._mitosis_engine.observe_interaction(session_id, text, tools_used)
                continue

            if accumulated_text:
                got_final_text = True
                if self.memory:
                    self.memory.working_push(session_id, {"role": "assistant", "text": accumulated_text[:300]})
                    await self._emit_brain_event(session_id, "memory_write", {"type": "episodic"})
                break

            break

        if not got_final_text and not any_tool_ran:
            # Only surface the placeholder when the turn truly
            # produced nothing — no streamed text AND no tool
            # execution. Tool-only turns already emitted tool_start /
            # tool_result chips plus any SDUI from results, so a
            # canned "no text response" bubble would be noise.
            await self._send_text(session_id, "I processed your request but have no text response.")

        self.conversation_history[session_id] = history[-self._conversation_max_per_session:]
        self._evict_stale_sessions()
        if self.learner:
            asyncio.ensure_future(self.learner.on_message(session_id, "user", text))

        # Phase 3 (audit-r10) — stream path snapshot, symmetric with
        # the non-stream `_handle_command_impl` epilogue.
        self._maybe_snapshot_primary(session_id)
        self._maybe_auto_compact(session_id)

    # ─────────────────────────────────────────────
    # Proactive Agent Loop
    # ─────────────────────────────────────────────

    async def check_proactive_triggers(self, session_id: str):
        """
        Called periodically. Examines context changes and decides whether
        to proactively act without user prompt.
        """
        if not self._proactive_enabled or not self.llm.available:
            return

        now = time.time()
        last = self._last_proactive_check.get(session_id, 0)
        if now - last < self._proactive_cooldown:
            return
        self._last_proactive_check[session_id] = now

        frame = self.perception.get_frame(session_id)
        alerts = []

        if frame.heart_rate > 150:
            alerts.append(f"HEALTH ALERT: User heart rate is {frame.heart_rate} BPM — critically elevated.")
        if frame.spo2_pct and frame.spo2_pct < 90:
            alerts.append(f"HEALTH ALERT: SpO2 is {frame.spo2_pct}% — dangerously low.")
        if frame.battery_pct < 10:
            alerts.append(f"DEVICE: Battery critically low at {frame.battery_pct}%.")

        if not alerts:
            return

        alert_text = " ".join(alerts)
        logger.info(f"[{session_id[:8]}] Proactive trigger: {alert_text}")

        # WS1 — proactive alert save is also fire-and-forget; the
        # synthesized command below is what carries the actual work.
        self._save_episode_async(
            session_id=session_id,
            event_type="proactive_alert",
            summary=alert_text,
            importance=0.9,
        )

        await self.handle_command(
            session_id=session_id,
            text=f"[SYSTEM PROACTIVE ALERT] {alert_text} Take appropriate action and notify the user.",
            context={"source": "proactive", "alerts": alerts},
        )

    def _evict_stale_sessions(self):
        """Evict oldest conversation sessions when the dict exceeds max size."""
        if len(self.conversation_history) <= self._conversation_max_sessions:
            return
        sorted_sids = sorted(
            self.conversation_history,
            key=lambda sid: len(self.conversation_history[sid]),
        )
        to_remove = len(self.conversation_history) - self._conversation_max_sessions
        for sid in sorted_sids[:to_remove]:
            del self.conversation_history[sid]
            # Drop the per-session lock too so long-running brains don't
            # grow the lock dict without bound.
            self._session_locks.pop(sid, None)
            self._session_surfaces.pop(sid, None)

    async def on_session_disconnect(self, session_id: str):
        """Called when a client disconnects. Summarize and learn."""
        if session_id in self._session_finalized:
            return
        self._session_finalized.add(session_id)
        if self.learner:
            await self.learner.extract_knowledge(session_id)
            await self.learner.summarize_session(session_id)
        self.conversation_history.pop(session_id, None)
        self._last_proactive_check.pop(session_id, None)
        self._session_locks.pop(session_id, None)
        self._session_surfaces.pop(session_id, None)
        self.tool_runner.clear_session(session_id)

    # ─────────────────────────────────────────────
    # Skill Routing
    # ─────────────────────────────────────────────

    # ─────────────────────────────────────────────
    # Heuristic-first routing (Lane 08 WS2)
    # ─────────────────────────────────────────────
    #
    # AUDIT-r14 finding 20 fix #2 and AUDIT-r13 finding 6.3 documented
    # the regression: ``_route_prompt`` used to fire a primary-model
    # LLM call (Opus / GPT-4) on every chat turn to pick which skills
    # the LLM should see, even when the user said "hi". The user
    # experience was a 1-5s "brain idle" before any token streamed.
    #
    # ``_route_prompt`` is now heuristic-first. The LLM is consulted
    # only when the heuristic signal is genuinely ambiguous, and even
    # then through ``LLMProvider.route_call`` at the ``cheap`` tier
    # (Wave 2 Lane 09 R-CONTRACT-001). The expected no-LLM coverage on
    # the canned production prompt corpus is ≥ 70%.
    #
    # ## Heuristic table (each row exits without an LLM call)
    #
    # | Match                              | Action                                        |
    # |------------------------------------|-----------------------------------------------|
    # | Empty / whitespace-only            | Return []                                     |
    # | ``/<skill>`` prefix                | Direct map to that skill                      |
    # | Memory recall regex (R_MEMORY)     | Map to ``notes_memory``                       |
    # | Calendar query regex (R_CALENDAR)  | Map to ``calendar_google``                    |
    # | Reminder verb regex (R_REMINDER)   | Map to ``feral_reminders``                    |
    # | Health query regex (R_HEALTH)      | Map to ``health_data``                        |
    # | Vision query regex (R_VISION)      | Map to ``perception_query``                   |
    # | Code/search prefix                 | Map to ``code_interpreter``/``web_search``    |
    # | Strong trigger match (score ≥ 20)  | Use registry's keyword/trigger ranking        |
    # | Confident lead (top1 ≥ 1.5× top2)  | Use registry's ranking                        |
    # | Action verb + no skill match       | Expose all skills (existing fallback)         |
    # | Skill catalog ≤ 5                  | Use registry's ranking unconditionally        |
    #
    # Only when none of the above apply does the orchestrator call
    # ``LLMProvider.route_call(call_site="routing", tier="cheap")``.

    # Explicit prefix → skill_id map. ``"/calendar what's tomorrow"``
    # routes straight to calendar_google without further parsing.
    _ROUTE_PREFIX_MAP: dict[str, str] = {
        "/memory": "notes_memory",
        "/note": "notes_memory",
        "/notes": "notes_memory",
        "/calendar": "calendar_google",
        "/cal": "calendar_google",
        "/remind": "feral_reminders",
        "/reminder": "feral_reminders",
        "/health": "health_data",
        "/search": "web_search",
        "/google": "web_search",
        "/code": "code_interpreter",
        "/run": "code_interpreter",
        "/screen": "screen_capture",
        "/vision": "perception_query",
        "/see": "perception_query",
    }

    # Memory-recall phrasings — "what did I do yesterday", etc.
    _R_MEMORY = re.compile(
        r"\b("
        r"what\s+did\s+i\s+(?:do|say|ask|save|note)|"
        r"summari[sz]e\s+(?:today|yesterday|my\s+week|my\s+day)|"
        r"what\s+(?:was|were)\s+(?:we\s+)?(?:talking|chatting)\s+about|"
        r"recall(?:\s+(?:that|the))?|"
        r"do\s+you\s+remember|"
        r"my\s+notes(?:\b|\s+from)|"
        r"what\s+did\s+we\s+discuss"
        r")\b",
        re.I,
    )

    # Calendar phrasings — explicit time + schedule words.
    _R_CALENDAR = re.compile(
        r"\b("
        r"(?:what'?s|do\s+i\s+have)\s+on\s+(?:my\s+)?(?:calendar|agenda|schedule)|"
        r"my\s+(?:calendar|agenda|schedule)\s+(?:for\s+)?(?:today|tomorrow|next\s+week|this\s+week)|"
        r"next\s+meeting|"
        r"upcoming\s+(?:meetings?|events?)|"
        r"am\s+i\s+free|"
        r"schedule\s+a\s+(?:meeting|call|event)"
        r")\b",
        re.I,
    )

    # Reminder verbs — "remind me to ..."
    _R_REMINDER = re.compile(
        r"\b("
        r"remind\s+me\s+to|"
        r"set\s+a?\s*reminder|"
        r"create\s+a?\s*reminder|"
        r"list\s+(?:my\s+)?reminders"
        r")\b",
        re.I,
    )

    # Health / vitals — "what's my heart rate", "how did I sleep"
    _R_HEALTH = re.compile(
        r"\b("
        r"(?:what'?s|show\s+me)\s+my\s+(?:heart\s+rate|hrv|spo2|vitals?|sleep)|"
        r"how\s+(?:did|was)\s+(?:my\s+)?sleep|"
        r"how\s+(?:recovered|ready)\s+am\s+i|"
        r"(?:my\s+)?(?:recovery|readiness|strain)"
        r")\b",
        re.I,
    )

    # Visual perception — "what do I see", "what is in front of me"
    _R_VISION = re.compile(
        r"\b("
        r"what\s+(?:do\s+i|am\s+i)\s+(?:see(?:ing)?|looking\s+at)|"
        r"what(?:'s|\s+is)\s+(?:in\s+front\s+of\s+me|on\s+the\s+(?:screen|table))|"
        r"describe\s+(?:the\s+)?(?:scene|view|room)"
        r")\b",
        re.I,
    )

    def _heuristic_route(self, text: str) -> tuple[list["SkillManifest"], str]:
        """Try to pick relevant skills without an LLM call.

        Returns ``(skills, reason)`` where ``reason`` is one of
        ``"empty"``, ``"prefix"``, ``"regex:<name>"``,
        ``"trigger_strong"``, ``"confident_lead"``,
        ``"action_fallback"``, ``"small_catalog"``, or ``"ambiguous"``
        (the only value that triggers an LLM disambiguation call).

        The orchestrator's primary contract: ``"ambiguous"`` is the
        ONLY exit that fires an LLM call. Every other return value is
        a free routing decision.
        """
        if not text or not text.strip():
            return ([], "empty")
        if not self.skills.skills:
            return ([], "empty")

        stripped = text.strip()

        # 1. Explicit ``/skill`` prefix. The slash form is intentional
        # — it can't collide with normal English.
        first_token = stripped.split(maxsplit=1)[0].lower() if stripped else ""
        if first_token.startswith("/"):
            mapped = self._ROUTE_PREFIX_MAP.get(first_token)
            if mapped and mapped in self.skills.skills:
                return ([self.skills.skills[mapped]], "prefix")

        # 2. Regex-driven memory / calendar / reminder / health / vision shortcuts.
        for pattern, sid, label in (
            (self._R_MEMORY, "notes_memory", "regex:memory"),
            (self._R_CALENDAR, "calendar_google", "regex:calendar"),
            (self._R_REMINDER, "feral_reminders", "regex:reminder"),
            (self._R_HEALTH, "health_data", "regex:health"),
            (self._R_VISION, "perception_query", "regex:vision"),
        ):
            if pattern.search(stripped) and sid in self.skills.skills:
                return ([self.skills.skills[sid]], label)

        # 3. Catalog ≤ 5 — registry's keyword ranking is always
        # enough; LLM routing would be more expensive than just
        # exposing the whole list.
        if len(self.skills.skills) <= 5:
            return (self._fallback_skills_for_query(stripped, top_k=5), "small_catalog")

        # 4. Trigger-phrase / category ranking from the registry.
        ranked = self.skills.find_skills_for_query(stripped, top_k=5)
        if ranked:
            # Re-score to get the raw numbers — ``find_skills_for_query``
            # returns sorted skills but not scores. We approximate by
            # re-running the trigger check on the top result; the
            # registry's exact-match score is 25, "contained in" is
            # 20, "contains" is 15. Anything ≥ 20 = a strong signal.
            top = ranked[0]
            top_score = self._trigger_score(stripped, top)
            if top_score >= 20.0:
                return (ranked, "trigger_strong")

            # Confident lead: top1 score / max(top2 score, 1.0) ≥ 1.5
            # The registry already sorted; we just need the second
            # skill's score for the ratio.
            if len(ranked) >= 2:
                second_score = self._trigger_score(stripped, ranked[1])
                if top_score >= 5.0 and (
                    second_score == 0.0 or top_score / second_score >= 1.5
                ):
                    return (ranked, "confident_lead")
            elif top_score >= 5.0:
                # Only one match in the catalog — clear winner.
                return (ranked, "confident_lead")

            # Weak signal — fall through to ambiguous.

        # 5. Action verbs with no skill match — surface every tool
        # (preserves the prior "imply action → expose all" behaviour).
        if self._query_implies_action(stripped):
            return (list(self.skills.skills.values()), "action_fallback")

        return (ranked or [], "ambiguous")

    @staticmethod
    def _trigger_score(query: str, skill: "SkillManifest") -> float:
        """Compute the same trigger-phrase score the registry uses.

        Mirrors ``SkillRegistry.find_skills_for_query`` so we can read
        a numeric "confidence" without re-implementing the registry.
        Phrases are matched case-insensitive; the registry's tiers
        (exact 25, contained 20, contains 15, partial words 3×count)
        are preserved here.
        """
        q = query.lower().strip()
        q_words = set(q.split())
        best = 0.0
        for phrase in getattr(skill, "trigger_phrases", []) or []:
            p = phrase.lower()
            if p == q:
                best = max(best, 25.0)
            elif p in q:
                best = max(best, 20.0)
            elif q in p:
                best = max(best, 15.0)
            else:
                p_words = set(p.split())
                overlap = p_words & q_words
                if overlap:
                    ratio = len(overlap) / max(len(p_words), 1)
                    best = max(best, len(overlap) * 3.0 * ratio)
        # Category bonus mirrors the registry too.
        for cat in getattr(skill, "categories", []) or []:
            if cat.lower() in q:
                best += 5.0
        return best

    async def _route_prompt(self, text: str) -> list["SkillManifest"]:
        """Pick which skills the LLM sees this turn.

        WS2 — heuristic-first; LLM disambiguation through
        ``LLMProvider.route_call(call_site="routing", tier="cheap")``
        only when the heuristic exit is ``"ambiguous"`` (see
        ``_heuristic_route`` table above).
        """
        if not self.skills.skills:
            return []

        # Pre-WS2 behaviour preserved when the LLM is unavailable:
        # heuristic is the only available choice.
        if not getattr(self.llm, "available", False):
            results, _reason = self._heuristic_route(text)
            if not results:
                results = self._fallback_skills_for_query(text, top_k=5)
            return await self._apply_routing_penalties(results)

        skills, reason = self._heuristic_route(text)
        if reason != "ambiguous":
            logger.debug(
                "route_prompt heuristic exit=%s skills=%s",
                reason, [s.skill_id for s in skills[:5]],
            )
            return await self._apply_routing_penalties(skills)

        # ── Ambiguous: ask the cheap-tier LLM to disambiguate ────
        provider_ref: dict[str, Any] = {}
        try:
            provider_ref = self.llm.route_call(
                call_site="routing", prompt=text, tier="cheap",
            ) or {}
        except Exception as exc:
            logger.debug("route_call refused, falling back to heuristic: %s", exc)
            return await self._apply_routing_penalties(skills)

        # Build a tight prompt — the catalog inflates token usage so
        # we pass the description in short form. The cheap tier
        # returns a JSON list of skill ids.
        catalog_lines = [
            f"- {sid}: {sk.description}"
            for sid, sk in self.skills.skills.items()
        ]
        prompt = (
            "You are a Semantic Tool Router. Pick up to 5 skill_ids "
            "relevant to the user's query.\nAvailable skills:\n"
            + "\n".join(catalog_lines)
            + f"\n\nUser Query: {text}\n"
            "Output ONLY a JSON list of skill_id strings. [] if none "
            "are clearly relevant. No prose. No markdown."
        )

        try:
            # The router instructed which (provider, model) to use for
            # the routing call; we surface it via the chat call's
            # ``call_site`` so the budget gate bills against routing,
            # not chat.
            response = await self.llm.chat_with_failover(
                [{"role": "user", "content": prompt}],
                tools=None,
                call_site="routing",
                model=provider_ref.get("model") or None,
            )
        except TypeError:
            # ``call_site`` / ``model`` kwargs may not be supported by
            # every adapter shape used in tests. Fall through to the
            # legacy positional form.
            response = await self.llm.chat_with_failover(
                [{"role": "user", "content": prompt}], tools=None,
            )
        except Exception as exc:
            logger.warning("RoutePrompt LLM call failed: %s", exc)
            return await self._apply_routing_penalties(skills)

        try:
            text_content, _ = self.llm.extract_response(response)
        except Exception:
            text_content = ""

        cleaned = (text_content or "").strip()
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:-3].strip()
        elif cleaned.startswith("```"):
            cleaned = cleaned[3:-3].strip()
        try:
            skill_ids = json.loads(cleaned) if cleaned else []
        except Exception:
            skill_ids = []

        relevant: list["SkillManifest"] = []
        for sid in skill_ids:
            if isinstance(sid, str) and sid in self.skills.skills:
                relevant.append(self.skills.skills[sid])

        results = relevant[:5] if relevant else (skills or self._fallback_skills_for_query(text, top_k=5))
        return await self._apply_routing_penalties(results)

    def _ensure_core_skills(self, skills: list[SkillManifest]) -> list[SkillManifest]:
        """Guarantee core skills like desktop_control are always available to the LLM."""
        existing_ids = {s.skill_id for s in skills}
        for core_id in self.ALWAYS_INCLUDE_SKILLS:
            if core_id not in existing_ids and core_id in self.skills.skills:
                skills.append(self.skills.skills[core_id])
        return skills

    def _fallback_skills_for_query(self, text: str, top_k: int = 5) -> list[SkillManifest]:
        results = self.skills.find_skills_for_query(text, top_k=top_k)
        if results:
            return results
        if self._query_implies_action(text):
            logger.info("RoutePrompt fallback: action-like query with no strong match, exposing all tools")
            return list(self.skills.skills.values())
        return results

    async def _apply_routing_penalties(self, skills: list[SkillManifest]) -> list[SkillManifest]:
        """Re-rank skills based on execution log reliability."""
        if not self.learner or not skills:
            return skills

        penalties = await self.learner.get_routing_penalties()
        if not penalties:
            return skills

        penalized = []
        for skill in skills:
            penalty = penalties.get(skill.skill_id, 1.0)
            if penalty < 0.2:
                logger.info(f"Routing penalty: skipping {skill.skill_id} (penalty={penalty})")
                continue
            penalized.append(skill)

        return penalized if penalized else skills[:1]

    # ─────────────────────────────────────────────
    # Confirmation & Capability Growth
    # ─────────────────────────────────────────────

    async def _queue_action_confirmation(
        self,
        session_id: str,
        tool_call: dict,
        available_skills: list[SkillManifest],
        reason: str,
    ) -> None:
        confirmation_id = str(uuid4())[:8]
        self._pending_confirmations[confirmation_id] = {
            "tool_call": {"name": tool_call["name"], "args": tool_call.get("args", {})},
            "skills": available_skills,
            "reason": reason,
            "created_at": time.time(),
        }
        args_preview = json.dumps(tool_call.get("args", {}), default=str)[:400]
        sdui = {
            "type": "VStack",
            "spacing": 12,
            "padding": 20,
            "children": [
                {"type": "Text", "value": "Confirmation Required", "style": "headline", "color": "#f59e0b"},
                {"type": "Text", "value": "This action can change your local system. Confirm to proceed.", "style": "body"},
                {"type": "Text", "value": f"Action: {tool_call['name']}", "style": "caption"},
                {"type": "Text", "value": f"Args: {args_preview}", "style": "caption"},
                {"type": "Text", "value": f"Reason: {reason[:240]}", "style": "caption"},
                {
                    "type": "HStack",
                    "spacing": 10,
                    "children": [
                        {"type": "Button", "action_id": f"confirm_{confirmation_id}", "label": "Confirm", "style": "primary"},
                        {"type": "Button", "action_id": f"reject_{confirmation_id}", "label": "Cancel", "style": "secondary"},
                    ],
                },
            ],
        }
        await self.send(
            session_id,
            FeralMessage(
                session_id=session_id,
                hop="brain",
                type="sdui",
                payload=SDUIPayload(root=sdui).model_dump(),
            ),
        )
        await self._send_text(session_id, "I need your confirmation for this higher-impact action.")

    async def _maybe_auto_expand_capability(self, session_id: str, text: str) -> None:
        """Auto-learn repeated unmet capabilities with safety guardrails."""
        if not text or self._action_text_is_destructive(text):
            return
        if "system_settings" not in self.skills.skills:
            return

        key = self._capability_key(text)
        if not key:
            return

        now = time.time()
        state = self._fallback_learning_state.get(
            key,
            {"count": 0, "last_seen": 0.0, "cooldown_until": 0.0},
        )
        if now - float(state.get("last_seen", 0.0)) <= self._auto_learn_window_seconds:
            state["count"] = int(state.get("count", 0)) + 1
        else:
            state["count"] = 1
        state["last_seen"] = now
        self._fallback_learning_state[key] = state

        if now < float(state.get("cooldown_until", 0.0)):
            return
        if int(state.get("count", 0)) < self._auto_learn_threshold:
            return

        state["cooldown_until"] = now + self._auto_learn_cooldown_seconds
        state["count"] = 0
        self._fallback_learning_state[key] = state

        tool_call = {
            "name": "system_settings__create_skill",
            "args": {
                "capability": text[:260],
                "service": "",
                "auto_approve": True,
                "source": "fallback_loop",
            },
        }
        result = await self._execute_tool_call_for_llm(session_id, tool_call, [])
        if not isinstance(result, dict) or not result.get("success"):
            logger.info("Auto capability growth skipped/failed for key=%s", key)
            return

        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        skill_id = data.get("skill_id", "")
        mode = "ready" if data.get("auto_approved", False) else "pending_approval"
        payload = {
            "skill_id": skill_id,
            "name": data.get("name", skill_id or "new capability"),
            "mode": mode,
            "message": data.get("message", "New capability learned."),
            "source": "auto_growth",
        }
        await self.send(
            session_id,
            FeralMessage(
                session_id=session_id,
                hop="brain",
                type="capability_learned",
                payload=payload,
            ),
        )
        await self._send_text(session_id, payload["message"])

    # ─────────────────────────────────────────────
    # Vision
    # ─────────────────────────────────────────────

    async def request_frame(self, node_id: str, resolution: str = "640x480",
                            quality: int = 80, reason: str = "", timeout: float = 10.0) -> Optional[dict]:
        ws = self.daemons.get(node_id)
        if not ws:
            logger.warning(f"Cannot request frame: node {node_id} not connected")
            return None

        msg_id = str(uuid4())[:8]
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_frame_futures[msg_id] = future

        request_msg = FeralMessage(
            msg_id=msg_id, hop="brain", type="vision_request",
            payload=VisionRequestPayload(resolution=resolution, quality=quality, reason=reason).model_dump(),
        )
        await ws.send_json(request_msg.model_dump())

        try:
            frame = await asyncio.wait_for(future, timeout=timeout)
            return frame
        except asyncio.TimeoutError:
            self._pending_frame_futures.pop(msg_id, None)
            return None

    def resolve_pending_frame(self, msg_id: str, frame_payload: dict):
        future = self._pending_frame_futures.pop(msg_id, None)
        if future and not future.done():
            future.set_result(frame_payload)

    # ─────────────────────────────────────────────
    # Direct Execution (no LLM)
    # ─────────────────────────────────────────────

    async def _direct_execute(self, session_id: str, text: str, skills: list[SkillManifest]):
        await helper_direct_execute(self, session_id=session_id, text=text, skills=skills)

    # ─────────────────────────────────────────────
    # Memory Direct Mode
    # ─────────────────────────────────────────────

    async def _handle_memory_direct(self, session_id: str, text: str, skill):
        await helper_handle_memory_direct(self, session_id=session_id, text=text, _skill=skill)

    async def _handle_daemon_direct(self, session_id: str, text: str, skill):
        await helper_handle_daemon_direct(self, session_id=session_id, text=text, _skill=skill)

    def _extract_args_from_text(self, text: str, endpoint) -> dict:
        return helper_extract_args_from_text(text=text, endpoint=endpoint)

    # ─────────────────────────────────────────────
    # UI Events & Daemon Results
    # ─────────────────────────────────────────────

    async def handle_ui_event(self, session_id: str, action_id: str, event: str, value=None, app_id: str | None = None, screen_id: str | None = None):
        await helper_handle_ui_event(
            self,
            session_id=session_id,
            action_id=action_id,
            event=event,
            value=value,
            app_id=app_id,
            screen_id=screen_id,
        )

    async def send_permission_request(self, session_id: str, path: str, operation: str, reason: str = "") -> None:
        await helper_send_permission_request(
            self,
            session_id=session_id,
            path=path,
            operation=operation,
            reason=reason,
        )

    async def _handle_permission_response(self, session_id: str, req_id: str, granted: bool, value=None) -> None:
        await helper_handle_permission_response(
            self,
            session_id=session_id,
            req_id=req_id,
            granted=granted,
            value=value,
        )

    async def handle_daemon_result(self, node_id: str, result: dict, session_id: str = None):
        await helper_handle_daemon_result(self, node_id=node_id, result=result, session_id=session_id)

    # ─────────────────────────────────────────────
    # Response Helpers
    # ─────────────────────────────────────────────

    async def _send_text(self, session_id: str, text: str):
        await helper_send_text(self, session_id=session_id, text=text)

    async def _try_send_sdui(self, session_id: str, text: str):
        await helper_try_send_sdui(self, session_id=session_id, text=text)

    async def _try_genui_for_result(self, session_id: str, tool_call: dict, result_data: dict):
        await helper_try_genui_for_result(self, session_id=session_id, tool_call=tool_call, result_data=result_data)
