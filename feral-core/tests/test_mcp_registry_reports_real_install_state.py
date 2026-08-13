"""`MCPServerRegistry` claimed every npx server was installed.

`_check_installed` was:

    if cmd == "npx":
        return shutil.which("npx") is not None

`npx` is one binary that can launch any package on npm, so this asks
"is npm's package runner present" and answers "the GitHub MCP server is
installed". Measured on the audit machine, where `npx` is at
/opt/homebrew/bin/npx and exactly two MCP packages are actually cached
locally (`@modelcontextprotocol/server-filesystem`, `server-memory`):

    stats()          -> {'known_servers': 9, 'installed': 9, ...}
    auto_discover()  -> all 9 ids, each {'installed': True}

Seven of those nine were not on the machine in any form. `ready` is
derived from `installed`, so it inherited the same lie, and the Settings
UI rendered "installed / ready" for servers that did not exist.

The distinction that matters to a user is not two-valued. A package
cached locally starts instantly; a package npx has to fetch works only
with a network and takes tens of seconds on first launch; a missing npx
cannot start anything at all. These tests pin all three.
"""

from __future__ import annotations

import json

import pytest

from mcp.registry import MCPServerRegistry


@pytest.fixture()
def registry(tmp_path, monkeypatch):
    monkeypatch.setattr("mcp.registry.CONFIG_PATH", tmp_path / "mcp_servers.json")
    return MCPServerRegistry(mcp_client=None)


def _row(rows, sid):
    return next(r for r in rows if r["id"] == sid)


def test_npx_on_path_does_not_mean_a_package_is_installed(registry, monkeypatch):
    """The core lie. npx present, no packages cached anywhere."""
    monkeypatch.setattr("mcp.registry.shutil.which", lambda c: "/usr/bin/npx" if c == "npx" else None)
    monkeypatch.setattr(registry, "_npx_package_roots", lambda: [])

    rows = registry.list_known()

    assert all(r["installed"] is False for r in rows), (
        "no package is cached, yet servers report installed"
    )
    assert registry.stats()["installed"] == 0


def test_locally_cached_package_reports_installed(registry, monkeypatch, tmp_path):
    cache = tmp_path / "node_modules"
    (cache / "@modelcontextprotocol" / "server-memory").mkdir(parents=True)
    monkeypatch.setattr("mcp.registry.shutil.which", lambda c: "/usr/bin/npx" if c == "npx" else None)
    monkeypatch.setattr(registry, "_npx_package_roots", lambda: [cache])

    rows = registry.list_known()

    assert _row(rows, "memory")["installed"] is True
    assert _row(rows, "github")["installed"] is False


def test_install_state_distinguishes_cached_from_fetch_on_launch(registry, monkeypatch, tmp_path):
    cache = tmp_path / "node_modules"
    (cache / "@modelcontextprotocol" / "server-memory").mkdir(parents=True)
    monkeypatch.setattr("mcp.registry.shutil.which", lambda c: "/usr/bin/npx" if c == "npx" else None)
    monkeypatch.setattr(registry, "_npx_package_roots", lambda: [cache])

    rows = registry.list_known()

    assert _row(rows, "memory")["install_state"] == "installed"
    assert _row(rows, "github")["install_state"] == "fetch_on_launch"


def test_missing_npx_is_its_own_state_with_an_actionable_detail(registry, monkeypatch):
    """"npx is not installed" and "the package is not cached" need
    different remedies; collapsing them sends the user to the wrong fix."""
    monkeypatch.setattr("mcp.registry.shutil.which", lambda c: None)

    rows = registry.list_known()

    row = _row(rows, "github")
    assert row["install_state"] == "unavailable"
    assert row["installed"] is False
    assert row["ready"] is False
    assert "npx" in row["install_detail"].lower()
    assert "node" in row["install_detail"].lower()


def test_fetch_on_launch_says_it_will_download(registry, monkeypatch):
    monkeypatch.setattr("mcp.registry.shutil.which", lambda c: "/usr/bin/npx" if c == "npx" else None)
    monkeypatch.setattr(registry, "_npx_package_roots", lambda: [])

    row = _row(registry.list_known(), "memory")

    assert "download" in row["install_detail"].lower()
    assert "@modelcontextprotocol/server-memory" in row["install_detail"]


def test_ready_survives_fetch_on_launch(registry, monkeypatch):
    """npx CAN start an uncached package, so `ready` must stay True.

    Tying `ready` to `installed` after making `installed` honest would
    turn a working server into a greyed-out row, which is a regression
    dressed as a fix.
    """
    monkeypatch.setattr("mcp.registry.shutil.which", lambda c: "/usr/bin/npx" if c == "npx" else None)
    monkeypatch.setattr(registry, "_npx_package_roots", lambda: [])

    row = _row(registry.list_known(), "memory")  # no required env

    assert row["installed"] is False
    assert row["ready"] is True


def test_auto_discover_only_returns_genuinely_present_packages(registry, monkeypatch, tmp_path):
    cache = tmp_path / "node_modules"
    (cache / "@modelcontextprotocol" / "server-filesystem").mkdir(parents=True)
    monkeypatch.setattr("mcp.registry.shutil.which", lambda c: "/usr/bin/npx" if c == "npx" else None)
    monkeypatch.setattr(registry, "_npx_package_roots", lambda: [cache])

    found = registry.auto_discover()

    assert [d["id"] for d in found] == ["filesystem"]


def test_non_npx_command_still_uses_path(registry, monkeypatch):
    """A server launched by its own binary is genuinely installed when
    that binary is on PATH; this path must not regress."""
    registry.register_custom("local", {
        "id": "local", "name": "Local", "command": "my-mcp-server", "args": [], "env": {},
    })
    monkeypatch.setattr("mcp.registry.shutil.which",
                        lambda c: "/usr/local/bin/my-mcp-server" if c == "my-mcp-server" else None)

    row = _row(registry.list_known(), "local")

    assert row["installed"] is True
    assert row["install_state"] == "installed"


def test_package_name_skips_npx_flags(registry):
    """`args` is `["-y", "@scope/pkg", "/some/path"]`; naive [0] picks `-y`."""
    assert registry._npx_package_name(["-y", "@modelcontextprotocol/server-filesystem", "/home/u"]) == \
        "@modelcontextprotocol/server-filesystem"
    assert registry._npx_package_name(["--yes", "pkg"]) == "pkg"
    assert registry._npx_package_name(["-p", "pkg", "bin"]) == "pkg"
    assert registry._npx_package_name([]) == ""


def test_stats_reports_launchable_separately_from_installed(registry, monkeypatch):
    """An operator needs "how many can I start" as well as "how many are
    on disk"; the old single `installed` number conflated them."""
    monkeypatch.setattr("mcp.registry.shutil.which", lambda c: "/usr/bin/npx" if c == "npx" else None)
    monkeypatch.setattr(registry, "_npx_package_roots", lambda: [])

    s = registry.stats()

    assert s["installed"] == 0
    assert s["launchable"] == 9


def test_user_config_shape_still_loads(registry, tmp_path, monkeypatch):
    """Guard: the install-state work must not disturb config loading."""
    path = tmp_path / "mcp_servers.json"
    path.write_text(json.dumps({"servers": [{"name": "github", "command": "npx", "args": []}]}))
    monkeypatch.setattr("mcp.registry.CONFIG_PATH", path)

    r = MCPServerRegistry(mcp_client=None)

    assert _row(r.list_known(), "github")["configured"] is True
