"""
Shared brain state singleton.

Every route module and the main server import ``state`` from here.
"""

import logging
import os
import time
from collections import deque
from pathlib import Path
from typing import Optional

from fastapi import WebSocket

from version import VERSION as __version__
from models.protocol import FeralMessage
from agents.orchestrator import Orchestrator
from agents.learner import Learner
from agents.skill_generator import SkillGenerator
from agents.taskflow import TaskFlowRuntime
from skills.registry import SkillRegistry
from memory.store import MemoryStore
from memory.ingest import MemoryIngestor
from memory.node_subdevices import NodeSubdeviceStore
from memory.session_snapshot import SessionSnapshotStore
from memory.capability_registry import CapabilityRegistry
from perception.fusion import PerceptionEngine
from perception.audio_pipeline import AudioPipeline
from perception.scene import SceneAnalyzer
from perception.change_detector import ChangeDetector
from config.loader import ConfigLoader, feral_home, feral_data_home
from config.runtime import brain_bind_host, brain_port, brain_public_base_url, ollama_base_url
from security.vault import BlindVault, PermissionTier, ExecutionSandbox
from security.sandbox_policy import SandboxPolicy
from hardware.protocol import DeviceRegistry, DeviceManifest, HUPAction, HUPActionType, FERAL_GLASSES_MANIFEST
from mcp.server import FeralMCPServer
from mcp.client import MCPClientManager, MCPServerConnection
from channels.base import ChannelManager, ChannelMessage, ChannelResponse
from voice.realtime_proxy import RealtimeProxy
from voice.gemini_realtime import GeminiRealtimeProxy
from voice.router import VoiceRouter
from memory.sync import SyncEngine
from security.wasm_sandbox import WASMSandbox
from perception.wake_word import WakeWordDetector, WakeWordConfig
from integrations.oauth_manager import OAuthManager
from integrations.spotify import SpotifyIntegration
from integrations.home_assistant import HomeAssistantIntegration
from integrations.mqtt_bridge import MQTTBridge
from integrations.notion import NotionIntegration
from integrations.webhook_receiver import WebhookReceiver, EventBus
from integrations.email_watcher import EmailWatcher
from integrations.calendar import CalendarIntegration
from integrations.email import EmailIntegration
from integrations.messaging import MessagingHub
from integrations.health_platforms import HealthAggregator, WhoopClient, OuraClient
from integrations.google_drive import GoogleDriveIntegration
from integrations.google_contacts import GoogleContactsIntegration
from integrations.microsoft365 import Microsoft365Integration
from skills.marketplace import MarketplaceClient
from gateway.protocol import MethodRegistry, GatewaySession, register_core_methods
from hardware.mesh import HardwareMesh
from identity.workspace import IdentityWorkspace
from genui.generator import GenUIEngine, ServiceProviderRegistry
from providers.catalog import ProviderCatalog, default_cache_path as _default_catalog_cache
from skills.impl.browser_use import BrowserController
from agents.session_handoff import SessionHandoffManager
from agents.identity_loader import IdentityLoader
from agents.baseline_engine import BaselineEngine
from agents.about_me import AboutMeStore
from agents.ideas_engine import IdeasEngine, IdeasStore
from agents.app_registry import (
    AppRegistry,
    HybridGenerator,
    default_apps_db_path,
    default_apps_dir,
    default_hybrid_cache_dir,
)
from security.device_pairing import DevicePairingStore
from api.boot_report import BootReport, boot_subsystem

logger = logging.getLogger("feral.brain")

VISION_MAX_FRAME_KB = int(os.environ.get("FERAL_VISION_MAX_FRAME_KB", "512"))

# W3-A13: avoid exporting channel/webhook secrets into process-global env
# when applying ConfigLoader/export_as_env at boot. These credentials are
# passed explicitly via config objects / channel configs instead.
_SENSITIVE_ENV_EXPORT_KEYS = frozenset({
    "FERAL_TELEGRAM_BOT_TOKEN",
    "FERAL_SLACK_BOT_TOKEN",
    "FERAL_SLACK_APP_TOKEN",
    "FERAL_SLACK_SIGNING_SECRET",
    "FERAL_DISCORD_BOT_TOKEN",
    "FERAL_WHATSAPP_PHONE_NUMBER_ID",
    "FERAL_WHATSAPP_ACCESS_TOKEN",
    "FERAL_WHATSAPP_VERIFY_TOKEN",
    "FERAL_WHATSAPP_APP_SECRET",
})


def _should_export_runtime_env_key(key: str) -> bool:
    if not isinstance(key, str):
        return False
    if key.startswith("FERAL_KEY_"):
        return False
    return key not in _SENSITIVE_ENV_EXPORT_KEYS


def _feature_flag_enabled(env_key: str) -> bool:
    """Return True when ``os.environ[env_key]`` holds a truthy flag value.

    Shared by the A6 boot gates for ScreenLoop + ProactiveEngine so the
    "is the operator opted in" check is consistent across call sites.
    Accepts the conventional ``"true"/"1"/"yes"/"on"`` values; anything
    else — including an unset env var — is treated as disabled, which
    is the safe default for ambient API-quota burners.
    """
    val = os.environ.get(env_key, "")
    return isinstance(val, str) and val.strip().lower() in ("true", "1", "yes", "on")


def _sanitize_orphan_tool_rows(rows: list[dict]) -> list[dict]:
    """Drop ``role:"tool"`` rows whose announcing assistant turn is
    missing from this list.

    Used by primary-thread rehydrate to scrub stale snapshots that
    were written by older brain builds with naive tail-truncation.
    A single orphan ``function_call_output`` in the input causes the
    Responses API to 400 on the very next request, so we strip them
    once at boot and leave clean history in RAM.
    """
    if not rows:
        return rows
    announced: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("role") == "assistant" and row.get("tool_calls"):
            for tc in row["tool_calls"]:
                if isinstance(tc, dict):
                    cid = tc.get("id")
                    if isinstance(cid, str) and cid:
                        announced.add(cid)
    cleaned: list[dict] = []
    dropped = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("role") == "tool":
            cid = row.get("tool_call_id") or row.get("call_id") or ""
            if not cid or cid not in announced:
                dropped += 1
                continue
        cleaned.append(row)
    if dropped:
        logger.warning(
            "primary-thread rehydrate: dropped %d orphan tool row(s) from snapshot",
            dropped,
        )
    return cleaned


def _load_configured_vec_index_or_default():
    """audit-r12 D4: read ``settings.memory.backend`` and either return
    a constructed :class:`VectorIndexBackend` for the operator's choice,
    or ``None`` so :class:`MemoryStore` falls back to its built-in
    sqlite-vec default.

    Why eagerly construct here (at module import) for non-default
    backends instead of deferring to ``BrainState.init()``? Because
    ``BrainState.__init__`` instantiates ``MemoryStore`` synchronously
    and other subsystems (``NodeSubdeviceStore``, ``SyncEngine``) read
    ``self.memory.db_path`` later in ``init()``. Deferring memory
    construction would cascade through six other constructors. The
    sync :class:`VectorIndexBackend` Protocol exists exactly so this
    construction stays sync.

    Fails LOUDLY on:

    * unknown backend id (``ValueError`` -> propagates, brain refuses
      to boot);
    * missing optional dependency (``ImportError`` -> propagates with
      an actionable ``feral-ai[memory-<id>]`` install hint);
    * constructor failure (e.g. corrupt Chroma persistence dir,
      unreachable Qdrant URL) -> propagates.

    Never silently falls back to sqlite-vec. Misconfiguring the
    selector is now a boot-time failure, not a "vectors silently
    aren't being stored" failure.
    """
    try:
        from config.loader import load_settings as _load_settings
        from memory.vector_index_backends import load_vector_index as _load_vec
        from memory.embeddings import EmbeddingProvider as _Embedder
    except Exception:
        # Import failed (e.g. during very early bootstrap before
        # memory/* is importable). Defer to MemoryStore default.
        return None

    settings = _load_settings() or {}
    memory_cfg = settings.get("memory") or {}
    backend_id = memory_cfg.get("backend") or "sqlite_vec"
    if backend_id == "sqlite_vec":
        # MemoryStore defaults to sqlite-vec internally; nothing for us
        # to construct.
        return None

    backend_config = memory_cfg.get("backend_config") or {}
    # The vector dimensionality must match whatever the embedder will
    # later produce; constructing one here is cheap (just provider
    # selection, no API calls) and gives us the same dim MemoryStore
    # will use.
    embedder = _Embedder()
    logger.info(
        "memory.backend=%s — constructing pluggable vector index (dim=%d)",
        backend_id, embedder.dimension,
    )
    return _load_vec(backend_id, dim=embedder.dimension, **backend_config)


class VisionBuffer:
    """Stores the latest N frames per hardware node in a memory-bounded ring buffer."""

    def __init__(self, max_frames_per_node: int = 3):
        self._max = max_frames_per_node
        self.frames: dict[str, deque] = {}

    def push(self, node_id: str, frame: dict):
        if node_id not in self.frames:
            self.frames[node_id] = deque(maxlen=self._max)
        self.frames[node_id].append(frame)

    def latest(self, node_id: str) -> Optional[dict]:
        buf = self.frames.get(node_id)
        return buf[-1] if buf else None

    def latest_data_url(self, node_id: str) -> Optional[str]:
        frame = self.latest(node_id)
        if not frame or not frame.get("data_b64"):
            return None
        encoding = frame.get("encoding", "jpeg")
        mime = f"image/{encoding}"
        return f"data:{mime};base64,{frame['data_b64']}"

    def node_ids_with_frames(self) -> list[str]:
        return [nid for nid, buf in self.frames.items() if buf]


class BrainState:
    def __init__(self):
        self._load_stored_credentials()
        self.config = ConfigLoader()
        self.config.discover()
        for env_key, env_value in self.config.export_as_env().items():
            if _should_export_runtime_env_key(env_key):
                os.environ[env_key] = env_value
        self.sessions: dict[str, WebSocket] = {}
        self.daemons: dict[str, WebSocket] = {}
        # Audit-r9 fix — operator report 2026-05-10:
        # > "the chat and memory should be the same for my phone chat
        # >  and the webui for feral brain on the local brain right?"
        #
        # Yes: one brain = one memory across surfaces. Until now the
        # web socket minted `session_id = str(uuid4())` per WebSocket
        # connection (`api/server.py:835`), and phone `chat_request`
        # used `phone-{node_id}` (`api/server.py:1486`). So
        # `Orchestrator.conversation_history[session_id]` and the
        # working-memory deque keyed by `session_id` were partitioned
        # — phone NEVER saw web's chat turns and vice versa. Even
        # multiple browser tabs got separate threads.
        #
        # `primary_session_id` is a stable per-install id minted on
        # first boot and persisted under `<feral_data_home>/primary_session_id`.
        # `/v1/session` and `chat_request` both default to it when the
        # client doesn't pass an explicit `session_id`, so by default
        # all surfaces share one conversation thread + working memory.
        # An explicit `session_id` from the client (e.g. a "new chat"
        # button, a multi-thread feature later) still wins.
        self.primary_session_id: str = self._load_or_mint_primary_session_id()
        # Phase 3 (audit-r10 overhaul) — refcount of live surfaces
        # attached to each `session_id`. `/v1/session` increments on
        # WebSocket accept, decrements on disconnect; cleanup only
        # fires when the count reaches zero. Without this, closing
        # one web tab (or refreshing) wiped the shared `primary_session_id`
        # thread in RAM even though the iOS surface was still
        # connected — that's the residual bug behind operator
        # complaint #15 ("app can't fetch stuff I did on the local
        # brain chat"). The primary session also persists across
        # zero-count detaches (see `SessionSnapshotStore` below).
        self.session_attach_count: dict[str, int] = {}
        # Phase 3 — primary thread snapshot store. Persists the last
        # ~50 turns to disk so a brain restart rehydrates them
        # automatically. Loaded later in `init()` after `orchestrator`
        # + `memory` exist.
        self.session_snapshot: Optional[SessionSnapshotStore] = None
        self.devices: dict[str, dict] = {}
        self.skill_registry = SkillRegistry()
        # Phase 5 (audit-r10 overhaul) — runtime catalog of which
        # `phone.*` / `glasses.*` action names connected nodes can
        # handle. Populated from `node_register.skills` in
        # `api/server.py` and consumed by `GET /api/capabilities` +
        # the orchestrator's capability-aware routing.
        self.capability_registry = CapabilityRegistry()
        # audit-r12 D4: the vector-index backend for MemoryStore is
        # selected from ``settings.memory.backend``. Default
        # (``sqlite_vec``) constructs synchronously inside MemoryStore
        # so the brain still has a working ``state.memory`` before
        # ``init()`` runs (tests + bootstrap paths rely on this).
        # Non-default backends (``chroma``, ``qdrant``) are built in
        # ``init()`` where their constructors can fail loudly on
        # missing deps without crashing module import.
        self.memory = MemoryStore(vec_index=_load_configured_vec_index_or_default())
        self.vision_buffer = VisionBuffer()
        self.perception = PerceptionEngine()
        self.audio = AudioPipeline()
        self.scene: Optional[SceneAnalyzer] = None
        self.change_detector = ChangeDetector()
        self.learner: Optional[Learner] = None
        # v2026.5.34 (PR 2 D11/D12): the two new memory-v2 services are
        # constructed in ``_boot_subsystems`` once ``self.memory`` +
        # ``self.sync_engine`` are wired; declared here so the
        # attributes always exist for HTTP-route guard checks
        # (``getattr(state, "memory_decay", None)`` patterns).
        self.memory_decay = None  # type: ignore[assignment]
        self.sync_scheduler = None  # type: ignore[assignment]
        self.skill_gen: Optional[SkillGenerator] = None
        self.vault: Optional[BlindVault] = None
        self.sandbox: Optional[ExecutionSandbox] = None
        self.policy: Optional[SandboxPolicy] = None
        self.device_registry: Optional[DeviceRegistry] = None
        self.mcp_server: Optional[FeralMCPServer] = None
        self.mcp_client: Optional[MCPClientManager] = None
        self.channel_manager: Optional[ChannelManager] = None
        self.voice_router: Optional[VoiceRouter] = None
        self.realtime_proxy: Optional[RealtimeProxy] = None
        self.oauth: Optional[OAuthManager] = None
        self.spotify: Optional[SpotifyIntegration] = None
        self.home_assistant: Optional[HomeAssistantIntegration] = None
        self.notion: Optional[NotionIntegration] = None
        self.calendar: Optional[CalendarIntegration] = None
        self.email: Optional[EmailIntegration] = None
        self.messaging: Optional[MessagingHub] = None
        self.health_aggregator: Optional[HealthAggregator] = None
        self.google_drive: Optional[GoogleDriveIntegration] = None
        self.google_contacts: Optional[GoogleContactsIntegration] = None
        self.microsoft365: Optional[Microsoft365Integration] = None
        self.digital_twin = None
        self.location_engine = None
        self.push_channel = None
        self.event_bus: Optional[EventBus] = None
        self.webhook_receiver: Optional[WebhookReceiver] = None
        self.marketplace: Optional[MarketplaceClient] = None
        self.sync_engine: Optional[SyncEngine] = None
        self.wasm_sandbox: Optional[WASMSandbox] = None
        self.wake_word: Optional[WakeWordDetector] = None
        self.orchestrator: Optional[Orchestrator] = None
        # ProviderCatalog — single source of truth for the LLM provider
        # inventory + model lists (see feral-core/providers/catalog.py).
        # Shared by the runtime LLMProvider, the REST /api/llm routes,
        # the CLI setup wizard, and v2 /setup page.
        self.provider_catalog: Optional[ProviderCatalog] = None
        self.activity_log: deque = deque(maxlen=100)
        self.gemini_proxy: Optional[GeminiRealtimeProxy] = None
        self.gateway_registry: Optional[MethodRegistry] = None
        self.hardware_mesh: Optional[HardwareMesh] = None
        self.identity_workspace: Optional[IdentityWorkspace] = None
        self.genui_engine: Optional[GenUIEngine] = None
        self.service_providers: Optional[ServiceProviderRegistry] = None
        self.browser: Optional[BrowserController] = None
        self.approval_manager = None
        self.cron_service = None
        self.scheduler = None
        self.docker_sandbox = None
        self.taskflows: Optional[TaskFlowRuntime] = None
        # PR 10: canonical upload store. Initialised lazily on first
        # use so unit-test fixtures don't have to spin up the brain.
        self.uploads = None
        self.session_handoff: Optional[SessionHandoffManager] = None
        self.baseline_engine: Optional[BaselineEngine] = None
        # AboutMe store — structured self-model of the user (see agents/about_me.py).
        # Initialised alongside BaselineEngine during state.init().
        self.about_me: Optional[AboutMeStore] = None
        # Ideas engine — "For you today" pane (see agents/ideas_engine.py).
        # Built on top of AboutMe + BaselineEngine + ConsciousnessStore.
        self.ideas_engine: Optional[IdeasEngine] = None
        # GenUI third-party app platform (see agents/app_registry.py).
        # AppRegistry indexes installed apps; HybridGenerator renders
        # authored + cached + LLM-generated surfaces.
        self.app_registry: Optional[AppRegistry] = None
        self.hybrid_genui: Optional[HybridGenerator] = None
        self.somatic_engine = None
        self.tool_genesis = None
        self.supervisor = None
        self.twin_policy = None
        self.agent_mitosis = None
        self.intent_compiler = None
        self.mqtt_bridge = None
        self.email_watcher: Optional[EmailWatcher] = None
        self.device_pairing_store: DevicePairingStore = DevicePairingStore()
        self._boot_report: BootReport = BootReport()
        # First-party agent personas and workflow packs loaded from
        # feral-core/agents/personas/ and feral-core/workflows/ at boot.
        # See feral-core/agents/persona_loader.py.
        self.personas: dict = {}
        self.workflow_packs: dict = {}
        # ConsciousnessStore (5th memory tier — in-flight operational state).
        # Initialised in state.init() after the FERAL_HOME is known.
        # See feral-core/memory/consciousness.py.
        self.consciousness = None

        # Node sub-device truth store. Tracks per-(node_id, capability)
        # status for everything an HUP node owns that is not the node
        # itself: BLE peripherals (Theora glasses), HealthKit pipelines,
        # cloud-synced wearables, etc. Persisted to memory.db so brain
        # restart preserves the prior view; liveness derate enforces
        # truth on the dashboard. See feral-core/memory/node_subdevices.py.
        self.node_subdevices: Optional[NodeSubdeviceStore] = None

        # Map daemon node_id → list of sessions interested in its data
        self._daemon_session_bindings: dict[str, set[str]] = {}

        # Collectors for channel-based sessions (no WebSocket)
        self._channel_collectors: dict[str, list[str]] = {}

        # A7 — Central registry of long-lived background tasks so
        # shutdown can cancel them BEFORE the LLM client / HTTP sessions
        # are closed. Producers (state.init, server startup, config
        # toggles) register their tasks via ``register_background_task``.
        # Tasks are auto-discarded on completion so the set doesn't grow
        # unboundedly during a long-running brain.
        import asyncio as _aio
        self._background_tasks: "set[_aio.Task]" = set()

    # ------------------------------------------------------------------
    # Back-compat attribute aliases (A2 fix)
    # ------------------------------------------------------------------
    # Several subsystems (self_introspection, tool_genesis routes, the
    # v2 Forge UI bridge) historically read ``state.skills`` /
    # ``state.mitosis_engine`` / ``state.node_registry``. The real field
    # names on ``BrainState`` are ``skill_registry`` / ``agent_mitosis``
    # / ``device_registry``. Without these aliases every legacy caller
    # silently hit ``AttributeError``, got swallowed by a broad ``except``
    # and returned empty lists with ``success: True``. Exposing them as
    # properties keeps the canonical attribute names while preserving
    # backward compatibility for any caller that still uses the old
    # names.
    @property
    def skills(self) -> SkillRegistry:
        return self.skill_registry

    @property
    def mitosis_engine(self):
        return self.agent_mitosis

    @property
    def node_registry(self):
        return self.device_registry

    def register_background_task(self, task):
        """Track a fire-and-forget task so it can be cancelled on shutdown.

        Accepts any ``asyncio.Task``; the task is auto-removed from the
        registry when it completes (success or exception) so short-lived
        tasks that happen to be registered don't leak.
        """
        if task is None:
            return task
        self._background_tasks.add(task)
        try:
            task.add_done_callback(self._background_tasks.discard)
        except Exception:
            pass
        return task

    async def shutdown_background_tasks(self, timeout: float = 5.0) -> int:
        """Cancel all registered background tasks and await their exit.

        Returns the number of tasks that were cancelled. Safe to call
        multiple times; already-done tasks are skipped. Tasks that refuse
        to finish within ``timeout`` seconds are logged and abandoned so
        shutdown can continue to the next phase (LLM close, etc.).
        """
        import asyncio as _aio
        tasks = [t for t in list(self._background_tasks) if not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            try:
                await _aio.wait_for(
                    _aio.gather(*tasks, return_exceptions=True),
                    timeout=timeout,
                )
            except _aio.TimeoutError:
                logger.warning(
                    "shutdown_background_tasks: %d tasks did not exit within %.1fs",
                    len(tasks), timeout,
                )
        self._background_tasks.clear()
        return len(tasks)

    @property
    def skill_executor(self):
        return self.orchestrator.executor if self.orchestrator else None

    async def init(self):
        _boot_start = time.time()
        with boot_subsystem(self._boot_report, "SkillRegistry", optional=False):
            self.skill_registry.load_builtin_skills()

        with boot_subsystem(self._boot_report, "Personas", optional=True):
            from agents.persona_loader import (
                load_personas,
                load_workflow_packs,
                default_personas_dir,
                default_workflow_packs_dir,
            )
            self.personas = load_personas(default_personas_dir())
            self.workflow_packs = load_workflow_packs(default_workflow_packs_dir())

        with boot_subsystem(self._boot_report, "Consciousness", optional=True):
            # 5th memory tier — in-flight operational state. On boot we
            # open the SQLite store and attempt to restore the last
            # snapshot file so "the agent knows where it left off"
            # survives restarts / upgrades / device handoffs.
            from memory.consciousness import (
                ConsciousnessStore,
                default_consciousness_db_path,
                default_snapshot_path,
            )
            self.consciousness = ConsciousnessStore(default_consciousness_db_path())

            # Broadcast any state mutation to every connected v2 client
            # via the existing /v1/session WebSocket. Fire-and-forget:
            # we schedule the coroutine onto the running loop so the
            # store's sync code path never blocks waiting for sends.
            def _on_consciousness_change(event_name: str, payload: dict) -> None:
                try:
                    import asyncio as _aio
                    loop = _aio.get_running_loop()
                except RuntimeError:
                    return  # no loop (e.g. during boot) — broadcast is optional
                loop.create_task(self.broadcast_event(event_name, payload))
                # Kick IdeasEngine whenever something enters waiting_user so
                # the "Right now" pane renders a fresh nudge within seconds.
                if (
                    event_name == "consciousness_status"
                    and isinstance(payload, dict)
                    and payload.get("status") == "waiting_user"
                    and self.ideas_engine is not None
                ):
                    try:
                        self.ideas_engine.refresh_waiting_user()
                    except Exception as exc:
                        logger.debug("IdeasEngine.refresh_waiting_user failed: %s", exc)

            self.consciousness.set_on_change(_on_consciousness_change)

            snap = default_snapshot_path()
            if snap.exists():
                try:
                    import json as _json
                    restored = self.consciousness.restore(_json.loads(snap.read_text()))
                    logger.info("Consciousness: restored %d entities from %s", restored, snap)
                except Exception as exc:
                    logger.warning("Consciousness snapshot restore skipped: %s", exc)

        with boot_subsystem(self._boot_report, "NodeSubdeviceStore", optional=False):
            # Open the sub-device store on the same SQLite file as the
            # rest of the memory stack. Wire its on_change callback so
            # every upsert / forget / live↔stale transition fans out as
            # a `subdevice_update` (or `subdevice_remove`) event over
            # every connected /v1/session WebSocket — matches the
            # ConsciousnessStore broadcast pattern above.
            def _on_subdevice_change(event_name: str, payload: dict) -> None:
                try:
                    import asyncio as _aio
                    loop = _aio.get_running_loop()
                except RuntimeError:
                    return  # no loop (e.g. boot phase) — broadcast is optional
                loop.create_task(self.broadcast_event(event_name, payload))

            self.node_subdevices = NodeSubdeviceStore(
                db_path=self.memory.db_path,
                on_change=_on_subdevice_change,
            )

            # Liveness sweep: scan every row, emit deltas only when a
            # row crosses the live↔stale threshold. 5 s tick is fast
            # enough that BLE rows (30 s window) derate within ~one
            # window past the last heartbeat. The sweep is cheap (one
            # SELECT, per-row dict comparison against the in-memory
            # tracker) so this is safe to run forever.
            async def _subdevice_liveness_loop():
                import asyncio as _aio
                while True:
                    await _aio.sleep(5.0)
                    try:
                        self.node_subdevices.sweep_stale()
                    except Exception as exc:
                        logger.debug("subdevice sweep failed: %s", exc)

            import asyncio as _aio_subdev
            self.register_background_task(
                _aio_subdev.create_task(
                    _subdevice_liveness_loop(),
                    name="feral-subdevice-liveness",
                )
            )

        with boot_subsystem(self._boot_report, "ProviderCatalog", optional=False):
            # Single registry of LLM providers + live model lists.
            # Built before LLMProvider so the runtime reads its config
            # through the catalog instead of a private tuple.
            self.provider_catalog = ProviderCatalog(cache_path=_default_catalog_cache())
            # Audit-r8 brief #07 root-cause fix: register the boot-time
            # catalog as the process-wide singleton BEFORE LLMProvider
            # init. Without this, `_default_model_for` lazily creates a
            # second empty `ProviderCatalog` (via `get_shared_catalog()`
            # in providers/catalog.py:947), which then returns "" for
            # `default_model_for("openai")` and the failover path
            # silently falls back to the stale `FERAL_LLM_MODEL` /
            # settings.json value — which is how the dated-transcribe
            # model id kept leaking into OpenAI calls despite my
            # boot self-heal + classifier fix. Single source of truth
            # = single catalog.
            from providers.catalog import set_shared_catalog
            set_shared_catalog(self.provider_catalog)

        from agents.llm_provider import LLMProvider
        with boot_subsystem(self._boot_report, "LLMProvider", optional=False):
            _shared_llm = LLMProvider()
            if self.provider_catalog is not None:
                _shared_llm.set_catalog(self.provider_catalog)
            try:
                _llm_cfg = dict(self.config._merged.get("llm", {})) if self.config else {}
            except Exception:
                _llm_cfg = {}
            if _llm_cfg:
                # Propagate fallback_providers + any other runtime-tunable
                # llm.* keys into the LLMProvider's internal config so
                # classify_error-triggered failover actually uses them.
                _shared_llm.set_config(_llm_cfg)

            # Self-heal contract (operator report 2026-05-09):
            # ``settings.json`` had ``llm.model`` pinned to
            # ``gpt-4o-mini-transcribe-2025-12-15`` (an audio-class
            # model written there by an earlier auto-pick before the
            # classifier knew about dated transcribe variants). Every
            # chat completion 404'd with `This is not a chat model`
            # and the operator had no obvious lever to fix it because
            # the CLI has no ``feral config set`` command. Self-heal:
            # if the resolved model classifies as audio / image /
            # embedding / realtime / completion-only, force-pick a
            # chat-class default from the catalog, persist the fix
            # back to settings.json, and continue. Pinned by
            # tests/test_llm_model_self_heal.py.
            try:
                from providers.model_classes import classify
                from providers.recommended import recommended_for
                _model = (_shared_llm.model or "").strip()
                _provider = _shared_llm.provider
                _cls = classify(_provider, _model) if _model else "unknown"
                _CHAT_OK = {"chat", "reasoning", "unknown"}
                if _model and _cls not in _CHAT_OK:
                    healed = ""
                    if self.provider_catalog is not None:
                        try:
                            healed = self.provider_catalog.default_model_for(_provider) or ""
                        except Exception as exc:
                            logger.warning(
                                "self_heal_llm_model: catalog lookup failed for %s: %s",
                                _provider, exc,
                            )
                    if healed and classify(_provider, healed) in {"chat", "reasoning"}:
                        logger.warning(
                            "self_heal_llm_model: %r is class=%r for provider=%r — "
                            "auto-picking %r (class=chat/reasoning) and persisting "
                            "to settings.json. The original value was likely "
                            "written by an older auto-picker that pre-dated the "
                            "dated-snapshot classifier fix.",
                            _model, _cls, _provider, healed,
                        )
                        _shared_llm.model = healed
                        os.environ["FERAL_LLM_MODEL"] = healed
                        try:
                            if self.config and hasattr(self.config, "update_settings"):
                                self.config.update_settings("llm", "model", healed)
                        except Exception as exc:
                            logger.warning(
                                "self_heal_llm_model: settings.json write failed: %s",
                                exc,
                            )
                    else:
                        logger.error(
                            "self_heal_llm_model: %r is class=%r for provider=%r and "
                            "no chat-class default was available from the catalog. "
                            "Chat completions will fail until the operator picks a "
                            "real chat/reasoning model in Settings → Providers.",
                            _model, _cls, _provider,
                        )
            except Exception as exc:
                # Self-heal must NEVER block boot; degrade gracefully.
                logger.warning("self_heal_llm_model: skipped (%s)", exc)
        self.learner = Learner(llm=_shared_llm, memory=self.memory)

        # v2026.5.34 (PR 2 D11): the MemoryDecayService runs an
        # Ebbinghaus + SM-2 sweep over ``episodes`` on a
        # ``settings.memory.decay.cadence_seconds`` cadence. The
        # service is constructed unconditionally so HTTP routes
        # (``/api/memory/{forget,recall,stats,decay/now}``) always
        # have a target; whether the background loop is *running*
        # depends on the ``enabled`` flag and is decided by
        # ``service.start()`` itself.
        from memory.decay import DecayConfig, MemoryDecayService
        from config.loader import load_settings as _load_settings
        self.memory_decay = MemoryDecayService(
            self.memory,
            DecayConfig.from_settings(_load_settings()),
        )
        self.scene = SceneAnalyzer(llm=_shared_llm)
        scene_cooldown = int(os.environ.get("FERAL_SCENE_COOLDOWN", "10"))
        self.scene.set_cooldown(scene_cooldown)

        self.skill_gen = SkillGenerator(
            llm=_shared_llm,
            skill_registry=self.skill_registry,
        )

        self.vault = BlindVault()
        self.sandbox = ExecutionSandbox(
            max_tier=os.environ.get("FERAL_MAX_TIER", "active")
        )

        self.policy = SandboxPolicy.load_default()
        self.device_registry = DeviceRegistry()
        self.mcp_server = FeralMCPServer(
            device_registry=self.device_registry,
            memory=self.memory,
            perception=self.perception,
        )
        self.mcp_client = MCPClientManager()
        with boot_subsystem(self._boot_report, "MCPClientManager"):
            await self.mcp_client.load_and_connect()
        self.channel_manager = ChannelManager()

        self.oauth = OAuthManager(vault=self.vault)
        self.spotify = SpotifyIntegration(oauth_manager=self.oauth)
        self.home_assistant = HomeAssistantIntegration(oauth_manager=self.oauth)
        self.notion = NotionIntegration(oauth_manager=self.oauth)
        self.calendar = CalendarIntegration(oauth_manager=self.oauth)
        self.email = EmailIntegration(oauth_manager=self.oauth)
        self.messaging = MessagingHub()
        with boot_subsystem(self._boot_report, "GoogleDriveIntegration"):
            self.google_drive = GoogleDriveIntegration(oauth_manager=self.oauth)
        with boot_subsystem(self._boot_report, "GoogleContactsIntegration"):
            self.google_contacts = GoogleContactsIntegration(oauth_manager=self.oauth)
        with boot_subsystem(self._boot_report, "Microsoft365Integration"):
            self.microsoft365 = Microsoft365Integration(oauth_manager=self.oauth)
        with boot_subsystem(self._boot_report, "HealthAggregator"):
            whoop = WhoopClient(oauth_manager=self.oauth)
            oura = OuraClient()
            self.health_aggregator = HealthAggregator(whoop=whoop, oura=oura)
        with boot_subsystem(self._boot_report, "LocationEngine"):
            from perception.location import LocationEngine
            self.location_engine = LocationEngine()
        with boot_subsystem(self._boot_report, "PushChannel"):
            from channels.push import PushChannel
            self.push_channel = PushChannel()
        with boot_subsystem(self._boot_report, "DigitalTwin"):
            from agents.digital_twin import DigitalTwin
            _identity_loader = IdentityLoader(memory=self.memory)
            self.digital_twin = DigitalTwin(memory=self.memory, identity_loader=_identity_loader, llm=_shared_llm)

            from skills.impl.digital_twin_skill import set_twin, DigitalTwinSkillBridge
            from skills.impl import register_instance as _register_twin
            set_twin(self.digital_twin)
            _register_twin("digital_twin", DigitalTwinSkillBridge())
        self.event_bus = EventBus()
        self.webhook_receiver = WebhookReceiver(event_bus=self.event_bus)
        self.marketplace = MarketplaceClient(skill_registry=self.skill_registry)

        # v2026.5.34 (PR 2 D12): the HLC node_id is now a persistent
        # per-brain identity. The previous ``hostname-pid`` value
        # changed on every restart, so the WAL could not de-duplicate
        # operations across reboots and a peer that came back from a
        # crash got every op replayed. ``stable_node_id`` writes a
        # one-shot UUID-v7-shaped value to
        # ``~/.feral/sync_node_id`` and re-reads it on every boot.
        from memory.sync import stable_node_id
        sync_node_id = stable_node_id()
        self.sync_engine = SyncEngine(node_id=sync_node_id, memory_store=self.memory)
        self.memory.set_sync_engine(self.sync_engine)
        await self.sync_engine.start_discovery()

        # v2026.5.34 (PR 2 D12): kick the SyncScheduler — it walks
        # every known peer on a cadence, applies exponential
        # backoff to flaky ones, and exposes per-peer health to
        # /api/sync/status. start() is a no-op when
        # settings.memory.sync.enabled is false.
        from memory.sync_scheduler import SchedulerConfig, SyncScheduler
        self.sync_scheduler = SyncScheduler(
            self.sync_engine,
            SchedulerConfig.from_settings(_load_settings()),
        )
        try:
            await self.sync_scheduler.start()
        except Exception as exc:
            logger.warning("SyncScheduler.start() failed: %s", exc)

        # v2026.5.34 (PR 2 D11): kick the decay sweeper. ``start()``
        # is a no-op when ``settings.memory.decay.enabled`` is false,
        # so the operator can disable it without changing this
        # call site. Failures are logged but do not block boot —
        # the brain runs fine with the sweeper off; the worst case
        # is unbounded episode growth, which a follow-up cron can
        # clean up.
        try:
            await self.memory_decay.start()
        except Exception as exc:
            logger.warning("MemoryDecayService.start() failed: %s", exc)

        # v2026.5.35 (PR 2.5 F1): one-shot migration of legacy flat-
        # knowledge rows into the unified KG. The function is
        # idempotent — once a row carries ``kg_migrated_at`` the
        # next boot skips it — and the rename of ``knowledge`` to
        # ``knowledge__deprecated`` only fires when every row has
        # been ported. When ``settings.memory.kg.unified`` is false
        # (chaos/rollback path) the migration is a no-op because
        # the read/write code paths stay on the flat table.
        try:
            from config.loader import load_settings as _load_settings
            f1_settings = _load_settings() or {}
        except Exception:
            f1_settings = {}
        unified = bool(((f1_settings.get("memory") or {}).get("kg") or {}).get("unified", True))
        if unified and self.memory:
            try:
                result = await self.memory.migrate_knowledge_to_kg()
                if result.get("ported") or result.get("deprecated"):
                    logger.info(
                        "F1 KG migration: ported=%d skipped=%d deprecated=%s",
                        result.get("ported", 0),
                        result.get("skipped", 0),
                        result.get("deprecated", False),
                    )
            except Exception as exc:
                logger.warning("F1 KG migration failed: %s", exc)

        self.wasm_sandbox = WASMSandbox()

        # Default OFF so a fresh install does not start listening to
        # the microphone without the user explicitly opting in. The
        # setup wizard and Settings expose a toggle that flips
        # FERAL_WAKE_WORD (and persists in config) when the user
        # consents. Pre-2026.5.13 builds defaulted to ON, which made
        # privacy-sensitive testers uncomfortable.
        self.wake_word = WakeWordDetector(WakeWordConfig(
            enabled=os.getenv("FERAL_WAKE_WORD", "false").lower() in ("true", "1", "yes"),
        ))

        from skills.impl import register_instance
        if self.spotify:
            register_instance("spotify_music", self.spotify)
        if self.home_assistant:
            register_instance("smart_home_hue", self.home_assistant)
        if self.notion:
            register_instance("notion", self.notion)
        if self.calendar:
            register_instance("calendar_google", self.calendar)
        if self.email:
            register_instance("email", self.email)
        if self.messaging and self.messaging.connected:
            register_instance("messaging_sms", self.messaging)
        if self.health_aggregator:
            register_instance("health_data", self.health_aggregator)
        if self.google_drive:
            register_instance("google_drive", self.google_drive)
        if self.google_contacts:
            register_instance("google_contacts", self.google_contacts)
        if self.microsoft365:
            register_instance("microsoft365", self.microsoft365)

        # PR 11 gap-fill — once the skill registry is populated, hook
        # it into the MCP server so a (manifest-AUTO, mcp-surface-safe)
        # subset of FERAL skills can be exposed to external MCP clients
        # like Claude Desktop / Cursor. Projection is OFF unless the
        # operator explicitly enables it via env var or the dedicated
        # POST /api/mcp/projection route (added by api.routes.mcp).
        try:
            if self.mcp_server is not None and self.skill_registry is not None:
                self.mcp_server.configure_skill_projection(
                    skill_registry=self.skill_registry,
                    skill_executor=self.skill_executor,
                    enabled=bool(os.getenv("FERAL_MCP_PROJECT_SKILLS")),
                )
        except Exception as exc:  # pragma: no cover - defensive boot wiring
            logger.warning("MCP projection wiring failed: %s", exc)

        with boot_subsystem(self._boot_report, "TaskFlowRuntime"):
            self.taskflows = TaskFlowRuntime(memory_store=self.memory)
            await self.taskflows.start()

        with boot_subsystem(self._boot_report, "UploadStore"):
            # PR 10: canonical chat-attachment store. Lives entirely on
            # local disk under $FERAL_HOME/uploads and never auto-syncs
            # anywhere — local-first contract.
            from memory.uploads import UploadStore
            self.uploads = UploadStore()

        with boot_subsystem(self._boot_report, "ApprovalManager"):
            from security.exec_approvals import ApprovalManager
            self.approval_manager = ApprovalManager()

        with boot_subsystem(self._boot_report, "Orchestrator", optional=False):
            self.orchestrator = Orchestrator(
                skill_registry=self.skill_registry,
                send_to_client=self.send_to_session,
                daemons=self.daemons,
                memory=self.memory,
                vision_buffer=self.vision_buffer,
                perception=self.perception,
                learner=self.learner,
                taskflows=self.taskflows,
                approval_manager=self.approval_manager,
            )
            self.orchestrator.set_llm(_shared_llm)
            # v2026.5.26 — apply persisted autonomy mode from
            # `~/.feral/settings.json::security.autonomy_mode`.
            # Pre-fix the boot path always reverted to "hybrid" (or
            # whatever `FERAL_AUTONOMY` env var pinned), so the
            # operator's WebUI Settings -> Autonomy choice never
            # survived a restart. Env var still wins for ops who
            # explicitly pin it via `FERAL_AUTONOMY`.
            try:
                _autonomy_env = os.environ.get("FERAL_AUTONOMY", "").strip().lower()
                if not _autonomy_env and self.config:
                    cfg_get = getattr(self.config, "get_setting", None) or self.config.get
                    _persisted = ""
                    if cfg_get is self.config.get:
                        # `ConfigLoader.get(section, key)` is the
                        # canonical accessor — `get_setting` doesn't
                        # exist on ConfigLoader (the API mismatch
                        # flagged by the v2026.5.26 audit).
                        _persisted = cfg_get("security", "autonomy_mode") or ""
                    else:
                        try:
                            _persisted = cfg_get("security", "autonomy_mode") or ""
                        except Exception:
                            _persisted = ""
                    _persisted = str(_persisted).strip().lower()
                    if _persisted in ("strict", "hybrid", "loose"):
                        self.orchestrator.tool_runner.set_autonomy_mode(_persisted)
                        logger.info(
                            "Loaded persisted autonomy_mode=%s from settings.json",
                            _persisted,
                        )
            except Exception as _auto_exc:
                logger.warning(
                    "autonomy boot-load skipped: %s — defaulting to hybrid",
                    _auto_exc,
                )
            # Phase 3 (audit-r10 overhaul) — wire snapshot store +
            # hydrate the primary thread from disk so the operator's
            # last ~50 turns survive brain restart. Persistence on the
            # write side fires from `Orchestrator._maybe_snapshot_primary`
            # after each turn; this just hands the orchestrator a
            # reference back to BrainState so it can call us.
            try:
                self.session_snapshot = SessionSnapshotStore(feral_data_home())
                self.orchestrator.set_session_snapshot_hook(self.snapshot_primary_thread)
                self._hydrate_primary_thread_from_snapshot()
            except Exception as snap_exc:
                logger.warning(
                    "Primary session snapshot wiring failed: %s — boot continues",
                    snap_exc,
                )
            # Phase 5 (audit-r10 overhaul) — wire capability registry
            # so `ToolRunner.execute_capability_action(...)` can route
            # `phone.*` / `glasses.*` actions or fail truthfully when
            # no connected node publishes the action.
            try:
                self.orchestrator.set_capability_registry(self.capability_registry)
            except Exception as cap_exc:
                logger.warning(
                    "Capability registry wiring failed: %s — boot continues",
                    cap_exc,
                )
            # Phase 11 (audit-r10 overhaul) — register the
            # desktop_control facade's brain-host skill manifests so
            # GET /api/capabilities surfaces them and the orchestrator
            # can route `desktop.*` actions in-process without an HUP
            # round-trip. Imported lazily so a non-Darwin host still
            # boots cleanly; the dispatcher's own platform guard
            # raises AppleScriptUnsupportedPlatform at action time on
            # those hosts.
            try:
                from skills.desktop_control import BRAIN_HOST_MANIFESTS

                self.capability_registry.register_brain_host_skills(BRAIN_HOST_MANIFESTS)
                logger.info(
                    "Registered %d brain-host desktop_control skill manifests",
                    len(BRAIN_HOST_MANIFESTS),
                )
            except Exception as dc_exc:
                logger.warning(
                    "desktop_control registration failed: %s — boot continues",
                    dc_exc,
                )
            if self.vault:
                self.orchestrator.set_vault(self.vault)
            if self.wasm_sandbox:
                self.orchestrator.executor.set_wasm_sandbox(self.wasm_sandbox)
            if self.mcp_client:
                self.orchestrator.set_mcp_client(self.mcp_client)

        with boot_subsystem(self._boot_report, "Supervisor"):
            # One seat that sees every input the Brain acts on. Wraps the
            # orchestrator's three public entry points with an audit +
            # kill-switch layer. Keeps the orchestrator unchanged.
            from agents.supervisor import Supervisor as _Supervisor

            def _broadcast_supervisor_event(frame: dict):
                if not self.sessions:
                    return None
                async def _fan():
                    for sid in list(self.sessions.keys()):
                        try:
                            await self.send_to_session(sid, frame)
                        except Exception:
                            pass
                return _fan()

            self.supervisor = _Supervisor(broadcaster=_broadcast_supervisor_event)
            self.supervisor.wrap(self.orchestrator)

        with boot_subsystem(self._boot_report, "TwinPolicyEngine"):
            # Digital-twin policy + approval queue. The twin's execute()
            # checks this engine before acting on the user's behalf; the
            # engine in turn respects the Supervisor kill switch.
            from agents.twin_policy import TwinPolicyEngine as _TPE
            self.twin_policy = _TPE(supervisor=self.supervisor)
            if self.digital_twin is not None:
                self.digital_twin.set_policy_engine(self.twin_policy)

        with boot_subsystem(self._boot_report, "RealtimeProxy"):
            self.realtime_proxy = RealtimeProxy(
                skill_registry=self.skill_registry,
                skill_executor=self.orchestrator.executor if self.orchestrator else None,
                memory=self.memory,
                perception=self.perception,
                send_to_node=self._send_dict_to_node,
                send_to_session=self.send_to_session,
                identity_workspace=self.identity_workspace,
                # PR9: hand the orchestrator so voice tool calls emit
                # the same tool_start/tool_result trace as chat.
                orchestrator=self.orchestrator,
            )

        with boot_subsystem(self._boot_report, "VoiceRouter"):
            self.voice_router = VoiceRouter(
                realtime_proxy=self.realtime_proxy,
                audio_pipeline=self.audio,
                orchestrator=self.orchestrator,
                memory=self.memory,
                perception=self.perception,
                wake_word_detector=self.wake_word,
                send_to_session=self.send_to_session,
                send_to_node=self._send_dict_to_node,
            )
            # Wire the realtime proxy back into the router so OpenAI
            # Realtime failures (WS 1013 insufficient_quota, 429,
            # invalid_api_key, etc.) trigger
            # ``VoiceRouter.handle_realtime_failure`` which emits the
            # ``voice_status`` frame + flips the session into whisper
            # TTS fallback mode. Without this, the receive loop just
            # logs and dies silent. Pinned by
            # ``tests/test_voice_realtime_quota_fallback.py``.
            if self.realtime_proxy:
                self.realtime_proxy.attach_fallback_router(self.voice_router)
            # Hand the router to the orchestrator so
            # ``response_delivery.send_text`` can synthesise mp3
            # ``tts_chunk`` fallback audio on degraded sessions
            # (operator report 2026-05-18: voice silent on every
            # surface after OpenAI Realtime credit drained).
            if self.orchestrator:
                self.orchestrator.voice_router = self.voice_router

        with boot_subsystem(self._boot_report, "GeminiRealtimeProxy"):
            self.gemini_proxy = GeminiRealtimeProxy(
                skill_registry=self.skill_registry,
                skill_executor=self.orchestrator.executor if self.orchestrator else None,
                memory=self.memory,
                perception=self.perception,
                send_to_node=self._send_dict_to_node,
                send_to_session=self.send_to_session,
                # PR9: voice tools share the same trace pipeline as chat.
                orchestrator=self.orchestrator,
            )
            if self.voice_router:
                self.voice_router.set_gemini_proxy(self.gemini_proxy)

        with boot_subsystem(self._boot_report, "MethodRegistry"):
            self.gateway_registry = MethodRegistry()
            register_core_methods(self.gateway_registry, self)

        with boot_subsystem(self._boot_report, "HardwareMesh"):
            self.hardware_mesh = HardwareMesh(
                device_registry=self.device_registry,
                daemons=self.daemons,
            )

        with boot_subsystem(self._boot_report, "IdentityWorkspace"):
            self.identity_workspace = IdentityWorkspace()
            self.identity_workspace.sync_tools_from_registry(self.skill_registry)

        self.screen_loop = None
        with boot_subsystem(self._boot_report, "ScreenLoop"):
            from perception.screen_loop import ScreenLoop
            self.screen_loop = ScreenLoop(
                perception=self.perception,
                memory=self.memory,
                llm=_shared_llm,
                scene_analyzer=self.scene,
            )
            # A6 — only start the ambient screen-capture loop when the
            # operator has explicitly opted in. Before this gate the
            # loop spent vision-model API quota on every cold start
            # regardless of ``features.vision`` / ``vision.enabled``.
            # The config loader coalesces both keys into
            # ``FERAL_VISION_ENABLED`` (see ``config/loader.py``).
            if _feature_flag_enabled("FERAL_VISION_ENABLED"):
                import asyncio
                self.register_background_task(
                    asyncio.create_task(
                        self.screen_loop.start(),
                        name="feral-screen-loop-bootstrap",
                    )
                )
            else:
                logger.info(
                    "ScreenLoop gated off at boot "
                    "(FERAL_VISION_ENABLED not truthy)"
                )

        self.session_handoff = None
        with boot_subsystem(self._boot_report, "SessionHandoffManager"):
            from agents.session_handoff import SessionHandoffManager
            self.session_handoff = SessionHandoffManager(
                sessions=self.sessions,
                daemons=self.daemons,
                memory=self.memory,
                send_to_session=self.send_to_session,
            )

        with boot_subsystem(self._boot_report, "GenUIEngine"):
            self.genui_engine = GenUIEngine(llm=_shared_llm)
            self.service_providers = ServiceProviderRegistry()
            if self.orchestrator:
                self.orchestrator.set_genui_engine(self.genui_engine)

        with boot_subsystem(self._boot_report, "AppRegistry"):
            # Install directory + SQLite index of third-party GenUI apps.
            # HybridGenerator wraps GenUIEngine to add the authored /
            # cached / regenerate branches the plan specifies.
            self.hybrid_genui = HybridGenerator(
                genui_engine=self.genui_engine,
                cache_dir=default_hybrid_cache_dir(),
            )
            self.app_registry = AppRegistry(
                db_path=default_apps_db_path(),
                apps_dir=default_apps_dir(),
                hybrid_generator=self.hybrid_genui,
            )
            # Runtime-first baseline app: ship one starter bundle at boot so
            # phone/genui routing can be validated in a fresh environment.
            starter_dir = (
                Path(__file__).resolve().parents[2]
                / "examples"
                / "apps"
                / "feral-reminders"
            )
            if starter_dir.is_dir():
                try:
                    if self.app_registry.get("feral-reminders") is None:
                        self.app_registry.install_from_dir(
                            starter_dir,
                            overwrite=False,
                        )
                except Exception as exc:
                    logger.warning("Starter app bootstrap skipped: %s", exc)

        with boot_subsystem(self._boot_report, "BrowserController"):
            self.browser = BrowserController()
            self._register_browser_skill()

        with boot_subsystem(self._boot_report, "DockerSandbox"):
            from security.docker_sandbox import get_sandbox
            self.docker_sandbox = get_sandbox()
        # The sandbox object may import cleanly even when the Docker daemon
        # is not actually reachable — in that case we can't execute anything,
        # so mark the subsystem DEGRADED rather than the misleading green OK.
        try:
            if self.docker_sandbox is None:
                self._boot_report.mark_degraded(
                    "DockerSandbox",
                    "Docker not installed — code execution skill disabled",
                )
            elif hasattr(self.docker_sandbox, "available") and not self.docker_sandbox.available():
                self._boot_report.mark_degraded(
                    "DockerSandbox",
                    "Docker daemon not running — start Docker Desktop to enable code exec",
                )
        except Exception as _e:
            self._boot_report.mark_degraded(
                "DockerSandbox",
                f"availability probe failed: {type(_e).__name__}",
            )

        with boot_subsystem(self._boot_report, "CronService"):
            from agents.scheduler import CronService
            self.cron_service = CronService()
            self.scheduler = self.cron_service
            self.skill_registry.set_cron_service(self.cron_service)

        with boot_subsystem(self._boot_report, "BaselineEngine"):
            _baseline_db = str(feral_home() / "baselines.db")
            self.baseline_engine = BaselineEngine(db_path=_baseline_db)

        with boot_subsystem(self._boot_report, "AboutMeStore"):
            _about_me_db = str(feral_home() / "about_me.db")
            self.about_me = AboutMeStore(db_path=_about_me_db)
            self.memory.set_about_me_store(self.about_me)

        with boot_subsystem(self._boot_report, "IdeasEngine"):
            _ideas_db = str(feral_home() / "ideas.db")
            _ideas_store = IdeasStore(db_path=_ideas_db)

            def _on_ideas_updated(_ideas):
                try:
                    import asyncio as _aio
                    loop = _aio.get_running_loop()
                except RuntimeError:
                    return
                loop.create_task(self.broadcast_event("ideas_updated", {"count": len(_ideas)}))

            try:
                _settings = self.config.get_settings() if hasattr(self.config, "get_settings") else {}
            except Exception:
                _settings = {}
            _polish_enabled = bool((_settings or {}).get("ideas_llm_polish", False))

            self.ideas_engine = IdeasEngine(
                store=_ideas_store,
                consciousness=self.consciousness,
                baseline=self.baseline_engine,
                about_me=self.about_me,
                on_ideas_updated=_on_ideas_updated,
                llm_polish_enabled=_polish_enabled,
            )
            if self.baseline_engine is not None:
                self.baseline_engine.on_alert(self.ideas_engine.handle_baseline_alert)

        with boot_subsystem(self._boot_report, "SomaticEngine"):
            from perception.somatic import SomaticEngine
            self.somatic_engine = SomaticEngine()
            if self.orchestrator:
                self.orchestrator.set_somatic_engine(self.somatic_engine)

        # Audit-r9 fix: wire the CalendarIntegration into the
        # orchestrator's IdentityLoader so the system prompt carries a
        # "## Today's Events" block on every turn. Without this, the
        # iOS chat had no way to know about events the operator
        # created on the web tab (subagent #cd995a59 confirmed root
        # cause: prompt assembly didn't preload calendar). See
        # `Orchestrator.set_calendar` for the long-form rationale.
        if self.orchestrator and self.calendar:
            self.orchestrator.set_calendar(self.calendar)

        with boot_subsystem(self._boot_report, "ToolGenesisEngine"):
            from agents.tool_genesis import ToolGenesisEngine
            _genesis_db = str(feral_data_home() / "tool_genesis.db")
            self.tool_genesis = ToolGenesisEngine(llm=_shared_llm, db_path=_genesis_db)
            if self.orchestrator:
                self.orchestrator.set_tool_genesis(self.tool_genesis)

        with boot_subsystem(self._boot_report, "AgentMitosisEngine"):
            from agents.agent_mitosis import AgentMitosisEngine
            _mitosis_db = str(feral_data_home() / "agent_mitosis.db")
            self.agent_mitosis = AgentMitosisEngine(llm=_shared_llm, memory=self.memory, db_path=_mitosis_db)
            if self.orchestrator:
                self.orchestrator.set_mitosis_engine(self.agent_mitosis)

        with boot_subsystem(self._boot_report, "IntentCompiler"):
            from agents.intent_compiler import IntentCompiler
            _intent_db = str(feral_data_home() / "intents.db")
            self.intent_compiler = IntentCompiler(llm=_shared_llm, db_path=_intent_db)

        if self.ideas_engine is not None:
            async def _ideas_daily_brief_loop():
                while True:
                    now = time.time()
                    dt = datetime.fromtimestamp(now).astimezone()
                    target = dt.replace(hour=7, minute=30, second=0, microsecond=0)
                    if dt >= target:
                        target = target.replace(day=target.day) + _timedelta(days=1)
                    wait_s = max(60.0, (target.timestamp() - now))
                    await asyncio.sleep(wait_s)
                    try:
                        self.ideas_engine.morning_brief()
                    except Exception as exc:
                        logger.debug("Ideas morning_brief failed: %s", exc)

            from datetime import datetime, timedelta as _timedelta
            import asyncio
            self.register_background_task(
                asyncio.create_task(_ideas_daily_brief_loop(), name="feral-ideas-daily-brief")
            )
            try:
                self.ideas_engine.morning_brief()
            except Exception as exc:
                logger.debug("Ideas initial morning_brief failed: %s", exc)

        self.proactive = None
        with boot_subsystem(self._boot_report, "ProactiveEngine"):
            from agents.proactive_engine import ProactiveEngine
            self.proactive = ProactiveEngine(
                perception=self.perception,
                memory=self.memory,
                orchestrator=self.orchestrator,
                llm=_shared_llm,
                calendar=self.calendar,
                health_aggregator=self.health_aggregator,
                baseline_engine=self.baseline_engine,
            )

            async def _proactive_delivery(msg):
                alert = {
                    "trigger_id": msg.trigger_id,
                    "priority": msg.priority.name,
                    "title": msg.title,
                    "body": msg.body,
                    "action": msg.action,
                    "action_payload": msg.action_payload,
                    "sdui": msg.sdui,
                    "voice_text": msg.voice_text,
                }
                await self.broadcast_event("proactive_alert", alert)

            self.proactive.on_message(_proactive_delivery)
            # A6 — only start the proactive loop when enabled. Before
            # this gate the engine ran its rule-based + 60s-LLM
            # evaluation on every cold start regardless of
            # ``features.proactive`` / ``FERAL_PROACTIVE``.
            # ``ProactiveEngine.start`` schedules its inner loop task
            # and returns fast (A7), so it's safe to ``await`` it.
            if _feature_flag_enabled("FERAL_PROACTIVE"):
                await self.proactive.start()
                if getattr(self.proactive, "_task", None) is not None:
                    self.register_background_task(self.proactive._task)
            else:
                logger.info(
                    "ProactiveEngine gated off at boot "
                    "(FERAL_PROACTIVE not truthy)"
                )

        with boot_subsystem(self._boot_report, "MQTTBridge"):
            self.mqtt_bridge = MQTTBridge()
            if self.mqtt_bridge.configured:
                await self.mqtt_bridge.start()

        with boot_subsystem(self._boot_report, "EmailWatcher"):
            async def _handle_email(incoming):
                text = (
                    f'New email from {incoming.sender}: "{incoming.subject}"\n\n'
                    f"{incoming.body[:2000]}"
                )
                for sid in list(self.sessions.keys())[:1]:
                    await self.orchestrator.handle_command(
                        sid,
                        text,
                        context={
                            "source": "email",
                            "message_id": incoming.message_id,
                        },
                    )

            self.email_watcher = EmailWatcher(on_email=_handle_email)
            if self.email_watcher.configured:
                await self.email_watcher.start()

        with boot_subsystem(self._boot_report, "mDNS"):
            from services.mdns import advertise_brain, discover_phone_bridge
            from config.runtime import brain_port
            advertise_brain(port=brain_port())

            try:
                settings = self.config.get_settings() if hasattr(self.config, "get_settings") else {}
            except Exception:
                settings = {}
            phone_url = (settings or {}).get("phone_bridge_url") or os.environ.get("FERAL_PHONE_BRIDGE_URL", "")
            if phone_url == "auto":
                discovered = discover_phone_bridge(timeout=2.5)
                if discovered:
                    logger.info("mDNS: phone bridge auto-discovered at %s", discovered)
                    os.environ["FERAL_PHONE_BRIDGE_URL"] = discovered
                    try:
                        if hasattr(self.config, "set_setting"):
                            self.config.set_setting("phone_bridge_url_resolved", discovered)
                    except Exception:
                        pass
                else:
                    logger.info("mDNS: no phone bridge found on LAN (continuing without)")

        # Wire inbound channels to the orchestrator
        await self._start_channels()

        if os.environ.get("FERAL_DEMO"):
            logger.warning("FERAL_DEMO is deprecated and ignored. Use FERAL_DEV_DEMO=1 for dev-only demo mode.")

        # Demo mode is opt-in dev-only. Synthetic biometrics + scripted
        # scenarios live in the optional `feral-demo-data` package, never
        # in the production wheel for `pip install feral-ai`. We discover
        # the demo plugin via the `feral.plugins` entry point group; if
        # the operator sets FERAL_DEV_DEMO=1 without `feral-demo-data`
        # installed we fail loud rather than silently no-op.
        self._demo = None
        if os.environ.get("FERAL_DEV_DEMO", "").lower() in ("1", "true", "yes"):
            with boot_subsystem(self._boot_report, "DemoMode"):
                self._demo = self._bootstrap_demo_plugin()

        self._boot_report.total_elapsed_ms = (time.time() - _boot_start) * 1000
        self._boot_report.log_summary()

        stats = await self.memory.stats()
        demo_tag = " [DEMO MODE]" if self._demo else ""
        logger.info(
            f"Brain v{__version__} initialized{demo_tag} — {len(self.skill_registry.skills)} skills, "
            f"{stats['notes']} notes, {stats['knowledge_triples']} knowledge triples, "
            f"{stats['episodes']} episodes | Self-learning: ON | Vault: {len(self.vault.list_keys()) if self.vault else 0} keys"
        )

    # ─────────────────────────────────────────────────────────────────
    # Phase 3 — primary-session lifecycle helpers
    # ─────────────────────────────────────────────────────────────────

    def attach_session(self, session_id: str) -> int:
        """Mark a new surface (web tab, phone daemon) as attached to a
        session_id. Returns the post-attach reference count.

        Called from `/v1/session` WebSocket accept. Multiple browser
        tabs sharing `primary_session_id` each call attach once; the
        count tracks how many sockets are alive on that thread.
        """
        if not session_id:
            return 0
        n = self.session_attach_count.get(session_id, 0) + 1
        self.session_attach_count[session_id] = n
        return n

    def detach_session(self, session_id: str) -> int:
        """Mark a surface as detached. Returns the post-detach reference
        count.

        Server's `WebSocketDisconnect` handler MUST call this before
        running per-session cleanup; cleanup only runs when the
        returned count is zero. For `primary_session_id` the cleanup
        path is additionally short-circuited by
        `should_clear_on_disconnect()` so the primary thread survives
        even when every surface has detached momentarily.
        """
        if not session_id:
            return 0
        current = self.session_attach_count.get(session_id, 0)
        n = max(0, current - 1)
        if n == 0:
            self.session_attach_count.pop(session_id, None)
        else:
            self.session_attach_count[session_id] = n
        return n

    def should_clear_on_disconnect(self, session_id: str) -> bool:
        """Return True when per-session cleanup should run after the
        last surface detaches.

        Returns False for `primary_session_id` so the shared thread
        survives surface lifecycle — the persistent thread is durable
        by design. Non-primary sessions still clean up on last detach
        as today.
        """
        if not session_id:
            return False
        if session_id == self.primary_session_id:
            return False
        return self.session_attach_count.get(session_id, 0) == 0

    def snapshot_primary_thread(self, *, force: bool = False) -> bool:
        """Save the current primary `conversation_history` +
        `working_memory` to disk so the next boot rehydrates.

        Called from the orchestrator after each successful turn, and
        on FastAPI shutdown. The store is debounced (~2.5s) so a hot
        chat loop isn't IO-bound. ``force=True`` bypasses debounce —
        used on shutdown to guarantee the last turn lands.
        """
        if self.session_snapshot is None or not self.primary_session_id:
            return False
        sid = self.primary_session_id
        history = None
        if self.orchestrator is not None:
            ch = getattr(self.orchestrator, "conversation_history", None)
            if isinstance(ch, dict):
                history = list(ch.get(sid, []) or [])
        working = None
        if self.memory is not None:
            try:
                working = self.memory.working_get(sid, limit=200) or []
            except Exception:
                working = None
        return self.session_snapshot.save(
            sid,
            conversation_history=history,
            working_memory=working,
            force=force,
        )

    def _hydrate_primary_thread_from_snapshot(self) -> None:
        """Read the on-disk primary-session snapshot (if any) and
        replay it into the in-RAM orchestrator + working memory.

        Called late in ``init()`` after orchestrator + memory are
        constructed. Never raises — if anything goes wrong the brain
        boots with an empty primary thread (today's behaviour).
        """
        if self.session_snapshot is None or self.orchestrator is None:
            return
        snapshot = self.session_snapshot.load()
        if not snapshot:
            return
        sid = snapshot.get("session_id") or self.primary_session_id
        if not sid:
            return
        # Orchestrator conversation_history (LLM tool-call format).
        ch_rows = snapshot.get("conversation_history") or []
        if isinstance(ch_rows, list) and ch_rows:
            ch_attr = getattr(self.orchestrator, "conversation_history", None)
            if isinstance(ch_attr, dict):
                # Defensive deep-copy so future mutations don't bleed
                # back into the snapshot in-memory.
                rehydrated = [dict(x) for x in ch_rows if isinstance(x, dict)]
                # v2026.5.29 — scrub orphan tool results before they
                # ever reach the orchestrator's history. Older brain
                # builds (pre tool-aware compaction) could persist a
                # snapshot whose tail begins inside a tool round-trip
                # with no announcing assistant turn. Reloading that
                # verbatim leaks an orphan ``function_call_output``
                # into the next LLM request and triggers
                # ``400 No tool call found for function call output``.
                ch_attr[sid] = _sanitize_orphan_tool_rows(rehydrated)
        # Working-memory deque (LLM-context format).
        wm_rows = snapshot.get("working_memory") or []
        if isinstance(wm_rows, list) and wm_rows and self.memory is not None:
            try:
                self.memory.working_replace(sid, [dict(x) for x in wm_rows if isinstance(x, dict)])
            except Exception:
                pass
        logger.info(
            "Primary session rehydrated from snapshot: %d conv rows, %d working rows",
            len(ch_rows) if isinstance(ch_rows, list) else 0,
            len(wm_rows) if isinstance(wm_rows, list) else 0,
        )

    def _load_or_mint_primary_session_id(self) -> str:
        """Read or mint the per-install shared chat session id.

        Stored as a single line at `<feral_data_home>/primary_session_id`.
        Operators on a custom layout can override via env var
        `FERAL_PRIMARY_SESSION_ID` (useful for tests + integration
        runs that want a deterministic id). The file is created on
        first boot and persists across restarts so the LLM keeps
        the same `conversation_history[session_id]` thread for both
        web and phone clients.
        """
        from uuid import uuid4

        env_override = os.environ.get("FERAL_PRIMARY_SESSION_ID", "").strip()
        if env_override:
            return env_override

        try:
            data_home = feral_data_home()
            data_home.mkdir(parents=True, exist_ok=True)
            path = data_home / "primary_session_id"
            if path.is_file():
                existing = path.read_text().strip()
                if existing:
                    return existing
            new_id = f"primary-{uuid4().hex[:16]}"
            path.write_text(new_id + "\n")
            return new_id
        except Exception as exc:
            # Filesystem failure must NOT block boot. Fall back to a
            # process-lifetime id; operator will see the same brain
            # behavior except the session resets across restarts.
            logger.warning(
                "primary_session_id persistence failed (%s); "
                "using process-lifetime id (chat will reset on restart).",
                exc,
            )
            return f"primary-ephemeral-{uuid4().hex[:16]}"

    def _bootstrap_demo_plugin(self):
        """Look up + invoke the `feral.plugins` -> `demo` entry point.

        Returns the started demo orchestrator (opaque to core) or
        raises with a clear install hint if `feral-demo-data` isn't
        installed. Fail-loud is deliberate: silent no-op would leave
        operators wondering why simulators vanished after upgrade.
        """
        try:
            from importlib.metadata import entry_points
        except ImportError:  # py<3.10 fallback (we require 3.11+)
            from importlib_metadata import entry_points  # type: ignore

        try:
            eps = entry_points(group="feral.plugins")
        except TypeError:
            # Older Python: entry_points() returned a dict
            eps = entry_points().get("feral.plugins", [])  # type: ignore

        demo_ep = None
        for ep in eps:
            if ep.name == "demo":
                demo_ep = ep
                break

        if demo_ep is None:
            raise RuntimeError(
                "FERAL_DEV_DEMO=1 set but `feral-demo-data` is not installed. "
                "Run: pip install feral-demo-data   (or: pip install feral-ai[demo])"
            )

        plugin_factory = demo_ep.load()
        plugin = plugin_factory()  # returns dict with bootstrap/status_routes/cli_handler
        bootstrap = plugin.get("bootstrap")
        if not callable(bootstrap):
            raise RuntimeError(
                "feral-demo-data plugin contract violation: missing bootstrap()"
            )
        return bootstrap(self)

    async def _start_channels(self):
        """Wire inbound channel messages to the orchestrator and start configured channels."""
        if not self.channel_manager or not self.orchestrator:
            return

        brain = self

        async def _channel_message_handler(channel_msg: ChannelMessage) -> ChannelResponse:
            channel_session_id = f"channel_{channel_msg.channel_type}_{channel_msg.user_id}"
            text = channel_msg.text or ""
            if not text:
                return ChannelResponse(text="")

            if brain.memory and brain.sessions:
                desktop_sid = next(iter(brain.sessions), None)
                if desktop_sid:
                    history = brain.memory.working_get(desktop_sid, limit=10)
                    if history:
                        brain.memory.working_replace(channel_session_id, list(history))

            if brain.session_handoff:
                # Messaging bridges are channels, not phones. Labelling them
                # "phone" made every Telegram/Slack/Discord session show up
                # as a permanently-connected phone even when the user never
                # paired one. Honest label = "channel" (see NODE_TYPES).
                brain.session_handoff.register_device(
                    channel_session_id,
                    "channel",
                    node_id=f"{channel_msg.channel_type}_{channel_msg.user_id}",
                )

            brain._channel_collectors[channel_session_id] = []
            try:
                await brain.orchestrator.handle_command(channel_session_id, text, context={
                    "source": "channel",
                    "channel": channel_msg.channel_type,
                    "user_id": channel_msg.user_id,
                    "username": channel_msg.username,
                })
            except Exception as e:
                logger.warning(f"Channel command failed: {e}")
                return ChannelResponse(text=f"Error: {e}")
            finally:
                collected = brain._channel_collectors.pop(channel_session_id, [])

            response_text = "\n".join(collected) if collected else "Done."
            return ChannelResponse(text=response_text)

        self.channel_manager.set_handler(_channel_message_handler)

        def _cred(key: str) -> str:
            """Resolve a channel credential without mutating ``os.environ``.

            W3-A13 — channel tokens are read in priority order:
            1. ``self.config._credentials`` (the in-process source of
               truth, populated from the encrypted vault and from
               ``credentials.json`` at boot).
            2. Process env (operator may set FERAL_TELEGRAM_BOT_TOKEN
               etc. directly for headless deployments).
            3. ``credentials.json`` on disk as a last-ditch fallback for
               very old layouts where neither the vault nor the merged
               config reached the in-memory ``_credentials`` dict.

            Previously this helper exported the resolved value into
            ``os.environ`` so subsequent reads would see it, which made
            two BrainState init cycles (e.g. test-suite teardown / setup)
            silently leak channel tokens into the process env. The token
            now flows explicitly through the channel config dict below;
            no global env mutation is required.
            """
            try:
                if self.config and hasattr(self.config, "get_credential"):
                    v = self.config.get_credential(key) or ""
                    if v:
                        return v
            except Exception:
                pass
            v = os.environ.get(key, "") or ""
            if v:
                return v
            try:
                import json as _json
                creds_path = feral_home() / "credentials.json"
                if creds_path.exists():
                    creds = _json.loads(creds_path.read_text())
                    v = creds.get(key) or ""
            except Exception:
                pass
            if v:
                return v
            # Final fallback for vault-only installs (v2026.5.0+). This
            # keeps channel startup independent of ``os.environ`` export.
            try:
                if self.vault and hasattr(self.vault, "retrieve"):
                    v = self.vault.retrieve(key) or ""
            except Exception:
                pass
            return v

        # Operator-level channel gates: settings.features.<channel>
        # (default True for legacy behavior). Without this, any install
        # whose vault carries a bot_token auto-starts polling loops even
        # when the operator explicitly toggled the channel off in
        # settings — which is what the phone test surfaced (telegram
        # pings /getUpdates every 30s despite features.telegram=false).
        merged_cfg = {}
        if self.config:
            merged_cfg = getattr(self.config, "_merged", {}) or {}
            if not isinstance(merged_cfg, dict):
                merged_cfg = {}
        features_cfg = merged_cfg.get("features") or {}
        if not isinstance(features_cfg, dict):
            features_cfg = {}

        def _ch_enabled(name: str, default: bool = True) -> bool:
            val = features_cfg.get(name)
            if val is None:
                return default
            if isinstance(val, bool):
                return val
            if isinstance(val, str):
                return val.lower() in ("true", "1", "yes", "on")
            return bool(val)

        channel_configs = {
            "telegram": {
                "bot_token": _cred("FERAL_TELEGRAM_BOT_TOKEN"),
                "enabled": _ch_enabled("telegram")
                           and bool(_cred("FERAL_TELEGRAM_BOT_TOKEN")),
            },
            "discord": {
                "bot_token": _cred("FERAL_DISCORD_BOT_TOKEN"),
                "enabled": _ch_enabled("discord")
                           and bool(_cred("FERAL_DISCORD_BOT_TOKEN")),
            },
            "slack": {
                "bot_token": _cred("FERAL_SLACK_BOT_TOKEN"),
                "app_token": _cred("FERAL_SLACK_APP_TOKEN"),
                "enabled": _ch_enabled("slack")
                           and bool(_cred("FERAL_SLACK_BOT_TOKEN")),
            },
            "whatsapp": {
                "access_token": _cred("FERAL_WHATSAPP_ACCESS_TOKEN"),
                "phone_number_id": _cred("FERAL_WHATSAPP_PHONE_NUMBER_ID"),
                "app_secret": _cred("FERAL_WHATSAPP_APP_SECRET"),
                "enabled": bool(_cred("FERAL_WHATSAPP_ACCESS_TOKEN") and _cred("FERAL_WHATSAPP_PHONE_NUMBER_ID")),
            },
        }

        started = []
        for ch_type, ch_config in channel_configs.items():
            if ch_config.get("enabled"):
                try:
                    await self.channel_manager.start_channel(ch_type, ch_config)
                    started.append(ch_type)
                except Exception as e:
                    logger.warning(f"Channel {ch_type} start failed: {e}")

        if started:
            logger.info(f"Channels started: {', '.join(started)}")
        else:
            logger.debug("No messaging channels configured (set FERAL_TELEGRAM_BOT_TOKEN etc.)")

    def _register_browser_skill(self):
        """Register browser control as a skill the agent can call via tool use."""
        try:
            from skills.impl.browser_use import get_browser_skill_manifest
            from skills.impl import register_instance
            from models.skill_manifest import (
                SkillManifest,
                SkillEndpoint,
                EndpointParam,
                BrandProfile,
                AuthConfig,
            )

            raw_manifest = get_browser_skill_manifest()
            endpoints: list[SkillEndpoint] = []
            for endpoint in raw_manifest.get("endpoints", []):
                params = []
                for param in endpoint.get("params", []):
                    ptype = str(param.get("type", "string"))
                    if ptype not in {"string", "number", "integer", "boolean", "array", "object"}:
                        ptype = "string"
                    params.append(
                        EndpointParam(
                            name=str(param.get("name", "")),
                            type=ptype,
                            required=bool(param.get("required", True)),
                            description=str(param.get("description", "")),
                            default=str(param.get("default")) if param.get("default") is not None else None,
                            enum=[str(v) for v in (param.get("enum") or [])],
                            items=param.get("items"),
                        )
                    )

                endpoint_id = str(endpoint.get("id", ""))
                endpoints.append(
                    SkillEndpoint(
                        id=endpoint_id,
                        method=endpoint.get("method", "PYTHON"),
                        url=endpoint.get("url", f"feral://browser/{endpoint_id}"),
                        description=str(endpoint.get("description", "")),
                        params=params,
                        returns_description=str(endpoint.get("returns_description", "Browser action result")),
                        ui_hint=endpoint.get("ui_hint"),
                    )
                )

            manifest = SkillManifest(
                skill_id=str(raw_manifest.get("skill_id", "browser")),
                version=str(raw_manifest.get("version", "1.0.0")),
                author="feral-core",
                brand=BrandProfile(
                    name=str(raw_manifest.get("name", "Browser Control")),
                    primary_color="#2563EB",
                    secondary_color="#1D4ED8",
                    logo_url="",
                    icon_set="sf_symbols",
                ),
                description=str(raw_manifest.get("description", "Control and inspect browser pages.")),
                categories=["browser", "automation"],
                auth=AuthConfig(type="none"),
                endpoints=endpoints,
            )
            self.skill_registry.register(manifest)

            class _BrowserSkillBridge:
                def __init__(self, state_ref: "BrainState"):
                    self.skill_id = manifest.skill_id
                    self._state = state_ref

                async def execute(self, endpoint_id: str, args: dict, vault: dict):
                    result = await self._state._execute_browser_action(endpoint_id, args or {})
                    success = not isinstance(result, dict) or bool(result.get("success", "error" not in result))
                    error = result.get("error") if isinstance(result, dict) else None
                    return {
                        "success": success,
                        "status_code": 200 if success else 500,
                        "data": result,
                        "error": error,
                    }

            register_instance(manifest.skill_id, _BrowserSkillBridge(self))
            logger.info(f"Browser skill registered: {manifest.skill_id} ({len(endpoints)} endpoints)")

            # Hand the shared controller to WebActionsSkill so we don't
            # boot a second Chrome / Playwright pair for higher-level
            # web flows. The skill exposes set_browser() exactly for
            # this purpose; without injection it lazily makes its own
            # BrowserController() on first call.
            try:
                from skills.impl import get_implementation
                web_actions = get_implementation("web_actions")
                if web_actions is not None and hasattr(web_actions, "set_browser"):
                    web_actions.set_browser(self.browser)
                    logger.info("WebActionsSkill bound to shared BrowserController")
            except Exception as e:
                logger.debug("WebActionsSkill browser injection skipped: %s", e)
        except Exception as e:
            logger.warning(f"Browser skill registration failed: {e}")

    # Manifest endpoint id → controller method name. Manifest history
    # outpaced the controller (save_session vs save_cookies, etc.); this
    # alias map is the single source of truth that keeps both honest
    # without renaming public agent-visible endpoints again.
    _BROWSER_ENDPOINT_ALIASES = {
        "save_session": "save_cookies",
        "restore_session": "restore_cookies",
        "network_monitor_start": "enable_network_monitor",
        "network_log": "get_network_log",
        # PR 7 gap-fill — tracing / HAR / download endpoints exposed
        # to the agent via stable ids. Aliases map to the controller
        # method names so we can rename internal methods later without
        # breaking the agent surface.
        "trace_start": "start_tracing",
        "trace_stop": "stop_tracing",
        "har_start": "start_har",
        "har_stop": "stop_har",
        "download_next": "wait_for_download",
    }

    async def _execute_browser_action(self, endpoint_id: str, args: dict) -> dict:
        """Execute a browser action when called by the agent."""
        if not self.browser:
            return {"error": "Browser not available"}
        if not self.browser.connected:
            ok = await self.browser.initialize()
            if not ok:
                return {"error": "Cannot connect to Chrome. Start it with --remote-debugging-port=9222"}
        method_name = self._BROWSER_ENDPOINT_ALIASES.get(endpoint_id, endpoint_id)
        method = getattr(self.browser, method_name, None)
        if not method:
            return {"error": f"Unknown browser action: {endpoint_id}"}
        if endpoint_id == "navigate":
            # Forward `wait_until` so the agent can choose between
            # `domcontentloaded` (default), `load`, `networkidle`, or
            # `commit`. Previously dropped on the floor.
            return await method(
                args.get("url", ""),
                wait_until=args.get("wait_until", "domcontentloaded"),
            )
        elif endpoint_id == "screenshot":
            return await method(args.get("full_page", False))
        elif endpoint_id == "snapshot":
            return await method()
        elif endpoint_id == "click":
            return await method(args.get("ref_or_selector", ""))
        elif endpoint_id == "hover":
            return await method(args.get("ref_or_selector", ""))
        elif endpoint_id == "type_text":
            return await method(args.get("ref_or_selector", ""), args.get("text", ""))
        elif endpoint_id == "fill_form":
            return await method(args.get("fields", {}))
        elif endpoint_id == "evaluate":
            return await method(args.get("js_code", ""))
        elif endpoint_id == "scroll":
            return await method(args.get("direction", "down"), args.get("amount", 500))
        elif endpoint_id == "get_console_logs":
            return await method(args.get("limit", 50), args.get("clear", False))
        elif endpoint_id == "get_page_pdf":
            return await method(
                args.get("print_background", True),
                args.get("landscape", False),
            )
        elif endpoint_id == "get_page_info":
            return await method()
        elif endpoint_id == "wait_for_selector":
            return await method(
                args.get("ref_or_selector", ""),
                timeout_ms=int(args.get("timeout_ms", 5000)),
                poll_ms=int(args.get("poll_ms", 100)),
                state=args.get("state", "visible"),
            )
        elif endpoint_id == "save_session" or endpoint_id == "restore_session":
            return await method(args.get("profile", "default"))
        elif endpoint_id == "network_log":
            return await method(args.get("filter_type", ""))
        elif endpoint_id == "network_monitor_start":
            return await method()
        return await method(**args) if args else await method()

    @staticmethod
    def _load_stored_credentials():
        """Load legacy SDK credential keys into process environment.

        If ``credentials.json`` is missing OR corrupt we fall back to
        reading every known env var out of the BlindVault directly.
        The vault is the authoritative store written by
        ``/api/llm/providers/{id}/configure`` and ``/api/config/credentials``;
        ``credentials.json`` is the plaintext mirror used for boot-time
        convenience. Keeping both loadable means a single-file corruption
        never locks a user out of their provider keys.
        """
        env_keys = [
            "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY",
            "GROQ_API_KEY", "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY",
            "TOGETHER_API_KEY", "FIREWORKS_API_KEY",
            "MOONSHOT_API_KEY", "DASHSCOPE_API_KEY",
            "TAVILY_API_KEY", "BRAVE_API_KEY", "EXA_API_KEY",
            "SERPER_API_KEY", "GITHUB_TOKEN", "SPOTIFY_CLIENT_ID",
            "PERPLEXITY_API_KEY", "GOOGLE_API_KEY", "GOOGLE_CSE_ID",
            "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
        ]
        loaded: list[str] = []
        creds: dict = {}
        creds_path = feral_home() / "credentials.json"
        if creds_path.exists():
            try:
                import json as _json
                creds = _json.loads(creds_path.read_text())
            except Exception as exc:
                logger.warning(
                    "credentials.json is corrupt (%s) — falling back to vault", exc,
                )

        for key in env_keys:
            if creds.get(key) and not os.environ.get(key):
                os.environ[key] = creds[key]
                loaded.append(key)
        if creds.get("web_search") and not os.environ.get("TAVILY_API_KEY"):
            os.environ["TAVILY_API_KEY"] = creds["web_search"]
            loaded.append("TAVILY_API_KEY")

        # Vault fallback — always hydrate keys still missing from env,
        # regardless of whether a partial credentials.json already
        # populated some others. A pre-W24b install can legitimately
        # have a subset of keys in the plaintext mirror while the
        # encrypted vault holds the rest (e.g. the user configured
        # OpenAI in v1 and added Anthropic after the /configure route
        # switched to vault-only writes). Gating vault lookup on
        # ``not loaded`` silently stranded those extra keys on every
        # boot; the corrupt-file path is preserved by the outer
        # exception swallow below.
        missing_keys = [key for key in env_keys if not os.environ.get(key)]
        if missing_keys:
            try:
                from security.vault import BlindVault
                vault = BlindVault()
                for key in missing_keys:
                    val = vault.retrieve(key) if hasattr(vault, "retrieve") else None
                    if val:
                        os.environ[key] = val
                        loaded.append(key + "(vault)")
            except Exception as exc:
                logger.debug("vault fallback during boot failed: %s", exc)

        if loaded:
            logger.info(f"Loaded credentials: {', '.join(loaded)}")

    async def send_to_session(self, session_id: str, msg: FeralMessage):
        ws = self.sessions.get(session_id)
        if ws:
            await ws.send_json(msg.model_dump())
        elif session_id in self._channel_collectors:
            payload = msg.payload or {}
            text = payload.get("text", "")
            if text:
                self._channel_collectors[session_id].append(text)

    async def broadcast_event(self, event_type: str, data: dict):
        """Push a state update to all connected WebSocket clients."""
        msg = {"type": "state_push", "event": event_type, "data": data}
        dead = []
        for sid, ws in self.sessions.items():
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(sid)
        for sid in dead:
            self.sessions.pop(sid, None)

    async def send_to_daemon(self, node_id: str, msg: FeralMessage):
        ws = self.daemons.get(node_id)
        if ws:
            await ws.send_json(msg.model_dump())

    async def _send_dict_to_node(self, node_id: str, msg_dict: dict):
        """Send a raw dict message to a daemon node (used by voice pipeline)."""
        ws = self.daemons.get(node_id)
        if ws:
            await ws.send_json(msg_dict)

    def bind_session_to_daemon(self, session_id: str, node_id: str):
        if node_id not in self._daemon_session_bindings:
            self._daemon_session_bindings[node_id] = set()
        self._daemon_session_bindings[node_id].add(session_id)

    def get_sessions_for_daemon(self, node_id: str) -> set[str]:
        return self._daemon_session_bindings.get(node_id, set())


state = BrainState()


def _log_activity(action: str, detail: str = ""):
    """Log an activity to the brain's activity feed."""
    state.activity_log.append({
        "action": action,
        "detail": detail,
        "timestamp": time.time(),
    })
