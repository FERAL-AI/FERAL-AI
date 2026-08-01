"""
OpenAI tool-list helpers — pinning + hard-cap retention.

OpenAI chat/completions and Realtime both reject payloads with more than
128 tools. FERAL installs can expose 200+ skill endpoints; naive
``tools[:128]`` drops tail entries alphabetically and has repeatedly
evicted ``feral_routines__create`` on voice realtime sessions.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from agents.multimodal_blocks import tool_list_contains

logger = logging.getLogger("feral.tool_list")

OPENAI_TOOL_HARD_LIMIT = 128

# Always retained at the front of capped lists (order = priority).
#
# Voice builds its tool list from ``get_all_tools()`` (176 tools today) and
# caps it at 128, so 48 tools are dropped every session. The pins were all
# robot / routine flavoured, which meant not one coding tool survived the
# cap: voice could never read, edit, write, grep or run a shell command.
# The five ``coding_tools`` entries below close that gap.
PINNED_OPENAI_TOOL_NAMES: tuple[str, ...] = (
    "feral_routines__create",
    "feral_routines__list",
    "cutebot__drive",
    "cutebot__set_lights",
    "cutebot__halt",
    "cutebot__status",
    "notes_memory__fused_timeline",
    "coding_tools__read_file",
    "coding_tools__edit_file",
    "coding_tools__write_file",
    "coding_tools__grep_search",
    "coding_tools__bash",
)


def tool_name_from_def(tool: dict) -> str:
    """Extract the wire tool name from an OpenAI-shape or flat definition."""
    if not isinstance(tool, dict):
        return ""
    fn = tool.get("function")
    if isinstance(fn, dict):
        name = fn.get("name")
        if name:
            return str(name)
    name = tool.get("name")
    return str(name) if name else ""


def cap_tools_with_pins(
    tools: list[dict],
    *,
    max_tools: int = OPENAI_TOOL_HARD_LIMIT,
    pin_names: tuple[str, ...] = PINNED_OPENAI_TOOL_NAMES,
) -> list[dict]:
    """Return ``tools`` capped at ``max_tools``, keeping ``pin_names`` first.

    Pinned tools that are absent from ``tools`` are skipped silently.
    Non-pinned tools keep their relative order after the pinned block.
    """
    if max_tools <= 0:
        return []
    if not tools or len(tools) <= max_tools:
        return list(tools)

    pin_set = frozenset(pin_names)
    by_name: dict[str, dict] = {}
    for t in tools:
        name = tool_name_from_def(t)
        if name and name not in by_name:
            by_name[name] = t

    pinned: list[dict] = []
    pinned_seen: set[str] = set()
    for pin in pin_names:
        t = by_name.get(pin)
        if t is not None and pin not in pinned_seen:
            pinned.append(t)
            pinned_seen.add(pin)

    rest: list[dict] = []
    for t in tools:
        name = tool_name_from_def(t)
        if name in pin_set:
            continue
        rest.append(t)

    capped = pinned + rest
    if len(capped) > max_tools:
        dropped = len(capped) - max_tools
        logger.warning(
            "cap_tools_with_pins: truncating tools from %d → %d "
            "(dropped %d tail tools; %d pinned retained)",
            len(capped), max_tools, dropped, len(pinned),
        )
        capped = capped[:max_tools]
    return capped


def openai_realtime_tool_choice(force_tool: Optional[str]) -> Any:
    """Realtime GA ``tool_choice`` wire value for a forced function."""
    if not force_tool:
        return "auto"
    return {"type": "function", "name": force_tool}


def resolve_forced_tool_choice(
    tools: list[dict],
    force_tool: Optional[str],
    *,
    wire_fn=openai_realtime_tool_choice,
) -> Any:
    """Return ``wire_fn(force_tool)`` only when the tool is in ``tools``."""
    if force_tool and tool_list_contains(tools, force_tool):
        return wire_fn(force_tool)
    if force_tool:
        logger.warning(
            "resolve_forced_tool_choice: %r not in capped tool list — "
            "degrading to auto",
            force_tool,
        )
    return wire_fn(None)
