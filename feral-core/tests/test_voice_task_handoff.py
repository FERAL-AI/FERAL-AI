"""Real pairing, CTM, executor policy, TaskFlow and receipt storage; inert model."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio

from agents.chat_turns import ChatTurnManager
from agents.orchestrator import Orchestrator
from agents.taskflow import TaskFlowRuntime
from bridges.client_voice_attempt import VoiceAttemptBinding
from memory.store import MemoryStore
from models.skill_manifest import SkillManifest
from security.approval_ingress import PairedDevicePrincipal
from security.device_pairing import DevicePairingStore
from skills.registry import SkillRegistry
from voice.task_handoff import VoiceTaskError, VoiceTaskHandoff, read_task_result
from voice.realtime_proxy import RealtimeSession


class DiagnosticRuntime(TaskFlowRuntime):
    def create_flow(self, **kwargs):
        try:
            return super().create_flow(**kwargs)
        except Exception as exc:
            self.debug.append((type(exc).__name__, str(exc)))
            raise


class InertModelOrchestrator(Orchestrator):
    """Provider boundary emits no tools/effects and makes no network requests."""

    async def handle_command(self, session_id, prompt, context=None):
        self.entered.set()
        await self.release.wait()
        return "Synthetic fixture report HARBOR-27. Processing only."


@pytest_asyncio.fixture
async def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("FERAL_AUTONOMY", "loose")
    registry = SkillRegistry()
    manifest = SkillManifest.model_validate(
        json.loads(Path("skills/manifests/task.json").read_text())
    )
    registry.register(manifest)
    memory = MemoryStore(db_path=str(tmp_path / "memory.db"))
    tf = DiagnosticRuntime(
        db_path=str(tmp_path / "tasks.db"), memory_store=memory, skill_registry=registry
    )

    tf.debug = []

    async def client(sid, msg):
        pass

    orch = InertModelOrchestrator(registry, client, {}, memory=memory, taskflows=tf)
    orch.entered, orch.release = asyncio.Event(), asyncio.Event()
    tf._orchestrator = orch
    brain = SimpleNamespace(
        taskflows=tf,
        memory=memory,
        orchestrator=orch,
        sessions={},
        chat_turns=None,
        tool_runner=orch.tool_runner,
    )
    manager = brain.chat_turns = ChatTurnManager(brain)
    monkeypatch.setattr("api.state.state", brain)
    monkeypatch.setattr("api.routes.taskflows.state", brain)
    store = DevicePairingStore(db_path=str(tmp_path / "devices.db"))
    issued = store.pair_device("fixture phone", kind="browser_node_v2")
    assert store.verify_phone_bearer(issued["phone_bearer"]) == issued["device_id"]

    def principal(credential):
        return PairedDevicePrincipal(
            issued["device_id"],
            lambda: store.admitted_credential_current(
                device_id=issued["device_id"],
                credential=credential,
                bearer_kind="phone_bearer",
            ),
        )

    peer = principal(issued["phone_bearer"])
    alive = {"value": True}
    binding = VoiceAttemptBinding(
        "fixture-conversation",
        str(uuid4()),
        object(),
        lambda: alive["value"],
        lambda: True,
    )
    events = []

    async def emit(kind, payload):
        events.append((kind, payload))

    handoff = VoiceTaskHandoff(brain, binding, peer, emit, current_state=lambda: brain)
    binding.task_handoff = handoff
    rig = SimpleNamespace(
        brain=brain,
        tf=tf,
        orch=orch,
        manager=manager,
        store=store,
        peer=peer,
        principal=principal,
        alive=alive,
        binding=binding,
        events=events,
        handoff=handoff,
        manifest=manifest,
        endpoint=manifest.endpoints[0],
    )
    yield rig
    alive["value"] = False
    orch.release.set()
    subscribers = [
        *list(handoff.subscriptions.values()),
        *list(handoff.pending_creations.values()),
    ]
    for task in subscribers:
        task.cancel()
    await asyncio.gather(*subscribers, return_exceptions=True)
    await tf.stop()
    active = [live.task for live in manager._live.values() if live.task]
    if active:
        await asyncio.gather(*active, return_exceptions=True)
    await orch.executor.client.aclose()
    tf._conn.close()
    await memory.aclose()


async def admit(
    rig, call="call-1", goal="Inspect the synthetic fixture and report HARBOR-27."
):
    return await rig.handoff.start(
        call, {"goal": goal}, rig.orch.executor, rig.manifest, rig.endpoint
    )


async def until(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait(), 4)


def recover(rig, data, **kwargs):
    return read_task_result(
        rig.tf,
        session_id=rig.binding.session_id,
        task_id=data["flow_id"],
        request_id=data["voice_request_id"],
        principal=rig.peer,
        guard=lambda: None,
        **kwargs,
    )


async def test_real_admission_durable_dedup_and_changed_terms_refused(runtime):
    r = runtime
    first = await admit(r)
    assert first["success"] is True, first
    assert first["data"]["handoff"]["origin"]["source"] == "tracked_chat_turn"
    assert first["data"]["handoff"]["origin"]["owner_verified"] is True
    second = await admit(r)
    assert second["data"]["flow_id"] == first["data"]["flow_id"]
    assert len(r.tf.list_flows()) == 1
    with pytest.raises(Exception, match="chat_turn_request_conflict"):
        await admit(r, goal="Changed unauthorized terms")
    await until(lambda: any(kind == "task_accepted" for kind, _ in r.events))
    assert r.peer.device_id not in json.dumps(r.events)
    assert r.tf.flow_matches_source(first["data"]["flow_id"], r.peer)


async def test_processing_runs_after_speech_subscriber_retirement_and_recovery(runtime):
    r = runtime
    first = await admit(r)
    data = first["data"]
    await r.tf.start()
    await asyncio.wait_for(r.orch.entered.wait(), 3)
    r.alive["value"] = False  # Voice Stop retires delivery only.
    await asyncio.sleep(0.3)
    r.orch.release.set()
    await until(lambda: r.tf.get_flow(data["flow_id"])["status"] == "completed")
    await until(lambda: any(kind == "task_result" for kind, _ in r.events))
    payload = recover(r, data, replayed=True, recovery_request_id=str(uuid4()))
    assert payload["processing_outcome"] == "completed"
    assert payload["action_outcome"] == "not_asserted"
    assert "HARBOR-27" in payload["report"]
    assert len(r.tf.list_flows()) == 1


@pytest.mark.parametrize("repetition", range(8))
async def test_two_distinct_tasks_complete_independently_and_results_exact(
    runtime, repetition
):
    r = runtime
    a = await admit(r)
    b = await admit(r, call="call-2", goal="Second synthetic goal.")
    assert a.get("success") is True, a
    assert b.get("success") is True, (b, r.tf.debug)
    assert a["data"]["flow_id"] != b["data"]["flow_id"]
    await r.tf.start()
    r.orch.release.set()
    await until(lambda: len([p for kind, p in r.events if kind == "task_result"]) == 2)
    results = [p for kind, p in r.events if kind == "task_result"]
    assert {p["task_id"] for p in results} == {
        a["data"]["flow_id"],
        b["data"]["flow_id"],
    }
    assert len({p["delivery_id"] for p in results}) == 2
    assert recover(r, a["data"])["delivery_id"] == next(
        p["delivery_id"] for p in results if p["task_id"] == a["data"]["flow_id"]
    )


async def test_reconnect_same_device_recovers_revoked_and_foreign_rejected(runtime):
    r = runtime
    first = await admit(r)
    data = first["data"]
    await r.tf.start()
    r.orch.release.set()
    await until(lambda: r.tf.get_flow(data["flow_id"])["status"] == "completed")
    rotated = r.store.rotate_phone_bearer(r.peer.device_id)
    newpeer = r.principal(rotated["phone_bearer"])
    with pytest.raises(asyncio.CancelledError):
        recover(r, data)
    r.peer = newpeer
    payload = recover(r, data, replayed=True)
    assert payload["replayed"] is True
    foreign = r.store.pair_device("foreign", kind="browser_node_v2")
    foreign_peer = PairedDevicePrincipal(
        foreign["device_id"],
        lambda: r.store.admitted_credential_current(
            device_id=foreign["device_id"],
            credential=foreign["phone_bearer"],
            bearer_kind="phone_bearer",
        ),
    )
    r.peer = foreign_peer
    with pytest.raises(VoiceTaskError, match="task_not_found"):
        recover(r, data)
    r.peer = newpeer
    with pytest.raises(VoiceTaskError, match="task_not_found"):
        recover(r, {**data, "voice_request_id": str(uuid4())})


async def test_pending_is_readonly_and_unknown_not_success(runtime):
    r = runtime
    first = await admit(r)
    data = first["data"]
    with pytest.raises(VoiceTaskError, match="task_result_pending"):
        recover(r, data)
    flow = r.tf.get_flow(data["flow_id"])
    r.tf._mark_unknown(flow, flow["steps"][0], "synthetic interrupted operation")
    payload = recover(r, data)
    assert payload["processing_outcome"] == "outcome_unknown"
    assert payload["action_outcome"] == "unknown"
    assert "unknown" in payload["report"]
    assert r.tf.get_flow(data["flow_id"])["status"] == "waiting"
    assert len(r.tf.list_flows()) == 1


class RecordingWire:
    def __init__(self):
        self.frames = []

    async def send(self, data):
        self.frames.append(json.loads(data))


async def test_bounded_reports_wait_idle_no_tool_output_and_retire(runtime):
    rs = RealtimeSession("fixture-conversation", "fixture-node", api_key="fixture")
    rs._ws = RecordingWire()
    rs._connected = True
    rs._response_in_progress = True
    alive = {"value": True}
    for n in range(16):
        assert rs.queue_task_report(
            {
                "contract_version": 1,
                "task_id": "fixture-task-" + str(n),
                "request_id": str(uuid4()),
                "delivery_id": str(uuid4()),
                "session_id": "fixture-conversation",
                "processing_outcome": "completed",
                "action_outcome": "not_asserted",
                "report": "Fixture result",
                "report_truncated": False,
                "durable": True,
                "replayed": False,
            },
            lambda: alive["value"],
        )
    assert not rs.queue_task_report(
        {"delivery_id": "overflow", "report": "ignored"}, lambda: True
    )
    await asyncio.sleep(0.55)
    assert rs._ws.frames == []
    rs._response_in_progress = False
    rs._user_speaking = True
    await asyncio.sleep(0.55)
    assert rs._ws.frames == []
    rs._user_speaking = False
    await until(lambda: len(rs._ws.frames) == 2)
    assert rs._ws.frames[0]["item"]["role"] == "system"
    assert rs._ws.frames[1]["response"]["tool_choice"] == "none"
    assert all(
        frame.get("item", {}).get("type") != "function_call_output"
        for frame in rs._ws.frames
    )
    alive["value"] = False
    rs._retired = True
    rs._task_report_worker.cancel()
    await asyncio.gather(rs._task_report_worker, return_exceptions=True)


async def test_reader_at_creation_commit_is_retried_without_duplicate_flow(runtime):
    import sqlite3

    r = runtime
    first = await admit(r)
    assert first["success"] is True
    reader = sqlite3.connect(r.tf._db_path, timeout=0)
    reader.execute("BEGIN")
    reader.execute("SELECT id FROM taskflows").fetchall()
    try:
        waiting = asyncio.create_task(
            admit(r, call="reader-contention", goal="Second bounded synthetic task.")
        )
        # A shared read transaction blocks COMMIT, rather than BEGIN IMMEDIATE.
        await asyncio.sleep(0.2)
        assert not waiting.done()
    finally:
        reader.rollback()
        reader.close()
    second = await asyncio.wait_for(waiting, 3)
    assert second["success"] is True, second
    assert first["data"]["flow_id"] != second["data"]["flow_id"]
    assert len(r.tf.list_flows()) == 2


def realtime_proxy(rig):
    from voice.realtime_proxy import RealtimeProxy

    rs = RealtimeSession(rig.binding.session_id, "fixture-node", api_key="fixture")
    rs._voice_attempt = rig.binding
    rs._ws = RecordingWire()
    rs._connected = True
    proxy = RealtimeProxy.__new__(RealtimeProxy)
    proxy._sessions = {rs.session_id: rs}
    proxy._skill_registry = rig.orch.skills
    proxy._skill_executor = rig.orch.executor
    proxy._memory = rig.brain.memory
    proxy._orchestrator = rig.orch
    rs._callback_guard = lambda: proxy._sessions.get(rs.session_id) is rs
    rs._on_tool_call = proxy._bind_callback(
        rs, proxy._handle_tool_call, response_scoped=True
    )
    return rs, proxy


async def test_actual_queued_tool_bargein_before_commit_retains_work_no_old_tool_output(
    runtime,
):
    import sqlite3

    r = runtime
    first = await admit(r)
    reader = sqlite3.connect(r.tf._db_path, timeout=0)
    reader.execute("BEGIN")
    reader.execute("SELECT id FROM taskflows").fetchall()
    rs, proxy = realtime_proxy(r)
    try:
        await rs._handle_event(
            {"type": "response.created", "response": {"id": "response-1"}}
        )
        await rs._queue_tool_event(
            {
                "type": "response.function_call_arguments.done",
                "response_id": "response-1",
                "call_id": "barge-in-task",
                "name": "background_task__start",
                "arguments": json.dumps({"goal": "Second synthetic task."}),
            }
        )
        await until(
            lambda: any(
                live.request_id != first["data"]["voice_request_id"]
                for live in r.manager._live.values()
            )
        )
        await rs._handle_event({"type": "input_audio_buffer.speech_started"})
        reader.rollback()
        await until(lambda: len(r.tf.list_flows()) == 2)
        await until(
            lambda: len([p for kind, p in r.events if kind == "task_accepted"]) == 2
        )
        assert all(
            frame.get("item", {}).get("type") != "function_call_output"
            for frame in rs._ws.frames
        )
        assert r.tf.get_flow(first["data"]["flow_id"]) is not None
        transcripts = []

        async def transcript(sid, text, final, **kwargs):
            transcripts.append(text)

        rs._on_transcript = transcript
        await rs._handle_event(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": "Tell me a short story.",
            }
        )
        assert transcripts == ["[user] Tell me a short story."]
    finally:
        reader.close()
        rs._tool_lane.close()
        rs._tool_failure_lane.close()


@pytest.mark.parametrize("provider", ["openai", "gemini"])
async def test_unpaired_background_voice_is_bounded_denial_not_legacy_creation(
    runtime, provider
):
    r = runtime
    if provider == "openai":
        rs, proxy = realtime_proxy(r)
    else:
        from voice.gemini_realtime import GeminiRealtimeSession, GeminiRealtimeProxy

        rs = GeminiRealtimeSession(
            r.binding.session_id, "fixture-node", api_key="fixture"
        )
        rs._ws = RecordingWire()
        rs._connected = True
        proxy = GeminiRealtimeProxy.__new__(GeminiRealtimeProxy)
        proxy._sessions = {rs.session_id: rs}
        proxy._skill_registry = r.orch.skills
        proxy._skill_executor = r.orch.executor
        proxy._orchestrator = r.orch
        proxy._memory = r.brain.memory
    rs._voice_attempt = None
    callback = proxy._bind_callback(rs, proxy._handle_tool_call, response_scoped=True)
    result = json.loads(
        await callback(
            rs.session_id,
            "unpaired-call",
            "background_task__start",
            json.dumps({"goal": "inert"}),
        )
    )
    assert result["success"] is False
    assert result["error_code"] == "task_device_authority_unavailable"
    assert len(r.tf.list_flows()) == 0


async def test_audio_uses_actual_captured_stream_and_provider_response_identity(
    runtime,
):
    r = runtime
    rs, proxy = realtime_proxy(r)
    r.binding.stream_id = "fixture-stream"
    frames = []

    async def node_send(node, frame):
        frames.append(frame)

    proxy._send_to_node = node_send
    proxy._send_to_session = None
    rs._on_audio_delta = proxy._bind_callback(
        rs, proxy._handle_audio_delta, response_scoped=True
    )
    await rs._handle_event(
        {"type": "response.created", "response": {"id": "provider-response-27"}}
    )
    await rs._handle_event(
        {
            "type": "response.output_audio.delta",
            "response_id": "provider-response-27",
            "delta": "AA==",
        }
    )
    assert frames[-1]["payload"]["stream_id"] == "fixture-stream"
    assert frames[-1]["payload"]["response_id"] == "provider-response-27"
    r.binding.active = False
    await rs._handle_event(
        {
            "type": "response.output_audio.delta",
            "response_id": "provider-response-27",
            "delta": "AA==",
        }
    )
    assert len(frames) == 1


async def test_readonly_discovery_recovers_lost_acceptance_exact_device_and_paginates(
    runtime,
):
    from voice.task_handoff import read_task_receipts

    r = runtime

    async def disconnected(kind, payload):
        raise ConnectionError("Synthetic socket retired before acceptance notice")

    r.handoff.emit = disconnected
    first = await admit(
        r, call="lost-accepted", goal="PRIVATE SYNTHETIC GOAL must not join discovery"
    )
    second = await admit(r, call="second-accepted", goal="Another private fixture goal")
    assert first["success"] and second["success"]
    rotated = r.store.rotate_phone_bearer(r.peer.device_id)
    r.peer = r.principal(rotated["phone_bearer"])

    def discover(**kwargs):
        return read_task_receipts(
            r.tf,
            session_id=r.binding.session_id,
            principal=r.peer,
            guard=lambda: None,
            recovery_request_id=str(uuid4()),
            **kwargs,
        )

    page = await asyncio.to_thread(discover, limit=1)
    assert len(page["receipts"]) == 1 and "next_cursor" in page
    tail = await asyncio.to_thread(
        discover, limit=1, older_than_task_id=page["next_cursor"]
    )
    assert len(tail["receipts"]) == 1 and "next_cursor" not in tail
    assert {page["receipts"][0]["task_id"], tail["receipts"][0]["task_id"]} == {
        first["data"]["flow_id"],
        second["data"]["flow_id"],
    }
    wire = json.dumps(page) + json.dumps(tail)
    assert "PRIVATE SYNTHETIC GOAL" not in wire and r.peer.device_id not in wire
    assert all(
        item["replayed"] is True for item in [*page["receipts"], *tail["receipts"]]
    )
    foreign = r.store.pair_device("foreign fixture", kind="browser_node_v2")
    r.peer = PairedDevicePrincipal(
        foreign["device_id"],
        lambda: r.store.admitted_credential_current(
            device_id=foreign["device_id"],
            credential=foreign["phone_bearer"],
            bearer_kind="phone_bearer",
        ),
    )
    assert (await asyncio.to_thread(discover))["receipts"] == []
    with pytest.raises(VoiceTaskError, match="task_cursor_unavailable"):
        await asyncio.to_thread(discover, older_than_task_id=page["next_cursor"])
    assert len(r.tf.list_flows()) == 2


async def test_replayed_refused_admission_no_flow_does_not_leak_futures_or_reexecute(
    runtime,
):
    r = runtime
    r.orch.tool_runner.plan_mode.enter(r.binding.session_id)
    first = await admit(r, call="blocked-admission")
    assert first.get("success") is False
    await until(lambda: not r.manager._live)
    for _ in range(20):
        replay = await admit(r, call="blocked-admission")
        assert replay.get("success") is False
        assert replay.get("replayed") is True
        assert r.handoff.admissions == {}
    assert len(r.tf.list_flows()) == 0
    assert r.handoff.subscriptions == {}


async def test_authenticated_hup_discovery_and_terminal_recovery_use_real_pairing(
    runtime, monkeypatch
):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api import server

    r = runtime
    first = await admit(r)
    await r.tf.start()
    r.orch.release.set()
    await until(
        lambda: r.tf.get_flow(first["data"]["flow_id"])["status"] == "completed"
    )
    owner = r.store.rotate_phone_bearer(r.peer.device_id)
    foreign = r.store.pair_device("foreign wire fixture", kind="browser_node_v2")
    r.brain.device_pairing_store = r.store
    from memory.capability_registry import CapabilityRegistry
    from api.state import BrainState
    from types import MethodType

    r.brain.daemons = {}
    r.brain.activity_log = []
    r.brain.supervisor = None
    r.brain.skill_executor = r.orch.executor
    r.brain.voice_router = r.brain.hardware_mesh = None
    r.brain.capability_registry = CapabilityRegistry()
    r.brain.perception = r.orch.perception
    r.brain._daemon_session_bindings = {}
    r.brain.get_sessions_for_daemon = MethodType(
        BrainState.get_sessions_for_daemon, r.brain
    )
    r.brain._background_tasks = set()

    def retain(task):
        r.brain._background_tasks.add(task)
        task.add_done_callback(r.brain._background_tasks.discard)
        return task

    r.brain.register_background_task = retain
    monkeypatch.setattr(server, "state", r.brain)
    app = FastAPI()
    app.websocket("/v1/node")(server.daemon_session)
    discovery_id = str(uuid4())
    lookup_id = str(uuid4())

    def wire():
        with TestClient(app, raise_server_exceptions=False) as client:
            with client.websocket_connect(
                "/v1/node", headers={"Authorization": "Bearer " + owner["phone_bearer"]}
            ) as ws:
                from models.protocol import TASK_RESULT_CONTRACT

                ws.send_json(
                    {
                        "type": "node_register",
                        "payload": {
                            "node_id": "owned-fixture-phone",
                            "node_type": "phone",
                            "platform": "ios",
                            "capabilities": [],
                        },
                    }
                )
                ack = ws.receive_json()
                assert ack["type"] == "node_ack", ack
                assert ack["payload"]["task_result_contract"] == TASK_RESULT_CONTRACT
                ws.send_json(
                    {
                        "type": "task_receipts_get",
                        "msg_id": discovery_id,
                        "payload": {
                            "contract_version": 1,
                            "session_id": r.binding.session_id,
                            "limit": 16,
                        },
                    }
                )
                discovery = ws.receive_json()
                assert discovery["type"] == "task_receipts", discovery
                item = discovery["payload"]["receipts"][0]
                assert item["task_id"] == first["data"]["flow_id"]
                assert discovery["payload"]["recovery_request_id"] == discovery_id
                ws.send_json(
                    {
                        "type": "task_result_get",
                        "msg_id": lookup_id,
                        "payload": {
                            "contract_version": 1,
                            "session_id": r.binding.session_id,
                            "task_id": item["task_id"],
                            "request_id": item["request_id"],
                        },
                    }
                )
                result = ws.receive_json()
                assert result["type"] == "task_result", result
                assert result["payload"]["processing_outcome"] == "completed"
                assert result["payload"]["recovery_request_id"] == lookup_id
                assert result["payload"]["replayed"] is True
            with client.websocket_connect(
                "/v1/node",
                headers={"Authorization": "Bearer " + foreign["phone_bearer"]},
            ) as ws:
                ws.send_json(
                    {
                        "type": "task_receipts_get",
                        "msg_id": str(uuid4()),
                        "payload": {
                            "contract_version": 1,
                            "session_id": r.binding.session_id,
                        },
                    }
                )
                assert ws.receive_json()["payload"]["receipts"] == []
                ws.send_json(
                    {
                        "type": "task_result_get",
                        "msg_id": str(uuid4()),
                        "payload": {
                            "contract_version": 1,
                            "session_id": r.binding.session_id,
                            "task_id": item["task_id"],
                            "request_id": item["request_id"],
                        },
                    }
                )
                denied = ws.receive_json()
                assert denied["type"] == "error"
                assert denied["payload"]["name"] == "task_not_found"
                assert "HARBOR-27" not in json.dumps(denied)

    await asyncio.wait_for(asyncio.to_thread(wire), 10)
    assert len(r.tf.list_flows()) == 1


async def test_reviewed_creation_keeps_exact_origin_and_subscribes_after_local_approval(
    runtime,
):
    r = runtime
    r.orch.tool_runner._autonomy_mode = "strict"
    pending = await admit(r, call="reviewed-task")
    assert pending.get("status") == "pending_approval", pending
    assert len(r.tf.list_flows()) == 0
    assert len(r.handoff.pending_creations) == 1
    # Existing local operator resolution only; paired voice gains no reviewer authority.
    resolution = await r.orch.resolve_tool_approval_request(
        pending["request_id"], approved=True, session_id=r.binding.session_id
    )
    assert (
        resolution.get("status") == "approved"
        and resolution.get("result", {}).get("success") is True
    ), resolution
    await until(lambda: any(kind == "task_accepted" for kind, _ in r.events))
    accepted = next(payload for kind, payload in r.events if kind == "task_accepted")
    assert r.tf.flow_matches_source(accepted["task_id"], r.peer)
    assert len(r.tf.list_flows()) == 1
    await r.tf.start()
    r.orch.release.set()
    await until(lambda: any(kind == "task_result" for kind, _ in r.events))
    terminal = next(payload for kind, payload in r.events if kind == "task_result")
    assert terminal["request_id"] == accepted["request_id"]


async def test_actual_response_lifecycle_precedes_audio_and_old_response_is_ignored(
    runtime,
):
    r = runtime
    rs, proxy = realtime_proxy(r)
    r.binding.stream_id = "fixture-owned-stream"
    frames = []

    async def send(node, frame):
        frames.append(frame)

    proxy._send_to_node = send
    proxy._send_to_session = None
    proxy._pending_replies = {}
    rs._on_response_created = proxy._bind_callback(
        rs, proxy._handle_response_created, response_scoped=True
    )
    rs._on_response_done = proxy._bind_callback(
        rs, proxy._handle_response_done, response_scoped=True
    )
    rs._on_audio_delta = proxy._bind_callback(
        rs, proxy._handle_audio_delta, response_scoped=True
    )
    await rs._handle_event(
        {"type": "response.created", "response": {"id": "actual-response-first"}}
    )
    await rs._handle_event(
        {
            "type": "response.output_audio.delta",
            "response_id": "actual-response-first",
            "delta": "AA==",
        }
    )
    assert [frame["type"] for frame in frames] == ["voice_response", "audio_response"]
    assert {
        key: frames[0]["payload"][key]
        for key in ("contract_version", "stream_id", "response_id", "state")
    } == {
        "contract_version": 1,
        "stream_id": "fixture-owned-stream",
        "response_id": "actual-response-first",
        "state": "created",
    }
    await rs._handle_event(
        {"type": "response.created", "response": {"id": "actual-response-current"}}
    )
    count = len(frames)
    await rs._handle_event(
        {
            "type": "response.output_audio.delta",
            "response_id": "actual-response-first",
            "delta": "AA==",
        }
    )
    await rs._handle_event(
        {
            "type": "response.done",
            "response": {"id": "actual-response-first", "status": "completed"},
        }
    )
    assert len(frames) == count
    await rs._handle_event(
        {
            "type": "response.done",
            "response": {"id": "actual-response-current", "status": "completed"},
        }
    )
    assert frames[-1]["type"] == "voice_response"
    assert frames[-1]["payload"]["state"] == "completed"
    assert frames[-1]["payload"]["response_id"] == "actual-response-current"


def report_payload():
    return {
        "contract_version": 1,
        "task_id": "fixture-notice",
        "request_id": str(uuid4()),
        "delivery_id": str(uuid4()),
        "session_id": "fixture-conversation",
        "processing_outcome": "completed",
        "action_outcome": "not_asserted",
        "report": "Synthetic report only.",
        "report_truncated": False,
        "durable": True,
        "replayed": False,
    }


async def test_report_dispatch_consumed_once_when_response_created_arrives_during_send(
    runtime,
):
    rs = RealtimeSession("fixture-conversation", "fixture-node", api_key="fixture")

    class LifecycleWire(RecordingWire):
        async def send(self, data):
            await super().send(data)
            if self.frames[-1]["type"] == "response.create":
                await rs._handle_event(
                    {
                        "type": "response.created",
                        "response": {"id": "owned-notice-response"},
                    }
                )

    rs._ws = LifecycleWire()
    rs._connected = True
    assert rs.queue_task_report(report_payload(), lambda: True)
    await until(lambda: len(rs._ws.frames) == 2)
    assert rs._task_reports == []
    await rs._handle_event(
        {
            "type": "response.done",
            "response": {"id": "owned-notice-response", "status": "completed"},
        }
    )
    await asyncio.sleep(0.6)
    assert (
        len([frame for frame in rs._ws.frames if frame["type"] == "response.create"])
        == 1
    )
    await asyncio.gather(rs._task_report_worker, return_exceptions=True)


async def test_report_pre_wire_bargein_retains_then_resumes_at_idle(runtime):
    rs = RealtimeSession("fixture-conversation", "fixture-node", api_key="fixture")

    class InterruptWire(RecordingWire):
        interrupted = False

        async def send(self, data):
            await super().send(data)
            if (
                self.frames[-1]["type"] == "conversation.item.create"
                and not self.interrupted
            ):
                self.interrupted = True
                await rs._handle_event({"type": "input_audio_buffer.speech_started"})

    rs._ws = InterruptWire()
    rs._connected = True
    assert rs.queue_task_report(report_payload(), lambda: True)
    await until(lambda: rs._ws.interrupted)
    assert len(rs._task_reports) == 1
    assert not any(frame["type"] == "response.create" for frame in rs._ws.frames)
    await rs._handle_event({"type": "input_audio_buffer.speech_stopped"})
    await rs._handle_event(
        {"type": "response.created", "response": {"id": "foreground-after-bargein"}}
    )
    await rs._handle_event(
        {
            "type": "response.done",
            "response": {"id": "foreground-after-bargein", "status": "completed"},
        }
    )
    await until(
        lambda: len(
            [frame for frame in rs._ws.frames if frame["type"] == "response.create"]
        )
        == 1
    )
    assert rs._task_reports == []
    await asyncio.gather(rs._task_report_worker, return_exceptions=True)
