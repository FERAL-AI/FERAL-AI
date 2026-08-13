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
            install_state, install_detail = self._resolve_install_state(server)
            installed = install_state == "installed"
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
                "install_state": install_state,
                "install_detail": install_detail,
                "connected": connected,
                "configured": configured,
                # `ready` means "we can attempt a launch", which npx can
                # do for an uncached package. Deriving it from the now
                # truthful `installed` would grey out every working
                # npx server, turning a fix into a regression.
                "ready": install_state != "unavailable" and has_required_env,
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
                # "Failed to connect to X" restates the request. The
                # cause lives on the connection (child stderr, missing
                # command) and in the manager's degraded record; both
                # existed and neither reached the caller.
                # Both lookups are best-effort: a caller may hand us a
                # manager stub without `stats` / `get_server`, and
                # failing to READ a diagnostic must never replace the
                # diagnostic with an AttributeError.
                degraded = {}
                try:
                    degraded = ((getattr(self._mcp_client, "stats", None) or {})
                                .get("degraded_servers", {})
                                .get(server_id, {})) or {}
                except Exception:
                    degraded = {}
                detail = degraded.get("detail") or ""
                if not detail:
                    try:
                        conn = self._mcp_client.get_server(server_id)
                    except Exception:
                        conn = None
                    detail = getattr(conn, "last_error", "") or ""
                install_state, install_detail = self._resolve_install_state(config)
                if not detail and install_state == "unavailable":
                    detail = install_detail
                return {
                    "error": f"Failed to connect to MCP server '{server_id}'",
                    "detail": detail or "No cause was captured by the client.",
                    "install_state": install_state,
                    "attempts": degraded.get("attempts"),
                }
            return {"ok": True, "server": server_id}
        except Exception as e:
            return {"error": str(e), "detail": f"{type(e).__name__}: {e}"}

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
        """Discover MCP servers whose package is genuinely on this machine.

        The docstring used to promise it "checks npx availability and
        node_modules" and only ever checked the former, so it returned
        every known server the moment npx existed. It now checks
        node_modules for real (see `_npx_package_roots`) and returns
        only servers that would start without a download.
        """
        discovered = []
        for sid, server in self._known.items():
            if self._check_installed(server):
                discovered.append({"id": sid, "name": server["name"], "installed": True})
        return discovered

    # ------------------------------------------------------------------
    # Install-state resolution
    # ------------------------------------------------------------------
    #
    # `npx` is a single binary that can launch ANY package on npm, so
    # `shutil.which("npx") is not None` answers "is npm's runner
    # present", not "is this server installed". The pre-fix
    # `_check_installed` returned the former as the latter, which on the
    # audit machine reported all 9 known servers installed while exactly
    # 2 packages existed on disk. `ready` is derived from `installed`,
    # so it inherited the lie and the Settings UI showed seven servers
    # as ready that were not present in any form.

    @staticmethod
    def _npx_package_name(args: list) -> str:
        """The package `npx` would run, skipping its own flags.

        `args` is shaped `["-y", "@scope/pkg", "/some/path"]`; taking
        `args[0]` yields `-y`. Flags that consume a value (`-p`,
        `--package`) name the package in the NEXT token, which is also
        the one we want, so they need no special case beyond being
        skipped themselves.
        """
        for arg in args or []:
            token = str(arg)
            if token.startswith("-"):
                continue
            return token
        return ""

    @staticmethod
    def _npx_package_roots() -> list[Path]:
        """Directories where an already-fetched npm package can live.

        Deliberately filesystem-only, no subprocess: this runs on every
        render of the Settings page and `npm ls -g` costs hundreds of
        milliseconds. Covers npx's own cache, the global prefix derived
        from the `npm` binary's location, `NODE_PATH`, and the cwd.
        """
        roots: list[Path] = []
        npx_cache = Path.home() / ".npm" / "_npx"
        if npx_cache.is_dir():
            try:
                for entry in npx_cache.iterdir():
                    node_modules = entry / "node_modules"
                    if node_modules.is_dir():
                        roots.append(node_modules)
            except OSError:
                pass
        npm_bin = shutil.which("npm")
        if npm_bin:
            # /opt/homebrew/bin/npm -> /opt/homebrew/lib/node_modules
            roots.append(Path(npm_bin).resolve().parent.parent / "lib" / "node_modules")
        for entry in (os.environ.get("NODE_PATH") or "").split(os.pathsep):
            if entry.strip():
                roots.append(Path(entry.strip()))
        roots.append(Path.cwd() / "node_modules")
        return [r for r in roots if r.is_dir()]

    def _resolve_install_state(self, server: dict) -> tuple[str, str]:
        """Return ``(install_state, detail)``.

        Three states, because the remedies differ:

        * ``installed`` - the package (or the server's own binary) is on
          this machine and starts without a network.
        * ``fetch_on_launch`` - `npx` is present but the package is not
          cached. It WILL start, after downloading, and only with a
          working network. Reporting this as installed is what made a
          30-second first connect look like a hang.
        * ``unavailable`` - the launcher itself is missing. Nothing can
          start and the fix is to install Node, not the package.
        """
        cmd = str(server.get("command", "") or "")
        if not cmd:
            return "unavailable", "No launch command configured for this server."
        if cmd != "npx":
            if shutil.which(cmd):
                return "installed", f"{cmd} found on PATH."
            return "unavailable", (
                f"Launch command {cmd!r} is not on PATH. Install it, or set an "
                f"absolute path in ~/.feral/mcp_servers.json."
            )
        if not shutil.which("npx"):
            return "unavailable", (
                "npx is not on PATH. Install Node.js (which provides npm and "
                "npx) and restart the brain."
            )
        package = self._npx_package_name(server.get("args", []) or [])
        if not package:
            return "fetch_on_launch", (
                "npx is available but this server declares no package to run."
            )
        for root in self._npx_package_roots():
            if (root / package).is_dir():
                return "installed", f"{package} found in {root}."
        return "fetch_on_launch", (
            f"npx is available but {package} is not cached locally. The first "
            f"connect will download it, which needs a network and can take "
            f"tens of seconds. Pre-install with: npm install -g {package}"
        )

    def _check_installed(self, server: dict) -> bool:
        """True only when the server can start WITHOUT a download."""
        return self._resolve_install_state(server)[0] == "installed"

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
        states = [self._resolve_install_state(s)[0] for s in self._known.values()]
        return {
            "known_servers": len(self._known),
            # On disk now, starts with no network.
            "installed": sum(1 for s in states if s == "installed"),
            # Can be started at all, counting the ones npx would fetch.
            # Kept separate from `installed` because the old single
            # number meant both and was therefore true of neither.
            "launchable": sum(1 for s in states if s != "unavailable"),
            "fetch_on_launch": sum(1 for s in states if s == "fetch_on_launch"),
            "unavailable": sum(1 for s in states if s == "unavailable"),
            "configured": len(self._user_configs),
        }
