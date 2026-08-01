"""
FERAL Gemini Multimodal Live — Realtime Voice via Google's API
================================================================
Single-WebSocket realtime voice using Gemini's BidiGenerateContent API.
Same pattern as OpenAI Realtime: audio in/out, function calling, transcriptions.
"""

from __future__ import annotations
import asyncio
import json
import logging
import os
import time
from typing import Optional, Callable
from uuid import uuid4

from agents.tool_display import tool_feedback_text
from voice.transcript_filter import should_commit_user_transcript

logger = logging.getLogger("feral.voice.gemini")

GEMINI_WS_URL = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
)
DEFAULT_MODEL = "gemini-2.0-flash-live-001"
INPUT_SAMPLE_RATE = 16000
OUTPUT_SAMPLE_RATE = 24000
AUDIO_FORMAT = "pcm16"


class GeminiRealtimeSession:
    """
    Single Gemini Multimodal Live session over WebSocket.
    Audio/video in, audio out, with function-calling support.
    """

    def __init__(
        self,
        session_id: str,
        node_id: str,
        *,
        api_key: str = "",
        model: str = "",
        system_prompt: str = "",
        tools: list[dict] | None = None,
        on_audio_delta: Callable | None = None,
        on_transcript: Callable | None = None,
        on_input_transcript: Callable | None = None,
        on_tool_call: Callable | None = None,
        on_speech_started: Callable | None = None,
        on_error: Callable | None = None,
    ):
        self.session_id = session_id
        self.node_id = node_id
        self._api_key = api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY", "")
        self._model = model or os.getenv("FERAL_GEMINI_LIVE_MODEL", DEFAULT_MODEL)
        self._system_prompt = system_prompt
        self._tools = tools or []

        self._on_audio_delta = on_audio_delta
        self._on_transcript = on_transcript
        self._on_input_transcript = on_input_transcript
        self._on_tool_call = on_tool_call
        self._on_speech_started = on_speech_started
        self._on_error = on_error

        self._ws = None
        self._connected = False
        self._recv_task: Optional[asyncio.Task] = None

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self):
        if not self._api_key:
            logger.error("Cannot start Gemini realtime — no GEMINI_API_KEY")
            return

        try:
            headers = {"x-goog-api-key": self._api_key}
            # Cross-version-safe; helper translates to `extra_headers`
            # if running on legacy `websockets.connect`. See
            # realtime_proxy.py `_connect_with_retry` for the contract.
            self._ws = await self._connect_with_retry(
                GEMINI_WS_URL,
                additional_headers=headers,
                max_size=10 * 1024 * 1024,
                ping_interval=20,
            )
            self._connected = True
            self._recv_task = asyncio.create_task(self._receive_loop())

            await self._send_setup()
            logger.info(f"Gemini realtime session opened: {self.session_id}")
        except Exception as e:
            logger.error(f"Failed to connect to Gemini realtime: {e}")
            self._connected = False

    @staticmethod
    async def _connect_with_retry(url, **kwargs):
        # Same cross-version dance as realtime_proxy._connect_with_retry.
        try:
            from websockets.asyncio.client import connect as _ws_connect
        except ImportError:
            import websockets as _ws
            _ws_connect = _ws.connect
            if "additional_headers" in kwargs and "extra_headers" not in kwargs:
                kwargs["extra_headers"] = kwargs.pop("additional_headers")
        for attempt in range(3):
            try:
                return await _ws_connect(url, **kwargs)
            except Exception:
                if attempt == 2:
                    raise
                logger.warning("Gemini WS connect failed (attempt %d/3) — retrying", attempt + 1)
                await asyncio.sleep(2 ** attempt)

    async def _send_setup(self):
        """Send initial config message with model, system instruction, and tools."""
        function_declarations = []
        for t in self._tools:
            fn = t.get("function", {})
            function_declarations.append({
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters", {}),
            })

        config: dict = {
            "model": f"models/{self._model}",
            "responseModalities": ["AUDIO"],
            "systemInstruction": {
                "parts": [{"text": self._system_prompt}],
            },
        }
        if function_declarations:
            config["tools"] = [{"functionDeclarations": function_declarations}]

        await self._send({"config": config})

    async def send_audio(self, audio_b64: str):
        """Stream a chunk of PCM16 audio at 16 kHz to Gemini."""
        if not self._connected:
            return
        t0 = time.monotonic()
        await self._send({
            "realtimeInput": {
                "audio": {
                    "data": audio_b64,
                    "mimeType": f"audio/pcm;rate={INPUT_SAMPLE_RATE}",
                },
            },
        })
        logger.debug("audio_chunk sent session=%s latency_ms=%.1f", self.session_id, (time.monotonic() - t0) * 1000)

    async def send_video(self, frame_b64: str, mime_type: str = "image/jpeg"):
        """Stream a video/image frame to Gemini for multimodal context."""
        if not self._connected:
            return
        await self._send({
            "realtimeInput": {
                "video": {
                    "data": frame_b64,
                    "mimeType": mime_type,
                },
            },
        })

    async def send_text(self, text: str):
        """Send a text message into the live session."""
        if not self._connected:
            return
        await self._send({
            "realtimeInput": {
                "text": text,
            },
        })

    async def send_tool_response(self, function_responses: list[dict]):
        """Return tool results to the model so it can continue generating."""
        await self._send({
            "toolResponse": {
                "functionResponses": function_responses,
            },
        })

    async def disconnect(self):
        self._connected = False
        if self._recv_task:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
        logger.info(f"Gemini realtime session closed: {self.session_id}")

    async def _send(self, message: dict):
        if self._ws and self._connected:
            try:
                await self._ws.send(json.dumps(message))
            except Exception as e:
                logger.error(f"Gemini send error: {e}")
                self._connected = False

    async def _receive_loop(self):
        try:
            async for raw_msg in self._ws:
                try:
                    event = json.loads(raw_msg)
                    await self._handle_event(event)
                except json.JSONDecodeError:
                    continue
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error(f"Gemini receive error: {e}")
            self._connected = False

    async def _handle_event(self, event: dict):
        if "setupComplete" in event:
            logger.info("Gemini setup complete")
            return

        server_content = event.get("serverContent")
        if server_content:
            await self._handle_server_content(server_content)

        tool_call = event.get("toolCall")
        if tool_call:
            await self._handle_tool_call_event(tool_call)

        if "error" in event:
            error = event["error"]
            msg = error.get("message", str(error))
            logger.error(f"Gemini API error: {msg}")
            if self._on_error:
                await self._on_error(self.session_id, msg)

    async def _handle_server_content(self, sc: dict):
        parts = sc.get("modelTurn", {}).get("parts", [])
        for part in parts:
            if "inlineData" in part:
                inline = part["inlineData"]
                if inline.get("mimeType", "").startswith("audio/"):
                    if self._on_audio_delta:
                        await self._on_audio_delta(
                            self.session_id, inline.get("data", ""), False,
                        )

        input_tx = sc.get("inputTranscription", {}).get("text")
        if input_tx and self._on_input_transcript:
            await self._on_input_transcript(self.session_id, input_tx)

        output_tx = sc.get("outputTranscription", {}).get("text")
        if output_tx and self._on_transcript:
            await self._on_transcript(self.session_id, output_tx, True)

        if sc.get("turnComplete"):
            if self._on_transcript:
                await self._on_transcript(self.session_id, "", False)
            if self._on_audio_delta:
                await self._on_audio_delta(self.session_id, "", True)

        if sc.get("interrupted"):
            if self._on_speech_started:
                await self._on_speech_started(self.session_id)

    async def _handle_tool_call_event(self, tool_call: dict):
        function_calls = tool_call.get("functionCalls", [])
        for fc in function_calls:
            name = fc.get("name", "")
            args = json.dumps(fc.get("args", {}))
            call_id = fc.get("id", str(uuid4())[:8])
            logger.info(f"Gemini tool call: {name}")
            if self._on_tool_call:
                result = await self._on_tool_call(
                    self.session_id, call_id, name, args,
                )
                await self.send_tool_response([{
                    "name": name,
                    "id": call_id,
                    "response": json.loads(result) if isinstance(result, str) else result,
                }])


class GeminiRealtimeProxy:
    """Manages Gemini realtime sessions, mirrors RealtimeProxy interface."""

    def __init__(
        self,
        *,
        skill_registry=None,
        skill_executor=None,
        memory=None,
        perception=None,
        send_to_node=None,
        send_to_session=None,
        orchestrator=None,
    ):
        self._sessions: dict[str, GeminiRealtimeSession] = {}
        self._node_to_session: dict[str, str] = {}
        self._skill_registry = skill_registry
        self._skill_executor = skill_executor
        self._memory = memory
        self._perception = perception
        self._send_to_node = send_to_node
        self._send_to_session = send_to_session
        # PR9: see RealtimeProxy.__init__ — Gemini voice tool calls
        # need the same tool_start/tool_result envelopes the chat path
        # emits, otherwise the v2 chat trace cannot render them.
        self._orchestrator = orchestrator
        self._api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY", "")
        # Lane 05  (AUDIT-r14 finding 15 fix #3): fallback parity with
        # OpenAI Realtime. Without an attached VoiceRouter the
        # ``_handle_error`` path just logs and dies silent — the phone
        # never sees a ``voice_status`` frame, the user gets dead air.
        # ``api/state.py`` calls ``attach_fallback_router(voice_router)``
        # right after both are constructed, mirroring the realtime_proxy
        # wiring (state.py:1080 OpenAI / state.py:~1101 Gemini).
        self._fallback_router = None

    def attach_fallback_router(self, router) -> None:
        """Attach the VoiceRouter so connect/auth/runtime failures
        trigger a structured ``voice_status: degraded`` fan-out + the
        chained-pipeline fallback path. Mirror of
        ``RealtimeProxy.attach_fallback_router``."""
        self._fallback_router = router

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    def get_session(self, node_id: str) -> GeminiRealtimeSession | None:
        """Look up session by node_id (mirrors RealtimeProxy.get_session)."""
        sid = self._node_to_session.get(node_id)
        return self._sessions.get(sid) if sid else None

    async def start_session(
        self,
        session_id: str,
        node_id: str,
        model: str = "",
        system_prompt: str = "",
        on_audio_delta: Callable | None = None,
        on_transcript: Callable | None = None,
        on_tool_call: Callable | None = None,
        on_speech_started: Callable | None = None,
        on_error: Callable | None = None,
    ) -> GeminiRealtimeSession:
        _sys_prompt = system_prompt or await self._build_system_prompt(session_id)
        _model = model or os.getenv("FERAL_GEMINI_LIVE_MODEL", DEFAULT_MODEL)
        tools = self._get_tools()

        gs = GeminiRealtimeSession(
            session_id=session_id,
            node_id=node_id,
            api_key=self._api_key,
            model=_model,
            system_prompt=_sys_prompt,
            tools=tools,
            on_audio_delta=on_audio_delta or self._handle_audio_delta,
            on_transcript=on_transcript or self._handle_transcript,
            on_input_transcript=self._handle_input_transcript,
            on_tool_call=on_tool_call or self._handle_tool_call,
            on_speech_started=on_speech_started or self._handle_speech_started,
            on_error=on_error or self._handle_error,
        )

        await gs.connect()
        if not getattr(gs, 'connected', False) and not getattr(gs, '_ws', None):
            logger.warning("Gemini voice session failed to connect for %s", session_id)
            # Lane 05 : same connect-failure fan-out OpenAI got in
            # workstream 9 — emit a structured ``voice_status:
            # degraded`` so the phone can render a fallback banner
            # instead of dead air.
            if not self._api_key:
                reason = "gemini_live_no_key"
                detail = "GEMINI_API_KEY / GOOGLE_API_KEY not configured"
            else:
                reason = "gemini_live_connect"
                detail = "Gemini Live WS handshake failed"
            if self._fallback_router:
                try:
                    await self._fallback_router.handle_realtime_failure(
                        session_id=session_id,
                        reason=reason,
                        detail=detail,
                        provider="gemini",
                    )
                except Exception:
                    logger.exception(
                        "Fallback router refused gemini connect failure"
                    )
            return None
        self._sessions[session_id] = gs
        self._node_to_session[node_id] = session_id

        try:
            from api.state import state
            if state.orchestrator:
                for sid in list(state.sessions.keys()):
                    await state.orchestrator._emit_brain_event(sid, "voice_session", {
                        "active": True, "provider": "gemini", "session_id": session_id,
                    })
        except Exception:
            pass

        return gs

    async def stop_session(self, session_id: str):
        gs = self._sessions.pop(session_id, None)
        if gs:
            self._node_to_session.pop(gs.node_id, None)
            await gs.disconnect()

            try:
                from api.state import state
                if state.orchestrator:
                    for sid in list(state.sessions.keys()):
                        await state.orchestrator._emit_brain_event(sid, "voice_session", {
                            "active": False, "provider": "gemini", "session_id": session_id,
                        })
            except Exception:
                pass

    def has_session(self, session_id: str) -> bool:
        return session_id in self._sessions

    async def relay_audio(self, session_id_or_node: str, audio_b64: str):
        gs = self._sessions.get(session_id_or_node)
        if not gs:
            sid = self._node_to_session.get(session_id_or_node)
            gs = self._sessions.get(sid) if sid else None
        if gs and gs.connected:
            await gs.send_audio(audio_b64)

    async def relay_video(self, session_id_or_node: str, frame_b64: str, mime_type: str = "image/jpeg"):
        """Forward a video/image frame to an active Gemini session."""
        gs = self._sessions.get(session_id_or_node)
        if not gs:
            sid = self._node_to_session.get(session_id_or_node)
            gs = self._sessions.get(sid) if sid else None
        if gs and gs.connected:
            await gs.send_video(frame_b64, mime_type)

    async def shutdown(self):
        for sid in list(self._sessions):
            await self.stop_session(sid)

    async def _build_system_prompt(self, session_id: str) -> str:
        parts = [
            "You are FERAL, a personal AI operating system. "
            "You run locally on the user's devices and can control hardware, "
            "search the web, manage memory, and more. Be concise in voice."
        ]
        if self._memory:
            ctx = await self._memory.build_context_for_llm(session_id, max_tokens_budget=400)
            if ctx:
                parts.append(f"\n[Memory]\n{ctx}")
        return "\n".join(parts)

    def _get_tools(self) -> list[dict]:
        if self._skill_registry:
            from agents.tool_list import OPENAI_TOOL_HARD_LIMIT, cap_tools_with_pins
            return cap_tools_with_pins(
                self._skill_registry.get_all_tools(),
                max_tools=OPENAI_TOOL_HARD_LIMIT,
            )
        return []

    @staticmethod
    def _tool_feedback_text(tool_name: str) -> str:
        return tool_feedback_text(tool_name)

    async def _send_tool_feedback(self, session_id: str, text: str):
        if not text:
            return
        gs = self._sessions.get(session_id)
        if not gs:
            return
        # Gemini Live exposes no per-item identity, so ``seq`` is the
        # only ordering signal the client gets on this provider.
        from voice.transcript_order import TRANSCRIPT_ORDER
        payload = {
            "text": text, "role": "assistant", "is_partial": False,
            "seq": TRANSCRIPT_ORDER.next_seq(session_id),
        }
        if gs.node_id.startswith("webclient_") and self._send_to_session:
            from models.protocol import FeralMessage

            msg = FeralMessage(
                session_id=session_id,
                hop="brain",
                type="transcript",
                payload=payload,
            )
            await self._send_to_session(session_id, msg)
            return
        if self._send_to_node:
            await self._send_to_node(gs.node_id, {
                "type": "transcript",
                "payload": payload,
            })

    async def _handle_audio_delta(self, session_id: str, audio_b64: str, is_done: bool):
        gs = self._sessions.get(session_id)
        if not gs:
            return
        payload = {
            "data_b64": audio_b64, "encoding": AUDIO_FORMAT,
            "sample_rate": OUTPUT_SAMPLE_RATE, "is_final": is_done,
        }
        if gs.node_id.startswith("webclient_") and self._send_to_session:
            from models.protocol import FeralMessage
            msg = FeralMessage(
                session_id=session_id, hop="brain", type="audio_response",
                payload=payload,
            )
            await self._send_to_session(session_id, msg)
        elif self._send_to_node:
            await self._send_to_node(gs.node_id, {"type": "audio_response", "payload": payload})

    async def _handle_transcript(self, session_id: str, text: str, is_partial: bool):
        if not is_partial and text and self._memory:
            self._memory.working_push(session_id, {
                "role": "assistant", "text": text[:300], "source": "gemini_realtime",
            })
            # PR 9 gap-fill — durable persistence under voice:<sid>.
            try:
                if hasattr(self._memory, "conversation_append"):
                    await self._memory.conversation_append(
                        f"voice:{session_id}", "assistant", text[:300],
                        source="voice_realtime_gemini",
                        title=f"Voice session {session_id[:8]}",
                    )
            except Exception as exc:
                logger.debug("gemini voice persistence skipped: %s", exc)

        # Assistant-side counterpart of the ``note_voice_user_turn``
        # hook in ``_handle_input_transcript``. Gemini Live has the same
        # asymmetry the OpenAI Realtime path had: only the user side
        # reached ``conversation_history``, so the next text turn saw
        # two consecutive user rows and the model denied having spoken.
        if not is_partial and text and self._orchestrator is not None:
            try:
                await self._orchestrator.note_voice_assistant_turn(session_id, text)
            except Exception:
                logger.exception(
                    "gemini: note_voice_assistant_turn failed (non-fatal)"
                )

    async def _handle_input_transcript(self, session_id: str, text: str):
        """Handle user-speech transcription returned by Gemini."""
        # Bug 3 (phantom commit gate): same blocklist the OpenAI
        # Realtime path applies — Gemini Live ALSO commits hallucinated
        # closers on trailing silence (mirrors whisper's training-set
        # bias). Filter the commit before any downstream side-effect.
        if not should_commit_user_transcript(text):
            logger.info(
                "gemini: dropping phantom user transcript session=%s text=%r",
                session_id, text,
            )
            return
        if text and self._memory:
            self._memory.working_push(session_id, {
                "role": "user", "text": text[:300], "source": "gemini_realtime_input",
            })
            try:
                if hasattr(self._memory, "conversation_append"):
                    await self._memory.conversation_append(
                        f"voice:{session_id}", "user", text[:300],
                        source="voice_realtime_gemini",
                        title=f"Voice session {session_id[:8]}",
                    )
            except Exception as exc:
                logger.debug("gemini voice input persistence skipped: %s", exc)

        # Bug 1 + Bug 2(B) hook (Gemini parity with realtime_proxy):
        # let the orchestrator track active subject + scan for
        # temporal-recall queries on the voice path even though
        # Gemini's audio bypass means ``handle_command_stream`` never
        # runs. See ``Orchestrator.note_voice_user_turn`` for the
        # contract; the Gemini equivalent of OpenAI's ``inject_context``
        # is ``send_text`` with a system-style framing — there's no
        # explicit ``role=system`` channel in BidiGenerateContent so we
        # piggyback on ``send_text`` with a square-bracketed header
        # that the model treats as out-of-band context.
        if text and self._orchestrator is not None:
            try:
                hook_out = await self._orchestrator.note_voice_user_turn(
                    session_id, text, emit_temporal_timeline=True,
                )
            except Exception:
                logger.exception(
                    "gemini: note_voice_user_turn failed (non-fatal)"
                )
                hook_out = {}
            context_hint = (hook_out or {}).get("context_hint") or ""
            if context_hint:
                gs_for_hint = self._sessions.get(session_id)
                if gs_for_hint is not None:
                    try:
                        await gs_for_hint.send_text(context_hint)
                    except Exception:
                        logger.debug(
                            "gemini: send_text for active-subject hint failed",
                            exc_info=True,
                        )
            # Per-turn memory refresh for recall queries (Bug 2 B parity).
            try:
                from agents.orchestrator import Orchestrator as _Orch
                if _Orch._R_MEMORY.search(text) and self._memory:
                    asyncio.create_task(
                        self._refresh_memory_context(session_id, text)
                    )
            except Exception:
                logger.debug(
                    "gemini: per-turn memory refresh schedule failed",
                    exc_info=True,
                )

        gs = self._sessions.get(session_id)
        if not gs:
            return
        from voice.transcript_order import TRANSCRIPT_ORDER
        payload = {
            "text": text, "role": "user", "is_partial": False,
            "seq": TRANSCRIPT_ORDER.next_seq(session_id),
        }
        if gs.node_id.startswith("webclient_") and self._send_to_session:
            from models.protocol import FeralMessage
            msg = FeralMessage(
                session_id=session_id, hop="brain", type="transcript",
                payload=payload,
            )
            await self._send_to_session(session_id, msg)
        elif self._send_to_node:
            await self._send_to_node(gs.node_id, {"type": "transcript", "payload": payload})

    async def _refresh_memory_context(self, session_id: str, query: str) -> None:
        """Pull the freshest memory context relevant to ``query`` and
        inject it into the Gemini live session via ``send_text``. Mirror
        of ``RealtimeProxy._refresh_memory_context``. Background-only,
        never raises."""
        gs = self._sessions.get(session_id)
        if not gs or not self._memory:
            return
        try:
            ctx = await self._memory.build_context_for_llm(
                session_id, query=query, max_tokens_budget=600,
            )
        except Exception:
            logger.debug(
                "gemini: build_context_for_llm failed for refresh",
                exc_info=True,
            )
            return
        if not ctx:
            return
        try:
            await gs.send_text(f"[Memory Context — query: {query[:80]}]\n{ctx}")
        except Exception:
            logger.debug(
                "gemini: send_text for memory refresh failed",
                exc_info=True,
            )

    def _plan_mode_refusal(self, tool_name: str, session_id: str) -> Optional[dict]:
        """Return a refusal envelope when plan mode blocks ``tool_name``.

        Delegates to ``ToolRunner.enforce_plan_mode`` so this surface and
        the chat surface share one definition of plan-safe. Returns None
        when plan mode is off, when the tool is plan-safe, or when no
        orchestrator is wired.
        """
        runner = getattr(self._orchestrator, "tool_runner", None)
        enforce = getattr(runner, "enforce_plan_mode", None)
        if not callable(enforce):
            return None
        try:
            return enforce(tool_name, session_id)
        except Exception:
            logger.exception("plan-mode check failed for gemini voice tool %s", tool_name)
            return None

    async def _handle_tool_call(self, session_id: str, call_id: str, name: str, arguments: str) -> str:
        if not self._skill_executor or not self._skill_registry:
            return json.dumps({"error": "No skill executor"})
        try:
            args = json.loads(arguments) if isinstance(arguments, str) else arguments
        except json.JSONDecodeError:
            args = {}
        parts = name.split("__", 1)
        if len(parts) != 2:
            return json.dumps({"error": f"Invalid tool: {name}"})
        skill_id, endpoint_id = parts
        skill = self._skill_registry.skills.get(skill_id)
        if not skill:
            return json.dumps({"error": f"Skill not found: {skill_id}"})
        endpoint = next((ep for ep in skill.endpoints if ep.id == endpoint_id), None)
        if not endpoint:
            return json.dumps({"error": f"Endpoint not found: {endpoint_id}"})
        await self._send_tool_feedback(session_id, self._tool_feedback_text(name))

        # PR9: see realtime_proxy._handle_tool_call — emit the chat
        # trace envelopes so voice tools render in the v2 ToolTrace.
        tool_call = {"name": name, "id": call_id, "args": args}
        t0 = time.time()
        if self._orchestrator is not None:
            try:
                await self._orchestrator._emit_tool_start(session_id, tool_call)
            except Exception:
                logger.exception("gemini voice tool_start emit failed")

        # Same re-check as the OpenAI realtime proxy: this path calls
        # SkillExecutor directly and never reaches ToolRunner, so the
        # plan-mode gate there does not see it. Without this a session
        # in plan mode could mutate state through a live Gemini voice
        # call, which is exactly what the mode promises it cannot do.
        _refusal = self._plan_mode_refusal(name, session_id)
        if _refusal is not None:
            result = _refusal
        else:
            result = await self._skill_executor.execute(name, args, skill, endpoint)

        if self._orchestrator is not None:
            latency_ms = (time.time() - t0) * 1000.0
            try:
                await self._orchestrator._emit_tool_result(
                    session_id, tool_call, result, latency_ms,
                )
            except Exception:
                logger.exception("gemini voice tool_result emit failed")

        # Bug 2 (A) — Gemini parity with RealtimeProxy: persist a
        # voice-driven tool call as an episode anchored to the live
        # session so "what did my robot do?" surfaces it. See
        # ``RealtimeProxy._record_voice_tool_episode`` for the shape.
        try:
            await self._record_voice_tool_episode(session_id, name, args, result)
        except Exception:
            logger.debug(
                "gemini: voice tool episode persistence skipped",
                exc_info=True,
            )

        return json.dumps(result.get("data") or {"status": result.get("error", "done")})

    async def _record_voice_tool_episode(
        self,
        session_id: str,
        name: str,
        args: dict,
        result: dict,
    ) -> None:
        """Persist a voice-driven tool call as an episode anchored to the
        live session. Gemini parity of
        ``RealtimeProxy._record_voice_tool_episode`` — same shape so
        recall code surfaces voice episodes uniformly regardless of
        which realtime provider fired the tool.
        """
        if not self._memory:
            return
        episode_save = getattr(self._memory, "episode_save", None)
        if episode_save is None:
            return

        parts = name.split("__", 1)
        skill_id = parts[0] if parts else ""
        endpoint_id = parts[1] if len(parts) == 2 else ""
        is_hardware = (
            skill_id == "cutebot"
            or skill_id.startswith("hwdev_")
        )
        sensor_endpoints = {"status", "read", "read_telemetry", "get_state"}
        is_sensor = endpoint_id in sensor_endpoints or endpoint_id.startswith("get_")
        if is_hardware:
            event_type = "sensor" if is_sensor else "actuator"
        else:
            event_type = "tool"

        success = bool(result.get("success"))
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        verified = data.get("verified") if isinstance(data, dict) else None
        verdict = (
            "verified" if verified is True
            else "UNVERIFIED" if verified is False
            else "ok" if success else "failed"
        )
        summary = f"{skill_id}: {endpoint_id} {verdict}"

        try:
            detail = json.dumps(
                {
                    "category": event_type,
                    "tool_name": name,
                    "skill_id": skill_id,
                    "endpoint": endpoint_id,
                    "params": dict(args or {}),
                    "success": success,
                    "verified": verified,
                    "observed": data.get("observed") if isinstance(data, dict) else None,
                    "expected": data.get("expected") if isinstance(data, dict) else None,
                    "source": "voice_realtime_gemini",
                    "ts": time.time(),
                },
                default=str,
            )[:2000]
        except Exception:
            detail = ""

        if event_type == "sensor":
            importance = 0.3
        elif not success or verified is False:
            importance = 0.7
        else:
            importance = 0.6

        async def _runner() -> None:
            try:
                await episode_save(
                    session_id=session_id,
                    event_type=event_type,
                    summary=summary,
                    detail=detail,
                    location=skill_id or "voice",
                    importance=importance,
                )
            except Exception:
                logger.debug(
                    "gemini: episode_save for voice tool call raised",
                    exc_info=True,
                )

        try:
            asyncio.create_task(_runner())
        except RuntimeError:
            await _runner()

    async def _handle_speech_started(self, session_id: str):
        gs = self._sessions.get(session_id)
        if not gs:
            return
        payload = {"action": "stop_playback"}
        if gs.node_id.startswith("webclient_") and self._send_to_session:
            from models.protocol import FeralMessage

            msg = FeralMessage(
                session_id=session_id,
                hop="brain",
                type="speech_started",
                payload=payload,
            )
            await self._send_to_session(session_id, msg)
            return
        if self._send_to_node:
            await self._send_to_node(gs.node_id, {
                "type": "speech_started", "payload": payload,
            })

    async def _handle_error(self, session_id: str, error: str):
        """Classify a Gemini Live error and trigger fallback parity
        with OpenAI Realtime.

        Lane 05  (AUDIT-r14 finding 15 fix #3): pre-fix this method
        only ``logger.error``-ed and the session died silent. With
        ``_fallback_router`` attached at boot we now emit a structured
        ``voice_status: degraded`` frame + delegate to the chained
        pipeline so the call keeps going on Deepgram + ElevenLabs (or
        whatever STT/TTS pair the user has selected).

        Classification table mirrors the OpenAI counterpart:
          * 401 / API_KEY_INVALID / authentication → ``gemini_live_auth``
          * 429 / quota exceeded / billing → ``gemini_live_quota``
          * 503 / model overloaded → ``gemini_live_overload``
          * any other → ``gemini_live_error``
        """
        err_lc = (error or "").lower()
        if (
            "api_key_invalid" in err_lc
            or "401" in err_lc
            or "unauthorized" in err_lc
            or "permission_denied" in err_lc
            or "403" in err_lc
        ):
            reason = "gemini_live_auth"
        elif (
            "quota" in err_lc
            or "billing" in err_lc
            or "exceeded" in err_lc
            or "429" in err_lc
        ):
            reason = "gemini_live_quota"
        elif "overloaded" in err_lc or "503" in err_lc or "unavailable" in err_lc:
            reason = "gemini_live_overload"
        else:
            reason = "gemini_live_error"

        logger.error(
            "Gemini error [%s]: %s -> classified=%s",
            session_id, error, reason,
        )

        if self._fallback_router:
            try:
                await self._fallback_router.handle_realtime_failure(
                    session_id=session_id,
                    reason=reason,
                    detail=str(error)[:200],
                    provider="gemini",
                )
            except Exception:
                logger.exception(
                    "Fallback router refused gemini failure handoff"
                )
