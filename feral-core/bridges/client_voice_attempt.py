"""Captured client voice identity; no task dispatch or effect replay authority."""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from models.protocol import VoiceAttemptPayload

MAX_VOICE_ATTEMPTS_PER_CONNECTION = 256


class VoiceAttemptError(RuntimeError):
    def __init__(self, code: str):
        super().__init__("Voice configuration could not be confirmed. Inspect settings before retrying.")
        self.code = code


@dataclass
class VoiceAttemptBinding:
    session_id: str
    attempt_id: str
    owner: object
    owner_current: Callable[[], bool]
    ledger_current: Callable[[], bool]
    active: bool = True
    lifecycle_pending: bool = False
    interrupt_ids: set[str] = field(default_factory=set)

    def current(self) -> bool:
        return self.active and not self.lifecycle_pending and self.owner_current() and self.ledger_current()

    def identity(self) -> dict:
        return {"voice_attempt_version": 1, "voice_attempt_id": self.attempt_id}


@dataclass
class VoiceAttemptLedger:
    owner: object
    seen: set[str] = field(default_factory=set)
    binding: VoiceAttemptBinding | None = None


_VOICE_ATTEMPT: ContextVar[VoiceAttemptBinding | None] = ContextVar("client_voice_attempt", default=None)


def parse_voice_attempt(params: dict) -> str | None:
    try:
        parsed = VoiceAttemptPayload(**{key: params[key] for key in
                                     ("voice_attempt_version", "voice_attempt_id") if key in params})
    except ValueError:
        raise VoiceAttemptError("voice_attempt_invalid") from None
    return parsed.voice_attempt_id


def current_voice_attempt(session_id: str) -> VoiceAttemptBinding | None:
    binding = _VOICE_ATTEMPT.get()
    if binding is not None and binding.session_id != session_id:
        raise asyncio.CancelledError("Unbound voice attempt")
    return binding


def assert_voice_attempt_current(binding: VoiceAttemptBinding | None) -> None:
    if binding is not None and not binding.current():
        raise asyncio.CancelledError("Retired voice attempt")


def require_voice_producer(binding: VoiceAttemptBinding | None, producer: object) -> None:
    if binding is not None and (not binding.current() or getattr(producer, "_voice_attempt", None) is not binding):
        raise VoiceAttemptError("voice_producer_superseded")


def client_voice_producers(state, session_id: str) -> tuple[object | None, ...]:
    router = getattr(state, "voice_router", None)
    node_id = f"webclient_{session_id[:8]}"
    direct = getattr(state, "gemini_proxy", None)
    candidates = [getattr(direct, "_sessions", {}).get(session_id)]
    for name in ("_realtime", "_gemini"):
        getter = getattr(getattr(router, name, None), "get_session", None)
        if callable(getter):
            candidates.append(getter(node_id))
        else:
            candidates.append(None)
    getter = getattr(getattr(router, "_chained", None), "get_session", None)
    if callable(getter):
        candidates.append(getter(session_id))
    else:
        candidates.append(None)
    return tuple(candidates)


def require_client_voice_producers(state, session_id: str, expected: VoiceAttemptBinding | None) -> None:
    """Inspect existing producers before a client changes SID-wide state."""
    candidates = client_voice_producers(state, session_id)
    for producer in candidates:
        if producer is not None and (getattr(producer, "session_id", None) != session_id
                                     or getattr(producer, "_voice_attempt", None) is not expected):
            raise VoiceAttemptError("voice_producer_superseded")


@contextmanager
def voice_attempt_scope(binding: VoiceAttemptBinding | None) -> Iterator[None]:
    token = _VOICE_ATTEMPT.set(binding)
    try:
        yield
    finally:
        _VOICE_ATTEMPT.reset(token)


def voice_attempt_payload(payload: dict, binding: VoiceAttemptBinding | None) -> dict:
    # Callers capture at producer admission. Never retrieve a SID's newest
    # attempt and attach it to an older producer's frame.
    captured = binding
    if captured is None:
        return payload
    if not captured.current():
        raise asyncio.CancelledError("Retired voice attempt")
    return {**payload, **captured.identity()}


def require_voice_attempt(state, session_id: str, owner: object, params: dict,
                          *, allow_stopped: bool = False) -> VoiceAttemptBinding | None:
    if getattr(state, "sessions", {}).get(session_id) is not owner:
        raise VoiceAttemptError("voice_owner_superseded")
    attempt_id = parse_voice_attempt(params)
    router = getattr(state, "voice_router", None)
    ledgers = getattr(router, "_client_voice_attempts", {})
    ledger = ledgers.get(session_id) if isinstance(ledgers, dict) else None
    if attempt_id is None:
        if isinstance(ledger, VoiceAttemptLedger) and ledger.owner is owner:
            raise VoiceAttemptError("voice_attempt_required")
        return None
    if not isinstance(ledger, VoiceAttemptLedger) or ledger.owner is not owner:
        raise VoiceAttemptError("voice_attempt_superseded")
    binding = ledger.binding
    if (binding is None or binding.attempt_id != attempt_id or not binding.owner_current()
            or not binding.ledger_current() or (not allow_stopped and (not binding.active or binding.lifecycle_pending))):
        raise VoiceAttemptError("voice_attempt_superseded")
    return binding


def begin_voice_attempt(state, session_id: str, owner: object, params: dict) -> VoiceAttemptBinding | None:
    attempt_id = parse_voice_attempt(params)
    router = state.voice_router
    ledgers = getattr(router, "_client_voice_attempts", {})
    ledger = ledgers.get(session_id)
    if ledger is not None and not isinstance(ledger, VoiceAttemptLedger):
        raise VoiceAttemptError("voice_attempt_unavailable")
    if attempt_id is None:
        if ledger is not None and ledger.owner is owner:
            raise VoiceAttemptError("voice_attempt_required")
        if ledger is not None:
            if ledger.binding is not None:
                ledger.binding.active = False
            ledgers.pop(session_id, None)
        return None
    if not isinstance(ledgers, dict) or not hasattr(router, "_client_voice_attempts"):
        raise VoiceAttemptError("voice_attempt_unavailable")
    if ledger is None or ledger.owner is not owner:
        if ledger is not None and ledger.binding is not None:
            ledger.binding.active = False
        ledger = VoiceAttemptLedger(owner)
        ledgers[session_id] = ledger
    if attempt_id in ledger.seen:
        raise VoiceAttemptError("voice_attempt_reused")
    if len(ledger.seen) >= MAX_VOICE_ATTEMPTS_PER_CONNECTION:
        raise VoiceAttemptError("voice_attempt_capacity")
    if ledger.binding is not None:
        ledger.binding.active = False
    admitted_ledger = ledger
    binding = VoiceAttemptBinding(session_id, attempt_id, owner,
        lambda: getattr(state, "sessions", {}).get(session_id) is owner,
        lambda: ledgers.get(session_id) is admitted_ledger and admitted_ledger.binding is binding)
    ledger.seen.add(attempt_id)
    ledger.binding = binding
    return binding


def release_voice_attempt(state, session_id: str, owner: object) -> None:
    router = getattr(state, "voice_router", None)
    ledgers = getattr(router, "_client_voice_attempts", {})
    ledger = ledgers.get(session_id) if isinstance(ledgers, dict) else None
    if isinstance(ledger, VoiceAttemptLedger) and ledger.owner is owner:
        if ledger.binding is not None:
            ledger.binding.active = False
        ledgers.pop(session_id, None)


def voice_attempt_error_identity(params: dict) -> dict:
    try:
        attempt_id = parse_voice_attempt(params)
    except VoiceAttemptError:
        return {}
    return {"voice_attempt_version": 1, "voice_attempt_id": attempt_id} if attempt_id is not None else {}
