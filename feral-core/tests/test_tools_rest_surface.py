"""L6 — canonical external REST tool surface (/api/tools, /api/tools/execute)."""
from __future__ import annotations

import pytest

from models.skill_manifest import BrandProfile, SkillEndpoint, SkillManifest
from skills.base import BaseSkill
from skills.registry import SkillRegistry
from skills.executor import SkillExecutor
from skills.impl import SKILL_IMPLEMENTATIONS

import api.routes.tools as tools


# The auto-tier fixture below has to use a FIRST-PARTY skill id. audit P0.1:
# ``safety_tier: "safe"`` is only honoured from a manifest that ships in this
# repo, so a fake id declaring itself safe now resolves to CONFIRM and the
# route answers 412 — which is the fix working, not a REST-surface defect.
# The deny/confirm fixtures keep their arbitrary ids because escalation is
# honoured from any author. The clamp itself is covered by
# ``tests/test_p0_security_clamps_and_gates.py``.
_SAFE_SKILL_ID = "notes_memory"


class _RecordingSkill(BaseSkill):
    def __init__(self, skill_id):
        super().__init__(skill_id=skill_id)
        self.calls = []

    async def execute(self, endpoint_id, args, vault):
        self.calls.append((endpoint_id, args))
        return {"success": True, "status_code": 200, "data": {"echo": args}, "error": None}


def _manifest(skill_id, endpoint_id, safety_tier):
    return SkillManifest(
        skill_id=skill_id,
        brand=BrandProfile(name=skill_id, primary_color="#111"),
        description=f"test {skill_id}",
        endpoints=[
            SkillEndpoint(
                id=endpoint_id,
                method="PYTHON",
                url=f"python://{skill_id}/{endpoint_id}",
                description="ep",
                safety_tier=safety_tier,
            )
        ],
    )


@pytest.fixture
def env(monkeypatch):
    reg = SkillRegistry()
    reg.load_builtin_skills()
    safe = _RecordingSkill(_SAFE_SKILL_ID)
    danger = _RecordingSkill("rest_danger")
    confirm = _RecordingSkill("rest_confirm")
    reg.register(_manifest(_SAFE_SKILL_ID, "ping", "safe"))
    reg.register(_manifest("rest_danger", "wipe", "deny"))
    reg.register(_manifest("rest_confirm", "act", "confirm"))
    # CI-flake fix: monkeypatch the global skill registry so the
    # fakes are restored at teardown automatically — no leak into
    # ``test_manifest_dispatch_contract`` or any other suite ordered
    # after this one.
    monkeypatch.setitem(SKILL_IMPLEMENTATIONS, _SAFE_SKILL_ID, safe)
    monkeypatch.setitem(SKILL_IMPLEMENTATIONS, "rest_danger", danger)
    monkeypatch.setitem(SKILL_IMPLEMENTATIONS, "rest_confirm", confirm)

    saved_reg = getattr(tools.state, "skill_registry", None)
    saved_orch = getattr(tools.state, "orchestrator", None)
    tools.state.skill_registry = reg

    class _OrchShim:
        executor = SkillExecutor(daemons={})

    tools.state.orchestrator = _OrchShim()  # skill_executor property reads orchestrator.executor

    yield {"reg": reg, "safe": safe, "danger": danger, "confirm": confirm}

    tools.state.skill_registry = saved_reg
    tools.state.orchestrator = saved_orch


@pytest.mark.asyncio
async def test_list_tools_enumerates(env):
    res = await tools.list_tools()
    assert res["count"] > 0
    names = {t["function"]["name"] for t in res["tools"]}
    assert "feral_routines__create" in names
    assert f"{_SAFE_SKILL_ID}__ping" in names


@pytest.mark.asyncio
async def test_list_tools_filter_by_skill(env):
    res = await tools.list_tools(skill_id=_SAFE_SKILL_ID)
    names = {t["function"]["name"] for t in res["tools"]}
    assert names == {f"{_SAFE_SKILL_ID}__ping"}


@pytest.mark.asyncio
async def test_execute_via_tool_name(env):
    res = await tools.execute_tool({"tool_name": f"{_SAFE_SKILL_ID}__ping", "args": {"x": 1}})
    assert res["success"] is True
    assert res["tool_name"] == f"{_SAFE_SKILL_ID}__ping"
    assert env["safe"].calls == [("ping", {"x": 1})]


@pytest.mark.asyncio
async def test_execute_via_skill_and_endpoint(env):
    res = await tools.execute_tool({"skill_id": _SAFE_SKILL_ID, "endpoint": "ping", "args": {"y": 2}})
    assert res["success"] is True
    assert env["safe"].calls[-1] == ("ping", {"y": 2})


@pytest.mark.asyncio
async def test_execute_unknown_skill(env):
    res = await tools.execute_tool({"tool_name": "nope__x"})
    assert res["success"] is False
    assert res["status_code"] == 404


@pytest.mark.asyncio
async def test_execute_deny_returns_403(env):
    res = await tools.execute_tool({"tool_name": "rest_danger__wipe"})
    assert res["success"] is False
    assert res["status_code"] == 403
    assert env["danger"].calls == []


@pytest.mark.asyncio
async def test_execute_confirm_requires_flag(env):
    res = await tools.execute_tool({"tool_name": "rest_confirm__act"})
    assert res["success"] is False
    assert res["status_code"] == 412
    assert env["confirm"].calls == []

    ok = await tools.execute_tool({"tool_name": "rest_confirm__act", "confirm": True}) 
    assert ok["success"] is True
    assert env["confirm"].calls == [("act", {})]


@pytest.mark.asyncio
async def test_execute_missing_params(env):
    res = await tools.execute_tool({"args": {}})
    assert res["success"] is False
    assert res["status_code"] == 400
