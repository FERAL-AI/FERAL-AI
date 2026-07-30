"""
Tests for agents.refusal_handler — refusal detection, action-intent building,
result summarization, and async fallback execution (mocked orchestrator).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.refusal_handler import RefusalHandler
from agents.tool_runner import SafetyLevel


# ── Initialization & detection ───────────────────────────────────────────────


def test_refusal_handler_stores_orchestrator():
    orch = MagicMock()
    h = RefusalHandler(orch)
    assert h._orch is orch


def test_is_refusal_empty_and_non_refusal():
    h = RefusalHandler(MagicMock())
    assert h.is_refusal("") is False
    assert h.is_refusal("   ") is False
    assert h.is_refusal("Here is the answer you wanted.") is False


def test_is_refusal_detects_phrases_and_normalizes_whitespace():
    h = RefusalHandler(MagicMock())
    assert h.is_refusal("I can't do that for you.") is True
    assert h.is_refusal("Unfortunately I am unable to help.") is True
    assert h.is_refusal("Not   possible\ntoday.") is True


def test_query_implies_action_long_text():
    h = RefusalHandler(MagicMock())
    long_q = "please tell me something interesting about the weather today"
    assert h.query_implies_action(long_q) is True


def test_query_implies_action_keywords():
    h = RefusalHandler(MagicMock())
    assert h.query_implies_action("find my keys") is True
    assert h.query_implies_action("hi there") is False


def test_action_text_is_destructive():
    h = RefusalHandler(MagicMock())
    assert h.action_text_is_destructive("delete all files") is True
    assert h.action_text_is_destructive("open Safari") is False


def test_extract_first_url_strips_trailing_punctuation():
    assert RefusalHandler.extract_first_url('Visit https://ex.com/path).') == "https://ex.com/path"
    assert RefusalHandler.extract_first_url("no url here") == ""


def test_extract_open_app_name_common_map():
    h = RefusalHandler(MagicMock())
    assert h.extract_open_app_name("Please open spotify") == "Spotify"
    assert h.extract_open_app_name("launch chrome now") == "Google Chrome"


def test_extract_open_app_name_generic_phrase():
    h = RefusalHandler(MagicMock())
    assert h.extract_open_app_name("Open my cool app") == "My Cool App"


def test_capability_key_normalizes():
    assert RefusalHandler.capability_key("Hello!!! World---99") == "hello world 99"


# ── build_action_intent_tool_call ───────────────────────────────────────────


def test_build_action_intent_prefers_open_app():
    h = RefusalHandler(MagicMock())
    tc = h.build_action_intent_tool_call("open Terminal please")
    assert tc is not None
    assert tc["name"] == "desktop_control__open_app"
    assert "Terminal" in tc["args"]["script"]


def test_build_action_intent_url():
    h = RefusalHandler(MagicMock())
    tc = h.build_action_intent_tool_call('go to https://example.com/foo)')
    assert tc is not None
    assert tc["name"] == "desktop_control__shell_command"
    assert "https://example.com/foo" in tc["args"]["command"]


def test_build_action_intent_desktop_note():
    h = RefusalHandler(MagicMock())
    tc = h.build_action_intent_tool_call(
        "create a desktop note with content: meeting notes",
    )
    assert tc is not None
    assert tc["name"] == "coding_tools__bash"
    assert "feral_note.txt" in tc["args"]["command"]


def test_build_action_intent_execute_task_fallback():
    h = RefusalHandler(MagicMock())
    tc = h.build_action_intent_tool_call("run the installer")
    assert tc is not None
    assert tc["name"] == "agentic_computer_use__execute_task"
    assert tc["args"]["task"] == "run the installer"


def test_build_action_intent_returns_none_when_no_action():
    h = RefusalHandler(MagicMock())
    assert h.build_action_intent_tool_call("ok") is None


# ── summarize_action_result ─────────────────────────────────────────────────


def test_summarize_action_result_non_dict():
    h = RefusalHandler(MagicMock())
    assert "foo_tool" in h.summarize_action_result({"name": "foo_tool"}, "not a dict")


def test_summarize_action_result_hardware_daemon():
    h = RefusalHandler(MagicMock())
    out = h.summarize_action_result(
        {"name": "x"},
        {"status": "command_sent_to_hardware_daemon"},
    )
    assert "device daemon" in out.lower()


def test_summarize_action_result_success_with_stdout():
    h = RefusalHandler(MagicMock())
    out = h.summarize_action_result(
        {"name": "t"},
        {"success": True, "data": {"stdout": "hello world"}},
    )
    assert "hello world" in out


def test_summarize_action_result_failure():
    h = RefusalHandler(MagicMock())
    out = h.summarize_action_result(
        {"name": "t"},
        {"success": False, "error": "boom"},
    )
    assert "boom" in out


# ── execute_action_intent_fallback (async) ──────────────────────────────────


@pytest.mark.asyncio
async def test_execute_fallback_returns_false_when_no_intent():
    orch = MagicMock()
    h = RefusalHandler(orch)
    assert await h.execute_action_intent_fallback("s1", "maybe", []) is False
    orch.tool_runner.execute_tool_call_for_llm.assert_not_called()


@pytest.mark.asyncio
async def test_execute_fallback_denied_safety_sends_error():
    orch = MagicMock()
    orch.tool_runner.classify_safety.return_value = SafetyLevel.DENY
    orch.tool_runner.enforce_safety.return_value = {"error": "policy block"}
    orch._send_text = AsyncMock()
    h = RefusalHandler(orch)

    ok = await h.execute_action_intent_fallback("s1", "open safari", [])
    assert ok is True
    orch._send_text.assert_awaited_once_with("s1", "policy block")


@pytest.mark.asyncio
async def test_execute_fallback_confirm_queues_confirmation():
    orch = MagicMock()
    orch.tool_runner.classify_safety.return_value = SafetyLevel.CONFIRM
    orch._maybe_auto_expand_capability = AsyncMock()
    orch._queue_action_confirmation = AsyncMock()
    h = RefusalHandler(orch)

    ok = await h.execute_action_intent_fallback("s1", "open music", [])
    assert ok is True
    orch._queue_action_confirmation.assert_awaited_once()
    orch._maybe_auto_expand_capability.assert_awaited()


@pytest.mark.asyncio
async def test_execute_fallback_auto_executes_and_pushes_memory():
    orch = MagicMock()
    orch.tool_runner.classify_safety.return_value = SafetyLevel.AUTO
    orch.tool_runner.execute_tool_call_for_llm = AsyncMock(
        return_value={"success": True, "data": {"note": "saved"}},
    )
    orch._try_genui_for_result = AsyncMock()
    orch._send_text = AsyncMock()
    orch.memory = MagicMock()
    orch._maybe_auto_expand_capability = AsyncMock()
    h = RefusalHandler(orch)

    ok = await h.execute_action_intent_fallback("s1", "open notes", [])
    assert ok is True
    orch.tool_runner.execute_tool_call_for_llm.assert_awaited_once()
    orch._send_text.assert_awaited()
    orch.memory.working_push.assert_called_once()
    orch._maybe_auto_expand_capability.assert_awaited()


@pytest.mark.asyncio
async def test_execute_fallback_destructive_bumps_auto_to_confirm():
    """URL open uses shell_command (AUTO); destructive wording upgrades to CONFIRM."""
    orch = MagicMock()
    orch.tool_runner.classify_safety.return_value = SafetyLevel.AUTO
    orch._maybe_auto_expand_capability = AsyncMock()
    orch._queue_action_confirmation = AsyncMock()
    h = RefusalHandler(orch)

    await h.execute_action_intent_fallback(
        "s1",
        "delete https://example.com/path",
        [],
    )
    orch._queue_action_confirmation.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_fallback_denied_without_enforce_message():
    orch = MagicMock()
    orch.tool_runner.classify_safety.return_value = SafetyLevel.DENY
    orch.tool_runner.enforce_safety.return_value = None
    orch._send_text = AsyncMock()
    h = RefusalHandler(orch)

    await h.execute_action_intent_fallback("s1", "open safari", [])
    orch._send_text.assert_awaited_once_with("s1", "This action is blocked by safety policy.")


def test_is_refusal_detects_cant_create_phrase():
    h = RefusalHandler(MagicMock())
    assert h.is_refusal("I can't create that file.") is True


def test_query_implies_action_six_words_without_action_keyword():
    h = RefusalHandler(MagicMock())
    assert h.query_implies_action("one two three four five six") is True
    assert h.query_implies_action("one two three four five") is False


def test_capability_key_empty_string():
    assert RefusalHandler.capability_key("") == ""


def test_extract_open_app_name_empty_and_no_verb():
    h = RefusalHandler(MagicMock())
    assert h.extract_open_app_name("") == ""
    assert h.extract_open_app_name("just chatting") == ""


def test_build_action_intent_desktop_note_uses_default_content_when_no_clause():
    h = RefusalHandler(MagicMock())
    tc = h.build_action_intent_tool_call("make a desktop note file please")
    assert tc is not None
    assert tc["name"] == "coding_tools__bash"
    assert "hello world" in tc["args"]["command"]


def test_summarize_action_result_success_with_note_only():
    h = RefusalHandler(MagicMock())
    out = h.summarize_action_result(
        {"name": "t"},
        {"success": True, "data": {"note": "Note saved"}},
    )
    assert "Note saved" in out


def test_summarize_action_result_success_plain_when_data_not_structured():
    h = RefusalHandler(MagicMock())
    out = h.summarize_action_result(
        {"name": "my_tool"},
        {"success": True, "data": "plain string"},
    )
    assert "my_tool" in out.lower() and "successfully" in out.lower()


def test_summarize_action_result_failure_prefers_note_over_generic():
    h = RefusalHandler(MagicMock())
    out = h.summarize_action_result(
        {"name": "t"},
        {"success": False, "note": "from note field"},
    )
    assert "from note field" in out


@pytest.mark.asyncio
async def test_execute_fallback_auto_without_memory_still_sends_summary():
    orch = MagicMock()
    orch.tool_runner.classify_safety.return_value = SafetyLevel.AUTO
    orch.tool_runner.execute_tool_call_for_llm = AsyncMock(
        return_value={"success": True, "data": {}},
    )
    orch._try_genui_for_result = AsyncMock()
    orch._send_text = AsyncMock()
    orch.memory = None
    orch._maybe_auto_expand_capability = AsyncMock()
    h = RefusalHandler(orch)

    ok = await h.execute_action_intent_fallback("s1", "open notes", [])
    assert ok is True
    orch._send_text.assert_awaited()
    assert orch.memory is None
