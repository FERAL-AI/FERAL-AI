"""A stdio MCP server with many tools was unreadable.

MCP frames one JSON-RPC message per line.
``asyncio.create_subprocess_exec`` builds its ``StreamReader`` with the
library default limit of 64 KiB, and ``mcp/client.py`` passed no
``limit=``, so any server whose ``tools/list`` response exceeded 64 KiB
could not be read at all.

The failure was silent in the worst way:

  * ``readline()`` raises ``ValueError: Separator is not found, and chunk
    exceed the limit`` and the generic ``except Exception`` logged it at
    WARNING as "MCP request error".
  * ``_discover_tools`` therefore left ``self._tools`` empty while
    ``connect()`` returned True, so the Settings UI showed the server
    connected with zero tools and nothing said why.
  * ``readline`` also CLEARS its buffer on that path while the rest of
    the oversized line is still arriving, so the stream desynchronises:
    the next request reads the tail of the previous message and dies on
    ``Expecting value: line 1 column 1``.

Found by registering cua-driver 0.22.2, whose ``tools/list`` response
for its 56 tools is a single 141,876-byte line, i.e. 2.2x the default.
Nothing about it is exotic; a few dozen tools with real JSON Schemas
gets any server there.

These tests use a fake stdio server rather than cua-driver so they run
on a machine that has never installed it.
"""

from __future__ import annotations

import asyncio
import json
import sys

import pytest

from mcp.client import MCPServerConnection


# A minimal MCP stdio server. `pad` bytes of filler go into the
# tools/list response so the test can put the frame either side of the
# 64 KiB default.
FAKE_SERVER = r"""
import json, sys

pad = int(sys.argv[1])
tools = [{
    "name": "big_tool",
    "description": "x" * pad,
    "inputSchema": {"type": "object", "properties": {}},
}]

for raw in sys.stdin:
    raw = raw.strip()
    if not raw:
        continue
    msg = json.loads(raw)
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        continue
    if method == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {},
                  "serverInfo": {"name": "fake", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": tools}
    elif method == "resources/list":
        result = {"resources": []}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}) + "\n")
    sys.stdout.flush()
"""


def _conn(pad: int) -> MCPServerConnection:
    return MCPServerConnection("fake", {
        "name": "fake",
        "transport": "stdio",
        "command": sys.executable,
        "args": ["-c", FAKE_SERVER, str(pad)],
        "env": {},
    })


@pytest.mark.asyncio
async def test_a_tools_list_larger_than_64kib_is_still_read():
    """The regression. 200 KiB of padding puts the frame well past the
    64 KiB default and near cua-driver's real 141,876-byte response."""
    conn = _conn(200 * 1024)
    try:
        assert await conn.connect() is True
        assert len(conn.tools) == 1, (
            "tools/list was dropped: the stdio reader could not hold one "
            "frame, which is how a 56-tool server showed up as connected "
            "with zero tools"
        )
        assert conn.tools[0]["name"] == "big_tool"
    finally:
        await conn.disconnect()


@pytest.mark.asyncio
async def test_a_small_tools_list_still_works():
    """Guard: raising the limit must not change the ordinary path."""
    conn = _conn(16)
    try:
        assert await conn.connect() is True
        assert len(conn.tools) == 1
    finally:
        await conn.disconnect()


@pytest.mark.asyncio
async def test_the_stream_stays_in_sync_for_the_request_after_a_big_one():
    """The second half of the bug. A frame that overruns the reader
    leaves the tail of that line in the pipe, so the NEXT read returns
    garbage. `resources/list` is issued right after `tools/list` during
    connect, which is exactly where that showed up."""
    conn = _conn(200 * 1024)
    try:
        assert await conn.connect() is True
        # Reached only if the reader is still framing correctly.
        result = await conn._send_request("tools/list", {})
        assert result is not None
        assert result["result"]["tools"][0]["name"] == "big_tool"
    finally:
        await conn.disconnect()


@pytest.mark.asyncio
async def test_a_frame_over_the_configured_limit_fails_loudly(monkeypatch):
    """The limit is raised, not removed. A server that still overruns it
    must drop the connection with a message naming the server and the
    knob, instead of sitting there connected and empty."""
    monkeypatch.setenv("FERAL_MCP_STDIO_LINE_LIMIT", "4096")
    conn = _conn(64 * 1024)
    try:
        await conn.connect()
        assert conn.tools == []
        assert "stdio line limit" in conn.last_error, conn.last_error
        assert "FERAL_MCP_STDIO_LINE_LIMIT" in conn.last_error
        assert conn.is_connected is False, (
            "a desynchronised stream must not keep reporting connected"
        )
    finally:
        await conn.disconnect()


@pytest.mark.asyncio
async def test_malformed_json_is_not_reported_as_an_oversized_frame():
    """`json.JSONDecodeError` IS a `ValueError`, so the oversize handler
    has to be scoped to the read itself. Catching both together would
    tear down a connection over one bad message and print the wrong
    remedy."""
    bad_server = (
        "import sys\n"
        "for raw in sys.stdin:\n"
        "    sys.stdout.write('this is not json\\n')\n"
        "    sys.stdout.flush()\n"
    )
    conn = MCPServerConnection("bad", {
        "name": "bad", "transport": "stdio", "command": sys.executable,
        "args": ["-c", bad_server], "env": {},
    })
    try:
        await conn.connect()
        assert "stdio line limit" not in conn.last_error, conn.last_error
    finally:
        await conn.disconnect()


def test_the_limit_env_override_rejects_junk(monkeypatch):
    conn = _conn(16)
    default = conn._STDIO_LINE_LIMIT_DEFAULT

    monkeypatch.setenv("FERAL_MCP_STDIO_LINE_LIMIT", "not-a-number")
    assert conn._stdio_line_limit == default

    monkeypatch.setenv("FERAL_MCP_STDIO_LINE_LIMIT", "0")
    assert conn._stdio_line_limit == default

    monkeypatch.setenv("FERAL_MCP_STDIO_LINE_LIMIT", str(2 * 1024 * 1024))
    assert conn._stdio_line_limit == 2 * 1024 * 1024


def test_the_default_limit_clears_a_real_world_tools_list():
    """cua-driver 0.22.2 measured at 141,876 bytes for 56 tools. The
    default has to clear that with room for the servers that are larger,
    or the fix only moves the cliff."""
    conn = _conn(16)
    assert conn._STDIO_LINE_LIMIT_DEFAULT > 141_876 * 4


def test_asyncio_default_would_not_have_been_enough():
    """Pins the premise: the stdlib default really is 64 KiB, so this
    fix is required rather than defensive."""
    assert asyncio.streams._DEFAULT_LIMIT == 64 * 1024
    assert json  # keeps the import honest for the fake server payloads
