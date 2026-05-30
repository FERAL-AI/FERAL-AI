"""
FERAL MCP Client — Consume Any MCP Server
=============================================
FERAL can connect to external MCP servers and use their tools.
This instantly gives FERAL access to thousands of existing tools
without writing any adapters.

Examples:
  - Connect to a GitHub MCP server → FERAL can manage repos
  - Connect to a Postgres MCP server → FERAL can query databases
  - Connect to a Notion MCP server → FERAL can manage pages
  - Connect to another FERAL instance → multi-Brain collaboration

Configuration via ~/.feral/mcp_servers.json:
{
  "servers": [
    {
      "name": "github",
      "transport": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {"GITHUB_TOKEN": "..."}
    }
  ]
}
"""

from __future__ import annotations
import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional, Union

import httpx
from pydantic import BaseModel, ConfigDict, Field

from config.loader import feral_home

logger = logging.getLogger("feral.mcp.client")

# audit-r12 D6: MCP Streamable HTTP transport per spec rev 2025-06-18.
# Pre-r12 the HTTP branch in :meth:`MCPServerConnection.connect` set
# ``self._connected = True`` and never spoke protocol — every
# subsequent ``call_tool`` returned ``{error: "No response"}`` because
# ``_send_request`` blindly tried to read from a stdio process that
# didn't exist. This module now implements the full Streamable HTTP
# flow (single endpoint, POST + optional SSE, ``Mcp-Session-Id``
# propagation, ``MCP-Protocol-Version`` header) using ``httpx``.
_MCP_PROTOCOL_VERSION = "2025-06-18"


# audit-r12 D7: ONE canonical config shape for MCP servers.
#
# Pre-r12 each layer invented its own keys:
#
# * ``MCPServerRegistry.connect_server`` passed ``server_id, command,
#   args, env`` as flat kwargs to a non-existent
#   ``MCPClientManager.connect(...)`` method.
# * ``MCPServerConnection.__init__`` expected ``{transport, command,
#   args, env}`` inside a dict.
# * ``api/routes/mcp.py:mcp_connect`` constructed the
#   ``MCPServerConnection`` directly from request JSON (no validation).
# * ``mcp_servers.json`` adds ``name`` and ``enabled`` on top.
#
# All five layers now go through this model. The Pydantic schema
# is the contract; everything else is a thin alias or a back-
# compat shim.
class MCPServerConfig(BaseModel):
    """Canonical MCP server configuration.

    The :func:`mcp_connect` HTTP endpoint validates against this; the
    registry constructs it; the client manager stores it. Adding a
    field here is the *only* place to add MCP config; do not invent
    parallel shapes in callers.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    name: str = Field(..., description="Stable server id used to address the connection")
    transport: str = Field(
        "stdio",
        description="Transport protocol — ``stdio`` (default) or ``http`` (Streamable HTTP, MCP rev 2025-06-18)",
    )
    command: str = Field(
        "",
        description="Executable to launch (stdio transport only)",
    )
    args: list[str] = Field(
        default_factory=list,
        description="Argument vector for ``command``",
    )
    env: dict[str, str] = Field(
        default_factory=dict,
        description="Environment overlay merged on top of ``os.environ``",
    )
    url: str = Field(
        "",
        description="MCP endpoint URL (http/https) — required for the ``http`` transport",
    )
    headers: dict[str, str] = Field(
        default_factory=dict,
        description="Extra HTTP headers (e.g. ``Authorization``) sent on every request to the MCP endpoint",
    )
    enabled: bool = Field(
        True,
        description="Set to ``False`` in ``mcp_servers.json`` to keep the entry but skip auto-connect",
    )

    def to_connection_kwargs(self) -> dict[str, Any]:
        """Project to the dict shape :class:`MCPServerConnection`'s
        ``connect()`` reads from ``self.config`` so the connection can
        keep its existing introspection surface during the migration."""
        return {
            "transport": self.transport,
            "command": self.command,
            "args": list(self.args),
            "env": dict(self.env),
            "url": self.url,
            "headers": dict(self.headers),
            "enabled": self.enabled,
        }


class MCPServerConnection:
    """A connection to a single external MCP server."""

    def __init__(self, name: str, config: Union[MCPServerConfig, dict, None] = None):
        # audit-r12 D7: accept either the canonical model or the
        # legacy dict shape. New callers pass MCPServerConfig; old
        # callers (config files, tests) keep working unchanged.
        if isinstance(config, MCPServerConfig):
            self._config_model: Optional[MCPServerConfig] = config
            config_dict: dict[str, Any] = {"name": name, **config.to_connection_kwargs()}
        else:
            raw = dict(config or {})
            # Be lenient — coerce only on the fields we read below.
            raw.setdefault("name", name)
            try:
                self._config_model = MCPServerConfig(**raw)
                config_dict = raw
            except Exception:
                self._config_model = None
                config_dict = raw
        self.name = name
        self.config = config_dict
        self.transport = config_dict.get("transport", "stdio")
        # audit-r12 D6: HTTP transport state. ``_http_client`` is the
        # long-lived httpx connection pool; ``_http_url`` is the MCP
        # endpoint; ``_session_id`` is the ``Mcp-Session-Id`` issued
        # by the server at initialize time (spec rev 2025-06-18 § Session Management).
        self._http_client: Optional[httpx.AsyncClient] = None
        self._http_url: str = config_dict.get("url", "")
        self._http_headers: dict[str, str] = dict(config_dict.get("headers", {}) or {})
        self._session_id: Optional[str] = None
        try:
            self._http_request_timeout = float(
                os.environ.get("FERAL_MCP_HTTP_TIMEOUT_S", "30")
            )
        except ValueError:
            self._http_request_timeout = 30.0
        self._process: Optional[asyncio.subprocess.Process] = None
        self._tools: list[dict] = []
        self._resources: list[dict] = []
        self._request_id = 0
        self._connected = False
        self._request_failures = 0
        self._request_failure_fuse = max(
            1, int(os.environ.get("FERAL_MCP_REQUEST_FAILURE_FUSE", "3"))
        )

    async def connect(self) -> bool:
        # Reconnect path: tear down old process first to avoid zombie stdio
        # children holding stale pipes.
        if self._process is not None and self.transport == "stdio":
            await self.disconnect()
        if self._http_client is not None and self.transport == "http":
            await self.disconnect()
        if self.transport == "stdio":
            return await self._connect_stdio()
        elif self.transport == "http":
            return await self._connect_http()
        logger.warning(f"Unsupported transport: {self.transport}")
        return False

    async def _connect_stdio(self) -> bool:
        command = self.config.get("command", "")
        args = self.config.get("args", [])
        env = {**os.environ, **self.config.get("env", {})}

        try:
            self._process = await asyncio.create_subprocess_exec(
                command, *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )

            init_result = await self._send_request("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "feral", "version": "1.0.0"},
            })

            if init_result and "error" not in init_result:
                await self._send_notification("initialized", {})
                self._connected = True
                self._request_failures = 0
                await self._discover_tools()
                await self._discover_resources()
                logger.info(f"MCP server connected: {self.name} ({len(self._tools)} tools, {len(self._resources)} resources)")
                return True

        except FileNotFoundError:
            logger.error(f"MCP server command not found: {command}")
        except Exception as e:
            logger.error(f"MCP server connection failed ({self.name}): {e}")

        if self._process is not None:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=2)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass
            self._process = None
        self._connected = False
        return False

    async def _connect_http(self) -> bool:
        """Connect to an MCP server over Streamable HTTP (spec rev
        :data:`_MCP_PROTOCOL_VERSION`).

        Spec: https://modelcontextprotocol.io/specification/2025-06-18/basic/transports#streamable-http

        Flow:
        1. Build a long-lived :class:`httpx.AsyncClient` pointed at
           the configured endpoint URL.
        2. POST an ``initialize`` JSON-RPC request, advertising the
           protocol version we speak.
        3. Capture any ``Mcp-Session-Id`` returned in the response
           headers — required on every subsequent request per § Session
           Management.
        4. POST a ``notifications/initialized`` to complete the
           handshake (server replies 202 Accepted with no body).
        5. Discover tools and resources so the manager can advertise
           them immediately.
        """
        if not self._http_url:
            logger.error(
                "MCP HTTP server '%s' has no `url` in config — refusing to connect",
                self.name,
            )
            return False
        try:
            self._http_client = httpx.AsyncClient(
                base_url="",  # we pass absolute URLs on every call
                timeout=httpx.Timeout(self._http_request_timeout),
                follow_redirects=True,
            )
        except Exception as exc:
            logger.error("MCP HTTP client construction failed (%s): %s", self.name, exc)
            self._http_client = None
            return False

        try:
            init_resp = await self._send_request(
                "initialize",
                {
                    "protocolVersion": _MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "feral", "version": "1.0.0"},
                },
            )
        except Exception as exc:
            logger.error("MCP HTTP initialize failed (%s): %s", self.name, exc)
            await self._teardown_http_client()
            return False

        if not init_resp or "error" in init_resp:
            err = (init_resp or {}).get("error") if init_resp else None
            logger.error(
                "MCP HTTP initialize rejected by %s: %s", self.name, err,
            )
            await self._teardown_http_client()
            return False

        # Per spec § Protocol Version Header, every request AFTER
        # initialize MUST carry the negotiated MCP-Protocol-Version —
        # including the ``notifications/initialized`` notification.
        # Flip ``_connected`` (which drives the header builder) before
        # sending the notification so the spec is honoured.
        self._connected = True
        self._request_failures = 0
        await self._send_notification("notifications/initialized", {})
        await self._discover_tools()
        await self._discover_resources()
        logger.info(
            "MCP server connected over HTTP: %s (%d tools, %d resources, session=%s)",
            self.name, len(self._tools), len(self._resources),
            self._session_id or "<none>",
        )
        return True

    async def _teardown_http_client(self) -> None:
        if self._http_client is not None:
            try:
                await self._http_client.aclose()
            except Exception:
                pass
            self._http_client = None
        self._session_id = None
        self._connected = False

    async def disconnect(self):
        if self.transport == "http":
            # Best-effort polite disconnect per spec § Session
            # Management: send DELETE so the server can release
            # session state. Server may respond 405 (which the spec
            # explicitly allows); we treat any response as "done".
            if self._http_client is not None and self._session_id is not None:
                try:
                    await self._http_client.request(
                        "DELETE",
                        self._http_url,
                        headers=self._build_http_headers(include_session=True),
                    )
                except Exception:
                    # Disconnection is silent — the server might already
                    # have torn the session down.
                    pass
            await self._teardown_http_client()
            return
        if self._process:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._process.kill()
        self._process = None
        self._connected = False

    async def _discover_tools(self):
        result = await self._send_request("tools/list", {})
        if result and "result" in result:
            self._tools = result["result"].get("tools", [])

    async def _discover_resources(self):
        result = await self._send_request("resources/list", {})
        if result and "result" in result:
            self._resources = result["result"].get("resources", [])

    async def call_tool(self, tool_name: str, arguments: dict) -> dict:
        if not self._connected:
            return {"error": f"MCP server {self.name} not connected"}
        result = await self._send_request("tools/call", {
            "name": tool_name,
            "arguments": arguments,
        })
        if result and "result" in result:
            return result["result"]
        return {"error": result.get("error", {}).get("message", "Unknown error") if result else "No response"}

    async def read_resource(self, uri: str) -> dict:
        if not self._connected:
            return {"error": f"MCP server {self.name} not connected"}
        result = await self._send_request("resources/read", {"uri": uri})
        if result and "result" in result:
            return result["result"]
        return {"error": "Failed to read resource"}

    async def _send_request(self, method: str, params: dict) -> Optional[dict]:
        self._request_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params,
        }
        if self.transport == "http":
            return await self._send_http_request(request)
        if not self._process or not self._process.stdin or not self._process.stdout:
            return None
        if self._process.returncode is not None:
            self._connected = False
            return None

        try:
            line = json.dumps(request) + "\n"
            self._process.stdin.write(line.encode())
            await self._process.stdin.drain()

            response_line = await asyncio.wait_for(
                self._process.stdout.readline(), timeout=30,
            )
            if response_line:
                decoded = json.loads(response_line.decode().strip())
                self._request_failures = 0
                return decoded
            self._request_failures += 1
        except asyncio.TimeoutError:
            logger.warning(f"MCP request timed out: {method}")
            self._request_failures += 1
        except Exception as e:
            logger.warning(f"MCP request error: {e}")
            self._request_failures += 1
        if self._request_failures >= self._request_failure_fuse:
            logger.error(
                "MCP connection fuse opened for %s after %d request failures",
                self.name,
                self._request_failures,
            )
            self._connected = False
        return None

    async def _send_notification(self, method: str, params: dict):
        notification = {"jsonrpc": "2.0", "method": method, "params": params}
        if self.transport == "http":
            await self._send_http_notification(notification)
            return
        if not self._process or not self._process.stdin:
            return
        try:
            line = json.dumps(notification) + "\n"
            self._process.stdin.write(line.encode())
            await self._process.stdin.drain()
        except Exception:
            pass

    # ─────────────────────────────────────────────
    # audit-r12 D6: Streamable HTTP transport (spec rev 2025-06-18)
    # ─────────────────────────────────────────────

    def _build_http_headers(self, *, include_session: bool = True) -> dict[str, str]:
        """Per-request HTTP headers. The spec requires:

        * ``Accept`` listing both ``application/json`` AND
          ``text/event-stream`` on every POST (§ Sending Messages);
        * ``MCP-Protocol-Version`` on every request *after*
          initialize so the server can route by version
          (§ Protocol Version Header);
        * ``Mcp-Session-Id`` echoed on every request if the server
          issued one (§ Session Management).
        """
        headers: dict[str, str] = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        # Don't claim a protocol version until the server has
        # acknowledged it — the spec is explicit that the header
        # carries the negotiated version, not a claim.
        if self._connected:
            headers["MCP-Protocol-Version"] = _MCP_PROTOCOL_VERSION
        if include_session and self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        headers.update(self._http_headers)
        return headers

    async def _send_http_request(self, request: dict) -> Optional[dict]:
        """POST a JSON-RPC request to the MCP endpoint and decode the
        reply. The server MAY answer with one of:

        * ``Content-Type: application/json`` + a single JSON-RPC
          response object;
        * ``Content-Type: text/event-stream`` + an SSE stream that
          eventually carries the JSON-RPC response (the server is
          allowed to send unrelated requests/notifications first);
        * HTTP 202 Accepted for notifications/responses (no body);
        * HTTP 4xx/5xx — surfaced as a JSON-RPC-style error so
          ``call_tool`` can return ``{"error": ...}`` cleanly.
        """
        if self._http_client is None:
            return None
        method = request.get("method", "<unknown>")
        try:
            resp = await self._http_client.post(
                self._http_url,
                content=json.dumps(request).encode("utf-8"),
                headers=self._build_http_headers(include_session=True),
            )
        except httpx.RequestError as exc:
            logger.warning("MCP HTTP request error (%s, %s): %r", self.name, method, exc)
            self._request_failures += 1
            self._maybe_open_request_fuse()
            return None

        # Spec: server MAY assign Mcp-Session-Id during initialize and
        # SHOULD respond 404 on stale session ids. Handle session
        # propagation BEFORE any error-handling so the very first
        # initialize response stores the id.
        sid = resp.headers.get("Mcp-Session-Id") or resp.headers.get("mcp-session-id")
        if sid:
            self._session_id = sid
        if resp.status_code == 404 and self._session_id is not None:
            # Server torn down the session — drop our id so the next
            # request reinitializes from scratch.
            logger.info("MCP HTTP session %s expired; clearing session id", self.name)
            self._session_id = None
            return {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "error": {"code": -32000, "message": "session expired (HTTP 404)"},
            }
        if resp.status_code == 202:
            # Acknowledged with no body — only legal for notifications
            # or responses. Treat as success with an empty result.
            self._request_failures = 0
            return None
        if resp.status_code >= 400:
            # Try to surface a JSON-RPC error body if the server sent one.
            error_body: dict[str, Any]
            try:
                parsed = resp.json()
            except Exception:
                parsed = None
            if isinstance(parsed, dict) and "error" in parsed:
                error_body = parsed["error"]
            else:
                error_body = {
                    "code": -32000,
                    "message": f"HTTP {resp.status_code}",
                    "data": resp.text[:512] if resp.text else None,
                }
            self._request_failures += 1
            self._maybe_open_request_fuse()
            return {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "error": error_body,
            }

        content_type = (resp.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type == "application/json":
            try:
                decoded = resp.json()
                self._request_failures = 0
                return decoded
            except Exception as exc:
                logger.warning("MCP HTTP JSON decode failed (%s): %r", self.name, exc)
                self._request_failures += 1
                self._maybe_open_request_fuse()
                return None
        if content_type == "text/event-stream":
            decoded = await self._read_sse_until_response(resp, request.get("id"))
            if decoded is not None:
                self._request_failures = 0
            return decoded
        logger.warning(
            "MCP HTTP unexpected Content-Type from %s: %s", self.name, content_type,
        )
        self._request_failures += 1
        self._maybe_open_request_fuse()
        return None

    async def _send_http_notification(self, notification: dict) -> None:
        if self._http_client is None:
            return
        try:
            await self._http_client.post(
                self._http_url,
                content=json.dumps(notification).encode("utf-8"),
                headers=self._build_http_headers(include_session=True),
            )
        except Exception as exc:
            logger.debug("MCP HTTP notification (%s) failed: %s", self.name, exc)

    async def _read_sse_until_response(
        self, resp: httpx.Response, request_id: Any,
    ) -> Optional[dict]:
        """Read an SSE stream until we see the JSON-RPC response with
        matching ``id``. Per spec, the server MAY emit unrelated
        requests/notifications first; we ignore those for now (a full
        bidirectional implementation would dispatch them, but FERAL's
        current MCP client model is request/response only)."""
        try:
            event_data: list[str] = []
            async for raw_line in resp.aiter_lines():
                if not raw_line:
                    # Blank line = event boundary. Concatenate any
                    # ``data:`` lines we've collected and try to parse.
                    if event_data:
                        payload = "\n".join(event_data)
                        event_data = []
                        try:
                            msg = json.loads(payload)
                        except Exception:
                            continue
                        # Skip server-initiated requests/notifications.
                        if isinstance(msg, dict) and "id" in msg and msg.get("id") == request_id:
                            return msg
                    continue
                if raw_line.startswith(":"):
                    # SSE comment per spec — ignore.
                    continue
                if raw_line.startswith("data:"):
                    event_data.append(raw_line[5:].lstrip())
                    continue
                # ``id:``, ``event:`` and ``retry:`` lines are
                # acceptable but we don't currently use them.
            # Stream ended without a matching response.
            return None
        except Exception as exc:
            logger.debug("MCP HTTP SSE read failed (%s): %s", self.name, exc)
            return None

    def _maybe_open_request_fuse(self) -> None:
        if self._request_failures >= self._request_failure_fuse:
            logger.error(
                "MCP connection fuse opened for %s after %d request failures",
                self.name, self._request_failures,
            )
            self._connected = False

    @property
    def tools(self) -> list[dict]:
        return self._tools

    @property
    def resources(self) -> list[dict]:
        return self._resources

    @property
    def is_connected(self) -> bool:
        return self._connected


class MCPClientManager:
    """
    Manages connections to multiple external MCP servers.
    Auto-discovers servers from ~/.feral/mcp_servers.json.
    """

    def __init__(self, config_path: Optional[str] = None):
        self._config_path = Path(config_path) if config_path else feral_home() / "mcp_servers.json"
        self._servers: dict[str, MCPServerConnection] = {}
        self._server_configs: dict[str, dict] = {}
        self._degraded_servers: dict[str, dict] = {}
        self._reconnect_not_before: dict[str, float] = {}
        self._connect_max_attempts = max(
            1, int(os.environ.get("FERAL_MCP_RECONNECT_MAX_ATTEMPTS", "4"))
        )
        self._connect_backoff_cap_sec = max(
            1, int(os.environ.get("FERAL_MCP_RECONNECT_BACKOFF_CAP_SEC", "60"))
        )

    def _mark_degraded(self, name: str, reason: str, attempts: int) -> None:
        self._degraded_servers[name] = {
            "state": "DEGRADED",
            "reason": reason,
            "attempts": attempts,
        }

    def _clear_degraded(self, name: str) -> None:
        self._degraded_servers.pop(name, None)
        self._reconnect_not_before.pop(name, None)

    async def _connect_with_retries(self, conn: MCPServerConnection) -> bool:
        delay = 1.0
        attempts = 0
        for attempts in range(1, self._connect_max_attempts + 1):
            ok = await conn.connect()
            if ok:
                self._clear_degraded(conn.name)
                return True
            if attempts < self._connect_max_attempts:
                backoff = min(delay, float(self._connect_backoff_cap_sec))
                logger.warning(
                    "MCP reconnect retry: server=%s attempt=%d/%d backoff=%.1fs",
                    conn.name,
                    attempts,
                    self._connect_max_attempts,
                    backoff,
                )
                await asyncio.sleep(backoff)
                delay = min(backoff * 2.0, float(self._connect_backoff_cap_sec))

        self._mark_degraded(
            conn.name,
            f"connection failed after {attempts} attempts",
            attempts,
        )
        self._reconnect_not_before[conn.name] = time.time() + float(self._connect_backoff_cap_sec)
        logger.error(
            "MCP server marked DEGRADED: %s (attempts=%d)",
            conn.name,
            attempts,
        )
        return False

    async def _try_reconnect(self, name: str) -> bool:
        conn = self._servers.get(name)
        if conn is None:
            return False
        now = time.time()
        not_before = float(self._reconnect_not_before.get(name, 0.0) or 0.0)
        if now < not_before:
            return False
        ok = await self._connect_with_retries(conn)
        if ok:
            self._clear_degraded(name)
            return True
        self._reconnect_not_before[name] = now + float(self._connect_backoff_cap_sec)
        return False

    async def load_and_connect(self):
        """Load MCP server configs and connect to all."""
        self._degraded_servers.pop("__config__", None)
        if not self._config_path.exists():
            logger.info("No MCP servers configured (create ~/.feral/mcp_servers.json)")
            return

        try:
            with open(self._config_path, encoding="utf-8") as f:
                config = json.load(f)
        except Exception as e:
            self._mark_degraded("__config__", f"invalid config: {e}", attempts=1)
            logger.error("MCP config load failed: %s", e)
            return

        for server_config in config.get("servers", []):
            name = server_config.get("name", "unnamed")
            if not server_config.get("enabled", True):
                continue

            self._server_configs[name] = dict(server_config)
            conn = MCPServerConnection(name, server_config)
            success = await self._connect_with_retries(conn)
            if success:
                self._servers[name] = conn

        logger.info(f"MCP Client: {len(self._servers)} servers connected")

    async def disconnect_all(self):
        for conn in self._servers.values():
            await conn.disconnect()
        self._servers.clear()
        self._server_configs.clear()
        self._degraded_servers.clear()
        self._reconnect_not_before.clear()

    # ─────────────────────────────────────────────
    # audit-r12 D7: canonical per-server connect/disconnect API.
    # ─────────────────────────────────────────────
    #
    # Pre-r12 there was no per-server connect path on the manager —
    # ``MCPServerRegistry.connect_server`` was calling a method that
    # didn't exist (``self._mcp_client.connect(...)``), and
    # ``api/routes/mcp.py:mcp_connect`` reached straight into
    # ``state.mcp_client._servers[name] = conn``. Both paths now go
    # through these two methods.

    async def connect_server(self, config: Union[MCPServerConfig, dict]) -> bool:
        """Start a single MCP server using the canonical
        :class:`MCPServerConfig`.

        Idempotent on ``config.name``: if a connection already exists
        we disconnect it first so configuration changes (e.g. updated
        env vars) take effect.
        """
        if not isinstance(config, MCPServerConfig):
            config = MCPServerConfig(**config)
        name = config.name
        if name in self._servers:
            try:
                await self._servers[name].disconnect()
            except Exception as exc:
                logger.debug("MCP disconnect during replace failed: %s", exc)
            del self._servers[name]
        self._server_configs[name] = {"name": name, **config.to_connection_kwargs()}
        conn = MCPServerConnection(name, config)
        ok = await self._connect_with_retries(conn)
        if ok:
            self._servers[name] = conn
        return ok

    async def disconnect_server(self, name: str) -> bool:
        """Disconnect a single MCP server by name. Returns True if a
        connection was actually torn down, False if the name was
        unknown."""
        conn = self._servers.pop(name, None)
        self._server_configs.pop(name, None)
        self._clear_degraded(name)
        if conn is None:
            return False
        try:
            await conn.disconnect()
        except Exception as exc:
            logger.debug("MCP disconnect_server failed for %s: %s", name, exc)
        return True

    # Back-compat aliases that match what ``MCPServerRegistry`` used to
    # try to call. Kept narrow on purpose — new code should call
    # ``connect_server`` / ``disconnect_server``.
    async def connect(self, **kwargs: Any) -> bool:
        """Deprecated kwargs-only alias for :meth:`connect_server`. The
        old (broken) :class:`MCPServerRegistry` code path called
        ``manager.connect(server_id=..., command=..., args=...,
        env=...)``; we accept that shape here so the in-flight migration
        of older operator scripts doesn't break. New code MUST use
        :meth:`connect_server` with a :class:`MCPServerConfig`."""
        # Translate the legacy kwargs shape to the canonical model.
        name = kwargs.pop("server_id", None) or kwargs.pop("name", None)
        if not name:
            raise TypeError(
                "MCPClientManager.connect() requires server_id= (or name=) "
                "— pre-r12 this method didn't exist; use connect_server() "
                "with an MCPServerConfig instead."
            )
        # `kwargs` carries command/args/env/transport at this point.
        return await self.connect_server(MCPServerConfig(name=name, **kwargs))

    async def disconnect(self, name: str) -> bool:
        """Deprecated alias for :meth:`disconnect_server` to match the
        symmetry of :meth:`connect`."""
        return await self.disconnect_server(name)

    def get_server(self, name: str) -> Optional[MCPServerConnection]:
        return self._servers.get(name)

    # ─────────────────────────────────────────────────────────────
    # Lane 05  (AUDIT-r14 finding 16 fix #5): tool dedup against
    # the canonical FERAL skill set.
    #
    # The MCP filesystem reference server exposes 14 file primitives
    # (read_file, write_file, list_directory, ...) that overlap with
    # the FERAL ``computer_use__*`` skill 1:1. When both are present
    # in the same LLM tool list:
    #
    #   * OpenAI / Anthropic models truncate after ~128 tools, so
    #     the duplicated MCP filesystem tools push *real* MCP tools
    #     (sequential-thinking, github, brave-search) off the end of
    #     the list.
    #   * Models that obey both manifests can confuse intent ("which
    #     read_file does the user want?") and increment the wrong
    #     metric on dispatch.
    #
    # We default to FERAL-wins (drop MCP filesystem) when the
    # canonical skill list contains a ``computer_use__*`` entry. The
    # operator can override by setting
    # ``FERAL_MCP_FILESYSTEM_WINS=1`` in env. Both halves of the
    # cascade are observable: the dropped tool count is logged once
    # per all_tools() call so the dashboard can render a "duplicate
    # MCP tools suppressed" badge.
    # ─────────────────────────────────────────────────────────────

    # Canonical filesystem-shaped tool stems exposed by the MCP
    # filesystem reference server. We compare against the un-prefixed
    # tool name (the MCP-server prefix is stripped before the test).
    _MCP_FS_TOOL_NAMES = frozenset({
        "read_file",
        "read_multiple_files",
        "write_file",
        "edit_file",
        "create_directory",
        "list_directory",
        "directory_tree",
        "move_file",
        "search_files",
        "get_file_info",
        "list_allowed_directories",
        "tree",
    })

    @staticmethod
    def _strip_mcp_prefix(name: str) -> str:
        """``mcp_filesystem_read_file`` → ``read_file``."""
        if not name.startswith("mcp_"):
            return name
        rest = name[4:]
        # The MCP server name is everything between ``mcp_`` and the
        # next underscore boundary — but server names themselves can
        # contain underscores (e.g. ``brave-search`` becomes
        # ``brave_search`` in the prefix). We can't recover the exact
        # split without the server list, so we strip the *first*
        # token only; that's good enough for the FS overlap check.
        if "_" not in rest:
            return rest
        return rest.split("_", 1)[1]

    def all_tools(
        self,
        *,
        feral_skill_names: Optional[set[str]] = None,
        prefer_mcp_filesystem: Optional[bool] = None,
    ) -> list[dict]:
        """Get all tools from all connected MCP servers, prefixed with
        server name.

        When *feral_skill_names* is provided AND it contains any
        ``computer_use__*`` entry, MCP filesystem tools whose names
        match :data:`_MCP_FS_TOOL_NAMES` are filtered out by default
        so the LLM never sees both halves of the duplicate.

        ``prefer_mcp_filesystem`` overrides the default policy:
          * True: keep MCP filesystem tools, drop any
            ``computer_use__*`` overlap from FERAL-side tool lists
            (the orchestrator handles the FERAL side; we just signal
            via the return value).
          * False: drop MCP filesystem tools (the default when FERAL
            has computer_use__*).
          * None: read from ``FERAL_MCP_FILESYSTEM_WINS`` env (default
            False — FERAL wins).
        """
        if prefer_mcp_filesystem is None:
            prefer_mcp_filesystem = (
                os.getenv("FERAL_MCP_FILESYSTEM_WINS", "").lower()
                in ("1", "true", "yes")
            )

        feral_has_computer_use = bool(
            feral_skill_names
            and any(s.startswith("computer_use__") for s in feral_skill_names)
        )
        drop_mcp_fs = feral_has_computer_use and not prefer_mcp_filesystem

        tools: list[dict] = []
        dropped = 0
        for name, server in self._servers.items():
            for tool in server.tools:
                prefixed_name = f"mcp_{name}_{tool['name']}"
                if (
                    drop_mcp_fs
                    and tool.get("name") in self._MCP_FS_TOOL_NAMES
                ):
                    dropped += 1
                    continue
                tools.append({**tool, "name": prefixed_name})

        if dropped:
            logger.info(
                "MCP tool dedup: dropped %d filesystem-overlap tools "
                "(set FERAL_MCP_FILESYSTEM_WINS=1 to keep them)",
                dropped,
            )
        return tools

    def all_resources(self) -> list[dict]:
        resources = []
        for name, server in self._servers.items():
            for resource in server.resources:
                resources.append({**resource, "server": name})
        return resources

    async def call_tool(self, prefixed_name: str, arguments: dict) -> dict:
        """Call a tool by its prefixed name (mcp_servername_toolname)."""
        if not prefixed_name.startswith("mcp_"):
            return {"error": "Not an MCP tool"}

        rest = prefixed_name[4:]
        for name in self._servers:
            if rest.startswith(f"{name}_"):
                tool_name = rest[len(name) + 1:]
                conn = self._servers[name]
                if not conn.is_connected:
                    await self._try_reconnect(name)
                if not conn.is_connected:
                    degraded = self._degraded_servers.get(name, {})
                    reason = degraded.get("reason") or "not connected"
                    return {"error": f"MCP server {name} unavailable ({reason})"}

                result = await conn.call_tool(tool_name, arguments)
                # One auto-reconnect chance when the call itself drops the
                # transport/fuse.
                if isinstance(result, dict) and result.get("error") and not conn.is_connected:
                    if await self._try_reconnect(name):
                        result = await conn.call_tool(tool_name, arguments)
                return result

        return {"error": f"MCP server not found for tool: {prefixed_name}"}

    def to_llm_tool_definitions(self) -> list[dict]:
        """Convert MCP tools to the format the LLM orchestrator expects."""
        definitions = []
        for tool in self.all_tools():
            definitions.append({
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("inputSchema", {}),
                },
            })
        return definitions

    @property
    def stats(self) -> dict:
        server_states = {
            name: ("connected" if conn.is_connected else "degraded")
            for name, conn in self._servers.items()
        }
        for name in self._degraded_servers:
            if name == "__config__":
                continue
            server_states.setdefault(name, "degraded")
        return {
            "servers_connected": len(self._servers),
            "total_tools": sum(len(s.tools) for s in self._servers.values()),
            "total_resources": sum(len(s.resources) for s in self._servers.values()),
            "server_names": list(self._servers.keys()),
            "server_states": server_states,
            "degraded_servers": dict(self._degraded_servers),
            "degraded_count": len(self._degraded_servers),
        }
