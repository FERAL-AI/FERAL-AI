"""-A11 regression tests for MCP runtime failure containment."""

from __future__ import annotations

import json

import pytest

from mcp.client import MCPClientManager, MCPServerConnection

pytestmark = pytest.mark.no_auto_feral_home


@pytest.mark.asyncio
async def test_load_and_connect_marks_degraded_when_retries_exhausted(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = tmp_path / "mcp_servers.json"
    cfg.write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "name": "unstable",
                        "transport": "stdio",
                        "command": "never-exists",
                        "enabled": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    async def _always_fail(self) -> bool:  # pragma: no cover - monkeypatched hook
        return False

    monkeypatch.setattr(MCPServerConnection, "connect", _always_fail)

    mgr = MCPClientManager(config_path=str(cfg))
    mgr._connect_max_attempts = 1
    await mgr.load_and_connect()

    assert mgr.get_server("unstable") is None
    stats = mgr.stats
    assert stats["degraded_count"] == 1
    assert "unstable" in stats["degraded_servers"]
    assert stats["degraded_servers"]["unstable"]["state"] == "DEGRADED"


@pytest.mark.asyncio
async def test_call_tool_reconnects_disconnected_server_once() -> None:
    class _Conn:
        def __init__(self) -> None:
            self.name = "demo"
            self.tools = []
            self.resources = []
            self._connected = False
            self.connect_calls = 0
            self.call_calls = 0

        @property
        def is_connected(self) -> bool:
            return self._connected

        async def connect(self) -> bool:
            self.connect_calls += 1
            self._connected = True
            return True

        async def call_tool(self, tool_name: str, arguments: dict) -> dict:
            self.call_calls += 1
            return {"ok": True, "tool": tool_name, "arguments": arguments}

    mgr = MCPClientManager(config_path="/tmp/unused.json")
    conn = _Conn()
    mgr._servers["demo"] = conn  # type: ignore[assignment]
    mgr._connect_max_attempts = 1

    out = await mgr.call_tool("mcp_demo_ping", {"n": 1})

    assert out.get("ok") is True
    assert conn.connect_calls == 1
    assert conn.call_calls == 1


@pytest.mark.asyncio
async def test_call_tool_reports_degraded_when_reconnect_fails() -> None:
    class _Conn:
        def __init__(self) -> None:
            self.name = "down"
            self.tools = []
            self.resources = []
            self._connected = False
            self.connect_calls = 0
            self.call_calls = 0

        @property
        def is_connected(self) -> bool:
            return self._connected

        async def connect(self) -> bool:
            self.connect_calls += 1
            return False

        async def call_tool(self, tool_name: str, arguments: dict) -> dict:
            self.call_calls += 1
            return {"ok": True}

    mgr = MCPClientManager(config_path="/tmp/unused.json")
    conn = _Conn()
    mgr._servers["down"] = conn  # type: ignore[assignment]
    mgr._connect_max_attempts = 1

    out = await mgr.call_tool("mcp_down_ping", {})

    assert "unavailable" in (out.get("error") or "")
    assert conn.connect_calls == 1
    assert conn.call_calls == 0
    assert "down" in mgr.stats["degraded_servers"]
