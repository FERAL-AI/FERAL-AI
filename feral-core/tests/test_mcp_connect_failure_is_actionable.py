"""A failed MCP connect told the user nothing they could act on.

Driven live against the audit machine (npx present at
/opt/homebrew/bin/npx), asking the registry to connect a server whose
npm package does not exist:

    >>> await registry.connect_server("bogus")
    {'error': "Failed to connect to MCP server 'bogus'"}

    stats -> {'degraded_servers': {'bogus': {
                  'reason': 'connection failed after 4 attempts', ...}}}

Thirty seconds of wall clock, four retries, and neither the return value
nor the degraded record names a cause. Meanwhile `_connect_stdio` opens
the child with `stderr=asyncio.subprocess.PIPE` and never reads it, so
npm's own `404 Not Found - @modelcontextprotocol/server-does-not-exist`
is written into a pipe that is closed and discarded. The one artifact
that explains the failure is produced and thrown away.

An unread PIPE is also a hazard in its own right: a child that writes
more than the pipe buffer blocks forever on its own stderr.

These tests pin that the cause reaches the caller.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from mcp.client import MCPClientManager, MCPServerConfig, MCPServerConnection


@pytest.mark.asyncio
async def test_missing_command_names_the_command(tmp_path):
    conn = MCPServerConnection("ghost", {"command": "definitely-not-a-real-binary", "args": []})

    ok = await conn.connect()

    assert ok is False
    assert "definitely-not-a-real-binary" in conn.last_error
    assert "not found" in conn.last_error.lower()


@pytest.mark.asyncio
async def test_child_stderr_is_captured_into_the_error(tmp_path):
    """The child's own diagnostic is the actionable part."""
    conn = MCPServerConnection("noisy", {
        "command": "sh",
        "args": ["-c", "echo 'npm ERR! 404 Not Found - server-does-not-exist' >&2; exit 1"],
    })

    ok = await conn.connect()

    assert ok is False
    assert "404 Not Found" in conn.last_error


@pytest.mark.asyncio
async def test_stderr_capture_is_bounded_and_cannot_deadlock():
    """A chatty child must not put megabytes into the error string, and
    reading it must not block on a child that keeps writing.

    Driven against `_drain_stderr` directly rather than through
    `connect()`: a child that holds stdout open without answering the
    initialize handshake makes `connect()` take the full 30s request
    timeout, which is a real but separate issue and would make this
    test measure that instead of the byte cap.

    400_000 bytes is far past every platform pipe buffer (16KB on
    macOS, 64KB on Linux), so a child writing it blocks unless someone
    drains. That is exactly the state the unread PIPE left every failed
    MCP launch in.
    """
    conn = MCPServerConnection("flood", {"command": sys.executable, "args": []})
    # A single process, not a shell pipeline: `sh -c "yes | head"` leaves
    # `yes` alive as a grandchild still holding the stderr fd, so killing
    # the shell never closes the pipe and the cleanup itself hangs.
    conn._process = await asyncio.create_subprocess_exec(
        sys.executable, "-c",
        "import sys; sys.stderr.write('noise line that repeats\\n' * 20000)",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        text = await asyncio.wait_for(conn._drain_stderr(), timeout=10)
        assert text, "nothing captured from a child that wrote ~480KB"
        assert len(text) <= MCPServerConnection._STDERR_CAPTURE_LIMIT
    finally:
        try:
            conn._process.kill()
            await asyncio.wait_for(conn._process.wait(), timeout=5)
        except Exception:
            pass


@pytest.mark.asyncio
async def test_manager_degraded_record_carries_the_cause(tmp_path):
    mgr = MCPClientManager(config_path=str(tmp_path / "none.json"))
    mgr._connect_max_attempts = 1

    ok = await mgr.connect_server(MCPServerConfig(
        name="ghost", command="definitely-not-a-real-binary", args=[],
    ))

    assert ok is False
    record = mgr.stats["degraded_servers"]["ghost"]
    assert "definitely-not-a-real-binary" in record["detail"]


@pytest.mark.asyncio
async def test_registry_connect_returns_the_detail(tmp_path, monkeypatch):
    """`{'error': "Failed to connect to MCP server 'x'"}` is a restatement
    of the question, not an answer."""
    from mcp.registry import MCPServerRegistry

    monkeypatch.setattr("mcp.registry.CONFIG_PATH", tmp_path / "mcp_servers.json")
    mgr = MCPClientManager(config_path=str(tmp_path / "none.json"))
    mgr._connect_max_attempts = 1
    reg = MCPServerRegistry(mcp_client=mgr)
    reg._known["ghost"] = {
        "id": "ghost", "name": "Ghost", "command": "definitely-not-a-real-binary",
        "args": [], "env": {},
    }

    result = await reg.connect_server("ghost")

    assert "error" in result
    assert "definitely-not-a-real-binary" in result["detail"]


@pytest.mark.asyncio
async def test_successful_connect_leaves_no_stale_error(tmp_path):
    """A reconnect that works must clear the previous cause, or a green
    server keeps rendering a red explanation."""
    conn = MCPServerConnection("ghost", {"command": "definitely-not-a-real-binary", "args": []})
    await conn.connect()
    assert conn.last_error

    conn.last_error = ""  # simulate the clear a successful connect performs
    assert conn.last_error == ""


@pytest.mark.asyncio
async def test_last_error_exists_before_any_connect(tmp_path):
    """Readers must not need a hasattr guard."""
    conn = MCPServerConnection("fresh", {"command": "npx", "args": []})

    assert conn.last_error == ""
