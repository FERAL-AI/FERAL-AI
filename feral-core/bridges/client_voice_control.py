"""Cancel only the response belonging to an authenticated client session.

An interrupt never closes a voice session, cancels tools, or accepts a
client-supplied destination. A provider cancellation request is not a
confirmation that every remote audio frame has stopped.
"""
from __future__ import annotations

import inspect


async def interrupt_client_voice(state, session_id: str) -> dict:
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
