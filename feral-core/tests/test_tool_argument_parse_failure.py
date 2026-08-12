"""A tool call whose arguments do not parse must say so, not run empty.

Live evidence (``~/.feral/memory.db``, execution_log, read 2026-08-12):

    web_search / web_search   61 executions, 61 failures, 0 successes
    every row's args column    '{}'
    every row's error          "Missing search query. Provide 'query' or
                                'q' parameter."
    all 61 on                  2026-05-15
    anti-loop guard fired at   streaks of 5, 6 and 7

The same shape elsewhere on other days: ``computer_use__bash`` 7 of 8
calls with empty args (2026-05-12), ``computer_use__write_file`` 11 of 19
(2026-05-21), ``desktop_control__shell_command`` 6 of 14 (2026-05-21).

Four sites in ``agents/llm_provider.py`` did
``json.loads(...) except: args = {}`` with no log line, so a truncated or
malformed arguments blob became a valid-looking argument-free call.
"""

from __future__ import annotations

import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agents.llm_provider import (  # noqa: E402
    LLMProvider,
    _finalise_tool_call,
    parse_tool_arguments,
)
from agents.tool_runner import ToolRunner  # noqa: E402


# ---------------------------------------------------------------------------
# The parse itself
# ---------------------------------------------------------------------------

def test_valid_arguments_parse():
    args, err = parse_tool_arguments('{"query": "cats"}', "web_search__web_search")
    assert args == {"query": "cats"}
    assert err == ""


def test_absent_arguments_are_a_legitimate_empty_call():
    """A no-argument tool is normal and must not be flagged."""
    for raw in ("", "   ", None):
        args, err = parse_tool_arguments(raw, "self_introspection__list_capabilities")
        assert args == {}
        assert err == ""


def test_truncated_arguments_report_an_error():
    """This is the 61-row case: the model's arguments were cut mid-string."""
    args, err = parse_tool_arguments('{"query": "how do I ', "web_search__web_search")
    assert args == {}
    assert err, "a truncated arguments blob must not read as an empty call"


def test_non_object_arguments_report_an_error():
    args, err = parse_tool_arguments('"just a string"', "web_search__web_search")
    assert args == {}
    assert "not an object" in err


def test_a_parse_failure_is_logged(caplog):
    with caplog.at_level("WARNING", logger="feral.llm"):
        parse_tool_arguments('{"query": ', "web_search__web_search")
    assert "web_search__web_search" in caplog.text
    assert "did not parse" in caplog.text


# ---------------------------------------------------------------------------
# The error survives the provider shapes
# ---------------------------------------------------------------------------

def test_responses_api_finaliser_carries_the_error():
    out = _finalise_tool_call({
        "call_id": "call_1",
        "item_id": "fc_1",
        "name": "web_search__web_search",
        "arguments": '{"query": "unterminat',
    })
    assert out["args"] == {}
    assert out["args_error"]


def test_chat_completions_extract_response_carries_the_error():
    provider = LLMProvider()
    _text, tools = provider.extract_response({
        "choices": [{
            "message": {
                "content": "",
                "tool_calls": [{
                    "id": "call_1",
                    "function": {
                        "name": "web_search__web_search",
                        "arguments": '{"query": "unterminat',
                    },
                }],
            },
        }],
    })
    assert tools[0]["args"] == {}
    assert tools[0]["args_error"]


def test_a_good_call_carries_no_error():
    provider = LLMProvider()
    _text, tools = provider.extract_response({
        "choices": [{
            "message": {
                "content": "",
                "tool_calls": [{
                    "id": "call_1",
                    "function": {
                        "name": "web_search__web_search",
                        "arguments": '{"query": "cats"}',
                    },
                }],
            },
        }],
    })
    assert tools[0]["args"] == {"query": "cats"}
    assert tools[0]["args_error"] == ""


# ---------------------------------------------------------------------------
# Dispatch refuses instead of running the tool with nothing
# ---------------------------------------------------------------------------

def _runner() -> ToolRunner:
    orch = types.SimpleNamespace(
        _mcp_client=None,
        skills=types.SimpleNamespace(skills={}),
        executor=None,
    )
    return ToolRunner(orch)


def test_dispatch_refuses_an_unparsable_call_without_executing(monkeypatch):
    runner = _runner()
    executed: list[str] = []

    def _boom(*a, **kw):
        executed.append("dispatched")
        return None

    monkeypatch.setattr(runner, "enforce_plan_mode", lambda *a, **kw: None)
    monkeypatch.setattr(runner, "enforce_safety", _boom)

    result = asyncio.run(runner._execute_tool_call_for_llm_inner(
        "session-1",
        {
            "name": "web_search__web_search",
            "args": {},
            "id": "call_1",
            "args_error": "arguments were not valid JSON (unterminated string)",
        },
        [],
        effective_surface="websocket",
    ))

    assert executed == [], "an unparsable call must not reach the safety gate or the skill"
    assert result["success"] is False
    assert result["error_code"] == "unparsable_arguments"
    assert "Nothing was executed" in result["reason"]
    assert "web_search__web_search" in result["reason"]


def test_a_well_formed_call_is_not_refused(monkeypatch):
    """The guard must not fire on the normal path."""
    runner = _runner()
    reached: list[str] = []

    monkeypatch.setattr(runner, "enforce_plan_mode", lambda *a, **kw: None)

    def _gate(*a, **kw):
        reached.append("gate")
        return None

    monkeypatch.setattr(runner, "enforce_safety", _gate)
    monkeypatch.setattr(runner, "register_tool_attempt", lambda *a, **kw: 1)

    result = asyncio.run(runner._execute_tool_call_for_llm_inner(
        "session-1",
        {"name": "web_search__web_search", "args": {"query": "cats"}, "id": "call_1"},
        [],
        effective_surface="websocket",
    ))
    assert reached == ["gate"]
    # No skill registered in the stub, so this is the expected outcome.
    assert "Skill not found" in str(result)
