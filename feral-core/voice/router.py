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
from typing import Any, Callable, Awaitable

logger = logging.getLogger("feral.voice.router")

_ENV_VOICE_PROVIDER = "FERAL_VOICE_PROVIDER"


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


def _settings_realtime_model() -> str:
    """Resolve the operator-configured OpenAI Realtime model.

    Reads ``audio.realtime_model`` from the merged settings tree
    (config/loader.py default: ``"gpt-realtime"``). When the settings
    layer is unavailable (e.g. in narrow unit tests) or the key is
    blank, fall back to :data:`voice.realtime_proxy.DEFAULT_MODEL` so
    the proxy still opens a session with a valid GA model id (Lane U2).
    """
    try:
        from config.loader import load_settings
        value = (load_settings().get("audio", {}) or {}).get("realtime_model")
        if isinstance(value, str) and value.strip():
            return value.strip()
    except Exception:
        logger.debug("settings.audio.realtime_model lookup failed", exc_info=True)
    try:
        from voice.realtime_proxy import DEFAULT_MODEL
        return DEFAULT_MODEL
    except Exception:
        return "gpt-realtime"


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

        env_provider = os.getenv(_ENV_VOICE_PROVIDER, "").lower()
        if env_provider == "gemini" and self._gemini and self._gemini.available:
            return "gemini"

        if cfg.get("supports_realtime") is True:
            if self._realtime and self._realtime.available:
                return "openai"

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

        env_provider = os.getenv(_ENV_VOICE_PROVIDER, "").lower()
        if env_provider == "gemini" and self._gemini and self._gemini.available:
            return "gemini"

        if self._realtime and self._realtime.available:
            return "openai"
        return "whisper"

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
            rs = self._realtime.get_session(node_id)
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
        provider = self._resolve_session_provider(session_id)
        client_node = f"webclient_{session_id[:8]}"

        if provider == "gemini":
            await self._handle_gemini_client(session_id, client_node, audio_b64)
            return

        if provider == "openai":
            rs = self._realtime.get_session(client_node)
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
        if self._send_to_session:
            msg = FeralMessage(
                session_id=session_id, hop="brain", type="transcript",
                payload=TranscriptPayload(text=transcript, is_partial=False).model_dump(),
            )
            await self._send_to_session(session_id, msg)

        if source_node_id and self._send_to_node:
            await self._send_to_node(source_node_id, {
                "type": "transcript",
                "payload": {"text": transcript, "role": "user", "is_partial": False},
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
          4. ``open_chained_session`` with the providers configured
             under ``audio.chained_fallback``.
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
        fallback_mode = str(audio_cfg.get("fallback_mode") or "whisper").lower()

        if fallback_mode == "chained" and await self._try_chained_morph(
            session_id,
            audio_cfg=audio_cfg,
            reason=reason,
            detail=detail,
            provider=provider,
        ):
            return

        fallback_provider = self._pick_fallback_provider()
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

    async def _try_chained_morph(
        self,
        session_id: str,
        *,
        audio_cfg: dict,
        reason: str,
        detail: str,
        provider: str,
    ) -> bool:
        """Attempt the S4 chained-pipeline morph. Returns True iff
        the morph completed (voice_status emitted, session flipped).
        Falls back to whisper degrade on missing keys / missing
        chained pipeline / open_chained_session failure.
        """
        chained_cfg = audio_cfg.get("chained_fallback") or {}
        stt_name = str(chained_cfg.get("stt_provider") or "deepgram")
        tts_name = str(chained_cfg.get("tts_provider") or "elevenlabs")

        # Vault preflight — every chained TTS / STT we ship needs a
        # key, but only deepgram + elevenlabs are wired as the S4
        # defaults. Resolve each provider's key via vault_keys so a
        # labeled-only entry (e.g. ``feral key add --provider openai
        # --label prod --set-active``) is visible here even if the
        # legacy default-namespace entry was never written. Env-only
        # setups continue to work via the fallback in
        # :func:`_resolve_provider_key`.
        provider_env = {
            "deepgram": ("deepgram", "DEEPGRAM_API_KEY"),
            "openai_whisper": ("openai", "OPENAI_API_KEY"),
            "groq_whisper": ("groq", "GROQ_API_KEY"),
            "elevenlabs": ("elevenlabs", "ELEVENLABS_API_KEY"),
            "cartesia": ("cartesia", "CARTESIA_API_KEY"),
            "openai": ("openai", "OPENAI_API_KEY"),
        }
        stt_pid, stt_env = provider_env.get(stt_name, ("deepgram", "DEEPGRAM_API_KEY"))
        tts_pid, tts_env = provider_env.get(tts_name, ("elevenlabs", "ELEVENLABS_API_KEY"))
        missing: list[str] = []
        for pid, env_var in {(stt_pid, stt_env), (tts_pid, tts_env)}:
            if not _resolve_provider_key(pid, env_var):
                missing.append(env_var)
        missing = sorted(set(missing))
        if missing:
            logger.warning(
                "Chained morph for %s aborted — missing vault keys: %s",
                session_id[:8], ",".join(sorted(missing)),
            )
            return False

        if not getattr(self, "_chained", None):
            logger.warning(
                "Chained morph for %s aborted — pipeline not wired",
                session_id[:8],
            )
            return False

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

        try:
            session = await self.open_chained_session(session_id, provider_opts)
        except Exception:
            logger.exception(
                "open_chained_session during morph failed for %s", session_id[:8],
            )
            session = None

        if session is None:
            return False

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
        return True

    async def emit_unavailable(
        self,
        session_id: str,
        *,
        reason: str,
        detail: str = "",
    ) -> None:
        """Emit ``voice_status state=unavailable`` — called when even
        the fallback TTS path failed (e.g. no API key, network down)."""
        meta = {
            "state": "unavailable",
            "reason": reason,
            "provider": "",
            "fallback_provider": "",
            "detail": detail,
        }
        self._session_degraded[session_id] = meta
        logger.error(
            "Voice unavailable session=%s reason=%s detail=%s",
            session_id[:8], reason, detail[:120],
        )
        await self._emit_voice_status(session_id, meta)

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
        logger.info(f"Voice stopped for session {session_id[:8]}")

    # --- Subagent A (realtime GA) + Subagent B (chained pipeline) integration ---
    #
    # open_session is the high-level mode dispatcher used by
    # daemon_session's voice_session_start handler. It routes to:
    #   - openai_realtime → RealtimeProxy (GA, gpt-realtime)
    #   - gemini_live     → GeminiRealtimeProxy (existing)
    #   - chained         → ChainedVoicePipeline via open_chained_session

    async def open_session(self, session_id: str, mode: str, provider_opts: dict | None = None):
        """High-level entry point for opening a voice session by mode.

        Dispatches by mode:
          - ``openai_realtime``: OpenAI Realtime GA (Subagent A)
          - ``chained``: Deepgram/Whisper STT → LLM → OpenAI/ElevenLabs TTS (Subagent B)
          - ``gemini_live``: existing GeminiRealtimeProxy
        """
        opts = provider_opts or {}
        if mode == "openai_realtime":
            if not self._realtime or not self._realtime.available:
                logger.warning("openai_realtime requested but proxy unavailable")
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
            return await self.open_chained_session(session_id, opts)
        if mode == "gemini_live":
            if not self._gemini or not self._gemini.available:
                logger.warning("gemini_live requested but proxy unavailable")
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
            return await self._gemini.start_session(session_id, node_id)
        logger.debug("open_session: mode=%s not recognised", mode)
        return None

    # Chained-pipeline helpers (Subagent B)

    def set_chained_pipeline(self, pipeline) -> None:
        """Inject ChainedVoicePipeline after construction."""
        self._chained = pipeline

    async def open_chained_session(
        self, session_id: str, provider_opts: dict | None = None
    ):
        """Create a chained STT→LLM→TTS session with configured providers.

        Called when ``mode="chained"`` is received in a
        ``voice_session_start`` envelope.  Reads provider choices from
        ``provider_opts`` (falls back to settings keys
        ``voice.chained.stt_provider``, ``voice.chained.tts_provider``).
        """
        if not hasattr(self, "_chained") or self._chained is None:
            logger.warning("Chained pipeline not available")
            return None

        opts = provider_opts or {}

        from voice.stt_providers import get_stt_provider
        from voice.tts_providers import get_tts_provider

        chained_cfg = self._load_audio_settings().get("chained_fallback") or {}
        default_stt = str(chained_cfg.get("stt_provider") or "deepgram")
        default_tts = str(chained_cfg.get("tts_provider") or "elevenlabs")

        stt_name = opts.get("stt_provider", default_stt)
        tts_name = opts.get("tts_provider", default_tts)
        stt_model = opts.get("stt_model", "nova-3")
        tts_model = opts.get("tts_model", "gpt-4o-mini-tts")
        tts_voice = opts.get("tts_voice", "alloy")

        # Resolve each provider's key via vault_keys.get_active_key
        # (with env fallback) so a labeled-only entry like
        # ``feral key add --provider openai --label prod --set-active``
        # — which does NOT also touch ``os.environ`` — is visible to
        # both the STT/TTS constructors below. Env-only setups keep
        # working unchanged.
        stt_keys = {
            "deepgram": _resolve_provider_key("deepgram", "DEEPGRAM_API_KEY"),
            "openai_whisper": _resolve_provider_key("openai", "OPENAI_API_KEY"),
            "groq_whisper": _resolve_provider_key("groq", "GROQ_API_KEY"),
        }
        tts_keys = {
            "openai": _resolve_provider_key("openai", "OPENAI_API_KEY"),
            "elevenlabs": _resolve_provider_key("elevenlabs", "ELEVENLABS_API_KEY"),
            # Lane 05 : Cartesia is now a first-class TTS provider.
            "cartesia": _resolve_provider_key("cartesia", "CARTESIA_API_KEY"),
        }

        stt_provider = get_stt_provider(
            stt_name,
            api_key=stt_keys.get(stt_name, ""),
            model=stt_model,
        )
        tts_kwargs = {"api_key": tts_keys.get(tts_name, "")}
        if tts_name == "openai":
            tts_kwargs["model"] = tts_model
            tts_kwargs["voice"] = tts_voice
        elif tts_name == "elevenlabs":
            if opts.get("tts_voice_id"):
                tts_kwargs["voice_id"] = opts["tts_voice_id"]
        elif tts_name == "cartesia":
            # Cartesia uses its own voice catalogue (Sonic-2 voice
            # ids are stable UUIDs); operators pin one via settings
            # `voice.chained.tts_voice_id` or per-session opts.
            if opts.get("tts_voice_id"):
                tts_kwargs["voice_id"] = opts["tts_voice_id"]
            if opts.get("tts_websocket"):
                tts_kwargs["websocket"] = bool(opts["tts_websocket"])
            # Use the operator-configured Sonic model id when set;
            # default sonic-2 is fine for everyone else.
            if opts.get("tts_model"):
                tts_kwargs["model_id"] = opts["tts_model"]

        tts_provider_inst = get_tts_provider(tts_name, **tts_kwargs)

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
        )
        self._session_voice_mode[session_id] = "chained"
        return session

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
