"""
FERAL Gemini Multimodal Live — Realtime Voice via Google's API
================================================================
Single-WebSocket realtime voice using Gemini's BidiGenerateContent API.
Same pattern as OpenAI Realtime: audio in/out, function calling, transcriptions.
"""

from __future__ import annotations
from agents.runtime_context_checkpoint import RuntimeContextError, legacy_context_mutation
import asyncio
import json
import logging
import os
import time
from contextvars import ContextVar
from typing import Optional, Callable, Awaitable, ParamSpec, TypeVar
from uuid import uuid4

from agents.tool_display import tool_feedback_text
from bridges.client_voice_attempt import current_voice_attempt, voice_attempt_payload, voice_attempt_scope
from skills.call_context import bind_context
from skills.result_budget import serialize_for_storage
from voice.transcript_filter import should_commit_user_transcript
from voice.tool_result_envelope import serialize_realtime_tool_result
from voice.realtime_tool_dispatch import (
    RealtimeToolLane, MAX_PENDING_TOOLS, MAX_TOOL_EVENT_CHARS,
    MAX_TURN_TOOL_IDS, TOOL_FAILURE_SEND_TIMEOUT, REALTIME_SEND_TIMEOUT,
)

logger = logging.getLogger("feral.voice.gemini")

_CallbackArgs = ParamSpec("_CallbackArgs")
_CallbackResult = TypeVar("_CallbackResult")
_CALLBACK_OWNER: ContextVar["GeminiRealtimeSession | None"] = ContextVar("gemini_voice_callback_owner", default=None)
_CALLBACK_RESPONSE: ContextVar["tuple[GeminiRealtimeSession, int] | None"] = ContextVar("gemini_voice_callback_response", default=None)
_CALLBACK_TOOL: ContextVar["tuple[GeminiRealtimeSession, str] | None"] = ContextVar("gemini_voice_callback_tool", default=None)

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
        self._retired = False
        self._callback_guard: Callable[[], bool] | None = None
        self._voice_attempt = current_voice_attempt(session_id)
        self._callback_tasks: set[asyncio.Task] = set()
        self._tool_lane = RealtimeToolLane(self._callback_tasks)
        self._tool_failure_lane = RealtimeToolLane(self._callback_tasks)
        self._tool_result_lock = asyncio.Lock()
        self._cancelled_tool_ids: set[str] = set()
        self._output_epoch = 0

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
        epoch = self._output_epoch
        async with self._tool_result_lock:
            if not self._owns_callbacks() or epoch != self._output_epoch:
                return
            current = [response for response in function_responses if response.get("id") not in self._cancelled_tool_ids]
            if current:
                await self._send({"toolResponse": {"functionResponses": current}})

    async def disconnect(self):
        self._retired = True
        self._output_epoch += 1
        self._tool_lane.close()
        self._tool_failure_lane.close()
        current = asyncio.current_task()
        for task in tuple(self._callback_tasks):
            if task is not current and task not in {self._tool_lane._task, self._tool_failure_lane._task}:
                task.cancel()
        self._connected = False
        if self._recv_task and self._recv_task is not current:
            self._recv_task.cancel()
            done, _ = await asyncio.wait({self._recv_task}, timeout=1.0)
            if done:
                try:
                    self._recv_task.result()
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
                await asyncio.wait_for(self._ws.send(json.dumps(message)), REALTIME_SEND_TIMEOUT)
            except Exception as e:
                logger.error(f"Gemini send error: {e}")
                self._connected = False

    async def _receive_loop(self):
        try:
            async for raw_msg in self._ws:
                try:
                    event = json.loads(raw_msg)
                    if event.get("toolCall"):
                        epoch = self._output_epoch
                        await self._handle_event({key: value for key, value in event.items() if key != "toolCall"})
                        if self._owns_callbacks() and epoch == self._output_epoch:
                            await self._queue_tool_event(event["toolCall"])
                            await asyncio.sleep(0)
                    else:
                        await self._handle_event(event)
                except json.JSONDecodeError:
                    continue
                except asyncio.CancelledError:
                    task = asyncio.current_task()
                    if not self._owns_callbacks() or (task is not None and task.cancelling()):
                        raise
                    continue
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error(f"Gemini receive error: {e}")
            self._connected = False
        finally:
            self._connected = False
            self._output_epoch += 1
            self._tool_lane.close()
            self._tool_failure_lane.close()

    def _tool_event_current(self, epoch: int, call_id: str) -> bool:
        return (
            self._owns_callbacks() and epoch == self._output_epoch
            and call_id not in self._cancelled_tool_ids
            and (self._voice_attempt is None or self._voice_attempt.current())
        )

    async def _tool_dispatch_failure(self, fc: dict, code: str, current: Callable[[], bool]) -> None:
        if not current():
            return
        result = serialize_realtime_tool_result("realtime_dispatch", {
            "success": False, "data": None, "error_code": code,
            "error": "Realtime tool was not dispatched" if code != "REALTIME_TOOL_FAILED" else "Realtime tool outcome is unverified",
            "outcome_unknown": code == "REALTIME_TOOL_FAILED",
        })
        try:
            await asyncio.wait_for(self.send_tool_response([{
                "name": fc.get("name", ""), "id": fc.get("id", ""), "response": json.loads(result),
            }]), TOOL_FAILURE_SEND_TIMEOUT)
        except Exception as exc:
            logger.warning("Gemini tool failure publication unavailable: %s", type(exc).__name__)

    def _queue_tool_failure(self, epoch: int, fc: dict, code: str, current: Callable[[], bool]) -> None:
        async def publish() -> None:
            await self._tool_dispatch_failure(fc, code, current)
        if self._tool_failure_lane.submit((epoch, fc["id"]), current, publish) == "full":
            logger.warning("Gemini refusal publication queue full; tool not dispatched")

    async def _queue_tool_event(self, tool_call: dict) -> None:
        calls = tool_call.get("functionCalls", [])
        if not isinstance(calls, list):
            return
        epoch = self._output_epoch
        for fc in calls[:MAX_PENDING_TOOLS]:
            if (not isinstance(fc, dict) or not isinstance(fc.get("id"), str)
                    or not fc["id"] or len(fc["id"]) > 256
                    or not isinstance(fc.get("name", ""), str) or len(fc.get("name", "")) > 256):
                continue
            call_id = fc["id"]
            def current(call_id=call_id) -> bool:
                return self._connected and self._tool_event_current(epoch, call_id)
            if len(calls) > MAX_PENDING_TOOLS or len(json.dumps(fc)) > MAX_TOOL_EVENT_CHARS:
                # Refuse the entire oversized batch, bounded to the maximum
                # correlatable refusal count; never dispatch its prefix.
                self._queue_tool_failure(epoch, fc, "REALTIME_TOOL_INPUT_TOO_LARGE", current)
                continue

            async def run(fc=fc, current=current) -> None:
                try:
                    await self._handle_tool_call_event({"functionCalls": [fc]})
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("Gemini tool execution produced no verified result: %s", type(exc).__name__)
                    await self._tool_dispatch_failure(fc, "REALTIME_TOOL_FAILED", current)

            if self._tool_lane.submit((epoch, call_id), current, run) == "full":
                self._queue_tool_failure(epoch, fc, "REALTIME_TOOL_QUEUE_FULL", current)

    def _owns_callbacks(self) -> bool:
        return not self._retired and (self._callback_guard is None or self._callback_guard())

    async def _handle_event(self, event: dict):
        if not self._owns_callbacks():
            return
        epoch = self._output_epoch
        cancellation = event.get("toolCallCancellation")
        if isinstance(cancellation, dict):
            ids = cancellation.get("ids", [])
            if isinstance(ids, list):
                if len(ids) + len(self._cancelled_tool_ids) > MAX_TURN_TOOL_IDS:
                    self._output_epoch += 1
                    self._cancelled_tool_ids.clear()
                else:
                    self._cancelled_tool_ids.update(identifier for identifier in ids if isinstance(identifier, str) and len(identifier) <= 256)
                self._tool_lane.discard_obsolete()
                self._tool_failure_lane.discard_obsolete()
        if "setupComplete" in event:
            logger.info("Gemini setup complete")
            return

        server_content = event.get("serverContent")
        if server_content:
            await self._handle_server_content(server_content)
            if not self._owns_callbacks() or epoch != self._output_epoch:
                return

        tool_call = event.get("toolCall")
        if tool_call:
            await self._handle_tool_call_event(tool_call)
            if not self._owns_callbacks() or epoch != self._output_epoch:
                return

        if "error" in event:
            error = event["error"]
            msg = error.get("message", str(error))
            logger.error(f"Gemini API error: {msg}")
            if self._on_error:
                await self._on_error(self.session_id, msg)

    async def _handle_server_content(self, sc: dict):
        # Gemini serverContent has no response ID. This epoch fences suspended
        # work after a known interruption, not arbitrary unmarked late packets.
        if sc.get("interrupted"):
            self._output_epoch += 1
            self._cancelled_tool_ids.clear()
            self._tool_lane.discard_obsolete()
            self._tool_failure_lane.discard_obsolete()
            if self._on_speech_started:
                await self._on_speech_started(self.session_id)
            # Input transcription is independent user content, even when
            # carried in the same serverContent envelope as interruption.
            input_tx = sc.get("inputTranscription", {}).get("text")
            if self._owns_callbacks() and input_tx and self._on_input_transcript:
                await self._on_input_transcript(self.session_id, input_tx)
            return
        epoch = self._output_epoch
        parts = sc.get("modelTurn", {}).get("parts", [])
        for part in parts:
            if not self._owns_callbacks() or epoch != self._output_epoch:
                return
            if "inlineData" in part:
                inline = part["inlineData"]
                if inline.get("mimeType", "").startswith("audio/"):
                    if self._on_audio_delta:
                        await self._on_audio_delta(
                            self.session_id, inline.get("data", ""), False,
                        )

        input_tx = sc.get("inputTranscription", {}).get("text")
        if not self._owns_callbacks() or epoch != self._output_epoch:
            return
        if input_tx and self._on_input_transcript:
            await self._on_input_transcript(self.session_id, input_tx)

        output_tx = sc.get("outputTranscription", {}).get("text")
        if not self._owns_callbacks() or epoch != self._output_epoch:
            return
        if output_tx and self._on_transcript:
            await self._on_transcript(self.session_id, output_tx, True)

        if sc.get("turnComplete"):
            if not self._owns_callbacks() or epoch != self._output_epoch:
                return
            if self._on_transcript:
                await self._on_transcript(self.session_id, "", False)
            if self._on_audio_delta:
                if not self._owns_callbacks() or epoch != self._output_epoch:
                    return
                await self._on_audio_delta(self.session_id, "", True)

    async def _handle_tool_call_event(self, tool_call: dict):
        epoch = self._output_epoch
        function_calls = tool_call.get("functionCalls", [])
        for fc in function_calls:
            if not self._owns_callbacks() or epoch != self._output_epoch:
                return
            name = fc.get("name", "")
            args = json.dumps(fc.get("args", {}))
            call_id = fc.get("id", str(uuid4())[:8])
            if not self._tool_event_current(epoch, call_id):
                continue
            logger.info(f"Gemini tool call: {name}")
            if self._on_tool_call:
                token = _CALLBACK_TOOL.set((self, call_id))
                try:
                    result = await self._on_tool_call(
                        self.session_id, call_id, name, args,
                    )
                finally:
                    _CALLBACK_TOOL.reset(token)
                if not self._tool_event_current(epoch, call_id):
                    return
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
        # AUDIT-FIXES F-06, parity with RealtimeProxy. Strong references to
        # the per-turn memory refresh and the voice-tool episode_save. Both
        # are fire-and-forget memory writes and the loop holds tasks only
        # weakly, so a collected task loses the turn with nothing logged.
        self._bg_tasks: set[asyncio.Task] = set()
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

    def _assert_callback_owner(self, session_id: str) -> None:
        owner = _CALLBACK_OWNER.get()
        if owner is not None and (owner.session_id != session_id or self._sessions.get(session_id) is not owner or owner._retired):
            raise asyncio.CancelledError("Retired voice callback")
        response = _CALLBACK_RESPONSE.get()
        if owner is not None and owner._voice_attempt is not None and not owner._voice_attempt.current():
            raise asyncio.CancelledError("Retired voice attempt")
        if owner is not None and response is not None and response[0] is owner and response[1] != owner._output_epoch:
            raise asyncio.CancelledError("Retired voice response")
        tool = _CALLBACK_TOOL.get()
        if owner is not None and tool is not None and tool[0] is owner and tool[1] in owner._cancelled_tool_ids:
            raise asyncio.CancelledError("Retired voice tool")

    def _bind_callback(
        self, owner: GeminiRealtimeSession,
        callback: Callable[_CallbackArgs, Awaitable[_CallbackResult]],
        *, response_scoped: bool = False,
    ) -> Callable[_CallbackArgs, Awaitable[_CallbackResult]]:
        async def bound(*args: _CallbackArgs.args, **kwargs: _CallbackArgs.kwargs) -> _CallbackResult:
            if not owner._owns_callbacks():
                raise asyncio.CancelledError("Retired voice callback")
            token = _CALLBACK_OWNER.set(owner)
            response_token = _CALLBACK_RESPONSE.set((owner, owner._output_epoch) if response_scoped else None)
            task = asyncio.current_task()
            already_tracked = task in owner._callback_tasks if task is not None else False
            if task is not None:
                owner._callback_tasks.add(task)
            try:
                self._assert_callback_owner(owner.session_id)
                with voice_attempt_scope(owner._voice_attempt):
                    result = await callback(*args, **kwargs)
                self._assert_callback_owner(owner.session_id)
                return result
            finally:
                if task is not None and not already_tracked:
                    owner._callback_tasks.discard(task)
                _CALLBACK_OWNER.reset(token)
                _CALLBACK_RESPONSE.reset(response_token)
        return bound

    def _track_callback_task(self, task: asyncio.Task) -> None:
        owner = _CALLBACK_OWNER.get()
        if owner is not None:
            owner._callback_tasks.add(task)
            task.add_done_callback(owner._callback_tasks.discard)

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
        async with legacy_context_mutation(self._orchestrator, self._memory, (session_id,), "voice"):
            return await self._checkpoint_legacy_start_session(session_id=session_id, node_id=node_id, model=model, system_prompt=system_prompt, on_audio_delta=on_audio_delta, on_transcript=on_transcript, on_tool_call=on_tool_call, on_speech_started=on_speech_started, on_error=on_error)

    async def _checkpoint_legacy_start_session(
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

        gs._callback_guard = lambda: self._sessions.get(session_id) is gs
        # Supplied application callbacks obey the same ownership contract as
        # built-in memory, tool, audio and fallback handlers.
        gs._on_audio_delta = self._bind_callback(gs, on_audio_delta or self._handle_audio_delta, response_scoped=True)
        gs._on_transcript = self._bind_callback(gs, on_transcript or self._handle_transcript, response_scoped=True)
        gs._on_input_transcript = self._bind_callback(gs, self._handle_input_transcript)
        gs._on_tool_call = self._bind_callback(gs, on_tool_call or self._handle_tool_call, response_scoped=True)
        gs._on_speech_started = self._bind_callback(gs, on_speech_started or self._handle_speech_started)
        gs._on_error = self._bind_callback(gs, on_error or self._handle_error)
        self._sessions[session_id] = gs
        try:
            await gs.connect()
        except (asyncio.CancelledError, Exception):
            if self._sessions.get(session_id) is gs:
                self._sessions.pop(session_id)
            await gs.disconnect()
            raise
        owns_start = self._sessions.get(session_id) is gs
        if not owns_start or not getattr(gs, 'connected', False):
            if owns_start:
                self._sessions.pop(session_id)
            await gs.disconnect()
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
            if owns_start and self._fallback_router:
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
        self._node_to_session[node_id] = session_id

        try:
            from api.state import state
            if state.orchestrator:
                for sid in list(state.sessions.keys()):
                    if self._sessions.get(session_id) is not gs:
                        break
                    await state.orchestrator._emit_brain_event(sid, "voice_session", {
                        "active": True, "provider": "gemini", "session_id": session_id,
                    })
        except Exception:
            pass

        return gs

    async def stop_session(self, session_id: str):
        self._assert_callback_owner(session_id)
        gs = self._sessions.pop(session_id, None)
        if gs:
            self._node_to_session.pop(gs.node_id, None)
            await gs.disconnect()
            if session_id in self._sessions:
                return

            try:
                from api.state import state
                if state.orchestrator:
                    for sid in list(state.sessions.keys()):
                        if session_id in self._sessions:
                            break
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
        # ``_get_tools`` withholds the skills whose prerequisite is
        # absent, so name them and say why. Same reasoning as the OpenAI
        # proxy: a capability that vanishes from the tool list with no
        # explanation is one the model will report as missing from the
        # brain rather than from the setup.
        try:
            from skills.availability import availability_note

            note = availability_note()
            if note:
                parts.append(f"\n{note}")
        except Exception:
            logger.debug("voice availability note unavailable", exc_info=True)
        return "\n".join(parts)

    def _get_tools(self) -> list[dict]:
        if self._skill_registry:
            from agents.tool_list import OPENAI_TOOL_HARD_LIMIT, cap_tools_with_pins
            from skills.availability import filter_unavailable_tools

            # Withhold what cannot run before spending the 128 slots on
            # it. Voice needs the gate more than chat does, not less: it
            # is the surface already fighting a hard tool ceiling, so
            # every schema spent on a dead tool evicts a live one.
            return cap_tools_with_pins(
                filter_unavailable_tools(self._skill_registry.get_all_tools()),
                max_tools=OPENAI_TOOL_HARD_LIMIT,
            )
        return []

    @staticmethod
    def _tool_feedback_text(tool_name: str) -> str:
        return tool_feedback_text(tool_name)

    async def _send_tool_feedback(self, session_id: str, text: str):
        self._assert_callback_owner(session_id)
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
                payload=voice_attempt_payload(payload, current_voice_attempt(session_id)),
            )
            await self._send_to_session(session_id, msg)
            self._assert_callback_owner(session_id)
            return
        if self._send_to_node:
            await self._send_to_node(gs.node_id, {
                "type": "transcript",
                "payload": voice_attempt_payload(payload, current_voice_attempt(session_id)),
            })
            self._assert_callback_owner(session_id)

    async def _handle_audio_delta(self, session_id: str, audio_b64: str, is_done: bool):
        self._assert_callback_owner(session_id)
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
                payload=voice_attempt_payload(payload, current_voice_attempt(session_id)),
            )
            await self._send_to_session(session_id, msg)
            self._assert_callback_owner(session_id)
        elif self._send_to_node:
            await self._send_to_node(gs.node_id, {"type": "audio_response", "payload": voice_attempt_payload(payload, current_voice_attempt(session_id))})
            self._assert_callback_owner(session_id)

    async def _handle_transcript(self, session_id: str, text: str, is_partial: bool):
        self._assert_callback_owner(session_id)
        async with legacy_context_mutation(self._orchestrator, self._memory, (session_id,), "voice"):
            self._assert_callback_owner(session_id)
            return await self._checkpoint_legacy_handle_transcript(session_id=session_id, text=text, is_partial=is_partial)

    async def _checkpoint_legacy_handle_transcript(self, session_id: str, text: str, is_partial: bool):
        self._assert_callback_owner(session_id)
        if not is_partial and text and self._memory:
            self._memory.working_push(session_id, {
                "role": "assistant", "text": text[:300], "source": "gemini_realtime",
            })
            # PR 9 gap-fill — durable persistence under voice:<sid>.
            try:
                if hasattr(self._memory, "conversation_append"):
                    # Full text. `[:300]` here was the only durable copy
                    # of what was said: working memory is an in-RAM
                    # deque and the `voice:<sid>` thread is the sole
                    # persistent home for a realtime transcript. The
                    # OpenAI path stores the whole thing through the
                    # identical call, so any utterance over ~300 chars
                    # was recoverable on one provider and truncated on
                    # the other.
                    await self._memory.conversation_append(
                        f"voice:{session_id}", "assistant", text,
                        source="voice_realtime_gemini",
                        title=f"Voice session {session_id[:8]}",
                    )
                    self._assert_callback_owner(session_id)
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
                self._assert_callback_owner(session_id)
            except Exception:
                logger.exception(
                    "gemini: note_voice_assistant_turn failed (non-fatal)"
                )

    async def _handle_input_transcript(self, session_id: str, text: str):
        self._assert_callback_owner(session_id)
        async with legacy_context_mutation(self._orchestrator, self._memory, (session_id,), "voice"):
            self._assert_callback_owner(session_id)
            return await self._checkpoint_legacy_handle_input_transcript(session_id=session_id, text=text)

    async def _checkpoint_legacy_handle_input_transcript(self, session_id: str, text: str):
        """Handle user-speech transcription returned by Gemini."""
        self._assert_callback_owner(session_id)
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
                        f"voice:{session_id}", "user", text,
                        source="voice_realtime_gemini",
                        title=f"Voice session {session_id[:8]}",
                    )
                    self._assert_callback_owner(session_id)
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
                self._assert_callback_owner(session_id)
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
                        self._assert_callback_owner(session_id)
                    except Exception:
                        logger.debug(
                            "gemini: send_text for active-subject hint failed",
                            exc_info=True,
                        )
            # Per-turn memory refresh for recall queries (Bug 2 B parity).
            try:
                from agents.orchestrator import Orchestrator as _Orch
                if _Orch._R_MEMORY.search(text) and self._memory:
                    _t = asyncio.create_task(
                        self._refresh_memory_context(session_id, text),
                        name="gemini-memory-refresh",
                    )
                    self._track_callback_task(_t)
                    self._bg_tasks.add(_t)
                    _t.add_done_callback(self._bg_tasks.discard)
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
                payload=voice_attempt_payload(payload, current_voice_attempt(session_id)),
            )
            await self._send_to_session(session_id, msg)
            self._assert_callback_owner(session_id)
        elif self._send_to_node:
            await self._send_to_node(gs.node_id, {"type": "transcript", "payload": voice_attempt_payload(payload, current_voice_attempt(session_id))})
            self._assert_callback_owner(session_id)

    async def _refresh_memory_context(self, session_id: str, query: str) -> None:
        """Pull the freshest memory context relevant to ``query`` and
        inject it into the Gemini live session via ``send_text``. Mirror
        of ``RealtimeProxy._refresh_memory_context``. Background-only,
        never raises."""
        self._assert_callback_owner(session_id)
        gs = self._sessions.get(session_id)
        if not gs or not self._memory:
            return
        try:
            ctx = await self._memory.build_context_for_llm(
                session_id, query=query, max_tokens_budget=600,
            )
            self._assert_callback_owner(session_id)
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
            self._assert_callback_owner(session_id)
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
        self._assert_callback_owner(session_id)
        try:
            async with legacy_context_mutation(self._orchestrator, self._memory, (session_id,), "voice"):
                self._assert_callback_owner(session_id)
                return await self._checkpoint_legacy_handle_tool_call(session_id=session_id, call_id=call_id, name=name, arguments=arguments)
        except RuntimeContextError as exc:
            return json.dumps({"success": False, "error": "Managed realtime tools are unavailable", "code": exc.code})

    async def _checkpoint_legacy_handle_tool_call(self, session_id: str, call_id: str, name: str, arguments: str) -> str:
        self._assert_callback_owner(session_id)
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
        self._assert_callback_owner(session_id)

        # PR9: see realtime_proxy._handle_tool_call — emit the chat
        # trace envelopes so voice tools render in the v2 ToolTrace.
        tool_call = {"name": name, "id": call_id, "args": args}
        t0 = time.time()
        if self._orchestrator is not None:
            try:
                await self._orchestrator._emit_tool_start(session_id, tool_call)
                self._assert_callback_owner(session_id)
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
            # Parity with RealtimeProxy: bind the call context so the
            # executor's approval gate sees the real session instead of
            # "", and so the call reaches the execution_log audit trail.
            with bind_context(
                session_id=session_id,
                surface="voice",
                tool_name=name,
                call_id=call_id,
            ):
                result = await self._skill_executor.execute(name, args, skill, endpoint)
                self._assert_callback_owner(session_id)

        if self._orchestrator is not None:
            latency_ms = (time.time() - t0) * 1000.0
            try:
                await self._orchestrator._emit_tool_result(
                    session_id, tool_call, result, latency_ms,
                )
                self._assert_callback_owner(session_id)
            except Exception:
                logger.exception("gemini voice tool_result emit failed")

        # Bug 2 (A) — Gemini parity with RealtimeProxy: persist a
        # voice-driven tool call as an episode anchored to the live
        # session so "what did my robot do?" surfaces it. See
        # ``RealtimeProxy._record_voice_tool_episode`` for the shape.
        try:
            await self._record_voice_tool_episode(session_id, name, args, result)
            self._assert_callback_owner(session_id)
        except Exception:
            logger.debug(
                "gemini: voice tool episode persistence skipped",
                exc_info=True,
            )

        return serialize_realtime_tool_result(name, result, registry=self._skill_registry)

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

        # B4: see the twin block in ``voice/realtime_proxy.py``. A byte
        # slice of serialized JSON leaves a row that ``json.loads``
        # rejects outright, so the whole detail is lost rather than its
        # tail. Shrink the structure instead.
        try:
            detail = serialize_for_storage({
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
            })
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
                # warning, not debug. A voice tool call leaves no other
                # trace: the audio is streamed straight to the client
                # and never enters handle_command_stream, so this
                # episode is the only record that the call happened.
                # The OpenAI twin was deliberately raised to warning for
                # exactly this reason; the change was never mirrored
                # here.
                logger.warning(
                    "gemini: episode_save for voice tool call raised",
                    exc_info=True,
                )

        try:
            _t = asyncio.create_task(_runner(), name="gemini-episode-save")
        except RuntimeError:
            await _runner()
        else:
            self._track_callback_task(_t)
            self._bg_tasks.add(_t)
            _t.add_done_callback(self._bg_tasks.discard)

    async def _handle_speech_started(self, session_id: str):
        self._assert_callback_owner(session_id)
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
                payload=voice_attempt_payload(payload, current_voice_attempt(session_id)),
            )
            await self._send_to_session(session_id, msg)
            self._assert_callback_owner(session_id)
            return
        if self._send_to_node:
            await self._send_to_node(gs.node_id, {
                "type": "speech_started", "payload": voice_attempt_payload(payload, current_voice_attempt(session_id)),
            })
            self._assert_callback_owner(session_id)

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
        self._assert_callback_owner(session_id)
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
                self._assert_callback_owner(session_id)
            except Exception:
                logger.exception(
                    "Fallback router refused gemini failure handoff"
                )
