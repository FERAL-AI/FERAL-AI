"""Characterization guard: live wearable biometrics ARE reachable from a turn.

This file fixes no defect. It pins a path that currently works and that
nothing else covers, because a sibling audit lane established that the
obvious route is broken and the conclusion "biometrics are unreachable"
would have been wrong.

What that lane found, and it is correct: `_handle_biometric_device_event`
writes `~/.feral/baselines.db`, which holds 1,286 real heart-rate samples
spanning 2026-06-21..2026-08-07, and NONE of them are in `memory.db`.
What memory.db holds is 209 heart-rate notes, 199 of them the same
115bpm value repeated. So `notes_memory__fused_timeline` -- the tool the
system prompt steers all personal recall toward -- cannot answer "what
was my heart rate on Tuesday" truthfully.

It is answerable anyway, by a different route, verified live against the
real database:

    health_data__health_history
      -> HealthAggregator.get_health_history
      -> BaselineEngine.get_samples
      -> baselines.db                      -> 1286 samples,
                                              sources jw_health_glasses,
                                              veepoo_wristband

and `_heuristic_route` puts `health_data` first for every past-tense
phrasing tested ("what was my heart rate on Tuesday" -> confident_lead,
active=['health_data', ...]).

Both halves are load-bearing and neither had a test. If the provider
wiring at `api/state.py` (`biometric_history_provider=lambda:
self.baseline_engine`) is dropped, or `health_data` stops leading the
route for past-tense health queries, the promise silently falls back to
the 115bpm noise in memory.db. These tests fail if either happens.
"""

from __future__ import annotations

import time

import pytest

from agents.baseline_engine import BaselineEngine
from agents.orchestrator import Orchestrator
from integrations.health_platforms import HealthAggregator
from skills.registry import SkillRegistry

DAY = 86400.0


@pytest.fixture()
def engine(tmp_path):
    eng = BaselineEngine(db_path=str(tmp_path / "baselines.db"))
    now = time.time()
    # Same shape the glasses write: metric "hr", source is the BLE
    # capability id, one row per reading.
    for i in range(20):
        eng.record_sample("hr", 72.0 + i % 5, source="jw_health_glasses", ts=now - i * 3600)
    return eng


@pytest.mark.asyncio
async def test_history_endpoint_reaches_the_durable_store(engine):
    """The endpoint the LLM calls must return the samples the glasses wrote."""
    agg = HealthAggregator(biometric_history_provider=lambda: engine)

    history = await agg.get_health_history(days=180)

    assert "hr" in history["metrics"]
    assert len(history["series"]["hr"]) == 20
    assert history["sources"] == ["jw_health_glasses"]


@pytest.mark.asyncio
async def test_history_carries_real_timestamps_and_values(engine):
    """"On Tuesday" needs per-sample timestamps, not a daily average."""
    agg = HealthAggregator(biometric_history_provider=lambda: engine)
    history = await agg.get_health_history(days=180)

    entries = history["series"]["hr"]
    assert all(e["ts"] > 0 for e in entries)
    assert all(70.0 <= e["value"] <= 80.0 for e in entries)
    # Ascending, so a caller can slice a day out of it.
    assert [e["ts"] for e in entries] == sorted(e["ts"] for e in entries)


@pytest.mark.asyncio
async def test_unwired_provider_says_so_instead_of_returning_empty():
    """An empty series and "no store wired" are different facts.

    Returning {} for both would let a broken boot render as "you have no
    health history", which is the failure mode this whole audit is about.
    """
    agg = HealthAggregator(biometric_history_provider=None)
    history = await agg.get_health_history(days=180)

    assert history["metrics"] == []
    assert "no durable biometric store" in history["note"].lower()


@pytest.mark.parametrize("query", [
    "what was my heart rate on Tuesday",
    "what was my heart rate yesterday",
    "how has my heart rate been this week",
    "what's my heart rate",
])
def test_health_data_leads_the_route_for_health_recall(query):
    """`_R_HEALTH` only matches present tense, so past-tense queries fall
    through to keyword routing. They must still lead with health_data --
    if they do not, the prompt's "personal recall -> fused_timeline" rule
    wins and the answer comes from the wrong store.
    """
    registry = SkillRegistry()
    registry.load_from_directory("skills/manifests")
    orch = Orchestrator.__new__(Orchestrator)
    orch.skills = registry

    skills, _label = Orchestrator._heuristic_route(orch, query)
    ids = [s.skill_id for s in (skills or [])]

    assert ids and ids[0] == "health_data", (
        f"{query!r} routed to {ids[:3]}; health_data is the only skill that "
        "can read baselines.db"
    )
