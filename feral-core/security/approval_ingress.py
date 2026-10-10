"""Server-owned node ingress restriction for unbound approval principals.

An authenticated device is not the owner of every pending review. The current
approval store has no originating device principal, so node ingress cannot
resolve reviews until that authority is persisted and validated. This restriction
is inherited by child tasks and is never derived from client context or a SID.
"""

import asyncio
from collections.abc import Callable
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from uuid import UUID


@dataclass(frozen=True)
class PairedDevicePrincipal:
    """Private server admission, with immutable identity and live peer fencing.

    Construct only from verified pairing credentials, never from message fields.
    The callback retains connection/store authority; it is not persisted or sent
    to a client. Persisted identity alone never grants permission to act.
    """

    device_id: str
    is_current: Callable[[], bool] = field(repr=False, compare=False)

    def __post_init__(self):
        if not isinstance(self.device_id, str) or str(UUID(self.device_id)) != self.device_id:
            raise ValueError("Invalid paired device identity")
        if not callable(self.is_current):
            raise ValueError("Missing paired device authority")

    def require_current(self) -> None:
        try:
            current = self.is_current() is True
        except Exception:
            current = False
        if not current:
            raise asyncio.CancelledError("Paired device authority changed")

    def storage_binding(self) -> dict:
        return {"version": 1, "kind": "paired_device", "device_id": self.device_id}


_node_ingress: ContextVar[bool | PairedDevicePrincipal] = ContextVar("feral_node_approval_ingress", default=False)


def begin_node_approval_ingress(principal: PairedDevicePrincipal | None = None) -> Token[bool | PairedDevicePrincipal]:
    """Bind only at the server's admitted node connection boundary."""
    if principal is not None and type(principal) is not PairedDevicePrincipal:
        raise ValueError("Invalid paired device authority")
    return _node_ingress.set(principal if principal is not None else True)


def end_node_approval_ingress(token: Token[bool | PairedDevicePrincipal]) -> None:
    _node_ingress.reset(token)


def device_approval_authority_unavailable() -> bool:
    return _node_ingress.get() is not False


def current_node_principal() -> PairedDevicePrincipal | None:
    value = _node_ingress.get()
    return value if type(value) is PairedDevicePrincipal else None
