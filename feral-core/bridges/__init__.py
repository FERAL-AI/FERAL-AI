"""Bridges: FERAL driving *other* agents as subprocesses.

Everything under ``skills/`` makes FERAL do work itself. This package is
the other direction: it lets FERAL hand a coding task to an external
agent that already exists (opencode, Claude Code, Codex) and watch it
work, rather than reimplementing a coding agent inside FERAL.

The transport is ACP (Agent Client Protocol), published by Zed: JSON-RPC
2.0 framed as newline-delimited JSON over the child process's stdin and
stdout. The editor (here, FERAL) is the *client*; the coding agent is the
*agent*. See ``bridges/acp.py`` for the protocol layer and
``bridges/jsonrpc.py`` for the framing.

Import order matters: ``jsonrpc`` has no FERAL dependencies at all, ``acp``
depends only on ``jsonrpc``, and ``permissions``/``catalog``/``sessions``
are where FERAL's own machinery (settings, approvals) gets wired in. That
layering is deliberate so the protocol code stays testable without a brain.
"""

from bridges.jsonrpc import JsonRpcError, NdjsonRpcPeer  # noqa: F401
from bridges.acp import (  # noqa: F401
    AcpEvent,
    AcpProtocolError,
    AcpSession,
    AcpAgentProcess,
    PROTOCOL_VERSION,
)
from bridges.permissions import (  # noqa: F401
    PermissionDecision,
    PermissionRequest,
    ApprovalManagerBroker,
    DenyAllBroker,
    QueueingBroker,
)

__all__ = [
    "JsonRpcError",
    "NdjsonRpcPeer",
    "AcpEvent",
    "AcpProtocolError",
    "AcpSession",
    "AcpAgentProcess",
    "PROTOCOL_VERSION",
    "PermissionDecision",
    "PermissionRequest",
    "ApprovalManagerBroker",
    "DenyAllBroker",
    "QueueingBroker",
]
