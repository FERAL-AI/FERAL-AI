"""
FERAL Voice Router — Triple-Path Audio Routing
===============================================
Routes audio based on source capabilities and provider config:
  - Gemini realtime → GeminiRealtimeProxy (Gemini BidiGenerateContent)
  - OpenAI realtime → RealtimeProxy (OpenAI Realtime API)
  - Whisper path    → AudioPipeline (Whisper STT → Orchestrator → TTS)
"""

from __future__ import annotations
import logging
import os
import time
from typing import Any, Callable, Awaitable

logger = logging.getLogger("feral.voice.router")

_ENV_VOICE_PROVIDER = "FERAL_VOICE_PROVIDER"

# ---------------------------------------------------------------------
# Ordered realtime fallback chain
# ---------------------------------------------------------------------
#
# The user's model of provider selection is a chain: they pick a
# provider, and when it fails the router falls through to the next one,
# ending at the local chained pipeline rather than at failure.
#
# Every spelling an operator surface can write for the two realtime
# providers, normalised to the two names the router routes on. The
# setup wizard writes ``openai_realtime`` / ``gemini_live``; the WebUI
# Settings page writes ``openai`` / ``gemini``; ``FERAL_VOICE_PROVIDER``
# has historically taken either.
_REALTIME_ALIASES: dict[str, str] = {
    "openai": "openai",
    "openai_realtime": "openai",
    "gemini": "gemini",
    "gemini_live": "gemini",
    "google": "gemini",
}

#: Terminal link. Reached when every realtime provider in the chain is
#: down, and the reason the chain ends in a working pipeline (local
#: engines included) instead of in a dead session.
CHAIN_TERMINAL = "chained"

#: Order used when the operator has expressed no preference at all.
#: Matches the historical hardcoded behaviour (OpenAI Realtime first).
DEFAULT_REALTIME_ORDER: tuple[str, ...] = ("openai", "gemini")

# Surface policy. The right DEFAULT order depends on who is asking.
#
# The user's rule is that iOS clients must use realtime for speed, and
# that this holds until a better alternative exists. A phone or a pair
# of glasses is latency-bound and usually battery/CPU-bound too, so it
# leads with a realtime provider. A desktop or a browser on a desktop
# can run the chained pipeline locally, which is both cheaper and keeps
# the audio on the machine, so local leads there.
#
# HUP ``node_type`` values (``models/protocol.py`` NodeRegisterPayload,
# ``memory/capability_registry._surface_for_node_type``) plus the
# spellings the JS/Swift clients actually send.
SURFACE_REALTIME_FIRST: frozenset[str] = frozenset({
    "phone", "ios", "iphone", "ipad", "android", "tablet",
    "glasses", "watch", "wearable",
})
SURFACE_LOCAL_FIRST: frozenset[str] = frozenset({
    "desktop", "server", "rpi", "browser_node", "web", "laptop",
})

def _dx():
    """``voice.diagnostics``, imported lazily.

    Every other reference in this module imports it inside the function
    that needs it, because ``diagnostics`` reaches into
    ``security/probe.py`` and importing it at module scope pulls that
    whole tree into any narrow unit test that only wanted the router.
    This keeps that property while giving the open-failure paths one
    short spelling.
    """
    from voice import diagnostics

    return diagnostics


# ``_try_chained_morph`` abort tags. Only the credential one changes
# what the caller emits; the rest are plain "couldn't morph, use the
# legacy degrade".
_MORPH_ABORT_NONE = ""
_MORPH_ABORT_NOT_WIRED = "pipeline_not_wired"
_MORPH_ABORT_MISSING_KEYS = "missing_keys"
_MORPH_ABORT_FAILED_CREDENTIAL = "failed_credential"
_MORPH_ABORT_OPEN_FAILED = "open_failed"

# Failure reasons that mean the OpenAI credential itself is the
# problem. Substituting OpenAI STT/TTS into the chained fallback for
# these is handing the pipeline the exact key that just failed.
_OPENAI_CREDENTIAL_FAILURES = frozenset({
    "openai_realtime_quota",
    "openai_realtime_auth",
})


def _resolve_provider_key(provider_id: str, env_key: str) -> str:
    """Resolve an API key for *provider_id* for the voice hot path.

    Consults :func:`security.vault_keys.get_active_key` first so a
    labeled key stored via ``feral key add --provider <p> --label <l>
    --set-active`` (which does NOT also write the legacy
    default-namespace entry) reaches the realtime/chained voice path —
    closing the "one OpenAI key works for chat AND voice AND video"
    mental-model gap that the chat path already honours via
    ``agents.llm_provider``.

    Then falls back to ``os.getenv(env_key)`` for voice-only providers
    that aren't registered in ``vault_keys._PROVIDER_ENV_KEYS``
    (``deepgram``, ``elevenlabs``, ``cartesia``, ``openai_whisper``,
    ``groq_whisper``) so env-only setups continue to work unchanged.
    Strictly additive — never raises.
    """
    try:
        from security.vault_keys import get_active_key
        key = get_active_key(provider_id) or ""
    except Exception:
        key = ""
    if key:
        return key
    if env_key:
        return os.getenv(env_key, "") or ""
    return ""


# Which vendor credential each chained provider needs. ``("", "")``
# means "runs on this machine, needs nothing".
_STT_CREDENTIALS: dict[str, tuple[str, str]] = {
    "deepgram": ("deepgram", "DEEPGRAM_API_KEY"),
    "openai_whisper": ("openai", "OPENAI_API_KEY"),
    "groq_whisper": ("groq", "GROQ_API_KEY"),
    "whispercpp": ("", ""),
    "faster_whisper": ("", ""),
}

_TTS_CREDENTIALS: dict[str, tuple[str, str]] = {
    "openai": ("openai", "OPENAI_API_KEY"),
    "openai_tts": ("openai", "OPENAI_API_KEY"),
    "elevenlabs": ("elevenlabs", "ELEVENLABS_API_KEY"),
    "cartesia": ("cartesia", "CARTESIA_API_KEY"),
    "macos_say": ("", ""),
    "piper": ("", ""),
}


def _provider_credential(kind: str, name: str) -> tuple[str, str]:
    """``(vault_provider_id, env_var)`` for a chained provider.

    Unknown names return ``("", "")`` - "no credential required" -
    rather than the previous behaviour, which fell through to
    ``("deepgram", "DEEPGRAM_API_KEY")`` for STT and
    ``("elevenlabs", "ELEVENLABS_API_KEY")`` for TTS. That default was
    harmless while every provider was a cloud vendor we shipped, and
    actively wrong the moment a local engine existed: selecting
    ``whispercpp`` aborted the session demanding a Deepgram key that
    nothing in the session would ever use, and the operator got a
    missing-credential banner for a provider they had not chosen.

    A genuinely unknown name still fails, just later and more
    honestly - at ``get_stt_provider``, with "Unknown STT provider",
    which is what actually went wrong.
    """
    table = _STT_CREDENTIALS if kind == "stt" else _TTS_CREDENTIALS
    return table.get(name, ("", ""))


def _is_local_provider(kind: str, name: str) -> bool:
    """True when *name* is an engine that runs on this machine."""
    try:
        from voice.provider_registry import requires_credential

        return not requires_credential(kind, name)
    except Exception:
        return not _provider_credential(kind, name)[1]


def _settings_realtime_model() -> str:
    """Resolve the operator-configured OpenAI Realtime model.

    Reads ``audio.realtime_model`` from the merged settings tree
    (config/loader.py default: ``"gpt-realtime"``). When the settings
    layer is unavailable (e.g. in narrow unit tests) or the key is
    blank, fall back to :data:`voice.realtime_proxy.DEFAULT_MODEL` so
    the proxy still opens a session with a valid GA model id (Lane U2).
    """
    model = ""
    try:
        from config.loader import load_settings
        value = (load_settings().get("audio", {}) or {}).get("realtime_model")
        if isinstance(value, str) and value.strip():
            model = value.strip()
    except Exception:
        logger.debug("settings.audio.realtime_model lookup failed", exc_info=True)
    if not model:
        try:
            from voice.realtime_proxy import DEFAULT_MODEL
            model = DEFAULT_MODEL
        except Exception:
            model = "gpt-realtime"
    # Auto-correct retired / account-gated OpenAI preview snapshots that a
    # stale settings.json may still pin (e.g. gpt-4o-realtime-preview-2025-06-03),
    # which OpenAI rejects with `4004 model_not_found`. Map the whole legacy
    # preview family to the GA rolling alias so voice works without the
    # operator having to hand-edit settings.
    if model.startswith("gpt-4o-realtime"):
        logger.warning(
            "audio.realtime_model=%r is a retired OpenAI preview model; "
            "using 'gpt-realtime' instead. Clear it in Settings → Voice to "
            "silence this.",
            model,
        )
        return "gpt-realtime"
    return model


class VoiceRouter:
    """
    Central audio routing layer.  Both web clients and daemon nodes can
    declare voice capabilities via a `voice_config` message.  The router
    uses that declaration to choose between the Gemini Realtime path,
    the OpenAI Realtime path, and the classic Whisper+TTS path.
    """

    def __init__(
        self,
        *,
        realtime_proxy=None,
        audio_pipeline=None,
        orchestrator=None,
        memory=None,
        perception=None,
        wake_word_detector=None,
        send_to_session: Callable[[str, Any], Awaitable[None]] | None = None,
        send_to_node: Callable[[str, dict], Awaitable[None]] | None = None,
    ):
        self._realtime = realtime_proxy
        self._gemini: Any = None
        self._audio = audio_pipeline
        self._orchestrator = orchestrator
        self._memory = memory
        self._perception = perception
        self._wake_word = wake_word_detector
        self._send_to_session = send_to_session
        self._send_to_node = send_to_node

        self._node_voice_config: dict[str, dict] = {}
        self._node_session_map: dict[str, str] = {}
        self._session_voice_mode: dict[str, str] = {}

        # node_id -> HUP node_type ("phone", "glasses", "desktop", ...).
        # Drives the surface-aware default provider chain; see
        # ``realtime_chain_for_surface``. Empty means "not known", and
        # the router never guesses a value into it.
        self._node_surface_map: dict[str, str] = {}
        self._capability_registry: Any = None

        # session_id -> the last fault the CLIENT reported (microphone
        # permission, dead capture device). The brain cannot probe a
        # microphone, so this is the only evidence that exists for the
        # whole class of "voice is broken and no provider is at fault".
        self._client_faults: dict[str, dict] = {}

        # Session-scoped mute ledger. See ``set_session_muted``. Held
        # separately from ``_session_voice_mode`` because mute must
        # outlive a transport drop: a voice session that came back
        # unmuted after a dropped socket is precisely the failure the
        # control exists to prevent.
        self._session_muted: dict[str, dict] = {}

        # Session-scoped degraded-state ledger. Populated by
        # ``handle_realtime_failure`` when OpenAI Realtime dies on a
        # given session — the router uses this to route ALL subsequent
        # outbound assistant text for that session through the whisper
        # TTS fallback (mp3 tts_chunk frames) instead of the dead
        # realtime PCM path. Cleared on ``stop_session_voice``.
        self._session_degraded: dict[str, dict] = {}

    def set_gemini_proxy(self, proxy) -> None:
        """Inject GeminiRealtimeProxy after construction (set from api/state.py)."""
        self._gemini = proxy

    def register_voice_config(self, node_id: str, config: dict):
        """Store a node's voice capabilities.  Called when a `voice_config` message arrives."""
        merged = {**self._node_voice_config.get(node_id, {}), **(config or {})}
        self._node_voice_config[node_id] = merged
        logger.info(f"Voice config for {node_id}: {merged}")

    def set_session_voice_mode(self, session_id: str, mode: str):
        """Set the voice mode for a web client session (realtime | whisper | disabled)."""
        self._session_voice_mode[session_id] = mode
        logger.info(f"Session {session_id[:8]} voice mode: {mode}")

    def bind_node_to_session(self, node_id: str, session_id: str):
        self._node_session_map[node_id] = session_id

    # ------------------------------------------------------------------
    # Provider selection helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalise_chain(values) -> list[str]:
        """Map operator-written provider names onto router names.

        Drops anything unrecognised rather than walking it: an unknown
        entry in the chain would otherwise be indistinguishable from a
        provider that is merely down, and the router would report
        "tried everything" having skipped a typo in silence.
        """
        out: list[str] = []
        if isinstance(values, str):
            values = values.replace(";", ",").split(",")
        for raw in list(values or []):
            name = _REALTIME_ALIASES.get(str(raw).strip().lower(), "")
            if name and name not in out:
                out.append(name)
        return out

    @classmethod
    def _explicit_realtime_pick(cls) -> list[str]:
        """The operator's EXPLICIT realtime pick, in order, or ``[]``.

        ``FERAL_VOICE_PROVIDER`` wins so an env override still beats
        persisted settings, and it now accepts a comma-separated list so
        an override can express a chain too. Failing that,
        ``audio.realtime_primary`` (the setup wizard's scalar pick).

        Deliberately does NOT consult ``audio.realtime_providers``: that
        key ships with a non-empty default, so folding it in here would
        make ``feral setup`` and ``feral doctor`` report a provider the
        operator never chose.
        """
        env_provider = os.getenv(_ENV_VOICE_PROVIDER, "").strip()
        if env_provider:
            return cls._normalise_chain(env_provider)
        primary = cls._load_audio_settings().get("realtime_primary")
        return cls._normalise_chain([primary] if primary else [])

    @classmethod
    def _preferred_realtime_provider(cls) -> str:
        """Legacy single-provider accessor: 'gemini', 'openai', or ''.

        Kept because ``audio.realtime_primary`` is the wizard's pick and
        several surfaces still ask "which one did they choose?" as a
        scalar question. Routing no longer goes through here; it walks
        :meth:`realtime_chain_for_surface`.
        """
        pick = cls._explicit_realtime_pick()
        return pick[0] if pick else ""

    @classmethod
    def _configured_realtime_chain(cls) -> list[str]:
        """Operator-configured realtime order, best link first.

        The explicit pick leads, then ``audio.realtime_providers`` -- the
        ordered list the WebUI Settings page writes. That key used to be
        read by nothing at all (it was allowlisted as dead in
        ``tests/test_settings_keys_have_readers.py``), which is why
        reordering the Realtime group in Settings changed nothing: the
        router resolved a single provider and, when it was down, went
        straight to the batch whisper path instead of to the next entry.
        """
        chain = cls._explicit_realtime_pick()
        audio_cfg = cls._load_audio_settings()
        for name in cls._normalise_chain(audio_cfg.get("realtime_providers")):
            if name not in chain:
                chain.append(name)
        return chain

    @classmethod
    def realtime_chain_for_surface(cls, surface: str = "") -> list[str]:
        """Ordered providers to try for ``surface``, terminating at chained.

        The last link is always :data:`CHAIN_TERMINAL`. That is the
        point of the chain: it ends at a pipeline that can still work
        (local engines included), not at a dead session.

        ``surface`` is a HUP ``node_type``. An unrecognised or missing
        surface keeps the realtime-first default -- guessing "desktop"
        for a surface we could not identify would route a phone through
        the slow path on the strength of a guess.

        An explicit provider pick (``FERAL_VOICE_PROVIDER`` or
        ``audio.realtime_primary``) overrides the surface policy: those
        keys mean "use this provider", which is an answer to a
        different question from "should realtime be tried before
        local". ``audio.realtime_providers`` only ORDERS the realtime
        segment and so never suppresses the policy.
        """
        chain = cls._configured_realtime_chain() or list(DEFAULT_REALTIME_ORDER)
        surface = (surface or "").strip().lower()
        if surface in SURFACE_LOCAL_FIRST and not cls._explicit_realtime_pick():
            # Local leads. Realtime stays in the chain behind it so a
            # desktop with no local engines installed still gets audio.
            return [CHAIN_TERMINAL] + chain + [CHAIN_TERMINAL]
        return chain + [CHAIN_TERMINAL]

    def _walk_chain(self, chain: list[str]) -> str:
        """First link in ``chain`` that is actually up right now.

        Returns ``"whisper"`` when nothing in the chain can serve --
        including the terminal, when no chained pipeline is wired. That
        is the honest end of the chain, not a silent success.
        """
        for name in chain:
            if name == CHAIN_TERMINAL:
                if getattr(self, "_chained", None) is not None:
                    return CHAIN_TERMINAL
                continue
            if name == "gemini" and self._gemini and self._gemini.available:
                return "gemini"
            if name == "openai" and self._realtime and self._realtime.available:
                return "openai"
        return "whisper"

    # -- Surface resolution -------------------------------------------

    def set_capability_registry(self, registry) -> None:
        """Inject the live node catalogue so the router can see surfaces.

        ``memory/capability_registry.CapabilityRegistry`` already records
        every connected node's HUP ``node_type`` at ``node_register``
        time. The router never had a reference to it, which is why
        surface-aware routing was impossible before: ``node_type`` is
        simply not present in any frame the router handles.
        """
        self._capability_registry = registry

    def set_node_surface(self, node_id: str, node_type: str) -> None:
        """Record a node's HUP ``node_type`` for provider-chain policy."""
        if not node_id:
            return
        surface = (node_type or "").strip().lower()
        if surface:
            self._node_surface_map[node_id] = surface

    def _node_surface(self, node_id: str) -> str:
        """This node's surface, or ``""`` when genuinely unknown.

        Resolution order, most explicit first:

          1. :meth:`set_node_surface` -- the brain told us.
          2. ``node_type`` inside the node's own ``voice_config``, for
             clients that declare it there.
          3. The capability registry, when one is injected.

        Never guesses. An empty string means "not known", and
        :meth:`realtime_chain_for_surface` treats that as the safe
        realtime-first default rather than inventing a device class.
        """
        if not node_id:
            return ""
        recorded = self._node_surface_map.get(node_id, "")
        if recorded:
            return recorded
        declared = (self._node_voice_config.get(node_id, {}) or {}).get("node_type")
        if isinstance(declared, str) and declared.strip():
            return declared.strip().lower()
        registry = getattr(self, "_capability_registry", None)
        if registry is not None:
            try:
                for entry in registry.snapshot_nodes():
                    if entry.get("node_id") == node_id:
                        return str(entry.get("node_type") or "").strip().lower()
            except Exception:
                logger.debug("capability registry surface lookup failed", exc_info=True)
        return ""

    def _session_surface(self, session_id: str) -> str:
        """Surface of whichever node is bound to ``session_id``.

        A web session with no node bound resolves to ``""`` (unknown),
        which keeps the realtime-first default.
        """
        if not session_id:
            return ""
        for node_id, sid in self._node_session_map.items():
            if sid != session_id:
                continue
            surface = self._node_surface(node_id)
            if surface:
                return surface
        return ""

    def _resolve_provider(self, node_id: str) -> str:
        """Return 'gemini', 'openai', 'chained', or 'whisper' for a given node."""
        cfg = self._node_voice_config.get(node_id, {})
        mode = (cfg.get("mode") or "").lower()

        if mode == "whisper":
            return "whisper"
        # --- Subagent B: chained mode resolution ---
        if mode == "chained":
            return "chained"
        # --- end Subagent B ---
        if mode == "gemini_live":
            if self._gemini and self._gemini.available:
                return "gemini"
        if mode == "openai_realtime":
            if self._realtime and self._realtime.available:
                return "openai"

        explicit = cfg.get("voice_provider", "").lower()
        if explicit == "gemini":
            if self._gemini and self._gemini.available:
                return "gemini"
        if explicit == "openai":
            if self._realtime and self._realtime.available:
                return "openai"

        # Walk the operator's chain when this node declared realtime
        # support, or when the operator made an explicit pick (the old
        # code honoured a settings pick of Gemini here regardless of
        # ``supports_realtime``; this is the same rule applied to both
        # providers instead of only one).
        if cfg.get("supports_realtime") is True or self._explicit_realtime_pick():
            return self._walk_chain(
                self.realtime_chain_for_surface(self._node_surface(node_id))
            )

        return "whisper"

    def _resolve_session_provider(self, session_id: str) -> str:
        """Return 'gemini', 'openai', 'chained', or 'whisper' for a web-client session."""
        mode = self._session_voice_mode.get(session_id, "")
        # --- Subagent B: chained mode session resolution ---
        if mode == "chained":
            return "chained"
        # --- end Subagent B ---
        if mode != "realtime":
            return "whisper"

        return self._walk_chain(
            self.realtime_chain_for_surface(self._session_surface(session_id))
        )

    # ------------------------------------------------------------------
    # Mute
    # ------------------------------------------------------------------
    #
    # Mute is an INPUT control and it is enforced in two places on
    # purpose.
    #
    # The client stops capture (``BrowserNode.setMicMuted`` disables the
    # MediaStreamTrack). That is the only place that can promise the
    # audio never leaves the device, which is the promise a mute button
    # makes. The brain drops ingress as well, because client-side
    # enforcement alone is one bug away from failing open, a session can
    # have more than one surface bound to it, and a stale worklet
    # surviving a reconnect would otherwise stream straight past a mute
    # the user thinks is on.
    #
    # Mute does NOT stop synthesis coming back. Cutting the assistant
    # off is what ``voice_interrupt`` / barge-in already does; a user
    # who mutes mid-answer to stop a background conversation being
    # transcribed still wants to hear the answer they asked for.
    #
    # Mute survives a reconnect: the ledger is session-scoped and node
    # teardown does not touch it. It fails safe toward muted, and only
    # an explicit unmute (or an explicit ``stop_session_voice``) clears
    # it. Coming back unmuted after a dropped socket is exactly the
    # failure the control exists to prevent.

    def is_session_muted(self, session_id: str) -> bool:
        """True when ``session_id``'s microphone is muted."""
        return bool((self._session_muted.get(session_id) or {}).get("muted"))

    def session_mute_detail(self, session_id: str) -> dict:
        """``{"muted", "source", "since"}`` for the mute banner / logs."""
        return dict(self._session_muted.get(session_id) or {"muted": False})

    def _node_declares_mute(self, node_id: str) -> bool:
        """True when a node's own ``voice_config`` says it is muted.

        ``voice_config`` is a wire path the phone and browser nodes
        already have, so a client can gate brain-side ingress through it
        without a dedicated ``voice_mute`` handler existing on the
        server. Treated as advisory input to the same gate, never as a
        way to UNMUTE a session the user muted.
        """
        return bool((self._node_voice_config.get(node_id) or {}).get("muted"))

    async def set_session_muted(
        self,
        session_id: str,
        muted: bool,
        *,
        source: str = "client",
    ) -> bool:
        """Set the mute state for ``session_id``. Returns True if changed.

        Publishes ``voice_status`` on every real change so the UI's mute
        indicator is a mirror of brain state rather than an independent
        local boolean that can disagree with what the microphone is
        actually doing.
        """
        muted = bool(muted)
        if self.is_session_muted(session_id) == muted:
            return False
        if muted:
            self._session_muted[session_id] = {
                "muted": True,
                "source": source or "client",
                "since": time.time(),
            }
        else:
            self._session_muted.pop(session_id, None)
        logger.info(
            "Voice %s session=%s source=%s",
            "muted" if muted else "unmuted", session_id[:8], source,
        )
        await self._emit_voice_status(session_id, self._current_status_meta(session_id))
        return True

    def _current_status_meta(self, session_id: str) -> dict:
        """The session's current voice_status, mute change or not.

        A mute toggle must not clear a degraded/unavailable banner, and
        must not invent an ``available`` one either, so the frame it
        publishes reuses whatever the session's live state already is.
        """
        meta = dict(self._session_degraded.get(session_id) or {})
        if not meta:
            meta = {
                "state": "available",
                "reason": "",
                "provider": "",
                "fallback_provider": "",
                "detail": "",
            }
        return meta

    def should_use_realtime(self, node_id: str) -> bool:
        """Decide whether a node should use any realtime path (OpenAI or Gemini)."""
        return self._resolve_provider(node_id) in ("openai", "gemini")

    def session_uses_realtime(self, session_id: str) -> bool:
        """Check if a web client session should use a realtime path."""
        return self._resolve_session_provider(session_id) in ("openai", "gemini")

    # ------------------------------------------------------------------
    # Audio from daemon / phone nodes
    # ------------------------------------------------------------------

    async def handle_audio_from_node(
        self,
        node_id: str,
        session_id: str,
        audio_b64: str,
        chunk_index: int = 0,
        is_final: bool = False,
        encoding: str = "pcm16",
        sample_rate: int = 24000,
    ):
        # Wake-word gate is only appropriate for always-listening
        # desktop/background mics. When a phone user explicitly tapped
        # "Start voice" we already have voice_session_start on record
        # and MUST NOT drop audio waiting for "hey feral" — that's
        # what stalled voice in the live test even though the mic was
        # streaming PCM correctly to the brain.
        #
        # Skip the gate if there's an active browser_node voice session
        # bound to this node (the phone-as-peer fast path).
        # Mute gate, ahead of everything including the wake word. A
        # muted microphone must not reach the wake-word detector, the
        # STT buffer, or a cloud realtime socket. See the Mute section
        # above for why this is enforced here as well as at the client.
        if self.is_session_muted(session_id) or self._node_declares_mute(node_id):
            return

        cfg = self._node_voice_config.get(node_id, {})
        skip_wake = bool(cfg.get("skip_wake"))
        if not skip_wake:
            try:
                if node_id and session_id in self._session_voice_mode:
                    skip_wake = True
                elif node_id and node_id.startswith("browser-node-"):
                    skip_wake = True
            except Exception:
                skip_wake = False

        if self._wake_word and self._wake_word.enabled and not skip_wake:
            import base64
            pcm_bytes = base64.b64decode(audio_b64)
            should_process = await self._wake_word.process_frame(session_id, pcm_bytes)
            if not should_process:
                return

        provider = self._resolve_provider(node_id)

        if provider == "gemini":
            await self._handle_gemini_node(node_id, session_id, audio_b64)
            return

        if provider == "openai":
            rs = await self._live_realtime_session(node_id)
            if not rs:
                rs = await self._realtime.start_session(
                    session_id,
                    node_id,
                    model=cfg.get("model") or _settings_realtime_model(),
                    voice=cfg.get("voice", "marin"),
                    input_sample_rate=int(cfg.get("sample_rate") or sample_rate or 24000),
                    language_hint=cfg.get("language_hint", ""),
                )
            if rs and rs.connected:
                await rs.send_audio(audio_b64)
            return

        # --- Subagent B: chained pipeline audio routing ---
        if provider == "chained":
            await self.handle_chained_audio(
                session_id=session_id,
                audio_b64=audio_b64,
                chunk_index=chunk_index,
                is_final=is_final,
            )
            return
        # --- end Subagent B ---

        await self._handle_whisper_path(
            session_id=session_id,
            audio_b64=audio_b64,
            chunk_index=chunk_index,
            is_final=is_final,
            encoding=encoding,
            sample_rate=sample_rate,
            source_node_id=node_id,
        )

    async def _live_realtime_session(self, node_id: str):
        """Return ``node_id``'s realtime session, evicting it if dead.

        Zombie-session fix (voice-collapse audit, 2026-07). Both audio
        entry points used to do a bare ``get_session`` and, because a
        dead session is still *present* in ``RealtimeProxy._sessions``,
        skipped the re-open and then dropped every chunk on the
        ``rs.connected`` gate below. Symptom: the user talks, the orb
        says "listening", and not one byte reaches OpenAI — forever,
        because nothing ever removes the corpse.

        A disconnected handle is now torn down (closing the leaked
        OpenAI socket) and reported as absent, so the caller opens a
        fresh session with the very chunk that found the zombie.
        """
        if not self._realtime:
            return None
        await self._realtime.evict_dead_session(node_id)
        return self._realtime.get_session(node_id)

    # ------------------------------------------------------------------
    # Audio from web clients
    # ------------------------------------------------------------------

    async def handle_audio_from_client(
        self,
        session_id: str,
        audio_b64: str,
        chunk_index: int = 0,
        is_final: bool = False,
        encoding: str = "pcm16",
        sample_rate: int = 24000,
    ):
        # Mute gate. Same rule as the node path.
        if self.is_session_muted(session_id):
            return

        provider = self._resolve_session_provider(session_id)
        client_node = f"webclient_{session_id[:8]}"

        if self._node_declares_mute(client_node):
            return

        if provider == "gemini":
            await self._handle_gemini_client(session_id, client_node, audio_b64)
            return

        if provider == "openai":
            rs = await self._live_realtime_session(client_node)
            if not rs:
                rs = await self._realtime.start_session(
                    session_id,
                    client_node,
                    model=_settings_realtime_model(),
                    input_sample_rate=sample_rate or 24000,
                )
            if rs and rs.connected:
                await rs.send_audio(audio_b64)
            return

        # --- Subagent B: chained pipeline audio routing (client) ---
        if provider == "chained":
            await self.handle_chained_audio(
                session_id=session_id,
                audio_b64=audio_b64,
                chunk_index=chunk_index,
                is_final=is_final,
            )
            return
        # --- end Subagent B ---

        await self._handle_whisper_path(
            session_id=session_id,
            audio_b64=audio_b64,
            chunk_index=chunk_index,
            is_final=is_final,
            encoding=encoding,
            sample_rate=sample_rate,
        )

    # ------------------------------------------------------------------
    # Gemini relay helpers
    # ------------------------------------------------------------------

    async def _handle_gemini_node(self, node_id: str, session_id: str, audio_b64: str):
        gs = self._gemini.get_session(node_id)
        if not gs:
            gs = await self._gemini.start_session(session_id, node_id)
        if gs and gs.connected:
            await gs.send_audio(audio_b64)

    async def _handle_gemini_client(self, session_id: str, client_node: str, audio_b64: str):
        gs = self._gemini.get_session(client_node)
        if not gs:
            gs = await self._gemini.start_session(session_id, client_node)
        if gs and gs.connected:
            await gs.send_audio(audio_b64)

    async def handle_audio_for_gemini(
        self,
        session_id: str,
        audio_b64: str,
        *,
        node_id: str = "",
    ):
        """
        Public entry-point for callers that know they want the Gemini path.
        Resolves or creates a session, then relays audio.
        """
        if not self._gemini or not self._gemini.available:
            logger.warning("handle_audio_for_gemini called but Gemini proxy unavailable")
            return
        lookup = node_id or session_id
        gs = self._gemini.get_session(lookup)
        if not gs:
            _node = node_id or f"webclient_{session_id[:8]}"
            gs = await self._gemini.start_session(session_id, _node)
        if gs and gs.connected:
            await gs.send_audio(audio_b64)

    # ------------------------------------------------------------------
    # Whisper STT → Orchestrator → TTS  (unchanged)
    # ------------------------------------------------------------------

    async def _handle_whisper_path(
        self,
        session_id: str,
        audio_b64: str,
        chunk_index: int,
        is_final: bool,
        encoding: str,
        sample_rate: int,
        source_node_id: str = "",
    ):
        """Classic STT → Orchestrator → TTS flow."""
        if not self._audio:
            return

        transcript = await self._audio.process_audio_chunk(
            session_id=session_id,
            chunk_b64=audio_b64,
            chunk_index=chunk_index,
            is_final=is_final,
            encoding=encoding,
            sample_rate=sample_rate,
        )

        if not transcript:
            return

        from models.protocol import FeralMessage, TranscriptPayload
        from voice.transcript_order import TRANSCRIPT_ORDER

        # ``transcript`` is the output of ``process_audio_chunk`` — it is
        # the USER's speech, unambiguously. The web branch used to omit
        # ``role`` entirely and ``TranscriptPayload`` defaults it to
        # ``"assistant"``, so the web client left-aligned the user's own
        # words as if the assistant had said them, while the node branch
        # three lines below tagged the same text ``"user"`` correctly.
        # Both branches now build one payload so they cannot drift again.
        #
        # This path has no provider item ids (whisper is batch STT with
        # no conversation-item concept), so ordering rests on the
        # brain-assigned ``seq``.
        payload = TranscriptPayload(
            text=transcript,
            role="user",
            is_partial=False,
            seq=TRANSCRIPT_ORDER.next_seq(session_id),
        ).model_dump()

        if self._send_to_session:
            msg = FeralMessage(
                session_id=session_id, hop="brain", type="transcript",
                payload=payload,
            )
            await self._send_to_session(session_id, msg)

        if source_node_id and self._send_to_node:
            await self._send_to_node(source_node_id, {
                "type": "transcript",
                "payload": payload,
            })

        if self._memory:
            self._memory.working_push(session_id, {
                "role": "user", "text": transcript, "source": "voice",
            })

        if self._perception:
            self._perception.update_audio_context(session_id, transcript=transcript)

        if self._orchestrator:
            await self._orchestrator.handle_command_stream(
                session_id=session_id,
                text=transcript,
                context={"source": "voice", "node_id": source_node_id} if source_node_id else {"source": "voice"},
            )

            tts_text = self._get_last_assistant_text(session_id)
            if tts_text and self._audio:
                chunks = await self._audio.synthesize_speech(tts_text)
                if chunks:
                    for chunk in chunks:
                        tts_msg = FeralMessage(
                            session_id=session_id, hop="brain", type="tts_chunk",
                            payload=chunk,
                        )
                        if self._send_to_session:
                            await self._send_to_session(session_id, tts_msg)
                        if source_node_id and self._send_to_node:
                            await self._send_to_node(source_node_id, {
                                "type": "tts_chunk",
                                "payload": chunk,
                            })

    def _get_last_assistant_text(self, session_id: str) -> str:
        """Pull the latest assistant response from working memory for TTS."""
        if not self._memory:
            return ""
        history = self._memory.working_get(session_id) or []
        for msg in reversed(history):
            if msg.get("role") == "assistant":
                return msg.get("text", "")[:1000]
        return ""

    # ------------------------------------------------------------------
    # Text routing
    # ------------------------------------------------------------------

    async def handle_text_from_node(self, node_id: str, session_id: str, text: str):
        """Route a text command from a node — if realtime is active, send as text there."""
        provider = self._resolve_provider(node_id)

        if provider == "gemini":
            gs = self._gemini.get_session(node_id) if self._gemini else None
            if gs and gs.connected:
                await gs.send_text(text)
                return

        if provider == "openai":
            rs = self._realtime.get_session(node_id)
            if rs and rs.connected:
                await rs.send_text(text)
                return

        if self._orchestrator:
            await self._orchestrator.handle_command_stream(
                session_id=session_id,
                text=text,
                context={"source": "node_text", "node_id": node_id},
            )

    async def handle_text_from_client_voice(self, session_id: str, text: str):
        """Route a text message into an active realtime voice session."""
        provider = self._resolve_session_provider(session_id)
        client_node = f"webclient_{session_id[:8]}"

        if provider == "gemini":
            gs = self._gemini.get_session(client_node) if self._gemini else None
            if gs and gs.connected:
                await gs.send_text(text)
                return

        if provider == "openai":
            rs = self._realtime.get_session(client_node) if self._realtime else None
            if rs and rs.connected:
                await rs.send_text(text)
                return

        if self._orchestrator:
            await self._orchestrator.handle_command_stream(
                session_id=session_id, text=text, context={"source": "voice_text"},
            )

    # ------------------------------------------------------------------
    # Fallback / status coordination (v2026.5.31)
    # ------------------------------------------------------------------

    def is_session_degraded(self, session_id: str) -> bool:
        """True when the realtime path failed for ``session_id`` and the
        router has flipped this session to the whisper TTS fallback.
        Consumers (e.g. ``response_delivery.send_text``) check this so
        every assistant turn after the failure event continues to
        produce audio via ``synthesize_assistant_speech``.
        """
        return session_id in self._session_degraded

    def session_degraded_detail(self, session_id: str) -> dict:
        """Return the failure metadata (reason, provider, detail) the
        client used to render the status banner — mirrors the payload
        published via ``voice_status`` so polling clients can recover."""
        return dict(self._session_degraded.get(session_id) or {})

    async def handle_realtime_failure(
        self,
        session_id: str,
        *,
        reason: str,
        detail: str = "",
        provider: str = "openai",
    ) -> None:
        """Recover voice for ``session_id`` after a realtime provider
        failure.

        Two recovery modes, selected by ``audio.fallback_mode``:

        ``"chained"`` (S4 acceptance, default) — morph the live
        session in place onto the chained STT→LLM→TTS pipeline so
        the call keeps going on Deepgram + ElevenLabs. Steps:
          1. Skip if the session already morphed (idempotent).
          2. ``stop_session`` on the dead realtime/gemini proxy.
          3. Retarget every bound node's voice config to
             ``mode="chained"`` so subsequent ``audio_chunk`` frames
             land on the chained pipeline.
          4. ``open_chained_session`` with the providers the operator
             configured under ``voice.chained`` (phone Settings panel),
             falling back to ``audio.chained_fallback`` (setup wizard
             and shipped defaults). See ``_resolve_chained_config``.
          5. Emit ``voice_status state=degraded
             fallback_provider="chained"`` so the UI banner switches.
          6. Do NOT populate ``_session_degraded`` — the chained
             pipeline owns TTS via its own ``tts_chunk`` frames, and
             ``response_delivery.send_text`` skips the whisper
             synthesiser when ``_session_voice_mode[session_id] ==
             "chained"`` (prevents double audio).

        ``"whisper"`` (legacy v2026.5.31) — keep streaming chunked
        TTS from the dead session's residual text via
        ``AudioPipeline.synthesize_speech`` (mp3 ``tts_chunk``
        frames). Used when ``fallback_mode`` is explicitly set to
        ``"whisper"`` OR when chained keys (``DEEPGRAM_API_KEY``,
        ``ELEVENLABS_API_KEY``) are missing — the user still gets
        audible feedback rather than going silent.

        Both modes are idempotent — calling this twice on the same
        session emits one voice_status / opens one chained session.
        """
        if self._session_voice_mode.get(session_id) == "chained":
            return  # already morphed
        if session_id in self._session_degraded:
            return  # already on the legacy whisper path

        audio_cfg = self._load_audio_settings()
        # Default MUST match ``config/loader.py`` (``"chained"``).
        # It said ``"whisper"`` here, so every test that stubs
        # ``load_settings`` without an explicit ``fallback_mode`` — and
        # any operator whose settings.json predates the key — silently
        # exercised the legacy branch. That divergence is why the
        # chained pipeline's dead-end bugs went unnoticed for so long.
        fallback_mode = str(audio_cfg.get("fallback_mode") or "chained").lower()

        morphed = False
        abort = ""
        if fallback_mode == "chained":
            morphed, abort = await self._try_chained_morph(
                session_id,
                audio_cfg=audio_cfg,
                reason=reason,
                detail=detail,
                provider=provider,
            )
        if morphed:
            return

        if abort == _MORPH_ABORT_FAILED_CREDENTIAL:
            # The only chained pair we could have built runs on the
            # OpenAI key that just failed, and so does the whisper
            # fallback (`AudioPipeline.synthesize_speech` -> OpenAI
            # /audio/speech). Claiming "degraded, falling back to
            # whisper" here would be a banner promising audio that
            # cannot arrive. Say unavailable and let the user go fix
            # the key.
            await self.emit_unavailable(session_id, reason=reason, detail=detail)
            return

        fallback_provider = self._pick_fallback_provider()

        # Privacy guard. ``_pick_fallback_provider`` returns "whisper"
        # -- the OpenAI ``/audio/speech`` endpoint -- whenever an OpenAI
        # key exists anywhere in the vault. For an operator whose
        # chained pair is local engines, taking it ships their audio to
        # a vendor and flies a "degraded, using fallback TTS" banner
        # over the top, which is the failure mode they chose local
        # voice to avoid, presented as a recovery. Refuse it and say
        # why.
        if fallback_provider and self._operator_chose_local_voice():
            if not _is_local_provider("tts", fallback_provider):
                from voice import diagnostics

                logger.warning(
                    "Voice fallback for %s refused: operator chose local "
                    "voice and the only fallback (%s) is a cloud service.",
                    session_id[:8], fallback_provider,
                )
                meta = {
                    "state": "unavailable",
                    "reason": diagnostics.CAUSE_PRIVACY_DOWNGRADE,
                    "provider": provider,
                    "fallback_provider": "",
                    "detail": (
                        f"{reason}: {detail}" if detail else reason
                    )[:400],
                    "cause": diagnostics.CAUSE_PRIVACY_DOWNGRADE,
                    "summary": (
                        f"{provider} failed ({reason}), and the only "
                        f"fallback available is {fallback_provider}, a cloud "
                        f"service. You chose local voice, so FERAL stopped "
                        f"rather than send your audio off this machine."
                    ),
                    "recommendation": diagnostics.recommendation_for(
                        diagnostics.CAUSE_PRIVACY_DOWNGRADE,
                    ),
                    "privacy_downgrade": True,
                }
                self._session_degraded[session_id] = meta
                await self._emit_voice_status(session_id, meta)
                return

        meta = {
            "state": "degraded" if fallback_provider else "unavailable",
            "reason": reason,
            "provider": provider,
            "fallback_provider": fallback_provider,
            "detail": detail,
        }
        self._session_degraded[session_id] = meta
        logger.warning(
            "Voice degraded session=%s reason=%s provider=%s fallback=%s",
            session_id[:8], reason, provider, fallback_provider or "(none)",
        )
        await self._emit_voice_status(session_id, meta)

    @staticmethod
    def _load_audio_settings() -> dict:
        """Return the ``audio`` settings block, tolerant of missing
        ``config.loader`` (narrow unit tests).
        """
        try:
            from config.loader import load_settings
            cfg = load_settings() or {}
            return dict((cfg.get("audio") or {}))
        except Exception:
            return {}

    @staticmethod
    def _load_voice_settings() -> dict:
        """Return the ``voice`` settings block, tolerant of missing
        ``config.loader`` (narrow unit tests).

        This is the block the phone Settings panel writes
        (``feral-client-v2/src/pages/phone/SettingsPanel.jsx``) and the
        one ``api/server.py`` already reads ``voice.mode`` from.
        """
        try:
            from config.loader import load_settings
            cfg = load_settings() or {}
            return dict((cfg.get("voice") or {}))
        except Exception:
            return {}

    def _resolve_chained_config(self, audio_cfg: dict | None = None) -> dict:
        """Resolve the chained STT/TTS pick an operator actually made.

        Two settings blocks can carry that pick and both are real:

        * ``voice.chained.*`` is what the phone Settings panel writes.
          It is the choice a user makes in the UI, so it wins.
        * ``audio.chained_fallback.*`` is what the setup wizard writes
          (``cli/setup/steps/voice_preflight.py``) and what
          ``config/loader.py`` ships defaults for. It is the headless
          operator pick and applies whenever the UI block is silent.

        Before this fix nothing anywhere read ``voice.chained.*``, so a
        user who picked Groq Whisper plus ElevenLabs in the phone UI
        still got Deepgram plus ElevenLabs on every chained session.
        Keys absent from both blocks fall through to the shipped
        provider defaults (empty string means "let the provider decide",
        which keeps each provider's own default model in play instead of
        forcing Deepgram's ``nova-3`` onto Whisper).

        ``audio_cfg`` may be passed by callers that already loaded the
        ``audio`` block so we do not read settings twice.
        """
        voice_chained = dict((self._load_voice_settings().get("chained") or {}))
        if audio_cfg is None:
            audio_cfg = self._load_audio_settings()
        audio_chained = dict((audio_cfg.get("chained_fallback") or {}))

        def _pick(key: str, default: str = "") -> str:
            for block in (voice_chained, audio_chained):
                value = block.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            return default

        return {
            "stt_provider": _pick("stt_provider", "deepgram"),
            "tts_provider": _pick("tts_provider", "elevenlabs"),
            "stt_model": _pick("stt_model"),
            "tts_model": _pick("tts_model"),
            "tts_voice": _pick("tts_voice"),
            "tts_voice_id": _pick("tts_voice_id"),
        }

    async def _try_chained_morph(
        self,
        session_id: str,
        *,
        audio_cfg: dict,
        reason: str,
        detail: str,
        provider: str,
    ) -> tuple[bool, str]:
        """Attempt the S4 chained-pipeline morph.

        Returns ``(morphed, abort_tag)``. ``morphed`` is True iff the
        morph completed (voice_status emitted, session flipped).
        ``abort_tag`` is one of the ``_MORPH_ABORT_*`` constants and
        tells the caller WHY we bailed — it only needs to distinguish
        ``failed_credential`` (the honest-banner case) from the rest,
        which all fall through to the legacy whisper degrade.
        """
        # Structural check first: without a wired pipeline no morph can
        # happen regardless of keys, and the caller must not read
        # anything into the abort tag in that case.
        if not getattr(self, "_chained", None):
            logger.warning(
                "Chained morph for %s aborted — pipeline not wired",
                session_id[:8],
            )
            return False, _MORPH_ABORT_NOT_WIRED

        chained_cfg = self._resolve_chained_config(audio_cfg)
        stt_name = chained_cfg["stt_provider"]
        tts_name = chained_cfg["tts_provider"]
        substituted_stt_model = False

        # Vault preflight — every chained TTS / STT we ship needs a
        # key, but only deepgram + elevenlabs are wired as the S4
        # defaults. Resolve each provider's key via vault_keys so a
        # labeled-only entry (e.g. ``feral key add --provider openai
        # --label prod --set-active``) is visible here even if the
        # legacy default-namespace entry was never written. Env-only
        # setups continue to work via the fallback in
        # :func:`_resolve_provider_key`.
        stt_pid, stt_env = _provider_credential("stt", stt_name)
        tts_pid, tts_env = _provider_credential("tts", tts_name)

        def _missing_keys(pairs: list[tuple[str, str]]) -> list[str]:
            m: list[str] = []
            for pid, env_var in pairs:
                # ``("", "")`` marks a local engine: nothing to resolve,
                # nothing that can be missing.
                if not env_var:
                    continue
                if not _resolve_provider_key(pid, env_var):
                    m.append(env_var)
            return sorted(set(m))

        missing = _missing_keys([(stt_pid, stt_env), (tts_pid, tts_env)])
        # Auto-fallback: if the configured chained providers (default
        # deepgram + elevenlabs) have no keys but an OpenAI key IS present,
        # use OpenAI Whisper STT + OpenAI TTS so a chat-key-only operator
        # still gets a working full-duplex chained pipeline instead of
        # degrading to a dead half-duplex (TTS-only) session.
        #
        # NOT when the OpenAI credential is what failed. A quota/auth
        # close means that exact key is dead: substituting it here
        # built a "recovered" session whose STT 401s and whose TTS
        # never returns a byte, and the user got a green banner over
        # total silence. Refuse, and tag the abort so the caller emits
        # ``unavailable`` instead of promising whisper (which runs on
        # the same dead key).
        if (
            missing
            and reason in _OPENAI_CREDENTIAL_FAILURES
            and _resolve_provider_key("openai", "OPENAI_API_KEY")
        ):
            logger.warning(
                "Chained morph for %s aborted — %s unavailable and the OpenAI "
                "credential is what failed (%s); refusing to substitute it.",
                session_id[:8], ",".join(missing), reason,
            )
            return False, _MORPH_ABORT_FAILED_CREDENTIAL
        if missing and _resolve_provider_key("openai", "OPENAI_API_KEY"):
            logger.info(
                "Chained morph for %s: %s unavailable — falling back to OpenAI "
                "Whisper STT + OpenAI TTS.",
                session_id[:8], ",".join(missing),
            )
            stt_name, tts_name = "openai_whisper", "openai"
            stt_pid, stt_env = _provider_credential("stt", stt_name)
            tts_pid, tts_env = _provider_credential("tts", tts_name)
            missing = _missing_keys([(stt_pid, stt_env), (tts_pid, tts_env)])
            # The configured STT model belongs to the provider we just
            # walked away from (a Deepgram ``nova-*`` id is not a valid
            # Whisper model). Blank it so ``open_chained_session`` lets
            # the substituted provider use its own default.
            substituted_stt_model = True
        if missing:
            logger.warning(
                "Chained morph for %s aborted — missing vault keys: %s",
                session_id[:8], ",".join(sorted(missing)),
            )
            return False, _MORPH_ABORT_MISSING_KEYS

        # Stop the dead realtime / gemini session before retargeting
        # so we don't race with a final on_error landing while the
        # chained pipeline is booting.
        try:
            if provider == "gemini" and self._gemini:
                await self._gemini.stop_session(session_id)
            elif self._realtime:
                await self._realtime.stop_session(session_id)
        except Exception:
            logger.exception(
                "stop_session during chained morph failed for %s", session_id[:8],
            )

        # Retarget every bound node so the next inbound audio_chunk
        # lands on handle_chained_audio instead of the dead proxy.
        bound_node = ""
        bound_sample_rate = 0
        for node_id, sid in list(self._node_session_map.items()):
            if sid != session_id:
                continue
            if not bound_node:
                bound_node = node_id
                bound_sample_rate = int(
                    (self._node_voice_config.get(node_id) or {}).get("sample_rate") or 0
                )
            self.register_voice_config(node_id, {
                "mode": "chained",
                "supports_realtime": False,
            })

        provider_opts = {
            "stt_provider": stt_name,
            "tts_provider": tts_name,
            "node_id": bound_node,
        }
        if bound_sample_rate:
            provider_opts["sample_rate"] = bound_sample_rate
        if substituted_stt_model:
            provider_opts["stt_model"] = ""

        try:
            session = await self.open_chained_session(session_id, provider_opts)
        except Exception:
            logger.exception(
                "open_chained_session during morph failed for %s", session_id[:8],
            )
            session = None

        if session is None:
            return False, _MORPH_ABORT_OPEN_FAILED

        # open_chained_session already flips _session_voice_mode but
        # set it defensively in case a test stubs the method.
        self._session_voice_mode[session_id] = "chained"

        meta = {
            "state": "degraded",
            "reason": reason,
            "provider": provider,
            "fallback_provider": "chained",
            "detail": detail,
        }
        logger.warning(
            "Voice morphed session=%s reason=%s provider=%s → chained(%s+%s)",
            session_id[:8], reason, provider, stt_name, tts_name,
        )
        await self._emit_voice_status(session_id, meta)
        return True, _MORPH_ABORT_NONE

    async def emit_voice_notice(
        self,
        session_id: str,
        *,
        reason: str,
        detail: str = "",
    ) -> None:
        """Publish a non-fatal voice notice for a session that is still up.

        Emitted when OpenAI Realtime rejects a single client event
        (``RealtimeProxy._handle_notice``). ``state="available"``
        because that is the truth — the socket is open and the call is
        continuing — while ``reason``/``detail`` keep the refusal on
        the wire for the client log. Never touches
        ``_session_degraded``: a notice must not clear or fake the
        fallback bookkeeping.
        """
        if session_id in self._session_degraded:
            # Already flying a degraded/unavailable banner — an
            # ``available`` frame here would wrongly clear it.
            return
        if self._session_voice_mode.get(session_id) == "chained":
            return
        logger.info(
            "Voice notice session=%s reason=%s detail=%s",
            session_id[:8], reason, str(detail)[:120],
        )
        await self._emit_voice_status(session_id, {
            "state": "available",
            "reason": reason,
            "provider": "openai",
            "fallback_provider": "",
            "detail": detail,
        })

    async def emit_unavailable(
        self,
        session_id: str,
        *,
        reason: str,
        detail: str = "",
        provider: str = "",
        cause: str = "",
        summary: str = "",
        recommendation: str = "",
        privacy_downgrade: bool = False,
    ) -> None:
        """Emit ``voice_status state=unavailable``, called when even
        the fallback TTS path failed (e.g. no API key, network down).

        ``cause`` / ``summary`` / ``recommendation`` are the human
        explanation from ``voice/diagnostics.py``. They are optional
        because several callers only know a machine tag, and an empty
        string is honest where a manufactured sentence would not be.
        """
        meta = {
            "state": "unavailable",
            "reason": reason,
            "provider": provider,
            "fallback_provider": "",
            "detail": detail,
            "cause": cause,
            "summary": summary,
            "recommendation": recommendation,
            "privacy_downgrade": privacy_downgrade,
        }
        self._session_degraded[session_id] = meta
        logger.error(
            "Voice unavailable session=%s reason=%s detail=%s",
            session_id[:8], reason, detail[:120],
        )
        await self._emit_voice_status(session_id, meta)

    # ------------------------------------------------------------------
    # Failure diagnosis (voice/diagnostics.py)
    # ------------------------------------------------------------------

    #: HUP chain names -> ``security.probe`` provider ids. The probe
    #: registry distinguishes the realtime products from the plain chat
    #: credentials, so the mapping has to be explicit.
    _PROBE_IDS: dict[str, str] = {
        "openai": "openai_realtime",
        "gemini": "gemini_live",
    }

    def report_client_fault(self, session_id: str, code: str, detail: str = "") -> None:
        """Record a fault the CLIENT observed (mic permission, dead device).

        The brain cannot probe a microphone. When the client knows why
        capture failed -- ``BrowserNode.startMic`` produces
        ``MIC_PERMISSION_DENIED`` / ``MIC_UNAVAILABLE`` /
        ``MIC_START_FAILED`` -- that is the only evidence anyone has, and
        without it the diagnostic probes OpenAI, finds it healthy, and
        sends the user to fix the wrong thing.

        See the lane report: the client currently keeps these codes to
        itself (``onMicError`` is a local callback), so the one wire hop
        that feeds this is still missing and lives in a tree this lane
        does not own.
        """
        if not session_id or not code:
            return
        self._client_faults[session_id] = {
            "code": str(code), "detail": str(detail)[:300], "at": time.time(),
        }
        logger.warning(
            "Voice client fault session=%s code=%s detail=%s",
            session_id[:8], code, str(detail)[:120],
        )

    def _operator_chose_local_voice(self) -> bool:
        """True when the operator's chained pair is local engines only.

        Used to refuse a cloud fallback rather than take it silently.
        Both halves must be local: a local STT feeding a cloud TTS still
        ships the conversation off the machine.
        """
        cfg = self._resolve_chained_config()
        return (
            _is_local_provider("stt", cfg["stt_provider"])
            and _is_local_provider("tts", cfg["tts_provider"])
        )

    def _diagnostic_chain(self, session_id: str) -> list[str]:
        """Probe ids for the chain this session would actually walk."""
        chain = self.realtime_chain_for_surface(self._session_surface(session_id))
        ids: list[str] = []
        for name in chain:
            if name == CHAIN_TERMINAL:
                cfg = self._resolve_chained_config()
                for pid in (cfg["stt_provider"], cfg["tts_provider"]):
                    if pid and pid not in ids:
                        ids.append(pid)
                continue
            pid = self._PROBE_IDS.get(name, name)
            if pid not in ids:
                ids.append(pid)
        return ids

    async def diagnose_voice(
        self,
        session_id: str = "",
        *,
        publish: bool = False,
        force: bool = False,
    ):
        """Explain why voice is or is not working for ``session_id``.

        Returns a :class:`voice.diagnostics.VoiceDiagnosis`. With
        ``publish=True`` the diagnosis also goes out as a
        ``voice_status`` frame, which is what turns the existing banner
        from a machine tag into an explanation plus a next step.

        Mute is checked first and short-circuits everything: "voice is
        not working" while the microphone is muted has a one-word
        answer, and probing four vendors to reach it wastes the user's
        time and ends in advice about API keys.
        """
        from voice import diagnostics

        if self.is_session_muted(session_id):
            diagnosis = diagnostics.VoiceDiagnosis(
                ok=False,
                cause=diagnostics.CAUSE_MUTED,
                summary=(
                    "The microphone is muted for this session, so no audio "
                    "is reaching the brain. Nothing else is wrong."
                ),
                recommendation=diagnostics.recommendation_for(
                    diagnostics.CAUSE_MUTED,
                ),
            )
        else:
            diagnosis = await diagnostics.diagnose_chain(
                self._diagnostic_chain(session_id),
                client_fault=self._client_faults.get(session_id),
                chose_local=self._operator_chose_local_voice(),
                force=force,
            )
        if publish:
            await self._emit_voice_status(session_id, diagnosis.as_status_meta())
        return diagnosis

    def _pick_fallback_provider(self) -> str:
        """Return the first configured fallback TTS provider name.

        Order: ``audio.fallback_tts_providers`` from settings (if any),
        then ``whisper`` (OpenAI ``/audio/speech`` — the
        ``AudioPipeline.synthesize_speech`` default). Returns ``""`` if
        nothing is reachable so callers can emit ``unavailable``.
        """
        try:
            from config.loader import load_settings
            cfg = load_settings() or {}
            providers = (
                ((cfg.get("audio") or {}).get("fallback_tts_providers"))
                or []
            )
            if isinstance(providers, list) and providers:
                return str(providers[0])
        except Exception:
            pass
        # AudioPipeline ALWAYS has the whisper /audio/speech path as
        # long as an OpenAI key is loaded — that's the conservative
        # default ("disable voice" never sounded right). Resolve via
        # vault_keys so a labeled-only OpenAI key (no env var, no
        # legacy default-namespace entry) still unlocks this branch.
        if _resolve_provider_key("openai", "OPENAI_API_KEY"):
            return "whisper"
        return ""

    async def _emit_voice_status(self, session_id: str, meta: dict) -> None:
        """Send ``voice_status`` to whichever surface the session lives on."""
        from models.protocol import FeralMessage, VoiceStatusPayload
        # Stamp the live mute state onto EVERY status frame, whatever
        # emitted it. Clients reconcile their microphone indicator from
        # the newest frame, so a degraded banner that omitted ``muted``
        # would silently flip the UI back to "listening" over a
        # microphone the brain is still refusing to read.
        meta = {**meta, "muted": self.is_session_muted(session_id)}
        try:
            payload = VoiceStatusPayload(**meta).model_dump()
        except Exception:
            payload = meta
        msg = FeralMessage(
            session_id=session_id,
            hop="brain",
            type="voice_status",
            payload=payload,
        )
        sent = False
        if self._send_to_session:
            try:
                await self._send_to_session(session_id, msg)
                sent = True
            except Exception:
                logger.debug("voice_status session emit failed", exc_info=True)
        if self._send_to_node:
            for node_id, sid in list(self._node_session_map.items()):
                if sid == session_id:
                    try:
                        await self._send_to_node(node_id, {
                            "type": "voice_status", "payload": payload,
                        })
                        sent = True
                    except Exception:
                        logger.debug(
                            "voice_status node emit failed for %s",
                            node_id, exc_info=True,
                        )
        if not sent:
            logger.debug("voice_status had no surface to deliver to session=%s", session_id[:8])

    async def synthesize_assistant_speech(
        self,
        session_id: str,
        text: str,
    ) -> bool:
        """Synthesise ``text`` via the whisper fallback and fan out as
        ``tts_chunk`` frames. Used by ``send_text`` when the session is
        degraded so the assistant keeps speaking after Realtime died.
        Returns True iff at least one chunk was delivered.
        """
        if not text or not text.strip():
            return False
        if not self._audio:
            return False

        try:
            chunks = await self._audio.synthesize_speech(text[:1000])
        except Exception:
            logger.exception("Fallback TTS synth failed for session=%s", session_id[:8])
            chunks = None

        if not chunks:
            await self.emit_unavailable(
                session_id,
                reason="fallback_tts_failed",
                detail="audio.fallback_tts_providers exhausted",
            )
            return False

        from models.protocol import FeralMessage
        delivered = False
        for chunk in chunks:
            tts_msg = FeralMessage(
                session_id=session_id, hop="brain", type="tts_chunk",
                payload=chunk,
            )
            if self._send_to_session:
                try:
                    await self._send_to_session(session_id, tts_msg)
                    delivered = True
                except Exception:
                    logger.debug("tts_chunk session emit failed", exc_info=True)
            for node_id, sid in list(self._node_session_map.items()):
                if sid != session_id:
                    continue
                if self._send_to_node:
                    try:
                        await self._send_to_node(node_id, {
                            "type": "tts_chunk", "payload": chunk,
                        })
                        delivered = True
                    except Exception:
                        logger.debug("tts_chunk node emit failed", exc_info=True)
        return delivered

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def stop_session_voice(self, session_id: str):
        """Stop realtime voice for a web client session."""
        client_node = f"webclient_{session_id[:8]}"

        if self._gemini:
            gsid = self._gemini._node_to_session.get(client_node)
            if gsid:
                await self._gemini.stop_session(gsid)

        if self._realtime:
            sid_for_node = self._realtime._node_to_session.get(client_node)
            if sid_for_node:
                await self._realtime.stop_session(sid_for_node)

        # --- Subagent B: close chained session ---
        if hasattr(self, "_chained") and self._chained:
            await self._chained.close_session(session_id)
        # --- end Subagent B ---

        self._session_voice_mode.pop(session_id, None)
        self._session_degraded.pop(session_id, None)
        # Ending voice deliberately is the one thing that resets mute.
        # A transport drop is NOT that (see ``stop_node_voice``, which
        # leaves the ledger alone).
        self._session_muted.pop(session_id, None)
        logger.info(f"Voice stopped for session {session_id[:8]}")

    async def stop_node_voice(self, node_id: str):
        """Stop voice for a daemon/phone node that just disconnected.

        Node-shaped counterpart to :meth:`stop_session_voice`, which
        can only find web sessions (it derives the synthetic
        ``webclient_<sid>`` node name). Daemon nodes register under
        their real node id, so the web helper could never reach them.

        Pre-fix NOTHING called either helper from a disconnect
        handler: a tab close, a dropped LTE connection, or the phone
        app going to background left the OpenAI Realtime WebSocket
        open and billing, and left a stale ``_node_to_session`` entry
        that made the next ``voice_session_start`` hand back a dead
        handle.
        """
        if not node_id:
            return
        session_id = self._node_session_map.get(node_id, "")

        if self._gemini:
            gsid = getattr(self._gemini, "_node_to_session", {}).get(node_id)
            if gsid:
                await self._gemini.stop_session(gsid)

        if self._realtime:
            rsid = getattr(self._realtime, "_node_to_session", {}).get(node_id)
            if rsid:
                await self._realtime.stop_session(rsid)

        # The chained pipeline is keyed by session, not node, so only
        # close it when this node was the session's audio source — and
        # only then drop the session-scoped bookkeeping. A session can
        # still have a live web surface attached; clearing its voice
        # mode unconditionally would knock that surface off its
        # provider.
        if session_id and getattr(self, "_chained", None):
            if self._chained.get_session(session_id) is not None:
                await self._chained.close_session(session_id)
                self._session_voice_mode.pop(session_id, None)
                self._session_degraded.pop(session_id, None)

        self._node_voice_config.pop(node_id, None)
        self._node_session_map.pop(node_id, None)
        self._node_surface_map.pop(node_id, None)
        logger.info("Voice stopped for node %s", node_id)

    # --- Subagent A (realtime GA) + Subagent B (chained pipeline) integration ---
    #
    # open_session is the high-level mode dispatcher used by
    # daemon_session's voice_session_start handler. It routes to:
    #   - openai_realtime → RealtimeProxy (GA, gpt-realtime)
    #   - gemini_live     → GeminiRealtimeProxy (existing)
    #   - chained         → ChainedVoicePipeline via open_chained_session
    #   - whisper         → classic AudioPipeline STT → orchestrator path

    async def open_session(self, session_id: str, mode: str, provider_opts: dict | None = None):
        """High-level entry point for opening a voice session by mode.

        Dispatches by mode:
          - ``openai_realtime``: OpenAI Realtime GA (Subagent A)
          - ``chained``: Deepgram/Whisper STT → LLM → OpenAI/ElevenLabs TTS (Subagent B)
          - ``gemini_live``: existing GeminiRealtimeProxy
          - ``whisper``: selected classic STT → orchestrator, with TTS optional
        """
        opts = provider_opts or {}
        # A session that is still muted has to say so the moment it
        # comes back, or the reconnecting UI renders a live microphone
        # over a mute the brain is still enforcing. Emitted before the
        # provider work so a failed open cannot swallow it.
        if self.is_session_muted(session_id):
            await self._emit_voice_status(
                session_id, self._current_status_meta(session_id),
            )
        # A new attempt supersedes the last one's verdict, and it has to
        # be dropped here rather than on success: `_report_open_failure`
        # refuses to overwrite a status set closer to the failure, which
        # is right within one attempt and wrong across two. Left in
        # place, a session that failed once and then succeeded keeps the
        # unavailable meta for as long as it lives, and the next mute
        # toggle republishes it (`_current_status_meta`) over a working
        # session. Emitted after the mute frame above so that frame
        # still reports the state the session was actually in.
        self._session_degraded.pop(session_id, None)
        if mode == "whisper":
            ready = getattr(self._audio, "selected_stt_ready", None)
            if ready is None:
                ready = bool(getattr(self._audio, "available", False))
            if not self._audio or not ready:
                logger.warning("whisper requested but selected STT is unavailable")
                await self._report_open_failure(
                    session_id,
                    reason="classic_stt_unavailable",
                    provider="whisper",
                    cause=_dx().CAUSE_UNKNOWN,
                    summary=(
                        "The selected classic speech-to-text backend is not "
                        "configured or ready, so voice could not start."
                    ),
                )
                return None
            node_id = opts.get("node_id", "")
            if node_id:
                self.register_voice_config(node_id, {
                    "mode": "whisper",
                    "voice_provider": "whisper",
                    "supports_realtime": False,
                    "sample_rate": int(opts.get("sample_rate") or 24000),
                    "language_hint": opts.get("language_hint", ""),
                    "skip_wake": True,
                })
            self._session_voice_mode[session_id] = "whisper"
            # The classic path has no upstream session object: AudioPipeline
            # buffers per session id. Returning the pipeline is the honest
            # live handle expected by the phone bootstrap contract.
            return self._audio
        if mode == "openai_realtime":
            if not self._realtime or not self._realtime.available:
                logger.warning("openai_realtime requested but proxy unavailable")
                await self._report_open_failure(
                    session_id,
                    reason="openai_realtime_unavailable",
                    provider="openai",
                    cause=(
                        _dx().CAUSE_NO_API_KEY if self._realtime
                        else _dx().CAUSE_UNKNOWN
                    ),
                    summary=(
                        "OpenAI Realtime has no API key, so the voice "
                        "session could not be opened."
                        if self._realtime else
                        "This brain has no OpenAI Realtime proxy wired in, "
                        "so mode openai_realtime cannot be opened."
                    ),
                )
                return None
            node_id = opts.get("node_id", f"webclient_{session_id[:8]}")
            model = opts.get("model") or _settings_realtime_model()
            voice = opts.get("voice", "marin")
            input_sample_rate = int(opts.get("sample_rate") or 24000)
            language_hint = opts.get("language_hint", "")
            self.register_voice_config(node_id, {
                "mode": "openai_realtime",
                "voice_provider": "openai",
                "supports_realtime": True,
                "sample_rate": input_sample_rate,
                "language_hint": language_hint,
                "voice": voice,
                "model": model,
                "skip_wake": True,
            })
            rs = await self._realtime.start_session(
                session_id,
                node_id,
                model=model,
                voice=voice,
                input_sample_rate=input_sample_rate,
                language_hint=language_hint,
            )
            if rs is None:
                rs = await self._recover_or_report(
                    session_id,
                    reason="openai_realtime_start_failed",
                    provider="openai",
                    summary=(
                        "OpenAI Realtime refused the connection, and no "
                        "fallback recovered the session."
                    ),
                )
            return rs
        if mode == "chained":
            node_id = opts.get("node_id", "")
            if node_id:
                self.register_voice_config(node_id, {
                    "mode": "chained",
                    "voice_provider": "openai",
                    "supports_realtime": False,
                    "skip_wake": True,
                })
            session = await self.open_chained_session(session_id, opts)
            if session is None:
                # ``open_chained_session`` refuses through
                # ``_construct_provider``, which has already emitted a
                # voice_status naming the engine that could not be
                # built. ``_report_open_failure`` will not overwrite it.
                await self._report_open_failure(
                    session_id,
                    reason="chained_open_failed",
                    provider="chained",
                    cause=_dx().CAUSE_UNKNOWN,
                    summary=(
                        "The chained STT/LLM/TTS pipeline could not be "
                        "opened for this session."
                    ),
                )
            return session
        if mode == "gemini_live":
            if not self._gemini or not self._gemini.available:
                logger.warning("gemini_live requested but proxy unavailable")
                await self._report_open_failure(
                    session_id,
                    reason="gemini_live_unavailable",
                    provider="gemini",
                    cause=(
                        _dx().CAUSE_NO_API_KEY if self._gemini
                        else _dx().CAUSE_UNKNOWN
                    ),
                    summary=(
                        "Gemini Live has no API key, so the voice session "
                        "could not be opened."
                        if self._gemini else
                        "This brain has no Gemini Live proxy wired in, so "
                        "mode gemini_live cannot be opened."
                    ),
                )
                return None
            node_id = opts.get("node_id", f"webclient_{session_id[:8]}")
            self.register_voice_config(node_id, {
                "mode": "gemini_live",
                "voice_provider": "gemini",
                "supports_realtime": True,
                "sample_rate": int(opts.get("sample_rate") or 16000),
                "language_hint": opts.get("language_hint", ""),
                "skip_wake": True,
            })
            gs = await self._gemini.start_session(session_id, node_id)
            if gs is None:
                gs = await self._recover_or_report(
                    session_id,
                    reason="gemini_live_start_failed",
                    provider="gemini",
                    summary=(
                        "Gemini Live refused the connection, and no "
                        "fallback recovered the session."
                    ),
                )
            return gs
        logger.warning("open_session: mode=%s not recognised", mode)
        await self._report_open_failure(
            session_id,
            reason="unknown_voice_mode",
            provider="",
            cause=_dx().CAUSE_UNKNOWN,
            summary=(
                f"Voice mode {mode!r} is not one this brain implements, so "
                "no session was opened."
            ),
        )
        return None

    async def _recover_or_report(
        self,
        session_id: str,
        *,
        reason: str,
        provider: str,
        summary: str = "",
    ):
        """Resolve what a ``start_session`` that returned None left behind.

        A realtime proxy reports a failed connect by returning ``None``
        AND calling ``handle_realtime_failure``, which may have morphed
        the session onto the chained pipeline. In that case voice IS
        live, on a different provider, and the caller must not be told
        the open failed. Returns the recovered handle when there is
        one; otherwise reports the failure and returns ``None``.
        """
        recovered = None
        if self._session_voice_mode.get(session_id) == "chained":
            chained = getattr(self, "_chained", None)
            getter = getattr(chained, "get_session", None) if chained else None
            if callable(getter):
                try:
                    recovered = getter(session_id)
                except Exception:
                    logger.debug(
                        "chained get_session probe failed for %s",
                        session_id[:8], exc_info=True,
                    )
        if recovered is not None:
            logger.info(
                "Voice open for session=%s failed on %s but recovered on "
                "the chained pipeline", session_id[:8], provider,
            )
            return recovered
        await self._report_open_failure(
            session_id,
            reason=reason,
            provider=provider,
            cause=_dx().CAUSE_UNKNOWN,
            summary=summary,
        )
        return None

    async def _report_open_failure(
        self,
        session_id: str,
        *,
        reason: str,
        provider: str = "",
        cause: str = "",
        summary: str = "",
        detail: str = "",
    ) -> None:
        """Say out loud that a voice session did not open.

        ``open_session`` reported every non-crash failure by returning
        ``None`` and, on most paths, logging at debug. Nothing reached
        the client, so a phone that asked for voice sat on "listening"
        against a session that does not exist. Every ``return None`` in
        ``open_session`` now routes through here.

        A session that already carries a degraded or unavailable status
        keeps it: those were produced closer to the failure and name
        the actual engine, and replacing them with this generic one
        would lose information.
        """
        existing = self._session_degraded.get(session_id)
        if existing and existing.get("state") in ("degraded", "unavailable"):
            logger.debug(
                "open failure for %s already reported as %s",
                session_id[:8], existing.get("reason"),
            )
            return
        await self.emit_unavailable(
            session_id,
            reason=reason,
            detail=detail or summary,
            provider=provider,
            cause=cause or _dx().CAUSE_UNKNOWN,
            summary=summary,
            recommendation=_dx().recommendation_for(
                cause or _dx().CAUSE_UNKNOWN, provider,
            ),
        )

    # Chained-pipeline helpers (Subagent B)

    def set_chained_pipeline(self, pipeline) -> None:
        """Inject ChainedVoicePipeline after construction."""
        self._chained = pipeline

    async def open_chained_session(
        self, session_id: str, provider_opts: dict | None = None
    ):
        """Create a chained STT→LLM→TTS session with configured providers.

        Called when ``mode="chained"`` is received in a
        ``voice_session_start`` envelope.  Per-session ``provider_opts``
        win; anything they leave unset comes from
        :meth:`_resolve_chained_config`, i.e. ``voice.chained.*`` (the
        phone Settings panel) first and ``audio.chained_fallback.*``
        (the setup wizard and the shipped defaults) second.
        """
        if not hasattr(self, "_chained") or self._chained is None:
            logger.warning("Chained pipeline not available")
            return None

        opts = provider_opts or {}

        from voice.stt_providers import get_stt_provider
        from voice.tts_providers import get_tts_provider

        chained_cfg = self._resolve_chained_config()

        stt_name = opts.get("stt_provider") or chained_cfg["stt_provider"]
        tts_name = opts.get("tts_provider") or chained_cfg["tts_provider"]
        # An unset STT model means "use the provider's own default".
        # Hardcoding Deepgram's ``nova-3`` here used to be harmless
        # because Deepgram was the only reachable STT; now that the
        # phone picker can actually select Whisper, forwarding
        # ``nova-3`` to Groq or OpenAI would be an invalid model id.
        stt_model = (
            opts["stt_model"] if "stt_model" in opts else chained_cfg["stt_model"]
        )
        stt_model = (stt_model or "").strip()
        # ``tts_model_raw`` is the operator's literal pick (empty when
        # unset). Only the OpenAI branch may substitute an OpenAI model
        # id for it; Cartesia has its own ``sonic-*`` ids.
        tts_model_raw = opts.get("tts_model") or chained_cfg["tts_model"]
        tts_model = tts_model_raw or "gpt-4o-mini-tts"
        tts_voice = opts.get("tts_voice") or chained_cfg["tts_voice"] or "alloy"
        tts_voice_id = opts.get("tts_voice_id") or chained_cfg["tts_voice_id"]
        # STT sample rate MUST match the source audio. The iOS HFP /
        # glasses path streams 24 kHz mono PCM16 (AVAudioConverter on
        # the iOS side feeds the brain), and the live audio routers
        # (``handle_audio_from_node`` / ``handle_audio_from_client``)
        # both default to 24 kHz too. Before this fix the STT provider
        # constructors fell through to their own 16 kHz default, which
        # made Deepgram interpret 24 kHz audio as 16 kHz (1.5× playback
        # speed) — producing the live "weird words then degraded"
        # symptom because every phoneme arrived 50% too fast. Always
        # pass an explicit rate; honour the per-session opt then fall
        # back to the audio-path default of 24 kHz. ``_try_chained_morph``
        # already threads the bound node's sample_rate through opts, so
        # the morph path picks up the real value when known.
        stt_sample_rate = int(opts.get("sample_rate") or 24000)

        # Resolve each provider's key via vault_keys.get_active_key
        # (with env fallback) so a labeled-only entry like
        # ``feral key add --provider openai --label prod --set-active``
        # — which does NOT also touch ``os.environ`` — is visible to
        # both the STT/TTS constructors below. Env-only setups keep
        # working unchanged.
        # Local engines resolve to ``("", "")`` and get an empty key,
        # which their constructors accept and discard. The router still
        # passes ``api_key=`` to every provider, so a local provider
        # that refused the kwarg would be unconstructible here.
        stt_pid, stt_env = _provider_credential("stt", stt_name)
        tts_pid, tts_env = _provider_credential("tts", tts_name)

        stt_kwargs = {
            "api_key": _resolve_provider_key(stt_pid, stt_env) if stt_env else "",
            "sample_rate": stt_sample_rate,
        }
        if stt_model:
            stt_kwargs["model"] = stt_model
        stt_provider = await self._construct_provider(
            session_id, "stt", stt_name, get_stt_provider, stt_kwargs
        )
        if stt_provider is None:
            return None
        tts_kwargs = {
            "api_key": _resolve_provider_key(tts_pid, tts_env) if tts_env else "",
        }
        if tts_name == "macos_say":
            # ``say`` addresses voices by their human name ("Samantha"),
            # so either settings key can carry it; an empty value means
            # "whatever the user picked in System Settings", which is
            # the right default and the only way Apple's downloadable
            # Enhanced / Premium voices get used.
            chosen_voice = tts_voice or tts_voice_id
            if chosen_voice:
                tts_kwargs["voice"] = chosen_voice
            # Synthesise at the session's own rate so no resampler sits
            # between the engine and the speaker.
            tts_kwargs["sample_rate"] = stt_sample_rate
        elif tts_name == "piper":
            # Piper addresses voices by model name
            # ("en_US-lessac-medium"), which operators put in either key.
            chosen_voice = tts_voice_id or tts_voice
            if chosen_voice:
                tts_kwargs["voice"] = chosen_voice
        elif tts_name == "openai":
            tts_kwargs["model"] = tts_model
            tts_kwargs["voice"] = tts_voice
        elif tts_name == "elevenlabs":
            # ElevenLabs addresses voices by opaque id, never by the
            # friendly name the phone picker used to write into
            # ``tts_voice``. Only ``tts_voice_id`` is meaningful here.
            if tts_voice_id:
                tts_kwargs["voice_id"] = tts_voice_id
        elif tts_name == "cartesia":
            # Cartesia uses its own voice catalogue (Sonic-2 voice
            # ids are stable UUIDs); operators pin one via settings
            # `voice.chained.tts_voice_id` or per-session opts.
            if tts_voice_id:
                tts_kwargs["voice_id"] = tts_voice_id
            if opts.get("tts_websocket"):
                tts_kwargs["websocket"] = bool(opts["tts_websocket"])
            # Use the operator-configured Sonic model id when set;
            # default sonic-2 is fine for everyone else.
            if tts_model_raw:
                tts_kwargs["model_id"] = tts_model_raw

        tts_provider_inst = await self._construct_provider(
            session_id, "tts", tts_name, get_tts_provider, tts_kwargs
        )
        if tts_provider_inst is None:
            try:
                await stt_provider.close()
            except Exception:
                logger.debug("STT close after TTS failure", exc_info=True)
            return None

        send_fn = self._send_to_session

        async def _send_frame(sid, frame):
            if send_fn:
                from models.protocol import FeralMessage
                msg = FeralMessage(
                    session_id=sid,
                    hop="brain",
                    type=frame["type"],
                    payload=frame.get("payload", {}),
                )
                await send_fn(sid, msg)

        session = await self._chained.open_session(
            session_id=session_id,
            stt_provider=stt_provider,
            tts_provider=tts_provider_inst,
            llm_handle=self._orchestrator,
            send_frame=_send_frame,
            sample_rate=stt_sample_rate,
        )
        self._session_voice_mode[session_id] = "chained"
        return session

    async def _construct_provider(
        self, session_id: str, kind: str, name: str, factory, kwargs: dict
    ):
        """Build one chained provider, or refuse loudly and return None.

        A local engine that cannot run raises from its constructor with
        a specific reason (extra not installed, weights not downloaded,
        `say` missing). This turns that into a
        ``voice_status state=unavailable`` frame naming the engine and
        the reason, and then stops.

        It deliberately does NOT substitute a cloud provider. The legacy
        path in ``perception/audio_pipeline.py`` does exactly that, and
        for someone who chose local engines specifically so that their
        voice never leaves the machine, a silent flip to a cloud vendor
        is the worst available outcome: it is the failure mode they were
        trying to prevent, presented as success.

        Cloud providers keep raising as before; the same banner just
        makes the reason visible instead of leaving a dead session.
        """
        try:
            return factory(name, **kwargs)
        except Exception as exc:
            local = _is_local_provider(kind, name)
            detail = f"{name}: {exc}"
            logger.error(
                "Chained %s provider %r unavailable for session %s: %s",
                kind, name, session_id[:8], exc,
            )
            await self.emit_unavailable(
                session_id,
                reason=(
                    f"local_{kind}_unavailable" if local
                    else f"chained_{kind}_unavailable"
                ),
                detail=detail[:400],
            )
            return None

    async def cancel_chained_response(self, session_id: str) -> bool:
        """Barge-in for the chained pipeline. Returns True if a turn was cut.

        The counterpart to ``RealtimeSession.cancel_response`` /
        ``GeminiSession.cancel_response``, which is what
        ``api/server.py``'s ``voice_interrupt`` handler already calls
        for the two realtime providers. The chained path had no
        equivalent, so speaking over a chained reply did nothing at all
        and the user had to listen to the whole wrong answer.

        Note the key: realtime sessions are keyed by ``node_id``,
        chained sessions by ``session_id``. The handler has both.

        Safe on an unknown or idle session, so the caller does not have
        to know which voice mode is live. That is what lets the server
        side be a single unconditional line.
        """
        chained = getattr(self, "_chained", None)
        if chained is None:
            return False
        try:
            return bool(await chained.cancel(session_id, reason="user_interrupt"))
        except Exception:
            logger.warning(
                "Chained barge-in failed for session %s", session_id[:8],
                exc_info=True,
            )
            return False

    async def handle_chained_audio(
        self,
        session_id: str,
        audio_b64: str,
        chunk_index: int = 0,
        is_final: bool = False,
    ):
        """Route audio into the chained pipeline for a session."""
        if not hasattr(self, "_chained") or self._chained is None:
            return
        await self._chained.handle_audio(
            session_id=session_id,
            audio_b64=audio_b64,
            chunk_index=chunk_index,
            is_final=is_final,
        )

    # --- end integration ---

    async def shutdown(self):
        if self._realtime:
            await self._realtime.shutdown()
        if self._gemini:
            await self._gemini.shutdown()
        # --- Subagent B: shutdown chained pipeline ---
        if hasattr(self, "_chained") and self._chained:
            await self._chained.shutdown()
        # --- end Subagent B ---
