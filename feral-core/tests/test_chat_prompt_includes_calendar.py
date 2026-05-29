"""Audit-r9 regression test: calendar events injected into chat prompt.

Operator report 2026-05-10: "I created an event on the FERAL webUI
locally and then I asked the chat on the iOS app but it has no idea."

Audit-r9 root cause (subagent #cd995a59 + #7f34513e + #b5298a37):
1. Web chat mints `uuid4()` per WebSocket; phone chat uses
   `phone-{node_id}`. Conversation history + working memory are
   PARTITIONED by `session_id`.
2. `IdentityLoader.build_system_prompt` did NOT inject calendar /
   reminder data. The LLM only learned about events when the routing
   layer happened to add `calendar_google` to active skills AND the
   model decided to call a lookup tool.

Fix: `Orchestrator.set_calendar` wires `state.calendar` into the
identity loader; `_build_events_section` now renders a
`## Today's Events` block on every prompt build. This test pins the
contract — without it a future refactor that drops the wiring would
silently regress to "iOS has no idea."
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agents.identity_loader import IdentityLoader


class _FakeCalendar:
    """Minimal stand-in for `CalendarIntegration`.

    `execute("list_events", ...)` returns the audited shape:
    `{"success": True, "data": {"events": [...]}}`. The identity
    loader must read THAT shape (not the legacy `{"events": [...]}`),
    same shape mismatch the audit caught in
    `api/routes/timeline.py:62`.
    """

    def __init__(self, events):
        self._events = events

    def execute(self, op, params):
        if op == "list_events":
            return {"success": True, "data": {"events": list(self._events)}}
        return {"success": False, "data": {}}


async def _build_prompt(loader: IdentityLoader, *, query: str = "") -> str:
    from perception.fusion import PerceptionFrame

    frame = PerceptionFrame()
    return await loader.build_system_prompt(
        session_id="test-session",
        frame=frame,
        skills=[],
        full_catalog=[],
        query=query,
    )


async def test_calendar_events_appear_in_system_prompt():
    cal = _FakeCalendar(events=[
        {
            "title": "Standup",
            "start": "2026-05-11T09:00:00Z",
            "location": "Zoom",
        },
        {
            "summary": "Lunch with Alpay",
            "when": "2026-05-11T12:30:00Z",
        },
    ])
    loader = IdentityLoader(memory=None, calendar=cal)
    prompt = await _build_prompt(loader)
    assert "## Today's Events" in prompt, (
        "IdentityLoader must inject `## Today's Events` block when "
        "calendar handle is wired. Audit-r9 root cause: this block "
        "missing meant iOS chat couldn't answer about web-created "
        "events. See `Orchestrator.set_calendar`."
    )
    assert "Standup" in prompt
    assert "Lunch with Alpay" in prompt
    assert "Zoom" in prompt
    assert "2026-05-11T09:00:00Z" in prompt


async def test_calendar_legacy_response_shape_still_works():
    """Older calendar stubs returned bare `{"events": [...]}`. Loader
    must tolerate both — same defensive read pattern used in the
    timeline route fix."""
    class _LegacyCal:
        def execute(self, op, params):
            return {"events": [{"title": "Legacy event", "start": "now"}]}

    loader = IdentityLoader(memory=None, calendar=_LegacyCal())
    prompt = await _build_prompt(loader)
    assert "Legacy event" in prompt


async def test_no_calendar_section_when_calendar_absent():
    loader = IdentityLoader(memory=None, calendar=None)
    prompt = await _build_prompt(loader)
    assert "## Today's Events" not in prompt


async def test_calendar_failure_does_not_break_prompt_build():
    """A calendar OAuth glitch must NEVER block chat. Loader swallows
    the error and returns the rest of the prompt."""
    class _BoomCal:
        def execute(self, op, params):
            raise RuntimeError("OAuth token expired")

    loader = IdentityLoader(memory=None, calendar=_BoomCal())
    prompt = await _build_prompt(loader)
    # Prompt still built; just no calendar section. The agents-prompt
    # deepening pass restructured the static header — the opening
    # fixed block is now "## Tool-Selection Discipline" (replaced the
    # earlier "## Execution Truthfulness" header). Assert against the
    # new sentinel; the operator-facing contract (build doesn't crash
    # on a calendar OAuth glitch) is unchanged.
    assert "## Tool-Selection Discipline" in prompt
    assert "## Today's Events" not in prompt  # rendered section header absent


def test_orchestrator_set_calendar_threads_to_identity_loader():
    """End-to-end wiring: `Orchestrator.set_calendar` must populate
    `identity_loader.calendar`. Pinned because a future refactor that
    splits orchestrator construction could silently drop the link."""
    from agents.orchestrator import Orchestrator

    skills = MagicMock()
    daemons = {}
    async def _send(_session, _msg): return None

    orch = Orchestrator(
        skill_registry=skills,
        send_to_client=_send,
        daemons=daemons,
        memory=None,
    )
    cal = _FakeCalendar(events=[{"title": "Sync", "start": "now"}])
    assert orch.identity_loader.calendar is None  # baseline
    orch.set_calendar(cal)
    assert orch.identity_loader.calendar is cal


async def test_async_caller_falls_back_to_cached_next_event():
    """When the prompt builder is invoked from an async context (the
    real brain — orchestrator runs inside an asyncio task), it cannot
    `await` a coroutine. Loader must close the coroutine and use the
    `_cached_next_event` snapshot if present."""
    class _AsyncCal:
        _cached_next_event = {"title": "Cached event", "start": "soon"}

        def execute(self, op, params):
            async def _go():
                return {"data": {"events": []}}
            return _go()

    loader = IdentityLoader(memory=None, calendar=_AsyncCal())

    prompt = await _build_prompt(loader)
    assert "Cached event" in prompt, (
        "Async-caller path: when execute() is a coroutine and a loop "
        "is running, the loader should fall back to "
        "`calendar._cached_next_event`. Without this iOS prompts "
        "(which build inside an async chat handler) miss live calendar "
        "data."
    )
