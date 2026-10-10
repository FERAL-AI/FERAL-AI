"""Server-owned node ingress restriction for unbound approval principals.

An authenticated device is not the owner of every pending review. The current
approval store has no originating device principal, so node ingress cannot
resolve reviews until that authority is persisted and validated. This restriction
is inherited by child tasks and is never derived from client context or a SID.
"""

from contextvars import ContextVar, Token


_node_ingress: ContextVar[bool] = ContextVar("feral_node_approval_ingress", default=False)


def begin_node_approval_ingress() -> Token[bool]:
    """Bind only at the server's admitted node connection boundary."""
    return _node_ingress.set(True)


def end_node_approval_ingress(token: Token[bool]) -> None:
    _node_ingress.reset(token)


def device_approval_authority_unavailable() -> bool:
    return _node_ingress.get() is not False
