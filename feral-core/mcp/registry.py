"""
FERAL MCP Server Registry — Catalog of Known Servers
=======================================================
Pre-configured definitions for popular MCP servers plus
auto-discovery of locally installed ones.

Users can browse, install, and connect to MCP servers through
the Setup Wizard or Settings UI.
"""

from __future__ import annotations
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Optional

from config.loader import feral_home

logger = logging.getLogger("feral.mcp.registry")

CONFIG_PATH = feral_home() / "mcp_servers.json"


KNOWN_SERVERS = {
    "github": {
        "id": "github",
        "name": "GitHub",
        "description": "Manage repos, issues, PRs, and code search",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": ""},
        "install_hint": "npm install -g @modelcontextprotocol/server-github",
        "category": "development",
    },
    "filesystem": {
        "id": "filesystem",
        "name": "Filesystem",
        "description": "Read/write files and directories",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", str(Path.home())],
        "env": {},
        "install_hint": "npm install -g @modelcontextprotocol/server-filesystem",
        "category": "system",
    },
    "slack": {
        "id": "slack",
        "name": "Slack",
        "description": "Send messages, manage channels, search conversations",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-slack"],
        "env": {"SLACK_BOT_TOKEN": "", "SLACK_TEAM_ID": ""},
        "install_hint": "npm install -g @modelcontextprotocol/server-slack",
        "category": "communication",
    },
    "brave-search": {
        "id": "brave-search",
        "name": "Brave Search",
        "description": "Web search via Brave Search API",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-brave-search"],
        "env": {"BRAVE_API_KEY": ""},
        "install_hint": "npm install -g @modelcontextprotocol/server-brave-search",
        "category": "search",
    },
    "memory": {
        "id": "memory",
        "name": "Memory",
        "description": "Persistent memory via knowledge graph",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-memory"],
        "env": {},
        "install_hint": "npm install -g @modelcontextprotocol/server-memory",
        "category": "memory",
    },
    "postgres": {
        "id": "postgres",
        "name": "PostgreSQL",
        "description": "Query and manage PostgreSQL databases",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-postgres"],
        "env": {"POSTGRES_CONNECTION_STRING": ""},
        "install_hint": "npm install -g @modelcontextprotocol/server-postgres",
        "category": "database",
    },
    "puppeteer": {
        "id": "puppeteer",
        "name": "Puppeteer",
        "description": "Browser automation for web scraping and testing",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-puppeteer"],
        "env": {},
        "install_hint": "npm install -g @modelcontextprotocol/server-puppeteer",
        "category": "browser",
    },
    "google-maps": {
        "id": "google-maps",
        "name": "Google Maps",
        "description": "Geocoding, directions, and place search",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-google-maps"],
        "env": {"GOOGLE_MAPS_API_KEY": ""},
        "install_hint": "npm install -g @modelcontextprotocol/server-google-maps",
        "category": "location",
    },
    "sequential-thinking": {
        "id": "sequential-thinking",
        "name": "Sequential Thinking",
        "description": "Step-by-step reasoning and problem solving",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-sequential-thinking"],
        "env": {},
        "install_hint": "npm install -g @modelcontextprotocol/server-sequential-thinking",
        "category": "reasoning",
    },
}


class MCPServerRegistry:
    """
    Catalog of known MCP servers with installation status,
    configuration, and auto-discovery.
    """

    def __init__(self, mcp_client=None):
        self._mcp_client = mcp_client
        self._known = dict(KNOWN_SERVERS)
        self._user_configs: dict[str, dict] = {}
        self._load_user_configs()

    def _load_user_configs(self):
        """Load ``~/.feral/mcp_servers.json`` into a flat
        ``{server_id: config}`` dict regardless of which on-disk
        shape the operator used.

        AUDIT-r14 finding 16 fix #5: pre-fix ``_user_configs`` ate the
        whole file as one nested dict. Since the canonical shape
        (matching ``MCPClientManager.load_and_connect``) is
        ``{"servers": [{"name": ..., ...}, ...]}``, the only key
        that ended up at the top level was ``"servers"`` — which made
        ``configured = sid in self._user_configs`` always False (and
        the Settings UI render every row as "not configured" even
        for servers the operator had explicitly enabled).

        We now accept three shapes, in order of preference:
          1. ``{"servers": [{name, command, ...}, ...]}`` — canonical
             (re-exports straight to the manager).
          2. ``{"<sid>": {command, args, env, ...}, ...}`` — flat
             dict of configs (legacy, but still supported).
          3. anything else — logged + ignored.
        """
        if not CONFIG_PATH.exists():
            return
        try:
            raw = json.loads(CONFIG_PATH.read_text())
        except Exception as e:
            logger.warning(f"Failed to load MCP server configs: {e}")
            return
        flat: dict[str, dict] = {}
        if isinstance(raw, dict) and isinstance(raw.get("servers"), list):
            for entry in raw["servers"]:
                if not isinstance(entry, dict):
                    continue
                sid = entry.get("name") or entry.get("id")
                if not sid:
                    continue
                flat[str(sid)] = dict(entry)
        elif isinstance(raw, dict):
            for sid, cfg in raw.items():
                if isinstance(cfg, dict):
                    flat[str(sid)] = dict(cfg)
        else:
            logger.warning(
                "MCP server config has unrecognised shape (%s) — ignoring",
                type(raw).__name__,
            )
        self._user_configs = flat

    def _save_user_configs(self):
        """Persist user configs in the canonical
        ``{"servers": [...]}`` shape so ``MCPClientManager.load_and_connect``
        can read the file without translation."""
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        servers_array = []
        for sid, cfg in self._user_configs.items():
            entry = dict(cfg)
            entry.setdefault("name", sid)
            servers_array.append(entry)
        CONFIG_PATH.write_text(
            json.dumps({"servers": servers_array}, indent=2)
        )

    def list_known(self) -> list[dict]:
        """List all known MCP servers with installation and connection status."""
        result = []
        for sid, server in self._known.items():
            installed = self._check_installed(server)
            # AUDIT-r14 finding 16 fix #5: ``MCPClientManager`` exposes
            # ``_servers`` (the canonical name); the pre-fix code looked
            # up ``_connections`` which doesn't exist, so every row in
            # the Settings UI rendered as "not connected" even when the
            # server was actually live.
            connected = bool(
                self._mcp_client
                and sid in getattr(self._mcp_client, "_servers", {})
            )
            configured = sid in self._user_configs
            has_required_env = self._check_env(server)

            result.append({
                **server,
                "installed": installed,
                "connected": connected,
                "configured": configured,
                "ready": installed and has_required_env,
            })
        return result

    def list_by_category(self) -> dict[str, list[dict]]:
        """Group known servers by category."""
        servers = self.list_known()
        categories = {}
        for s in servers:
            cat = s.get("category", "other")
            if cat not in categories:
                categories[cat] = []
            categories[cat].append(s)
        return categories

    def get_server_config(self, server_id: str) -> Optional[dict]:
        """Get the full config for a server (known defaults + user overrides)."""
        base = self._known.get(server_id, {}).copy()
        user = self._user_configs.get(server_id, {})
        base.update(user)
        return base if base else None

    def configure_server(self, server_id: str, config: dict):
        """Save user configuration for a server (env vars, custom args, etc.)."""
        self._user_configs[server_id] = config
        self._save_user_configs()
        logger.info(f"MCP server configured: {server_id}")

    async def connect_server(self, server_id: str) -> dict:
        """Start and connect to an MCP server.

        audit-r12 D7: pre-r12 this called
        ``self._mcp_client.connect(server_id=, command=, args=, env=)``
        — a method that didn't exist on
        :class:`mcp.client.MCPClientManager`. Phone clients and the
        Settings UI both routed through here, so every "Connect server"
        action silently 404'd into an ``AttributeError`` and the
        registry returned ``{error: ...}`` with no real diagnostic.

        Now: build a canonical :class:`MCPServerConfig` and hand it to
        :meth:`MCPClientManager.connect_server`.
        """
        config = self.get_server_config(server_id)
        if not config:
            return {"error": f"Unknown server: {server_id}"}

        if not self._mcp_client:
            return {"error": "MCP client not available"}

        try:
            from mcp.client import MCPServerConfig
            model = MCPServerConfig(
                name=server_id,
                transport=config.get("transport", "stdio"),
                command=config.get("command", ""),
                args=list(config.get("args", []) or []),
                env=dict(config.get("env", {}) or {}),
                enabled=bool(config.get("enabled", True)),
            )
            ok = await self._mcp_client.connect_server(model)
            if not ok:
                return {"error": f"Failed to connect to MCP server '{server_id}'"}
            return {"ok": True, "server": server_id}
        except Exception as e:
            return {"error": str(e)}

    async def disconnect_server(self, server_id: str) -> dict:
        if not self._mcp_client:
            return {"error": "MCP client not available"}
        try:
            torn_down = await self._mcp_client.disconnect_server(server_id)
            if not torn_down:
                return {"error": f"MCP server '{server_id}' was not connected"}
            return {"ok": True, "server": server_id}
        except Exception as e:
            return {"error": str(e)}

    def auto_discover(self) -> list[dict]:
        """
        Discover locally installed MCP servers by checking common locations.
        Checks npx availability and node_modules.
        """
        discovered = []
        npx = shutil.which("npx")
        if not npx:
            return discovered

        for sid, server in self._known.items():
            if self._check_installed(server):
                discovered.append({"id": sid, "name": server["name"], "installed": True})

        return discovered

    def _check_installed(self, server: dict) -> bool:
        """Check if the MCP server's command is available."""
        cmd = server.get("command", "")
        if cmd == "npx":
            return shutil.which("npx") is not None
        return shutil.which(cmd) is not None

    def _check_env(self, server: dict) -> bool:
        """Check if all required env vars are set."""
        env_reqs = server.get("env", {})
        for key, default_val in env_reqs.items():
            if not default_val and not os.getenv(key):
                user_env = self._user_configs.get(server.get("id", ""), {}).get("env", {})
                if not user_env.get(key):
                    return False
        return True

    def register_custom(self, server_id: str, config: dict):
        """Register a custom MCP server not in the known list."""
        self._known[server_id] = config
        self._user_configs[server_id] = config
        self._save_user_configs()

    def stats(self) -> dict:
        installed = sum(1 for s in self._known.values() if self._check_installed(s))
        return {
            "known_servers": len(self._known),
            "installed": installed,
            "configured": len(self._user_configs),
        }
