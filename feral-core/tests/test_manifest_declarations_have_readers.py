"""Three manifest declarations, and what actually reads them.

Measured on the shipped manifests before this change:

  * ``max_calls_per_hour``: declared in 39 of 41 manifests, with
    hand-tuned per-skill values (image_gen 30/hour, notion 600,
    github 5000, coding_tools 10000). Written in exactly two places in
    the source, ``models/skill_manifest.py`` (the field) and
    ``agents/tool_genesis.py`` (a value for generated skills). Read
    nowhere. A runaway agent loop could call image_gen ten thousand
    times an hour and nothing in the runtime would notice.

  * ``flows``: 9 declared across 6 manifests. One dispatcher exists,
    ``SkillRegistry._flow_to_taskflow_steps``, reachable only from a
    cron that names a ``flow_id``. One flow (health_data
    morning_check_in) has such a cron. Two are named by triggers, see
    below. The remaining six (browser read_a_page / act_on_a_page /
    fill_and_submit / debug_a_page, desktop_control lock_screen,
    web_actions compare_and_buy) are referenced by nothing at all, and
    nothing shows them to the model either, so a recipe for combining
    endpoints that somebody wrote down was unreachable from every
    direction.

  * ``triggers[].action_flow_id``: both shipped triggers declare one
    and ``ProactiveEngine._evaluate_manifest_triggers`` deliberately
    does not dispatch it. That is not an oversight, it is the fix for
    an incident where two auto-created routines each ran 4,766 times,
    one of them a Telegram send gated on a stress reading nothing
    checked. The evaluator notifies and stops.

Resolutions, and the reasons:

  * ``max_calls_per_hour`` is WIRED, at ``SkillExecutor.execute``,
    the same chokepoint the plan-mode and approval gates use.

  * ``flows`` are WIRED as data rather than as a new dispatcher: the
    cron path stays as it is, and ``self_introspection.describe_skill``
    now returns them, so the recipes reach the model that would follow
    them. A new automatic dispatcher is exactly the thing the incident
    above argues against.

  * ``action_flow_id`` is NOT wired, on purpose. What changes is that
    the declaration has to be truthful: it must name a flow that
    exists in the same manifest, which is checked here.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from models.skill_manifest import SkillEndpoint, SkillManifest, BrandProfile
from skills.executor import SkillExecutor

MANIFEST_DIR = Path(__file__).resolve().parents[1] / "skills" / "manifests"


def _manifests() -> list[tuple[str, dict]]:
    out = []
    for path in sorted(MANIFEST_DIR.glob("*.json")):
        with path.open() as fh:
            out.append((path.name, json.load(fh)))
    return out


def _skill(limit: int) -> SkillManifest:
    return SkillManifest(
        skill_id="ratelimited_skill",
        version="1.0.0",
        author="test",
        brand=BrandProfile(name="Rate Limited"),
        description="A skill with a declared hourly ceiling.",
        endpoints=[
            SkillEndpoint(
                id="ping",
                description="ping",
                method="GET",
                url="https://example.invalid/ping",
            ),
        ],
        max_calls_per_hour=limit,
    )


@pytest.mark.asyncio
async def test_max_calls_per_hour_is_enforced():
    skill = _skill(2)
    executor = SkillExecutor()
    executor._execute_inner = AsyncMock(
        return_value={"success": True, "status_code": 200, "data": {}, "error": None},
    )
    executor._record_audit = AsyncMock()

    results = [
        await executor.execute("ratelimited_skill__ping", {}, skill, skill.endpoints[0])
        for _ in range(3)
    ]

    assert [r["success"] for r in results] == [True, True, False], (
        "the declared ceiling of 2 calls/hour had no reader, so every call "
        f"through the chokepoint succeeded: {results}"
    )
    assert results[-1]["status_code"] == 429
    assert "2" in results[-1]["error"]
    assert executor._execute_inner.await_count == 2, (
        "a refused call must not reach the dispatcher"
    )


@pytest.mark.asyncio
async def test_a_zero_or_absent_limit_does_not_gate():
    """A manifest that declares no ceiling is unlimited, as before."""
    skill = _skill(0)
    executor = SkillExecutor()
    executor._execute_inner = AsyncMock(
        return_value={"success": True, "status_code": 200, "data": {}, "error": None},
    )
    executor._record_audit = AsyncMock()

    for _ in range(5):
        result = await executor.execute(
            "ratelimited_skill__ping", {}, skill, skill.endpoints[0],
        )
        assert result["success"] is True


@pytest.mark.asyncio
async def test_the_window_rolls():
    """The ceiling is per hour, not for the lifetime of the process."""
    skill = _skill(1)
    executor = SkillExecutor()
    executor._execute_inner = AsyncMock(
        return_value={"success": True, "status_code": 200, "data": {}, "error": None},
    )
    executor._record_audit = AsyncMock()

    first = await executor.execute("x__ping", {}, skill, skill.endpoints[0])
    assert first["success"] is True
    blocked = await executor.execute("x__ping", {}, skill, skill.endpoints[0])
    assert blocked["status_code"] == 429

    # Age every recorded call past the window.
    window = executor._skill_call_times["ratelimited_skill"]
    executor._skill_call_times["ratelimited_skill"] = type(window)(
        t - 3601 for t in window
    )

    after = await executor.execute("x__ping", {}, skill, skill.endpoints[0])
    assert after["success"] is True


@pytest.mark.asyncio
async def test_an_undeclared_limit_is_not_the_pydantic_default():
    """``SkillManifest`` defaults the field to 1000 and two shipped
    manifests (cutebot, robot_action) declare nothing. Enforcing the
    default would invent a cap of 1000 motor commands an hour for the
    two authors who never asked for one."""
    skill = SkillManifest(
        skill_id="robot_action",
        version="1.0.0",
        author="test",
        brand=BrandProfile(name="Robot"),
        description="drives a robot",
        endpoints=[
            SkillEndpoint(id="move", description="move", method="POST", url="x"),
        ],
    )
    assert "max_calls_per_hour" not in skill.model_fields_set
    assert skill.max_calls_per_hour == 1000

    executor = SkillExecutor()
    executor._execute_inner = AsyncMock(
        return_value={"success": True, "status_code": 200, "data": {}, "error": None},
    )
    executor._record_audit = AsyncMock()
    assert executor._rate_limit("robot_action__move", skill) is None
    assert executor._skill_call_times == {}, (
        "an undeclared limit must not even start counting"
    )


@pytest.mark.asyncio
async def test_limits_are_per_skill():
    busy = _skill(1)
    other = _skill(1)
    other.skill_id = "other_skill"
    executor = SkillExecutor()
    executor._execute_inner = AsyncMock(
        return_value={"success": True, "status_code": 200, "data": {}, "error": None},
    )
    executor._record_audit = AsyncMock()

    assert (await executor.execute("a", {}, busy, busy.endpoints[0]))["success"]
    assert (await executor.execute("a", {}, busy, busy.endpoints[0]))["status_code"] == 429
    assert (await executor.execute("b", {}, other, other.endpoints[0]))["success"], (
        "one skill exhausting its budget must not gate a different skill"
    )


def test_every_declared_flow_names_real_endpoints():
    """A recipe that names an endpoint the skill does not have is a lie
    whether or not anything runs it."""
    broken = []
    for name, data in _manifests():
        endpoint_ids = {e.get("id") for e in (data.get("endpoints") or [])}
        for flow in (data.get("flows") or []):
            for step in (flow.get("steps") or []):
                ep = step.get("endpoint_id")
                if ep and ep not in endpoint_ids:
                    broken.append(f"{name}:{flow.get('id')} -> {ep}")
    assert broken == [], f"flow steps naming endpoints that do not exist: {broken}"


def test_every_trigger_action_flow_id_resolves():
    """``action_flow_id`` is deliberately not dispatched (see the module
    docstring), which makes it the easiest field in the schema to get
    wrong without anyone noticing. It still has to name a real flow."""
    broken = []
    for name, data in _manifests():
        flow_ids = {f.get("id") for f in (data.get("flows") or [])}
        for trig in (data.get("triggers") or []):
            target = trig.get("action_flow_id")
            if target and target not in flow_ids:
                broken.append(f"{name}:{trig.get('id')} -> {target}")
    assert broken == [], f"triggers naming flows that do not exist: {broken}"


def test_describe_skill_returns_the_flows_and_the_ceiling():
    """The recipes and the ceiling reach the one surface that can use
    them: the model asking what a skill can do."""
    from skills.impl.self_introspection import SelfIntrospectionSkill

    skill = SkillManifest(
        skill_id="browser",
        version="1.0.0",
        author="test",
        brand=BrandProfile(name="Browser"),
        description="drives a browser",
        endpoints=[
            SkillEndpoint(id="navigate", description="go", method="GET", url="x"),
            SkillEndpoint(id="get_page_text", description="read", method="GET", url="x"),
        ],
        flows=[{
            "id": "read_a_page",
            "description": "Open a URL and read what it says",
            "steps": [{"endpoint_id": "navigate"}, {"endpoint_id": "get_page_text"}],
        }],
        max_calls_per_hour=600,
    )
    state = MagicMock()
    state.skill_registry = MagicMock()
    state.skill_registry.skills = {"browser": skill}

    out = SelfIntrospectionSkill()._describe_skill(state, {"skill_id": "browser"})

    assert out["success"] is True
    assert out["data"]["max_calls_per_hour"] == 600
    flows = out["data"]["flows"]
    assert [f["id"] for f in flows] == ["read_a_page"]
    assert flows[0]["steps"] == ["navigate", "get_page_text"]
    assert "read what it says" in flows[0]["description"]
