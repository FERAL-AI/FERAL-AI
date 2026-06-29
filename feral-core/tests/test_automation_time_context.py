"""Regression tests for the automation / time-context bug batch
(branch ``fix/automation-time-context``).

One focused test per user-reported problem:

  P1  Host-local timezone is derived (not hardcoded UTC) and injected
      into the per-turn system prompt; the scheduler defaults to it.
  P2  Scheduled device/action requests route to feral_routines (not a
      one-shot reminder); the poll loop tightens near a due job.
  P3  feral_routines / feral_reminders create verify-after-write and
      only report success when the item is actually in the list.
  P4  A one-shot routine with auto_confirm dispatches the device action
      directly even when the cron safety pre-flight would DENY it.
  P5  A short ambiguous follow-up ("check now") is routed using the
      last-referenced subject from the session's recent turns.
"""
from __future__ import annotations

import os
import tempfile
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from agents.orchestrator import Orchestrator
from agents.scheduler import CronService, JobType
from config.loader import local_timezone_name
from models.skill_manifest import BrandProfile, SkillEndpoint, SkillManifest


# ── shared skill catalog for routing tests ─────────────────────────


def _skill(skill_id, triggers, categories=None):
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
            )
        ],
    )


CATALOG = {
    "feral_routines": _skill(
        "feral_routines",
        ["every day at", "daily", "recurring", "schedule a routine", "set up a routine"],
        ["automation", "scheduling"],
    ),
    "feral_reminders": _skill(
        "feral_reminders",
        ["create reminder", "remind me", "list reminders"],
        ["reminders"],
    ),
    "cutebot": _skill(
        "cutebot",
        ["follow the line", "cutebot", "drive the robot"],
        ["robot", "hardware"],
    ),
    "calendar_google": _skill(
        "calendar_google",
        ["what's on my calendar", "schedule a meeting"],
        ["calendar"],
    ),
    "web_search": _skill("web_search", ["search for", "google"], ["search"]),
    "spotify_music": _skill("spotify_music", ["play music"], ["music"]),
}


def _make_orchestrator(llm_available=False):
    reg = MagicMock()
    reg.skills = CATALOG

    def _find(query, top_k=5):
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
        memory=None,
        vision_buffer=None,
        perception=None,
        learner=None,
    )
    orch.llm = MagicMock()
    orch.llm.available = llm_available
    # Routing must NOT need the LLM for any of these prompts.
    orch.llm.chat_with_failover = AsyncMock(
        side_effect=AssertionError("LLM routing must not be needed here")
    )
    orch.llm.route_call = MagicMock(
        side_effect=AssertionError("route_call must not be needed here")
    )
    return orch


# ── P1: timezone derivation + time-context injection ───────────────


def test_local_timezone_honours_env_override(monkeypatch):
    monkeypatch.setenv("FERAL_TIMEZONE", "America/Los_Angeles")
    assert local_timezone_name() == "America/Los_Angeles"


def test_current_time_context_injects_local_time(monkeypatch):
    monkeypatch.setenv("FERAL_TIMEZONE", "America/Los_Angeles")
    from agents.identity_loader import current_time_context

    block = current_time_context()
    assert "Current local time:" in block
    assert "America/Los_Angeles" in block
    # Must steer the model NOT to ask for / assume UTC.
    assert "do NOT ask the user for their" in block


def test_scheduler_defaults_to_host_local_tz(monkeypatch):
    monkeypatch.setenv("FERAL_TIMEZONE", "America/New_York")
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        svc = CronService(db_path=path)
        assert str(svc._timezone) == "America/New_York"
        # A job created without an explicit tz_name inherits the local tz.
        job = svc.create_job(JobType.SCHEDULED, "daily 17:00", "x", {}, "")
        assert job.tz_name == "America/New_York"
        svc.close()
    finally:
        os.unlink(path)


def test_scheduler_explicit_config_tz_overrides(monkeypatch):
    monkeypatch.setenv("FERAL_TIMEZONE", "America/New_York")
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        svc = CronService(db_path=path, config={"timezone": "Asia/Tokyo"})
        assert str(svc._timezone) == "Asia/Tokyo"
        svc.close()
    finally:
        os.unlink(path)


# ── P2: routing scheduled actions → feral_routines + tighter poll ──


@pytest.mark.asyncio
async def test_recurring_device_action_routes_to_routines():
    orch = _make_orchestrator()
    result = await orch._route_prompt("follow the line every day at 5pm")
    ids = [s.skill_id for s in result]
    assert ids and ids[0] == "feral_routines"
    # The device skill is still exposed so the routine payload can target it.
    assert "cutebot" in ids


@pytest.mark.asyncio
async def test_oneshot_device_action_routes_to_routines():
    orch = _make_orchestrator()
    result = await orch._route_prompt(
        "run the cutebot one time today at 3:01pm and auto confirm it"
    )
    ids = [s.skill_id for s in result]
    assert ids and ids[0] == "feral_routines"
    assert "cutebot" in ids


@pytest.mark.asyncio
async def test_genuine_remind_me_still_routes_to_reminders():
    orch = _make_orchestrator()
    result = await orch._route_prompt("remind me to call mom at 5pm")
    assert [s.skill_id for s in result] == ["feral_reminders"]


def test_poll_interval_bounds():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        svc = CronService(db_path=path)
        # No enabled jobs → flat 30s ceiling.
        assert svc._poll_interval() == 30.0
        # A job already due → tighten to ~1s so it fires on time.
        job = svc.create_job(JobType.SCHEDULED, "every 30m", "x", {}, "")
        import time as _t
        with svc._lock:
            svc._conn.execute(
                "UPDATE scheduled_jobs SET next_run = ? WHERE id = ?",
                (_t.time() - 5, job.id),
            )
            svc._conn.commit()
        assert svc._poll_interval() == 1.0
        svc.close()
    finally:
        os.unlink(path)


# ── P3: verify-after-write honesty ─────────────────────────────────


@pytest.mark.asyncio
async def test_routines_create_verifies_and_succeeds():
    import skills.impl.feral_routines as fr

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    svc = CronService(db_path=path)
    fr.set_scheduler_override(svc)
    try:
        skill = fr.FeralRoutinesSkill()
        res = await skill.execute("create", {"cron_expr": "daily 17:00", "prompt": "x"}, {})
        assert res["success"] is True
        assert res["data"].get("verified") is True
    finally:
        fr.set_scheduler_override(None)
        svc.close()
        os.unlink(path)


@pytest.mark.asyncio
async def test_routines_create_reports_failure_when_not_persisted():
    import skills.impl.feral_routines as fr

    class _Job:
        id = 999
        job_type = JobType.SCHEDULED
        cron_expr = "daily 17:00"
        description = "x"
        payload = {}
        session_id = ""
        created_at = 0.0
        last_run = None
        next_run = 1.0
        enabled = True
        run_count = 0
        recurring = True
        tz_name = "UTC"

    class _LyingScheduler:
        def create_job(self, *a, **k):
            return _Job()

        def get_job(self, job_id):
            return None  # write "failed" — not in the store

        def list_jobs(self, session_id=None):
            return []

    fr.set_scheduler_override(_LyingScheduler())
    try:
        skill = fr.FeralRoutinesSkill()
        res = await skill.execute("create", {"cron_expr": "daily 17:00", "prompt": "x"}, {})
        assert res["success"] is False
        assert res["reason"] == "verify_after_write_failed"
    finally:
        fr.set_scheduler_override(None)


@pytest.mark.asyncio
async def test_reminders_create_reports_failure_when_not_persisted(monkeypatch):
    import skills.impl.feral_reminders as frem

    skill = frem.FeralRemindersSkill()
    # Simulate a write that silently drops the data on disk.
    monkeypatch.setattr(frem, "_save_items", lambda path, items: None)
    monkeypatch.setattr(frem, "_load_items", lambda path: [])
    res = await skill.execute(
        "create", {"title": "call mom", "due": "2026-06-24T17:00:00-07:00"}, {}
    )
    assert res["success"] is False
    assert res["reason"] == "verify_after_write_failed"


# ── P4: one-shot auto-confirm routine drives the device ────────────


def test_oneshot_autoconfirm_routine_dispatches_follow_line():
    import api.server as server
    from skills.base import BaseSkill
    from skills.impl import register_instance
    from skills.registry import SkillRegistry

    class _RecordingSkill(BaseSkill):
        def __init__(self, skill_id):
            super().__init__(skill_id=skill_id)
            self.calls = []

        async def execute(self, endpoint_id, args, vault):
            self.calls.append((endpoint_id, args))
            return {"success": True, "status_code": 200, "data": {"ran": endpoint_id}, "error": None}

    fd, cron_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    cron = CronService(db_path=cron_path)
    reg = SkillRegistry()
    cutebot = _RecordingSkill("cutebot")
    # follow_line declared DENY to prove auto_confirm bypasses the cron
    # pre-flight for an explicitly user-scheduled device action.
    reg.register(
        SkillManifest(
            skill_id="cutebot",
            brand=BrandProfile(name="cutebot", primary_color="#111"),
            description="robot",
            endpoints=[
                SkillEndpoint(
                    id="follow_line",
                    method="PYTHON",
                    url="python://cutebot/follow_line",
                    description="follow the line",
                    safety_tier="deny",
                )
            ],
        )
    )
    register_instance("cutebot", cutebot)

    saved = {k: getattr(server.state, k, None) for k in ("cron_service", "skill_registry", "orchestrator", "cron_cost_guard", "taskflows")}
    server.state.cron_service = cron
    server.state.skill_registry = reg
    server.state.orchestrator = None
    server.state.cron_cost_guard = None
    server.state.taskflows = None
    try:
        payload = {"skill": "cutebot", "endpoint": "follow_line", "auto_confirm": True}
        job = cron.create_job(JobType.SCHEDULED, "daily 15:01", "cutebot", payload, "", recurring=False)
        server.execute_routine_job(job)
        # auto_confirm → the device action fired despite the DENY tier.
        assert cutebot.calls == [("follow_line", {})]
        run = cron.get_runs(job.id, limit=1)[0]
        assert run["status"] == "success"

        # Control: same DENY endpoint WITHOUT auto_confirm is skipped.
        cutebot.calls.clear()
        job2 = cron.create_job(JobType.SCHEDULED, "daily 15:01", "cutebot", {"skill": "cutebot", "endpoint": "follow_line"}, "", recurring=False)
        server.execute_routine_job(job2)
        assert cutebot.calls == []
        run2 = cron.get_runs(job2.id, limit=1)[0]
        assert run2["status"] == "skipped"
    finally:
        for k, v in saved.items():
            setattr(server.state, k, v)
        cron.close()
        os.unlink(cron_path)


# ── P5: conversational coreference for short follow-ups ────────────


@pytest.mark.asyncio
async def test_followup_resolves_against_last_subject():
    orch = _make_orchestrator()
    sid = "sess-coref"
    orch.conversation_history[sid] = [
        {"role": "user", "content": "check the cutebot"},
        {"role": "assistant", "content": "The cutebot looks online."},
    ]
    result = await orch._route_prompt("check now", session_id=sid)
    assert "cutebot" in [s.skill_id for s in result]


@pytest.mark.asyncio
async def test_followup_without_history_does_not_invent_subject():
    orch = _make_orchestrator()
    result = await orch._route_prompt("check now", session_id="empty-sess")
    assert "cutebot" not in [s.skill_id for s in result]


@pytest.mark.asyncio
async def test_subject_tracked_across_intervening_followups():
    """A subject set on a concrete turn survives later pronoun-only
    follow-ups: "check the cutebot" → "do it" → "again" all resolve to
    the cutebot, not to the intermediate "do it"."""
    orch = _make_orchestrator()
    sid = "sess-chain"
    await orch._route_prompt("check the cutebot", session_id=sid)
    await orch._route_prompt("do it", session_id=sid)
    result = await orch._route_prompt("again", session_id=sid)
    assert "cutebot" in [s.skill_id for s in result]


@pytest.mark.asyncio
async def test_history_scan_skips_intervening_followup():
    """With no tracked subject (history-only path) the scan skips an
    underspecified follow-up and resolves to the concrete subject behind
    it."""
    orch = _make_orchestrator()
    sid = "sess-hist-chain"
    orch.conversation_history[sid] = [
        {"role": "user", "content": "check the cutebot"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "do it"},
        {"role": "assistant", "content": "done"},
    ]
    result = await orch._route_prompt("again", session_id=sid)
    assert "cutebot" in [s.skill_id for s in result]


@pytest.mark.asyncio
async def test_new_concrete_topic_overrides_active_subject():
    """A genuine new topic refreshes the active subject so a later
    follow-up resolves to the NEW subject, not a stale one."""
    orch = _make_orchestrator()
    sid = "sess-switch"
    await orch._route_prompt("check the cutebot", session_id=sid)
    await orch._route_prompt("play music", session_id=sid)
    result = await orch._route_prompt("do it again", session_id=sid)
    ids = [s.skill_id for s in result]
    assert "spotify_music" in ids
    assert "cutebot" not in ids


@pytest.mark.asyncio
async def test_new_topic_not_hijacked_by_prior_subject():
    """A concrete new request is never rewritten with a stale subject."""
    orch = _make_orchestrator()
    sid = "sess-newtopic"
    await orch._route_prompt("check the cutebot", session_id=sid)
    result = await orch._route_prompt("what's on my calendar", session_id=sid)
    ids = [s.skill_id for s in result]
    assert "calendar_google" in ids
    assert "cutebot" not in ids


@pytest.mark.asyncio
async def test_coref_resolution_fed_into_prompt_query():
    """The coref-resolved text routing used is reusable for the system
    prompt (so tool selection + prompts see the implied subject), and a
    concrete turn leaves the prompt query untouched."""
    orch = _make_orchestrator()
    sid = "sess-prompt"
    await orch._route_prompt("check the cutebot", session_id=sid)
    # Concrete turn: no coref override, prompt query == raw text.
    assert orch._coref_query_for_prompt(sid, "check the cutebot") == "check the cutebot"

    await orch._route_prompt("check now", session_id=sid)
    resolved = orch._coref_query_for_prompt(sid, "check now")
    assert resolved != "check now"
    assert resolved.startswith("check now (re:")
    assert "cutebot" in resolved.lower()


def test_is_underspecified_followup_classification():
    orch = _make_orchestrator()
    assert orch._is_underspecified_followup("check now") is True
    assert orch._is_underspecified_followup("do it again") is True
    assert orch._is_underspecified_followup("the same one") is True
    # Genuine new topics / long utterances are not follow-ups.
    assert orch._is_underspecified_followup("play music") is False
    assert orch._is_underspecified_followup("what's on my calendar today") is False
    assert orch._is_underspecified_followup("") is False


# ── P6: multi-turn routine setup (demo transcript regression) ──────


@pytest.mark.asyncio
async def test_multi_turn_routine_setup_carries_intent_to_clarification():
    """Exact demo-blocker flow: the user asks for a recurring task on one
    turn, the brain asks a clarifying question (during a live demo the
    brain dishonestly invented a "background task workaround"), and the
    user fills in the action on the next turn. The clarification
    follow-up has no recurring marker by itself — without carry-over,
    routing falls back to the action skill and the model fabricates a
    "workaround" because feral_routines only reaches it via the
    always-include fallback (appended last, easy to miss). With
    carry-over, the follow-up routes to feral_routines first so the
    model creates a real cron-backed routine.
    """
    orch = _make_orchestrator()
    sid = "demo-recurring"

    # Turn 1 — the user asks for a recurring task. The schedule is
    # explicit ("every night at 9") but the action is not.
    turn1 = (
        "I want you to make sure that there is a recurrent task for the "
        "robot to run every night at 9."
    )
    result1 = await orch._route_prompt(turn1, session_id=sid)
    ids1 = [s.skill_id for s in result1]
    assert ids1 and ids1[0] == "feral_routines", (
        f"Turn 1 should route feral_routines FIRST; got {ids1}"
    )

    # The brain logged Turn 1 + asked a clarifying question. Both turns
    # land in conversation_history before Turn 3 routes.
    orch.conversation_history[sid] = [
        {"role": "user", "content": turn1},
        {"role": "assistant", "content": "What exactly should the robot do at that time?"},
    ]

    # Turn 3 — the user fills in the action. No time marker; routing
    # would otherwise pick only the action skill (cutebot) and the LLM
    # would invent a "background task workaround" because feral_routines
    # would no longer be a primary suggestion.
    turn3 = "I just want you to make it spin."
    result3 = await orch._route_prompt(turn3, session_id=sid)
    ids3 = [s.skill_id for s in result3]
    assert ids3 and ids3[0] == "feral_routines", (
        f"Turn 3 (clarification follow-up) must still route feral_routines FIRST; got {ids3}"
    )


@pytest.mark.asyncio
async def test_pending_routine_intent_clears_after_create_succeeds():
    """Once an assistant turn dispatches `feral_routines__create`, the
    pending intent is consumed: subsequent unrelated action turns must
    NOT keep sticky-routing to feral_routines."""
    orch = _make_orchestrator()
    sid = "demo-recurring-done"
    orch.conversation_history[sid] = [
        {"role": "user", "content": "every night at 9, spin the robot"},
        {
            "role": "assistant",
            "content": "Scheduled.",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "feral_routines__create", "arguments": "{}"},
                }
            ],
        },
    ]
    # A subsequent unrelated action turn — the user moved on. Routing
    # must not keep prepending feral_routines.
    result = await orch._route_prompt("play music", session_id=sid)
    ids = [s.skill_id for s in result]
    assert ids and ids[0] != "feral_routines"
    assert "spotify_music" in ids


@pytest.mark.asyncio
async def test_pending_routine_intent_does_not_hijack_chitchat():
    """A short non-action follow-up after a routine ask (e.g. "thanks")
    must NOT carry the routine intent — the safeguard is the
    ``_query_implies_action`` gate inside the heuristic."""
    orch = _make_orchestrator()
    sid = "demo-recurring-chitchat"
    orch.conversation_history[sid] = [
        {"role": "user", "content": "every night at 9, spin the robot"},
        {"role": "assistant", "content": "What exactly should it do?"},
    ]
    # 1-word chit-chat ack with no action verb — query_implies_action is
    # False, so carry-over does not fire.
    result = await orch._route_prompt("thanks", session_id=sid)
    ids = [s.skill_id for s in result]
    if ids:
        assert ids[0] != "feral_routines"


@pytest.mark.asyncio
async def test_clarification_followup_e2e_create_persists():
    """End-to-end: the multi-turn carry-over routes feral_routines AND a
    direct call into the skill persists a real CronService job. Proves
    the chain "model picks feral_routines → tool dispatch → CronService
    job lives in the store after the call returns" works for the demo
    prompt's combined schedule + action."""
    import time

    import skills.impl.feral_routines as fr

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    svc = CronService(db_path=path)
    fr.set_scheduler_override(svc)
    try:
        skill = fr.FeralRoutinesSkill()
        # Mirror what the LLM would produce after both turns are
        # combined: a recurring routine that drives the cutebot.
        res = await skill.execute(
            "create",
            {
                "cron_expr": "every day at 9pm",
                "skill_id": "cutebot",
                "endpoint_id": "drive",
                "args": {"left": 60, "right": -60},
                "description": "spin the robot every night at 9 PM",
                "auto_confirm": True,
            },
            {},
        )
        assert res["success"] is True
        assert res["data"]["verified"] is True
        routine = res["data"]["routine"]
        # NL → canonical scheduler form.
        assert routine["cron_expr"] == "daily 21:00"
        assert routine["payload"]["skill"] == "cutebot"
        assert routine["payload"]["endpoint"] == "drive"
        assert routine["payload"]["auto_confirm"] is True
        # Persistence: re-reading the store sees the same job (this is the
        # "create verifies persistence" honesty contract).
        jobs = svc.list_jobs()
        assert any(j.id == routine["id"] for j in jobs)
        # Scheduler will fire it: next_run is a positive epoch in the
        # future, no further than ~24h out (matches "every day at 9pm").
        assert routine["next_run"] > time.time()
        assert routine["next_run"] - time.time() < 86400 + 60
    finally:
        fr.set_scheduler_override(None)
        svc.close()
        os.unlink(path)
