"""Lane 05 (Wave 2) — health_data manifest contract.

Closes AUDIT-r14 finding 16 fix #2 (missing health_data manifest)
and unblocks THESIS_SCENARIOS S2 (multi-device mesh — phone HealthKit
data flows into the brain through this skill).

The manifest declares three endpoints (health_summary, sleep_trend,
recovery_trend) which match the HealthAggregator.execute dispatch.
The aggregator's execute() signature was also missing the third
``vault`` arg the skill executor passes — fixed in  so dispatch
no longer raises TypeError on the very first call.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.tool_dispatch_validator import ToolDispatchValidator  # noqa: E402
from integrations.health_platforms import HealthAggregator  # noqa: E402
from models.skill_manifest import SkillManifest  # noqa: E402

MANIFEST_PATH = ROOT / "skills" / "manifests" / "health_data.json"


def test_manifest_loads_and_is_valid_pydantic():
    data = json.loads(MANIFEST_PATH.read_text())
    manifest = SkillManifest(**data)
    assert manifest.skill_id == "health_data"
    endpoint_ids = {ep.id for ep in manifest.endpoints}
    assert endpoint_ids == {
        "health_summary", "sleep_trend", "recovery_trend", "vitals_trend",
        "health_history",
    }


def test_manifest_endpoints_match_health_aggregator_dispatch():
    """Every manifest endpoint id resolves to a HealthAggregator method."""
    validator = ToolDispatchValidator()
    for endpoint_id in (
        "health_summary", "sleep_trend", "recovery_trend", "vitals_trend",
        "health_history",
    ):
        violations = validator.contract_violations("health_data", endpoint_id)
        assert not violations, (
            f"Manifest↔backend mismatch on health_data__{endpoint_id}: {violations}"
        )


def test_manifest_listed_in_trigger_phrases_for_health_questions():
    """Health-question phrasing must hit health_data triggers so the
    keyword router doesn't fall back to web search or notes_memory."""
    data = json.loads(MANIFEST_PATH.read_text())
    triggers = {p.lower() for p in data["trigger_phrases"]}
    for required in ("how did i sleep", "what's my heart rate", "my hrv"):
        assert required in triggers, f"missing trigger: {required!r}"


@pytest.mark.asyncio
async def test_aggregator_execute_accepts_vault_arg():
    """The 3-arg dispatch contract used by the skill executor must work."""
    aggregator = HealthAggregator()
    # No platforms connected → health_summary still returns a snapshot
    # with all fields None and an empty sources list.
    result = await aggregator.execute("health_summary", {}, vault={})
    assert result["success"] is True
    assert result["data"]["sleep_hours"] is None
    assert result["data"]["sources"] == []


@pytest.mark.asyncio
async def test_aggregator_execute_unknown_endpoint_returns_structured_error():
    aggregator = HealthAggregator()
    result = await aggregator.execute("not_a_thing", {}, vault={})
    assert result["success"] is False
    assert result["status_code"] == 404
    assert "not_a_thing" in result["error"]


@pytest.mark.asyncio
async def test_health_summary_merges_whoop_and_oura(monkeypatch):
    """When both platforms are connected, the summary merges fields with
    whoop-precedence for recovery/HR/HRV and oura filling readiness +
    activity. This pins the contract LLM E2E callers depend on."""
    aggregator = HealthAggregator()

    fake_whoop = MagicMock()
    fake_whoop.connected = True
    fake_whoop.get_recovery = AsyncMock(return_value={
        "success": True,
        "data": {"recovery_score": 72, "resting_hr": 58, "hrv_ms": 65},
    })
    fake_whoop.get_sleep = AsyncMock(return_value={
        "success": True,
        "data": [{"total_sleep_hours": 7.5, "sleep_score": 88}],
    })
    fake_whoop.get_cycles = AsyncMock(return_value={
        "success": True,
        "data": [{"strain": 14.2}],
    })
    aggregator._whoop = fake_whoop

    fake_oura = MagicMock()
    fake_oura.connected = True
    fake_oura.get_readiness = AsyncMock(return_value={
        "success": True,
        "data": [{"readiness_score": 78, "resting_hr": 60}],
    })
    fake_oura.get_sleep = AsyncMock(return_value={
        "success": True,
        "data": [{"total_sleep_hours": 7.4, "sleep_score": 90}],
    })
    fake_oura.get_activity = AsyncMock(return_value={
        "success": True,
        "data": [{"activity_score": 82}],
    })
    aggregator._oura = fake_oura

    result = await aggregator.execute("health_summary", {}, vault={})
    data = result["data"]

    assert data["recovery_score"] == 72
    assert data["resting_hr"] == 58  # whoop precedence
    assert data["hrv"] == 65
    assert data["sleep_hours"] == 7.5
    assert data["strain"] == 14.2
    assert data["readiness"] == 78
    assert data["activity_score"] == 82
    assert set(data["sources"]) == {"whoop", "oura"}
