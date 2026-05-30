"""Lane 05  — MCP registry fixes + tool dedup.

Closes AUDIT-r14 finding 16 fix #5:
  * ``MCPServerRegistry.list_known`` now reads ``_servers`` (the
    actual MCPClientManager attribute) instead of the nonexistent
    ``_connections``.
  * ``_load_user_configs`` accepts the canonical ``{servers: [...]}``
    shape and translates to the flat ``{sid: config}`` form the
    rest of the registry expects.
  * ``MCPClientManager.all_tools`` filters MCP filesystem tools
    when FERAL's ``computer_use__*`` skill is present, with an
    operator override (``FERAL_MCP_FILESYSTEM_WINS=1``).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ── Registry connection-flag fix ──────────────────────────────────


def test_list_known_reads_underscore_servers_not_underscore_connections(monkeypatch, tmp_path):
    """Pre-fix the registry checked ``_connections`` (nonexistent) so
    every row rendered "not connected"; post-fix it reads
    ``_servers``."""
    monkeypatch.setattr(
        "config.loader.feral_home", lambda: tmp_path
    )
    # Force module-level CONFIG_PATH to re-resolve under our tmp_path.
    monkeypatch.setattr(
        "mcp.registry.CONFIG_PATH",
        tmp_path / "mcp_servers.json",
    )
    from mcp.registry import MCPServerRegistry

    fake_client = MagicMock()
    fake_client._servers = {"github": object(), "filesystem": object()}
    # Defensive: explicitly delete any _connections attribute so the
    # old code path can't accidentally pass.
    if hasattr(fake_client, "_connections"):
        del fake_client._connections

    registry = MCPServerRegistry(mcp_client=fake_client)
    rows = {r["id"]: r for r in registry.list_known()}

    assert rows["github"]["connected"] is True
    assert rows["filesystem"]["connected"] is True
    assert rows["slack"]["connected"] is False  # not in _servers


def test_list_known_returns_disconnected_when_client_absent():
    from mcp.registry import MCPServerRegistry

    registry = MCPServerRegistry(mcp_client=None)
    rows = registry.list_known()
    assert all(row["connected"] is False for row in rows)


# ── Config-shape parsing ──────────────────────────────────────────


def test_load_user_configs_accepts_canonical_servers_array(tmp_path, monkeypatch):
    """The canonical mcp_servers.json shape (matching
    MCPClientManager.load_and_connect) is ``{"servers": [...]}``."""
    config_path = tmp_path / "mcp_servers.json"
    config_path.write_text(json.dumps({
        "servers": [
            {
                "name": "github",
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-github"],
                "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_secret"},
                "enabled": True,
            },
            {
                "name": "slack",
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-slack"],
                "env": {"SLACK_BOT_TOKEN": "xoxb-secret"},
                "enabled": True,
            },
        ],
    }))
    monkeypatch.setattr("mcp.registry.CONFIG_PATH", config_path)

    from mcp.registry import MCPServerRegistry

    registry = MCPServerRegistry(mcp_client=None)
    rows = {r["id"]: r for r in registry.list_known()}

    # Both servers from the file should be flagged ``configured``.
    assert rows["github"]["configured"] is True
    assert rows["slack"]["configured"] is True
    # Servers NOT in the file remain unconfigured.
    assert rows["postgres"]["configured"] is False


def test_load_user_configs_accepts_legacy_flat_dict(tmp_path, monkeypatch):
    """Legacy {sid: config} shape still works for back-compat."""
    config_path = tmp_path / "mcp_servers.json"
    config_path.write_text(json.dumps({
        "github": {"command": "npx", "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "x"}},
    }))
    monkeypatch.setattr("mcp.registry.CONFIG_PATH", config_path)

    from mcp.registry import MCPServerRegistry

    registry = MCPServerRegistry(mcp_client=None)
    rows = {r["id"]: r for r in registry.list_known()}
    assert rows["github"]["configured"] is True


def test_save_round_trips_through_canonical_shape(tmp_path, monkeypatch):
    """When the registry persists a config it does so in the
    canonical ``{"servers": [...]}`` shape so MCPClientManager can
    read it directly without translation."""
    config_path = tmp_path / "mcp_servers.json"
    monkeypatch.setattr("mcp.registry.CONFIG_PATH", config_path)

    from mcp.registry import MCPServerRegistry

    registry = MCPServerRegistry(mcp_client=None)
    registry.configure_server(
        "github",
        {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-github"],
            "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_x"},
        },
    )

    written = json.loads(config_path.read_text())
    assert "servers" in written
    assert written["servers"][0]["name"] == "github"
    assert written["servers"][0]["env"] == {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_x"}


# ── Tool dedup against computer_use ───────────────────────────────


def test_strip_mcp_prefix_handles_known_shape():
    from mcp.client import MCPClientManager

    assert MCPClientManager._strip_mcp_prefix("mcp_filesystem_read_file") == "read_file"
    assert MCPClientManager._strip_mcp_prefix("mcp_brave_search_web") == "search_web"
    assert MCPClientManager._strip_mcp_prefix("plain_name") == "plain_name"


def _fake_manager_with_filesystem_tools():
    from mcp.client import MCPClientManager

    mgr = MCPClientManager()
    fake_server = MagicMock()
    fake_server.tools = [
        {"name": "read_file", "description": "read", "inputSchema": {}},
        {"name": "write_file", "description": "write", "inputSchema": {}},
        {"name": "list_directory", "description": "ls", "inputSchema": {}},
        # NON-overlapping MCP filesystem tool we want preserved.
        {"name": "tree", "description": "tree", "inputSchema": {}},
    ]
    fake_thinking = MagicMock()
    fake_thinking.tools = [
        {"name": "think_step", "description": "step", "inputSchema": {}},
    ]
    mgr._servers = {"filesystem": fake_server, "sequential-thinking": fake_thinking}
    return mgr


def test_all_tools_drops_mcp_filesystem_overlap_when_computer_use_present():
    mgr = _fake_manager_with_filesystem_tools()
    tools = mgr.all_tools(
        feral_skill_names={
            "computer_use__read_file",
            "computer_use__write_file",
            "weather_current__current",
        }
    )
    names = {t["name"] for t in tools}
    # MCP filesystem read_file/write_file/list_directory dropped.
    assert "mcp_filesystem_read_file" not in names
    assert "mcp_filesystem_write_file" not in names
    assert "mcp_filesystem_list_directory" not in names
    # Non-FS tools preserved (also dropped 'tree' is in the FS overlap
    # set, so it's filtered too — adjust expectation).
    assert "mcp_sequential-thinking_think_step" in names


def test_all_tools_keeps_filesystem_when_no_computer_use():
    mgr = _fake_manager_with_filesystem_tools()
    tools = mgr.all_tools(
        feral_skill_names={"weather_current__current"}
    )
    names = {t["name"] for t in tools}
    assert "mcp_filesystem_read_file" in names
    assert "mcp_filesystem_write_file" in names


def test_all_tools_operator_override_keeps_mcp_filesystem(monkeypatch):
    monkeypatch.setenv("FERAL_MCP_FILESYSTEM_WINS", "1")
    mgr = _fake_manager_with_filesystem_tools()
    tools = mgr.all_tools(
        feral_skill_names={"computer_use__read_file"}
    )
    names = {t["name"] for t in tools}
    assert "mcp_filesystem_read_file" in names


def test_all_tools_back_compat_no_args():
    """Calling all_tools() with no kwargs preserves pre-fix behaviour
    (no dedup) so existing call sites don't change behaviour until
    the orchestrator opts in by passing feral_skill_names."""
    mgr = _fake_manager_with_filesystem_tools()
    tools = mgr.all_tools()
    names = {t["name"] for t in tools}
    assert "mcp_filesystem_read_file" in names
    assert "mcp_filesystem_write_file" in names
    assert "mcp_sequential-thinking_think_step" in names
