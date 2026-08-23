"""Voice and text must not disagree about what the agent can do.

Reported 2026-08-22: "open <url> in chrome" spoken to the brain over
the iOS app did nothing, while the identical sentence typed worked. Not
a permissions problem: source "voice" resolves to the websocket
surface, where desktop_control__shell_command is allowed.

The cause was the OpenAI Realtime 128-tool cap. Measured on a real
registry, get_all_tools() returns 265 tools, so 137 are dropped from
every voice session, and straight truncation dropped ALL TEN
desktop_control tools. Four of the twelve pins were cutebot: a demo
robot outranked the operator's own computer.

The narrower bug is that desktop control was not pinned. The wider one
is that truncation is arbitrary with respect to capability: it left 17
skills with zero tools, including all 49 browser tools, all of calendar,
email, github, google_drive, notion, microsoft365 and health_data. A
skill with no tools is invisible, and an invisible capability is what
makes the same sentence work typed and fail spoken.
"""
from __future__ import annotations

import pytest

from agents.tool_list import (
    MIN_TOOLS_PER_SKILL,
    OPENAI_TOOL_HARD_LIMIT,
    PINNED_OPENAI_TOOL_NAMES,
    cap_tools_with_pins,
    skill_id_from_tool_name,
    tool_name_from_def,
    truncation_notice,
)


def _tool(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"tool {name}",
            "parameters": {"type": "object", "properties": {}},
        },
    }


@pytest.fixture(scope="module")
def live_tools():
    """The real registry, not a synthetic list.

    The whole finding was that the comment in tool_list.py said 176
    tools while the registry produced 265. A synthetic fixture would
    have kept saying 176.
    """
    from skills.registry import SkillRegistry

    registry = SkillRegistry()
    registry.load_builtin_skills()
    tools = registry.get_all_tools()
    if not tools:
        pytest.skip("no skills registered in this environment")
    return tools


def _names(tools) -> set[str]:
    return {tool_name_from_def(t) for t in tools}


def _skills(tools) -> set[str]:
    return {skill_id_from_tool_name(tool_name_from_def(t)) for t in tools}


# ── the reported bug ────────────────────────────────────────────────


class TestDesktopControlSurvivesTheCap:

    def test_the_live_list_really_does_exceed_the_limit(self, live_tools):
        """If it did not, the pins would be moot and the cause would be
        elsewhere. It does: this is what makes the cap load-bearing."""
        assert len(live_tools) > OPENAI_TOOL_HARD_LIMIT

    def test_the_spoken_sentence_has_a_tool_to_land_on(self, live_tools):
        """"open <url> in chrome" needs open_url, which exists as a
        distinct tool and was being dropped."""
        capped = _names(cap_tools_with_pins(live_tools))
        assert "desktop_control__open_url" in capped

    @pytest.mark.parametrize("tool", [
        "desktop_control__open_url",
        "desktop_control__shell_command",
        "desktop_control__open_app",
    ])
    def test_the_mac_is_drivable_by_voice(self, live_tools, tool):
        assert tool in _names(cap_tools_with_pins(live_tools))

    def test_open_url_outranks_open_app(self):
        """open_url shells `open -a <app> -- <url>` through
        LaunchServices and needs no macOS Automation grant; open_app
        sends an AppleScript that TCC gates per responsible process and
        which fails silently when the brain was not launched from a
        client holding the grant. The one that works unconditionally
        goes first."""
        pins = list(PINNED_OPENAI_TOOL_NAMES)
        assert pins.index("desktop_control__open_url") < pins.index(
            "desktop_control__open_app"
        )

    def test_the_robot_no_longer_outranks_the_computer(self):
        pins = list(PINNED_OPENAI_TOOL_NAMES)
        first_desktop = min(
            i for i, n in enumerate(pins) if n.startswith("desktop_control__")
        )
        first_robot = min(
            i for i, n in enumerate(pins) if n.startswith("cutebot__")
        )
        assert first_desktop < first_robot

    def test_a_moving_robot_can_still_be_stopped(self):
        """Pinning drive without halt would be worse than pinning
        neither: voice could start a physical machine and not stop it."""
        if "cutebot__drive" in PINNED_OPENAI_TOOL_NAMES:
            assert "cutebot__halt" in PINNED_OPENAI_TOOL_NAMES


# ── the wider bug ───────────────────────────────────────────────────


class TestNoCapabilityGoesInvisible:

    def test_every_skill_keeps_at_least_one_tool(self, live_tools):
        """The parity rule, stated as a test.

        Before: 17 skills had zero tools on voice. A skill with zero
        tools cannot be reached and cannot be reasoned about, so the
        agent reports the BRAIN as incapable of something it does every
        day in chat.
        """
        capped = cap_tools_with_pins(live_tools)
        lost = _skills(live_tools) - _skills(capped)
        assert lost == set(), f"these skills are invisible on voice: {sorted(lost)}"

    def test_coverage_holds_on_a_pathological_list(self):
        """One enormous skill must not starve forty small ones."""
        tools = [_tool(f"huge__endpoint_{i}") for i in range(300)]
        tools += [_tool(f"small_{i}__only") for i in range(40)]

        capped = cap_tools_with_pins(tools, max_tools=OPENAI_TOOL_HARD_LIMIT)
        assert len(capped) == OPENAI_TOOL_HARD_LIMIT
        for i in range(40):
            assert f"small_{i}__only" in _names(capped)

    def test_coverage_is_at_least_the_declared_minimum(self):
        tools = []
        for s in range(50):
            for e in range(10):
                tools.append(_tool(f"skill{s}__ep{e}"))
        capped = cap_tools_with_pins(tools, max_tools=OPENAI_TOOL_HARD_LIMIT)
        per_skill = {}
        for t in capped:
            sk = skill_id_from_tool_name(tool_name_from_def(t))
            per_skill[sk] = per_skill.get(sk, 0) + 1
        assert min(per_skill.values()) >= MIN_TOOLS_PER_SKILL
        assert len(per_skill) == 50

    def test_depth_is_still_spent_when_there_is_budget(self):
        """Breadth first does not mean breadth only."""
        tools = [_tool(f"a__ep{i}") for i in range(60)]
        tools += [_tool(f"b__ep{i}") for i in range(60)]
        capped = cap_tools_with_pins(tools, max_tools=OPENAI_TOOL_HARD_LIMIT)
        assert len(capped) == 120  # under the cap, nothing dropped
        deep = [t for t in capped if tool_name_from_def(t).startswith("a__")]
        assert len(deep) == 60

    def test_an_uncapped_list_is_returned_untouched(self):
        tools = [_tool(f"x__ep{i}") for i in range(10)]
        assert cap_tools_with_pins(tools) == tools


# ── the silence ─────────────────────────────────────────────────────


class TestTheModelIsToldItsListWasCut:
    """The complaint was not only that voice could not drive the Mac; it
    was that it failed "with no error explaining the difference"."""

    def test_a_notice_is_produced_when_tools_are_dropped(self, live_tools):
        capped = cap_tools_with_pins(live_tools)
        notice = truncation_notice(live_tools, capped)
        assert notice
        assert str(len(live_tools)) in notice
        assert str(len(capped)) in notice

    def test_it_tells_the_model_absence_is_not_incapability(self, live_tools):
        notice = truncation_notice(live_tools, cap_tools_with_pins(live_tools))
        assert "does NOT mean the brain cannot do it" in notice
        assert "chat" in notice

    def test_no_notice_when_nothing_was_dropped(self):
        tools = [_tool(f"x__ep{i}") for i in range(5)]
        assert truncation_notice(tools, tools) == ""

    def test_the_notice_reaches_the_realtime_session(self):
        """A notice nothing sends is the same as no notice."""
        from pathlib import Path

        src = (
            Path(__file__).resolve().parents[1] / "voice" / "realtime_proxy.py"
        ).read_text()
        assert "truncation_notice(" in src
        assert "instructions=instructions" in src
