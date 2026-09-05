"""Every backing-implementation call has a wall-clock budget.

``SkillExecutor._execute_inner`` awaited ``impl.execute(...)`` with no
``asyncio.wait_for``. On 2026-09-02 one ``email__get_unread_count`` call
ran for 181 s and the executor had no opinion about it. Manifests already
carried five spellings of a timeout (``timeout`` x5, ``timeout_s`` x2,
``timeout_ms`` x2, ``timeout_seconds`` x1, ``manual_timeout_s`` x1) that
nothing at the execute site read, and the ``SkillEndpoint`` model had no
timeout field at all.

Pinned here: a slow implementation comes back as a 504 envelope inside
the budget, the legacy spellings normalise to seconds, a per-call timeout
argument raises the budget rather than being cut short by it, and every
shipped manifest still loads.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from models.skill_manifest import (
    BrandProfile,
    EndpointParam,
    LEGACY_TIMEOUT_KEYS,
    SkillEndpoint,
    SkillManifest,
    normalize_timeout_seconds,
)
from skills.executor import (
    DEFAULT_TOOL_TIMEOUT_SECONDS,
    TOOL_TIMEOUT_GRACE_SECONDS,
    SkillExecutor,
    tool_budget_seconds,
)
from skills.impl import register_instance

MANIFEST_DIR = Path(__file__).resolve().parents[1] / "skills" / "manifests"


class _SlowImpl:
    skill_id = "slow_skill"

    def __init__(self):
        self.calls = 0

    async def execute(self, endpoint_id, args, vault):
        self.calls += 1
        await asyncio.sleep(5)
        return {"success": True, "data": {"never": "reached"}}


def _manifest(**endpoint_overrides) -> tuple[SkillManifest, SkillEndpoint]:
    endpoint = SkillEndpoint(
        id="wait",
        method="PYTHON",
        url="python://slow_skill/wait",
        description="sleeps",
        safety_tier="safe",
        read_only_hint=True,
        **endpoint_overrides,
    )
    manifest = SkillManifest(
        skill_id="slow_skill",
        brand=BrandProfile(name="Slow"),
        description="a skill that sleeps",
        endpoints=[endpoint],
        max_calls_per_hour=0,
    )
    return manifest, endpoint


@pytest.mark.asyncio
async def test_slow_impl_returns_504_inside_the_budget():
    impl = _SlowImpl()
    register_instance("slow_skill", impl)
    manifest, endpoint = _manifest(timeout_seconds=0.2)

    executor = SkillExecutor()
    t0 = time.monotonic()
    result = await executor._execute_inner("slow_skill__wait", {}, manifest, endpoint)
    elapsed = time.monotonic() - t0

    assert elapsed < 1.0, f"took {elapsed:.2f}s; the 0.2s budget was not enforced"
    assert impl.calls == 1
    assert result["success"] is False
    assert result["status_code"] == 504
    assert result["data"] is None
    assert result["error"] == "slow_skill__wait exceeded 0s"


@pytest.mark.asyncio
async def test_public_execute_surfaces_the_504_envelope():
    """Through ``execute`` (gates, rate limit, audit) rather than the inner
    call, so the envelope shape the callers see is the one asserted."""
    register_instance("slow_skill", _SlowImpl())
    manifest, endpoint = _manifest(timeout_seconds=0.2)

    result = await SkillExecutor().execute("slow_skill__wait", {}, manifest, endpoint)
    assert result["success"] is False
    assert result["status_code"] == 504
    assert "exceeded" in result["error"]


@pytest.mark.parametrize(
    "key, value, expected",
    [
        ("timeout", 45, 45.0),
        ("timeout_s", 8, 8.0),
        ("timeout_ms", 30000, 30.0),
        ("timeout_seconds", "10", 10.0),
        ("manual_timeout_s", 20, 20.0),
    ],
)
def test_legacy_spellings_normalise_to_seconds(key, value, expected):
    assert key in LEGACY_TIMEOUT_KEYS
    assert normalize_timeout_seconds(key, value) == pytest.approx(expected)


@pytest.mark.parametrize("key", sorted(LEGACY_TIMEOUT_KEYS))
def test_legacy_endpoint_key_lands_in_timeout_seconds(key):
    raw = {"id": "e", "url": "x", "description": "d", key: 4000 if key == "timeout_ms" else 4}
    endpoint = SkillEndpoint(**raw)
    assert endpoint.timeout_seconds == pytest.approx(4.0)


def test_explicit_timeout_seconds_wins_over_legacy_key():
    endpoint = SkillEndpoint(id="e", url="x", description="d", timeout_seconds=7, timeout_ms=1)
    assert endpoint.timeout_seconds == 7.0


@pytest.mark.parametrize("bad", [None, 0, -3, "soon", True])
def test_unusable_values_fall_back_to_none(bad):
    assert normalize_timeout_seconds("timeout", bad) is None


def test_budget_precedence_endpoint_then_skill_then_default(monkeypatch):
    monkeypatch.delenv("FERAL_TOOL_TIMEOUT_SECONDS", raising=False)
    manifest, endpoint = _manifest()
    assert tool_budget_seconds(manifest, endpoint, {}) == DEFAULT_TOOL_TIMEOUT_SECONDS

    skill_wide = manifest.model_copy(update={"timeout_seconds": 120.0})
    assert tool_budget_seconds(skill_wide, endpoint, {}) == 120.0

    per_endpoint = endpoint.model_copy(update={"timeout_seconds": 9.0})
    assert tool_budget_seconds(skill_wide, per_endpoint, {}) == 9.0

    # Operator override, read through default_tool_timeout_seconds().
    monkeypatch.setattr("skills.executor.os.getenv", lambda name, default="": "90" if name == "FERAL_TOOL_TIMEOUT_SECONDS" else default)
    assert tool_budget_seconds(manifest, endpoint, {}) == 90.0
    monkeypatch.setattr("skills.executor.os.getenv", lambda name, default="": "junk" if name == "FERAL_TOOL_TIMEOUT_SECONDS" else default)
    assert tool_budget_seconds(manifest, endpoint, {}) == DEFAULT_TOOL_TIMEOUT_SECONDS


def test_caller_supplied_timeout_raises_the_budget_never_lowers_it(monkeypatch):
    """``coding_tools__bash`` with ``timeout=600`` must not be cut at 30 s,
    and ``code_interpreter`` (manifest default 45) must not be cut at 30 s
    when the model omits the argument."""
    monkeypatch.delenv("FERAL_TOOL_TIMEOUT_SECONDS", raising=False)
    manifest, endpoint = _manifest(
        params=[EndpointParam(name="timeout", type="integer", required=False, default="45")],
    )
    assert tool_budget_seconds(manifest, endpoint, {}) == 45.0 + TOOL_TIMEOUT_GRACE_SECONDS
    assert tool_budget_seconds(manifest, endpoint, {"timeout": 600}) == 600.0 + TOOL_TIMEOUT_GRACE_SECONDS
    # A tiny per-call timeout does not shrink the budget below the default.
    assert tool_budget_seconds(manifest, endpoint, {"timeout": 1}) == DEFAULT_TOOL_TIMEOUT_SECONDS

    ms = _manifest(
        params=[EndpointParam(name="timeout_ms", type="integer", required=False, default="60000")],
    )[1]
    assert tool_budget_seconds(manifest, ms, {}) == 60.0 + TOOL_TIMEOUT_GRACE_SECONDS


def test_every_shipped_manifest_still_loads():
    paths = sorted(MANIFEST_DIR.glob("*.json"))
    assert paths, "no manifests found"
    for path in paths:
        manifest = SkillManifest(**json.loads(path.read_text()))
        for endpoint in manifest.endpoints:
            budget = tool_budget_seconds(manifest, endpoint, {})
            assert budget >= DEFAULT_TOOL_TIMEOUT_SECONDS, (path.name, endpoint.id, budget)
