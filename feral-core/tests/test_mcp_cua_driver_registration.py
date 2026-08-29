"""cua-driver is a first-class but OPTIONAL MCP server.

Two properties have to hold at once, and they pull in opposite
directions:

1. **First-class.** It is in ``KNOWN_SERVERS`` with the same field shape
   as every other entry, its launch command is the one the tool itself
   prints from ``cua-driver mcp-config`` (``cua-driver mcp``), and the
   registry's install-state resolution answers honestly for it. It is
   the first entry that is NOT an npx package, so it is also the first
   one to exercise the non-npx branch of ``_resolve_install_state`` from
   the shipped catalog rather than from a test-registered stub.

2. **Optional.** Nothing connects it. An operator who has never heard of
   cua-driver must get no startup cost, no connection attempt, and no
   error - only a row in the Settings catalog they can ignore. That is a
   property of the *boot path*, so the test that pins it reads the boot
   path rather than the entry.

The doctor side of the same feature is pinned in
``tests/test_doctor_cua_driver.py``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from mcp.registry import KNOWN_SERVERS, MCPServerRegistry


CORE_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setattr("mcp.registry.CONFIG_PATH", tmp_path / "mcp_servers.json")
    return MCPServerRegistry(mcp_client=None)


def _row(rows, sid):
    return next(r for r in rows if r["id"] == sid)


# ── 1. The entry exists and matches the catalog's shape ───────────────


def test_cua_driver_is_a_known_server():
    assert "cua-driver" in KNOWN_SERVERS


def test_cua_driver_has_every_field_the_other_entries_have():
    """A partial entry renders a broken Settings row rather than an
    error, so the shape is asserted against the catalog's own union of
    keys instead of a hand-written list that could drift."""
    others = [v for k, v in KNOWN_SERVERS.items() if k != "cua-driver"]
    required = set(others[0])
    for entry in others[1:]:
        required &= set(entry)

    entry = KNOWN_SERVERS["cua-driver"]
    missing = required - set(entry)
    assert not missing, f"cua-driver entry is missing fields: {sorted(missing)}"
    assert entry["id"] == "cua-driver"
    assert isinstance(entry["name"], str) and entry["name"]
    assert isinstance(entry["description"], str) and entry["description"]
    assert isinstance(entry["args"], list)
    assert isinstance(entry["env"], dict)
    assert isinstance(entry["category"], str) and entry["category"]


def test_launch_invocation_is_the_one_cua_driver_prints_for_itself():
    """`cua-driver mcp-config` emits

        {"mcpServers": {"cua-driver": {
            "command": ".../cua-driver", "args": ["mcp"]}}}

    The command is stored as the bare binary name (the absolute path
    that mcp-config prints is machine-specific and this catalog ships to
    every machine), but the subcommand must be exactly ``mcp``. Any
    other subcommand starts something that does not speak MCP on stdio.
    """
    entry = KNOWN_SERVERS["cua-driver"]
    assert entry["command"] == "cua-driver"
    assert entry["args"] == ["mcp"]


def test_it_needs_no_env_so_it_is_ready_the_moment_it_is_installed():
    """Unlike github/slack/postgres, there is no token to paste. If a
    required env var ever appears here, the Settings row goes from
    one-click to a form and that should be a deliberate change."""
    assert KNOWN_SERVERS["cua-driver"]["env"] == {}


def test_install_hint_is_the_real_installer_not_an_npm_package():
    """Every other entry's hint is `npm install -g ...`, which for a
    native binary would be a command that cannot work. The hint is the
    installer cua-driver's own binary embeds."""
    hint = KNOWN_SERVERS["cua-driver"]["install_hint"]
    assert "npm install" not in hint
    assert "https://cua.ai/driver/install.sh" in hint


# ── 2. The command resolves through the registry, not just in a dict ──


def test_present_binary_reports_installed(registry, monkeypatch):
    """The non-npx branch of `_resolve_install_state` has to answer for
    the shipped catalog entry, not only for test-registered stubs."""
    monkeypatch.setattr(
        "mcp.registry.shutil.which",
        lambda c: "/opt/bin/cua-driver" if c == "cua-driver" else None,
    )

    row = _row(registry.list_known(), "cua-driver")

    assert row["install_state"] == "installed"
    assert row["installed"] is True
    assert row["ready"] is True


def test_absent_binary_is_unavailable_with_an_actionable_detail(registry, monkeypatch):
    """It must NOT report `fetch_on_launch`: there is no npx that can
    download this on first connect, so promising a launch would be the
    same class of lie the npx install-state work was written to end."""
    monkeypatch.setattr("mcp.registry.shutil.which", lambda c: None)

    row = _row(registry.list_known(), "cua-driver")

    assert row["install_state"] == "unavailable"
    assert row["installed"] is False
    assert row["ready"] is False
    assert "cua-driver" in row["install_detail"]


def test_adding_it_did_not_make_the_npx_entries_lie(registry, monkeypatch):
    """Guard on the shared code path: a non-npx entry must not leak
    `installed` into rows whose package is genuinely absent."""
    monkeypatch.setattr(
        "mcp.registry.shutil.which",
        lambda c: {"npx": "/usr/bin/npx", "cua-driver": "/opt/bin/cua-driver"}.get(c),
    )
    monkeypatch.setattr(registry, "_npx_package_roots", lambda: [])

    rows = {r["id"]: r for r in registry.list_known()}

    assert rows["cua-driver"]["installed"] is True
    assert rows["github"]["installed"] is False
    assert rows["github"]["install_state"] == "fetch_on_launch"


def test_connect_builds_a_stdio_config_from_the_catalog_entry(registry):
    """`get_server_config` is what `connect_server` hands to
    `MCPServerConfig`; if it dropped the args the child would launch as
    a bare `cua-driver` and print help instead of speaking MCP."""
    cfg = registry.get_server_config("cua-driver")

    assert cfg["command"] == "cua-driver"
    assert cfg["args"] == ["mcp"]
    assert cfg.get("transport", "stdio") == "stdio"


# ── 3. Optional: nothing connects it ─────────────────────────────────


def test_nothing_in_the_boot_path_connects_known_servers():
    """The catalog must stay a catalog.

    ``MCPClientManager.load_and_connect`` connects what is written in
    ``~/.feral/mcp_servers.json``; ``KNOWN_SERVERS`` is only read to
    RENDER choices and to look a config up on an explicit
    ``connect_server(server_id)`` call. If some future startup hook
    iterates the catalog and connects it, every operator on the planet
    starts paying for cua-driver's daemon at boot.

    Asserted statically over the source rather than by booting, because
    "did not connect" is otherwise indistinguishable from "the test
    never reached that code".
    """
    offenders: list[str] = []
    for path in sorted(CORE_ROOT.rglob("*.py")):
        rel = path.relative_to(CORE_ROOT)
        if rel.parts[0] in ("build", "tests"):
            continue
        try:
            tree = ast.parse(path.read_text())
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.For, ast.AsyncFor)):
                continue
            iterated = ast.dump(node.iter)
            if "KNOWN_SERVERS" not in iterated:
                continue
            body = ast.dump(ast.Module(body=node.body, type_ignores=[]))
            if "connect_server" in body or "load_and_connect" in body:
                offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, (
        "KNOWN_SERVERS is iterated and connected at "
        f"{offenders}. Every catalog entry, cua-driver included, is "
        "opt-in and must only be connected on an explicit user action."
    )


def test_a_fresh_registry_reports_cua_driver_unconfigured_and_unconnected(registry):
    """The state an operator who has never heard of it must see."""
    row = _row(registry.list_known(), "cua-driver")

    assert row["configured"] is False
    assert row["connected"] is False


def test_user_config_file_is_untouched_by_merely_listing(registry, tmp_path):
    """Listing the catalog must not write anything: a side-effecting
    render would enable the server for a user who only opened a page."""
    config_path = tmp_path / "mcp_servers.json"
    registry.list_known()
    registry.auto_discover()
    assert not config_path.exists()
