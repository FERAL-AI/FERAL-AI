"""Operator report 2026-06-07 — chat tool ``health_summary`` answered
"no current data coming in from your health sources right now" while
the W300 glasses were actively streaming heart rate into the brain's
perception frame. Whoop and Oura were both disconnected, so the prior
``HealthAggregator.get_health_summary`` returned an empty snapshot
(``sources: []``, ``resting_hr: None``) and the LLM rightly said
"no data".

Fix in this PR: ``HealthAggregator`` now accepts an optional
``live_wearable_provider`` callable; the brain wires it up at boot
in ``BrainState._latest_live_wearable_snapshot`` so the chat path
sees the same fresh wearable HR/SpO2 the WebUI's
``/api/dashboard.latest_health`` already surfaces.

These tests pin:

* The aggregator surfaces ``current_hr`` / ``current_hr_source`` from
  the provider when no Whoop/Oura recovery data is present, AND
  promotes the wearable bpm into the ``resting_hr`` slot so the
  manifest-declared shape never returns ``None`` while a live
  reading exists.
* Wearable sources are appended to the ``sources`` array so the LLM
  prompt can describe where the reading came from.
* When Whoop/Oura DO contribute, the wearable adds a ``current_hr``
  alongside the existing ``resting_hr`` — never demotes the
  cloud-mirror values silently.
* Provider exceptions are swallowed (best-effort) and never crash
  the aggregator — same defensive contract as the other branches.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from integrations.health_platforms import HealthAggregator  # noqa: E402


@pytest.mark.asyncio
async def test_health_summary_surfaces_w300_when_whoop_oura_offline():
    """No cloud platforms connected, but the W300 is streaming HR into
    perception. The chat path must surface the live reading instead
    of returning the all-null snapshot that triggered the operator's
    "no current data" complaint."""
    def provider():
        return {
            "heart_rate": 72,
            "heart_rate_source": "jw_health_glasses",
        }

    aggregator = HealthAggregator(live_wearable_provider=provider)
    result = await aggregator.execute("health_summary", {}, vault={})
    data = result["data"]

    assert data["current_hr"] == 72
    assert data["current_hr_source"] == "jw_health_glasses"
    # When no cloud platform contributed a resting_hr, the live
    # wearable bpm is the best answer for the manifest-declared
    # ``resting_hr`` slot.
    assert data["resting_hr"] == 72
    assert "jw_health_glasses" in data["sources"]


@pytest.mark.asyncio
async def test_health_summary_surfaces_veepoo_wristband():
    """Wearable source string flows through verbatim so the LLM can
    name the device. Pinned because the iOS adapters emit canonical
    capability ids (``veepoo_wristband``, ``jw_health_glasses``) and
    the chat surface must round-trip them unchanged."""
    aggregator = HealthAggregator(
        live_wearable_provider=lambda: {
            "heart_rate": 64,
            "heart_rate_source": "veepoo_wristband",
            "spo2": 97,
            "spo2_source": "veepoo_wristband",
        },
    )
    result = await aggregator.execute("health_summary", {}, vault={})
    data = result["data"]

    assert data["current_hr"] == 64
    assert data["current_hr_source"] == "veepoo_wristband"
    assert data["current_spo2"] == 97
    assert data["current_spo2_source"] == "veepoo_wristband"
    assert "veepoo_wristband" in data["sources"]


@pytest.mark.asyncio
async def test_health_summary_keeps_whoop_resting_hr_when_both_present():
    """Whoop's recovery resting_hr stays authoritative even when a
    live wearable reading exists; the wearable populates
    ``current_hr`` (the LIVE slot) without overwriting the
    cloud-mirror's resting estimate. This prevents a fresh PPG
    spike from clobbering the daily resting baseline."""
    fake_whoop = MagicMock()
    fake_whoop.connected = True
    fake_whoop.get_recovery = AsyncMock(return_value={
        "success": True,
        "data": {"recovery_score": 70, "resting_hr": 55, "hrv_ms": 62},
    })
    fake_whoop.get_sleep = AsyncMock(return_value={"success": True, "data": []})
    fake_whoop.get_cycles = AsyncMock(return_value={"success": True, "data": []})

    aggregator = HealthAggregator(
        whoop=fake_whoop,
        live_wearable_provider=lambda: {
            "heart_rate": 105,  # live workout spike
            "heart_rate_source": "jw_health_glasses",
        },
    )
    result = await aggregator.execute("health_summary", {}, vault={})
    data = result["data"]

    assert data["resting_hr"] == 55  # whoop recovery wins resting slot
    assert data["current_hr"] == 105  # wearable wins LIVE slot
    assert data["current_hr_source"] == "jw_health_glasses"
    assert "whoop" in data["sources"]
    assert "jw_health_glasses" in data["sources"]


@pytest.mark.asyncio
async def test_health_summary_no_provider_keeps_legacy_shape():
    """Passing no provider must leave the prior all-null snapshot
    intact (back-compat with the manifest contract callers depend
    on). ``current_*`` fields default to None so consumers can
    distinguish "no live wearable" from "0 bpm"."""
    aggregator = HealthAggregator()
    result = await aggregator.execute("health_summary", {}, vault={})
    data = result["data"]

    assert data["current_hr"] is None
    assert data["current_spo2"] is None
    assert data["sources"] == []


@pytest.mark.asyncio
async def test_health_summary_provider_exception_is_swallowed():
    """A buggy provider must not crash the aggregator — the rest of
    the chat tool dispatch (and the WebUI) still gets a usable
    snapshot. Defensive same-shape-on-error contract pins so the
    ``health_summary`` tool never returns an HTTP-shaped error from
    a wearable hiccup."""
    def boom():
        raise RuntimeError("perception engine boom")

    aggregator = HealthAggregator(live_wearable_provider=boom)
    result = await aggregator.execute("health_summary", {}, vault={})
    assert result["success"] is True
    data = result["data"]
    assert data["current_hr"] is None
    assert data["sources"] == []


@pytest.mark.asyncio
async def test_health_summary_provider_returns_none_is_ok():
    """A provider with no fresh sample returns None — aggregator
    must not crash and must not falsely fabricate a ``current_hr``."""
    aggregator = HealthAggregator(live_wearable_provider=lambda: None)
    result = await aggregator.execute("health_summary", {}, vault={})
    data = result["data"]
    assert data["current_hr"] is None
    assert data["resting_hr"] is None
    assert data["sources"] == []
