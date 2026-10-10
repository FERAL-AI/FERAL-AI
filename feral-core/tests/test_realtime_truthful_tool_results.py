"""Actual bound realtime callbacks preserve executor truth on inert wires."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from voice.gemini_realtime import GeminiRealtimeProxy, GeminiRealtimeSession
from voice.realtime_proxy import RealtimeProxy, RealtimeSession


class Wire:
    def __init__(self):
        self.frames = []

    async def send(self, raw):
        self.frames.append(json.loads(raw))


async def callback_to_wire(provider, result):
    proxy_cls, session_cls = (
        (RealtimeProxy, RealtimeSession) if provider == "openai"
        else (GeminiRealtimeProxy, GeminiRealtimeSession)
    )
    owner = session_cls("truthful-result-sid", "fixture-node", api_key="fixture")
    owner._connected = True
    owner._ws = Wire()
    proxy = proxy_cls.__new__(proxy_cls)
    proxy._memory = proxy._orchestrator = None
    proxy._sessions = {owner.session_id: owner}
    proxy._skill_registry = SimpleNamespace(skills={"fixture": SimpleNamespace(
        endpoints=[SimpleNamespace(id="read")],
    )})
    proxy._skill_executor = SimpleNamespace(execute=AsyncMock(return_value=result))
    proxy._send_tool_feedback = AsyncMock()
    proxy._record_voice_tool_episode = AsyncMock()
    owner._callback_guard = lambda: proxy._sessions.get(owner.session_id) is owner
    owner._on_tool_call = proxy._bind_callback(
        owner, proxy._handle_tool_call, response_scoped=True,
    )
    if provider == "openai":
        owner._active_response_id = "fixture-response"
        owner._response_in_progress = True
        await owner._handle_event({
            "type": "response.function_call_arguments.done",
            "response_id": "fixture-response", "call_id": "fixture-call",
            "name": "fixture__read", "arguments": "{}",
        })
        frame = owner._ws.frames[0]
        assert frame["item"]["type"] == "function_call_output"
        assert frame["item"]["call_id"] == "fixture-call"
        decoded = json.loads(frame["item"]["output"])
        assert owner._ws.frames[1] == {"type": "response.create"}
    else:
        await owner._handle_event({"toolCall": {"functionCalls": [{
            "id": "fixture-call", "name": "fixture__read", "args": {},
        }]}})
        response = owner._ws.frames[0]["toolResponse"]["functionResponses"][0]
        assert response["id"] == "fixture-call"
        assert response["name"] == "fixture__read"
        decoded = response["response"]
    proxy._skill_executor.execute.assert_awaited_once()
    assert not owner._callback_tasks
    return decoded


@pytest.mark.parametrize("provider", ["openai", "gemini"])
@pytest.mark.parametrize("data", [None, False, 0, "", [], {}])
async def test_preserve_falsy_data_and_canonical_envelope(provider, data):
    result = {"success": True, "status_code": 200, "data": data, "error": None}
    original = copy.deepcopy(result)
    assert await callback_to_wire(provider, result) == result
    assert result == original


@pytest.mark.parametrize("provider", ["openai", "gemini"])
@pytest.mark.parametrize("result", [
    {"success": False, "status_code": 503, "data": {"detail": "partial"},
     "error": "Unavailable", "outcome": "outcome_unknown"},
    {"success": False, "status_code": 202, "data": None, "error": None,
     "status": "pending_approval", "approval_request_id": "fixture-approval"},
    {"success": True, "status_code": 200, "data": {"items": [1]}, "error": None,
     "_truncated": True, "_truncation_note": "Remaining items omitted",
     "_pagination_hint": {"next_offset": 1}},
])
async def test_preserve_failure_approval_and_truncation_metadata(provider, result):
    assert await callback_to_wire(provider, result) == result


@pytest.mark.parametrize("provider", ["openai", "gemini"])
async def test_oversized_failure_keeps_receipt_and_marks_omission(provider):
    from voice.tool_result_envelope import MAX_REALTIME_RESULT_CHARS
    result = {"success": False, "status_code": 503, "error": "Unavailable",
              "outcome": "outcome_unknown", "data": {"body": "x" * 100_000},
              "_truncated": True, "_truncation_note": "Merchant returned partial data"}
    output = await callback_to_wire(provider, result)
    assert output["success"] is False and output["status_code"] == 503
    assert output["error"] == "Unavailable" and output["outcome"] == "outcome_unknown"
    assert output["_truncated"] is True and "data" in output
    assert output["_upstream_truncation_note"] == "Merchant returned partial data"
    assert len(json.dumps(output)) <= MAX_REALTIME_RESULT_CHARS


@pytest.mark.parametrize("provider", ["openai", "gemini"])
async def test_credential_and_card_fields_do_not_reach_provider(provider, caplog):
    credential = "sk-" + "fixture" * 8
    result = {"success": False, "status_code": 503, "error": "Retry withheld",
              "data": {"api_key": credential, "nested": {"card_number": "4242424242424242"},
                       "detail": "Authorization: Bearer fixture-token; card 4242 4242 4242 4242"}}
    output = await callback_to_wire(provider, result)
    encoded = json.dumps(output)
    assert credential not in encoded and "4242" not in encoded and "fixture-token" not in encoded
    assert output["success"] is False and output["error"] == "Retry withheld"
    assert credential not in caplog.text and "4242" not in caplog.text


def test_large_width_and_cycles_are_bounded_without_losing_receipt():
    from voice.tool_result_envelope import MAX_REALTIME_RESULT_CHARS, serialize_realtime_tool_result
    result = {f"extra-{index}": "x" * 1_000 for index in range(1_000)}
    result.update(success=False, status_code=409, error="Uncertain", outcome="outcome_unknown", data=False,
                  error_code="outcome_unknown", request_id="fixture-request",
                  tool_name="fixture__read", session_id="fixture-session")
    result["cycle"] = result
    encoded = serialize_realtime_tool_result("fixture__read", result)
    output = json.loads(encoded)
    assert len(encoded) <= MAX_REALTIME_RESULT_CHARS
    assert output["success"] is False and output["error"] == "Uncertain"
    assert output["outcome"] == "outcome_unknown" and output["data"] is False
    assert output["error_code"] == "outcome_unknown"
    assert output["request_id"] == "fixture-request" and output["session_id"] == "fixture-session"
    assert output["tool_name"] == "fixture__read"
    assert output["_truncated"] is True


def test_untrusted_object_repr_and_nonfinite_values_are_withheld():
    from voice.tool_result_envelope import serialize_realtime_tool_result
    class PrivateObject:
        def __str__(self):
            raise AssertionError("Private repr must not be evaluated")
    output = json.loads(serialize_realtime_tool_result("fixture__read", {
        "success": True, "error": None, "data": [PrivateObject(), float("nan")],
    }))
    assert output["success"] is True and output["_truncated"] is True
    assert len(output["data"]) == 2


def test_known_pan_crossing_visible_cut_is_redacted():
    from voice.tool_result_envelope import serialize_realtime_tool_result
    encoded = serialize_realtime_tool_result("fixture__read", {
        "success": True, "data": "x" * 1_990 + " 4242424242424242", "error": None,
    })
    assert "4242" not in encoded


def test_non_envelope_is_unknown_instead_of_inferred_success():
    from voice.tool_result_envelope import serialize_realtime_tool_result
    output = json.loads(serialize_realtime_tool_result("fixture__read", False))
    assert output["success"] is False and output["outcome"] == "outcome_unknown"


def test_large_image_is_explicitly_omitted_from_text_result():
    from voice.tool_result_envelope import serialize_realtime_tool_result
    output = json.loads(serialize_realtime_tool_result("fixture__read", {
        "success": True, "data": {"image_base64": "A" * 100_000}, "error": None,
    }))
    assert output["_image_omitted"] is True
    assert "A" * 512 not in json.dumps(output)


def test_tiny_payload_budget_cannot_erase_failure_receipt(monkeypatch):
    from skills.result_budget import ResultBudget
    from voice import tool_result_envelope
    monkeypatch.setattr(tool_result_envelope, "budget_for_tool", lambda *_: ResultBudget(
        "tiny", max_depth=1, max_str_len=1, max_list_len=1,
        max_dict_keys=1, max_result_chars=64,
    ))
    output = json.loads(tool_result_envelope.serialize_realtime_tool_result("fixture__read", {
        "success": False, "status_code": 409, "error": "Uncertain",
        "status": "outcome_unknown", "data": False,
    }))
    assert output["success"] is False and output["status_code"] == 409
    assert output["error"] == "Uncertain" and output["status"] == "outcome_unknown"
    assert output["data"] is False and output["_truncated"] is True


@pytest.mark.parametrize("data", [None, False, 0, "", [], {}])
def test_falsy_data_survives_pathological_metadata(data):
    from voice.tool_result_envelope import serialize_realtime_tool_result
    result = {"success": False, "status_code": 409, "error": {"details": [
        {"items": ["x" * 1_000] * 50} for _ in range(100)
    ]}, "data": data}
    output = json.loads(serialize_realtime_tool_result("fixture__read", result))
    assert output["success"] is False and output["data"] == data
    assert type(output["data"]) is type(data)
