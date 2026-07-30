"""Batch 2 — executor enforcement of manifest permissions + rate limits.

Pins that ``models/skill_manifest.py`` metadata (``permissions``,
``max_calls_per_hour``) is now actually honored by ``skills/executor.py``:

  * exceeding the per-skill hourly cap is refused (429), and
  * an endpoint whose declared ``required_permission`` isn't granted by the
    manifest is refused (403) before any execution happens,

while preserving the permissive defaults (``max_calls_per_hour=1000``,
``permissions=[]``) so every existing manifest keeps loading and running.
"""

from __future__ import annotations

import glob
import json
import os
from unittest.mock import AsyncMock

import pytest

from models.skill_manifest import BrandProfile, SkillEndpoint, SkillManifest
from skills.executor import SkillExecutor


def _skill(skill_id="t_skill", permissions=None, max_calls_per_hour=1000):
    return SkillManifest(
        skill_id=skill_id,
        brand=BrandProfile(name="Test"),
        description="test skill",
        permissions=permissions if permissions is not None else [],
        max_calls_per_hour=max_calls_per_hour,
    )


def _endpoint(ep_id="ep", required_permission=None):
    return SkillEndpoint(
        id=ep_id,
        method="GET",
        url="https://example.invalid/endpoint",
        description="test endpoint",
        required_permission=required_permission,
    )


def _passthrough_executor():
    ex = SkillExecutor()
    # Skip real HTTP — we're testing the enforcement gates in execute(),
    # which run BEFORE _execute_inner.
    ex._execute_inner = AsyncMock(
        return_value={"success": True, "status_code": 200, "data": {}, "error": None}
    )
    return ex


class TestPermissionGate:
    @pytest.mark.asyncio
    async def test_undeclared_permission_is_refused(self):
        ex = _passthrough_executor()
        skill = _skill(permissions=[])
        ep = _endpoint(required_permission="camera")

        res = await ex.execute("t_skill__ep", {}, skill, ep)
        assert res["success"] is False
        assert res["status_code"] == 403
        assert "camera" in res["error"]
        ex._execute_inner.assert_not_called()

    @pytest.mark.asyncio
    async def test_declared_permission_is_allowed(self):
        ex = _passthrough_executor()
        skill = _skill(permissions=["camera"])
        ep = _endpoint(required_permission="camera")

        res = await ex.execute("t_skill__ep", {}, skill, ep)
        assert res["success"] is True
        ex._execute_inner.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_required_permission_is_allowed(self):
        # Backward compat: an endpoint declaring no permission runs even when
        # the manifest grants none.
        ex = _passthrough_executor()
        skill = _skill(permissions=[])
        ep = _endpoint(required_permission=None)

        res = await ex.execute("t_skill__ep", {}, skill, ep)
        assert res["success"] is True
        ex._execute_inner.assert_awaited_once()


class TestRateLimit:
    @pytest.mark.asyncio
    async def test_hourly_cap_is_enforced(self):
        ex = _passthrough_executor()
        skill = _skill(max_calls_per_hour=2)
        ep = _endpoint()

        r1 = await ex.execute("t_skill__ep", {}, skill, ep)
        r2 = await ex.execute("t_skill__ep", {}, skill, ep)
        assert r1["success"] is True and r2["success"] is True

        r3 = await ex.execute("t_skill__ep", {}, skill, ep)
        assert r3["success"] is False
        assert r3["status_code"] == 429
        assert "Rate limit" in r3["error"]
        # Only the two permitted calls reached execution.
        assert ex._execute_inner.await_count == 2

    @pytest.mark.asyncio
    async def test_cap_is_per_skill(self):
        ex = _passthrough_executor()
        ep = _endpoint()
        skill_a = _skill(skill_id="a", max_calls_per_hour=1)
        skill_b = _skill(skill_id="b", max_calls_per_hour=1)

        assert (await ex.execute("a__ep", {}, skill_a, ep))["success"] is True
        # skill_a is now capped, but skill_b has its own budget.
        assert (await ex.execute("a__ep", {}, skill_a, ep))["success"] is False
        assert (await ex.execute("b__ep", {}, skill_b, ep))["success"] is True

    @pytest.mark.asyncio
    async def test_nonpositive_cap_is_unlimited(self):
        ex = _passthrough_executor()
        skill = _skill(max_calls_per_hour=0)
        ep = _endpoint()
        for _ in range(5):
            assert (await ex.execute("t_skill__ep", {}, skill, ep))["success"] is True


class TestDefaultsPreserved:
    def test_manifest_defaults(self):
        skill = SkillManifest(
            skill_id="bare",
            brand=BrandProfile(name="Bare"),
            description="bare manifest",
        )
        assert skill.max_calls_per_hour == 1000
        assert skill.permissions == []

    def test_endpoint_permission_defaults_none(self):
        ep = SkillEndpoint(id="e", method="GET", url="https://x", description="d")
        assert ep.required_permission is None

    def test_shipped_manifests_are_not_refused_by_permission_gate(self):
        # No shipped manifest sets required_permission, so the gate must pass
        # every endpoint — this guards against a future over-broad gate that
        # would break existing skills.
        ex = SkillExecutor()
        manifest_dir = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "skills", "manifests"
        )
        files = glob.glob(os.path.join(manifest_dir, "*.json"))
        assert files, "no shipped manifests found"
        for path in files:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            skill = SkillManifest(**data)
            for ep in skill.endpoints:
                assert ex._check_permission(skill, ep) is None, (
                    f"{skill.skill_id}__{ep.id} unexpectedly refused"
                )
