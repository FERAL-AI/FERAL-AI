"""Registered HUP route intake while inert interactive phone work waits."""
from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.websockets import WebSocketDisconnect

from tests.test_daemon_session_phone_branches import _mock_state_with_supervisor, _flush_with_known_error
from tests.test_hup_protocol import _TEST_NODE_KEY, _node_client, _register_node
from tests.test_taskflow_model_steps import wired as taskflow_wired  # noqa: F401

pytestmark = [pytest.mark.no_auto_feral_home, pytest.mark.timeout(15)]


def _state():
    brain = _mock_state_with_supervisor()
    brain._native_agent_turn_tasks = set()
    brain._background_tasks = set()
    brain._phone_intake_locks = {}
    brain.orchestrator._text_response_suppressed = {}

    def retain(task):
        brain._background_tasks.add(task)
        task.add_done_callback(brain._background_tasks.discard)
        return task

    brain.register_background_task = MagicMock(side_effect=retain)
    return brain


def _chat(ws, *, text="read fixture", session="shared", reply="request-one"):
    ws.send_json({"type": "chat_request", "payload": {
        "text": text, "session_id": session, "reply_to": reply,
        "reply_mode": "final", "channel": "chat", "device_target": "brain",
    }})


def test_heartbeat_action_response_and_voice_control_arrive_while_chat_waits():
    brain = _state()
    started = threading.Event()
    release = asyncio.Event()

    async def inert_provider(**kwargs):
        started.set()
        await release.wait()
        return {"text": "fixture read completed"}

    brain.orchestrator.handle_command = AsyncMock(side_effect=inert_provider)
    brain.voice_router = MagicMock()
    brain.voice_router.session_for_node.return_value = "shared"
    brain.voice_router._realtime = brain.voice_router._gemini = None
    brain.voice_router.cancel_chained_response = AsyncMock(return_value=True)
    brain.voice_router.stop_node_voice = AsyncMock()
    with _node_client(brain) as client:
        with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
            assert _register_node(ws, "phone", "phone")["type"] == "node_ack"
            _chat(ws)
            assert started.wait(2)
            ws.send_json({"type": "node_heartbeat", "payload": {"ts": 1.0}})
            ws.send_json({"type": "hup_action_response", "payload": {
                "action_id": "fixture-action", "success": True, "result": {"read": "ok"}}})
            ws.send_json({"type": "voice_interrupt", "payload": {"reason": "user_interrupt"}})
            # This ordered read proves receive/control intake before release;
            # sleeping and later inspecting mocks would miss the deadlock.
            _flush_with_known_error(ws)
            assert not release.is_set()
            brain.hardware_mesh.node_health.record_heartbeat.assert_called_once()
            call = brain.hardware_mesh.resolve_invoke.call_args
            assert call.args[0] == "fixture-action"
            assert call.kwargs == {"connection": brain.daemons["phone"], "node_id": "phone"}
            brain.orchestrator.handle_daemon_result.assert_awaited_once()
            brain.voice_router.cancel_chained_response.assert_awaited_once_with("shared")
            ws.portal.call(release.set)
            reply = ws.receive_json()
            assert reply["type"] == "chat_response"
            assert reply["payload"] == {
                "session_id": "shared", "text": "fixture read completed", "reply_mode": "final",
                "channel": "chat", "reply_to": "request-one", "error": None,
            }


def test_same_session_queued_requests_keep_exact_identity_and_order():
    brain = _state()
    started = threading.Event()
    release = asyncio.Event()
    calls = []

    async def inert_provider(**kwargs):
        calls.append(kwargs["text"])
        if kwargs["text"] == "first":
            started.set()
            await release.wait()
        return {"text": kwargs["text"]}

    brain.orchestrator.handle_command = AsyncMock(side_effect=inert_provider)
    with _node_client(brain) as client:
        with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
            _register_node(ws, "phone", "phone")
            _chat(ws, text="first", reply="one")
            assert started.wait(2)
            _chat(ws, text="second", reply="two")
            _flush_with_known_error(ws)
            assert calls == ["first"]
            ws.portal.call(release.set)
            first, second = ws.receive_json(), ws.receive_json()
            assert [first["payload"]["reply_to"], second["payload"]["reply_to"]] == ["one", "two"]
            assert [first["payload"]["text"], second["payload"]["text"]] == ["first", "second"]
            assert all(r["payload"]["session_id"] == "shared" for r in (first, second))
    assert calls == ["first", "second"]
    assert brain._phone_intake_locks == {}


@pytest.mark.parametrize("graceful", [False, True])
def test_disconnect_cancels_owned_work_and_does_not_replay(graceful):
    brain = _state()
    started, cancelled = threading.Event(), threading.Event()

    async def inert_provider(**kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    brain.orchestrator.handle_command = AsyncMock(side_effect=inert_provider)
    with _node_client(brain) as client:
        with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
            _register_node(ws, "phone", "phone")
            _chat(ws)
            assert started.wait(2)
            intake = brain.daemons["phone"]._feral_phone_chat_intake
            assert len(intake.tasks) == 1
            if graceful:
                ws.send_json({"type": "node_bye", "payload": {"reason": "user_disconnect"}})
                with pytest.raises(WebSocketDisconnect):
                    ws.receive_json()
        assert cancelled.wait(2)
        assert intake.closed
        assert not intake.tasks
        assert brain.daemons == {}
        assert brain.orchestrator._text_response_suppressed.get("shared") is None
        with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
            _register_node(ws, "phone", "phone")
            _flush_with_known_error(ws)
    assert brain.orchestrator.handle_command.await_count == 1


def test_replacement_cannot_receive_old_reply_or_be_torn_down_by_old_socket():
    brain = _state()
    started, cancelled = threading.Event(), threading.Event()
    release_stale = asyncio.Event()

    async def inert_provider(**kwargs):
        if kwargs["text"] == "old":
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release_stale.wait()
                # A late collaborator result does not make the old connection
                # current, even if the collaborator swallowed cancellation.
                return {"text": "stale result"}
        return {"text": "new result"}

    brain.orchestrator.handle_command = AsyncMock(side_effect=inert_provider)
    with _node_client(brain) as client:
        with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as old:
            _register_node(old, "phone", "phone")
            _chat(old, text="old", reply="old-request")
            assert started.wait(2)
            old_socket = brain.daemons["phone"]
            # Share the existing ASGI portal without invoking brain startup.
            # Both real registered sockets use the production single loop.
            client.portal = old.portal
            try:
                with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as new:
                    _register_node(new, "phone", "phone")
                    assert cancelled.wait(2)
                    new_socket = brain.daemons["phone"]
                    assert new_socket is not old_socket
                    _chat(new, text="new", reply="new-request")
                    _flush_with_known_error(new)
                    assert brain.orchestrator.handle_command.await_count == 1
                    new.portal.call(release_stale.set)
                    result = new.receive_json()
                    assert result["type"] == "chat_response"
                    assert result["payload"]["reply_to"] == "new-request"
                    assert result["payload"]["text"] == "new result"
                    old.send_json({"type": "node_bye", "payload": {"reason": "old_connection"}})
                    with pytest.raises(WebSocketDisconnect):
                        old.receive_json()
                    assert brain.daemons["phone"] is new_socket
                    assert brain.sessions["shared"] is new_socket
                    brain.skill_executor.unregister_daemon.assert_not_called()
                    brain.hardware_mesh.on_node_disconnected.assert_not_called()
                    _flush_with_known_error(new)
            finally:
                client.portal = None
    assert brain.orchestrator.handle_command.await_count == 2
    assert brain._phone_intake_locks == {}


def test_queue_saturation_refuses_exact_request_without_starting_it():
    brain = _state()
    started = threading.Event()
    release = asyncio.Event()

    async def inert_provider(**kwargs):
        started.set()
        await release.wait()
        return {"text": kwargs["text"]}

    brain.orchestrator.handle_command = AsyncMock(side_effect=inert_provider)
    with _node_client(brain) as client:
        with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
            _register_node(ws, "phone", "phone")
            _chat(ws, text="one")
            assert started.wait(2)
            for number in range(2, 18):
                _chat(ws, text=str(number), reply=f"request-{number}")
            refusal = ws.receive_json()
            assert refusal["type"] == "chat_response"
            assert refusal["payload"]["reply_to"] == "request-17"
            assert refusal["payload"]["session_id"] == "shared"
            assert refusal["payload"]["error"] == "phone_chat_busy; request was not started"
            assert brain.orchestrator.handle_command.await_count == 1
            assert len(brain.daemons["phone"]._feral_phone_chat_intake.tasks) == 16
    assert brain.orchestrator.handle_command.await_count == 1


def test_claimed_native_pair_token_keeps_the_same_responsive_chat_contract(tmp_path):
    from security.device_pairing import DevicePairingStore

    brain = _state()
    started = threading.Event()
    release = asyncio.Event()
    store = DevicePairingStore(db_path=str(tmp_path / "responsive-pairs.db"))
    issued = store.pair_device("Fixture phone", kind="hup", node_id="phone", require_pin=False)
    store.mark_claimed(issued["token"])

    async def inert_provider(**kwargs):
        started.set()
        await release.wait()
        return {"text": "paired fixture"}

    brain.orchestrator.handle_command = AsyncMock(side_effect=inert_provider)
    with _node_client(brain) as client:
        brain.device_pairing_store = store
        with client.websocket_connect("/v1/node", headers={"Authorization": f"Bearer {issued['token']}"}) as ws:
            _register_node(ws, "phone", "phone")
            _chat(ws, session="paired-thread", reply="paired-request")
            assert started.wait(2)
            _flush_with_known_error(ws)
            ws.portal.call(release.set)
            response = ws.receive_json()
            assert response["type"] == "chat_response"
            assert response["payload"]["session_id"] == "paired-thread"
            assert response["payload"]["reply_to"] == "paired-request"
            context = brain.orchestrator.handle_command.call_args.kwargs["context"]
            assert context["paired_device_id"] == issued["device_id"]
            assert context["device_target"] == "brain"


def test_different_session_can_finish_while_another_session_waits():
    brain = _state()
    started = threading.Event()
    release = asyncio.Event()

    async def inert_provider(**kwargs):
        if kwargs["session_id"] == "A":
            started.set()
            await release.wait()
        return {"text": kwargs["session_id"]}

    brain.orchestrator.handle_command = AsyncMock(side_effect=inert_provider)
    with _node_client(brain) as client:
        with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
            _register_node(ws, "phone", "phone")
            _chat(ws, session="A", reply="A-request")
            assert started.wait(2)
            _chat(ws, session="B", reply="B-request")
            response = ws.receive_json()
            assert response["payload"]["session_id"] == "B"
            assert response["payload"]["reply_to"] == "B-request"
            assert not release.is_set()
            ws.portal.call(release.set)
            response = ws.receive_json()
            assert response["payload"]["session_id"] == "A"
            assert response["payload"]["reply_to"] == "A-request"


def test_queued_request_does_not_adopt_a_replacement_runtime():
    brain = _state()
    started = threading.Event()
    release = asyncio.Event()
    original = brain.orchestrator

    async def inert_provider(**kwargs):
        started.set()
        await release.wait()
        return {"text": "old runtime"}

    original.handle_command = AsyncMock(side_effect=inert_provider)
    with _node_client(brain) as client:
        with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
            _register_node(ws, "phone", "phone")
            _chat(ws, text="first", reply="first-request")
            assert started.wait(2)
            _chat(ws, text="queued", reply="queued-request")
            _flush_with_known_error(ws)
            replacement = MagicMock()
            replacement._text_response_suppressed = {"shared": "replacement-owner"}
            replacement.handle_command = AsyncMock(return_value={"text": "must not run"})
            brain.orchestrator = replacement
            intake = brain.daemons["phone"]._feral_phone_chat_intake
            ws.portal.call(release.set)

            async def settled():
                await asyncio.gather(*tuple(intake.tasks), return_exceptions=True)

            ws.portal.call(settled)
            # No old or queued reply is delivered into the replacement runtime.
            _flush_with_known_error(ws)
            assert original.handle_command.await_count == 1
            replacement.handle_command.assert_not_awaited()
            assert replacement._text_response_suppressed == {"shared": "replacement-owner"}
            assert original._text_response_suppressed == {}
            pushes = brain.memory.working_push.call_args_list
            assert len(pushes) == 1 and pushes[0].args[1]["text"] == "first"


def test_managed_disconnect_retains_pending_checkpoint_and_reconnect_refuses_replay(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from starlette.testclient import TestClient
    from api import server
    from memory.store import MemoryStore
    from memory.runtime_session_checkpoint import CheckpointStatus
    from tests.test_runtime_context_lifecycle import make_orch

    brain = _state()
    store = MemoryStore(db_path=str(tmp_path / "phone-disconnect-context.db"))
    orch = make_orch(store, [])
    orch._context_checkpoints.legacy_passthrough = True
    orch.send = AsyncMock(return_value=True)
    brain.memory, brain.orchestrator = store, orch
    brain.device_pairing_store = None
    monkeypatch.setattr(server, "state", brain)
    monkeypatch.setattr(server, "NODE_API_KEY", _TEST_NODE_KEY)
    started, cancelled = threading.Event(), threading.Event()
    provider_calls = []

    async def inert_provider(messages, **kwargs):
        provider_calls.append(messages)
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    orch.llm.chat_with_failover = inert_provider
    app = FastAPI()
    app.websocket("/v1/node")(server.daemon_session)
    with TestClient(app) as client:
        attachment = client.portal.call(lambda: orch._context_checkpoints.attach("managed-phone", checkpoint_version=1))
        try:
            with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
                _register_node(ws, "phone", "phone")
                _chat(ws, session="managed-phone", reply="managed-request")
                assert started.wait(2)
                assert client.portal.call(store.runtime_checkpoint_read, "managed-phone").status == CheckpointStatus.IN_PROGRESS
                _flush_with_known_error(ws)
            assert cancelled.wait(2)
            assert client.portal.call(store.runtime_checkpoint_read, "managed-phone").status == CheckpointStatus.IN_PROGRESS
            with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
                _register_node(ws, "phone", "phone")
                _chat(ws, session="managed-phone", reply="review-required")
                error = ws.receive_json()
                assert error["type"] == "error"
                assert "checkpoint_in_progress" in error["payload"]["message"]
                refused = ws.receive_json()
                assert refused["type"] == "chat_response"
                assert refused["payload"]["session_id"] == "managed-phone"
                assert refused["payload"]["reply_to"] == "review-required"
                assert refused["payload"]["error"] == "checkpoint_in_progress"
            assert len(provider_calls) == 1
        finally:
            client.portal.call(orch._context_checkpoints.detach, attachment.token)
            client.portal.call(orch.drain_background_tasks)
            client.portal.call(store.aclose)


@pytest.mark.parametrize("replacement_kind", ["state", "daemon"])
def test_error_send_replacement_does_not_publish_final_or_consume_sources(monkeypatch, replacement_kind):
    brain = _state()
    replacement = _state()
    original_sources = {"shared": [{"title": "Original turn", "url": "https://example.test/original"}]}
    replacement_sources = {"shared": [{"title": "Replacement turn", "url": "https://example.test/replacement"}]}
    brain.orchestrator.tool_runner.pop_grounding_sources = MagicMock(side_effect=lambda sid: original_sources.pop(sid, []))
    replacement.orchestrator.tool_runner.pop_grounding_sources = MagicMock(side_effect=lambda sid: replacement_sources.pop(sid, []))
    brain.orchestrator.handle_command = AsyncMock(side_effect=RuntimeError("fixture provider failure"))
    changed = threading.Event()

    with _node_client(brain) as client:
        from api import server
        original_error_sender = server._send_protocol_error
        somatic_read = MagicMock(wraps=server._somatic_state_for_turn)
        monkeypatch.setattr(server, "_somatic_state_for_turn", somatic_read)

        async def replace_during_error_send(socket, *args, **kwargs):
            await original_error_sender(socket, *args, **kwargs)
            if kwargs.get("name") == "orchestrator_error":
                if replacement_kind == "state":
                    server.state = replacement
                else:
                    brain.daemons["phone"] = object()
                changed.set()
                await asyncio.sleep(0)

        monkeypatch.setattr(server, "_send_protocol_error", replace_during_error_send)
        with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
            _register_node(ws, "phone", "phone")
            intake = brain.daemons["phone"]._feral_phone_chat_intake
            _chat(ws)
            error = ws.receive_json()
            assert error["type"] == "error" and error["payload"]["name"] == "orchestrator_error"
            assert changed.wait(2)

            async def settled():
                await asyncio.gather(*tuple(intake.tasks), return_exceptions=True)

            ws.portal.call(settled)
            assert original_sources["shared"][0]["title"] == "Original turn"
            assert replacement_sources["shared"][0]["title"] == "Replacement turn"
            brain.orchestrator.tool_runner.pop_grounding_sources.assert_not_called()
            replacement.orchestrator.tool_runner.pop_grounding_sources.assert_not_called()
            somatic_read.assert_not_called()
            ws.send_json({"type": "node_bye", "payload": {"reason": "stale_socket"}})
            # A stale final response would be read here before the close.
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()


def test_unregistered_authenticated_old_socket_cannot_register_in_replacement_state(monkeypatch):
    brain = _state()
    replacement = _state()
    with _node_client(brain) as client:
        from api import server
        with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
            # Authentication accepted the original state; no node was registered.
            ws.portal.call(asyncio.sleep, 0)
            server.state = replacement
            ws.send_json({"type": "node_register", "payload": {
                "node_id": "phone", "node_type": "phone", "platform": "ios", "capabilities": []}})
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
    assert brain.daemons == {} and replacement.daemons == {}
    replacement.skill_executor.register_daemon_type.assert_not_called()
    replacement.capability_registry.register_node.assert_not_called()
    replacement.hardware_mesh.on_node_connected.assert_not_awaited()


def test_scope_exit_replacement_cannot_record_success_or_consume_sources(monkeypatch):
    from contextlib import asynccontextmanager
    from tests.test_daemon_session_phone_branches import _phone_recorded

    brain = _state()
    brain.orchestrator.handle_command = AsyncMock(return_value={"text": "completed old turn"})
    sources = {"shared": [{"title": "Owned source", "url": "https://example.test/source"}]}
    brain.orchestrator.tool_runner.pop_grounding_sources = MagicMock(side_effect=lambda sid: sources.pop(sid, []))
    replaced = threading.Event()
    with _node_client(brain) as client:
        from api import server
        original_scope = server.prepared_scope

        @asynccontextmanager
        async def replace_at_scope_exit(*args, **kwargs):
            async with original_scope(*args, **kwargs) as receipt:
                yield receipt
            brain.daemons["phone"] = object()
            replaced.set()

        monkeypatch.setattr(server, "prepared_scope", replace_at_scope_exit)
        with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
            _register_node(ws, "phone", "phone")
            intake = brain.daemons["phone"]._feral_phone_chat_intake
            _chat(ws)
            assert replaced.wait(2)

            async def settled():
                await asyncio.gather(*tuple(intake.tasks), return_exceptions=True)

            ws.portal.call(settled)
            assert not _phone_recorded(brain, "chat_request", "allowed")
            brain.orchestrator.tool_runner.pop_grounding_sources.assert_not_called()
            assert sources["shared"][0]["title"] == "Owned source"
            ws.send_json({"type": "node_bye", "payload": {"reason": "stale_socket"}})
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()


def test_state_replacement_during_node_connected_prevents_ack(monkeypatch):
    brain = _state()
    replacement = _state()
    with _node_client(brain) as client:
        from api import server

        async def replace_during_connect(*args, **kwargs):
            server.state = replacement
            await asyncio.sleep(0)

        brain.hardware_mesh.on_node_connected = AsyncMock(side_effect=replace_during_connect)
        with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
            ws.send_json({"type": "node_register", "payload": {
                "node_id": "phone", "node_type": "phone", "platform": "ios", "capabilities": []}})
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
        assert brain.daemons == {} and replacement.daemons == {}
        replacement.skill_executor.register_daemon_type.assert_not_called()
        replacement.capability_registry.register_node.assert_not_called()


@pytest.mark.parametrize(
    "invalidation", ["stop", "socket", "state", "orchestrator", "memory"]
)
def test_invalidated_phone_cannot_dispatch_after_collaborator_suppresses_cancel(
    invalidation,
):
    """Actual public ToolRunner dispatch entry, with an inert inner boundary."""
    from types import SimpleNamespace
    from api.phone_chat_intake import PhoneChatIntake
    from agents.tool_runner import ToolRunner
    from security.agent_turn_lease import AgentTurnRevoked, attach_agent_dispatch_lease

    async def exercise():
        socket = object()
        runner = ToolRunner.__new__(ToolRunner)
        orchestrator = SimpleNamespace(tool_runner=runner)
        runner._orch = orchestrator
        runner._native_agent_dispatch_lease = None
        effects = []
        runner._resolve_surface_for_session = lambda sid: "brain_host"
        runner._record_tool_invocation = MagicMock()
        runner._turn_id_for = lambda sid: ""

        async def inert_inner(*args, **kwargs):
            effects.append("inert dispatcher boundary")

        runner._execute_tool_call_inner = AsyncMock(side_effect=inert_inner)
        brain = SimpleNamespace(
            _native_vault_deferred=True,
            _native_bootstrap_required=False,
            _native_agent_turn_generation=7,
            agent_bootstrap_controller=SimpleNamespace(
                _completed=object(), _active_binding=None
            ),
            orchestrator=orchestrator,
            memory=object(),
            daemons={"fixture-phone": socket},
            _background_tasks=set(),
        )

        def retain(task):
            brain._background_tasks.add(task)
            task.add_done_callback(brain._background_tasks.discard)

        brain.register_background_task = retain
        attach_agent_dispatch_lease(brain)
        current = [brain]
        intake = PhoneChatIntake(brain, socket, lambda: current[0])
        intake.node_id = "fixture-phone"
        entered = asyncio.Event()
        denied = []

        async def operation():
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                with pytest.raises(AgentTurnRevoked):
                    await runner.execute_tool_call(
                        "fixture-session",
                        {"id": "one", "name": "fixture__inert", "args": {}},
                        [],
                    )
                denied.append(True)

        assert intake.submit("fixture-session", operation)
        await entered.wait()
        if invalidation == "socket":
            brain.daemons["fixture-phone"] = object()
        elif invalidation == "state":
            current[0] = object()
        elif invalidation == "orchestrator":
            brain.orchestrator = object()
        elif invalidation == "memory":
            brain.memory = object()
        task = next(iter(intake.tasks))
        if invalidation == "stop":
            intake.stop()
        else:
            # Cancel without closing intake: each captured identity must fence
            # dispatch on its own, including same-state runtime replacement.
            task.cancel()
        await task
        assert denied == [True] and effects == []
        await intake.drain()
        assert denied == [True] and effects == []
        assert runner._execute_tool_call_inner.await_count == 0
        assert brain._native_agent_turn_generation == 7
        # Owner revocation is local to inherited phone work; an unrelated
        # foreground call keeps the same unchanged native lease/approval path.
        brain.orchestrator = orchestrator
        await runner.execute_tool_call(
            "foreground-session",
            {"id": "two", "name": "fixture__inert", "args": {}},
            [],
        )
        assert effects == ["inert dispatcher boundary"]
        assert brain._phone_intake_locks == {}

    asyncio.run(exercise())


def test_child_task_inherits_phone_dispatch_fence_without_revoking_other_sessions():
    from types import SimpleNamespace
    from api.phone_chat_intake import PhoneChatIntake
    from agents.tool_runner import ToolRunner
    from security.agent_turn_lease import AgentTurnRevoked

    async def exercise():
        socket = object()
        runner = ToolRunner.__new__(ToolRunner)
        runner._orch = SimpleNamespace()
        runner._native_agent_dispatch_lease = None
        runner._resolve_surface_for_session = lambda sid: "brain_host"
        runner._record_tool_invocation = MagicMock()
        runner._turn_id_for = lambda sid: ""
        runner._execute_tool_call_inner = AsyncMock()
        brain = SimpleNamespace(
            orchestrator=runner._orch,
            memory=object(),
            daemons={"fixture-phone": socket},
            _background_tasks=set(),
        )

        def retain(task):
            brain._background_tasks.add(task)
            task.add_done_callback(brain._background_tasks.discard)

        brain.register_background_task = retain
        intake = PhoneChatIntake(brain, socket, lambda: brain)
        intake.node_id = "fixture-phone"
        entered, release = asyncio.Event(), asyncio.Event()
        children = []

        async def child():
            await release.wait()
            with pytest.raises(AgentTurnRevoked):
                await runner.execute_tool_call(
                    "fixture-session",
                    {"id": "child", "name": "fixture__inert", "args": {}},
                    [],
                )

        async def operation():
            task = asyncio.create_task(child())
            children.append(task)
            brain.register_background_task(task)
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                release.set()
                await task

        assert intake.submit("fixture-session", operation)
        await entered.wait()
        intake.stop()
        await intake.drain()
        await asyncio.gather(*children)
        runner._execute_tool_call_inner.assert_not_awaited()
        assert brain._phone_intake_locks == {}
        await runner.execute_tool_call(
            "unrelated", {"id": "foreground", "name": "fixture__inert", "args": {}}, []
        )
        runner._execute_tool_call_inner.assert_awaited_once()

    asyncio.run(exercise())


def test_nested_dispatch_owner_cannot_replace_revoked_outer_owner():
    from security.agent_turn_lease import (
        AgentTurnRevoked,
        bind_agent_dispatch_owner,
        guard_agent_dispatch,
    )

    allowed = [True]
    with bind_agent_dispatch_owner(lambda: allowed[0]):
        guard_agent_dispatch()
        allowed[0] = False
        with pytest.raises(AgentTurnRevoked):
            with bind_agent_dispatch_owner(lambda: True):
                guard_agent_dispatch()
    guard_agent_dispatch()


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["phone", "native"])
@pytest.mark.usefixtures("taskflow_wired")
async def test_executor_backing_rechecks_owner_after_child_is_scheduled(
    request, monkeypatch, owner
):
    from types import SimpleNamespace
    from api.phone_chat_intake import PhoneChatIntake
    from api import state as state_module
    from security.agent_turn_lease import attach_agent_dispatch_lease
    from skills import impl
    from skills.executor import SkillExecutor
    from tests.test_taskflow_model_steps import action

    _, orch, _ = request.getfixturevalue("taskflow_wired")
    socket = object()
    brain = SimpleNamespace(
        orchestrator=orch,
        memory=object(),
        daemons={"phone": socket},
        _background_tasks=set(),
        _native_vault_deferred=True,
        _native_bootstrap_required=False,
        _native_agent_turn_generation=7,
        agent_bootstrap_controller=SimpleNamespace(
            _completed=object(), _active_binding=None
        ),
    )

    def retain(task):
        brain._background_tasks.add(task)
        task.add_done_callback(brain._background_tasks.discard)

    brain.register_background_task = retain
    attach_agent_dispatch_lease(brain)
    intake = PhoneChatIntake(brain, socket, lambda: brain)
    intake.node_id = "phone"
    effects = []

    async def inert_effect(*args):
        effects.append("new backing effect")
        return {"success": True, "data": {"nonce": "fixture"}}

    monkeypatch.setattr(
        impl, "get_implementation", lambda _: SimpleNamespace(execute=inert_effect)
    )
    monkeypatch.setattr(state_module, "state", brain)
    executor = SkillExecutor()
    executor._record_audit = AsyncMock()
    orch.executor = executor
    original = orch.tool_runner.approved_backing_transfer

    def invalidate():
        if owner == "phone":
            intake.stop()
        else:
            brain._native_agent_turn_generation += 1

    def schedule_invalidation(*args):
        transfer = original(*args)
        asyncio.get_running_loop().call_soon(invalidate)
        return transfer

    monkeypatch.setattr(
        orch.tool_runner, "approved_backing_transfer", schedule_invalidation
    )

    async def operation():
        await action(orch, "phone-session", endpoint="search_notes")

    try:
        assert intake.submit("phone-session", operation)
        results = await asyncio.gather(*tuple(intake.tasks), return_exceptions=True)
        assert effects == []
        assert len(results) == 1 and isinstance(results[0], asyncio.CancelledError)
        assert brain._native_agent_turn_generation == (7 if owner == "phone" else 8)
    finally:
        await intake.drain()
        await executor.client.aclose()


@pytest.mark.parametrize("owner", ["phone", "native"])
@pytest.mark.parametrize(
    "path,command", [("shell", "echo fixture"), ("applescript", 'return "fixture"')]
)
def test_queued_local_daemon_rechecks_owner_in_thread(
    monkeypatch, owner, path, command
):
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace
    from api.phone_chat_intake import PhoneChatIntake
    from security.sandbox_policy import SandboxPolicy
    from skills.executor import SkillExecutor
    import subprocess

    policy = SimpleNamespace(
        validate_shell_command=lambda _: (True, ""),
        validate_applescript=lambda _: (True, ""),
    )
    monkeypatch.setattr(SandboxPolicy, "load_default", lambda: policy)
    effect = MagicMock(
        return_value=SimpleNamespace(stdout="fixture", stderr="", returncode=0)
    )
    monkeypatch.setattr(subprocess, "run", effect)

    async def exercise():
        loop = asyncio.get_running_loop()
        pool = ThreadPoolExecutor(max_workers=1)
        loop.set_default_executor(pool)
        release, occupied = threading.Event(), threading.Event()

        def hold():
            occupied.set()
            assert release.wait(3)

        blocked = pool.submit(hold)
        assert occupied.wait(1)
        queued = asyncio.Event()
        original = loop.run_in_executor

        def capture_queue(executor, func, *args):
            future = original(executor, func, *args)
            queued.set()
            return future

        monkeypatch.setattr(loop, "run_in_executor", capture_queue)
        socket = object()
        brain = SimpleNamespace(
            orchestrator=object(),
            memory=object(),
            daemons={"phone": socket},
            _background_tasks=set(),
            _native_vault_deferred=True,
            _native_bootstrap_required=False,
            _native_agent_turn_generation=7,
            agent_bootstrap_controller=SimpleNamespace(
                _completed=object(), _active_binding=None
            ),
        )

        def retain(task):
            brain._background_tasks.add(task)
            task.add_done_callback(brain._background_tasks.discard)

        brain.register_background_task = retain
        intake = PhoneChatIntake(brain, socket, lambda: brain)
        intake.node_id = "phone"

        async def operation():
            await SkillExecutor._execute_local_daemon(path, command)

        try:
            assert intake.submit("phone-session", operation)
            tasks = tuple(intake.tasks)
            await asyncio.wait_for(queued.wait(), 1)
            # Do not cancel the queued Future; exercise the callable's guard.
            if owner == "phone":
                brain.orchestrator = object()
            else:
                brain._native_agent_turn_generation += 1
            release.set()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            assert len(results) == 1 and isinstance(results[0], asyncio.CancelledError)
            effect.assert_not_called()
        finally:
            release.set()
            blocked.result(timeout=2)
            await intake.drain()

    asyncio.run(exercise())
