"""
FERAL Realtime Voice Proxy — OpenAI Realtime API Bridge (GA)
=============================================================
The Brain opens one OpenAI Realtime WebSocket session per connected
phone/glasses node.  Phone sends PCM16 audio to the Brain; the Brain
relays it to OpenAI Realtime.  OpenAI sends audio back; the Brain
relays it to the phone.  Tool calls are intercepted and executed
locally through the full skill/hardware/memory ecosystem.

GA migration (PR #62 / Subagent A):
  - Removed ``OpenAI-Beta: realtime=v1`` header
  - Model default: ``gpt-realtime``
  - Session update carries ``type: "realtime"`` and GA audio config shape
  - Event names: ``response.output_audio.delta``,
    ``response.output_audio_transcript.delta``, ``response.output_text.delta``
  - Conversation item events: ``conversation.item.added``, ``conversation.item.done``
  - Content types: ``output_text``, ``output_audio`` (not legacy ``text``/``audio``)

The phone never talks to OpenAI directly — the Brain owns the context.
"""

from __future__ import annotations
import asyncio
import json
import logging
import os
import time
from typing import Optional, Callable, Awaitable, Any

from agents.tool_display import tool_feedback_text
from agents.tool_list import (
    OPENAI_TOOL_HARD_LIMIT,
    cap_tools_with_pins,
    openai_realtime_tool_choice,
    resolve_forced_tool_choice,
)
from voice.transcript_filter import should_commit_user_transcript

logger = logging.getLogger("feral.voice.openai")

OPENAI_REALTIME_URL = "wss://api.openai.com/v1/realtime"
DEFAULT_MODEL = "gpt-realtime"
SAMPLE_RATE = 24000
AUDIO_FORMAT = "pcm16"


def _resolve_openai_key() -> str:
    """Resolve the OpenAI API key for the realtime proxy.

    Routes through :func:`security.vault_keys.get_active_key` so a
    labeled-only key (``feral key add --provider openai --label <l>
    --set-active`` without also writing the legacy default-namespace
    entry) reaches the realtime WS handshake. ``get_active_key``
    internally cascades labeled-active → default-namespace vault →
    env, then degrades to ``""`` so the existing ``not self._api_key``
    guard in :meth:`RealtimeSession.connect` still triggers the
    ``openai_realtime_no_key`` fallback path when nothing is
    configured.

    Voice router parity: ``voice/router.py`` already routes its
    OpenAI key reads through ``get_active_key`` (commit ``01eda5d9``).
    The proxy was the missing surface — pre-fix, a labeled-only key
    failed at ``RealtimeProxy.__init__`` time (where the os.getenv
    read snapshotted ``""``) and every voice session started with no
    auth header even though chat worked.
    """
    try:
        from security.vault_keys import get_active_key

        return get_active_key("openai") or ""
    except Exception:
        return os.getenv("OPENAI_API_KEY", "") or ""


class RealtimeSession:
    """
    Manages a single OpenAI Realtime WebSocket session on behalf of a
    connected phone/glasses node.  Handles audio relay, tool interception,
    and perception context injection.

    GA API: no beta header, new event names, session.type = "realtime".
    """

    def __init__(
        self,
        session_id: str,
        node_id: str,
        *,
        api_key: str = "",
        model: str = DEFAULT_MODEL,
        voice: str = "marin",
        input_sample_rate: int = SAMPLE_RATE,
        output_sample_rate: int = SAMPLE_RATE,
        language_hint: str = "",
        system_prompt: str = "",
        tools: list[dict] | None = None,
        on_audio_delta: Callable[[str, str, bool], Awaitable[None]] | None = None,
        on_transcript: Callable[[str, str, bool], Awaitable[None]] | None = None,
        on_tool_call: Callable[[str, str, str, str], Awaitable[str]] | None = None,
        on_speech_started: Callable[[str], Awaitable[None]] | None = None,
        on_error: Callable[[str, str], Awaitable[None]] | None = None,
        on_conversation_item: Callable[[str, dict], Awaitable[None]] | None = None,
    ):
        self.session_id = session_id
        self.node_id = node_id
        self._api_key = api_key or _resolve_openai_key()
        self._model = model
        self._voice = voice
        self._input_sample_rate = int(input_sample_rate or SAMPLE_RATE)
        self._output_sample_rate = int(output_sample_rate or SAMPLE_RATE)
        self._language_hint = self._normalize_language_hint(language_hint)
        self._system_prompt = system_prompt
        self._tools = tools or []

        self._on_audio_delta = on_audio_delta
        self._on_transcript = on_transcript
        self._on_tool_call = on_tool_call
        self._on_speech_started = on_speech_started
        # GA Realtime response lifecycle tracking. Flips True on
        # `response.created`, False on `response.done`. Prevents the
        # cancel_response no-op error spam when VAD fires on initial
        # speech (no response yet) or after a response completed.
        self._response_in_progress = False
        self._on_error = on_error
        self._on_conversation_item = on_conversation_item

        self._ws = None
        self._connected = False
        self._recv_task: Optional[asyncio.Task] = None
        self._pending_tool_calls: dict[str, dict] = {}
        # When set, the next tool result resets tool_choice back to auto.
        self._active_force_tool: str = ""
        # Tools executed during the current user speech turn — used to
        # skip redundant force_tool_for_turn when the transcript hook
        # arrives after VAD already drove the correct tool call.
        self._turn_tools_executed: set[str] = set()

    @staticmethod
    def _normalize_language_hint(language_hint: str) -> str:
        """Convert browser/phone locale hints (e.g. en-US) to whisper locale."""
        if not language_hint or not isinstance(language_hint, str):
            return ""
        raw = language_hint.strip()
        if not raw:
            return ""
        primary = raw.split("-", 1)[0].split("_", 1)[0].strip().lower()
        if primary and primary.isalpha():
            return primary
        return ""

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self):
        """Open a WebSocket to the OpenAI Realtime API (GA — no beta header)."""
        if not self._api_key:
            logger.error("Cannot start realtime session — no OPENAI_API_KEY")
            return

        try:
            url = f"{OPENAI_REALTIME_URL}?model={self._model}"
            headers = {
                "Authorization": f"Bearer {self._api_key}",
            }
            # `_connect_with_retry` handles the cross-version
            # `websockets` kwarg dance (14.x removed the legacy
            # `extra_headers` entrypoint, 13.x has both). We pass the
            # new-style `additional_headers` here; the helper translates
            # to `extra_headers` if running against legacy. Pinned by
            # tests/test_voice_realtime_headers.py.
            self._ws = await self._connect_with_retry(
                url,
                additional_headers=headers,
                max_size=10 * 1024 * 1024,
                ping_interval=20,
            )
            self._connected = True
            self._recv_task = asyncio.create_task(self._receive_loop())
            logger.info(f"Realtime session opened: {self.session_id} / node={self.node_id}")
        except Exception as e:
            logger.error(f"Failed to connect to OpenAI Realtime: {e}")
            self._connected = False

    @staticmethod
    async def _connect_with_retry(url, **kwargs):
        # Cross-version websockets compatibility: 13.x ships both the
        # legacy `websockets.connect` (kwarg: `extra_headers`) AND the
        # newer `websockets.asyncio.client.connect` (kwarg:
        # `additional_headers`). 14.x+ removed the legacy entrypoint.
        # Use the asyncio client when available so the code works on
        # both lines without runtime guesswork; fall back to legacy
        # only if the import chain doesn't expose it. Caller passes
        # the new-style `additional_headers` kwarg.
        try:
            from websockets.asyncio.client import connect as _ws_connect
        except ImportError:
            import websockets as _ws
            _ws_connect = _ws.connect
            # Old-line callers expect `extra_headers`. Translate.
            if "additional_headers" in kwargs and "extra_headers" not in kwargs:
                kwargs["extra_headers"] = kwargs.pop("additional_headers")
        for attempt in range(3):
            try:
                return await _ws_connect(url, **kwargs)
            except Exception:
                if attempt == 2:
                    raise
                logger.warning("OpenAI WS connect failed (attempt %d/3) — retrying", attempt + 1)
                await asyncio.sleep(2 ** attempt)

    @staticmethod
    def _feral_tools_to_openai(tools: list[dict]) -> list[dict]:
        """Convert registry OpenAI-shape defs to Realtime GA flat tool objects."""
        openai_tools = []
        for t in tools:
            fn = t.get("function", {}) if isinstance(t.get("function"), dict) else {}
            name = fn.get("name") or t.get("name") or ""
            openai_tools.append({
                "type": "function",
                "name": name,
                "description": fn.get("description") or t.get("description") or "",
                "parameters": fn.get("parameters") or t.get("parameters") or {},
            })
        return openai_tools

    @staticmethod
    def _ga_session_update(**session_fields) -> dict:
        """Build a GA ``session.update`` — ``session.type`` is required."""
        return {
            "type": "session.update",
            "session": {"type": "realtime", **session_fields},
        }

    async def configure(
        self,
        system_prompt: str = "",
        tools: list[dict] | None = None,
        *,
        force_tool: str = "",
    ):
        """Send session.update with GA session shape (type='realtime', audio config)."""
        if not self._connected:
            return

        self._system_prompt = system_prompt or self._system_prompt
        if tools is not None:
            self._tools = tools

        capped = cap_tools_with_pins(self._tools, max_tools=OPENAI_TOOL_HARD_LIMIT)
        openai_tools = self._feral_tools_to_openai(capped)
        tool_choice = resolve_forced_tool_choice(capped, force_tool or None)

        transcription_cfg = {"model": "whisper-1"}
        if self._language_hint:
            transcription_cfg["language"] = self._language_hint

        session_update = self._ga_session_update(
            model=self._model,
            output_modalities=["audio"],
            instructions=self._system_prompt,
            audio={
                "input": {
                    "format": {"type": "audio/pcm", "rate": self._input_sample_rate},
                    "transcription": transcription_cfg,
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": 0.5,
                        "prefix_padding_ms": 300,
                        # Bumped 800→1000ms to reduce VAD splits on
                        # trailing HFP silence — empirically this
                        # also lowers (but does not eliminate) the
                        # rate of whisper-1 hallucinated closers.
                        # The blocklist gate above is the authoritative
                        # filter; this just gives the user another
                        # ~200ms of natural pause before a commit.
                        # Latency tradeoff: voice replies start
                        # ~200ms later when the user trails off, but
                        # interruption / barge-in latency is
                        # unchanged (driven by speech_started, not
                        # silence_duration_ms).
                        "silence_duration_ms": 1000,
                    },
                },
                "output": {
                    "format": {"type": "audio/pcm", "rate": self._output_sample_rate},
                    "voice": self._voice,
                },
            },
            tools=openai_tools,
            tool_choice=tool_choice,
            # NOTE: GA Realtime API no longer accepts
            # `session.temperature` at the session.update path —
            # live test surfaced:
            #   "Unknown parameter: 'session.temperature'"
            # which silently broke session configuration so the
            # model never responded. `max_output_tokens` is still
            # accepted at session scope as of GA 2025-11.
            max_output_tokens=4096,
        )

        await self._send(session_update)
        logger.info(
            "Realtime session configured (GA): %d tools (from %d raw), voice=%s%s",
            len(openai_tools),
            len(self._tools),
            self._voice,
            f", force_tool={force_tool}" if force_tool else "",
        )

    async def force_tool_for_turn(self, tool_name: str) -> None:
        """Pin ``tool_choice`` to one function and (re)start the model turn.

        Realtime server VAD usually fires ``response.create`` before the
        user transcript reaches us, so schedule-intent forcing happens here
        on the transcript hook — cancel any in-flight response, update
        ``tool_choice``, then create a fresh response.
        """
        if not self._connected or not tool_name:
            return
        if tool_name in self._turn_tools_executed:
            logger.info(
                "realtime: skip force %s — already executed this turn session=%s",
                tool_name, self.session_id,
            )
            return
        self._active_force_tool = tool_name
        await self._send(self._ga_session_update(
            tool_choice=openai_realtime_tool_choice(tool_name),
        ))
        await self.cancel_response()
        await self._send({"type": "response.create"})

    async def reset_tool_choice(self) -> None:
        """Restore free tool selection after a one-shot forced turn."""
        if not self._connected:
            return
        self._active_force_tool = ""
        await self._send(self._ga_session_update(tool_choice="auto"))

    async def send_audio(self, audio_b64: str):
        """Relay PCM16 audio from the phone to OpenAI Realtime."""
        if not self._connected:
            return
        t0 = time.monotonic()
        await self._send({
            "type": "input_audio_buffer.append",
            "audio": audio_b64,
        })
        logger.debug("audio_chunk sent session=%s latency_ms=%.1f", self.session_id, (time.monotonic() - t0) * 1000)

    async def send_text(self, text: str):
        """Send a text message into the realtime conversation."""
        if not self._connected:
            return
        await self._send({
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        })
        await self._send({"type": "response.create"})

    async def send_tool_result(self, call_id: str, result: str):
        """Return a tool execution result to OpenAI and continue the response."""
        await self._send({
            "type": "conversation.item.create",
            "item": {
                "type": "function_call_output",
                "call_id": call_id,
                "output": result,
            },
        })
        if self._active_force_tool:
            await self.reset_tool_choice()
        await self._send({"type": "response.create"})

    async def cancel_response(self):
        """Cancel the current response (e.g., user interrupted).

        Guarded: only send response.cancel when a response is actually
        in flight. Otherwise OpenAI returns
           "Cancellation failed: no active response found"
        which floods the log and hints that VAD-triggered cancellations
        are firing before any response was generated. We track the
        in_progress flag via response.created / response.done events
        on this session.
        """
        if not self._connected:
            return
        if not getattr(self, "_response_in_progress", False):
            # No-op. Don't waste a round-trip or generate error spam.
            return
        await self._send({"type": "response.cancel"})

    async def inject_context(self, context_text: str):
        """Inject updated perception context as a system-level message."""
        if not self._connected or not context_text:
            return
        await self._send({
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "system",
                "content": [{"type": "input_text", "text": context_text}],
            },
        })

    async def disconnect(self):
        """Gracefully close the realtime session."""
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
        logger.info(f"Realtime session closed: {self.session_id}")

    async def _send(self, event: dict):
        if self._ws and self._connected:
            try:
                await self._ws.send(json.dumps(event))
            except Exception as e:
                # Symmetry with ``_receive_loop`` below: a failed send is
                # just as terminal as a failed receive, so it must reach
                # ``RealtimeProxy._handle_error`` for classification and
                # fallback. Before this, a send failure only flipped
                # ``_connected = False`` while leaving the session
                # registered in ``_sessions`` / ``_node_to_session``.
                # ``VoiceRouter`` then found a non-None session, skipped
                # re-opening, and dropped every subsequent audio chunk on
                # the ``rs.connected`` gate — voice went dead with no
                # error, no voice_status frame, and no recovery short of
                # a brain restart (operator report 2026-07: "the voice
                # collapses").
                logger.error(f"Realtime send error: {e}")
                self._connected = False
                if self._on_error:
                    try:
                        await self._on_error(self.session_id, str(e))
                    except Exception:
                        logger.exception("Realtime on_error callback failed")

    async def _receive_loop(self):
        """Process incoming events from OpenAI Realtime."""
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
            # OpenAI Realtime signals out-of-credit by closing the WS
            # with WebSocket code 1013 + reason "insufficient_quota".
            # Hand the raw exception text to the error callback so
            # ``RealtimeProxy._handle_error`` can classify and trigger
            # the voice_status -> whisper-TTS fallback path. Without
            # this routing the receive loop just sets ``_connected =
            # False`` and the session goes silent forever (operator
            # report 2026-05-18). Pinned by
            # tests/test_voice_realtime_quota_fallback.py.
            logger.error(f"Realtime receive error: {e}")
            self._connected = False
            if self._on_error:
                try:
                    await self._on_error(self.session_id, str(e))
                except Exception:
                    logger.exception("Realtime on_error callback failed")

    async def _handle_event(self, event: dict):
        event_type = event.get("type", "")

        if event_type == "session.created":
            logger.info("Realtime session.created — configuring...")
            await self.configure()

        # --- GA event names (output_audio / output_audio_transcript / output_text) ---

        elif event_type == "response.output_audio.delta":
            delta_b64 = event.get("delta", "")
            if delta_b64 and self._on_audio_delta:
                await self._on_audio_delta(self.session_id, delta_b64, False)

        elif event_type == "response.output_audio.done":
            if self._on_audio_delta:
                await self._on_audio_delta(self.session_id, "", True)

        elif event_type == "response.output_audio_transcript.delta":
            text = event.get("delta", "")
            if text and self._on_transcript:
                await self._on_transcript(self.session_id, text, False)

        elif event_type == "response.output_audio_transcript.done":
            text = event.get("transcript", "")
            if text and self._on_transcript:
                await self._on_transcript(self.session_id, text, True)

        elif event_type == "response.output_text.delta":
            text = event.get("delta", "")
            if text and self._on_transcript:
                await self._on_transcript(self.session_id, text, False)

        elif event_type == "response.output_text.done":
            text = event.get("text", "")
            if text and self._on_transcript:
                await self._on_transcript(self.session_id, text, True)

        # --- Conversation item events (GA) ---

        elif event_type == "conversation.item.added":
            item = event.get("item", {})
            if self._on_conversation_item:
                await self._on_conversation_item(self.session_id, {"action": "added", "item": item})

        elif event_type == "conversation.item.done":
            item = event.get("item", {})
            if self._on_conversation_item:
                await self._on_conversation_item(self.session_id, {"action": "done", "item": item})

        # --- Input audio transcription (unchanged) ---

        elif event_type == "conversation.item.input_audio_transcription.completed":
            text = event.get("transcript", "")
            # Bug 3 (phantom commit gate): drop whisper-1's stock
            # closers ("Bye-bye.", "Thank you.", "Thanks for watching.",
            # bare "You.", etc.) BEFORE they reach the proxy callback,
            # so the orchestrator never sees a phantom user turn. Real
            # short commands ("yes", "stop", "halt") still go through —
            # see ``voice/transcript_filter.py`` for the allowlist.
            if not should_commit_user_transcript(text):
                logger.info(
                    "realtime: dropping phantom user transcript session=%s text=%r",
                    self.session_id, text,
                )
                return
            if text and self._on_transcript:
                await self._on_transcript(self.session_id, f"[user] {text}", True)

        elif event_type == "input_audio_buffer.speech_started":
            self._turn_tools_executed.clear()
            if self._on_speech_started:
                await self._on_speech_started(self.session_id)

        elif event_type == "response.function_call_arguments.done":
            call_id = event.get("call_id", "")
            name = event.get("name", "")
            arguments = event.get("arguments", "{}")
            logger.info(f"Realtime tool call: {name} (call_id={call_id})")

            if self._on_tool_call:
                result = await self._on_tool_call(self.session_id, call_id, name, arguments)
                if name:
                    self._turn_tools_executed.add(name)
                await self.send_tool_result(call_id, result)

        elif event_type == "error":
            err = event.get("error", {})
            msg = err.get("message", str(err))
            self._response_in_progress = False
            # The "no active response" cancel race is benign and
            # frequent: VAD turn-detection fires `response.cancel`
            # while OpenAI's state has already advanced past
            # response.done (our local `_response_in_progress` flag
            # races behind the OpenAI server). Demoted to INFO so it
            # doesn't spam the operator log; we DO NOT call `on_error`
            # for this case because it's not actionable.
            benign_cancel_race = (
                "Cancellation failed" in msg
                and "no active response" in msg
            )
            if benign_cancel_race:
                logger.info(
                    "Realtime cancel race (benign): %s session=%s",
                    msg, self.session_id,
                )
            else:
                logger.error(f"Realtime API error: {msg}")
                if self._on_error:
                    await self._on_error(self.session_id, msg)

        elif event_type == "response.created":
            self._response_in_progress = True
            logger.info("Realtime response.created session=%s", self.session_id)

        elif event_type == "response.done":
            self._response_in_progress = False
            # Surface the response's status so we can tell if OpenAI
            # is rejecting our requests silently (e.g. "content_filter",
            # "failed", "incomplete"). A healthy response is "completed".
            resp = event.get("response", {})
            status = resp.get("status", "")
            status_details = resp.get("status_details", {})
            if status and status != "completed":
                logger.warning(
                    "Realtime response.done session=%s status=%s details=%s",
                    self.session_id, status, status_details,
                )
            else:
                logger.info(
                    "Realtime response.done session=%s status=%s",
                    self.session_id, status or "(unknown)",
                )

        elif event_type in {"response.failed", "response.cancelled", "response.canceled"}:
            self._response_in_progress = False
            logger.warning("Realtime %s session=%s", event_type, self.session_id)

        elif event_type == "rate_limits.updated":
            pass

        else:
            # Catch-all: log unknown/unhandled event types so we can
            # see WHAT the server sent if voice still doesn't produce
            # audio after the guard above.
            logger.debug(
                "Realtime unhandled event type=%s session=%s",
                event_type, self.session_id,
            )


class RealtimeProxy:
    """
    Manages all active RealtimeSession instances — one per phone/glasses node.
    Provides the high-level interface that the Brain server uses.
    """

    def __init__(
        self,
        *,
        skill_registry=None,
        skill_executor=None,
        memory=None,
        perception=None,
        send_to_node: Callable[[str, dict], Awaitable[None]] | None = None,
        send_to_session: Callable[[str, Any], Awaitable[None]] | None = None,
        identity_workspace=None,
        orchestrator=None,
        voice: str = "marin",
    ):
        self._sessions: dict[str, RealtimeSession] = {}
        self._node_to_session: dict[str, str] = {}
        self._skill_registry = skill_registry
        self._skill_executor = skill_executor
        self._memory = memory
        self._perception = perception
        self._send_to_node = send_to_node
        self._send_to_session = send_to_session
        # PR9: voice tool executions used to surface only as a free-form
        # transcript line, never as the same `tool_start`/`tool_result`
        # envelopes the chat path emits. The chat client buffers those
        # envelopes into the expandable ToolTrace component; without
        # them, voice-driven tools were invisible in the chat record.
        # Holding the orchestrator lets the proxy re-use the *same*
        # emit helpers so voice and chat now produce identical traces.
        self._orchestrator = orchestrator
        self._api_key = _resolve_openai_key()
        self._voice = voice

        # Fallback coordination — populated by VoiceRouter at construction
        # so RealtimeProxy can punt to whisper TTS when OpenAI Realtime
        # closes with a quota / rate-limit / auth error. Without this
        # hook the receive loop just dies silent (operator report
        # 2026-05-18). Set via ``attach_fallback_router``.
        self._fallback_router = None

        from voice.personality import VoicePersonality
        self._voice_personality = VoicePersonality(identity_workspace=identity_workspace)

    def attach_fallback_router(self, router) -> None:
        """Inject the parent VoiceRouter so error handling can punt to
        the whisper TTS fallback when Realtime dies (e.g. quota).

        Separate setter (instead of constructor arg) because the
        router is built AFTER the proxy in ``api/state.py`` — circular
        dependency at construction. Pinned by
        ``tests/test_voice_realtime_quota_fallback.py``.
        """
        self._fallback_router = router

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    def get_session(self, node_id: str) -> Optional[RealtimeSession]:
        sid = self._node_to_session.get(node_id)
        return self._sessions.get(sid) if sid else None

    async def start_session(
        self,
        session_id: str,
        node_id: str,
        model: str = DEFAULT_MODEL,
        voice: str = "",
        input_sample_rate: int = SAMPLE_RATE,
        language_hint: str = "",
    ) -> RealtimeSession:
        """Create and connect a new realtime session for a phone/glasses node."""
        system_prompt = await self._build_system_prompt(session_id)
        tools = self._get_tools()

        rs = RealtimeSession(
            session_id=session_id,
            node_id=node_id,
            api_key=self._api_key,
            model=model,
            voice=voice or self._voice,
            input_sample_rate=input_sample_rate,
            output_sample_rate=SAMPLE_RATE,
            language_hint=language_hint,
            system_prompt=system_prompt,
            tools=tools,
            on_audio_delta=self._handle_audio_delta,
            on_transcript=self._handle_transcript,
            on_tool_call=self._handle_tool_call,
            on_speech_started=self._handle_speech_started,
            on_error=self._handle_error,
            on_conversation_item=self._handle_conversation_item,
        )

        await rs.connect()
        if not getattr(rs, 'connected', False) and not getattr(rs, '_ws', None):
            logger.warning("Voice session failed to connect for %s", session_id)
            # Lane 05  (AUDIT-r14 finding 15 fix #2): pre-fix the
            # connect-time failure path returned None silently and the
            # phone never saw a ``voice_status`` frame — the user got
            # dead air with no banner. Now we route the failure through
            # the same fallback router that runtime errors use, with a
            # synthetic ``openai_realtime_connect`` reason so the UI
            # can distinguish "couldn't even open the WS" from "WS
            # opened but quota tripped mid-call".
            if not self._api_key:
                reason = "openai_realtime_no_key"
                detail = "OPENAI_API_KEY not configured"
            else:
                reason = "openai_realtime_connect"
                detail = "OpenAI Realtime WS handshake failed"
            if self._fallback_router:
                try:
                    await self._fallback_router.handle_realtime_failure(
                        session_id=session_id,
                        reason=reason,
                        detail=detail,
                    )
                except Exception:
                    logger.exception(
                        "Fallback router refused realtime connect failure"
                    )
            return None
        self._sessions[session_id] = rs
        self._node_to_session[node_id] = session_id

        try:
            from api.state import state
            if state.orchestrator:
                for sid in list(state.sessions.keys()):
                    await state.orchestrator._emit_brain_event(sid, "voice_session", {
                        "active": True, "provider": "openai", "session_id": session_id,
                    })
        except Exception:
            pass

        return rs

    async def stop_session(self, session_id: str):
        rs = self._sessions.pop(session_id, None)
        if rs:
            node_id = rs.node_id
            await rs.disconnect()
            self._node_to_session.pop(node_id, None)

            try:
                from api.state import state
                if state.orchestrator:
                    for sid in list(state.sessions.keys()):
                        await state.orchestrator._emit_brain_event(sid, "voice_session", {
                            "active": False, "provider": "openai", "session_id": session_id,
                        })
            except Exception:
                pass

    async def relay_audio(self, node_id: str, audio_b64: str):
        """Relay audio from a phone node to its OpenAI Realtime session."""
        rs = self.get_session(node_id)
        if rs and rs.connected:
            await rs.send_audio(audio_b64)

    async def relay_text(self, node_id: str, text: str):
        """Send text into a node's realtime session."""
        rs = self.get_session(node_id)
        if rs and rs.connected:
            await rs.send_text(text)

    async def update_context(self, node_id: str, session_id: str):
        """Inject fresh perception context into the realtime session."""
        rs = self.get_session(node_id)
        if not rs or not rs.connected:
            return
        if self._perception:
            frame = self._perception.get_frame(session_id)
            ctx = frame.to_system_context()
            if ctx:
                await rs.inject_context(f"[Updated sensor/environment context]\n{ctx}")

    async def shutdown(self):
        for sid in list(self._sessions):
            await self.stop_session(sid)

    async def _build_system_prompt(self, session_id: str) -> str:
        user_name = ""
        recent_context = ""
        if self._memory:
            history = self._memory.working_get(session_id, limit=3)
            snippets = [
                e.get("text", "")[:80]
                for e in history
                if e.get("role") == "user" and e.get("text")
            ]
            if snippets:
                recent_context = " | ".join(snippets)

        try:
            from config.loader import feral_home
            user_md = feral_home() / "USER.md"
            if user_md.exists():
                for line in user_md.read_text().splitlines():
                    stripped = line.strip().lstrip("#").strip()
                    if stripped and stripped.lower().startswith("name:"):
                        user_name = stripped.split(":", 1)[1].strip()
                        break
        except Exception:
            pass

        tod = self._voice_personality.current_time_of_day()
        personality_block = self._voice_personality.get_voice_instructions(
            time_of_day=tod,
            user_name=user_name,
            recent_context=recent_context,
        )

        parts = [personality_block]
        parts.append(
            "\nLanguage policy: Always respond in English unless the user clearly "
            "starts speaking in another language first."
        )

        parts.append(
            "\nYou have access to the user's physical environment through "
            "smart glasses, phone sensors, and connected devices. "
            "You can see what the user sees, monitor their health, "
            "control smart home devices, search the web, manage notes, and more."
        )

        if self._perception:
            frame = self._perception.get_frame(session_id)
            ctx = frame.to_system_context()
            if ctx:
                parts.append(f"\n[Current Environment]\n{ctx}")

        if self._memory:
            mem_ctx = await self._memory.build_context_for_llm(session_id, max_tokens_budget=500)
            if mem_ctx:
                parts.append(f"\n[Memory Context]\n{mem_ctx}")

        return "\n".join(parts)

    def _get_tools(self) -> list[dict]:
        if self._skill_registry:
            raw = self._skill_registry.get_all_tools()
            return cap_tools_with_pins(raw, max_tools=OPENAI_TOOL_HARD_LIMIT)
        return []

    @staticmethod
    def _tool_feedback_text(tool_name: str) -> str:
        """Generate natural spoken feedback while a tool is executing."""
        return tool_feedback_text(tool_name)

    async def _send_tool_feedback(self, session_id: str, text: str):
        """Send transcript-style progress feedback to active voice clients."""
        if not text:
            return
        rs = self._sessions.get(session_id)
        if not rs:
            return

        if rs.node_id.startswith("webclient_") and self._send_to_session:
            from models.protocol import FeralMessage, TranscriptPayload

            msg = FeralMessage(
                session_id=session_id,
                hop="brain",
                type="transcript",
                payload=TranscriptPayload(
                    text=text,
                    role="assistant",
                    is_partial=False,
                ).model_dump(),
            )
            await self._send_to_session(session_id, msg)
            return

        if self._send_to_node:
            await self._send_to_node(rs.node_id, {
                "type": "transcript",
                "payload": {"text": text, "role": "assistant", "is_partial": False},
            })

    async def _handle_audio_delta(self, session_id: str, audio_b64: str, is_done: bool):
        """Forward audio from OpenAI back to the connected client (web or daemon node)."""
        rs = self._sessions.get(session_id)
        if not rs:
            return

        payload = {
            "data_b64": audio_b64,
            "encoding": AUDIO_FORMAT,
            "sample_rate": getattr(rs, "_output_sample_rate", SAMPLE_RATE),
            "is_final": is_done,
        }

        # Defense against the post-disconnect send race (operator
        # report 2026-05-08): when the phone closes its WS while
        # OpenAI is still streaming `response.output_audio.delta`
        # events, the underlying starlette WebSocket raises
        # ``RuntimeError: Cannot call "send" once a close message has
        # been sent`` and uvicorn complains about ``Unexpected ASGI
        # message 'websocket.send', after sending 'websocket.close'``.
        # Catch + tear down the OpenAI side so the OpenAI WS doesn't
        # keep paying for tokens we can't deliver. Pinned by
        # tests/test_voice_realtime_post_close.py.
        try:
            if rs.node_id.startswith("webclient_") and self._send_to_session:
                from models.protocol import FeralMessage
                msg = FeralMessage(
                    session_id=session_id, hop="brain", type="audio_response",
                    payload=payload,
                )
                await self._send_to_session(session_id, msg)
            elif self._send_to_node:
                await self._send_to_node(rs.node_id, {"type": "audio_response", "payload": payload})
        except (RuntimeError, ConnectionError) as exc:
            # Most likely the downstream WS is gone. Tear down the
            # session so the OpenAI socket stops streaming and we
            # don't log this on every subsequent chunk.
            logger.warning(
                "voice_audio_forward dropped: downstream WS closed for "
                "session=%s node=%s err=%s — closing realtime session.",
                session_id, rs.node_id, exc,
            )
            await self.stop_session(session_id)

    async def _handle_transcript(self, session_id: str, text: str, is_final: bool):
        """Store transcripts in memory and forward to both client sessions and daemon nodes."""
        if is_final and text and self._memory:
            if text.startswith("[user] "):
                self._memory.working_push(session_id, {
                    "role": "user", "text": text[7:], "source": "voice_realtime",
                })
            else:
                self._memory.working_push(session_id, {
                    "role": "assistant", "text": text[:300], "source": "voice_realtime",
                })
            # PR 9 gap-fill — also persist into the durable conversations
            # store under a voice-scoped thread so the conversation list
            # surfaces voice sessions next to chat threads. We key the
            # thread on the realtime session id (not the WS session) so a
            # reconnect of the same realtime session keeps appending to
            # the same thread.
            try:
                conv_id = f"voice:{session_id}"
                role = "user" if text.startswith("[user] ") else "assistant"
                clean_for_store = text[len("[user] "):] if text.startswith("[user] ") else text
                if hasattr(self._memory, "conversation_append"):
                    await self._memory.conversation_append(
                        conv_id, role, clean_for_store,
                        source="voice_realtime_openai",
                        title=f"Voice session {session_id[:8]}",
                    )
            except Exception as exc:
                logger.debug("voice transcript persistence skipped: %s", exc)

        # Bug 1 + Bug 2(B) hook: hand the final USER transcript to the
        # orchestrator so coref tracking, conversation_history, and
        # temporal-recall side-channels stay in sync with the text
        # path — the audio path bypasses ``handle_command_stream`` so
        # this is the only seam where voice can touch that state.
        #
        # Realtime timing caveat: by the time we receive this event,
        # server VAD has likely already triggered ``response.create`` on
        # the OpenAI side, so the model is mid-generation when we
        # inject ``context_hint``. We still attempt the inject for the
        # CURRENT turn (it's a no-op if the response has already
        # completed) and the active-subject tracker IS authoritative
        # for the NEXT turn either way — so the "what about now"
        # follow-up resolves correctly from turn N+1 onward, and
        # opportunistically from turn N when the response is still
        # in flight.
        if (
            is_final
            and text
            and text.startswith("[user] ")
            and self._orchestrator is not None
        ):
            clean = text[len("[user] "):]
            voice_tools = self._get_tools()
            try:
                hook_out = await self._orchestrator.note_voice_user_turn(
                    session_id, clean, emit_temporal_timeline=True,
                    tools=voice_tools,
                )
            except Exception:
                logger.exception(
                    "realtime: note_voice_user_turn failed (non-fatal)"
                )
                hook_out = {}

            forced_tool = (hook_out or {}).get("forced_tool") or ""
            if forced_tool:
                rs_for_force = self._sessions.get(session_id)
                if rs_for_force is not None:
                    try:
                        await rs_for_force.force_tool_for_turn(forced_tool)
                        logger.info(
                            "realtime: forced %s for schedule intent session=%s",
                            forced_tool, session_id,
                        )
                    except Exception:
                        logger.exception(
                            "realtime: force_tool_for_turn failed (non-fatal)"
                        )

            context_hint = (hook_out or {}).get("context_hint") or ""
            if context_hint:
                rs = self._sessions.get(session_id)
                if rs is not None:
                    try:
                        await rs.inject_context(context_hint)
                    except Exception:
                        logger.debug(
                            "realtime: inject_context for active-subject hint failed",
                            exc_info=True,
                        )

            # Bug 2(B) — voice recall parity: when the utterance is a
            # memory / temporal-recall query, the system prompt's
            # ``[Memory Context]`` block was built ONCE at session
            # start with no query, so episodes from earlier in the
            # session never surface. Refresh per-turn via
            # ``build_context_for_llm(session_id, query=clean)`` and
            # inject the (cheap) result so the model has the relevant
            # episodes when it generates the next reply.
            #
            # Fire-and-forget on a background task — the build is
            # async-safe but pulls from SQLite + vec index, so we don't
            # want it on the hot transcript path. Realtime timing:
            # this lands as a second ``conversation.item.create`` after
            # the user's transcript is already in flight, so the
            # *current* turn is best-effort; the NEXT turn is
            # authoritative. Same caveat as the active-subject hint.
            try:
                from agents.orchestrator import Orchestrator as _Orch
                if _Orch._R_MEMORY.search(clean) and self._memory:
                    asyncio.create_task(
                        self._refresh_memory_context(session_id, clean)
                    )
            except Exception:
                logger.debug(
                    "realtime: per-turn memory refresh schedule failed",
                    exc_info=True,
                )

        if is_final and text:
            rs = self._sessions.get(session_id)

            # The ``[user] `` prefix is an INTERNAL sentinel set by the
            # ``input_audio_transcription.completed`` handler so this
            # callback can disambiguate user-spoken vs assistant-spoken
            # transcripts (OpenAI's Realtime SDK funnels both into the
            # same ``response.output_audio_transcript.*`` event family
            # this code path forwards). The sentinel must be stripped
            # before any wire emit, otherwise iOS / web render
            # ``"[user] Hello"`` as visible bubble text. Pinned by
            # tests/test_voice_transcript_role_wire.py.
            is_user = text.startswith("[user] ")
            clean_text = text[len("[user] "):] if is_user else text
            wire_role = "user" if is_user else "assistant"

            # Routing contract (operator report 2026-05-09: every
            # voice turn rendered TWICE in the iOS chat). The fan-out
            # is web-OR-node, NOT both — same shape as
            # ``_handle_audio_delta``. iPhone is a daemon node so it
            # gets ``_send_to_node``; web clients get
            # ``_send_to_session``. The prior code fired both branches
            # unconditionally and the iPhone received each transcript
            # via two parallel WS paths (session + node), then the
            # ChatStore polling-mirror ingested both copies.
            #
            # Same post-disconnect guard as ``_handle_audio_delta`` —
            # a transcript can land after the phone has closed its WS
            # (response.output_audio_transcript.done arrives later than
            # the audio deltas), and writing into a closed
            # WebSocket raises ``RuntimeError`` from starlette.
            try:
                is_web_client = bool(rs and rs.node_id.startswith("webclient_"))
                if is_web_client and self._send_to_session:
                    from models.protocol import FeralMessage, TranscriptPayload
                    msg = FeralMessage(
                        session_id=session_id, hop="brain", type="transcript",
                        payload=TranscriptPayload(
                            text=clean_text,
                            role=wire_role,
                            is_partial=not is_final,
                        ).model_dump(),
                    )
                    await self._send_to_session(session_id, msg)
                elif rs and self._send_to_node:
                    # Audit-r8 brief #08 HIGH fix: node path was
                    # hardcoding `is_partial: False`, contradicting the
                    # web path which used `not is_final`. Asymmetry
                    # meant iOS clients always rendered the final
                    # variant even on partial deltas; partial text was
                    # treated as committed. Match the web path.
                    await self._send_to_node(rs.node_id, {
                        "type": "transcript",
                        "payload": {
                            "text": clean_text,
                            "role": wire_role,
                            "is_partial": not is_final,
                        },
                    })
            except (RuntimeError, ConnectionError) as exc:
                logger.warning(
                    "voice_transcript_forward dropped: downstream WS closed for "
                    "session=%s err=%s — closing realtime session.",
                    session_id, exc,
                )
                await self.stop_session(session_id)

    async def _refresh_memory_context(self, session_id: str, query: str) -> None:
        """Pull the freshest memory context relevant to ``query`` and
        inject it into the realtime session as a ``[Memory Context]``
        system frame. Background-only; never raises.

        Used by the live-voice recall path so a question like "what did
        my robot do yesterday?" sees the episodes saved by earlier voice
        tool calls in this session, instead of the stale once-per-session
        context built when the realtime WS was opened.
        """
        rs = self._sessions.get(session_id)
        if not rs or not self._memory:
            return
        try:
            ctx = await self._memory.build_context_for_llm(
                session_id, query=query, max_tokens_budget=600,
            )
        except Exception:
            logger.debug(
                "realtime: build_context_for_llm failed for refresh",
                exc_info=True,
            )
            return
        if not ctx:
            return
        try:
            await rs.inject_context(f"[Memory Context — query: {query[:80]}]\n{ctx}")
        except Exception:
            logger.debug(
                "realtime: inject_context for memory refresh failed",
                exc_info=True,
            )

    async def _handle_tool_call(
        self, session_id: str, call_id: str, name: str, arguments: str,
    ) -> str:
        """Execute a tool call through the local skill executor."""
        if not self._skill_executor or not self._skill_registry:
            return json.dumps({"error": "No skill executor available"})

        try:
            args = json.loads(arguments) if isinstance(arguments, str) else arguments
        except json.JSONDecodeError:
            args = {}

        parts = name.split("__", 1)
        if len(parts) != 2:
            return json.dumps({"error": f"Invalid tool name format: {name}"})

        skill_id, endpoint_id = parts
        skill = self._skill_registry.skills.get(skill_id)
        if not skill:
            return json.dumps({"error": f"Skill not found: {skill_id}"})

        endpoint = next((ep for ep in skill.endpoints if ep.id == endpoint_id), None)
        if not endpoint:
            return json.dumps({"error": f"Endpoint not found: {endpoint_id}"})

        logger.info(f"Realtime tool execution: {name} -> {args}")
        await self._send_tool_feedback(session_id, self._tool_feedback_text(name))

        # PR9: emit the same tool_start/tool_result envelopes the chat
        # path emits so the v2 ToolTrace component renders voice tools
        # identically to text tools (chip + expandable detail). We synth
        # a tool_call shape matching the chat ladder; the call_id ties
        # tool_start <-> tool_result for the client buffer.
        tool_call = {"name": name, "id": call_id, "args": args}
        t0 = time.time()
        if self._orchestrator is not None:
            try:
                await self._orchestrator._emit_tool_start(session_id, tool_call)
            except Exception:
                # Never let trace emission abort a voice tool call. The
                # legacy transcript path above stays as the fallback.
                logger.exception("voice tool_start emit failed")

        result = await self._skill_executor.execute(name, args, skill, endpoint)

        if self._orchestrator is not None:
            latency_ms = (time.time() - t0) * 1000.0
            try:
                await self._orchestrator._emit_tool_result(
                    session_id, tool_call, result, latency_ms,
                )
            except Exception:
                logger.exception("voice tool_result emit failed")

        if self._memory:
            self._memory.working_push(session_id, {
                "role": "tool", "tool": name, "result_summary": str(result.get("data", ""))[:200],
            })

        # Bug 2 (A) — voice tool calls used to leave NO episode trail,
        # so "what did my robot do yesterday?" returned nothing. The
        # hand-written CuteBot skill doesn't record episodes
        # (``GenericHardwareSkill._record_episode`` does, but the
        # router picks the bespoke skill for cutebot first), and this
        # voice path itself never wrote one. Centralizing the
        # persistence here means EVERY voice-driven hardware action
        # gets logged — anchored to the user's live session_id, not
        # the device's anonymous ``hwdev-*`` fallback — so recall
        # queries surface it.
        #
        # Fire-and-forget so the tool-result round-trip stays on
        # the latency budget (the orchestrator's
        # ``_save_episode_async`` does the same thing on the text
        # path; if it's wired we delegate, otherwise we call
        # ``episode_save`` directly).
        try:
            await self._record_voice_tool_episode(session_id, name, args, result)
        except Exception:
            logger.debug(
                "realtime: voice tool episode persistence skipped",
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
        live session. Mirrors the shape of
        ``hardware/capability_skill.GenericHardwareSkill._record_episode``
        and the orchestrator's text-path ``_save_episode_async`` so
        recall code (timeline_fusion, episode_search) can match either
        source uniformly.

        Hardware tool calls (cutebot__*, hwdev_*) get
        ``event_type="actuator"`` (or ``"sensor"`` for telemetry reads)
        so the timeline / recall surface that already filters on
        ``event_type`` in {"actuator","sensor"} surfaces them. Non-
        hardware tools fall through to ``event_type="tool"`` which is
        still searchable by name/summary.
        """
        if not self._memory:
            return
        episode_save = getattr(self._memory, "episode_save", None)
        if episode_save is None:
            return

        # Classify the tool. Hand-written hardware skills are e.g.
        # ``cutebot__drive``, ``cutebot__status`` — the second token
        # ``status``/``read_telemetry``/``get_*`` is a read; anything
        # else is an actuator. Generic auto-generated hardware skills
        # use the ``hwdev_<id>__<cap>`` shape; same heuristic applies.
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

        # Compact JSON detail — keeps the rows small but preserves the
        # args + observed/expected fields that the LLM uses to narrate
        # recall ("yesterday at 10:32 you asked the cutebot to drive").
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
                    "source": "voice_realtime",
                    "ts": time.time(),
                },
                default=str,
            )[:2000]
        except Exception:
            detail = ""

        # Importance heuristic matches GenericHardwareSkill: sensor
        # reads are routine (0.3), unverified or failed actuators
        # are interesting (0.7), verified actuators are 0.6.
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
                    "realtime: episode_save for voice tool call raised",
                    exc_info=True,
                )

        try:
            asyncio.create_task(_runner())
        except RuntimeError:
            await _runner()

    async def _handle_speech_started(self, session_id: str):
        """User started speaking — cancel current response and notify client."""
        rs = self._sessions.get(session_id)
        if not rs:
            return
        await rs.cancel_response()
        payload = {"action": "stop_playback"}
        if rs.node_id.startswith("webclient_") and self._send_to_session:
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
            await self._send_to_node(rs.node_id, {
                "type": "speech_started",
                "payload": payload,
            })

    async def _handle_conversation_item(self, session_id: str, item_event: dict):
        """Handle GA conversation.item.added / conversation.item.done events."""
        action = item_event.get("action", "")
        item = item_event.get("item", {})
        logger.debug("Conversation item %s in session %s: role=%s type=%s",
                      action, session_id[:8], item.get("role", ""), item.get("type", ""))

    async def _handle_error(self, session_id: str, error: str):
        """Classify an OpenAI Realtime error and trigger fallback when possible.

        Classification table:
          - ``insufficient_quota`` / WS close 1013 -> ``state=degraded``,
            ``reason=openai_realtime_quota``, mark session for whisper
            TTS fallback so the assistant keeps speaking via the cheap
            ``/audio/speech`` path.
          - ``invalid_api_key`` / 401 -> ``state=degraded`` with
            ``reason=openai_realtime_auth``, same fallback.
          - any other error -> log + emit ``state=degraded`` (the
            session is already dead, so emitting *something* lets the
            client surface a banner instead of silent failure).
        """
        err_lc = (error or "").lower()
        if "insufficient_quota" in err_lc or "1013" in err_lc or "exceeded your current quota" in err_lc:
            reason = "openai_realtime_quota"
        elif "invalid_api_key" in err_lc or "401" in err_lc or "unauthorized" in err_lc:
            reason = "openai_realtime_auth"
        elif "rate_limit" in err_lc or "rate limit" in err_lc or "429" in err_lc:
            reason = "openai_realtime_rate_limit"
        else:
            reason = "openai_realtime_error"

        logger.error(
            "Realtime error [%s]: %s -> classified=%s",
            session_id, error, reason,
        )

        if self._fallback_router:
            try:
                await self._fallback_router.handle_realtime_failure(
                    session_id=session_id,
                    reason=reason,
                    detail=str(error)[:200],
                )
            except Exception:
                logger.exception("Fallback router refused realtime failure handoff")
