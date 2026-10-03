"""Configure existing client voice engines without claiming provider inference."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager

from agents.runtime_context_checkpoint import RuntimeContextError, runtime_coordinator
from bridges.client_voice_attempt import (VoiceAttemptError, begin_voice_attempt, require_client_voice_producers,
    MAX_VOICE_ATTEMPTS_PER_CONNECTION, parse_voice_attempt, require_voice_attempt, voice_attempt_scope)


async def _cancel_owned_chained_turn(state, session_id: str, owner: object, params: dict, *, disabled: bool) -> None:
    """Only this connection's captured voice turn can unblock lifecycle admission."""
    attempt_id = parse_voice_attempt(params)
    if attempt_id is None:
        return
    router = getattr(state, "voice_router", None)
    ledger = getattr(router, "_client_voice_attempts", {}).get(session_id)
    if disabled:
        binding = require_voice_attempt(state, session_id, owner, params, allow_stopped=True)
    else:
        if ledger is not None and ledger.owner is owner:
            if attempt_id in ledger.seen:
                raise VoiceAttemptError("voice_attempt_reused")
            if len(ledger.seen) >= MAX_VOICE_ATTEMPTS_PER_CONNECTION:
                raise VoiceAttemptError("voice_attempt_capacity")
        binding = getattr(ledger, "binding", None) if getattr(ledger, "owner", None) is owner else None
    if binding is None or not binding.active:
        return
    if not binding.owner_current() or not binding.ledger_current():
        raise VoiceAttemptError("voice_attempt_superseded")
    require_client_voice_producers(state, session_id, binding)
    pipeline = getattr(router, "_chained", None)
    getter = getattr(pipeline, "get_session", None)
    selected = getter(session_id) if callable(getter) else None
    if selected is None:
        return
    cancel = getattr(pipeline, "request_owned_turn_cancel", None)
    if not callable(cancel):
        raise VoiceAttemptError("voice_cancellation_unavailable")
    with voice_attempt_scope(binding):
        was_pending = binding.lifecycle_pending
        binding.lifecycle_pending = True
        task = cancel(selected)
        if task is None:
            binding.lifecycle_pending = was_pending
        if task is not None:
            if not isinstance(task, asyncio.Task) or task is asyncio.current_task():
                raise VoiceAttemptError("voice_cancellation_unavailable")
            _, pending = await asyncio.wait({task}, timeout=2)
            if pending:
                raise VoiceAttemptError("voice_cancellation_unconfirmed")
    if not binding.owner_current() or not binding.ledger_current():
        raise VoiceAttemptError("voice_attempt_superseded")
    if state.voice_router is not router or not callable(getter) or getter(session_id) is not selected:
        raise VoiceAttemptError("voice_producer_superseded")


class ClientVoiceConfigurationError(VoiceAttemptError):
    def __init__(self, code: str):
        super().__init__("Voice configuration could not be confirmed. Inspect settings before retrying.")
        self.code = code


@asynccontextmanager
async def _configuration_scope(state, session_id: str, *, disabled: bool):
    orchestrator = getattr(state, "orchestrator", None)
    coordinator = runtime_coordinator(orchestrator)
    if coordinator is not None:
        if disabled and await coordinator.is_managed(session_id):
            # Managed voice starts are refused on both ingress paths. Preserve
            # Stop without opening a context writer; provider teardown must
            # not silently turn its own refused history mutation into success.
            yield
            return
        async with coordinator.legacy_mutation_scope((session_id,), "voice", command_handoff=True):
            yield
        return
    # Reuse the existing orchestration SID lock. Startup/teardown cannot
    # safely mutate a replacement engine in an embedding with no lock owner.
    lock_for = getattr(orchestrator, "_get_session_lock", None)
    if not callable(lock_for):
        raise ClientVoiceConfigurationError("voice_runtime_unavailable")
    lock = lock_for(session_id)
    if not isinstance(lock, asyncio.Lock):
        raise ClientVoiceConfigurationError("voice_runtime_unavailable")
    async with lock:
        yield


async def configure_client_voice(state, session_id: str, owner, params: dict,
                                 *, start_realtime: Callable[[], Awaitable[object]] | None = None,
                                 readiness_guard: Callable[[], Awaitable[None]] | None = None) -> dict:
    mode = params.get("mode", "realtime")
    provider = params.get("provider", "openai")
    if not isinstance(mode, str) or mode not in {"realtime", "chained", "whisper", "auto", "disabled"}:
        raise ClientVoiceConfigurationError("voice_mode_unsupported")
    if (not isinstance(provider, str) or len(provider) > 128
            or provider not in {"openai", "gemini", "configured", "chained"}):
        raise ClientVoiceConfigurationError("voice_provider_unsupported")
    if mode == "realtime" and provider == "chained":
        raise ClientVoiceConfigurationError("voice_provider_unsupported")

    def require_owner():
        if getattr(state, "sessions", {}).get(session_id) is not owner:
            raise ClientVoiceConfigurationError("voice_owner_superseded")

    require_owner()
    try:
        attempt_id = parse_voice_attempt(params)
        await _cancel_owned_chained_turn(state, session_id, owner, params, disabled=mode == "disabled")
    except VoiceAttemptError as exc:
        raise ClientVoiceConfigurationError(exc.code) from None
    if readiness_guard is not None:
        await readiness_guard()
        require_owner()
    binding = None
    try:
        async with _configuration_scope(state, session_id, disabled=mode == "disabled"):
            require_owner()
            router = getattr(state, "voice_router", None)
            if router is None:
                raise ClientVoiceConfigurationError("voice_runtime_unavailable")
            previous_ledger = getattr(router, "_client_voice_attempts", {}).get(session_id)
            previous_binding = getattr(previous_ledger, "binding", None)
            if mode == "disabled":
                binding = require_voice_attempt(state, session_id, owner, params, allow_stopped=True)
                if binding is not None and not binding.active and not binding.lifecycle_pending:
                    return {"mode": mode, "provider": provider, "status": "ok", **binding.identity()}
                if binding is not None:
                    require_client_voice_producers(state, session_id, binding)
            else:
                if attempt_id is not None:
                    require_client_voice_producers(state, session_id, previous_binding)
                binding = begin_voice_attempt(state, session_id, owner, params)
            with voice_attempt_scope(binding):
                if mode != "disabled" and (attempt_id is not None or previous_ledger is not None):
                    # Explicit new admission retires the previous producer before
                    # lazy realtime can reuse its already connected session.
                    if binding is not None:
                        await router.stop_session_voice(session_id, expected_attempt=previous_binding, fenced=True)
                    else:
                        await router.stop_session_voice(session_id)
                    require_owner()
                    proxy = getattr(state, "gemini_proxy", None)
                    sessions = getattr(proxy, "_sessions", None)
                    if proxy is not None and isinstance(sessions, dict) and sessions.get(session_id) is not None:
                        if binding is not None:
                            require_client_voice_producers(state, session_id, previous_binding)
                        stopper = getattr(proxy, "stop_session", None)
                        if not callable(stopper):
                            raise ClientVoiceConfigurationError("voice_realtime_teardown_failed")
                        await stopper(session_id)
                    require_owner()
                if mode == "chained":
                    pipeline = getattr(router, "_chained", None)
                    getter = getattr(pipeline, "get_session", None)
                    opener = getattr(router, "open_chained_session", None)
                    closer = getattr(pipeline, "close_session", None)
                    if not callable(getter) or not callable(opener) or not callable(closer):
                        raise ClientVoiceConfigurationError("voice_chained_unavailable")
                    modes = getattr(router, "_session_voice_mode", None)
                    if not isinstance(modes, dict):
                        raise ClientVoiceConfigurationError("voice_chained_unavailable")
                    previous = modes.get(session_id)
                    old = getter(session_id)
                    opened = None
                    try:
                        # The operator's saved configuration resolves credentials,
                        # engines and voices. No client-provided secret/callback or
                        # arbitrary provider_opts reaches the constructors.
                        opened = await opener(session_id, provider_opts=None)
                        require_owner()
                        if opened is None or getattr(opened, "session_id", None) != session_id or getter(session_id) is not opened:
                            raise ClientVoiceConfigurationError("voice_chained_open_failed")
                    except BaseException:
                        # The SID lock excludes other client configurations, but
                        # coordinator-free direct engine users are not serialized.
                        # Never infer ownership merely from a changed SID entry.
                        try:
                            current = getter(session_id)
                            if opened is not None and current is opened and opened is not old:
                                await closer(session_id)
                        finally:
                            current = getter(session_id)
                            if current is None and modes.get(session_id) == "chained":
                                # An old chained engine may have been closed by
                                # replacement startup; its mode is not rollback.
                                if previous is None or previous == "chained":
                                    modes.pop(session_id, None)
                                else:
                                    modes[session_id] = previous
                        raise
                elif mode == "disabled":
                    if binding is not None:
                        await router.stop_session_voice(session_id, expected_attempt=binding, fenced=True)
                    else:
                        await router.stop_session_voice(session_id)
                    require_owner()
                    proxy = getattr(state, "gemini_proxy", None)
                    sessions = getattr(proxy, "_sessions", None)
                    if isinstance(sessions, dict) and sessions.get(session_id) is not None:
                        if binding is not None:
                            require_client_voice_producers(state, session_id, binding)
                        stopper = getattr(proxy, "stop_session", None)
                        if not callable(stopper):
                            raise ClientVoiceConfigurationError("voice_realtime_teardown_failed")
                        await stopper(session_id)
                    require_owner()
                else:
                    # Preserve the existing lazy realtime/whisper configuration;
                    # this acknowledgment does not claim provider availability.
                    modes = getattr(router, "_session_voice_mode", None)
                    previous = modes.get(session_id) if isinstance(modes, dict) else None
                    router.set_session_voice_mode(session_id, mode)
                    if start_realtime is not None:
                        proxy = getattr(state, "gemini_proxy", None)
                        sessions = getattr(proxy, "_sessions", None)
                        old = sessions.get(session_id) if isinstance(sessions, dict) else None
                        opened = None
                        try:
                            opened = await start_realtime()
                            require_owner()
                            if (opened is None or getattr(opened, "session_id", None) != session_id
                                    or not isinstance(sessions, dict) or sessions.get(session_id) is not opened):
                                raise ClientVoiceConfigurationError("voice_realtime_open_failed")
                        except BaseException:
                            try:
                                if opened is not None and isinstance(sessions, dict) and sessions.get(session_id) is opened and opened is not old:
                                    stopper = getattr(proxy, "stop_session", None)
                                    if not callable(stopper):
                                        raise ClientVoiceConfigurationError("voice_realtime_teardown_failed")
                                    await stopper(session_id)
                            finally:
                                if (isinstance(modes, dict) and modes.get(session_id) == mode
                                        and isinstance(sessions, dict)
                                        and (sessions.get(session_id) is None or sessions.get(session_id) is old)):
                                    if previous is None:
                                        modes.pop(session_id, None)
                                    else:
                                        modes[session_id] = previous
                            raise
                require_owner()
                if binding is not None and mode == "disabled":
                    binding.active = False
                    binding.lifecycle_pending = False
                return {"mode": mode, "provider": provider, "status": "ok", **(binding.identity() if binding is not None else {})}
    except VoiceAttemptError as exc:
        if binding is not None and mode != "disabled":
            binding.active = False
        raise ClientVoiceConfigurationError(exc.code) from None
    except (ClientVoiceConfigurationError, RuntimeContextError, asyncio.CancelledError):
        if binding is not None and mode != "disabled":
            binding.active = False
        raise
    except Exception:
        if binding is not None and mode != "disabled":
            binding.active = False
        raise ClientVoiceConfigurationError("voice_configuration_failed") from None
