"""Attribution links travel on the reply frame that carries the answer.

places__find_places returns per-place Google Maps sources, but the phone
only ever sees chat_response, whose payload had no structured field. So
the links had no way to reach the app, and Grounding Lite's terms are not
met by speech alone: grounded output may reach an end user only when its
sources are viewable within the same interaction, and these glasses have
no screen.

chat_response now carries an optional `sources`, mirroring how `somatic`
was added. Older clients ignore it.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import api.server as srv
from agents.tool_runner import ToolRunner
from tests.test_daemon_session_phone_branches import _mock_state_with_supervisor
from tests.test_hup_protocol import _TEST_NODE_KEY, _node_client, _register_node

pytestmark = pytest.mark.no_auto_feral_home

SESSION = "s-grounded"


def _runner() -> ToolRunner:
    return ToolRunner.__new__(ToolRunner)


def _places_result(*pairs, attributed=True):
    return {"data": {
        "attribution_required": attributed,
        "places": [{"name": t, "sources": [{"title": t, "url": u}]} for t, u in pairs],
    }}


class TestCapture:
    def test_sources_are_kept_from_a_grounded_result(self):
        runner = _runner()
        runner._record_grounding_sources(SESSION, _places_result(
            ("Nero", "https://maps.google.com/?cid=1"),
            ("Tim Hortons", "https://maps.google.com/?cid=2"),
        ))
        assert runner.pop_grounding_sources(SESSION) == [
            {"title": "Nero", "url": "https://maps.google.com/?cid=1"},
            {"title": "Tim Hortons", "url": "https://maps.google.com/?cid=2"},
        ]

    def test_a_result_that_does_not_require_attribution_is_ignored(self):
        """Keyed off the result saying so, not off a tool-name allowlist."""
        runner = _runner()
        runner._record_grounding_sources(SESSION, _places_result(
            ("Nero", "https://maps.google.com/?cid=1"), attributed=False))
        assert runner.pop_grounding_sources(SESSION) == []

    def test_top_level_sources_are_taken_too(self):
        runner = _runner()
        runner._record_grounding_sources(SESSION, {
            "attribution_required": True,
            "sources": [{"title": "Maps", "url": "https://maps.google.com/?cid=9"}],
        })
        assert len(runner.pop_grounding_sources(SESSION)) == 1

    def test_the_same_link_is_not_repeated(self):
        runner = _runner()
        same = ("Nero", "https://maps.google.com/?cid=1")
        runner._record_grounding_sources(SESSION, _places_result(same, same))
        assert len(runner.pop_grounding_sources(SESSION)) == 1

    def test_the_list_is_capped(self):
        runner = _runner()
        runner._record_grounding_sources(SESSION, _places_result(
            *[(f"p{i}", f"https://maps.google.com/?cid={i}") for i in range(40)]))
        assert len(runner.pop_grounding_sources(SESSION)) == ToolRunner._GROUNDING_SOURCE_CAP

    def test_reading_clears_them_so_the_next_answer_is_not_attributed(self):
        runner = _runner()
        runner._record_grounding_sources(SESSION, _places_result(
            ("Nero", "https://maps.google.com/?cid=1")))
        assert runner.pop_grounding_sources(SESSION)
        assert runner.pop_grounding_sources(SESSION) == []

    def test_sessions_do_not_share(self):
        runner = _runner()
        runner._record_grounding_sources("a", _places_result(
            ("Nero", "https://maps.google.com/?cid=1")))
        assert runner.pop_grounding_sources("b") == []


def _chat(mock):
    mock.orchestrator.handle_command = AsyncMock(return_value={"text": "Nero, 380 m away."})
    with _node_client(mock) as client:
        with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
            _register_node(ws, node_id="phone-grounded", node_type="phone")
            ws.send_json({"type": "chat_request", "hup_version": "1.3.0", "ts": 1.0,
                          "payload": {"session_id": SESSION, "text": "cafe near me",
                                      "reply_mode": "final", "channel": "chat",
                                      "reply_to": None}})
            reply = ws.receive_json()
    assert reply["type"] == "chat_response"
    return reply["payload"]


def test_the_reply_frame_carries_the_sources():
    mock = _mock_state_with_supervisor()
    mock.orchestrator.tool_runner.pop_grounding_sources = MagicMock(return_value=[
        {"title": "Nero", "url": "https://maps.google.com/?cid=1"},
    ])
    payload = _chat(mock)
    assert payload["sources"] == [{"title": "Nero", "url": "https://maps.google.com/?cid=1"}]


def test_an_ungrounded_answer_has_no_sources_field_at_all():
    """Absent, not empty: the phone must be able to tell them apart."""
    mock = _mock_state_with_supervisor()
    mock.orchestrator.tool_runner.pop_grounding_sources = MagicMock(return_value=[])
    assert "sources" not in _chat(mock)


def test_a_runner_without_the_hook_does_not_break_the_reply():
    """Older orchestrators, and the test doubles that mimic them."""
    mock = _mock_state_with_supervisor()
    del mock.orchestrator.tool_runner.pop_grounding_sources
    payload = _chat(mock)
    assert payload["text"] == "Nero, 380 m away."
    assert "sources" not in payload
