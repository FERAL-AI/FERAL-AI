"""
OpenAI tool-list helpers: pinning + hard-cap retention.

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
# Measured 2026-08-22 on a real registry: ``get_all_tools()`` returns
# **265** tools, not the 176 this comment used to claim, so the cap drops
# 137 of them on every voice session.
#
# The reported symptom: "open <url> in chrome" spoken to the brain did
# nothing, while the identical sentence typed worked. Not a permissions
# problem. All ten ``desktop_control__*`` tools were being dropped by
# this cap, so on voice the model was never shown that driving the Mac
# was possible at all. Four of the twelve pins were ``cutebot__*``: a
# demo robot outranked the operator's own computer.
#
# ``open_url`` is pinned FIRST among them, ahead of ``open_app``,
# because it is also the path that works regardless of macOS
# Automation permission: it shells ``open -a <app> -- <url>`` through
# LaunchServices, whereas ``open_app`` sends an AppleScript that TCC
# gates per responsible process and fails silently when the brain was
# not launched from a client holding the grant.
#
# cutebot keeps ONE pin rather than four. It is not removed: verified on
# the live registry that its tools land early enough in registration
# order to survive the cap unpinned, so this costs the robot nothing and
# buys the guarantee for tools that were being erased outright.
PINNED_OPENAI_TOOL_NAMES: tuple[str, ...] = (
    # Driving the operator's machine. The product.
    "desktop_control__open_url",
    "desktop_control__shell_command",
    "desktop_control__open_app",
    "desktop_control__screenshot",
    # Reading and writing their files.
    "coding_tools__read_file",
    "coding_tools__edit_file",
    "coding_tools__write_file",
    "coding_tools__grep_search",
    "coding_tools__bash",
    # Recall. `list_conversations` answers "did you hear that", which
    # the agent used to answer wrongly from its self-model.
    "notes_memory__fused_timeline",
    "notes_memory__list_conversations",
    "notes_memory__conversation_commitments",
    # Automation the operator set up.
    "feral_routines__create",
    "feral_routines__list",
    # The robot, kept reachable but no longer ahead of the Mac.
    #
    # `halt` is pinned WITH `drive` and not as an afterthought. This
    # drives a physically moving machine, so a session that can start it
    # and cannot stop it is worse than one that can do neither. If drive
    # is ever unpinned, unpin halt in the same edit.
    "cutebot__drive",
    "cutebot__halt",
    # Things people actually say out loud.
    #
    # The coverage pass below guarantees every skill at least one tool,
    # which is enough to keep a capability visible and not enough to
    # operate it. These are the endpoints whose skills lost depth to
    # that pass and whose whole point is being spoken to: measured,
    # spotify fell from 10 tools to 1, which leaves voice able to see
    # that music exists and unable to pause it.
    "spotify_music__play_pause",
    "spotify_music__next_track",
    "spotify_music__search",
    "spotify_music__now_playing",
    "feral_reminders__create",
    "feral_reminders__list",
)

#: Guarantee every registered skill at least this many tools under the
#: cap, before any remaining budget is spent on depth.
#:
#: Pins alone fix the skills somebody thought to list. They do nothing
#: for the rest, and the rest is where the silence was: measured on the
#: live registry, the old cap left **19 skills with zero tools** on
#: voice, including every one of the 49 ``browser`` tools, all of
#: calendar, email, github, google_drive, notion, microsoft365 and
#: health_data, while cutebot kept 6 of 6 and spotify 10 of 10.
#:
#: That is what makes voice and text disagree about what the agent CAN
#: do, and it disagrees silently: the model is not told its list was
#: truncated, so it answers from a smaller world without knowing the
#: world is smaller. One tool per skill is enough for the model to know
#: a capability exists and to say so.
MIN_TOOLS_PER_SKILL = 1


def skill_id_from_tool_name(name: str) -> str:
    """The owning skill for a wire tool name (``skill__endpoint``)."""
    if not name:
        return ""
    return name.split("__", 1)[0]


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

    if len(pinned) + len(rest) <= max_tools:
        return pinned + rest

    # Breadth before depth.
    #
    # Straight truncation kept the head of the list and erased the tail,
    # and because the list is in registration order that is an arbitrary
    # alphabet of luck: measured, it left 19 skills with NO tools at all
    # while others kept every one they had. A skill with zero tools is
    # invisible, and an invisible capability is what makes the same
    # sentence work typed and fail spoken.
    #
    # So spend the budget in two passes. First give every skill its
    # first MIN_TOOLS_PER_SKILL tools, in the order the registry
    # produced them, so nothing disappears entirely. Then spend whatever
    # is left on depth, again in registry order, so the richer skills
    # get their remaining endpoints back.
    covered_skills: set[str] = {
        skill_id_from_tool_name(tool_name_from_def(t)) for t in pinned
    }
    budget = max_tools - len(pinned)
    first_pass: list[dict] = []
    deferred: list[dict] = []
    per_skill: dict[str, int] = {}
    for t in rest:
        skill = skill_id_from_tool_name(tool_name_from_def(t))
        taken = per_skill.get(skill, 0)
        # A skill already represented by a pin does not need its
        # coverage quota spent again.
        quota = 0 if skill in covered_skills else MIN_TOOLS_PER_SKILL
        if taken < quota and len(first_pass) < budget:
            first_pass.append(t)
            per_skill[skill] = taken + 1
        else:
            deferred.append(t)

    remaining = budget - len(first_pass)
    capped = pinned + first_pass + deferred[:max(0, remaining)]

    kept_skills = {
        skill_id_from_tool_name(tool_name_from_def(t)) for t in capped
    }
    all_skills = {
        skill_id_from_tool_name(tool_name_from_def(t)) for t in tools
    }
    lost_skills = sorted(all_skills - kept_skills)
    logger.warning(
        "cap_tools_with_pins: %d tools over the %d limit; kept %d "
        "(%d pinned, %d for skill coverage). Skills represented: %d of %d.%s",
        len(tools), max_tools, len(capped), len(pinned), len(first_pass),
        len(kept_skills), len(all_skills),
        f" NO tools survived for: {', '.join(lost_skills)}." if lost_skills else "",
    )
    return capped


def truncation_notice(tools: list[dict], capped: list[dict]) -> str:
    """One line telling the model its tool list was cut, or "".

    The reported complaint was not only that voice could not drive the
    Mac; it was that the same sentence worked typed and failed spoken
    "with no error explaining the difference". The cap is a property of
    the OpenAI Realtime session, not of the brain, and nothing told the
    model it was operating on a smaller list than the text path, so it
    answered from a smaller world without knowing the world was smaller.

    Appended to the voice system prompt. It does not restore a dropped
    tool; it makes the model's ignorance of one legible, so an honest
    "I can't reach that from voice, ask me in chat" replaces a confident
    "I can't do that".
    """
    if not tools or len(capped) >= len(tools):
        return ""
    dropped = len(tools) - len(capped)
    return (
        "\n\n## Voice Tool Limit\n"
        f"This voice session can carry {len(capped)} of the "
        f"{len(tools)} tools this brain actually has: the realtime API "
        f"caps the list, so {dropped} are not visible to you right now. "
        "Every skill is still represented, but some have only their "
        "main action available here.\n"
        "This means a tool being absent from your list does NOT mean the "
        "brain cannot do it. If the operator asks for something you "
        "cannot see a tool for, do not say the system is incapable of "
        "it. Say you cannot reach it from the voice session and offer to "
        "do it in chat, where the full set is available."
    )


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
            "resolve_forced_tool_choice: %r not in capped tool list, "
            "degrading to auto",
            force_tool,
        )
    return wire_fn(None)
