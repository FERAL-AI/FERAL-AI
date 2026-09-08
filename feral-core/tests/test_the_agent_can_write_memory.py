"""The agent must be able to record a memory, whatever else it is given.

`cap_tools_with_pins` trims the tool list to the provider's hard limit.
Pinned tools survive; the rest compete, with a coverage pass that
guarantees every skill at least one tool. That pass is why this went
unnoticed: `notes_memory` was represented by its recall tools, which are
pinned, so the skill looked present while its write verb had been cut.

Measured on a live brain on 2026-09-07. Asked to "remember this and
share it with my work scope", the agent called `list_capabilities`, then
`describe_skill(notes_memory)`, then `describe_skill(notion)`, never
called `save_note` because it was not among the 128 tools it had been
handed, and replied:

    Done, I saved that the roadmap review moved to Thursday at 3 PM and
    shared it to your work scope for your teammate's brain.

Nothing was written. Zero notes matched, and the sync WAL was unchanged.

Two failures stacked. The agent could not reach the tool, which is
ours. The agent then reported success it had not achieved, which is a
model behaviour this file cannot fix. Only the first is tested here,
and removing it removes the setup for the second.

The set that survives varies per turn, because withheld skills change
the size of the pool being trimmed, so this cannot be left to luck: a
turn where "remember this" works and the next where it silently does
not is worse than a consistent failure.
"""

from __future__ import annotations

import pytest

from agents.tool_list import PINNED_OPENAI_TOOL_NAMES, cap_tools_with_pins


def _tool(name: str) -> dict:
    return {"type": "function", "function": {"name": name, "parameters": {}}}


def _names(tools) -> set[str]:
    return {(t.get("function") or {}).get("name") for t in tools}


#: What a person says, and the tool that has to exist for it to happen.
CORE_VERBS = [
    ("remember this", "notes_memory__save_note"),
    ("what do you know about X", "notes_memory__search_notes"),
]


@pytest.mark.parametrize("spoken,tool", CORE_VERBS)
def test_the_core_memory_verbs_are_pinned(spoken, tool):
    assert tool in PINNED_OPENAI_TOOL_NAMES, (
        f"'{spoken}' depends on {tool}, which is unpinned and therefore "
        "competes for a slot it can lose"
    )


@pytest.mark.parametrize("spoken,tool", CORE_VERBS)
def test_they_survive_a_pool_far_over_the_limit(spoken, tool):
    """The live turn trimmed 206 tools to 128. Try worse than that."""
    pool = [_tool(f"filler_skill__endpoint_{i}") for i in range(400)]
    pool.append(_tool(tool))
    kept = _names(cap_tools_with_pins(pool, max_tools=128))
    assert tool in kept, f"'{spoken}' lost its tool to the cap"


def test_writing_and_reading_survive_together():
    """Writing what can never be read back is not memory."""
    pool = [_tool(f"filler__e{i}") for i in range(400)]
    pool += [_tool("notes_memory__save_note"), _tool("notes_memory__search_notes")]
    kept = _names(cap_tools_with_pins(pool, max_tools=128))
    assert {"notes_memory__save_note", "notes_memory__search_notes"} <= kept


def test_a_pool_under_the_limit_is_untouched():
    """The cap must not reorder or drop when there is nothing to trim."""
    pool = [_tool("notes_memory__save_note"), _tool("a__b")]
    assert cap_tools_with_pins(pool, max_tools=128) == pool


def test_pinning_did_not_starve_skill_coverage():
    """Pins must not eat the pass that keeps every skill visible.

    Adding pins takes slots from the coverage pass. If a pin ever costs
    a whole skill its only tool, that skill becomes invisible to the
    model, which is the failure this cap exists to avoid.
    """
    skills = [f"skill{i}" for i in range(42)]
    pool = [_tool(f"{s}__endpoint_{j}") for s in skills for j in range(6)]
    pool += [_tool(n) for n in PINNED_OPENAI_TOOL_NAMES]
    kept = _names(cap_tools_with_pins(pool, max_tools=128))
    represented = {n.split("__", 1)[0] for n in kept if n}
    missing = [s for s in skills if s not in represented]
    assert not missing, f"skills lost every tool to the pins: {missing}"
