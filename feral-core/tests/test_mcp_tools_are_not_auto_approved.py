"""A tool name is not evidence about what the tool does.

Two defects that only become reachable when a large MCP server is
connected. Neither is about any particular server; both are in how
FERAL treats `mcp_*` names in general.

1. AUTO BY SUBSTRING. No `mcp_*` name is in `TOOL_DANGER_MAP`, so
   `get_danger_level` returns SAFE, which means "the table says
   nothing". `resolve_policy` then falls through to a substring
   heuristic whose unknown default is CONFIRM, correctly fail-closed.
   But before that default it awards AUTO to any name containing
   read/get/list/status/current/query/search. So a hypothetical
   `clipboard_read` auto-executes with no approval in every autonomy
   mode, including strict, and reads whatever the operator last copied:
   a password, a token, a recovery phrase.

   For a native skill the substrings are backed by a manifest that was
   reviewed. For an MCP tool there is no manifest, no review, and the
   name is chosen by a third party. `agents/plan_mode.py` already draws
   exactly this line, blocking `mcp_*` by name with the comment "Fails
   closed on everything that has no FERAL manifest behind it." The
   safety resolver should agree with plan mode rather than contradict
   it.

2. TOOL-CAP EVICTION. `skill_id_from_tool_name` splits on `__`. MCP
   names use single underscores, so every MCP tool becomes its own
   "skill" and claims a per-skill coverage slot in the breadth-first
   pass, ahead of native depth. Connecting one 56-tool server evicts 56
   native endpoints from a 128-tool budget. Breadth survives; depth
   does not.

Both are pre-existing. They are being fixed before a large server is
registered rather than after.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.tool_list import skill_id_from_tool_name  # noqa: E402
from security.safety_resolver import is_read_only, resolve_policy  # noqa: E402


def _level(tool_name: str) -> str:
    policy = resolve_policy(tool_name, {})
    return str(getattr(policy, "level", policy)).lower()


# ── 1. no MCP tool is auto-approved on the strength of its name ────

@pytest.mark.parametrize("tool_name", [
    "mcp_cua_clipboard_read",      # reads the clipboard: passwords, tokens
    "mcp_cua_get_window_state",
    "mcp_cua_list_windows",
    "mcp_cua_get_desktop_state",   # a screenshot of everything on screen
    "mcp_anything_search_files",
    "mcp_anything_query_database",
    "mcp_anything_read_secrets",
])
def test_a_read_shaped_mcp_name_is_not_auto_approved(tool_name):
    assert _level(tool_name) != "auto", (
        f"{tool_name} auto-executes with no approval purely because its "
        "name contains a read-ish word. There is no manifest behind an MCP "
        "tool and the name is chosen by a third party."
    )


@pytest.mark.parametrize("tool_name", [
    "mcp_cua_clipboard_read",
    "mcp_cua_get_window_state",
    "mcp_anything_get_anything",
])
def test_a_read_shaped_mcp_name_is_not_treated_as_read_only(tool_name):
    """Strict mode short-circuits approval on `read_only`.

    `ToolRunner` skips the approval prompt in strict mode when
    `is_read_only` says yes, so a lenient answer here defeats the
    strictest setting the operator can choose.
    """
    assert is_read_only(tool_name) is False, (
        f"{tool_name} is treated as read-only, so strict mode skips its "
        "approval prompt"
    )


def test_dangerous_mcp_names_were_already_correct_and_stay_correct():
    """The unknown default is CONFIRM and that part was never broken."""
    for tool_name in ("mcp_cua_kill_app", "mcp_cua_type_text",
                      "mcp_cua_launch_app", "mcp_cua_browser_navigate"):
        assert _level(tool_name) == "confirm", tool_name
        assert is_read_only(tool_name) is False, tool_name


def test_the_heuristic_still_serves_the_names_it_was_written_for():
    """Only `mcp_` names lose the auto shortcut.

    Tested against `_legacy_substring_level` directly, because that is
    where the auto branch lives. Going through `resolve_policy` would
    prove nothing here: with no skill registry loaded every native name
    already resolves to CONFIRM for an unrelated reason, so it would
    pass whether or not the change were correct.

    Native tools carry a reviewed manifest, so a read-shaped name there
    means something. Turning every native read into a prompt would be a
    real regression.
    """
    from security.safety_resolver import _legacy_substring_level

    level, source = _legacy_substring_level("some_skill__get_status", {})
    assert level == "auto", (
        f"a native read-shaped name lost its auto shortcut ({level}, {source})"
    )

    level, _ = _legacy_substring_level("mcp_cua_get_status", {})
    assert level != "auto", "the mcp guard did not fire in the heuristic"


# ── 2. one MCP server claims one coverage slot, not fifty-six ──────

def test_mcp_tools_from_one_server_share_a_bucket():
    """Otherwise each tool claims a per-skill slot in the tool cap."""
    a = skill_id_from_tool_name("mcp_cua_kill_app")
    b = skill_id_from_tool_name("mcp_cua_clipboard_read")
    c = skill_id_from_tool_name("mcp_cua_get_window_state")
    assert a == b == c, (
        f"tools from one MCP server landed in different buckets: {a}, {b}, {c}. "
        "Each then claims its own coverage slot and evicts a native endpoint."
    )


def test_different_mcp_servers_stay_in_different_buckets():
    """Bucketing must not merge unrelated servers into one slot."""
    assert skill_id_from_tool_name("mcp_cua_click") != skill_id_from_tool_name(
        "mcp_github_create_issue"
    )


def test_native_bucketing_is_unchanged():
    assert skill_id_from_tool_name("macos_ax__click") == "macos_ax"
    assert skill_id_from_tool_name("gui_computer_use__type_text") == "gui_computer_use"


def test_a_large_mcp_server_does_not_evict_native_depth():
    """The measured regression: 56 MCP tools cost 56 native endpoints."""
    from agents.tool_list import cap_tools_with_pins

    native = [
        {"function": {"name": f"skill{s}__ep{e}"}}
        for s in range(40) for e in range(4)
    ]
    mcp = [{"function": {"name": f"mcp_cua_tool{i}"}} for i in range(56)]

    kept_without = cap_tools_with_pins(list(native), max_tools=128)
    kept_with = cap_tools_with_pins(list(native) + mcp, max_tools=128)

    def _native_count(tools):
        return sum(
            1 for t in tools
            if not (t.get("function", {}).get("name", "")).startswith("mcp_")
        )

    lost = _native_count(kept_without) - _native_count(kept_with)
    assert lost <= 10, (
        f"connecting one MCP server evicted {lost} native endpoints "
        f"({_native_count(kept_without)} -> {_native_count(kept_with)})"
    )
