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
    TimelinePayload,
    ToolResultPayload,
    ToolStartPayload,
    VisionRequestPayload,
    stamp_hup_envelope,
)
from models.skill_manifest import SkillManifest
from memory.execution_audit import claimed_by_caller, status_of as audit_status_of
from skills.registry import SkillRegistry
from skills.executor import SkillExecutor
from skills.availability import filter_unavailable_tools
from skills.result_budget import (
    serialize_tool_result,
    serialize_tool_result_with_images,
)
from agents.multimodal_blocks import (
    IMAGE_DELIVERY_NONE,
    image_delivery_mode,
    materialize_tool_result_images,
    prune_tool_result_images,
    should_prune_images,
)
from agents.llm_provider import LLMProvider
from agents import llm_router
from agents.genui_generator import GenUIGenerator
from perception.fusion import PerceptionEngine, PerceptionFrame

# Sub-modules — orchestrator delegates to these focused classes
from agents.tool_runner import ToolRunner
from security.dangerous_tools import resolve_surface_from_context
from agents.context_manager import ContextManager, _chars_from
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
    send_error as helper_send_error,
    send_text as helper_send_text,
    try_genui_for_result as helper_try_genui_for_result,
    try_send_sdui as helper_try_send_sdui,
)
from agents.llm_provider import llm_response_error
from agents.multi_agent import MultiAgentProviderError
from agents.turn_attribution import (
    accumulate_turn_usage as _accumulate_turn_usage,
    model_of_llm_response as _model_of_llm_response,
)

if TYPE_CHECKING:
    from api.server import VisionBuffer
    from memory.store import MemoryStore
    from agents.learner import Learner
    from agents.multi_agent import MultiAgentOrchestrator

logger = logging.getLogger("feral.orchestrator")


def _smart_loops_enabled() -> bool:
    """Whether the smart-loops tools (feral_routines / feral_workflows) are
    auto-exposed to the LLM. Defaults ON; ``FERAL_SMART_LOOPS=0`` is the kill
    switch (mirrors the FERAL_GENERIC_HARDWARE_SKILLS pattern)."""
    val = os.environ.get("FERAL_SMART_LOOPS", "1")
    return str(val).strip().lower() not in ("0", "false", "no", "off", "")


class Orchestrator:
    """
    The core agentic loop — fully wired to perception, memory, and safety.

    Heavy lifting is delegated to:
      - ToolRunner      – tool dispatch, safety, anti-loop, subagents
      - ContextManager   – conversation history compaction
      - RefusalHandler   – LLM refusal detection and fallback execution
      - IdentityLoader   – ~/.feral/ identity files → system prompt
    """

    # Class-level constants kept on Orchestrator for backward compat.
    #
    # This is a tuple, not a set, on purpose. ``_ensure_core_skills`` appends
    # in iteration order, and a set iterates in hash order, which varies with
    # PYTHONHASHSEED across processes. That made the tail of the tool list
    # (and therefore whatever any future cap would drop) differ from boot to
    # boot. A tuple pins a single deterministic priority order.
    #
    # ``"browser"`` used to sit in here and matched no registered manifest,
    # so it was a silent no-op swallowed by the ``in self.skills.skills``
    # guard in ``_ensure_core_skills``. Removed.
    ALWAYS_INCLUDE_SKILLS = (
        # Core OS / desktop surface.
        # ``coding_tools`` replaced ``computer_use`` here: the two skills
        # exposed identical endpoints with identical trigger phrases, so the
        # model saw two indistinguishable ``bash`` tools and picked
        # arbitrarily. ``coding_tools`` is the better implementation
        # (paginated grep/glob, workspace-relative output) and is now the
        # single canonical shell + filesystem surface.
        "coding_tools",
        "desktop_control",
        # ``gui_computer_use`` is the canonical synthetic mouse/keyboard
        # surface and the ONLY one carrying ``screenshot``,
        # ``window_list`` and ``window_focus``. It was missing from this
        # list while ``desktop_automation``, an eight-endpoint
        # compatibility shim that delegates every call straight back to
        # ``gui_computer_use`` (skills/impl/desktop_automation.py), was
        # in it. Same shape as the ``computer_use`` problem noted above:
        # the two manifests shared nine byte-identical trigger phrases
        # ("click on", "type text", "move mouse", …), which produced a
        # 20.0/20.0 scoring tie on every realistic phrasing, so the model
        # was shown two indistinguishable ``type_text`` tools and the
        # canonical one was the one that could fall out of the top-5.
        # The shim keeps its endpoint ids (persona files and older
        # callers hardcode them) but no longer competes for routing.
        "gui_computer_use",
        "desktop_automation",
        "screen_capture",
        "system_settings",
        "agentic_computer_use",
        # Messaging / comms, always reachable so the agent never claims it "can't send"
        "messaging_channels",
        # Never-say-no escape hatches + self-knowledge
        "workspace_scripts",
        "self_introspection",
        # Memory + search
        "notes_memory",
        "web_search",
        # Smart loops: recurring routines + multi-step workflows as
        # first-class tools (gated by FERAL_SMART_LOOPS, default on, via
        # _ensure_core_skills below).
        "feral_routines",
        "feral_workflows",
    )

    # Smart-loops skill ids whose *exposure* can be killed with
    # ``FERAL_SMART_LOOPS=0`` without touching the rest of the always-include
    # surface. Mirrors the FERAL_GENERIC_HARDWARE_SKILLS kill-switch pattern.
    _SMART_LOOPS_SKILLS = {"feral_routines", "feral_workflows"}

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
        # When a turn last finished, and how full the context window was
        # the last time one was built. Nothing measured either, so the
        # dashboard could not answer "when did this thing last do
        # anything" or "how much room is left", which are the two
        # questions the design's Brain readout asks. Both are recorded
        # off paths that already run per turn rather than adding work.
        self._last_turn_at: float = 0.0
        self._last_context_chars: int = 0
        self.refusal_handler = RefusalHandler(self)
        self.identity_loader = IdentityLoader(memory=memory)

        # State
        self.biometric_state: dict[str, dict] = {}
        # The FULL transcript per session, bounded only by
        # ``_conversation_max_per_session``. The window the LLM sees is
        # derived from this per request by ``_compact_context`` and is
        # never written back — see ``_finalize_turn``.
        self.conversation_history: dict[str, list[dict]] = {}
        self._conversation_max_per_session = 200
        self._conversation_max_sessions = 500
        # Per-session async lock. Two concurrent turns on the SAME session
        # used to race on `conversation_history` + the outgoing tool
        # ordering. Different sessions still run fully parallel — only
        # turns on the same session are serialised.
        self._session_locks: dict[str, asyncio.Lock] = {}
        # Image-bearing tool results (screenshots) travel OUT OF BAND.
        #
        # ``conversation_history`` stays pure text and provider-agnostic:
        # the base64 blob is never written into a history row, so the
        # transcript can be replayed on any provider, handed to the
        # memory compactor, or persisted without carrying megabytes of
        # image. The images live here, keyed by ``tool_call_id``, and are
        # spliced back in -- in the shape the SELECTED provider accepts --
        # only at the moment a chat request is built
        # (``_materialize_tool_images``).
        #
        #   _tool_result_images[session_id][tool_call_id] =
        #       {"images": [ToolResultImage.to_dict(), ...],
        #        "pruned": bool, "tool_name": str}
        #   _tool_image_order[session_id] = [tool_call_id, ...]  (append order)
        #   _tool_image_rounds[session_id] = agent rounds since session start
        self._tool_result_images: dict[str, dict[str, dict]] = {}
        self._tool_image_order: dict[str, list[str]] = {}
        self._tool_image_rounds: dict[str, int] = {}
        # Per-session stack of in-flight turn records. See the
        # "Turn write-back" block below. Stacked because the stream
        # path can delegate to the non-stream path mid-turn.
        self._active_turns: dict[str, list[dict]] = {}
        # CI-flake fix: track every fire-and-forget background task the
        # orchestrator schedules (episode_save, temporal-timeline
        # side-channel, etc.) so tests can drain them deterministically
        # via ``drain_background_tasks()`` instead of scanning
        # ``asyncio.all_tasks()`` with a magic timeout. Production
        # behaviour is unchanged — callers still do not await these
        # tasks; the set just holds a strong reference until the task
        # finishes (preventing the GC-warning on never-awaited tasks)
        # and the ``add_done_callback`` discards the entry on
        # completion so the set never grows unbounded.
        self._background_tasks: set[asyncio.Task] = set()
        # Event-loop affinity (live-voice "different event loop" fix).
        #
        # The orchestrator's stateful asyncio primitives — the memory
        # store's aiosqlite connection pool (an ``asyncio.Queue`` that
        # binds itself to the loop on first contended ``get``), the
        # ``_session_locks`` dict, the daemon-result futures — were
        # all opened on whatever loop first touched them at brain
        # boot (typically the main FastAPI / uvicorn loop). Any
        # subsequent caller that lands on a DIFFERENT loop (the
        # cron/routine path uses ``asyncio.new_event_loop()`` in a
        # daemon thread; an embed of the brain into another async
        # runtime could re-host the orchestrator) and then schedules
        # a fire-and-forget ``episode_save`` via the new device-action
        # logging hook in :meth:`_emit_tool_result` triggers
        # ``RuntimeError: <Queue ...> is bound to a different event
        # loop`` the first time the runner has to wait on the pool —
        # which is what the user perceives on the realtime voice path
        # as "the command isn't going through, there's a misalignment
        # in the event loop". The error is caught in :meth:`_save_episode_async`'s
        # runner so the tool itself doesn't fail, but the
        # device_action episode never lands and the brain (correctly)
        # reports the failure.
        #
        # ``_owning_loop`` is captured the first time any background
        # scheduler sees a running loop. Subsequent foreign-loop
        # scheduling is routed back to the owning loop via
        # ``call_soon_threadsafe`` so the runner runs on the loop
        # that owns the memory pool. See :meth:`_save_episode_async`.
        self._owning_loop: Optional[asyncio.AbstractEventLoop] = None
        # Per-session execution surface, populated from handle_command's
        # context dict. Threaded into ToolRunner.enforce_safety so
        # surface deny-lists fire on the actual invocation surface
        # instead of the historical "websocket" default.
        self._session_surfaces: dict[str, str] = {}
        # B7: wall-clock of the last activity on each session, the key
        # ``_evict_stale_sessions`` sorts by.
        #
        # There was no per-session timestamp before, so the cap sorted by
        # ``len(conversation_history[sid])`` and evicted the SHORTEST
        # transcripts. Short means "just started" far more often than it
        # means "finished", so the cap reliably deleted the session the
        # operator was mid-conversation on and kept the abandoned ones.
        # Written at both ends of a turn (start and finalisation) and on
        # every live-voice row; dropped by eviction and by
        # ``on_session_disconnect`` so it cannot outgrow the dict it
        # describes.
        self._session_last_active: dict[str, float] = {}
        # F2 — turns-since-last-compaction counter per session.
        # ``_maybe_auto_compact`` increments after every full turn and
        # fires ``memory.compact_session`` once it crosses
        # ``settings.memory.compaction.turns_threshold`` (default 20),
        # at which point the counter resets. ``_compaction_inflight``
        # gates against overlapping compactions on the same session.
        self._turns_since_compaction: dict[str, int] = {}
        self._compaction_inflight: dict[str, bool] = {}
        # F6: the other two rungs of the consolidation ladder.
        # ``_pending_since`` is when the OLDEST un-consolidated turn in
        # this session arrived, which is what the hard deadline is
        # measured against. ``_session_last_turn_at`` is the per-session
        # idle clock: ``_last_turn_at`` above is process-wide and is
        # read only for status reporting, so it cannot answer "is THIS
        # session quiet".
        self._pending_since: dict[str, float] = {}
        self._session_last_turn_at: dict[str, float] = {}
        self._consolidation_task: Optional[asyncio.Task] = None
        self._consolidation_stop: Optional[asyncio.Event] = None
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

        # Multi-agent. The default is NOT a literal here: it is
        # imported from ``config.loader`` so this line and
        # ``DEFAULT_SETTINGS["features"]["multi_agent"]`` cannot drift
        # apart again. They did, in the opposite direction from
        # streaming: this read defaulted to "false" (4df5fc1cc, April
        # 2026) while the settings default was True and
        # ``export_as_env`` published FERAL_MULTI_AGENT=true into
        # os.environ, so whether the multi-agent branch answered a turn
        # depended on whether the loader had run first in the process.
        # See ``DEFAULT_MULTI_AGENT`` in config/loader.py for the full
        # history and why ON is the resolved answer.
        from config.loader import DEFAULT_MULTI_AGENT
        self._multi_agent_enabled = os.environ.get(
            "FERAL_MULTI_AGENT", str(DEFAULT_MULTI_AGENT).lower()
        ).lower() in ("true", "1", "yes")
        self._multi_agent: Optional["MultiAgentOrchestrator"] = None

        # Vision config
        self._vision_enabled = os.environ.get("FERAL_VISION_ENABLED", "").lower() in ("true", "1", "yes")

        # Proactive loop config
        self._proactive_enabled = os.environ.get("FERAL_PROACTIVE", "").lower() in ("true", "1", "yes")
        self._last_proactive_check: dict[str, float] = {}
        self._proactive_cooldown = 60.0

        # Streaming config. The default is NOT a literal here: it is
        # imported from ``config.loader`` so this line and
        # ``DEFAULT_SETTINGS["features"]["streaming"]`` cannot drift
        # apart again. They did: this read defaulted to "true" while
        # the settings default was False and ``export_as_env``
        # published FERAL_STREAMING=false into os.environ, so whether
        # streaming was on depended on whether the config loader had
        # run in this process. An explicit FERAL_STREAMING still wins
        # over both, which is what the Settings toggle and an operator
        # shell export rely on.
        from config.loader import DEFAULT_STREAMING
        self._streaming_enabled = os.environ.get(
            "FERAL_STREAMING", str(DEFAULT_STREAMING).lower()
        ).lower() in ("true", "1", "yes")
        # v2026.6.11 — tool loops are UNLIMITED by default (0). A limit is
        # a user-set option only: settings.json ``agents.max_tool_iterations``
        # or the FERAL_MAX_ITERATIONS env var. Runaway loops are stopped by
        # the no-progress guard + a generous configurable wall-clock backstop
        # instead of an arbitrary count (see agents/iteration_budget.py).
        from agents.iteration_budget import (
            resolve_max_tool_iterations,
            resolve_tool_loop_max_seconds,
        )
        self._max_iterations = resolve_max_tool_iterations()
        self._tool_loop_max_seconds = resolve_tool_loop_max_seconds()

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

    def set_subdevice_store(self, store, live_node_ids=None):
        """Wire the NodeSubdeviceStore so the prompt names real hardware.

        `frame.connected_nodes` gives the prompt HUP node ids and
        nothing more. The peripherals behind those nodes -- the W300
        glasses and VITRO wristband arriving over BLE through the
        iPhone companion -- live only in `node_subdevices`, which the
        dashboard, `/api/devices/connected` and the iOS UI all read and
        the prompt builder did not. On the audited install that table
        held 7 rows across 6 iPhone nodes while the model's entire view
        of attached hardware was the string
        `Connected devices: ['feral-iphone-6053b3cdc4ed']`.

        Same shape as `set_calendar` above and for the same reason: a
        capability the brain owns is useless until the prompt carries
        it, because the LLM will not call a tool for a fact it has no
        reason to believe exists.

        `live_node_ids` is an optional zero-arg callable returning the
        node ids currently holding a HUP WebSocket. It is a callable and
        not a snapshot because the set changes between prompt builds,
        and a stale snapshot would put "connected" in the prompt for a
        phone that dropped an hour ago -- which is the defect the owner
        reported.
        """
        self.identity_loader.subdevice_store = store
        self.identity_loader.live_node_ids = live_node_ids

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
                orchestrator=self,
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
            # 0.0 when no turn has run in this process, which a caller
            # must read as "unknown" rather than "just now".
            "last_turn_at": self._last_turn_at,
            "context_used_pct": self.context_used_pct(),
        }

    def context_used_pct(self) -> float:
        """How full the last context view was, 0..100, or 0.0 if unknown.

        Measured against `history_budget_chars`, which is the share of
        the model's window the conversation is allowed to occupy, not
        the whole window: the rest is the system prompt, tool schemas
        and the memory block, so dividing by the full window would
        report a number that is always comfortable and never true.
        """
        try:
            budget = int(self.context_manager.history_budget_chars)
            if budget <= 0 or self._last_context_chars <= 0:
                return 0.0
            return round(min(100.0, (self._last_context_chars / budget) * 100.0), 1)
        except Exception:
            return 0.0

    # ─────────────────────────────────────────────
    # Subagent spawn (additive)
    # Single additive method; behaviour of every existing handler is
    # unchanged. Spawning is gated by ``agents.subagent_policy`` and
    # audited via the supervisor.
    # ─────────────────────────────────────────────

    async def spawn_subsession(
        self,
        parent_session_id: str,
        kind: str,
        *,
        scope_key: str,
        model_override: Optional[str] = None,
    ) -> str:
        """Spawn a child subsession of *parent_session_id* ().

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
        """Sync hook called from the session-lock teardown path ().

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
        view = self.context_manager.compact(history)
        # Measure the VIEW, not the stored history: the view is what is
        # actually sent to the model, so it is the thing that can
        # overflow the window. Measured here because this already runs
        # once per request and the numbers are otherwise unobservable
        # from any surface.
        try:
            self._last_context_chars = _chars_from(view, 0)
        except Exception:
            # A reporting number is never worth failing a turn over.
            self._last_context_chars = 0
        return view

    # ─────────────────────────────────────────────
    # F6: the consolidation trigger ladder
    # ─────────────────────────────────────────────
    #
    # Consolidate when the session goes QUIET, but also when the
    # backlog crosses a soft bound, and ALWAYS by a hard deadline,
    # whichever fires first.
    #
    # The deadline is not defensive padding, it is the load-bearing
    # part. An idle-only trigger STARVES: a session that is never idle
    # never consolidates, and the sessions that most need consolidating
    # are exactly the busy ones. The W3C requestIdleCallback spec
    # states this failure mode normatively and answers it by racing the
    # idle callback against a timeout. The same ladder (free background
    # work -> throttle -> forced inline) is what RocksDB's write
    # stalls, Postgres autovacuum's freeze age, Linux writeback's
    # dirty_expire_centisecs and Go's GC forced-cycle all implement.
    #
    # The two evaluation points are deliberate:
    #   * backlog and deadline are evaluated ON EVERY TURN, in
    #     ``_maybe_auto_compact``, so a busy session cannot starve even
    #     if the background loop was never started.
    #   * idle is evaluated by ``_consolidation_tick`` on a background
    #     cadence, because by definition no turn arrives to trigger it.
    # The tick re-checks the deadline too, so a session that went quiet
    # just under the deadline is still covered.

    _DEFAULT_IDLE_SECONDS = 90.0
    _DEFAULT_MAX_PENDING_SECONDS = 900.0
    _DEFAULT_MIN_TURNS = 4
    _DEFAULT_SCHEDULER_CADENCE = 30.0

    def _compaction_cfg(self) -> dict | None:
        """The ``memory.compaction`` settings block, or None when
        compaction is disabled / unreadable."""
        try:
            from config.loader import load_settings
            settings = load_settings()
            cfg = (settings.get("memory") or {}).get("compaction") or {}
        except Exception:
            return None
        if not cfg.get("enabled", True):
            return None
        return cfg

    def _maybe_auto_compact(self, session_id: str) -> None:
        """Post-turn hook: record the turn, then evaluate the ladder.

        Called from the post-turn save sites in ``handle_command``
        and ``_handle_command_stream_impl``. Runs in fire-and-forget
        mode so the user-visible turn isn't blocked on compaction.
        Idempotent against overlapping invocations via
        ``_compaction_inflight``.

        The backlog bound is still ``memory.compaction.turns_threshold``
        and still defaults to 20, so existing settings.json files keep
        the behaviour they had. What is new is that crossing it is no
        longer the ONLY way a session ever consolidates.
        """
        now = time.time()
        # A turn just finished. Recorded here because this is already
        # the post-turn hook on BOTH the streaming and non-streaming
        # paths, so it cannot drift out of step with one of them, and
        # recorded BEFORE the settings read below: compaction being
        # disabled returns early, and a turn still happened.
        self._last_turn_at = now
        self._session_last_turn_at[session_id] = now

        cfg = self._compaction_cfg()
        if cfg is None:
            return
        threshold = int(cfg.get("turns_threshold", 20))
        if threshold <= 0:
            return

        pending = self._turns_since_compaction.get(session_id, 0) + 1
        self._turns_since_compaction[session_id] = pending
        self._pending_since.setdefault(session_id, now)

        reason = self._consolidation_reason(session_id, cfg, now, threshold)
        if reason:
            self._schedule_compaction(session_id, reason)

    def _consolidation_reason(
        self, session_id: str, cfg: dict, now: float, threshold: int
    ) -> str:
        """Which rung of the ladder fires for this session, or "".

        Reads ``memory.compaction.turns_threshold`` (via *threshold*),
        ``memory.compaction.min_turns``,
        ``memory.compaction.max_pending_seconds`` and
        ``memory.compaction.idle_seconds`` off *cfg*.
        """
        pending = self._turns_since_compaction.get(session_id, 0)
        if pending <= 0:
            return ""

        # Backlog, soft. The historical trigger, unchanged.
        if pending >= threshold:
            return "backlog"

        min_turns = int(cfg.get("min_turns", self._DEFAULT_MIN_TURNS))
        if pending < max(1, min_turns):
            # Below this there is nothing worth a generation:
            # ``compact_session`` itself no-ops under preserve_last_n+2.
            return ""

        # Deadline, hard. Evaluated on the turn path as well as the
        # tick, so continuous traffic cannot outrun it.
        max_pending = float(cfg.get("max_pending_seconds", self._DEFAULT_MAX_PENDING_SECONDS))
        since = self._pending_since.get(session_id)
        if max_pending > 0 and since is not None and (now - since) >= max_pending:
            return "deadline"

        # Idle, debounced. Only meaningful from the background tick,
        # but harmless to evaluate here (a turn just landed, so the
        # session is by definition not idle).
        idle_seconds = float(cfg.get("idle_seconds", self._DEFAULT_IDLE_SECONDS))
        last_turn = self._session_last_turn_at.get(session_id)
        if idle_seconds > 0 and last_turn is not None and (now - last_turn) >= idle_seconds:
            return "idle"

        return ""

    def _consolidation_tick(self, now: float | None = None) -> list[str]:
        """Evaluate idle + deadline for every session with a backlog.

        Returns the session ids it scheduled, which is what makes the
        ladder testable without a running loop. Never raises: this runs
        on a background cadence and a bad settings file must not kill
        the loop.
        """
        now = time.time() if now is None else now
        cfg = self._compaction_cfg()
        if cfg is None:
            return []
        threshold = int(cfg.get("turns_threshold", 20))
        if threshold <= 0:
            return []

        fired: list[str] = []
        for session_id in list(self._turns_since_compaction.keys()):
            try:
                reason = self._consolidation_reason(session_id, cfg, now, threshold)
                if reason and self._schedule_compaction(session_id, reason):
                    fired.append(session_id)
            except Exception as exc:
                logger.warning(
                    "consolidation tick failed for session %s: %s", session_id, exc
                )
        return fired

    async def start_consolidation_scheduler(self) -> None:
        """Start the background cadence that evaluates the idle rung.

        Shape copied from ``memory.decay.MemoryDecayService._loop``:
        a stop Event, ``wait_for`` on it as the sleep so shutdown is
        immediate, and an exception inside the body that is logged and
        stepped over rather than allowed to kill the loop for the rest
        of the process lifetime.

        Cadence comes from ``memory.compaction.scheduler_cadence_seconds``
        and is re-read every iteration, so an operator changing it does
        not need a restart.
        """
        if self._consolidation_task is not None and not self._consolidation_task.done():
            return
        self._consolidation_stop = asyncio.Event()
        # Closed over as a local, not read back off self each pass.
        # The attribute is Optional and mypy cannot narrow it across a
        # closure, correctly: something could reassign it mid-flight.
        # Binding here also makes the loop respond to the event it was
        # started with rather than whichever one happens to be on the
        # instance later, which is the behaviour a restart wants.
        stop = self._consolidation_stop

        async def _loop() -> None:
            while not stop.is_set():
                cfg = self._compaction_cfg() or {}
                cadence = float(
                    cfg.get("scheduler_cadence_seconds", self._DEFAULT_SCHEDULER_CADENCE)
                )
                try:
                    await asyncio.wait_for(
                        stop.wait(),
                        timeout=max(0.01, cadence),
                    )
                    return  # stop was set
                except asyncio.TimeoutError:
                    pass
                try:
                    self._consolidation_tick()
                except Exception as exc:
                    logger.exception("consolidation scheduler tick failed: %s", exc)

        self._consolidation_task = asyncio.create_task(
            _loop(), name="consolidation-scheduler"
        )
        self._track_background_task(self._consolidation_task)

    async def stop_consolidation_scheduler(self) -> None:
        """Cancel the cadence task and await its termination. Safe when
        it was never started."""
        stop = getattr(self, "_consolidation_stop", None)
        if stop is not None:
            stop.set()
        task = getattr(self, "_consolidation_task", None)
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        self._consolidation_task = None

    def _forget_consolidation_state(self, session_id: str) -> None:
        """Drop every per-session consolidation clock and counter.

        Called when the transcript goes away. ``_turns_since_compaction``
        was already leaked by eviction and disconnect before the ladder
        existed; it matters more now, because ``_consolidation_tick``
        walks that dict and would keep scheduling compactions for a
        session whose history no longer exists.
        """
        self._turns_since_compaction.pop(session_id, None)
        self._pending_since.pop(session_id, None)
        self._session_last_turn_at.pop(session_id, None)
        self._compaction_inflight.pop(session_id, None)

    def _schedule_compaction(self, session_id: str, reason: str) -> bool:
        """Fire-and-forget one compaction. True when it was scheduled."""
        if self._compaction_inflight.get(session_id):
            return False
        if not self.memory:
            return False

        async def _run() -> None:
            self._compaction_inflight[session_id] = True
            try:
                # Snapshot under the lock, summarize OUTSIDE it, then
                # reconcile under the lock again.
                #
                # The lock exists because the body REPLACES
                # ``conversation_history[session_id]``; held naively it
                # raced the turn write-back and could drop a whole
                # turn. But it used to wrap the summarization too, and
                # ``handle_command`` / ``handle_command_stream`` take
                # that same per-session lock for the whole turn. So a
                # compaction that took twenty seconds on a local model
                # blocked the operator's NEXT message for twenty
                # seconds. The scheduling turn returned immediately,
                # which is what made this look like it was off the hot
                # path; the stall simply landed one turn later.
                #
                # Nothing about summarization needs the lock. It reads
                # a list it was handed and returns a new one. Only the
                # swap needs exclusivity, and the turns that arrive
                # while the model is working are recoverable by
                # position: everything past the snapshot length is new,
                # and is re-appended after the compacted prefix.
                async with self._get_session_lock(session_id):
                    history = list(self.conversation_history.get(session_id, []))
                    snapshot_len = len(history)
                if not history:
                    # The transcript is gone (evicted, disconnected,
                    # reset). Clear the bookkeeping here or the
                    # background tick, which iterates
                    # ``_turns_since_compaction``, retries this ghost
                    # session on every cadence for the life of the
                    # process.
                    self._forget_consolidation_state(session_id)
                    return

                result = await self.memory.compact_session(
                    session_id, history, llm=self.llm,
                )

                async with self._get_session_lock(session_id):
                    if result.get("compacted") and result.get("history"):
                        current = self.conversation_history.get(session_id, [])
                        if len(current) >= snapshot_len:
                            # Turns that landed mid-compaction. They are
                            # NOT in the summary, so they are carried
                            # over verbatim rather than dropped.
                            arrived_during = list(current[snapshot_len:])
                            self.conversation_history[session_id] = (
                                list(result["history"]) + arrived_during
                            )
                        else:
                            # The transcript shrank under us (eviction,
                            # a reset, another writer). Position-based
                            # reconciliation is meaningless now, so the
                            # live history is left exactly as it is.
                            # The episode was still written, so the
                            # consolidation is not lost.
                            logger.warning(
                                "auto-compact: transcript shrank during "
                                "summarization (%d -> %d); keeping live history",
                                snapshot_len, len(current),
                            )
                    self._turns_since_compaction[session_id] = 0
                    # The backlog clock restarts from the next turn, not
                    # from now: a session with nothing pending has no
                    # deadline to miss.
                    self._pending_since.pop(session_id, None)
                logger.info(
                    "auto-compact (%s): session=%s episode_id=%s entities=%s",
                    reason,
                    session_id,
                    result.get("episode_id"),
                    result.get("key_entities", []),
                )
            except Exception as exc:
                logger.warning("auto-compact (%s) failed: %s", reason, exc)
            finally:
                self._compaction_inflight[session_id] = False

        # Claimed SYNCHRONOUSLY, not at the top of ``_run``. There are
        # now two callers (the post-turn hook and the background tick)
        # and the window between "task created" and "task body starts"
        # is a real one, so claiming the session inside the coroutine
        # would let both schedule the same compaction. ``_run`` still
        # sets it too, which keeps the invariant api/server.py
        # documents true.
        self._compaction_inflight[session_id] = True
        try:
            # AUDIT-FIXES F-06: ensure_future has the same weak-reference
            # hazard as create_task. A collected compaction task leaves
            # ``_compaction_inflight[session_id]`` stuck True, because the
            # flag is only cleared in _run's finally, so the session never
            # compacts again for the life of the process.
            self._track_background_task(asyncio.ensure_future(_run()))
            return True
        except RuntimeError:
            # No running loop (sync context, a closed test loop). Nothing
            # will ever clear the flag we just set, so release it here or
            # the session never compacts again.
            self._compaction_inflight[session_id] = False
            return False

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
        plan_mode = self.plan_mode.is_active(session_id)
        if plan_mode:
            # Same prune as the active list. Without it the "Available
            # (full catalog)" block re-advertises every mutating endpoint
            # in the install, one paragraph after we said they are off.
            from agents.plan_mode import filter_skills_for_plan_mode
            full_catalog = filter_skills_for_plan_mode(
                full_catalog, registry=self.skills,
            )
        return await self.identity_loader.build_system_prompt(
            frame,
            skills,
            session_id,
            identity_text=self._load_identity(),
            full_catalog=full_catalog,
            memory_filter=memory_filter,
            query=query,
            plan_mode=plan_mode,
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
                {"role": "assistant", "text": summary},
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

    # ─────────────────────────────────────────────
    # Device/robot action provenance (timeline gap fix)
    # ─────────────────────────────────────────────
    #
    # Physical-device skills (CuteBot, robot arm, smart-home) executed
    # the requested action but recorded NOTHING durable — only the
    # user's command *text* was saved as a ``user_command`` episode in
    # ``handle_command``. So when the user later asked "what did my
    # robot do today?", ``notes_memory__fused_timeline`` had no entry
    # describing the action itself and the brain truthfully answered
    # "I don't have logs of that" even though the robot had been busy.
    #
    # Writing the *result* of every device action here — the one
    # tool-result hook that all three dispatch paths funnel through
    # (approved-pending, non-stream, stream) — closes that gap without
    # scattering ``episode_save`` calls across every adapter.
    _DEVICE_ACTION_SKILLS: tuple[str, ...] = (
        "cutebot",
        "robot_ext",
        "robot_arm",
        "smart_home",
        "smart_home_hue",
    )
    # Read-only endpoints are telemetry/status polls, not actions —
    # logging them would bury the real activity ("drove", "set lights")
    # under a stream of status noise.
    _DEVICE_READ_ENDPOINTS: frozenset = frozenset({
        "status",
        "read_telemetry",
        "telemetry",
        "get_entities",
        "get_entity_state",
        "get_state",
        "read",
        "list",
    })
    _DEVICE_LABELS: dict = {
        "cutebot": "CuteBot",
        "robot_ext": "Robot arm",
        "robot_arm": "Robot arm",
        "smart_home": "Smart home",
        "smart_home_hue": "Smart home",
    }

    def _device_action_episode_fields(
        self, tool_call: dict, result_data: dict
    ) -> Optional[tuple]:
        """Return ``(summary, detail)`` for a successful, loggable device
        action — or ``None`` when the tool call is not a physical action
        worth a timeline entry (wrong skill, read-only endpoint, or the
        action did not succeed)."""
        name = str(tool_call.get("name") or "")
        if "__" not in name:
            return None
        skill_id, _, endpoint = name.partition("__")
        if skill_id not in self._DEVICE_ACTION_SKILLS:
            return None
        if endpoint in self._DEVICE_READ_ENDPOINTS:
            return None
        if not isinstance(result_data, dict):
            return None
        success = bool(
            result_data.get("success")
            or result_data.get("status") == "command_sent_to_hardware_daemon"
        )
        if not success:
            return None

        args = tool_call.get("args")
        args = args if isinstance(args, dict) else {}
        data = result_data.get("data")
        data = data if isinstance(data, dict) else {}
        verified = data.get("verified")

        device_label = self._DEVICE_LABELS.get(skill_id, skill_id)
        arg_bits = ", ".join(
            f"{k}={v}"
            for k, v in list(args.items())[:4]
            if not isinstance(v, (dict, list))
        )
        summary = f"{device_label}: {endpoint}"
        if arg_bits:
            summary += f" ({arg_bits})"
        if verified is True:
            summary += " — verified"
        elif verified is False:
            summary += " — UNVERIFIED"

        detail = json.dumps(
            {"tool": name, "args": args, "verified": verified},
            default=str,
        )[:2000]
        return summary, detail

    @staticmethod
    def _refusal_code(result_data: dict) -> str:
        """Classify a DECLINED tool result for the client. "" if it ran.

        Three gates refuse a call and each returns a different envelope,
        because they were written at different times:

        * plan mode already carries ``error_code`` (``plan_mode_blocked``)
        * a surface/policy deny returns ``status`` ``PermissionOutcome::Deny``
        * strict/hybrid autonomy returns ``status`` ``pending_approval``

        All three arrive at the UI as ``success: False``, which is how a
        deliberate refusal came to render identically to a crash. Rather
        than rewrite three envelopes the LLM also consumes, the shapes are
        normalised here, at the one place that builds the client frame.

        Only these three map to a code. Anything else is a real failure and
        must keep the error treatment, so a future refusal shape shows up as
        a loud failure rather than being silently softened.
        """
        code = str(result_data.get("error_code") or "")
        if code == "plan_mode_blocked":
            return code
        status = str(result_data.get("status") or "")
        if status == "PermissionOutcome::Deny" or result_data.get("safety_level") == "deny":
            return "policy_denied"
        if status == "pending_approval":
            return "pending_approval"
        return ""

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
            err_code = ""
            if isinstance(result_data, dict):
                err = str(result_data.get("error") or "")[:240]
                err_code = self._refusal_code(result_data)
            tool_name = str(tool_call.get("name", "tool"))
            # UI result excerpt, opt-in per endpoint. See
            # skills/result_budget.preview_enabled_for for why this is
            # opt-in and how the marketplace trust clamp applies.
            preview, preview_truncated = "", False
            try:
                from skills.result_budget import (
                    build_result_preview,
                    preview_enabled_for_tool,
                )
                if preview_enabled_for_tool(tool_name, self.skills):
                    preview, preview_truncated = build_result_preview(result_data)
            except Exception:
                logger.debug("result preview build failed (non-fatal)", exc_info=True)
            await self.send(session_id, FeralMessage(
                session_id=session_id, hop="brain", type="tool_result",
                payload=ToolResultPayload(
                    tool=tool_name,
                    call_id=str(tool_call.get("id", "")),
                    success=success,
                    error=err,
                    error_code=err_code,
                    latency_ms=float(latency_ms or 0.0),
                    result_preview=preview,
                    result_preview_truncated=preview_truncated,
                ).model_dump(),
            ))
        except Exception:
            # This used to be a bare `pass`, which made a failed emit
            # invisible: the client never got `tool_result`, so the WebUI
            # tool chip spun forever with nothing in the logs or the UI to
            # explain it. `tool_result` is the frame that *closes* the chip,
            # so losing it is a user-visible hang — warning, not debug, and
            # with the traceback so the cause is diagnosable. The three
            # sibling handlers in this function stay at debug because their
            # frames are enrichment; this one is the terminator.
            logger.warning(
                "tool_result frame emit failed — the UI tool chip for %r "
                "will not resolve",
                str(tool_call.get("name", "tool")),
                exc_info=True,
            )

        # S1 closer (cut-list item #8): the fused-timeline tool returns
        # a structured {entries, summary, window, sources_queried,
        # degraded_sources} payload. Emit it as a dedicated ``timeline``
        # WS frame so the WebUI's TimelineCard can mount immediately
        # — in parallel with whatever prose the LLM is still streaming.
        try:
            await self._maybe_emit_timeline_frame(session_id, tool_call, result_data)
        except Exception:
            logger.debug("timeline frame emit failed (non-fatal)", exc_info=True)

        # Same pattern for the agent's todo list. The panel is pinned and
        # rewritten in place rather than appended as a card per write,
        # because the model rewrites the whole list constantly and a card
        # per write buries the conversation.
        try:
            await self._maybe_emit_todo_frame(session_id, tool_call, result_data)
        except Exception:
            logger.debug("todo frame emit failed (non-fatal)", exc_info=True)

        # Persist what the robot actually DID (not just what the user
        # asked) so temporal recall can answer "what did my robot do
        # today?" with grounded entries. Fire-and-forget; never blocks
        # or raises on the tool-result hot path.
        try:
            fields = self._device_action_episode_fields(tool_call, result_data)
            if fields is not None:
                summary, detail = fields
                self._save_episode_async(
                    session_id=session_id,
                    event_type="device_action",
                    summary=summary,
                    detail=detail,
                    importance=0.6,
                )
        except Exception:
            logger.debug(
                "device-action episode log failed (non-fatal)", exc_info=True
            )

    # The one tool whose results the client renders as a pinned panel
    # instead of a ToolCallCard. See `_maybe_emit_todo_frame`.
    TODO_WRITE_TOOL = "feral_workflows__todo_write"

    async def _maybe_emit_todo_frame(
        self,
        session_id: str,
        tool_call: dict,
        result_data: dict,
    ) -> None:
        """Emit a ``todo_update`` frame iff the call was a todo write.

        Field-tolerant like its timeline sibling: a malformed payload
        falls through silently rather than blocking the response loop.
        """
        if str(tool_call.get("name") or "") != self.TODO_WRITE_TOOL:
            return
        if not isinstance(result_data, dict) or not result_data.get("success"):
            return
        data = result_data.get("data")
        if not isinstance(data, dict):
            return
        todos = data.get("todos")
        if not isinstance(todos, list):
            return
        await self.send(session_id, FeralMessage(
            session_id=session_id, hop="brain", type="todo_update",
            payload={
                "session_id": session_id,
                "todos": todos,
                "counts": data.get("counts") or {},
            },
        ))

    async def _maybe_emit_timeline_frame(
        self,
        session_id: str,
        tool_call: dict,
        result_data: dict,
    ) -> None:
        """Emit a ``timeline`` WS frame iff the tool call was a fused-
        timeline dispatch and the result carries the expected shape.

        Field-tolerant by design: a malformed payload falls through
        silently rather than blocking the rest of the response loop.
        """
        if not isinstance(result_data, dict):
            return
        if not result_data.get("success"):
            return
        tool_name = str(tool_call.get("name") or "")
        if not tool_name.endswith("__fused_timeline"):
            return
        data = result_data.get("data")
        if not isinstance(data, dict):
            return
        entries = data.get("entries")
        window = data.get("window")
        if not isinstance(entries, list) or not isinstance(window, dict):
            return
        payload = TimelinePayload(
            session_id=session_id,
            query=str(data.get("query") or tool_call.get("args", {}).get("query") or ""),
            window=window,
            entries=entries,
            summary=str(data.get("summary") or ""),
            sources_queried=list(data.get("sources_queried") or []),
            degraded_sources=list(data.get("degraded_sources") or []),
        )
        await self.send(
            session_id,
            FeralMessage(
                session_id=session_id,
                hop="brain",
                type="timeline",
                payload=payload.model_dump(),
            ),
        )

    # ─────────────────────────────────────────────
    # Live-path side-channel: proactive timeline fusion
    # ─────────────────────────────────────────────
    #
    # The LLM tool-call path above (``_maybe_emit_timeline_frame``)
    # only fires when the model actually dispatches
    # ``*__fused_timeline``. Live testing against claude-opus-4-7
    # showed the model answering "what did I do yesterday?" in prose
    # without invoking the tool — so the TimelineCard never mounted.
    # Routing-closure (regex_:memory_) only EXPOSES the tool; it
    # cannot force the model to call it.
    #
    # The side-channel below closes that gap. When the heuristic
    # detects a temporal-recall query (strict ``_R_TEMPORAL`` subset
    # of ``_R_MEMORY``), the orchestrator dispatches
    # ``timeline_fusion()`` directly, in parallel with the LLM
    # stream, and pushes a ``timeline`` WS frame iff the fusion
    # returned ≥ 1 entry. The prose stream is unaffected — both
    # render. If the LLM ALSO picks the tool (best case), the client
    # de-dupes by ``{session_id, query}`` so the redundant emit just
    # replaces.

    _TEMPORAL_WINDOW_ORDER = (
        # Order matters — more specific phrases first.
        ("this_morning", ("this morning",)),
        ("this_afternoon", ("this afternoon",)),
        ("this_evening", ("this evening", "tonight")),
        ("last_week", ("last week",)),
        ("this_week", ("this week", "past week")),
        ("last_month", ("last month",)),
        ("yesterday", ("yesterday",)),
        ("today", ("today",)),
        ("morning", ("morning",)),
        ("afternoon", ("afternoon",)),
        ("evening", ("evening",)),
    )

    @staticmethod
    def _temporal_window_label_from_text(text: str) -> str:
        """Pick a ``parse_window`` label from natural-language text.

        Returns the canonical label string ``parse_window`` consumes
        (``"yesterday"``, ``"morning"``, ``"last_tuesday"``, etc.).
        Defaults to ``"yesterday"`` when no specific window is
        mentioned — that's the thesis-default and matches
        ``parse_window``'s own fallback.
        """
        if not text:
            return "yesterday"
        t = text.lower()
        for label, keys in Orchestrator._TEMPORAL_WINDOW_ORDER:
            for key in keys:
                if key in t:
                    return label
        for d in (
            "monday", "tuesday", "wednesday", "thursday",
            "friday", "saturday", "sunday",
        ):
            if f"last {d}" in t:
                return f"last_{d}"
        return "yesterday"

    # ─────────────────────────────────────────────
    # Grounded-memory closure (v2026.5.48 follow-up)
    # ─────────────────────────────────────────────
    #
    # The forced-tool name the orchestrator asks the LLM layer to
    # require on temporal-recall turns. Kept as a class constant so
    # tests can introspect (and the orchestrator's guards can confirm
    # the right tool name is in the tool list before forcing).
    _FORCED_TIMELINE_TOOL_NAME = "notes_memory__fused_timeline"
    _FORCED_ROUTINE_TOOL_NAME = "feral_routines__create"
    _FORCED_ROBOT_LIGHTS_TOOL_NAME = "cutebot__set_lights"

    @staticmethod
    def _tool_list_contains(tools: list[dict], target: str) -> bool:
        """True when ``target`` appears in an OpenAI-style tool list."""
        for t in tools:
            if not isinstance(t, dict):
                continue
            fn = t.get("function")
            if isinstance(fn, dict) and fn.get("name") == target:
                return True
            if t.get("name") == target:
                return True
        return False

    def _force_tool_for_query(
        self, text: str, tools: Optional[list[dict]], session_id: str = "",
    ) -> Optional[str]:
        """Return the tool name to force the LLM to call this turn, or
        ``None`` for the default (auto) tool-selection behaviour.

        Live testing on v2026.5.46/47 showed claude-opus-4-7 answering
        temporal-recall queries (``what did I do yesterday?``) in prose
        without dispatching ``notes_memory__fused_timeline`` — so the
        prose stayed un-grounded in the actual stored episodes. The
        v2026.5.46 side-channel rendered the TimelineCard widget
        regardless, but the LLM's narration kept narrating from
        working-memory context instead of the retrieved tool result.
        v2026.5.47 deepened the prompts to nudge the call; this gate
        makes it deterministic at the wire by passing the tool name
        through to per-provider ``tool_choice`` forcing.

        Scheduled-automation queries get the same treatment: when the
        user asks for a recurring or one-shot scheduled device action
        (or is mid-setup after stating the schedule on a prior turn),
        force ``feral_routines__create`` so the brain stops inventing
        "background task" workarounds or refusing to schedule motion.

        Guards (each branch):
          * The query must match the branch's regex / intent gate
            (non-matching turns keep free tool selection).
          * ``tools`` must actually contain the forced tool name —
            never force a tool the model wasn't given.

        Returns the tool name when both gates pass, ``None`` otherwise.
        Temporal recall takes priority when both gates would match.
        The orchestrator uses ``None`` on the temporal path as the
        signal to leave the side-channel timeline-fusion task scheduled.
        """
        if not text or not isinstance(tools, list) or not tools:
            return None
        try:
            if self._R_TEMPORAL.search(text):
                target = self._FORCED_TIMELINE_TOOL_NAME
                if self._tool_list_contains(tools, target):
                    return target
        except Exception:
            pass
        try:
            if self._query_is_robot_lights(text, session_id):
                target = self._FORCED_ROBOT_LIGHTS_TOOL_NAME
                if self._tool_list_contains(tools, target):
                    return target
        except Exception:
            pass
        try:
            stripped = text.strip()
            recurring_hit = self._R_ROUTINE_RECURRING.search(stripped)
            oneshot_hit = self._R_ROUTINE_ONESHOT.search(stripped)
            current_hit = bool(
                recurring_hit
                or (oneshot_hit and self._query_implies_action(stripped))
            )
            carry_hit = bool(
                not current_hit
                and session_id
                and self._query_implies_action(stripped)
                and self._has_pending_routine_intent(session_id)
            )
            if current_hit or carry_hit:
                target = self._FORCED_ROUTINE_TOOL_NAME
                if self._tool_list_contains(tools, target):
                    return target
        except Exception:
            pass
        return None

    async def _maybe_emit_temporal_timeline(
        self, session_id: str, text: str,
    ) -> bool:
        """Dispatch ``timeline_fusion`` proactively on the live chat
        path when the user asked a temporal-recall question, and emit
        a ``timeline`` WS frame iff the fusion returned ≥ 1 entry.

        Returns ``True`` when a frame was emitted, ``False`` otherwise.
        Defensive throughout — every failure path is silent, so the
        side-channel can never break the LLM stream.
        """
        try:
            if not text or not self._R_TEMPORAL.search(text):
                return False
            try:
                from skills.impl.timeline_fusion import (
                    DEFAULT_PER_SOURCE_LIMIT,
                    timeline_fusion,
                )
            except Exception as exc:
                logger.debug(
                    "temporal-timeline side-channel: import failed: %s",
                    exc,
                )
                return False

            memory = self.memory
            calendar = None
            health_aggregator = None
            try:
                from api.state import state as _state
                calendar = getattr(_state, "calendar", None)
                health_aggregator = getattr(_state, "health_aggregator", None)
                if memory is None:
                    memory = getattr(_state, "memory", None)
            except Exception:
                pass

            window_label = self._temporal_window_label_from_text(text)

            try:
                result = await timeline_fusion(
                    query=text,
                    memory=memory,
                    calendar=calendar,
                    health_aggregator=health_aggregator,
                    window_label=window_label,
                    per_source_limit=DEFAULT_PER_SOURCE_LIMIT,
                )
            except Exception as exc:
                logger.debug(
                    "temporal-timeline side-channel: fusion failed: %s",
                    exc,
                )
                return False

            if not isinstance(result, dict):
                return False
            entries = result.get("entries")
            window = result.get("window")
            if not isinstance(entries, list) or not entries:
                return False
            if not isinstance(window, dict):
                return False

            payload = TimelinePayload(
                session_id=session_id,
                query=text,
                window=window,
                entries=entries,
                summary=str(result.get("summary") or ""),
                sources_queried=list(result.get("sources_queried") or []),
                degraded_sources=list(result.get("degraded_sources") or []),
            )
            await self.send(
                session_id,
                FeralMessage(
                    session_id=session_id,
                    hop="brain",
                    type="timeline",
                    payload=payload.model_dump(),
                ),
            )
            return True
        except Exception:
            logger.debug(
                "temporal-timeline side-channel: unexpected error",
                exc_info=True,
            )
            return False

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
    # Turn write-back
    # ─────────────────────────────────────────────
    #
    # The assistant side of a turn used to be written back by exactly
    # ONE statement, at the very bottom of the single-agent text loop.
    # Every early return above it — refusal fallback, budget cap, LLM
    # exception, multi-agent hand-off, stream error — recorded the user
    # row and then jumped over the assistant row. The next turn handed
    # the model two consecutive user messages, and the model correctly
    # answered that it had never spoken ("looks like we got cut off").
    #
    # Fix: every turn opens a record and closes it from a ``finally``,
    # so the write-back cannot be skipped. The record also carries the
    # prose ``_send_text`` actually emitted, so a turn that replied
    # without ever reaching the LLM loop still records what it said.
    #
    # Second invariant: ``conversation_history`` is the FULL transcript.
    # The old code assigned the compacted 15-row window back over it,
    # making truncation permanent and cumulative and rendering
    # ``_conversation_max_per_session`` dead. ``_finalize_turn`` appends
    # this turn's new rows instead.

    def _begin_turn(self, session_id: str, text: str) -> dict:
        """Open a turn record and push it on the session's turn stack."""
        turn = {
            "text": text,
            # True once the body has appended this turn's user row to
            # ``conversation_history``; when False and the turn still
            # produced a reply we record both rows.
            "user_recorded": False,
            # The per-request compacted window plus every row the loop
            # appended to it. ``base_len`` marks the boundary.
            "working": None,
            "base_len": 0,
            # Prose handed to ``_send_text`` during this turn.
            "outbound": [],
            # Explicit final reply for branches that answer without
            # going through the tool loop (multi-agent).
            "reply_text": "",
            # Set when this turn handed the whole exchange to a nested
            # turn, which owns the write-back and the epilogue.
            "delegated": False,
        }
        self._active_turns.setdefault(session_id, []).append(turn)
        return turn

    def _note_outbound_text(self, session_id: str, text: str) -> None:
        """Record prose ``_send_text`` is about to emit against the
        innermost in-flight turn.

        Recorded before the send rather than after: a downstream socket
        failure must not leave the transcript ending on a user row.
        No-op outside a turn — proactive pushes, UI-event replies and
        daemon results are not part of a user/assistant exchange.
        """
        if not text:
            return
        stack = self._active_turns.get(session_id)
        if stack:
            stack[-1]["outbound"].append(text)

    def _turn_rows(self, turn: dict) -> list[dict]:
        """The rows this turn adds to the transcript."""
        working = turn.get("working") or []
        rows = [
            row for row in working[turn.get("base_len", 0):]
            if isinstance(row, dict)
        ]
        spoke = any(
            row.get("role") == "assistant" and row.get("content")
            for row in rows
        )
        if not spoke:
            reply = turn.get("reply_text") or "\n".join(
                t for t in turn.get("outbound", []) if t
            ).strip()
            if reply:
                rows.append({"role": "assistant", "content": reply})
        if rows and not turn.get("user_recorded"):
            rows.insert(0, {"role": "user", "content": turn.get("text", "")})
        return rows

    @staticmethod
    def _assistant_row_text(row: dict) -> str:
        """Plain text of one assistant row, whatever shape it arrived in.

        ``content`` is a string on most providers and a list of typed
        blocks on the Anthropic-style ones, so a naive ``str(content)``
        would persist a repr of a list.
        """
        content = row.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = [
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            return "\n".join(p for p in parts if p).strip()
        return ""

    def _persist_assistant_rows(self, session_id: str, rows: list[dict]) -> None:
        """Write this turn's assistant replies to the episode store.

        Only ``user_command`` was ever persisted per turn, so what the
        agent said had no durable home at all. It lived in
        ``conversation_history`` until ``compact_session`` replaced that
        list with a summary plus the last few turns, and in
        ``conversations.messages_json`` only when a web client was
        attached to autosave it. A voice or CLI session therefore lost
        the agent's own words: "I booked the flight for Tuesday" was
        recoverable only if a summariser happened to keep it, which is
        the one thing a summariser must not be trusted for.

        ``event_type="assistant_reply"`` is in
        ``store._NO_SELF_MODEL_EVENT_TYPES``. These are the agent's
        words, not the operator's, and feeding them to About-Me
        extraction is how a self-model learns facts about itself and
        reports them as facts about the user.
        """
        for row in rows or []:
            if row.get("role") != "assistant":
                continue
            text = self._assistant_row_text(row)
            if not text:
                continue
            self._save_episode_async(
                session_id=session_id,
                event_type="assistant_reply",
                summary=text[:200],
                detail=text,
            )

    def _finalize_turn(self, session_id: str, turn: dict) -> None:
        """Commit the turn's rows and run the post-turn epilogue.

        Called from a ``finally`` in both command paths, so an early
        return or a raised exception can no longer drop the assistant
        row or skip the snapshot / compaction bookkeeping.
        """
        stack = self._active_turns.get(session_id)
        if stack:
            if turn in stack:
                stack.remove(turn)
            if not stack:
                self._active_turns.pop(session_id, None)
        if turn.get("delegated"):
            return

        rows = self._turn_rows(turn)
        self._persist_assistant_rows(session_id, rows)
        if rows:
            history = self.conversation_history.setdefault(session_id, [])
            history.extend(rows)
            if len(history) > self._conversation_max_per_session:
                self.conversation_history[session_id] = history[
                    -self._conversation_max_per_session:
                ]
        elif turn.get("user_recorded"):
            # The turn stored the user's words and produced nothing at
            # all. Loud, because the next request will need the
            # coalescer in ``ContextManager`` to stay well-formed.
            logger.warning(
                "[%s] turn produced no assistant row; transcript ends on a user message",
                session_id[:8],
            )

        # B7: stamp BEFORE the cap runs, or this turn's own session is a
        # candidate for the eviction its own completion triggered.
        self._touch_session(session_id)
        self._evict_stale_sessions()
        if self.learner:
            # AUDIT-FIXES F-06: referenced so the self-learning write cannot
            # be collected mid-flight and drop the turn.
            self._track_background_task(
                asyncio.ensure_future(
                    self.learner.on_message(session_id, "user", turn.get("text", ""))
                )
            )
        # Phase 3 (audit-r10) — persist primary thread snapshot so the
        # operator's last 50 turns survive brain restart.
        self._maybe_snapshot_primary(session_id)
        # F2 — fire compaction when this session crosses
        # ``turns_threshold`` since its last compaction (async, no
        # block).
        self._maybe_auto_compact(session_id)

    # ─────────────────────────────────────────────
    # Background-task bookkeeping (CI flake fix)
    # ─────────────────────────────────────────────
    #
    # Bare ``asyncio.create_task(...)`` calls had two failure modes
    # under pytest-asyncio:
    #
    #   1. The reference is dropped immediately, so the event loop may
    #      garbage-collect the task before it runs to completion — a
    #      "Task was destroyed but it is pending!" warning that flips
    #      to a test failure under ``--strict``.
    #   2. Tests that wanted to "make sure the side-effect happened"
    #      resorted to scanning ``asyncio.all_tasks()`` with a magic
    #      sleep, which deadlocks when a task is parked on an
    #      ``aiosqlite`` worker-thread (``Thread.join()`` can't be
    #      interrupted, defeating ``--timeout-method=thread`` and
    #      surfacing as the 60s fast-lane timeout that's been
    #      admin-merged for four releases).
    #
    # ``_track_background_task`` stores a strong reference in
    # ``self._background_tasks`` and attaches a discard callback so
    # the set self-cleans. ``drain_background_tasks`` lets tests await
    # everything the orchestrator scheduled this turn with a bounded
    # ``gather`` instead of an unsafe ``all_tasks`` sweep.

    def _track_background_task(
        self, task: Optional[asyncio.Task]
    ) -> Optional[asyncio.Task]:
        """Register a fire-and-forget task for deterministic teardown.

        Returns the task unchanged so call-sites stay one-liner:
        ``self._track_background_task(asyncio.create_task(...))``.
        ``None`` passes through (caller already handled the no-loop
        / no-memory branch).
        """
        if task is None:
            return None
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    def _capture_owning_loop(self) -> Optional[asyncio.AbstractEventLoop]:
        """Return — and lazily capture — the loop that owns the
        orchestrator's shared asyncio primitives.

        Called from any path that schedules a fire-and-forget task
        whose body touches the memory store / session locks. The
        first call from inside a running loop pins ``_owning_loop``;
        every subsequent call returns the pinned value (or ``None``
        if no loop was ever observed, e.g. a unit test poking the
        helper from sync code).

        ``set_owning_loop`` lets BrainState seed the loop explicitly
        at boot when the orchestrator is constructed before any
        coroutine runs — the lazy path covers everything else.
        """
        if self._owning_loop is not None:
            return self._owning_loop
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        self._owning_loop = loop
        return loop

    def set_owning_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Pin the loop that owns the orchestrator's shared asyncio
        primitives. BrainState calls this from its startup coroutine
        so the binding is set before the first cron / voice tool
        call arrives from a foreign loop."""
        self._owning_loop = loop

    async def drain_background_tasks(self, timeout: float = 5.0) -> None:
        """Await every tracked fire-and-forget task with a bounded gather.

        Tests should call this in teardown (or wherever they need to
        prove the background side-effect landed) instead of scanning
        ``asyncio.all_tasks()``. Exceptions inside background tasks
        have already been logged + metric-counted by their respective
        runners; ``return_exceptions=True`` keeps the drain from
        masking those with a re-raise.

        Production callers do NOT need this — the tracked set already
        cleans itself via the ``add_done_callback`` discard. The
        timeout exists as a safety net for a pathological hang and is
        deliberately conservative.
        """
        pending = list(self._background_tasks)
        if not pending:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            # A tracked task overran the drain budget. Do NOT just log and
            # leave it running — an un-cancelled straggler parked on the
            # loop wedges the *next* consumer's loop teardown (this is the
            # ubuntu-only "Task was pending" → 60s pytest-timeout kill that
            # macOS never reproduced). Cancel the stragglers and await the
            # cancellation under a short secondary budget so the tracked
            # set drains via the done-callback discard. Bounded throughout.
            stragglers = list(self._background_tasks)
            logger.warning(
                "drain_background_tasks: %d task(s) still pending after "
                "%.1fs — cancelling", len(stragglers), timeout,
            )
            for task in stragglers:
                task.cancel()
            if stragglers:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*stragglers, return_exceptions=True),
                        timeout=1.0,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "drain_background_tasks: %d task(s) ignored "
                        "cancellation", len(self._background_tasks),
                    )

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

        ``getattr`` rather than ``self.memory``: an orchestrator built
        without the attribute at all is unwired in exactly the sense
        this guard means, and raising ``AttributeError`` from a
        fire-and-forget audit write would take down the caller's turn
        over a memory record. Reached once the voice row path began
        persisting episodes, since that path is exercised against
        partially-built orchestrators.
        """
        if not getattr(self, "memory", None):
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

        # Event-loop affinity (see ``self._owning_loop`` docstring).
        #
        # The runner's first await lands inside ``memory.episode_save``,
        # which awaits ``self._pool.get()`` — an ``asyncio.Queue`` that
        # binds itself to the loop on the first contended ``get``.
        # If we are currently servicing a different loop than the one
        # that owns the memory pool (the cron/routine path opens a
        # fresh ``asyncio.new_event_loop()`` per job; the realtime
        # voice path *can* land here when the orchestrator was wired
        # before the WS handler's loop took over), naïvely calling
        # ``asyncio.create_task(_runner())`` schedules the task on the
        # foreign loop and the first ``get`` raises
        # ``RuntimeError: <Queue ...> is bound to a different event
        # loop``. The runner catches it (so the tool succeeds) but the
        # ``device_action`` episode silently never lands and the
        # operator sees the "what did my robot do" recall path return
        # nothing.
        #
        # Route the scheduling back to the owning loop instead.
        try:
            running_loop: Optional[asyncio.AbstractEventLoop] = (
                asyncio.get_running_loop()
            )
        except RuntimeError:
            running_loop = None

        owning_loop = self._capture_owning_loop()

        if (
            owning_loop is not None
            and running_loop is not None
            and running_loop is not owning_loop
        ):
            # Foreign-loop scheduling. ``call_soon_threadsafe`` is the
            # one safe hand-off across loops: it wakes the owning
            # loop, which then creates the task in its own context so
            # the task — and every primitive it awaits — is bound to
            # the loop that owns the memory pool. The task can't be
            # returned to the foreign caller (it doesn't exist yet on
            # this loop, and asyncio Tasks aren't loop-portable), so
            # we return ``None`` — the contract for the foreign-loop
            # branch. Tests that need to await the side-effect must
            # drive ``drain_background_tasks`` from the owning loop.
            def _spawn_on_owning() -> None:
                try:
                    self._track_background_task(
                        owning_loop.create_task(_runner())
                    )
                except RuntimeError:
                    # Owning loop is closing. Nothing useful we can do
                    # from the foreign loop; drop the schedule. The
                    # tool already succeeded — this is best-effort
                    # provenance logging, not user-visible behaviour.
                    pass

            try:
                owning_loop.call_soon_threadsafe(_spawn_on_owning)
            except RuntimeError:
                return None
            return None

        try:
            return self._track_background_task(asyncio.create_task(_runner()))
        except RuntimeError:
            # No running loop — extremely rare on the orchestrator
            # hot path, but defensively swallow so the caller can
            # proceed instead of crashing the turn.
            return None

    # ─────────────────────────────────────────────
    # LLM call wrappers (Lane 08 WS8 — budget gate)
    # ─────────────────────────────────────────────

    async def _call_llm_chat(
        self,
        *,
        messages: list[dict],
        tools: Optional[list[dict]],
        call_site: str = "chat",
        force_tool: Optional[str] = None,
        route: Optional[dict] = None,
    ) -> dict:
        """Wrapper around ``LLMProvider.chat_with_failover`` that
        propagates the ``call_site`` label so the budget gate bills
        the right bucket. Returns the raw provider dict — including
        the ``budget_exceeded`` short-circuit shape from Lane 09 / 04
        — so the caller can react.

        ``force_tool`` — optional tool name the orchestrator wants the
        model to call this turn (grounded-memory closure: temporal-recall
        queries pass ``"notes_memory__fused_timeline"`` so the LLM's
        prose answer is grounded in the tool's retrieved data instead
        of narrated from working-memory context). Forwarded into
        ``chat_with_failover`` which translates per provider via
        :mod:`agents.multimodal_blocks`.

        Falls through to the legacy positional signature for adapters
        in tests that don't accept ``call_site`` / ``force_tool`` kw.
        """
        kw: dict[str, Any] = {"call_site": call_site}
        if force_tool:
            kw["force_tool"] = force_tool
        if route:
            kw["route"] = route
        try:
            return await self.llm.chat_with_failover(
                messages=messages,
                tools=tools,
                **kw,
            )
        except TypeError:
            # Older test adapters: shed the newest kwargs first —
            # ``route`` → ``force_tool`` → ``call_site`` — so the call
            # still lands. The adaptive route / forced-tool / budget
            # intents degrade to the legacy path in that case.
            kw.pop("route", None)
            try:
                return await self.llm.chat_with_failover(
                    messages=messages,
                    tools=tools,
                    **kw,
                )
            except TypeError:
                kw.pop("force_tool", None)
                try:
                    return await self.llm.chat_with_failover(
                        messages=messages,
                        tools=tools,
                        **kw,
                    )
                except TypeError:
                    return await self.llm.chat_with_failover(
                        messages=messages,
                        tools=tools,
                    )

    def _adaptive_routing_on(self) -> bool:
        """Whether adaptive per-turn model routing is enabled.

        Reads ``settings.llm.adaptive_routing`` off the live provider
        config (default ON). Lets an operator pin a single model by
        setting it ``false`` without touching code.
        """
        cfg = getattr(self.llm, "_config", None)
        if isinstance(cfg, dict):
            return bool(cfg.get("adaptive_routing", True))
        return True

    def _route_for_tier(self, tier: str) -> Optional[dict]:
        """Resolve a chat-call ``ProviderRef`` for *tier* via the provider's
        adaptive router, or ``None`` when routing is off / unavailable.

        Tolerant of provider stand-ins in tests that lack ``route_call``.
        """
        if not self._adaptive_routing_on():
            return None
        route_call = getattr(self.llm, "route_call", None)
        if not callable(route_call):
            return None
        try:
            return route_call("chat", tier=tier, adaptive=True)
        except Exception:
            logger.debug("route_call failed for tier=%s", tier, exc_info=True)
            return None

    async def _emit_budget_exceeded(
        self,
        *,
        session_id: str,
        budget: dict,
        call_site: str = "chat",
    ) -> None:
        """Emit a structured ``budget_exceeded`` WS frame.

        WS8 acceptance (parent reminder #3, 2026-05-22T18:40Z):
        payload includes ``call_site``, ``cap_dollars``,
        ``current_dollars``, ``reset_at`` so Lane 12 can render
        ``"Chat budget reached ($X.XX / hour). Resets at HH:MM."``.

        Followed by a brief assistant text so chat clients that don't
        special-case the new frame type still see a sensible reply
        instead of silence.
        """
        try:
            from models.protocol import BudgetExceededPayload
        except Exception:
            logger.debug("BudgetExceededPayload import failed", exc_info=True)
            return

        payload = BudgetExceededPayload(
            call_site=str(budget.get("call_site") or call_site),
            cap_dollars=float(budget.get("cap_dollars") or 0.0),
            current_dollars=float(budget.get("current_dollars") or 0.0),
            window=str(budget.get("window") or "hour"),
            reset_at=float(budget.get("reset_at") or 0.0),
        )

        try:
            await self.send(
                session_id,
                FeralMessage(
                    session_id=session_id,
                    hop="brain",
                    type="budget_exceeded",
                    payload=payload.model_dump(),
                ),
            )
        except Exception:
            logger.warning("budget_exceeded frame emit failed", exc_info=True)

        # Friendly text so older chat clients still see a banner.
        try:
            human = (
                f"Cost cap reached ({payload.call_site}, "
                f"${payload.cap_dollars:.2f}/{payload.window}). "
                "Adjust in Settings → Cost, or wait for the cap to reset."
            )
            await self._send_text(session_id, human)
        except Exception:
            logger.debug("budget_exceeded follow-up text failed", exc_info=True)

    # ─────────────────────────────────────────────
    # Image-bearing tool results (screenshots)
    # ─────────────────────────────────────────────

    # Ceiling on how many tool_call_id entries the per-session image side
    # table remembers (live images plus "an image used to be here"
    # tombstones). Live images are bounded much more tightly by the batch
    # pruner; this only stops the tombstone list growing forever.
    _TOOL_IMAGE_ORDER_MAX = 500

    def _image_delivery_mode(self) -> str:
        """Which wire shape can carry a tool-result image right now.

        Derived from the LIVE provider, not from a constant: capability
        is per-model for several providers (an Ollama text model, a
        non-vision OpenRouter route, DeepSeek's text-only chat models),
        and ``LLMProvider._vision_support_status`` is the one place that
        knows. Anything we cannot positively confirm resolves to
        ``IMAGE_DELIVERY_NONE`` -- no image on the wire, and the model is
        told in words that one existed.
        """
        provider = str(getattr(self.llm, "provider", "") or "")
        vision_ok = True
        status = getattr(self.llm, "_vision_support_status", None)
        if callable(status):
            try:
                vision_ok = bool(status()[0])
            except Exception:
                logger.debug("vision support probe failed", exc_info=True)
                vision_ok = False
        return image_delivery_mode(provider, vision_supported=vision_ok)

    def _images_allowed(self) -> bool:
        return self._image_delivery_mode() != IMAGE_DELIVERY_NONE

    def _serialize_tool_result_for_history(
        self, session_id: str, tool_call_id: str, tool_name: str, result_data: Any,
    ) -> str:
        """Budget the text half of a tool result and stash any image.

        Returns the string that goes into the ``role:"tool"`` history row.
        Any image found in the result is lifted out BEFORE the text budget
        runs, so a 400 000-char screenshot no longer consumes (and blow
        past) a 2 000-char budget.
        """
        allow = self._images_allowed()
        try:
            content, images = serialize_tool_result_with_images(
                tool_name, result_data, registry=self.skills, allow_images=allow,
            )
        except Exception:
            logger.warning(
                "image-aware tool-result serialization failed for %s; "
                "falling back to text-only", tool_name, exc_info=True,
            )
            return serialize_tool_result(tool_name, result_data, registry=self.skills)
        if images and tool_call_id:
            self._record_tool_images(session_id, tool_call_id, tool_name, images)
        return content

    def _record_tool_images(
        self, session_id: str, tool_call_id: str, tool_name: str, images: list,
    ) -> None:
        table = self._tool_result_images.setdefault(session_id, {})
        order = self._tool_image_order.setdefault(session_id, [])
        if tool_call_id not in table:
            order.append(tool_call_id)
        table[tool_call_id] = {
            "images": [img.to_dict() for img in images],
            "pruned": False,
            "tool_name": tool_name,
        }
        # Pruned entries are tiny (they only carry the "there was an image
        # here" marker) but they are not free, and a long-lived session can
        # accumulate thousands. Drop the oldest tombstones once the order
        # list gets long; the live images are bounded separately by
        # ``_maybe_prune_tool_images``.
        if len(order) > self._TOOL_IMAGE_ORDER_MAX:
            for stale in order[: len(order) - self._TOOL_IMAGE_ORDER_MAX]:
                if table.get(stale, {}).get("pruned"):
                    table.pop(stale, None)
            self._tool_image_order[session_id] = [
                call_id for call_id in order if call_id in table
            ]

    def _materialize_tool_images(
        self, session_id: str, messages: list[dict],
    ) -> list[dict]:
        """Splice stashed tool-result images into an outgoing request.

        Called immediately before every chat/stream call. The history the
        orchestrator keeps is never mutated: this returns a per-request
        view, the same discipline ``_compact_context`` uses.
        """
        table = self._tool_result_images.get(session_id)
        if not table:
            return messages
        try:
            return materialize_tool_result_images(
                messages, table, self._image_delivery_mode(),
            )
        except Exception:
            logger.warning(
                "tool-result image materialization failed; sending text only",
                exc_info=True,
            )
            return messages

    def _maybe_prune_tool_images(self, session_id: str) -> int:
        """Batch-prune old screenshots from the side table.

        Source for the policy (read, not guessed): Anthropic's computer-use
        tool documentation, "Manage screenshot history for prompt caching"
        -- screenshots cost roughly 1000-1800 input tokens each, and the
        documented recommendation is to keep the last three and prune in
        BATCHES (every ~25 turns) rather than one per turn, because
        dropping one every turn changes the cached prefix every turn and
        invalidates the prompt cache.

        Returns the number of tool results whose image was dropped.
        """
        table = self._tool_result_images.get(session_id)
        if not table:
            return 0
        order = self._tool_image_order.setdefault(session_id, [])
        rounds = self._tool_image_rounds.get(session_id, 0)
        live = sum(
            1 for entry in table.values()
            if isinstance(entry, dict) and not entry.get("pruned") and entry.get("images")
        )
        if not should_prune_images(round_counter=rounds, live_images=live):
            return 0
        pruned = prune_tool_result_images(table, order)
        if pruned:
            logger.info(
                "Pruned %d old tool-result image(s) from session %s "
                "(round %d, %d live before prune)",
                len(pruned), session_id, rounds, live,
            )
        return len(pruned)

    def _note_agent_round(self, session_id: str) -> None:
        """Advance the per-session agent-round counter used by the batch
        pruner. One round = one LLM call inside the tool loop, which is
        the unit Anthropic's guidance counts."""
        self._tool_image_rounds[session_id] = (
            self._tool_image_rounds.get(session_id, 0) + 1
        )

    def _forget_tool_images(self, session_id: str) -> None:
        self._tool_result_images.pop(session_id, None)
        self._tool_image_order.pop(session_id, None)
        self._tool_image_rounds.pop(session_id, None)

    # ─────────────────────────────────────────────
    # Vision context attach (Lane 08 WS4 — S5 prereq)
    # ─────────────────────────────────────────────

    def _attach_vision_context(
        self,
        user_content: Any,
        *,
        context: Optional[dict],
        session_id: str,
    ) -> Any:
        """Possibly augment ``user_content`` with a fresh glasses /
        screen frame as an ``image_url`` content block.

        Pulls from Lane 11's ``perception.glasses_buffer`` when the
        turn is in voice mode AND ``vision.enabled=true`` in settings
        AND a frame within 30s exists. Stale frames are NOT attached
        (parent-acked reminder #1 — emit nothing rather than a stale
        image; the LLM can ask if it needs visual context).

        Falls back to the legacy ``api.state.VisionBuffer`` for the
        existing phone vision_ask channel only when Lane 11's buffer
        is empty.
        """
        try:
            from perception.context_attach import attach_vision_context
        except Exception:
            logger.debug("context_attach module unavailable", exc_info=True)
            return user_content
        try:
            return attach_vision_context(
                user_content,
                context=context,
                session_id=session_id,
                vision_buffer=self.vision_buffer,
            )
        except Exception:
            logger.warning("vision context attach failed", exc_info=True)
            return user_content

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
            # : tear down subagents tied to this parent session.
            # Lock release stays synchronous; cancellation is fire-and-forget.
            self._w17_cancel_subsessions_nowait(session_id)

    # ─────────────────────────────────────────────
    # Plan mode
    # ─────────────────────────────────────────────
    #
    # A per-session, ephemeral posture in which the agent researches and
    # proposes but cannot mutate state. It is a SEPARATE AXIS from
    # ``security.autonomy_mode``: that setting is persisted and global,
    # this one dies with the session. Nothing below reads or writes the
    # autonomy mode.
    #
    # There are two enforcement points and both are required. The tool-list
    # filter applied at the two chat sites below is ADVISORY: a model can
    # emit a tool name it was never given, and the voice surfaces build
    # their tool list from ``SkillRegistry.get_all_tools()`` on a path this
    # filter never touches. The gate that actually holds is
    # ``ToolRunner.enforce_plan_mode``. See ``agents/plan_mode.py``.

    @property
    def plan_mode(self):
        """The single :class:`~agents.plan_mode.PlanModeState`.

        It lives on the ToolRunner because that is where the dispatch gate
        is; this property exists so API routes and the client-facing code
        do not have to reach through ``orchestrator.tool_runner``.
        """
        return self.tool_runner.plan_mode

    async def enter_plan_mode(
        self,
        session_id: str,
        *,
        reason: str = "",
        entered_by: str = "user",
    ) -> dict:
        """Explicit entry point, shared by the ``/plan`` meta-command and
        the REST route. There is deliberately no heuristic entry: a mode
        the user did not ask for and cannot see is worse than no mode."""
        state = self.plan_mode.enter(session_id, reason=reason, entered_by=entered_by)
        await self._emit_plan_mode_frame(session_id, state)
        return state

    async def exit_plan_mode(
        self,
        session_id: str,
        *,
        approved: bool = False,
        actor: str = "user",
    ) -> dict:
        """Leave plan mode. ``actor`` must be ``"user"``.

        ``approved=True`` records that the user accepted the plan. It
        grants NOTHING: no standing tool approval is created, so every
        mutating call in the following turns still goes through the
        session's autonomy mode. Blanket approval on plan approval would
        be a real security regression, so the flag stays purely
        informational.
        """
        plan = self.plan_mode.latest_plan(session_id)
        self.plan_mode.exit(session_id, approved=approved, actor=actor)
        state = self.plan_mode.describe(session_id)
        state["approved"] = bool(approved)
        state["latest_plan"] = plan
        await self._emit_plan_mode_frame(session_id, state)
        return state

    _PLAN_SKILL_ID = "plan"

    def _inject_plan_skill(
        self,
        tools: list[dict],
        skills: list["SkillManifest"],
    ) -> tuple[list[dict], list["SkillManifest"]]:
        """Add ``plan__submit`` to this turn if it is not already there."""
        try:
            manifest = self.skills.skills.get(self._PLAN_SKILL_ID)
        except Exception:
            manifest = None
        if manifest is None:
            logger.warning(
                "plan mode is active but the 'plan' skill is not registered; "
                "the model has no way to submit a plan",
            )
            return tools, skills

        out_skills = list(skills or [])
        if not any(
            getattr(s, "skill_id", "") == self._PLAN_SKILL_ID for s in out_skills
        ):
            out_skills.append(manifest)

        out_tools = list(tools or [])
        have = {
            (t.get("function") or {}).get("name")
            for t in out_tools if isinstance(t, dict)
        }
        for tool in self.skills.get_tools_for_skills([manifest]):
            if (tool.get("function") or {}).get("name") not in have:
                out_tools.append(tool)
        return out_tools, out_skills

    def _apply_plan_mode_filter(
        self,
        session_id: str,
        tools: list[dict],
        relevant_skills: list["SkillManifest"],
    ) -> tuple[list[dict], list["SkillManifest"]]:
        """Narrow this turn's tool array and prompt view to plan-safe only.

        No-op outside plan mode. Inside it, both halves matter: the array
        is what the model is offered, the manifest list is what
        ``build_tooling_catalog`` enumerates as "Active this turn".
        Filtering only one of them produces a model that is told it has a
        tool and then refused when it uses it.
        """
        if not self.plan_mode.is_active(session_id):
            return tools, relevant_skills
        from agents.plan_mode import (
            filter_skills_for_plan_mode,
            filter_tools_for_plan_mode,
        )
        filtered_tools = filter_tools_for_plan_mode(tools, registry=self.skills)
        filtered_skills = filter_skills_for_plan_mode(
            relevant_skills, registry=self.skills,
        )
        # The `plan` skill is deliberately NOT in ALWAYS_INCLUDE_SKILLS,
        # which already puts ~59 tools in front of the model every turn on
        # a chat path with no tool cap. It is injected here instead, only
        # for the turns that can actually use it, so plan mode costs one
        # extra tool while it is on and zero the rest of the time.
        filtered_tools, filtered_skills = self._inject_plan_skill(
            filtered_tools, filtered_skills,
        )
        logger.info(
            "[%s] plan mode: %d/%d tools exposed",
            session_id[:8], len(filtered_tools), len(tools or []),
        )
        return filtered_tools, filtered_skills

    async def _emit_plan_mode_frame(self, session_id: str, state: dict) -> None:
        """Push the current plan-mode posture to the client."""
        try:
            await self.send(session_id, FeralMessage(
                session_id=session_id, hop="brain", type="plan_mode",
                payload=dict(state),
            ))
        except Exception:
            logger.debug("plan_mode frame emit failed (non-fatal)", exc_info=True)

    async def _maybe_handle_plan_meta_command(self, session_id: str, text: str) -> bool:
        """Handle ``/plan``-family meta-commands. Returns True when handled.

        Matching is an exact first-token check on ``/plan`` so that prose
        about planning never enters the mode. Intercepted before
        ``_route_prompt``, which would otherwise treat ``/plan`` as the
        explicit ``/skill`` prefix form for the ``plan`` skill.
        """
        raw = (text or "").strip()
        if not raw:
            return False
        parts = raw.split()
        if parts[0].lower() != "/plan":
            return False
        arg = parts[1].lower() if len(parts) > 1 else ""

        if arg in ("", "on", "start", "enter"):
            reason = " ".join(parts[2:]) if arg else ""
            await self.enter_plan_mode(session_id, reason=reason)
            await self._send_text(
                session_id,
                "Plan mode is ON. I'll research with read-only tools and "
                "propose a plan; I won't change anything. `/plan approve` to "
                "accept, `/plan off` to discard. Approving does not "
                "pre-approve the individual steps.",
            )
            return True

        if arg in ("approve", "accept", "ok"):
            was_active = self.plan_mode.is_active(session_id)
            state = await self.exit_plan_mode(session_id, approved=True)
            await self._send_text(
                session_id,
                "Plan approved, plan mode is OFF. Each step still goes "
                "through the normal approval flow."
                if was_active else "Not in plan mode.",
            )
            del state
            return True

        if arg in ("off", "exit", "cancel", "stop", "discard"):
            was_active = self.plan_mode.is_active(session_id)
            await self.exit_plan_mode(session_id, approved=False)
            await self._send_text(
                session_id,
                "Plan mode is OFF." if was_active else "Not in plan mode.",
            )
            return True

        if arg == "status":
            state = self.plan_mode.describe(session_id)
            await self._send_text(
                session_id,
                f"Plan mode: {'ON' if state['plan_mode'] else 'OFF'} "
                f"({state['plan_count']} plan(s) submitted).",
            )
            return True

        await self._send_text(
            session_id,
            "Usage: `/plan` (enter), `/plan approve`, `/plan off`, `/plan status`.",
        )
        return True

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
        """Real body of handle_command. Guarded by the session lock above.

        Wraps ``_handle_command_body`` so ``_finalize_turn`` runs on
        EVERY exit path — return, early return, or exception. See the
        "Turn write-back" block for why that matters.
        """
        turn = self._begin_turn(session_id, text)
        try:
            return await self._handle_command_body(session_id, text, context, turn)
        finally:
            self._finalize_turn(session_id, turn)

    async def _handle_command_body(
        self,
        session_id: str,
        text: str,
        context: Optional[dict],
        turn: dict,
    ):
        """Single-agent pipeline for one turn. Never writes the turn
        back itself — ``_finalize_turn`` owns that."""
        logger.info(f"[{session_id[:8]}] Command: {text}")
        self._session_finalized.discard(session_id)
        self._stamp_session_surface(session_id, context)

        # Explicit plan-mode entry/exit, before routing. `/plan` would
        # otherwise be read as the `/skill` prefix form in `_route_prompt`.
        if await self._maybe_handle_plan_meta_command(session_id, text):
            return

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
            # The full text, not just the 200-char preview. ``summary``
            # is capped because it is what previews and list views
            # render, and that cap used to be the ONLY durable record
            # of what the operator said: everything past character 200
            # existed solely in ``conversation_history``, which
            # ``compact_session`` replaces wholesale. On any surface
            # without the web client's autosave (voice, CLI, a paired
            # phone) the rest of a long message was unrecoverable once
            # compaction fired. ``episodes_fts`` indexes ``detail``
            # alongside ``summary``, so this is searchable on arrival.
            detail=json.dumps({"text": text, "context": context or {}}),
        )

        # S1 live-path closure (non-stream parity). The forced-tool
        # path (v2026.5.48 grounded-memory closure) is the primary —
        # when the LLM is required to call ``notes_memory__fused_timeline``
        # the natural ``_emit_tool_result`` → ``_maybe_emit_timeline_frame``
        # branch already mounts the widget AND the prose answer is
        # grounded in the retrieved tool result. The side-channel
        # below is the SAFETY NET for the providers / cases where the
        # forced-tool path is unavailable (Gemini OpenAI-compat layer
        # can't name a single tool; tests using bare ``llm.chat`` with
        # no failover wrapper; tool not in the routed skill set). We
        # therefore defer scheduling the side-channel until after tool
        # routing, and skip it entirely when the forced-tool path is
        # going to fire — running ``timeline_fusion`` twice would
        # double-bill memory I/O and the client de-dupes the second
        # widget anyway.
        #
        # See ``_force_tool_for_query`` below for the gate.
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
                        # Attribution for the multi-agent turn. This branch
                        # runs BEFORE the single-agent loop and returns, so
                        # without this the default profile (multi_agent
                        # defaults on) would show no model and no tokens at
                        # all: the loop that records them never executes.
                        _model, _usage = self._pop_multi_agent_attribution(session_id)
                        await self._try_send_sdui(
                            session_id, response_text,
                            model=_model, usage=_usage,
                        )
                        if self.memory:
                            # Full text, not [:300] — the phone's chat_response
                            # falls back to working memory and must not get a
                            # truncated stub (working_context_string slices to
                            # [:200] itself, so prompt size is unaffected).
                            self.memory.working_push(session_id, {"role": "assistant", "text": response_text})
                        # The multi-agent hand-off never touches
                        # ``conversation_history``; hand both rows to
                        # ``_finalize_turn`` so the next turn sees this
                        # exchange instead of a gap. (``_try_send_sdui``
                        # is not ``_send_text``, so nothing else records
                        # the reply.) The learner call moved to the
                        # epilogue with it.
                        turn["reply_text"] = response_text
                        # Return the full final text so callers (api/server.py
                        # chat_request handler) can carry it in chat_response
                        # instead of relying on the working-memory fallback.
                        return response_text
                except MultiAgentProviderError as exc:
                    # The provider itself failed. Deliver an error
                    # frame and stop: the single-agent fallback below
                    # would call the same provider again and produce
                    # a second failure, and ``response_text`` must
                    # not carry the failure into ``_try_send_sdui`` /
                    # ``turn["reply_text"]`` (which is how "HTTP 400
                    # ..." ended up in conversation_history).
                    logger.error(
                        "[%s] Multi-agent LLM provider failed: %s",
                        session_id[:8], exc,
                    )
                    await self._send_error(session_id, str(exc))
                    return None
                except Exception as e:
                    logger.warning(f"Multi-agent failed, falling back to single-agent: {e}")

        # Step 1: Semantic Tool Routing
        relevant_skills = await self._route_prompt(text, session_id=session_id)

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
        # Withhold the skills whose prerequisite is provably absent: no
        # key, no OAuth, no Docker, no robot. 79 of the 266 schemas on
        # the operator's brain, every one a call the model could only
        # lose. skills/availability.py also writes the prompt line that
        # tells the model what is off and why.
        tools = filter_unavailable_tools(tools)

        if self._mcp_client:
            mcp_tools = self._mcp_client.to_llm_tool_definitions()
            if mcp_tools:
                tools = (tools or []) + mcp_tools

        # Plan mode, exposure half. Applied AFTER the MCP merge so MCP
        # tools (which carry no manifest safety metadata and therefore fail
        # closed) are filtered too. `relevant_skills` is pruned in the same
        # breath because `self_model.build_tooling_catalog` reads endpoints
        # off the manifest independently of this array, and would otherwise
        # tell the model `edit_file` is "Active this turn" and then refuse it.
        tools, relevant_skills = self._apply_plan_mode_filter(
            session_id, tools, relevant_skills,
        )

        # v2026.5.48 grounded-memory closure (non-stream path). See the
        # docstring on ``_force_tool_for_query``. ``forced_tool`` pins
        # the LLM to ``notes_memory__fused_timeline`` (or
        # ``feral_routines__create`` for scheduled actions), but the
        # proactive side-channel still runs on every ``_R_TEMPORAL``
        # match — live testing showed models ignore forced_tool and pick
        # ``search_notes`` unless the timeline card is already mounted.
        forced_tool = self._force_tool_for_query(text, tools, session_id)
        try:
            if self._R_TEMPORAL.search(text or ""):
                self._track_background_task(asyncio.create_task(
                    self._maybe_emit_temporal_timeline(session_id, text)
                ))
        except Exception:
            logger.debug(
                "temporal-timeline side-channel: task schedule failed",
                exc_info=True,
            )

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
            query=self._coref_query_for_prompt(session_id, text or ""),
        )
        if specialist:
            system_prompt = self._build_specialist_system_prompt(specialist, system_prompt)

        if session_id not in self.conversation_history:
            self.conversation_history[session_id] = []
        # B7: a turn in flight counts as activity. Without this a long
        # turn (slow provider, tool loop) looks idle to the cap for its
        # whole duration.
        self._touch_session(session_id)

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
        # WS4 — vision context attach. Pulls the freshest glasses /
        # screen frame from Lane 11's buffer when the turn is in
        # voice mode + vision is enabled + frame ≤ 30s. Stale frames
        # are NOT attached (parent reminder #1).
        user_content = self._attach_vision_context(
            user_content, context=context, session_id=session_id,
        )
        user_message = {"role": "user", "content": user_content}
        self.conversation_history[session_id].append(user_message)
        turn["user_recorded"] = True

        # Per-request VIEW of the transcript. ``history`` is a working
        # copy: the loop appends this turn's assistant / tool rows to it
        # and ``_finalize_turn`` commits everything past ``base_len``.
        # The compacted window itself is never stored back.
        history = self._compact_context(self.conversation_history[session_id].copy())
        turn["working"] = history
        turn["base_len"] = len(history)

        from agents.iteration_budget import (
            GUARD_OK,
            GUARD_STOP,
            GUARD_WARN,
            IterationBudget,
            NO_PROGRESS_GUIDANCE,
            NO_PROGRESS_WARNING,
            unavailable_tool_notice,
        )
        budget = IterationBudget(self._max_iterations, self._tool_loop_max_seconds)
        # Set only by the guard's STOP level: tools are withdrawn and the
        # model gets exactly one more round to produce an honest final
        # answer. The WARN level below deliberately does NOT set this.
        final_answer_only = False
        no_progress_warned = False
        refusal_retry_used = False
        reasoning_retry_count = 0
        empty_retry_used = False
        pending_retry_addition: Optional[str] = None
        # Per-turn attribution, accumulated ACROSS ROUNDS. One user turn
        # can drive several LLM calls (each tool round, plus refusal and
        # empty-response retries, plus a tier escalation), and the user is
        # billed for every one of them. Reporting only the last round
        # would understate a tool-heavy turn by most of its real cost,
        # which is the opposite of the point. ``turn_model`` tracks the
        # model that produced the FINAL answer, since that is the one the
        # user is looking at when they read the label.
        turn_usage: dict[str, int] = {}
        turn_model = ""
        # Never-stall: if the user turn is a short ack, inject the fast-path
        # instruction into the very first call (before the model replies).
        if self.refusal_handler.is_ack_execution(text):
            pending_retry_addition = self.refusal_handler.ACK_EXECUTION_FAST_PATH_INSTRUCTION

        # Adaptive routing — estimate this turn's difficulty once and pick
        # a model tier. ``current_tier`` is escalated on a bad/empty answer
        # (the verifier-gated cascade) so a wrong cheap reply is recovered
        # by a stronger model rather than shipped. ``route_ref`` is re-
        # resolved on each escalation. All surfaces (WebUI / iOS / voice)
        # share this because they all drive ``handle_command``.
        has_vision_turn = isinstance(user_content, list) and any(
            isinstance(p, dict)
            and str(p.get("type", "")).startswith(("image", "input_image"))
            for p in user_content
        )
        context_chars = 0
        if hasattr(self.llm, "_message_char_count"):
            try:
                context_chars = self.llm._message_char_count(history)
            except Exception:
                context_chars = 0
        try:
            current_tier = llm_router.classify_difficulty(
                text,
                has_tools=bool(tools),
                has_vision=has_vision_turn,
                context_chars=context_chars,
            )
        except Exception:
            current_tier = llm_router.BALANCED
        route_ref = self._route_for_tier(current_tier)

        sent_response = False
        final_response_text = ""
        while budget.start_iteration():
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
            # Screenshot lifecycle, in the order the guidance requires:
            # count the round, batch-prune when a batch boundary is due,
            # then splice the surviving images into THIS request in the
            # active provider's shape. The stored history stays text-only.
            self._note_agent_round(session_id)
            self._maybe_prune_tool_images(session_id)
            messages = self._materialize_tool_images(session_id, messages)
            # Withdraw any tool the precondition guard has written off
            # for this turn. Rebound once per round so every ``tools if
            # tools else None`` below this point sees the same list, and
            # so a tool withdrawn on round 3 is genuinely absent from
            # round 4 rather than merely discouraged in prose.
            tools = budget.filter_tools(tools)

            try:
                model_name = getattr(self.llm, 'model_name', 'llm')
                llm_call_event: dict[str, Any] = {"model": model_name}
                if route_ref:
                    llm_call_event["route"] = {
                        "tier": route_ref.get("tier"),
                        "provider": route_ref.get("provider"),
                        "model": route_ref.get("model"),
                        "source": route_ref.get("source"),
                    }
                await self._emit_brain_event(session_id, "llm_call", llm_call_event)
                response = await self._call_llm_chat(
                    messages=messages,
                    tools=tools if tools else None,
                    call_site="chat",
                    force_tool=forced_tool,
                    route=route_ref,
                )
                # Force the tool ONLY on the first iteration. Once the
                # model has dispatched ``notes_memory__fused_timeline``
                # we want it free to synthesise the grounded prose from
                # the tool result on the next pass; re-forcing would
                # spin a tool-call loop.
                forced_tool = None

                # WS8 — BudgetExceeded surfaces as a structured WS
                # frame (NOT a stack trace) for Lane 12 to render as
                # a yellow banner. The LLM provider already returns
                # the structured shape; we just propagate.
                if isinstance(response, dict) and response.get("budget_exceeded"):
                    await self._emit_budget_exceeded(
                        session_id=session_id,
                        budget=response["budget_exceeded"],
                    )
                    return

                # Accumulate this round's cost before any of the branches
                # below can ``continue``/``break`` past it. A refusal retry
                # or a tier escalation still burned real tokens, so it has
                # to count even though its output is discarded.
                _accumulate_turn_usage(turn_usage, response)
                _round_model = _model_of_llm_response(response)
                if _round_model:
                    turn_model = _round_model

                # Provider failure travels as an error frame, never as
                # assistant text. Checked BEFORE the empty-response
                # retry below: a 400 from the provider is not an empty
                # answer, and retrying with a prompt addition would
                # burn a second failing call. Nothing is appended to
                # ``history`` here, so ``_finalize_turn`` commits no
                # assistant row and the failure never becomes model
                # context on the next turn.
                provider_error = llm_response_error(response)
                if provider_error:
                    logger.error(
                        "[%s] LLM provider failed: %s", session_id[:8], provider_error,
                    )
                    await self._send_error(session_id, provider_error)
                    sent_response = True
                    break

                text_content, tool_calls = self.llm.extract_response(response)

                # Never-stall: empty response — no text, no tool calls.
                if not text_content and not tool_calls and not empty_retry_used:
                    empty_retry_used = True
                    logger.warning("[%s] Empty response; prompt-addition retry", session_id[:8])
                    pending_retry_addition = self.refusal_handler.EMPTY_RESPONSE_RETRY_INSTRUCTION
                    current_tier = llm_router.escalate(current_tier)
                    route_ref = self._route_for_tier(current_tier)
                    continue

                # Reasoning-only: provider returned reasoning trace but no visible output.
                if self.refusal_handler.is_reasoning_only(response) and reasoning_retry_count < 2:
                    reasoning_retry_count += 1
                    logger.warning(
                        "[%s] Reasoning-only response (retry %d); prompt-addition steer",
                        session_id[:8], reasoning_retry_count,
                    )
                    pending_retry_addition = self.refusal_handler.REASONING_ONLY_RETRY_INSTRUCTION
                    current_tier = llm_router.escalate(current_tier)
                    route_ref = self._route_for_tier(current_tier)
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
                        current_tier = llm_router.escalate(current_tier)
                        route_ref = self._route_for_tier(current_tier)
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

                if tool_calls and final_answer_only:
                    # Tools were withdrawn after the loop guard tripped,
                    # yet the model still tried to call one — hard stop
                    # before the assistant msg lands in history (a
                    # tool_calls msg without tool results poisons the
                    # next turn's OpenAI-shape conversation).
                    logger.warning(
                        "[%s] tool call after loop guard closed tools; stopping",
                        session_id[:8],
                    )
                    break

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
                # WS5 — multi-actuator ordered WS frames. Tools still
                # execute in parallel (cap = ``FERAL_MAX_PARALLEL_TOOLS``)
                # for speed, but the ``tool_start`` AND ``tool_result``
                # frames are emitted in the LLM's original tool_call
                # index order so the consumer renders the chain
                # consistently — e.g. ``vision__describe_scene`` →
                # ``home_assistant__vacuum_start`` even when vacuum_start
                # happens to finish first.
                #
                # Step 1: emit tool_start frames synchronously in order.
                # Step 2: execute everything in parallel.
                # Step 3: emit tool_result frames in tool_call order.
                #
                # The OpenAI tool-message contract still requires
                # ``history`` to be appended in tool_call order; that
                # property is preserved below.
                parallel_cap = max(1, int(os.environ.get("FERAL_MAX_PARALLEL_TOOLS", "6")))
                sem = asyncio.Semaphore(parallel_cap)

                # Step 1 — ordered tool_start emission.
                for _tc in tool_calls:
                    await self._emit_tool_start(session_id, _tc)

                async def _run_tool(tc: dict) -> dict:
                    async with sem:
                        t_start = time.time()
                        # Claim the audit row: this loop logs every tool
                        # call below, including the mcp_/daemon_/subagent
                        # branches and the refusals that never reach
                        # SkillExecutor. Without the claim the executor
                        # would write a second row for the same call.
                        with claimed_by_caller():
                            result_data = await self._execute_tool_call_for_llm(
                                session_id, tc, relevant_skills,
                            )
                        latency_ms = (time.time() - t_start) * 1000
                        return {
                            "tc": tc,
                            "result": result_data,
                            "latency_ms": latency_ms,
                        }

                tool_outputs = await asyncio.gather(
                    *[_run_tool(tc) for tc in tool_calls]
                )

                # Step 3 — ordered tool_result emission. ``tool_outputs``
                # is already ordered because asyncio.gather preserves
                # input order regardless of completion order.
                for output in tool_outputs:
                    await self._emit_tool_result(
                        session_id,
                        output["tc"],
                        output["result"],
                        output["latency_ms"],
                    )

                for tool_output in tool_outputs:
                    tc = tool_output["tc"]
                    result_data = tool_output["result"]
                    latency_ms = tool_output["latency_ms"]

                    audit_status = audit_status_of(result_data)
                    tool_pending = audit_status == "pending_approval"
                    tool_success = audit_status == "success"
                    await self._emit_brain_event(session_id, "tool_exec", {"tool": tc["name"], "success": tool_success})

                    # Earned autonomy. A pending approval is a question,
                    # not an outcome, so it must not count either way --
                    # recording it as a failure would punish a tool for
                    # being gated, and as a success would promote one
                    # that never ran.
                    if not tool_pending:
                        self.tool_runner.record_trust_outcome(
                            tc["name"], success=tool_success,
                        )

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
                            result_status=audit_status,
                            result_summary=json.dumps(result_data)[:300],
                            latency_ms=latency_ms,
                        )

                    history.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "name": tc["name"],
                        # Budgeted per tool (skills/result_budget.py). This
                        # used to be json.dumps(...)[:2000] — a blind byte
                        # slice that cut mid-token, so the model was
                        # routinely handed JSON that does not parse, with
                        # nothing saying anything had been removed.
                        #
                        # An image in the result (screenshot) is lifted out
                        # BEFORE this budget runs and stashed for delivery
                        # as a real image block; only the text half is
                        # budgeted. Without that, a 400k-char screenshot
                        # came back as 1405 chars of truncated base64 and
                        # every vision path in FERAL was dead.
                        "content": self._serialize_tool_result_for_history(
                            session_id, tc["id"], tc["name"], result_data,
                        ),
                    })
                    # A tool waiting on the operator's approval is not a
                    # failing tool. Feeding it to the no-progress guard
                    # told the model its call had failed, so the model
                    # re-issued it and one approval prompt became
                    # several: the live store holds three consecutive
                    # workspace_scripts__rerun rows with identical args
                    # and three different request_ids, all recorded as
                    # 'failure', plus four near-identical
                    # agentic_computer_use__execute_task rows.
                    guard_level = GUARD_OK if tool_pending else budget.observe_tool(
                        tc["name"], tc.get("args", {}), tool_success, result_data
                    )
                    if guard_level == GUARD_STOP:
                        final_answer_only = True
                    elif guard_level == GUARD_WARN and not no_progress_warned:
                        # Warn once, keep every other tool available. The
                        # old code withdrew the WHOLE toolset here after
                        # two identical failures, so a single unavailable
                        # tool disarmed an agent mid-task.
                        no_progress_warned = True
                        history.append(
                            {"role": "system", "content": NO_PROGRESS_WARNING}
                        )
                    anti_loop_guidance = result_data.get("_anti_loop_guidance")
                    if anti_loop_guidance:
                        history.append({"role": "system", "content": anti_loop_guidance})

                    # A tool whose PRECONDITION keeps failing is dropped
                    # from the next round's list and named once. Unlike
                    # the streaks above this ignores arguments, because
                    # no argument connects a robot. See
                    # agents/iteration_budget.py.
                    for _dead_tool, _dead_why in budget.take_unannounced_unavailable():
                        history.append({
                            "role": "system",
                            "content": unavailable_tool_notice(_dead_tool, _dead_why),
                        })

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

                if final_answer_only:
                    # No-progress guard: withdraw tools and steer the model
                    # to one final honest answer instead of a third
                    # identical failing call.
                    tools = None
                    history.append({"role": "system", "content": NO_PROGRESS_GUIDANCE})
            elif text_content:
                if self.memory:
                    self.memory.working_push(session_id, {"role": "assistant", "text": text_content})
                    await self._emit_brain_event(session_id, "memory_write", {"type": "episodic"})

                await self._send_text(
                    session_id, text_content,
                    model=turn_model, usage=turn_usage,
                )
                sent_response = True
                final_response_text = text_content
                break
            else:
                break

        if not sent_response:
            await self._send_text(session_id, "I processed your request but have nothing to report.")

        # Write-back, session eviction, snapshot and F2 compaction all
        # live in ``_finalize_turn`` now, which the caller runs from a
        # ``finally`` so the early returns above cannot skip them.

        # Return the final assistant text so synchronous callers
        # (phone chat_request → chat_response) get the FULL reply
        # rather than reconstructing it from working memory.
        return final_response_text or None

    async def handle_command_stream(self, session_id: str, text: str, context: Optional[dict] = None):
        """Streaming variant of handle_command with a per-session lock."""
        try:
            async with self._get_session_lock(session_id):
                return await self._handle_command_stream_impl(session_id, text, context)
        finally:
            # : tear down subagents tied to this parent session.
            self._w17_cancel_subsessions_nowait(session_id)

    async def _handle_command_stream_impl(self, session_id: str, text: str, context: Optional[dict] = None):
        """Streaming variant of handle_command. Guarded by the session
        lock above, and wrapped so ``_finalize_turn`` runs on every exit
        path — see ``_handle_command_impl``."""
        turn = self._begin_turn(session_id, text)
        try:
            return await self._handle_command_stream_body(
                session_id, text, context, turn,
            )
        finally:
            self._finalize_turn(session_id, turn)

    async def _handle_command_stream_body(
        self,
        session_id: str,
        text: str,
        context: Optional[dict],
        turn: dict,
    ):
        """
        Streaming variant of handle_command. Sends text deltas in real-time
        so the client gets token-by-token output.
        Falls back to non-streaming if LLM doesn't support it.
        """
        self._stamp_session_surface(session_id, context)
        # Explicit plan-mode entry/exit, before routing. `/plan` would
        # otherwise be read as the `/skill` prefix form in `_route_prompt`.
        if await self._maybe_handle_plan_meta_command(session_id, text):
            return
        if await self._maybe_handle_pending_tool_approval_text(session_id, text):
            return

        if not self._streaming_enabled or not self.llm.available:
            # ``_handle_command_impl`` directly, NOT ``handle_command``:
            # we already hold this session's lock and asyncio.Lock is
            # not reentrant, so re-entering the wrapper deadlocks the
            # turn. The nested turn owns the write-back and epilogue.
            turn["delegated"] = True
            await self._handle_command_impl(session_id, text, context)
            return

        if self._somatic_engine:
            self._somatic_engine.update_interaction(session_id, len(text))

        # WS1 — episode_save fire-and-forget on the stream path too.
        # See ``_save_episode_async`` docstring.
        self._save_episode_async(
            session_id=session_id,
            event_type="user_command",
            summary=text[:200],
            # The full text, not just the 200-char preview. ``summary``
            # is capped because it is what previews and list views
            # render, and that cap used to be the ONLY durable record
            # of what the operator said: everything past character 200
            # existed solely in ``conversation_history``, which
            # ``compact_session`` replaces wholesale. On any surface
            # without the web client's autosave (voice, CLI, a paired
            # phone) the rest of a long message was unrecoverable once
            # compaction fired. ``episodes_fts`` indexes ``detail``
            # alongside ``summary``, so this is searchable on arrival.
            detail=json.dumps({"text": text, "context": context or {}}),
        )

        # S1 live-path closure: the timeline side-channel scheduling
        # was previously fired unconditionally HERE, before tool
        # routing. v2026.5.48 grounded-memory closure moves that
        # scheduling AFTER tool routing — see the parallel block in
        # ``_handle_command_impl`` for the rationale. We can't make
        # the forced-tool decision before we know which tools are
        # routed, so the side-channel decision waits too.

        # WS3 — multi-agent pre-path parity. The non-stream branch
        # hands the turn to ``MultiAgentOrchestrator`` when enabled +
        # not in vision_ask mode + not a proactive source. The stream
        # branch used to skip this entirely, which meant enabling
        # multi-agent changed the assistant's behaviour depending on
        # whether the client opted in to streaming — drift the audit
        # called out in finding 20.
        context_data = context or {}
        vision_fast_path = context_data.get("channel") == "vision_ask"
        if (
            not vision_fast_path
            and self._multi_agent_enabled
            and self._multi_agent
            and self.llm
            and self.llm.available
            and context_data.get("source", "") != "proactive"
        ):
            try:
                response_text = await self._multi_agent.run(session_id, text, context)
                if response_text:
                    # Same as the non-stream multi-agent branch above.
                    _model, _usage = self._pop_multi_agent_attribution(session_id)
                    await self._try_send_sdui(
                        session_id, response_text,
                        model=_model, usage=_usage,
                    )
                    if self.memory:
                        # Full text (no [:300]) — see the non-stream
                        # multi-agent branch for rationale.
                        self.memory.working_push(
                            session_id,
                            {"role": "assistant", "text": response_text},
                        )
                    if self.learner:
                        # AUDIT-FIXES F-06, same as the non-stream branch.
                        self._track_background_task(
                            asyncio.ensure_future(
                                self.learner.on_message(session_id, "user", text)
                            )
                        )
                    return response_text
            except MultiAgentProviderError as exc:
                # Same as the non-stream branch: error frame, no
                # single-agent retry against the failing provider.
                logger.error(
                    "[%s] Multi-agent (stream) LLM provider failed: %s",
                    session_id[:8], exc,
                )
                await self._send_error(session_id, str(exc))
                return None
            except Exception as e:
                logger.warning(
                    f"Multi-agent (stream) failed, falling back to single-agent: {e}"
                )

        relevant_skills = await self._route_prompt(text, session_id=session_id)
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
        # Withhold the skills whose prerequisite is provably absent: no
        # key, no OAuth, no Docker, no robot. 79 of the 266 schemas on
        # the operator's brain, every one a call the model could only
        # lose. skills/availability.py also writes the prompt line that
        # tells the model what is off and why.
        tools = filter_unavailable_tools(tools)

        if self._mcp_client:
            mcp_tools = self._mcp_client.to_llm_tool_definitions()
            if mcp_tools:
                tools = (tools or []) + mcp_tools

        # Plan mode, exposure half (stream path). Mirrors the non-stream
        # branch above; see the comment there.
        tools, relevant_skills = self._apply_plan_mode_filter(
            session_id, tools, relevant_skills,
        )

        # v2026.5.48 grounded-memory closure (stream path). Mirrors the
        # non-stream branch — force the timeline tool when the gate
        # fires, AND always mount the proactive side-channel on
        # ``_R_TEMPORAL`` matches (forced_tool alone is not enough;
        # models still pick ``search_notes`` without the widget).
        forced_tool = self._force_tool_for_query(text, tools, session_id)
        try:
            if self._R_TEMPORAL.search(text or ""):
                self._track_background_task(asyncio.create_task(
                    self._maybe_emit_temporal_timeline(session_id, text)
                ))
        except Exception:
            logger.debug(
                "temporal-timeline side-channel: task schedule failed",
                exc_info=True,
            )

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
            query=self._coref_query_for_prompt(session_id, text or ""),
        )
        if specialist:
            system_prompt = self._build_specialist_system_prompt(specialist, system_prompt)

        if session_id not in self.conversation_history:
            self.conversation_history[session_id] = []
        # B7: a turn in flight counts as activity. Without this a long
        # turn (slow provider, tool loop) looks idle to the cap for its
        # whole duration.
        self._touch_session(session_id)

        # WS3 — paused-thoughts re-thread parity. Symmetric with the
        # non-stream prelude: when ``/api/consciousness/resume``
        # queued kind=thought fragments for this session, prepend
        # them as synthetic assistant messages so the LLM continues
        # the same thread instead of starting cold. The non-stream
        # branch always did this; the stream branch used to drop the
        # fragments on the floor.
        for paused in self.drain_paused_thoughts(session_id):
            fragment = paused.get("text") or ""
            if not fragment:
                continue
            self.conversation_history[session_id].append({
                "role": "assistant",
                "content": f"[RESUMED THOUGHT] {fragment}",
            })

        user_content = perception_frame.to_llm_user_content(text)
        # WS4 — vision context attach mirrors the non-stream prelude.
        user_content = self._attach_vision_context(
            user_content, context=context, session_id=session_id,
        )
        self.conversation_history[session_id].append({"role": "user", "content": user_content})
        turn["user_recorded"] = True
        # Per-request view; see the matching comment in the non-stream
        # body. ``_finalize_turn`` commits everything past ``base_len``.
        history = self._compact_context(self.conversation_history[session_id].copy())
        turn["working"] = history
        turn["base_len"] = len(history)
        from models.protocol import StreamDeltaPayload

        got_final_text = False
        any_tool_ran = False
        refusal_retry_used = False
        empty_retry_used = False
        pending_retry_addition: Optional[str] = None
        if self.refusal_handler.is_ack_execution(text):
            pending_retry_addition = self.refusal_handler.ACK_EXECUTION_FAST_PATH_INSTRUCTION
        from agents.iteration_budget import (
            GUARD_OK,
            GUARD_STOP,
            GUARD_WARN,
            IterationBudget,
            NO_PROGRESS_GUIDANCE,
            NO_PROGRESS_WARNING,
            unavailable_tool_notice,
        )
        budget = IterationBudget(self._max_iterations, self._tool_loop_max_seconds)
        # Only the guard's STOP level withdraws tools; see the
        # non-streaming path and agents/iteration_budget.py.
        final_answer_only = False
        no_progress_warned = False
        while budget.start_iteration():
            effective_system_prompt = system_prompt
            if pending_retry_addition:
                effective_system_prompt = (
                    f"{system_prompt}\n\n[RETRY_STEER]\n{pending_retry_addition}"
                )
                pending_retry_addition = None
            messages = [{"role": "system", "content": effective_system_prompt}, *history]
            # Same screenshot lifecycle as the non-streaming loop.
            self._note_agent_round(session_id)
            self._maybe_prune_tool_images(session_id)
            messages = self._materialize_tool_images(session_id, messages)
            # Withdraw any tool the precondition guard has written off
            # for this turn. Rebound once per round so every ``tools if
            # tools else None`` below this point sees the same list, and
            # so a tool withdrawn on round 3 is genuinely absent from
            # round 4 rather than merely discouraged in prose.
            tools = budget.filter_tools(tools)
            stream_id = str(uuid4())[:8]
            accumulated_text = ""
            streamed_text = False
            tool_calls_received = []

            # Stream coalescer (AUDIT-r14 round3 surface spec #1): instead of
            # one WS frame per token, batch incremental pieces into ~100ms
            # windows and emit a single stream_delta per window. Cuts frame
            # rate ~10-50x → far smoother chat. Client-transparent: it still
            # appends `delta`, just fewer/larger ones. Arrival-driven (no
            # background timer to manage in the hot loop); is_final / tool /
            # error boundaries force-flush. Set FERAL_STREAM_BATCH_MS=0 to
            # restore per-token frames for debugging.
            try:
                _stream_batch_ms = int(os.environ.get("FERAL_STREAM_BATCH_MS", "100"))
            except (TypeError, ValueError):
                _stream_batch_ms = 100
            _stream_buf: list[str] = []
            _stream_last_flush = time.monotonic()

            async def _flush_stream_prose() -> None:
                nonlocal _stream_buf, _stream_last_flush
                if _stream_buf:
                    merged = "".join(_stream_buf)
                    _stream_buf = []
                    await self.send(session_id, FeralMessage(
                        session_id=session_id, hop="brain", type="stream_delta",
                        payload=StreamDeltaPayload(
                            delta=merged, stream_id=stream_id, is_final=False,
                        ).model_dump(),
                    ))
                _stream_last_flush = time.monotonic()

            try:
                stream_model = getattr(self.llm, 'model_name', 'llm')
                await self._emit_brain_event(session_id, "llm_call", {"model": stream_model})
                stream_kw: dict[str, Any] = {"call_site": "chat"}
                if forced_tool:
                    stream_kw["force_tool"] = forced_tool
                # Force the tool ONLY on the first iteration — see the
                # paired comment in ``_handle_command_impl`` for why.
                forced_tool = None
                try:
                    stream_iter = self.llm.chat_stream(
                        messages=messages,
                        tools=tools if tools else None,
                        **stream_kw,
                    )
                except TypeError:
                    # Test adapters that only accept (messages, tools).
                    # Try shedding ``force_tool`` first, then ``call_site``.
                    stream_kw.pop("force_tool", None)
                    try:
                        stream_iter = self.llm.chat_stream(
                            messages=messages,
                            tools=tools if tools else None,
                            **stream_kw,
                        )
                    except TypeError:
                        stream_iter = self.llm.chat_stream(
                            messages=messages,
                            tools=tools if tools else None,
                        )
                async for delta in stream_iter:
                    if delta["type"] == "text_delta":
                        piece = delta.get("content", "")
                        if not piece:
                            continue
                        streamed_text = True
                        accumulated_text += piece
                        if _stream_batch_ms <= 0:
                            # Legacy per-token path (debug).
                            await self.send(session_id, FeralMessage(
                                session_id=session_id, hop="brain", type="stream_delta",
                                payload=StreamDeltaPayload(
                                    delta=piece, stream_id=stream_id, is_final=False,
                                ).model_dump(),
                            ))
                        else:
                            _stream_buf.append(piece)
                            if (time.monotonic() - _stream_last_flush) * 1000.0 >= _stream_batch_ms:
                                await _flush_stream_prose()
                    elif delta["type"] == "tool_call_delta":
                        tc = delta.get("tool_call") or {}
                        if tc:
                            tool_calls_received.append(tc)
                    elif delta["type"] == "done":
                        # Flush any buffered prose before the terminal frame.
                        await _flush_stream_prose()
                        if streamed_text:
                            # Carry per-turn attribution on the terminal frame
                            # so the UI can show which model answered and what
                            # the turn cost. The provider reports both on its
                            # own done event; before this they were dropped
                            # here, so the client had no way to distinguish
                            # "answered by the configured model" from
                            # "answered by hop 4 of the failover chain".
                            # Absent keys stay absent rather than rendering
                            # a fabricated zero.
                            await self.send(session_id, FeralMessage(
                                session_id=session_id, hop="brain", type="stream_delta",
                                payload=StreamDeltaPayload(
                                    delta="", stream_id=stream_id, is_final=True,
                                    model=str(delta.get("model") or ""),
                                    usage=delta.get("usage") or {},
                                ).model_dump(),
                            ))
                    elif delta["type"] == "budget_exceeded":
                        # WS8 — surface as a structured frame, not a
                        # stack trace. Lane 12 renders the banner.
                        await _flush_stream_prose()
                        await self._emit_budget_exceeded(
                            session_id=session_id,
                            budget=delta.get("payload") or {},
                        )
                        return
                    elif delta["type"] == "error":
                        await _flush_stream_prose()
                        # Error frame, not "Stream error: ..." prose:
                        # ``_send_text`` records what it sends and
                        # ``_finalize_turn`` would commit the provider
                        # failure as the assistant's reply.
                        await self._send_error(
                            session_id,
                            str(delta.get("content") or "unknown stream error"),
                            code="llm_stream_error",
                        )
                        return
                # Safety net: flush any tail prose if the stream ended
                # without an explicit `done` event.
                await _flush_stream_prose()
            except Exception as e:
                logger.error(f"Streaming failed, falling back: {e}")
                # The stream path already appended this turn's user
                # row to ``conversation_history``. The non-stream body
                # will append it again, duplicating the turn. Drop
                # the trailing user row here so the fallback re-adds
                # exactly one copy.
                hist = self.conversation_history.get(session_id) or []
                if hist and hist[-1].get("role") == "user":
                    hist.pop()
                    turn["user_recorded"] = False
                # ``_handle_command_impl``, not ``handle_command``: the
                # session lock is already held by our caller and
                # asyncio.Lock is not reentrant. The nested turn owns
                # the write-back and the epilogue.
                turn["delegated"] = True
                await self._handle_command_impl(session_id, text, context)
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

            if normalized_tool_calls and final_answer_only:
                # Loop guard already withdrew tools; a further tool call
                # means the model is stuck — stop before the dangling
                # tool_calls assistant msg lands in history.
                logger.warning(
                    "[%s] (stream) tool call after loop guard closed tools; stopping",
                    session_id[:8],
                )
                break

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

                # WS5 — multi-actuator ordered WS frames on the
                # stream path. Symmetric with the non-stream branch:
                # emit ``tool_start`` for every tool in tool_call
                # order BEFORE any execution starts, then execute
                # (in parallel under ``FERAL_MAX_PARALLEL_TOOLS``),
                # then emit ``tool_result`` in the same order.
                parallel_cap = max(
                    1, int(os.environ.get("FERAL_MAX_PARALLEL_TOOLS", "6"))
                )
                sem = asyncio.Semaphore(parallel_cap)

                for tc in normalized_tool_calls:
                    await self._emit_tool_start(session_id, tc)

                async def _stream_run(tc: dict) -> dict:
                    async with sem:
                        t_start = time.time()
                        # See the non-streaming loop: this path writes its
                        # own execution_log row, so it claims the call and
                        # the executor does not write a duplicate.
                        with claimed_by_caller():
                            result_data = await self._execute_tool_call_for_llm(
                                session_id, tc, relevant_skills,
                            )
                        latency_ms = (time.time() - t_start) * 1000
                        return {
                            "tc": tc,
                            "result": result_data,
                            "latency_ms": latency_ms,
                        }

                outputs = await asyncio.gather(
                    *[_stream_run(tc) for tc in normalized_tool_calls]
                )

                for output in outputs:
                    tc = output["tc"]
                    result_data = output["result"]
                    latency_ms = output["latency_ms"]
                    # Parity with the non-streaming loop: pending
                    # approval is its own status, not a failure.
                    stream_audit_status = audit_status_of(result_data)
                    stream_tool_pending = stream_audit_status == "pending_approval"
                    stream_tool_success = stream_audit_status == "success"
                    await self._emit_tool_result(session_id, tc, result_data, latency_ms)
                    await self._emit_brain_event(
                        session_id, "tool_exec",
                        {"tool": tc["name"], "success": stream_tool_success},
                    )

                    if stream_audit_status != "pending_approval":
                        self.tool_runner.record_trust_outcome(
                            tc["name"], success=stream_tool_success,
                        )

                    if self._tool_genesis:
                        self._tool_genesis.record_tool_call(
                            session_id, tc["name"], tc.get("args", {}),
                        )

                    if self.memory:
                        parts = tc["name"].split("__", 1)
                        skill_id = parts[0] if len(parts) == 2 else tc["name"]
                        endpoint_id = parts[1] if len(parts) == 2 else ""
                        await self.memory.log_execution(
                            session_id=session_id, skill_id=skill_id,
                            endpoint_id=endpoint_id, args=tc.get("args", {}),
                            result_status=stream_audit_status,
                            result_summary=json.dumps(result_data)[:300],
                            latency_ms=latency_ms,
                        )
                    stream_tool_call_id = tc.get("id") or str(uuid4())[:8]
                    history.append({
                        "role": "tool",
                        "tool_call_id": stream_tool_call_id,
                        "name": tc["name"],
                        # Same per-tool budget and same image lift as the
                        # non-streaming path; see the comment there.
                        "content": self._serialize_tool_result_for_history(
                            session_id, stream_tool_call_id, tc["name"], result_data,
                        ),
                    })
                    guard_level = GUARD_OK if stream_tool_pending else budget.observe_tool(
                        tc["name"], tc.get("args", {}), stream_tool_success, result_data
                    )
                    if guard_level == GUARD_STOP:
                        final_answer_only = True
                    elif guard_level == GUARD_WARN and not no_progress_warned:
                        # Warn once and leave the toolset intact; see the
                        # non-streaming path.
                        no_progress_warned = True
                        history.append(
                            {"role": "system", "content": NO_PROGRESS_WARNING}
                        )
                    anti_loop_guidance = result_data.get("_anti_loop_guidance")
                    if anti_loop_guidance:
                        history.append({"role": "system", "content": anti_loop_guidance})

                    # Mirrors the non-streaming path; see the comment there.
                    for _dead_tool, _dead_why in budget.take_unannounced_unavailable():
                        history.append({
                            "role": "system",
                            "content": unavailable_tool_notice(_dead_tool, _dead_why),
                        })

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

                if final_answer_only:
                    # No-progress guard: withdraw tools, steer to one final
                    # honest answer.
                    tools = None
                    history.append({"role": "system", "content": NO_PROGRESS_GUIDANCE})
                continue

            if accumulated_text:
                got_final_text = True
                if self.memory:
                    self.memory.working_push(session_id, {"role": "assistant", "text": accumulated_text})
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

        # Write-back, eviction, snapshot and F2 compaction live in
        # ``_finalize_turn``, run from a ``finally`` by the caller —
        # symmetric with the non-stream body.

        # Symmetric with `_handle_command_impl`: hand the full final
        # text back to synchronous callers (phone chat_request).
        return accumulated_text if got_final_text else None

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

    def _touch_session(self, session_id: str) -> None:
        """Stamp ``session_id`` as active now.

        B7: the input to :meth:`_evict_stale_sessions`. Called at the
        start of a turn as well as at its end so a session that is
        mid-turn when the cap fires is never the victim; a turn can run
        for minutes behind a slow provider, and the row that would prove
        it alive is not written until the turn finishes.
        """
        if session_id:
            self._session_last_active[session_id] = time.time()

    def _forget_session_activity(self, session_id: str) -> None:
        self._session_last_active.pop(session_id, None)

    def _evict_stale_sessions(self):
        """Evict the LEAST RECENTLY ACTIVE sessions once the dict is over
        ``_conversation_max_sessions``.

        B7: this used to sort by ``len(conversation_history[sid])`` and
        delete the head, so it evicted the SHORTEST transcripts while
        claiming to evict the oldest. Measured with the cap at 5: five
        abandoned 50-row sessions and one 1-row session the operator had
        just opened, and the 1-row session was the one destroyed while
        all five dead ones survived. Length is not age. A session is
        short because it just began at least as often as because it is
        finished.

        This cap is also the only cleanup most sessions ever get.
        ``on_session_disconnect`` has a single caller, the WebSocket
        handler in ``api/server.py``, so channel sessions
        (``channel_{type}_{user_id}``), cron sessions (``routine-{id}``)
        and REST turns never reach it.

        A session with no stamp sorts as infinitely old. That is the safe
        direction: an unstamped entry is one nothing has claimed through
        a turn, and treating it as fresh would let it pin a cap slot
        forever.
        """
        if len(self.conversation_history) <= self._conversation_max_sessions:
            return
        sorted_sids = sorted(
            self.conversation_history,
            key=lambda sid: self._session_last_active.get(sid, 0.0),
        )
        to_remove = len(self.conversation_history) - self._conversation_max_sessions
        for sid in sorted_sids[:to_remove]:
            del self.conversation_history[sid]
            # Drop the per-session lock too so long-running brains don't
            # grow the lock dict without bound.
            self._session_locks.pop(sid, None)
            self._session_surfaces.pop(sid, None)
            # Same reasoning for the consolidation clocks: the ladder's
            # background tick iterates them, so a stale entry is a
            # forever-retrying compaction on a transcript that is gone.
            self._forget_consolidation_state(sid)
            # And the activity stamp eviction itself ranks by, or the
            # next pass would rank a session that no longer exists.
            self._forget_session_activity(sid)
            # The image side table holds whole base64 payloads; evicting
            # the transcript without it would leak megabytes per session.
            self._forget_tool_images(sid)

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
        self._forget_consolidation_state(session_id)
        self._forget_session_activity(session_id)
        self._forget_tool_images(session_id)
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

    # Memory-recall + temporal-recall phrasings — covers both the
    # "remember/recall the project name" intent and the fused-timeline
    # intent ("what did I do yesterday", "summarize my morning",
    # "what happened today", "earlier today"). Matching this regex
    # routes ``notes_memory`` to the LLM tool set, whose
    # ``fused_timeline`` endpoint description steers the model to call
    # the right tool for any natural-language temporal window.
    _R_MEMORY = re.compile(
        r"\b("
        # "what did/have I do/say/ask/save/note/work on/focus on/accomplish"
        r"what\s+(?:did|have)\s+i\s+(?:do|done|say|said|ask|asked|save|saved|note|noted|work(?:ed)?\s+on|focus(?:ed)?\s+on|accomplish(?:ed)?)|"
        # "what did/has my robot/device/cutebot/it do/done" — device recall
        # (parallels the "what did I do" clause; covers voice-driven
        # cutebot episodes that BUG 2 (B) used to miss). Accepts an
        # optional "my"/"the" determiner so "what did THE device do"
        # also matches.
        r"what\s+(?:did|has|have)\s+(?:(?:my|the)\s+)?(?:robot|device|cutebot|drone|roomba|it)\s+(?:do|done|did)|"
        # "what is/was my robot/cutebot doing" — progressive recall
        r"what\s+(?:is|was|were)\s+(?:(?:my|the)\s+)?(?:robot|device|cutebot|drone|roomba|it)\s+doing|"
        r"what\s+(?:is|was|were)\s+i\s+doing|"
        # "summarize/recap/review <window>"
        r"(?:summari[sz]e|recap|review)\s+(?:today|yesterday|tonight|this\s+(?:morning|afternoon|evening|week|day)|my\s+(?:morning|afternoon|evening|night|day|week|month))|"
        # "what happened/went on/was going on" — any temporal context
        r"what\s+(?:happened|went\s+on|was\s+going\s+on)\b|"
        # legacy chat-recall phrasings
        r"what\s+(?:was|were)\s+(?:we\s+)?(?:talking|chatting|discussing)\s+about|"
        r"what\s+did\s+we\s+(?:discuss|talk\s+about)|"
        # "earlier today/yesterday/this morning"
        r"earlier\s+(?:today|yesterday|this\s+(?:morning|afternoon|evening))|"
        # generic recall verbs
        r"recall(?:\s+(?:that|the))?|"
        r"do\s+you\s+remember|"
        # "my notes …" and "my <window> so far|recap|summary|review"
        r"my\s+notes(?:\b|\s+from)|"
        r"my\s+(?:morning|afternoon|evening|day|week)\s+(?:so\s+far|recap|summary|review|in\s+review)"
        r")\b",
        re.I,
    )

    # Strict temporal-recall subset of ``_R_MEMORY`` — the queries
    # where a fused-timeline card is the right v1 affordance. Used by
    # ``_maybe_emit_temporal_timeline`` to decide whether the live
    # chat-stream path should proactively dispatch ``timeline_fusion``
    # as a side-channel (in parallel with the LLM stream). This
    # exists separately from ``_R_MEMORY`` because the latter also
    # matches non-temporal recall ("do you remember the project
    # name?", "my notes from the meeting") — those should not push a
    # TimelineCard at the user. The patterns below are exactly the
    # clauses in ``_R_MEMORY`` that anchor on a temporal window or
    # past-event verb.
    _R_TEMPORAL = re.compile(
        r"\b("
        # "what did/have I do/done/say/work on/focus on/accomplish …"
        r"what\s+(?:did|have)\s+i\s+(?:do|done|say|said|ask|asked|save|saved|note|noted|work(?:ed)?\s+on|focus(?:ed)?\s+on|accomplish(?:ed)?)|"
        # "what did/has my robot/device/cutebot/it do/done" — voice-driven
        # device recall ("what did my robot do yesterday?"). Parallels the
        # "what did I do" clause so the fused-timeline side-channel mounts
        # for device episodes too. Accepts optional "my"/"the" determiner.
        r"what\s+(?:did|has|have)\s+(?:(?:my|the)\s+)?(?:robot|device|cutebot|drone|roomba|it)\s+(?:do|done|did)|"
        # "what is/was my robot/cutebot doing" — progressive recall
        r"what\s+(?:is|was|were)\s+(?:(?:my|the)\s+)?(?:robot|device|cutebot|drone|roomba|it)\s+doing|"
        r"what\s+(?:is|was|were)\s+i\s+doing|"
        # "summarize/recap/review <window>"
        r"(?:summari[sz]e|recap|review)\s+(?:today|yesterday|tonight|this\s+(?:morning|afternoon|evening|week|day)|my\s+(?:morning|afternoon|evening|night|day|week|month))|"
        # "what happened/went on/was going on" — temporal recall
        r"what\s+(?:happened|went\s+on|was\s+going\s+on)\b|"
        # chat-recall ("what were we discussing") — fused timeline
        # surfaces the actual chat-turn episodes that match.
        r"what\s+(?:was|were)\s+(?:we\s+)?(?:talking|chatting|discussing)\s+about|"
        r"what\s+did\s+we\s+(?:discuss|talk\s+about)|"
        # "earlier today/yesterday/this morning"
        r"earlier\s+(?:today|yesterday|this\s+(?:morning|afternoon|evening))|"
        # "my <window> so far|recap|summary|review|in review"
        r"my\s+(?:morning|afternoon|evening|day|week)\s+(?:so\s+far|recap|summary|review|in\s+review)"
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

    # Scheduled-automation phrasings that must become a RECURRING routine
    # (feral_routines), never a one-shot reminder. "every day at 5pm",
    # "every morning", "every 30 minutes", "daily", "weekly", "every Monday".
    _R_ROUTINE_RECURRING = re.compile(
        r"\b("
        r"every\s+(?:day|morning|afternoon|evening|night|weekday|hour|week|"
        r"\d+\s*(?:m|mins?|minutes?|h|hrs?|hours?))|"
        r"each\s+(?:day|morning|afternoon|evening|night)|"
        r"every\s+(?:mon|tues?|wed(?:nes)?|thur?s?|fri|sat(?:ur)?|sun)(?:day)?|"
        r"daily|weekly|nightly"
        r")\b",
        re.I,
    )

    # One-shot scheduled-action markers — a clock time ("at 3pm", "at 3:01pm",
    # "at 15:01") or an explicit single-fire phrase ("one time", "once"). These
    # only route to feral_routines when paired with an action (see
    # ``_heuristic_route``) so "remind me at 3pm" stays a reminder.
    _R_ROUTINE_ONESHOT = re.compile(
        r"(?:\bat\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)\b)|"
        r"(?:\bat\s+\d{1,2}:\d{2}\b)|"
        r"\b(?:one\s+time|just\s+once|once)\b",
        re.I,
    )

    # Robot/CuteBot LED strip — NOT Philips Hue / Home Assistant lights.
    # Text chat was routing "make robot lights red" to smart_home_hue because
    # the Hue manifest owns generic "lights" triggers; voice already pinned
    # cutebot__set_lights. Explicit robot+cutebot+qtbot phrasings plus
    # ambiguous "lights … red/off" when the session's active subject is a robot.
    _R_ROBOT_SUBJECT = re.compile(r"\b(?:robot|cutebot|qtbot)\b", re.I)
    _R_ROBOT_LIGHTS = re.compile(
        r"\b("
        r"(?:robot|cutebot|qtbot)(?:'?s)?\s+(?:the\s+)?lights?|"
        r"lights?\s+(?:on\s+)?(?:the\s+)?(?:robot|cutebot|qtbot)|"
        r"(?:make|turn|set|change|flash)\s+(?:the\s+)?(?:robot|cutebot|qtbot)\s+(?:the\s+)?lights?|"
        r"(?:robot|cutebot|qtbot).{0,40}\b(?:red|green|blue|yellow|purple|orange|white|off|on|dim|bright|color|colour)|"
        r"\b(?:red|green|blue|yellow|purple|orange|white|off|on|dim|bright|color|colour).{0,40}(?:robot|cutebot|qtbot)"
        r")\b",
        re.I,
    )
    _R_LIGHTS_ACTION = re.compile(
        r"\b("
        r"(?:turn|set|make|change|flash)\s+(?:the\s+)?lights?\s+(?:to\s+)?(?:red|green|blue|off|on)|"
        r"lights?\s+(?:to\s+)?(?:red|green|blue|off|on)|"
        r"(?:red|green|blue|yellow|purple|orange|white)\s+lights?"
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

    # "Is anything running on this machine?", process / activity
    # introspection.
    #
    # The capability exists and works: ``coding_tools__bash`` running
    # ``pgrep -fl claude`` passes the shell policy and answers the
    # question. Routing never offered it. Measured on the shipped
    # catalog before this regex existed:
    #
    #   "is claude working on something right now" -> confident_lead,
    #        top5 = macos_ax, desktop_control, external_agent,
    #               screen_capture, messaging_channels  (no coding_tools)
    #   "is claude still going"      -> ambiguous, no coding_tools
    #   "what's my machine doing"    -> ambiguous, no coding_tools
    #   "check if claude is busy"    -> ambiguous, no coding_tools
    #   "what's using my cpu"        -> ambiguous, no coding_tools
    #   "did claude finish yet"      -> ambiguous, no coding_tools
    #   "what is my mac doing right now" -> trigger_strong on WEB_SEARCH
    #
    # That last one is the dangerous shape: a high-confidence WRONG
    # match returns early and suppresses both the LLM disambiguation and
    # the action fallback, so the better answer is never considered.
    #
    # A regex tier is the right mechanism rather than new trigger
    # phrases: it runs before the keyword scorer, so it also displaces
    # the wrong strong match, which trigger phrases on their own cannot
    # do (they would merely tie at 20.0 and lose the ordering coin flip).
    #
    # Placed AFTER the memory/calendar/reminder/health/vision shortcuts
    # so "how did I sleep" and friends keep their existing owners.
    _R_PROCESS_QUERY = re.compile(
        r"("
        # "is claude code still running", "anything running right now",
        # "what processes are running", "show me running processes"
        r"\b(?:is|are|anything|something|what|what's|whats|which|show|list)\b"
        r"[^.?!]{0,40}\brunning\b|"
        # "list processes" / "show me all processes"
        r"\b(?:list|show|kill)\s+(?:me\s+)?(?:the\s+|all\s+)?process(?:es)?\b|"
        # "is claude still going", "is claude working on something",
        # "check if claude is busy". The lookahead keeps "are you busy"
        # and "is it still going" out: those are about the assistant or
        # an unnamed referent, not about a process on the machine.
        r"\b(?:is|are)\s+(?!you\b|i\b|it\b|we\b|they\b|there\b|that\b|this\b)"
        r"(?:\w+\s+){0,3}(?:still\s+)?(?:going|busy|working\s+on)\b|"
        # "did claude finish yet"
        r"\bdid\s+(?!i\b|you\b|we\b)(?:\w+\s+){0,2}finish(?:ed)?\b|"
        # "what's my machine doing", "what is my mac doing right now"
        r"\bwhat(?:'s|s|\s+is)\s+(?:my\s+|this\s+|the\s+)?"
        r"(?:mac|macbook|machine|computer|laptop|system)\s+doing\b|"
        # "what's using my cpu", "what's eating all my memory"
        r"\b(?:using|eating|hogging|taking)\s+(?:up\s+)?"
        r"(?:my\s+|all\s+(?:my|the)\s+)?(?:cpu|memory|ram)\b"
        r")",
        re.I,
    )

    # Skills that can actually answer a process-activity question, most
    # capable first. ``coding_tools__bash`` is the general answer (ps,
    # pgrep, top); ``desktop_control__list_running_apps`` is the better
    # one for "what apps do I have open" and rides along via the keyword
    # ranking below.
    _PROCESS_QUERY_SKILLS = ("coding_tools",)

    def _heuristic_route(self, text: str, session_id: str = "") -> tuple[list["SkillManifest"], str]:
        """Try to pick relevant skills without an LLM call.

        Returns ``(skills, reason)`` where ``reason`` is one of
        ``"empty"``, ``"prefix"``, ``"regex:<name>"`` (including
        ``"regex:process_query"``),
        ``"trigger_strong"``, ``"confident_lead"``,
        ``"action_fallback"``, ``"small_catalog"``, ``"carry:routine"``,
        or ``"ambiguous"`` (the only value that triggers an LLM
        disambiguation call).

        The orchestrator's primary contract: ``"ambiguous"`` is the
        ONLY exit that fires an LLM call. Every other return value is
        a free routing decision.

        ``session_id`` is optional and enables multi-turn routine-intent
        carry-over: when the user expressed a recurring/scheduled-action
        intent on a recent prior turn but the brain is still mid-setup
        (asked a clarifying question and the user's new turn doesn't
        repeat the time marker), the route still hoists ``feral_routines``
        so the model creates the real routine instead of inventing a
        "background task workaround".
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

        # 2.5 Scheduled device/action requests → feral_routines.
        # A recurring marker ("every day at 5pm", "daily") OR a one-shot
        # clock time paired with an action ("at 3:01pm follow the line",
        # "run the cutebot one time today at 3pm") should create a routine,
        # NOT a one-shot reminder. Genuine "remind me" notifications were
        # already claimed by ``_R_REMINDER`` above, so they never reach here.
        # We expose feral_routines FIRST plus the top keyword matches so the
        # model can still pick the device skill (e.g. cutebot) for the
        # routine payload.
        if "feral_routines" in self.skills.skills:
            recurring_hit = self._R_ROUTINE_RECURRING.search(stripped)
            oneshot_hit = self._R_ROUTINE_ONESHOT.search(stripped)
            current_hit = bool(
                recurring_hit
                or (oneshot_hit and self._query_implies_action(stripped))
            )
            # Multi-turn carry-over: a prior turn in this session already
            # expressed a routine intent ("every night at 9") and the
            # brain asked a clarifying question; the user's current
            # follow-up names the action ("just make it spin") but no
            # longer repeats the time marker. Without this carry-over the
            # heuristic would route only to the action skill (cutebot) and
            # the model would invent a "background task workaround"
            # because feral_routines would only be reachable via the
            # always-include fallback (appended last, easily missed).
            # Guarded by ``_query_implies_action`` so an unrelated chit-
            # chat turn after a routine ask doesn't sticky-route forever.
            carry_hit = bool(
                not current_hit
                and session_id
                and self._query_implies_action(stripped)
                and self._has_pending_routine_intent(session_id)
            )
            if current_hit or carry_hit:
                ranked = self.skills.find_skills_for_query(stripped, top_k=4)
                result = [self.skills.skills["feral_routines"]]
                result += [s for s in ranked if s.skill_id != "feral_routines"]
                return (result[:5], "regex:routine" if current_hit else "carry:routine")

        # 2.6 Robot LED strip → cutebot, never smart_home_hue. Generic
        # "turn on the lights" still routes to Hue; only robot-scoped
        # light commands (explicit mention or session coref subject) land here.
        if self._query_is_robot_lights(stripped, session_id):
            if "cutebot" in self.skills.skills:
                return ([self.skills.skills["cutebot"]], "regex:robot_lights")

        # 2.7 "Is anything running on this machine?", see
        # ``_R_PROCESS_QUERY``. Hoist the skill that can answer it, then
        # keep the keyword ranking behind it so a phrasing about apps
        # ("what apps are running") still surfaces
        # ``desktop_control__list_running_apps`` and a phrasing about a
        # background job still surfaces ``background_task``.
        if self._R_PROCESS_QUERY.search(stripped):
            hoisted = [
                self.skills.skills[sid]
                for sid in self._PROCESS_QUERY_SKILLS
                if sid in self.skills.skills
            ]
            if hoisted:
                hoisted_ids = {s.skill_id for s in hoisted}
                ranked = self.skills.find_skills_for_query(stripped, top_k=5)
                result = hoisted + [s for s in ranked if s.skill_id not in hoisted_ids]
                return (result[:5], "regex:process_query")

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

    def _query_is_robot_lights(self, text: str, session_id: str = "") -> bool:
        """True when ``text`` is a robot/CuteBot LED command, not Hue.

        Matches explicit robot+cutebot+qtbot light phrasings, or an
        ambiguous lights/color/off command when the session's active
        subject (coref tracker or recent history) is robot-scoped.
        """
        stripped = (text or "").strip()
        if not stripped:
            return False
        if self._R_ROBOT_LIGHTS.search(stripped):
            return True
        if not self._R_LIGHTS_ACTION.search(stripped):
            return False
        if self._R_ROBOT_SUBJECT.search(stripped):
            return True
        if session_id:
            try:
                prior = self._last_referenced_subject(session_id) or ""
            except Exception:
                prior = ""
            if prior and self._R_ROBOT_SUBJECT.search(prior):
                return True
        return False

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

    # Coreference cue words that mark an utterance as a follow-up referring
    # back to a recently-discussed subject/device rather than a fresh topic.
    _COREF_CUES = {
        "it", "that", "this", "them", "those", "these",
        "same", "again", "one", "now", "there",
    }
    # Bare command verbs that, on their own (or with a coref cue), are
    # follow-ups whose object is implied by the prior turn.
    _COREF_BARE_VERBS = {
        "check", "do", "run", "try", "go", "start", "stop", "continue",
        "repeat", "retry", "again", "redo", "rerun",
    }
    # Function words that carry no concrete object. Used to tell a truly
    # underspecified follow-up ("check now") from a self-contained command
    # that merely starts with a bare verb ("check the cutebot").
    #
    # Voice fix (live-voice coref hole): "how about now" / "what about now"
    # were classified as concrete turns because "how"/"what"/"about" were
    # content words. Treating them as stopwords (in this narrow follow-up
    # context — not in routing at large) lets the same short-utterance
    # gate identify them as underspecified follow-ups so the active subject
    # is reused. We additionally pattern-match "how about" / "what about"
    # below (see ``_R_FOLLOWUP_ABOUT``) so a multi-word follow-up never
    # bypasses the word-set gate.
    _COREF_STOPWORDS = {
        "the", "a", "an", "please", "just", "to", "of", "for", "on",
        "my", "your", "right", "ok", "okay", "then", "too", "also",
        # voice-follow-up steerers ("how about now", "what about now",
        # "how about", "what about") — see _R_FOLLOWUP_ABOUT.
        "how", "what", "about",
    }

    # Explicit follow-up phrases that should ALWAYS be treated as
    # underspecified follow-ups, regardless of the cue/bare-verb word
    # gate. The realtime path commits "how about now" / "what about now"
    # as a fresh transcript with the prior subject only implied — without
    # this regex the (short-utterance, content-word) gate would still
    # need every token to be in the stopword set, which is brittle.
    _R_FOLLOWUP_ABOUT = re.compile(
        r"^(?:how|what)\s+about(?:\s+(?:now|it|that|this|them))?\??$",
        re.I,
    )

    def _coref_state(self) -> dict[str, str]:
        """Lazily-initialised per-session "active subject" map.

        Tracks the most recent *concrete* (non-underspecified) user
        utterance per session so multi-turn follow-ups resolve against
        the real topic instead of an intermediate pronoun-only turn
        (e.g. "check the cutebot" → "do it" → "again" all resolve to the
        cutebot, not to "do it"). Lazy so it survives instances built in
        tests without touching ``__init__``.
        """
        store = getattr(self, "_session_active_subject", None)
        if store is None:
            store = {}
            self._session_active_subject = store
        return store

    def _coref_query_state(self) -> dict[str, str]:
        """Per-session cache of the coref-resolved routing text for the
        current turn, so the same resolution can be reused when building
        the system prompt (feeds tool selection + prompt, not just
        routing). Cleared on concrete turns."""
        store = getattr(self, "_session_coref_query", None)
        if store is None:
            store = {}
            self._session_coref_query = store
        return store

    def _is_underspecified_followup(self, text: str) -> bool:
        """True when ``text`` is a short follow-up whose object is implied
        by a prior turn (carries a coref cue or is a bare command verb).

        Conservative: only short utterances (≤ 5 words) qualify so genuine
        new topics are never treated as follow-ups.
        """
        stripped = (text or "").strip()
        if not stripped:
            return False
        # Explicit voice-followup phrases — "how about now",
        # "what about now", "how about", "what about" — short-circuit
        # the cue/word-set gate so realtime turns with these stems are
        # always coref-resolved against the active subject.
        if self._R_FOLLOWUP_ABOUT.match(stripped.rstrip("?!. ")):
            return True
        words = [w.strip(".,!?;:") for w in stripped.lower().split()]
        words = [w for w in words if w]
        if not words or len(words) > 5:
            return False
        has_cue = any(w in self._COREF_CUES for w in words)
        bare_cmd = words[0] in self._COREF_BARE_VERBS
        if not (has_cue or bare_cmd):
            return False
        # A bare verb / cue with an explicit object ("check the cutebot")
        # is self-contained, not a follow-up. Only treat it as
        # underspecified when it has NO content word (every token is a
        # cue, a bare verb, or a function word).
        noncontent = self._COREF_CUES | self._COREF_BARE_VERBS | self._COREF_STOPWORDS
        has_content_word = any(w not in noncontent for w in words)
        return not has_content_word

    def _set_active_subject(self, session_id: str, text: str) -> None:
        """Record a concrete utterance as the session's active subject."""
        if not session_id:
            return
        t = (text or "").strip()
        if t:
            self._coref_state()[session_id] = t[:200]

    def _last_referenced_subject(self, session_id: str) -> str:
        """Return the most recent prior *concrete* subject for this session.

        Resolution order:
          1. the tracked active subject (set on the last concrete turn —
             this is what lets the reference survive intervening
             pronoun-only follow-ups);
          2. a scan of the orchestrator's conversation history that
             SKIPS underspecified follow-ups (so "do it again" never
             resolves to a previous "check it");
          3. working memory as a last resort.
        Returns "" when nothing usable is found.
        """
        tracked = self._coref_state().get(session_id, "")
        if tracked:
            return tracked

        history = self.conversation_history.get(session_id) or []
        fallback = ""
        for msg in reversed(history):
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if isinstance(content, list):
                content = " ".join(
                    p.get("text", "")
                    for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                )
            text = str(content or "").strip()
            if not text:
                continue
            if self._is_underspecified_followup(text):
                # Remember the first non-empty one as a weak fallback but
                # keep looking for a concrete subject behind it.
                if not fallback:
                    fallback = text
                continue
            return text[:200]

        if self.memory and hasattr(self.memory, "working_get"):
            try:
                for entry in reversed(self.memory.working_get(session_id, 10)):
                    if entry.get("role") == "user":
                        t = str(entry.get("text") or entry.get("summary") or "").strip()
                        if t and not self._is_underspecified_followup(t):
                            return t[:200]
            except Exception:
                pass
        return fallback[:200] if fallback else ""

    def _augment_routing_text_with_context(self, session_id: str, text: str) -> str:
        """Resolve coreference for routing AND track the active subject.

        - A *concrete* turn (not an underspecified follow-up) becomes the
          session's active subject and is returned unchanged — so it never
          degrades normal routing and so it overrides a stale subject when
          the user genuinely switches topics.
        - An *underspecified* follow-up ("check now", "do it", "the same
          one") is rewritten as ``"<text> (re: <subject>)"`` so routing
          can see the implied device/skill. The resolved text is cached
          for this turn so the system-prompt builder can reuse it.
        """
        stripped = (text or "").strip()
        if not stripped:
            return text

        if not self._is_underspecified_followup(stripped):
            # Concrete turn — define/refresh the active subject. When the
            # user names a lights/color action without repeating "robot"
            # but the session's prior subject was robot-scoped ("check the
            # cutebot" → "make the lights red"), augment routing text the
            # same way underspecified follow-ups do so Hue never wins.
            prior = self._coref_state().get(session_id, "") if session_id else ""
            if (
                prior
                and prior.strip().lower() != stripped.lower()
                and self._R_ROBOT_SUBJECT.search(prior)
                and self._R_LIGHTS_ACTION.search(stripped)
                and not self._R_ROBOT_SUBJECT.search(stripped)
            ):
                augmented = f"{stripped} (re: {prior})"
                if session_id:
                    self._coref_query_state()[session_id] = augmented
                self._set_active_subject(session_id, stripped)
                logger.debug(
                    "coref routing: robot lights '%s' resolved against prior '%s'",
                    stripped, prior[:60],
                )
                return augmented
            self._set_active_subject(session_id, stripped)
            self._coref_query_state().pop(session_id, None)
            return text

        prior = self._last_referenced_subject(session_id)
        if not prior or prior.strip().lower() == stripped.lower():
            self._coref_query_state().pop(session_id, None)
            return text
        augmented = f"{stripped} (re: {prior})"
        if session_id:
            self._coref_query_state()[session_id] = augmented
        logger.debug(
            "coref routing: '%s' resolved against prior subject '%s'",
            stripped, prior[:60],
        )
        return augmented

    def _coref_query_for_prompt(self, session_id: str, fallback: str) -> str:
        """Return the coref-resolved query for the current turn if routing
        produced one, else ``fallback``. Lets the system prompt / tool
        selection see the same resolved subject routing used."""
        if not session_id:
            return fallback
        return self._coref_query_state().get(session_id) or fallback

    # ─────────────────────────────────────────────
    # Live-voice transcript hook
    # ─────────────────────────────────────────────
    #
    # The ``openai_realtime`` / ``gemini_live`` modes relay raw audio
    # straight to the upstream provider and BYPASS ``handle_command_stream``
    # — so coreference tracking, per-turn memory recall, and episode
    # logging that exist for text chat never run for voice. This entry
    # point hooks the realtime *transcript* path (not the audio path) so
    # voice turns at least keep the orchestrator's session state honest:
    #
    #   1. The final USER transcript is appended to
    #      ``conversation_history[session_id]`` so subsequent text/voice
    #      turns can see the voice utterance in the same history that
    #      text chat sees.
    #   2. Concrete turns refresh the active-subject tracker so the NEXT
    #      voice/text follow-up ("how about now") coref-resolves
    #      against the right subject.
    #   3. Underspecified follow-ups are coref-resolved synchronously so
    #      the caller can inject the resolved context into the realtime
    #      session before the LLM generates a reply.
    #
    # The voice provider has already started generating a response by
    # the time we see the transcript (server VAD is faster than the
    # transcription event), so steering the *current* turn is best-
    # effort. The tracker IS authoritative for the NEXT turn either way.
    async def note_voice_user_turn(
        self,
        session_id: str,
        text: str,
        *,
        emit_temporal_timeline: bool = False,
        tools: Optional[list[dict]] = None,
    ) -> dict[str, str]:
        """Hook the live-voice transcript path into orchestrator state.

        Called by ``voice/realtime_proxy.py`` (OpenAI Realtime) and
        ``voice/gemini_realtime.py`` (Gemini Live) on every *final* user
        transcript so the orchestrator's coref tracker, conversation
        history, and (optionally) the temporal-timeline side-channel
        all see the voice turn even though the audio path bypasses
        ``handle_command_stream``.

        Returns::

            {
                "resolved_text": <text>,         # coref-augmented when
                                                 # the turn was an
                                                 # underspecified
                                                 # follow-up; the raw
                                                 # text otherwise
                "active_subject": <subject>,    # last concrete subject
                                                 # for this session
                "context_hint": <str>,           # ready-to-inject system
                                                 # message; empty for
                                                 # concrete turns
                "forced_tool": <str>,            # when schedule/temporal
                                                 # intent matches and the
                                                 # tool is in ``tools``,
                                                 # the name to force on
                                                 # the voice realtime path
            }

        ``emit_temporal_timeline`` is opt-in: when true and the
        utterance matches the strict temporal-recall regex the orchestrator
        dispatches the timeline-fusion side-channel as it would for the
        text path. The dispatch is fire-and-forget so the voice latency
        budget is untouched.
        """
        out = {
            "resolved_text": text or "",
            "active_subject": "",
            "context_hint": "",
            "forced_tool": "",
        }
        clean = (text or "").strip()
        if not session_id or not clean:
            return out

        # 1. Conversation-history append (so follow-up routing / recall
        # scans the voice utterance too). Cap matches the text path
        # so a long-running voice session can't unbounded-grow memory.
        # Under the session lock: this used to mutate the list while a
        # concurrent text turn held a snapshot of it, and the turn's
        # write-back then destroyed the voice row.
        await self._append_voice_row(session_id, "user", clean)

        # 2 + 3. Coref-resolution + active-subject tracking. Reuses the
        # exact same helper the text path uses so behaviour stays in
        # lock-step. ``_augment_routing_text_with_context`` updates
        # ``_session_active_subject`` for concrete turns and caches a
        # resolved query for follow-ups.
        try:
            resolved = self._augment_routing_text_with_context(session_id, clean)
        except Exception:
            logger.debug("note_voice_user_turn: coref resolve failed", exc_info=True)
            resolved = clean
        out["resolved_text"] = resolved or clean
        try:
            out["active_subject"] = self._last_referenced_subject(session_id) or ""
        except Exception:
            out["active_subject"] = ""

        # Build a small system-style context hint the voice proxy can
        # inject into the live realtime session. Only emitted for
        # underspecified follow-ups (resolved_text differs from clean)
        # so concrete turns don't pollute the session with redundant
        # "active subject is …" frames.
        if resolved and resolved != clean and out["active_subject"]:
            out["context_hint"] = (
                "[Active subject hint]\n"
                f"The user's current follow-up '{clean}' refers to: "
                f"{out['active_subject'][:160]}. Interpret short references "
                f"(it/that/now/again) against this subject."
            )

        # Fire-and-forget temporal-timeline side-channel for voice recall
        # parity. Same fan-out the text path uses; the side-channel
        # itself is fully defensive, so a failure can't break voice.
        if emit_temporal_timeline:
            try:
                if self._R_TEMPORAL.search(clean):
                    self._track_background_task(asyncio.create_task(
                        self._maybe_emit_temporal_timeline(session_id, clean)
                    ))
            except Exception:
                logger.debug(
                    "note_voice_user_turn: temporal timeline schedule failed",
                    exc_info=True,
                )

        # Voice parity with the text orchestrator path: when the utterance
        # is a scheduled device/action request, force feral_routines__create
        # (or fused_timeline on temporal recall) so the realtime model
        # cannot fall through to reminders/workflows/notes.
        try:
            query_for_force = out.get("resolved_text") or clean
            if isinstance(tools, list) and tools:
                forced = self._force_tool_for_query(
                    query_for_force, tools, session_id,
                )
                if forced:
                    out["forced_tool"] = forced
        except Exception:
            logger.debug(
                "note_voice_user_turn: forced_tool resolution failed",
                exc_info=True,
            )

        return out

    async def _append_voice_row(self, session_id: str, role: str, text: str) -> None:
        """Append one live-voice row to the full transcript.

        Serialised on the per-session lock, the same lock
        ``handle_command`` / ``handle_command_stream`` hold. Without it a
        transcript landing mid-turn was written into a list the text
        turn had already snapshotted, and the turn's write-back then
        overwrote it.
        """
        async with self._get_session_lock(session_id):
            history = self.conversation_history.setdefault(session_id, [])
            # B7: a live-voice session never runs ``_finalize_turn``, so
            # without a stamp here it looks permanently idle to the
            # eviction cap while the operator is speaking to it.
            self._touch_session(session_id)
            if (
                history
                and history[-1].get("role") == role
                and history[-1].get("content") == text
            ):
                # Providers re-emit the same final transcript on
                # reconnect; one utterance is one row.
                return
            history.append({"role": role, "content": text, "source": "voice_realtime"})
            if len(history) > self._conversation_max_per_session:
                self.conversation_history[session_id] = history[
                    -self._conversation_max_per_session:
                ]

        # Durable copy. Everything above is in-memory: `compact_session`
        # replaces `conversation_history` wholesale, and the cap just
        # above discards the head with nothing behind it. The text paths
        # persist a `user_command` / `assistant_reply` episode per turn,
        # but realtime audio never enters `handle_command_stream`, so it
        # reached neither that prelude nor `_finalize_turn` and a purely
        # conversational voice session produced no episode at all. It
        # was invisible to episode_search, episode_recent and the
        # timeline.
        #
        # Written outside the lock: `_save_episode_async` is
        # fire-and-forget and holding a per-session lock across a
        # SQLite write is the pattern that made compaction stall the
        # next turn. Placed after the dedup return above, so a provider
        # re-emitting the same final transcript on reconnect yields one
        # episode, not two.
        event_type = "assistant_reply" if role == "assistant" else "user_command"
        self._save_episode_async(
            session_id=session_id,
            event_type=event_type,
            summary=text[:200],
            detail=text,
        )

    async def note_voice_assistant_turn(self, session_id: str, text: str) -> None:
        """Record a live-voice ASSISTANT turn in ``conversation_history``.

        Counterpart to :meth:`note_voice_user_turn`, and the fix for the
        headline amnesia bug. ``openai_realtime`` / ``gemini_live``
        stream the model's audio straight to the client, so the
        assistant's final transcript only ever reached working memory
        and the durable ``voice:<sid>`` thread — never the array the
        next text turn is built from. The model was then handed two
        consecutive user messages and correctly answered that it had
        never spoken.

        Safe to call on every final assistant transcript: duplicate
        consecutive rows are collapsed.
        """
        clean = (text or "").strip()
        if not session_id or not clean:
            return
        await self._append_voice_row(session_id, "assistant", clean)

    # Max user turns scanned for a pending routine intent. Two is enough
    # to cover "request → clarification → answer" (the typical
    # demo-blocker flow) without sticky-routing feral_routines forever
    # after a long-completed setup.
    _PENDING_ROUTINE_USER_TURN_WINDOW = 2

    def _has_pending_routine_intent(self, session_id: str) -> bool:
        """True when the user expressed a routine intent on a recent prior
        turn but no ``feral_routines__create`` tool call has succeeded
        since. This is what lets the routing heuristic still hoist
        ``feral_routines`` on a clarification follow-up whose own text
        carries no recurring marker (e.g. "I just want you to make it
        spin" after "every night at 9").

        Scans the last :data:`_PENDING_ROUTINE_USER_TURN_WINDOW` user
        turns and bails early on:
          * an assistant ``feral_routines__create`` tool call (the
            routine was already created — intent is satisfied),
          * an explicit ``tool``-role result for that call.
        Conservative by design — the caller (``_heuristic_route``) also
        gates this on ``_query_implies_action`` so a chit-chat turn
        after a routine ask doesn't sticky-route here.
        """
        if not session_id:
            return False
        history = self.conversation_history.get(session_id) or []
        if not history:
            return False
        user_turns_scanned = 0
        for msg in reversed(history):
            role = msg.get("role")
            if role == "assistant":
                tool_calls = msg.get("tool_calls") or []
                if isinstance(tool_calls, list):
                    for tc in tool_calls:
                        if not isinstance(tc, dict):
                            continue
                        fn = (tc.get("function") or {}) if isinstance(tc.get("function"), dict) else {}
                        name = fn.get("name") or tc.get("name") or ""
                        if name == "feral_routines__create":
                            # The brain already asked the scheduler to
                            # create the routine — intent is consumed.
                            return False
                continue
            if role == "tool":
                if (msg.get("name") or "") == "feral_routines__create":
                    content = msg.get("content")
                    if isinstance(content, str) and '"success": true' in content.lower():
                        return False
                continue
            if role != "user":
                continue
            user_turns_scanned += 1
            if user_turns_scanned > self._PENDING_ROUTINE_USER_TURN_WINDOW:
                break
            content = msg.get("content")
            if isinstance(content, list):
                text_str = " ".join(
                    part.get("text", "")
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                )
            else:
                text_str = str(content or "")
            text_str = text_str.strip()
            if not text_str:
                continue
            if self._R_ROUTINE_RECURRING.search(text_str):
                return True
            if self._R_ROUTINE_ONESHOT.search(text_str) and self._query_implies_action(text_str):
                return True
        return False

    async def _route_prompt(self, text: str, session_id: str = "") -> list["SkillManifest"]:
        """Pick which skills the LLM sees this turn.

        WS2 — heuristic-first; LLM disambiguation through
        ``LLMProvider.route_call(call_site="routing", tier="cheap")``
        only when the heuristic exit is ``"ambiguous"`` (see
        ``_heuristic_route`` table above).

        Conversational coreference: an ambiguous short follow-up
        ("check now", "do it", "the same one") is resolved against the
        last-referenced subject from this session's recent turns BEFORE
        routing, so "check now" right after "check the cutebot" still
        routes to the cutebot device skill. This only augments the
        routing input, never the actual LLM turn (which already sees the
        full conversation history).
        """
        if not self.skills.skills:
            return []

        if session_id:
            text = self._augment_routing_text_with_context(session_id, text)

        # Pre-WS2 behaviour preserved when the LLM is unavailable:
        # heuristic is the only available choice.
        if not getattr(self.llm, "available", False):
            results, _reason = self._heuristic_route(text, session_id=session_id)
            if not results:
                results = self._fallback_skills_for_query(text, top_k=5)
            return await self._apply_routing_penalties(results)

        skills, reason = self._heuristic_route(text, session_id=session_id)
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
        smart_loops_on = _smart_loops_enabled()
        for core_id in self.ALWAYS_INCLUDE_SKILLS:
            if core_id in self._SMART_LOOPS_SKILLS and not smart_loops_on:
                continue
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
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending_frame_futures[msg_id] = future

        request_msg = FeralMessage(
            msg_id=msg_id, hop="brain", type="vision_request",
            payload=VisionRequestPayload(resolution=resolution, quality=quality, reason=reason).model_dump(),
        )
        # ``ws`` here is a daemon socket, so the frame needs the
        # HUP_SPEC.md section 5 envelope ``FeralMessage`` does not carry.
        await ws.send_json(stamp_hup_envelope(request_msg.model_dump()))

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

    async def _send_text(
        self,
        session_id: str,
        text: str,
        *,
        model: str = "",
        usage: dict | None = None,
    ):
        # Record first: every path that sends text must record what it
        # sent, including the ones that return before the tool loop.
        self._note_outbound_text(session_id, text)
        # ``model``/``usage`` are supplied only by the main tool loop,
        # which is the only caller that knows what the turn actually cost.
        # The many status/error/ack sends keep the empty default, so the
        # UI attributes an answer and stays quiet about everything else.
        await helper_send_text(
            self, session_id=session_id, text=text, model=model, usage=usage,
        )

    async def _send_error(
        self,
        session_id: str,
        message: str,
        *,
        code: str = "llm_provider_error",
        recoverable: bool = True,
    ):
        """Deliver a provider / pipeline failure as an ``error`` frame.

        Not ``_send_text``: this must not call ``_note_outbound_text``,
        or ``_finalize_turn`` commits the failure to the transcript as
        an assistant row and the next turn feeds it back to the model.
        """
        await helper_send_error(
            self, session_id, message, code=code, recoverable=recoverable,
        )

    def _pop_multi_agent_attribution(self, session_id: str) -> tuple[str, dict]:
        """Attribution for the multi-agent turn that just finished.

        Every failure mode returns "unknown" rather than raising. A token
        label is cosmetic; a turn whose answer never reaches the user
        because the label could not be computed is a real outage. The
        multi-agent orchestrator is also swappable (``_init_multi_agent``
        falls back, and tests inject doubles), so neither the method nor
        the shape of what it returns can be assumed.
        """
        fn = getattr(self._multi_agent, "pop_turn_attribution", None)
        if not callable(fn):
            return "", {}
        try:
            attr = fn(session_id)
        except Exception:
            logger.debug("multi-agent attribution unavailable", exc_info=True)
            return "", {}
        if not isinstance(attr, dict):
            return "", {}
        model = attr.get("model")
        usage = attr.get("usage")
        return (
            model if isinstance(model, str) else "",
            usage if isinstance(usage, dict) else {},
        )

    async def _try_send_sdui(
        self,
        session_id: str,
        text: str,
        *,
        model: str = "",
        usage: dict | None = None,
    ):
        await helper_try_send_sdui(
            self, session_id=session_id, text=text, model=model, usage=usage,
        )

    async def _try_genui_for_result(self, session_id: str, tool_call: dict, result_data: dict):
        await helper_try_genui_for_result(self, session_id=session_id, tool_call=tool_call, result_data=result_data)
