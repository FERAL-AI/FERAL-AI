"""Text chat robot lights routing — cutebot not smart_home_hue (v2026.6.28).

Regression for live failure: "make robot lights red/off" routed to
smart_home_hue.get_entities (DNS fail) while voice correctly used
cutebot__set_lights. Root causes: Hue manifest owns generic "lights"
triggers, multi-agent home worker keyword-matched "light", and the
text path lacked _force_tool_for_query for cutebot__set_lights.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.multi_agent import AgentRouter
from agents.orchestrator import Orchestrator
from agents.refusal_handler import RefusalHandler
from models.skill_manifest import (
    BrandProfile,
    SkillEndpoint,
    SkillManifest,
)


def _skill(skill_id: str, triggers: list[str], categories: list[str] | None = None) -> SkillManifest:
    return SkillManifest(
        skill_id=skill_id,
        version="1.0.0",
        author="test",
        brand=BrandProfile(name=skill_id, primary_color="#000", logo_url="", icon_set="sf_symbols"),
        description=f"{skill_id} skill",
        categories=categories or [],
        trigger_phrases=triggers,
        endpoints=[
            SkillEndpoint(
                id="default",
                method="POST",
                url=f"https://example.test/{skill_id}",
                description="default endpoint",
                returns_description="result",
                ui_hint="detail_card",
            ),
            SkillEndpoint(
                id="set_lights",
                method="POST",
                url=f"https://example.test/{skill_id}/set_lights",
                description="set lights",
                returns_description="result",
                ui_hint="detail_card",
            ),
        ],
    )


CATALOG = {
    "cutebot": _skill(
        "cutebot",
        ["robot lights", "cutebot lights", "robot status", "cutebot"],
        ["hardware", "robotics"],
    ),
    "smart_home_hue": _skill(
        "smart_home_hue",
        ["turn on the lights", "turn off the lights", "dim the lights", "set lights to red"],
        ["smart_home"],
    ),
    "notes_memory": _skill("notes_memory", ["remember this"], ["memory"]),
}


def _orch_stub() -> Orchestrator:
    reg = MagicMock()
    reg.skills = CATALOG

    def _find(query: str, top_k: int = 5):
        scored = []
        for sk in CATALOG.values():
            s = Orchestrator._trigger_score(query, sk)
            if s > 0:
                scored.append((s, sk))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [sk for _, sk in scored[:top_k]]

    reg.find_skills_for_query = _find
    reg.get_tools_for_skills = MagicMock(return_value=[])

    orch = Orchestrator(
        skill_registry=reg,
        send_to_client=AsyncMock(),
        daemons={},
    )
    orch.llm = MagicMock()
    orch.llm.available = True
    orch.llm.chat_with_failover = AsyncMock(side_effect=AssertionError("LLM must not be called"))
    orch.conversation_history = {}
    return orch


def _orch_for_force_tool() -> Orchestrator:
    orch = Orchestrator.__new__(Orchestrator)
    orch.refusal_handler = RefusalHandler(MagicMock())
    orch.conversation_history = {}
    # note_voice_user_turn now appends under the per-session lock.
    orch._session_locks = {}
    orch._conversation_max_per_session = 200
    return orch


def _tool(name: str) -> dict:
    return {"type": "function", "function": {"name": name}}


@pytest.mark.parametrize(
    "prompt",
    [
        "make robot lights red",
        "turn off robot lights",
        "set cutebot lights to green",
        "make the cutebot lights go off",
    ],
)
@pytest.mark.asyncio
async def test_route_prompt_robot_lights_prefers_cutebot(prompt: str):
    orch = _orch_stub()
    result = await orch._route_prompt(prompt)
    assert result, f"no skills for {prompt!r}"
    assert result[0].skill_id == "cutebot", (
        f"expected cutebot first for {prompt!r}, got {[s.skill_id for s in result]}"
    )


@pytest.mark.asyncio
async def test_route_prompt_generic_lights_still_hue():
    """Unscoped room lights must NOT hijack to cutebot."""
    orch = _orch_stub()
    result = await orch._route_prompt("turn on the lights please")
    assert result[0].skill_id == "smart_home_hue"


@pytest.mark.asyncio
async def test_route_prompt_robot_context_coref_lights():
    """Ambiguous lights command after cutebot subject routes to cutebot."""
    orch = _orch_stub()
    sid = "sess-robot-lights"
    await orch._route_prompt("check the cutebot", session_id=sid)
    result = await orch._route_prompt("make the lights red", session_id=sid)
    assert result[0].skill_id == "cutebot"


def test_force_tool_for_query_robot_lights():
    tools = [_tool("cutebot__set_lights"), _tool("smart_home_hue__get_entities")]
    out = _orch_for_force_tool()._force_tool_for_query(
        "make robot lights red", tools,
    )
    assert out == "cutebot__set_lights"


def test_force_tool_for_query_robot_lights_absent_tool():
    tools = [_tool("smart_home_hue__get_entities")]
    assert _orch_for_force_tool()._force_tool_for_query(
        "make robot lights red", tools,
    ) is None


@pytest.mark.asyncio
async def test_voice_note_user_turn_forces_robot_lights():
    orch = _orch_for_force_tool()
    tools = [_tool("cutebot__set_lights"), _tool("smart_home_hue__set_light")]
    out = await orch.note_voice_user_turn(
        "sess-voice-lights",
        "make robot lights red",
        tools=tools,
    )
    assert out["forced_tool"] == "cutebot__set_lights"


@pytest.mark.asyncio
async def test_multi_agent_router_robot_lights_to_general():
    router = AgentRouter(llm=None)
    route = await router.route("make robot lights red")
    assert route["workers"] == ["general"]
    assert route["strategy"] == "single"


@pytest.mark.asyncio
async def test_multi_agent_router_room_lights_still_home():
    router = AgentRouter(llm=None)
    route = await router.route("turn on the living room lights")
    assert route["workers"] == ["home"]
