"""Real model loops close no-effect selected-Chrome traces on the same session."""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.taskflow_browser_consent import REASON, structural_checkpoint
from api.routes.taskflows import router as task_router
from api.state import state
from models.skill_manifest import BrandProfile, EndpointParam, SkillEndpoint, SkillManifest
from skills.impl import SKILL_IMPLEMENTATIONS
from tests import test_job_chrome_renewal as renewal
from tests.test_job_chrome_grants import grant, http, page_packets

pytestmark = [pytest.mark.asyncio, pytest.mark.no_auto_feral_home, pytest.mark.timeout(15)]
wired = renewal.wired
chrome_fixture = renewal.chrome_fixture
phone = renewal.phone


def configure_model(phone, monkeypatch, *, streaming, calls):
    """Stub only provider output/routing; TaskFlow, model loop and runner are real."""
    orch = phone.orch
    orch._multi_agent_enabled = False
    orch._streaming_enabled = streaming
    orch.llm = MagicMock()
    orch.llm.available = True
    orch.llm.model_name = "inert-provider"
    orch.llm.chat_with_failover = AsyncMock(return_value={"choices": [{"message": {
        "role": "assistant", "content": "", "tool_calls": [{"id": call["id"],
            "type": "function", "function": {"name": call["name"],
            "arguments": json.dumps(call["args"])}} for call in calls]}}]})
    orch.llm.extract_response = MagicMock(side_effect=[("", calls), ("Inert task finished", [])])
    provider_calls = []

    async def stream(messages, tools=None, **kwargs):
        provider_calls.append(messages)
        if len(provider_calls) == 1:
            for call in calls:
                yield {"type": "tool_call_delta", "tool_call": call}
        else:
            yield {"type": "text_delta", "content": "Inert task finished"}
        yield {"type": "done"}

    orch.llm.chat_stream = stream
    orch._route_prompt = AsyncMock(return_value=[orch.skills.skills["browser"]])
    orch._ensure_core_skills = lambda skills: skills
    if streaming:
        monkeypatch.setattr(orch, "handle_command", orch.handle_command_stream)
    return provider_calls


def inert_reads(phone, monkeypatch):
    """A local backing implementation with counted returns, behind real policy."""
    from types import SimpleNamespace

    skill = SkillManifest(skill_id="inert_read", brand=BrandProfile(name="Inert read"),
        description="Disposable read fixture", endpoints=[SkillEndpoint(id="read", method="PYTHON", url="",
            description="Return the inert marker", safety_tier="safe", read_only_hint=True,
            params=[EndpointParam(name="marker", type="string", required=True)])])
    phone.orch.skills.register(skill)
    effects = []

    async def execute(endpoint_id, args, vault):
        effects.append(args["marker"])
        return {"success": True, "data": {"marker": args["marker"]}}

    monkeypatch.setitem(SKILL_IMPLEMENTATIONS, "inert_read", SimpleNamespace(execute=execute))
    return effects


def tool_frames(phone):
    return [msg.model_dump() for _, msg in (call.args for call in phone.orch.send.await_args_list)
            if msg.type in {"tool_start", "tool_result"}]


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("call_count", [1, 2])
async def test_real_model_loop_closes_consent_trace_without_effect_or_completion(phone, monkeypatch, streaming, call_count):
    call = {"id": "inert-browser-call", "name": "browser__press_key", "args": {"key": "Tab"}}
    calls = [call] + ([{**call, "id": "not-dispatched-browser-call"}] if call_count == 2 else [])
    provider_calls = configure_model(phone, monkeypatch, streaming=streaming, calls=calls)
    before = list(page_packets(phone))
    await phone.runtime._run_flow(phone.flow_id)

    flow = phone.runtime.get_flow(phone.flow_id)
    assert flow["status"] == flow["steps"][0]["status"] == "waiting", flow
    checkpoint = flow["steps"][0]["result"]
    assert checkpoint["reason"] == REASON and structural_checkpoint(checkpoint) is not None
    assert checkpoint["pending_call_dispatch_started"] is False and checkpoint["model_actions"] == {}
    assert page_packets(phone) == before and not phone.orch.tool_runner._pending_approvals
    assert (len(provider_calls) if streaming else phone.orch.llm.chat_with_failover.await_count) == 1

    execution_sid = "taskflow-" + phone.flow_id
    frames = [(sid, msg.model_dump()) for sid, msg in (call.args for call in phone.orch.send.await_args_list)
              if msg.type in {"tool_start", "tool_result"}]
    assert [(sid, frame["type"], frame["payload"]["call_id"]) for sid, frame in frames] == [
        (execution_sid, kind, call["id"]) for kind in ("tool_start", "tool_result") for call in calls]
    result = frames[-1][1]["payload"]
    assert result["success"] is False and result["error_code"] == REASON
    assert "waiting" in result["error"].lower() and "nothing" in result["error"].lower()
    # The normal session transport is preserved; no publication to the shared
    # origin chat, a foreign node, or the private review-card channel is added.
    assert all(sid == execution_sid and frame["session_id"] == execution_sid for sid, frame in frames)
    assert not any(msg.type in {"approval_request", "text_response"}
                   for _, msg in (call.args for call in phone.orch.send.await_args_list))


@pytest.mark.parametrize("streaming", [False, True])
async def test_real_model_wait_grant_resume_saved_read_once_and_continue(phone, monkeypatch, streaming):
    call = {"id": "inert-browser-read", "name": "browser__get_page_info", "args": {}}
    provider_calls = configure_model(phone, monkeypatch, streaming=streaming, calls=[call])
    before = list(page_packets(phone))
    await phone.runtime._run_flow(phone.flow_id)
    waiting = phone.runtime.get_flow(phone.flow_id)
    assert waiting["status"] == "waiting" and waiting["steps"][0]["result"]["reason"] == REASON
    await grant(phone, waiting, [call["name"]])
    assert page_packets(phone) == before and phone.runtime.get_flow(phone.flow_id)["status"] == "waiting"
    phone.app.include_router(task_router)
    async with http(phone) as client:
        response = await client.post("/api/taskflows/" + phone.flow_id + "/resume")
    assert response.status_code == 200 and response.json()["resource_resume"]["status"] == "queued"
    assert page_packets(phone) == before
    await phone.runtime._run_flow(phone.flow_id)
    saved = phone.runtime.get_flow(phone.flow_id)
    assert saved["status"] == "queued" and saved["steps"][0]["result"]["reason"] == "model_continuation"
    assert saved["steps"][0]["result"]["model_actions"][call["id"]]["result"]["success"] is True
    packets = list(page_packets(phone))
    assert len(packets) > len(before)
    assert (len(provider_calls) if streaming else phone.orch.llm.chat_with_failover.await_count) == 1
    await phone.runtime._run_flow(phone.flow_id)
    assert phone.runtime.get_flow(phone.flow_id)["status"] == "completed"
    assert page_packets(phone) == packets
    assert (len(provider_calls) if streaming else phone.orch.llm.chat_with_failover.await_count) == 2


@pytest.mark.parametrize("streaming", [False, True])
async def test_deferred_trace_uses_existing_session_transport_only(phone, monkeypatch, streaming):
    call = {"id": "private-browser-call", "name": "browser__get_page_info", "args": {}}
    configure_model(phone, monkeypatch, streaming=streaming, calls=[call])
    execution_sid = "taskflow-" + phone.flow_id
    owner, foreign, origin = MagicMock(), MagicMock(), MagicMock()
    for socket in (owner, foreign, origin):
        socket.send_json = AsyncMock()
    monkeypatch.setattr(state, "sessions", {execution_sid: owner, "foreign-session": foreign, "origin-A": origin})
    phone.orch.send = state.send_to_session
    await phone.runtime._run_flow(phone.flow_id)
    traces = [call.args[0] for call in owner.send_json.await_args_list
              if call.args[0]["type"] in {"tool_start", "tool_result"}]
    assert [frame["type"] for frame in traces] == ["tool_start", "tool_result"]
    assert traces[-1]["payload"]["error_code"] == REASON
    assert all(frame["session_id"] == execution_sid for frame in traces)
    foreign.send_json.assert_not_awaited()
    origin.send_json.assert_not_awaited()


@pytest.mark.parametrize("streaming", [False, True])
async def test_deferred_trace_delivery_failure_preserves_consent_wait(phone, monkeypatch, streaming):
    call = {"id": "disconnected-browser-call", "name": "browser__get_page_info", "args": {}}
    configure_model(phone, monkeypatch, streaming=streaming, calls=[call])
    before = list(page_packets(phone))

    async def send(session_id, message):
        if message.type == "tool_result":
            raise RuntimeError("inert transport disconnected")

    phone.orch.send = AsyncMock(side_effect=send)
    await phone.runtime._run_flow(phone.flow_id)
    flow = phone.runtime.get_flow(phone.flow_id)
    assert flow["status"] == "waiting" and flow["steps"][0]["result"]["reason"] == REASON
    assert page_packets(phone) == before


@pytest.mark.parametrize("streaming", [False, True])
async def test_completed_result_precedes_later_wait_and_survives_resume(phone, monkeypatch, streaming):
    effects = inert_reads(phone, monkeypatch)
    completed = {"id": "completed-read", "name": "inert_read__read", "args": {"marker": "verified-inert-read"}}
    paused = {"id": "paused-browser", "name": "browser__get_page_info", "args": {}}
    remaining = {"id": "unstarted-read", "name": "inert_read__read", "args": {"marker": "unstarted"}}
    configure_model(phone, monkeypatch, streaming=streaming, calls=[completed, paused, remaining])
    phone.orch._route_prompt = AsyncMock(return_value=list(phone.orch.skills.skills.values()))
    result_states = []

    async def record_result_state(session_id, message):
        if message.type == "tool_result":
            result_states.append((message.payload["call_id"], phone.runtime.get_flow(phone.flow_id)["status"]))

    phone.orch.send.side_effect = record_result_state
    before = list(page_packets(phone))
    await phone.runtime._run_flow(phone.flow_id)
    waiting = phone.runtime.get_flow(phone.flow_id)
    checkpoint = waiting["steps"][0]["result"]
    assert waiting["status"] == "waiting" and checkpoint["reason"] == REASON
    assert effects == ["verified-inert-read"] and page_packets(phone) == before
    assert checkpoint["model_actions"][completed["id"]]["result"]["success"] is True
    results = [frame["payload"] for frame in tool_frames(phone) if frame["type"] == "tool_result"]
    assert [(result["call_id"], result["success"], result["error_code"]) for result in results] == [
        (completed["id"], True, ""), (paused["id"], False, REASON), (remaining["id"], False, REASON)]
    assert not results[0]["error"]
    assert result_states == [(completed["id"], "running"), (paused["id"], "waiting"), (remaining["id"], "waiting")]
    await grant(phone, waiting, [paused["name"]])
    phone.app.include_router(task_router)
    async with http(phone) as client:
        response = await client.post("/api/taskflows/" + phone.flow_id + "/resume")
    assert response.status_code == 200
    await phone.runtime._run_flow(phone.flow_id)
    saved = phone.runtime.get_flow(phone.flow_id)["steps"][0]["result"]
    assert saved["model_actions"][completed["id"]]["result"] == checkpoint["model_actions"][completed["id"]]["result"]
    assert saved["model_actions"][paused["id"]]["result"]["success"] is True
    packets = list(page_packets(phone))
    await phone.runtime._run_flow(phone.flow_id)
    assert phone.runtime.get_flow(phone.flow_id)["status"] == "completed"
    assert effects == ["verified-inert-read"] and page_packets(phone) == packets


@pytest.mark.parametrize("streaming", [False, True])
async def test_task_model_multiple_returns_are_emitted_once_in_call_order(phone, monkeypatch, streaming):
    effects = inert_reads(phone, monkeypatch)
    calls = [{"id": "read-" + marker, "name": "inert_read__read", "args": {"marker": marker}} for marker in ("first", "second")]
    configure_model(phone, monkeypatch, streaming=streaming, calls=calls)
    phone.orch._route_prompt = AsyncMock(return_value=[phone.orch.skills.skills["inert_read"]])
    await phone.runtime._run_flow(phone.flow_id)
    assert phone.runtime.get_flow(phone.flow_id)["status"] == "completed"
    assert effects == ["first", "second"]
    frames = tool_frames(phone)
    assert [(frame["type"], frame["payload"]["call_id"]) for frame in frames] == [
        (kind, call["id"]) for kind in ("tool_start", "tool_result") for call in calls]
    assert all(frame["payload"]["success"] is True for frame in frames if frame["type"] == "tool_result")
