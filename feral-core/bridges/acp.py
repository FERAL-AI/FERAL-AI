"""ACP (Agent Client Protocol) client: FERAL as the editor, agent as a child.

Shape of a session
==================
1. Spawn the agent binary with stdin/stdout wired to us (``opencode acp``,
   ``claude-code-acp``, ``codex-acp`` all behave identically here).
2. ``initialize``: send the highest protocol version we speak plus our
   client capabilities; the agent replies with the version it settled on
   and what it can do.
3. ``session/new`` with an absolute ``cwd``.
4. ``session/prompt``, one call per user turn. It does not return until
   the turn ends; progress arrives meanwhile as ``session/update``
   notifications *from* the agent, and permission questions arrive as
   ``session/request_permission`` requests from the agent.
5. ``session/cancel`` (a notification) to interrupt, ``session/close`` to
   finish, then the process is killed.

Method names are not guessed: they are the ``agentMethods`` /
``clientMethods`` tables in schema/v1/meta.json of
github.com/agentclientprotocol/agent-client-protocol.

Capabilities, and one that was learned the hard way
===================================================
``terminal`` is false: we do not want to reimplement a PTY, and an agent
that cannot delegate its shell runs it itself, which is fine.

``fs.readTextFile`` and ``fs.writeTextFile`` are TRUE, and that was not
the first design. The first design declared them false on the reasoning
that the agent should touch its own disk. Driving a real opencode 1.18.10
disproved it: once the user grants an edit permission, opencode's ACP
layer calls ``fs/write_text_file`` on the client to apply the patch, and
it does so whether or not the client advertised the capability. Refusing
produced::

    RequestError: fs/write_text_file is not available:
    client did not advertise fs capability

and the turn died after the human had already said yes. So the client
implements both methods.

That turns out to be the better security posture anyway: every write the
agent makes through this path goes through :meth:`AcpAgentProcess._fs_write`,
which refuses any path outside the session's own workspace. FERAL is the
gate rather than a bystander.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shlex
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional, Sequence

from bridges.jsonrpc import (
    DEFAULT_STREAM_LIMIT,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    JsonRpcError,
    NdjsonRpcPeer,
)
from bridges.permissions import (
    DenyAllBroker,
    PermissionBroker,
    PermissionDecision,
    parse_permission_request,
)

logger = logging.getLogger("feral.bridges.acp")

# The newest ACP major we implement. The agent may negotiate down; it may
# not negotiate up, so a future agent that only speaks 2 will answer with
# 1 or refuse, and we surface the refusal rather than guessing.
PROTOCOL_VERSION = 1

# Agent -> client methods (schema/v1/meta.json, clientMethods).
M_SESSION_UPDATE = "session/update"
M_REQUEST_PERMISSION = "session/request_permission"
M_FS_READ = "fs/read_text_file"
M_FS_WRITE = "fs/write_text_file"

# Client -> agent methods (schema/v1/meta.json, agentMethods).
M_INITIALIZE = "initialize"
M_SESSION_NEW = "session/new"
M_SESSION_PROMPT = "session/prompt"
M_SESSION_CANCEL = "session/cancel"
M_SESSION_CLOSE = "session/close"


class AcpProtocolError(RuntimeError):
    """The agent did something the protocol does not allow, or died."""


@dataclass
class AcpEvent:
    """One thing that happened during a turn, flattened for FERAL.

    ``raw`` keeps the untouched ``update`` object so a consumer can reach
    a field this dataclass has never heard of. That matters: ACP adds
    ``sessionUpdate`` variants between minor versions and a client that
    only understands its own dataclass silently drops them.
    """

    kind: str
    session_id: str
    text: str = ""
    tool_call_id: str = ""
    tool_name: str = ""
    title: str = ""
    status: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    at: float = field(default_factory=time.time)

    @property
    def is_tool_call(self) -> bool:
        return self.kind in ("tool_call", "tool_call_update")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "session_id": self.session_id,
            "text": self.text,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "title": self.title,
            "status": self.status,
            "at": self.at,
        }


def _content_text(block: Any) -> str:
    """Pull display text out of an ACP ContentBlock without assuming a type."""
    if isinstance(block, str):
        return block
    if not isinstance(block, dict):
        return ""
    if block.get("type") == "text" or "text" in block:
        return str(block.get("text") or "")
    if block.get("type") == "resource_link":
        return str(block.get("uri") or block.get("name") or "")
    resource = block.get("resource")
    if isinstance(resource, dict):
        return str(resource.get("text") or resource.get("uri") or "")
    return ""


def parse_session_update(session_id: str, update: dict[str, Any]) -> AcpEvent:
    """Flatten one ``SessionUpdate`` into an :class:`AcpEvent`."""
    kind = str(update.get("sessionUpdate") or "unknown")
    event = AcpEvent(kind=kind, session_id=session_id, raw=dict(update))

    if kind in ("agent_message_chunk", "agent_thought_chunk", "user_message_chunk"):
        event.text = _content_text(update.get("content"))
        return event

    if kind in ("tool_call", "tool_call_update"):
        event.tool_call_id = str(update.get("toolCallId") or "")
        event.title = str(update.get("title") or "")
        event.status = str(update.get("status") or "")
        # ACP calls the semantic bucket ``kind`` (read / edit / execute /
        # think / fetch / other). Agents also send ``toolName`` when they
        # have one; prefer that, fall back to the bucket, then the title.
        event.tool_name = str(
            update.get("toolName") or update.get("kind") or event.title or "tool"
        )
        contents = update.get("content")
        if isinstance(contents, list):
            parts = []
            for item in contents:
                if isinstance(item, dict) and "content" in item:
                    parts.append(_content_text(item.get("content")))
                else:
                    parts.append(_content_text(item))
            event.text = "\n".join(p for p in parts if p)
        return event

    if kind == "plan":
        entries = update.get("entries")
        if isinstance(entries, list):
            event.text = "\n".join(
                str(e.get("content") or "") for e in entries if isinstance(e, dict)
            )
        return event

    return event


@dataclass
class PromptResult:
    """What one ``session/prompt`` turn produced."""

    stop_reason: str
    events: list[AcpEvent]
    text: str
    awaiting_permission: bool = False

    @property
    def tool_calls(self) -> list[AcpEvent]:
        seen: dict[str, AcpEvent] = {}
        for event in self.events:
            if event.is_tool_call and event.tool_call_id:
                seen[event.tool_call_id] = event
        return list(seen.values())


class AcpSession:
    """One ``session/new`` on a running agent process."""

    def __init__(self, process: "AcpAgentProcess", session_id: str, cwd: str):
        self.process = process
        self.session_id = session_id
        self.cwd = cwd
        self.events: asyncio.Queue = asyncio.Queue()
        self.transcript: list[AcpEvent] = []
        self._closed = False

    def _record(self, event: AcpEvent) -> None:
        self.transcript.append(event)
        self.events.put_nowait(event)

    async def stream(self, idle_timeout: float = 1.0) -> AsyncIterator[AcpEvent]:
        """Yield events as they land, stopping when the queue goes quiet.

        For callers that want live output rather than the batch that
        :meth:`prompt` returns. ``idle_timeout`` is how long to wait for
        the next event before deciding the turn has gone silent.
        """
        while True:
            try:
                event = await asyncio.wait_for(self.events.get(), idle_timeout)
            except asyncio.TimeoutError:
                return
            yield event

    async def prompt(
        self, text: str, *, timeout: Optional[float] = None
    ) -> PromptResult:
        """Send one user turn and wait for it to end.

        Returns everything the agent streamed during the turn. If the
        turn stalls on a permission question that nobody answers, the
        broker's own timeout resolves it as a rejection, so this call
        cannot hang on a human forever.
        """
        if self._closed:
            raise AcpProtocolError("session is closed")

        start = len(self.transcript)
        try:
            result = await self.process.peer.request(
                M_SESSION_PROMPT,
                {
                    "sessionId": self.session_id,
                    "prompt": [{"type": "text", "text": text}],
                },
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            await self.cancel()
            raise AcpProtocolError(
                f"prompt timed out after {timeout}s (session cancelled)"
            )

        events = self.transcript[start:]
        stop_reason = ""
        if isinstance(result, dict):
            stop_reason = str(result.get("stopReason") or "")
        message = "".join(e.text for e in events if e.kind == "agent_message_chunk")
        return PromptResult(stop_reason=stop_reason, events=events, text=message)

    async def cancel(self) -> None:
        """Interrupt the current turn. Fire-and-forget by protocol design."""
        broker = self.process.broker
        reject_all = getattr(broker, "reject_all", None)
        if callable(reject_all):
            reject_all("session cancelled")
        with contextlib.suppress(Exception):
            await self.process.peer.notify(
                M_SESSION_CANCEL, {"sessionId": self.session_id}
            )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # session/close is optional in v1: an agent that predates it
        # answers METHOD_NOT_FOUND, which is not an error for us.
        with contextlib.suppress(Exception):
            await self.process.peer.request(
                M_SESSION_CLOSE, {"sessionId": self.session_id}, timeout=10
            )
        self.process.sessions.pop(self.session_id, None)


class AcpAgentProcess:
    """A spawned ACP agent and the connection to it."""

    def __init__(
        self,
        proc: asyncio.subprocess.Process,
        peer: NdjsonRpcPeer,
        *,
        command: Sequence[str],
        broker: PermissionBroker,
    ):
        self.proc = proc
        self.peer = peer
        self.command = list(command)
        self.broker = broker
        self.sessions: dict[str, AcpSession] = {}
        self.agent_capabilities: dict[str, Any] = {}
        self.agent_info: dict[str, Any] = {}
        self.negotiated_version: Optional[int] = None
        self._stderr_tail: list[str] = []
        self._stderr_task: Optional[asyncio.Task] = None
        self._closed = False

    # ------------------------------------------------------------------
    # Spawn
    # ------------------------------------------------------------------

    @classmethod
    async def spawn(
        cls,
        command: Sequence[str],
        *,
        cwd: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
        broker: Optional[PermissionBroker] = None,
        stream_limit: int = DEFAULT_STREAM_LIMIT,
    ) -> "AcpAgentProcess":
        """Start the agent binary with its stdio wired to a JSON-RPC peer.

        ``env`` fully replaces the environment when given. Callers that
        want the ambient environment plus overrides should build that
        dict themselves; silently merging would make it impossible to
        run an agent in a deliberately stripped environment, which is how
        the tests keep an agent out of the user's real config directory.
        """
        if not command:
            raise ValueError("command must not be empty")

        argv = list(command)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
                limit=stream_limit,
            )
        except FileNotFoundError as exc:
            raise AcpProtocolError(
                f"agent binary not found: {shlex.join(argv)}"
            ) from exc

        self = cls(proc, peer=None, command=argv, broker=broker or DenyAllBroker())  # type: ignore[arg-type]
        self.peer = NdjsonRpcPeer(
            proc.stdout,
            proc.stdin,
            request_handler=self._on_request,
            notification_handler=self._on_notification,
            name=os.path.basename(argv[0]),
        )
        self.peer.start()
        self._stderr_task = asyncio.ensure_future(self._drain_stderr())
        return self

    async def _drain_stderr(self) -> None:
        """Keep stderr flowing and remember the tail.

        Not draining it deadlocks the child once the pipe buffer fills,
        which presents as an agent that goes silent mid-turn for no
        visible reason. The tail is kept because an agent that refuses to
        start (missing model, bad config) says so on stderr and nowhere
        else.
        """
        stream = self.proc.stderr
        if stream is None:
            return
        try:
            while True:
                line = await stream.readline()
                if not line:
                    return
                text = line.decode("utf-8", "replace").rstrip()
                if not text:
                    continue
                self._stderr_tail.append(text)
                del self._stderr_tail[:-50]
                logger.debug("agent stderr: %s", text)
        except (asyncio.CancelledError, GeneratorExit):
            raise
        except Exception:
            return

    @property
    def stderr_tail(self) -> str:
        return "\n".join(self._stderr_tail)

    @property
    def alive(self) -> bool:
        return self.proc.returncode is None and not self._closed

    # ------------------------------------------------------------------
    # Inbound: the agent talking to us
    # ------------------------------------------------------------------

    async def _on_notification(self, method: str, params: dict) -> None:
        if method != M_SESSION_UPDATE:
            logger.debug("ignoring agent notification %s", method)
            return
        session_id = str(params.get("sessionId") or "")
        update = params.get("update")
        if not isinstance(update, dict):
            return
        event = parse_session_update(session_id, update)
        session = self.sessions.get(session_id)
        if session is None:
            logger.debug("update for unknown session %s", session_id)
            return
        session._record(event)

    async def _on_request(self, method: str, params: dict) -> Any:
        if method == M_REQUEST_PERMISSION:
            return await self._handle_permission(params)
        if method == M_FS_READ:
            return await asyncio.to_thread(self._fs_read, params)
        if method == M_FS_WRITE:
            return await asyncio.to_thread(self._fs_write, params)
        raise JsonRpcError(METHOD_NOT_FOUND, f"unsupported client method {method}")

    # -- filesystem, on the agent's behalf ------------------------------

    def _resolve_in_workspace(self, params: dict) -> str:
        """Absolute path, guaranteed to sit inside the session's workspace.

        The agent names the path, so the agent must not be able to name
        ``../../.ssh/authorized_keys``. Symlinks are resolved before the
        containment check, because a symlink planted inside the workspace
        is the obvious way around a prefix test.
        """
        session = self.sessions.get(str(params.get("sessionId") or ""))
        if session is None:
            raise JsonRpcError(INVALID_PARAMS, "unknown sessionId")
        raw = params.get("path")
        if not isinstance(raw, str) or not raw:
            raise JsonRpcError(INVALID_PARAMS, "path is required")
        if not os.path.isabs(raw):
            raw = os.path.join(session.cwd, raw)

        root = os.path.realpath(session.cwd)
        # The target may not exist yet (a write), so resolve its parent.
        resolved = os.path.realpath(raw)
        parent = os.path.realpath(os.path.dirname(raw))
        for candidate in (resolved, parent):
            if candidate != root and not candidate.startswith(root + os.sep):
                raise JsonRpcError(
                    INVALID_PARAMS,
                    f"path escapes the session workspace {root}",
                )
        return resolved

    def _fs_read(self, params: dict) -> dict[str, Any]:
        path = self._resolve_in_workspace(params)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()
        except OSError as exc:
            raise JsonRpcError(INTERNAL_ERROR, f"cannot read {path}: {exc}") from exc

        start = params.get("line")
        limit = params.get("limit")
        if isinstance(start, int) and start > 0:
            lines = lines[start - 1:]
        if isinstance(limit, int) and limit >= 0:
            lines = lines[:limit]
        return {"content": "".join(lines)}

    def _fs_write(self, params: dict) -> dict[str, Any]:
        path = self._resolve_in_workspace(params)
        content = params.get("content")
        if not isinstance(content, str):
            raise JsonRpcError(INVALID_PARAMS, "content must be a string")
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(content)
        except OSError as exc:
            raise JsonRpcError(INTERNAL_ERROR, f"cannot write {path}: {exc}") from exc
        logger.info("external agent wrote %s (%d bytes)", path, len(content))
        return {}

    async def _handle_permission(self, params: dict) -> dict[str, Any]:
        request = parse_permission_request(params)
        if not request.options:
            raise JsonRpcError(INVALID_PARAMS, "permission request offered no options")
        try:
            decision = await self.broker.decide(request)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("permission broker raised; denying %s", request.tool_name)
            from bridges.permissions import reject

            decision = reject(request, "approval surface failed")
        if not isinstance(decision, PermissionDecision):
            from bridges.permissions import reject

            decision = reject(request, "broker returned a non-decision")
        logger.info(
            "external-agent permission %s for %s: %s",
            "ALLOWED" if decision.allowed else "DENIED",
            request.tool_name,
            decision.reason or "-",
        )
        return decision.to_outcome()

    # ------------------------------------------------------------------
    # Outbound: us driving the agent
    # ------------------------------------------------------------------

    async def initialize(
        self,
        *,
        client_name: str = "feral",
        client_version: str = "1",
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        result = await self.peer.request(
            M_INITIALIZE,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "clientCapabilities": {
                    # True because opencode calls fs/write_text_file to
                    # apply an approved edit regardless of what we
                    # advertise; see the module docstring. Every such
                    # write is confined to the session workspace by
                    # ``_resolve_in_workspace``.
                    "fs": {"readTextFile": True, "writeTextFile": True},
                    "terminal": False,
                },
                "clientInfo": {"name": client_name, "version": client_version},
            },
            timeout=timeout,
        )
        if not isinstance(result, dict):
            raise AcpProtocolError(f"initialize returned {type(result).__name__}")
        version = result.get("protocolVersion")
        if isinstance(version, int):
            self.negotiated_version = version
            if version > PROTOCOL_VERSION:
                raise AcpProtocolError(
                    f"agent negotiated protocol v{version}; this client speaks "
                    f"v{PROTOCOL_VERSION}"
                )
        self.agent_capabilities = result.get("agentCapabilities") or {}
        self.agent_info = result.get("agentInfo") or {}
        return result

    async def new_session(
        self,
        cwd: str,
        *,
        mcp_servers: Optional[list[dict]] = None,
        timeout: float = 60.0,
    ) -> AcpSession:
        absolute = os.path.abspath(cwd)
        if not os.path.isdir(absolute):
            raise AcpProtocolError(f"session cwd does not exist: {absolute}")
        result = await self.peer.request(
            M_SESSION_NEW,
            {"cwd": absolute, "mcpServers": mcp_servers or []},
            timeout=timeout,
        )
        if not isinstance(result, dict) or not result.get("sessionId"):
            raise AcpProtocolError(f"session/new returned no sessionId: {result!r}")
        session = AcpSession(self, str(result["sessionId"]), absolute)
        self.sessions[session.session_id] = session
        return session

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    async def close(self, *, grace: float = 5.0) -> None:
        """Close every session, drop the connection, then end the child."""
        if self._closed:
            return
        self._closed = True

        for session in list(self.sessions.values()):
            with contextlib.suppress(Exception):
                await session.close()

        with contextlib.suppress(Exception):
            await self.peer.close("agent shutting down")

        if self.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), grace)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    self.proc.kill()
                with contextlib.suppress(Exception):
                    await self.proc.wait()

        if self._stderr_task is not None:
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._stderr_task
            self._stderr_task = None
