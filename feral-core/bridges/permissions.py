"""Answering an external agent's ``session/request_permission``.

The rule this module exists to enforce
======================================
An external coding agent runs with the user's own filesystem and shell.
When it asks "may I run this", the answer must come from the same place
every other dangerous-tool answer in FERAL comes from:
``security/exec_approvals.py``'s :class:`~security.exec_approvals.ApprovalManager`.
Inventing a second approval store here would mean an operator who set
``policy=deny`` still had an external agent writing files, which is the
exact failure mode the approval manager was built to stop.

So the mapping is:

===================  ==============================================
ACP option kind      FERAL meaning
===================  ==============================================
``allow_once``       the grant already exists, or a human just said
                     yes for this one call; nothing is persisted
``allow_always``     ``ApprovalManager.grant_approval(scope="session")``
``reject_once``      no grant; the agent should stop this one action
``reject_always``    no grant, and the broker remembers the refusal
                     for the rest of the session
===================  ==============================================

Fail-closed is not optional. Every path that is not an explicit human
"yes" resolves to a rejection: no broker, broker raised, broker timed
out, agent offered no allow-shaped option, session cancelled. There is
deliberately no "auto allow" switch anywhere in this file, because the
one that gets added "just for local models" is the one that ships.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("feral.bridges.permissions")

ALLOW_KINDS = ("allow_once", "allow_always")
REJECT_KINDS = ("reject_once", "reject_always")


@dataclass
class PermissionOption:
    """One button the agent offered."""

    option_id: str
    name: str
    kind: str

    @property
    def allows(self) -> bool:
        return self.kind in ALLOW_KINDS

    @property
    def remembers(self) -> bool:
        return self.kind in ("allow_always", "reject_always")


@dataclass
class PermissionRequest:
    """A parked ``session/request_permission`` waiting on a human."""

    request_id: str
    session_id: str
    tool_call_id: str
    tool_name: str
    title: str
    options: list[PermissionOption]
    raw: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def option(self, option_id: str) -> Optional[PermissionOption]:
        for opt in self.options:
            if opt.option_id == option_id:
                return opt
        return None

    def first_of(self, kinds: tuple[str, ...]) -> Optional[PermissionOption]:
        for kind in kinds:
            for opt in self.options:
                if opt.kind == kind:
                    return opt
        return None

    def approval_key(self) -> str:
        """The name this request is remembered under in ``ApprovalManager``.

        Namespaced so an ``allow_always`` for an external agent's ``bash``
        can never be mistaken for a grant on FERAL's own ``bash`` tool.
        """
        return f"external_agent:{self.tool_name or 'unknown'}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "session_id": self.session_id,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "title": self.title,
            "created_at": self.created_at,
            "options": [
                {"option_id": o.option_id, "name": o.name, "kind": o.kind}
                for o in self.options
            ],
        }


@dataclass
class PermissionDecision:
    """What to send back on the wire.

    ``option_id`` of ``None`` means "no option selected"; the ACP outcome
    then depends on ``cancelled``. A rejection with no reject-shaped
    option available still has to be expressed, and ``cancelled`` is the
    only outcome the protocol defines for "no selection".
    """

    option_id: Optional[str]
    allowed: bool
    reason: str = ""
    cancelled: bool = False

    def to_outcome(self) -> dict[str, Any]:
        if self.option_id is None or self.cancelled:
            return {"outcome": {"outcome": "cancelled"}}
        return {"outcome": {"outcome": "selected", "optionId": self.option_id}}


def parse_permission_request(params: dict[str, Any]) -> PermissionRequest:
    """Turn raw ``session/request_permission`` params into a PermissionRequest."""
    tool_call = params.get("toolCall") or {}
    options: list[PermissionOption] = []
    for raw in params.get("options") or []:
        if not isinstance(raw, dict):
            continue
        options.append(
            PermissionOption(
                option_id=str(raw.get("optionId", "")),
                name=str(raw.get("name", "")),
                kind=str(raw.get("kind", "")),
            )
        )
    title = str(tool_call.get("title") or "")
    raw_name = tool_call.get("rawInput") or {}
    tool_name = str(
        tool_call.get("toolName")
        or tool_call.get("kind")
        or (raw_name.get("tool") if isinstance(raw_name, dict) else "")
        or title
        or "unknown"
    )
    return PermissionRequest(
        request_id=str(uuid.uuid4()),
        session_id=str(params.get("sessionId", "")),
        tool_call_id=str(tool_call.get("toolCallId") or ""),
        tool_name=tool_name,
        title=title or tool_name,
        options=options,
        raw=params,
    )


class PermissionBroker:
    """Decides one permission request. Subclasses must never auto-allow."""

    async def decide(self, request: PermissionRequest) -> PermissionDecision:
        raise NotImplementedError


def reject(request: PermissionRequest, reason: str) -> PermissionDecision:
    """Build the strongest available rejection for ``request``."""
    option = request.first_of(("reject_once", "reject_always"))
    if option is None:
        return PermissionDecision(
            option_id=None, allowed=False, reason=reason, cancelled=True
        )
    return PermissionDecision(option_id=option.option_id, allowed=False, reason=reason)


class DenyAllBroker(PermissionBroker):
    """The default. Refuses everything, loudly.

    This is what a bridge gets when nobody wired a human into it. It
    exists so that "forgot to pass a broker" is a session that does
    nothing, not a session that does everything.
    """

    def __init__(self, reason: str = "no approval surface is attached to this session"):
        self._reason = reason

    async def decide(self, request: PermissionRequest) -> PermissionDecision:
        logger.warning(
            "denying external-agent permission for %s: %s",
            request.tool_name,
            self._reason,
        )
        return reject(request, self._reason)


class ApprovalManagerBroker(PermissionBroker):
    """Answers from an existing grant in ``ApprovalManager``, else defers.

    Checks the store first, so an ``allow_always`` the operator gave
    earlier in the session does not re-prompt. Anything without a grant
    is handed to ``fallback`` (normally a :class:`QueueingBroker` that
    parks it for a human). With no fallback the answer is no.
    """

    def __init__(self, manager: Any, session_key: str, fallback: Optional[PermissionBroker] = None):
        self._manager = manager
        self._session_key = session_key
        self._fallback = fallback or DenyAllBroker(
            "no prior approval and no approval surface attached"
        )

    async def decide(self, request: PermissionRequest) -> PermissionDecision:
        allowed = False
        try:
            allowed, _reason = self._manager.check_approval(
                request.approval_key(), self._session_key
            )
        except Exception:
            # A broken approval store must not become an open door.
            logger.exception("approval lookup failed; denying")
            return reject(request, "approval store unavailable")

        if allowed:
            option = request.first_of(ALLOW_KINDS)
            if option is None:
                return reject(request, "agent offered no allow option")
            return PermissionDecision(
                option_id=option.option_id,
                allowed=True,
                reason="existing approval grant",
            )

        decision = await self._fallback.decide(request)
        if decision.allowed:
            option = request.option(decision.option_id or "")
            if option is not None and option.remembers:
                try:
                    self._manager.grant_approval(
                        request.approval_key(), self._session_key, scope="session"
                    )
                except Exception:
                    logger.exception("failed to persist allow_always grant")
        return decision


class QueueingBroker(PermissionBroker):
    """Parks the request and waits for someone to answer it out of band.

    This is the shape the skill uses: ``external_agent__run_task`` returns
    with ``status="awaiting_permission"`` carrying the parked request, and
    ``external_agent__respond_permission`` resolves it. The turn is only
    unblocked by an explicit answer or by ``timeout_seconds`` elapsing,
    and the timeout resolves to a rejection.
    """

    def __init__(self, timeout_seconds: float = 120.0):
        self.timeout_seconds = float(timeout_seconds)
        self._pending: dict[str, tuple[PermissionRequest, asyncio.Future]] = {}
        self._arrivals: asyncio.Queue = asyncio.Queue()

    @property
    def pending(self) -> list[PermissionRequest]:
        return [req for req, _fut in self._pending.values()]

    async def next_request(self, timeout: float) -> Optional[PermissionRequest]:
        """Wait for the next request to arrive, for callers that poll."""
        try:
            return await asyncio.wait_for(self._arrivals.get(), timeout)
        except asyncio.TimeoutError:
            return None

    def resolve(self, request_id: str, option_id: str) -> bool:
        """Answer a parked request. Returns False when it is already gone."""
        entry = self._pending.pop(request_id, None)
        if entry is None:
            return False
        request, future = entry
        if future.done():
            return False
        option = request.option(option_id)
        if option is None:
            future.set_result(
                reject(request, f"unknown option id {option_id!r}")
            )
            return True
        future.set_result(
            PermissionDecision(
                option_id=option.option_id,
                allowed=option.allows,
                reason="answered by user",
            )
        )
        return True

    def reject_all(self, reason: str = "session cancelled") -> None:
        for request_id in list(self._pending):
            entry = self._pending.pop(request_id, None)
            if entry is None:
                continue
            request, future = entry
            if not future.done():
                future.set_result(reject(request, reason))

    async def decide(self, request: PermissionRequest) -> PermissionDecision:
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request.request_id] = (request, future)
        self._arrivals.put_nowait(request)
        try:
            return await asyncio.wait_for(future, self.timeout_seconds)
        except asyncio.TimeoutError:
            self._pending.pop(request.request_id, None)
            return reject(
                request,
                f"no answer within {self.timeout_seconds:g}s",
            )
        except asyncio.CancelledError:
            self._pending.pop(request.request_id, None)
            raise
