from types import SimpleNamespace
import pytest
from bridges.acp import AcpAgentProcess, parse_session_update


def setup(events):
    process = AcpAgentProcess.__new__(AcpAgentProcess)
    session = SimpleNamespace(session_id="s", transcript=events)
    process.sessions = {"s": session}
    params = {"sessionId": "s", "toolCall": {"toolCallId": "c", "kind": "read", "rawInput": {}, "locations": []}}
    return process, params


def event(**extra):
    return parse_session_update("s", {"sessionUpdate": "tool_call", "toolCallId": "c", "kind": "read", "status": "in_progress", "rawInput": {"filePath": "/tmp/project/hello.txt"}, "locations": [{"path": "/tmp/project/hello.txt"}], **extra})


def test_exact_live_event_supplies_missing_read_scope_without_mutation():
    process, params = setup([event()])
    enriched = process._permission_context(params)
    assert enriched["toolCall"]["rawInput"] == {"filePath": "/tmp/project/hello.txt"}
    assert enriched["toolCall"]["review_scope_source"] == "same_session_live_tool_call"
    assert params["toolCall"]["rawInput"] == {}


@pytest.mark.parametrize("events", [[], [event(toolCallId="foreign")], [event(status="completed")], [event(kind="execute")], [event(rawInput={})], [event(), event(rawInput={"filePath": "/tmp/other.txt"})]])
def test_missing_foreign_terminal_or_changing_scope_never_enriched(events):
    process, params = setup(events)
    assert process._permission_context(params) is params


def test_different_session_cannot_supply_scope():
    foreign = event()
    foreign.session_id = "foreign"
    process, params = setup([foreign])
    assert process._permission_context(params) is params


@pytest.mark.parametrize("session_id", [None, "", False, 0, 1.5, [], {}, "foreign", " "])
def test_invalid_or_unknown_session_id_never_supplies_scope(session_id):
    process, params = setup([event()])
    params["sessionId"] = session_id
    assert process._permission_context(params) is params
    assert params["toolCall"]["rawInput"] == {}


def test_missing_session_id_never_supplies_scope():
    process, params = setup([event()])
    del params["sessionId"]
    assert process._permission_context(params) is params
    assert params["toolCall"]["rawInput"] == {}


def test_changing_same_call_locations_never_supplies_scope():
    process, params = setup([
        event(),
        event(locations=[{"path": "/tmp/other.txt"}]),
    ])
    assert process._permission_context(params) is params
    assert params["toolCall"]["locations"] == []


def test_conflicting_request_locations_never_supplies_scope():
    process, params = setup([event()])
    params["toolCall"]["locations"] = [{"path": "/tmp/other.txt"}]
    assert process._permission_context(params) is params
    assert params["toolCall"]["rawInput"] == {}
