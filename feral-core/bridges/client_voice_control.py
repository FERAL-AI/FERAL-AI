"""Cancel only the response belonging to an authenticated client session.

An interrupt never closes a voice session, cancels tools, or accepts a
client-supplied destination. A provider cancellation request is not a
confirmation that every remote audio frame has stopped.
"""
from __future__ import annotations

import inspect
from contextlib import asynccontextmanager

from bridges.client_voice_attempt import (VoiceAttemptBinding, VoiceAttemptError, client_voice_producers, require_client_voice_producers, require_voice_attempt, require_voice_producer,
    voice_attempt_scope)
from models.protocol import AudioChunkPayload, VoiceInterruptPayload, VoiceMutePayload


@asynccontextmanager
async def client_voice_control_scope(state, session_id: str, owner: object, params: dict, *, audio: bool = False):
    # Ephemeral media/speech controls must not wait for a whole command's
    # history writer. Startup/lazy producer creation still uses that SID lock.
    captured = require_voice_attempt(state, session_id, owner, params)
    if captured is None:
        with voice_attempt_scope(None):
            yield None
        if getattr(state, "sessions", {}).get(session_id) is not owner:
            raise VoiceAttemptError("voice_owner_superseded")
        return
    router = getattr(state, "voice_router", None)
    direct = getattr(state, "gemini_proxy", None)
    selected = client_voice_producers(state, session_id)
    provider = router._resolve_session_provider(session_id) if router is not None else ""
    audio_producer = selected[0] or selected[{"openai": 1, "gemini": 2, "chained": 3}.get(provider, 0)]
    if not audio or audio_producer is not None:
        require_client_voice_producers(state, session_id, captured)
        with voice_attempt_scope(captured):
            yield captured
        if not captured.current():
            raise VoiceAttemptError("voice_attempt_superseded")
        current = client_voice_producers(state, session_id)
        if (getattr(state, "voice_router", None) is not router or getattr(state, "gemini_proxy", None) is not direct
                or any(old is not new for old, new in zip(selected, current))):
            raise VoiceAttemptError("voice_producer_superseded")
        return
    from bridges.client_voice_configuration import _configuration_scope
    async with _configuration_scope(state, session_id, disabled=not audio):
        binding = require_voice_attempt(state, session_id, owner, params)
        if binding is not None:
            require_client_voice_producers(state, session_id, binding)
        with voice_attempt_scope(binding):
            yield binding
        if binding is not None and not binding.current():
            raise VoiceAttemptError("voice_attempt_superseded")


async def handle_client_voice_audio(state, session_id: str, owner: object, params: dict) -> dict:
    try:
        payload = AudioChunkPayload(**params)
    except ValueError:
        raise VoiceAttemptError("voice_audio_invalid") from None
    async with client_voice_control_scope(state, session_id, owner, params, audio=True) as binding:
        router = getattr(state, "voice_router", None)
        if binding is not None and router is not None and router.is_session_muted(session_id):
            return {"received": True, **binding.identity()}
        proxy = getattr(state, "gemini_proxy", None)
        if proxy is not None and proxy.has_session(session_id):
            if binding is not None:
                selected = getattr(proxy, "_sessions", {}).get(session_id)
                if selected is None:
                    raise VoiceAttemptError("voice_producer_superseded")
                require_voice_producer(binding, selected)
                await selected.send_audio(payload.data_b64)
            else:
                await proxy.relay_audio(session_id, payload.data_b64)
        elif getattr(state, "voice_router", None) is not None:
            producers = client_voice_producers(state, session_id)
            provider = state.voice_router._resolve_session_provider(session_id)
            selected = producers[{"openai": 1, "gemini": 2, "chained": 3}.get(provider, 0)]
            if binding is not None and selected is not None:
                require_voice_producer(binding, selected)
                if provider == "chained":
                    pipeline = state.voice_router._chained
                    await pipeline.handle_audio_for_session(selected, payload.data_b64,
                        payload.chunk_index, payload.is_final, wait_for_turn=False)
                elif provider in {"openai", "gemini"}:
                    sender = getattr(selected, "send_audio", None)
                    if getattr(selected, "connected", False) is not True or not callable(sender):
                        raise VoiceAttemptError("voice_producer_unavailable")
                    await sender(payload.data_b64)
            else:
                await state.voice_router.handle_audio_from_client(
                    session_id=session_id, audio_b64=payload.data_b64,
                    chunk_index=payload.chunk_index, is_final=payload.is_final,
                    encoding=payload.encoding, sample_rate=payload.sample_rate)
        return {"received": True, **(binding.identity() if binding is not None else {})}


async def mute_client_voice(state, session_id: str, owner: object, params: dict) -> dict:
    try:
        parsed = VoiceMutePayload(**params)
    except ValueError:
        raise VoiceAttemptError("voice_mute_invalid") from None
    async with client_voice_control_scope(state, session_id, owner, params) as binding:
        router = getattr(state, "voice_router", None)
        if router is None:
            raise VoiceAttemptError("voice_runtime_unavailable")
        if binding is not None:
            require_client_voice_producers(state, session_id, binding)
        await router.set_session_muted(session_id, parsed.muted, source="web")
        return {"muted": parsed.muted, **(binding.identity() if binding is not None else {})}


async def interrupt_client_voice_attempt(state, session_id: str, owner: object, params: dict) -> dict:
    try:
        parsed = VoiceInterruptPayload(**params)
    except ValueError:
        raise VoiceAttemptError("voice_interrupt_invalid") from None
    async with client_voice_control_scope(state, session_id, owner, params) as binding:
        request_id = parsed.voice_request_id
        identity = {**(binding.identity() if binding is not None else {}),
                    **({"voice_request_id": request_id} if request_id is not None else {})}
        if binding is not None and request_id is not None:
            if request_id in binding.interrupt_ids:
                return {"status": "duplicate", "cancel_requested": False, "session_preserved": True, **identity}
            if len(binding.interrupt_ids) >= 256:
                raise VoiceAttemptError("voice_interrupt_capacity")
            binding.interrupt_ids.add(request_id)
        return {**(await interrupt_client_voice(state, session_id, binding=binding)), **identity}


async def interrupt_client_voice(state, session_id: str, *, binding: VoiceAttemptBinding | None = None) -> dict:
    base = {"cancel_requested": False, "session_preserved": True}
    if not isinstance(session_id, str) or not session_id:
        return {**base, "status": "no_active_response"}
    router = getattr(state, "voice_router", None)
    node_id = f"webclient_{session_id[:8]}"
    candidates = []
    direct = getattr(state, "gemini_proxy", None)
    sessions = getattr(direct, "_sessions", None)
    if isinstance(sessions, dict):
        candidates.append(sessions.get(session_id))
    for name in ("_realtime", "_gemini"):
        proxy = getattr(router, name, None)
        getter = getattr(proxy, "get_session", None)
        if callable(getter):
            try:
                candidates.append(getter(node_id))
            except Exception:
                return {**base, "status": "error"}
    unsupported = False
    for voice in candidates:
        # Prefix-based node lookup can collide. Refuse another session even
        # if the provider returns it for the derived webclient key.
        if voice is None or getattr(voice, "session_id", None) != session_id:
            continue
        if (getattr(voice, "_connected", True) is False
                or getattr(voice, "_response_in_progress", True) is False):
            continue
        require_voice_producer(binding, voice)
        cancel = getattr(voice, "cancel_response", None)
        if not callable(cancel):
            unsupported = True
            continue
        try:
            result = cancel()
            if inspect.isawaitable(result):
                result = await result
            if result is False:
                return {**base, "status": "no_active_response"}
            return {**base, "status": "requested", "cancel_requested": True}
        except Exception:
            return {**base, "status": "error"}
    cancel_chained = getattr(router, "cancel_chained_response", None)
    if binding is not None:
        pipeline = getattr(router, "_chained", None)
        getter = getattr(pipeline, "get_session", None)
        selected = getter(session_id) if callable(getter) else None
        if selected is not None and pipeline is not None:
            require_voice_producer(binding, selected)
            result = await pipeline.interrupt_output(session_id, expected_session=selected)
            return {**base, "status": "requested" if result else "no_active_response", "cancel_requested": bool(result)}
    if callable(cancel_chained):
        try:
            result = cancel_chained(session_id)
            if inspect.isawaitable(result):
                result = await result
            if result is True:
                return {**base, "status": "requested", "cancel_requested": True}
        except Exception:
            return {**base, "status": "error"}
    return {**base, "status": "unsupported" if unsupported else "no_active_response"}
