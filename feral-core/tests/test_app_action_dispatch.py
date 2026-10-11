"""Tests for handle_ui_event(app_id=...) — app-scoped action dispatch.

These exercise `feral-core/agents/ui_handlers.py::_handle_app_action`
against a real `AppRegistry` with a faked orchestrator so the scoping
+ contract validation paths are proven end-to-end.
"""

from __future__ import annotations

from pathlib import Path
import time
from uuid import UUID
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.app_registry import AppRegistry, HybridGenerator
from agents.ui_handlers import handle_ui_event
from models.app_manifest import ActionSpec, AppManifest, SurfaceSpec
from models.skill_manifest import BrandProfile
from models.protocol import parse_message


def _collect_action_ids(node) -> list[str]:
    out: list[str] = []
    if isinstance(node, dict):
        action_id = node.get("action_id")
        if isinstance(action_id, str) and action_id:
            out.append(action_id)
        for value in node.values():
            out.extend(_collect_action_ids(value))
    elif isinstance(node, list):
        for value in node:
            out.extend(_collect_action_ids(value))
    return out


def _build_manifest() -> AppManifest:
    return AppManifest(
        app_id="demo-app",
        brand=BrandProfile(name="Demo"),
        surfaces=[
            SurfaceSpec(
                surface_id="home",
                kind="authored",
                template_root={"type": "Text", "value": "home"},
                action_contract=[
                    ActionSpec(action_id="open_thread", handler="navigate", target="thread"),
                    ActionSpec(action_id="send", handler="app_event"),
                    ActionSpec(action_id="run_tool", handler="skill_call", target="demo_skill/ping"),
                    ActionSpec(action_id="close_modal", handler="close"),
                    ActionSpec(action_id="bump", handler="patch"),
                    ActionSpec(action_id="danger", handler="app_event", requires_confirmation=True),
                ],
            ),
            SurfaceSpec(
                surface_id="thread",
                kind="authored",
                template_root={"type": "Text", "value": "thread"},
                action_contract=[],
            ),
        ],
        entry_surface_id="home",
    )


@pytest.fixture
def registry(tmp_path):
    reg = AppRegistry(
        db_path=str(tmp_path / "apps.db"),
        apps_dir=tmp_path / "apps",
    )
    reg.set_hybrid_generator(HybridGenerator(cache_dir=tmp_path / "cache"))
    src = tmp_path / "src"
    src.mkdir()
    (src / "manifest.json").write_text(_build_manifest().model_dump_json())
    reg.install_from_dir(src)
    return reg


@pytest.fixture
def orchestrator():
    mock = MagicMock()
    mock._send_text = AsyncMock()
    mock._execute_tool_call = AsyncMock()
    mock.handle_command = AsyncMock()
    mock.send = AsyncMock(return_value=True)
    mock._pending_confirmations = {}
    return mock


@pytest.mark.asyncio
async def test_unknown_app_replies_polite_not_handle_command(orchestrator):
    mock_state = MagicMock()
    mock_state.app_registry = None
    with patch("api.state.state", mock_state):
        await handle_ui_event(
            orchestrator,
            session_id="s1",
            action_id="send",
            event="tap",
            app_id="whatever",
        )
    orchestrator._send_text.assert_awaited_once()
    orchestrator.handle_command.assert_not_called()


@pytest.mark.asyncio
async def test_uninstalled_app_replies_polite(orchestrator):
    mock_state = MagicMock()
    # registry exists but `get` returns None.
    reg = MagicMock()
    reg.get.return_value = None
    mock_state.app_registry = reg
    with patch("api.state.state", mock_state):
        await handle_ui_event(
            orchestrator,
            session_id="s1",
            action_id="send",
            event="tap",
            app_id="ghost",
        )
    orchestrator._send_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_unknown_action_rejected(registry, orchestrator):
    mock_state = MagicMock()
    mock_state.app_registry = registry
    with patch("api.state.state", mock_state):
        await handle_ui_event(
            orchestrator,
            session_id="s1",
            action_id="evil_action",
            event="tap",
            app_id="demo-app",
            screen_id="demo-app:home:s1",
        )
    orchestrator._send_text.assert_awaited_once()
    orchestrator.handle_command.assert_not_called()
    orchestrator._execute_tool_call.assert_not_called()


@pytest.mark.asyncio
async def test_navigate_action_pushes_sdui(registry, orchestrator):
    mock_state = MagicMock()
    mock_state.app_registry = registry
    with patch("api.state.state", mock_state):
        await handle_ui_event(
            orchestrator,
            session_id="s1",
            action_id="open_thread",
            event="tap",
            app_id="demo-app",
            screen_id="demo-app:home:s1",
        )
    orchestrator.send.assert_awaited_once()
    msg = orchestrator.send.await_args.args[1]
    assert msg.type == "sdui"
    assert msg.payload["screen_id"].startswith("demo-app:thread:")
    assert msg.payload["root"]["value"] == "thread"


@pytest.mark.asyncio
async def test_skill_call_routes_to_tool_executor(registry, orchestrator):
    mock_state = MagicMock()
    mock_state.app_registry = registry
    with patch("api.state.state", mock_state):
        await handle_ui_event(
            orchestrator,
            session_id="s1",
            action_id="run_tool",
            event="tap",
            value={"foo": "bar"},
            app_id="demo-app",
            screen_id="demo-app:home:s1",
        )
    orchestrator._execute_tool_call.assert_awaited_once()
    call_args = orchestrator._execute_tool_call.await_args
    tool_call = call_args.args[1]
    assert tool_call["name"] == "demo_skill/ping"
    assert tool_call["args"] == {"foo": "bar"}


@pytest.mark.asyncio
async def test_app_event_falls_through_to_handle_command(registry, orchestrator):
    mock_state = MagicMock()
    mock_state.app_registry = registry
    with patch("api.state.state", mock_state):
        await handle_ui_event(
            orchestrator,
            session_id="s1",
            action_id="send",
            event="tap",
            value={"text": "hi"},
            app_id="demo-app",
            screen_id="demo-app:home:s1",
        )
    orchestrator.handle_command.assert_awaited_once()
    prompt = orchestrator.handle_command.await_args.args[1]
    assert "demo-app" in prompt and "send" in prompt


@pytest.mark.asyncio
async def test_close_action_acks_without_skill_call(registry, orchestrator):
    mock_state = MagicMock()
    mock_state.app_registry = registry
    with patch("api.state.state", mock_state):
        await handle_ui_event(
            orchestrator,
            session_id="s1",
            action_id="close_modal",
            event="tap",
            app_id="demo-app",
            screen_id="demo-app:home:s1",
        )
    orchestrator._send_text.assert_awaited_once()
    orchestrator._execute_tool_call.assert_not_called()


@pytest.mark.asyncio
async def test_patch_action_logs_and_noops(registry, orchestrator):
    mock_state = MagicMock()
    mock_state.app_registry = registry
    with patch("api.state.state", mock_state):
        await handle_ui_event(
            orchestrator,
            session_id="s1",
            action_id="bump",
            event="tap",
            app_id="demo-app",
            screen_id="demo-app:home:s1",
            value={"patches": [{"path": "/value", "op": "replace", "value": "after"}]},
        )
    orchestrator.send.assert_awaited_once()
    msg = orchestrator.send.await_args.args[1]
    assert msg.type == "sdui_patch"
    assert msg.payload["screen_id"] == "demo-app:home:s1"
    assert msg.payload["patches"][0]["op"] == "replace"
    orchestrator._execute_tool_call.assert_not_called()
    orchestrator.handle_command.assert_not_called()


@pytest.mark.asyncio
async def test_app_path_does_not_trigger_legacy_call_prefix_routing(registry, orchestrator):
    """call_ prefix is a first-party skill shortcut; must NOT hijack app events."""
    mock_state = MagicMock()
    mock_state.app_registry = registry
    with patch("api.state.state", mock_state):
        await handle_ui_event(
            orchestrator,
            session_id="s1",
            action_id="call_demo_skill/ping",
            event="tap",
            app_id="demo-app",
            screen_id="demo-app:home:s1",
        )
    # Action 'call_demo_skill/ping' is not in the surface contract;
    # the app path rejects, never calls _execute_tool_call.
    orchestrator._execute_tool_call.assert_not_called()
    orchestrator._send_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_non_app_event_preserves_legacy_prefix_routing(orchestrator):
    """Without app_id, the legacy call_ prefix still dispatches."""
    mock_state = MagicMock()
    mock_state.app_registry = None
    with patch("api.state.state", mock_state):
        await handle_ui_event(
            orchestrator,
            session_id="s1",
            action_id="call_demo_skill/ping",
            event="tap",
        )
    orchestrator._execute_tool_call.assert_awaited_once()


@pytest.mark.asyncio
async def test_requires_confirmation_for_app_action(registry, orchestrator):
    mock_state = MagicMock()
    mock_state.app_registry = registry
    with patch("api.state.state", mock_state):
        await handle_ui_event(
            orchestrator,
            session_id="s1",
            action_id="danger",
            event="tap",
            value={"x": 1},
            app_id="demo-app",
            screen_id="demo-app:home:s1",
        )
        orchestrator.handle_command.assert_not_called()
        orchestrator.send.assert_awaited_once()
        confirm_msg = orchestrator.send.await_args.args[1]
        confirm_action_id = next(
            aid for aid in _collect_action_ids(confirm_msg.payload["root"])
            if aid.startswith("confirm_")
        )
        await handle_ui_event(
            orchestrator,
            session_id="s1",
            action_id=confirm_action_id,
            event="tap",
            app_id="demo-app",
            screen_id="demo-app:home:s1",
        )
    orchestrator.handle_command.assert_awaited_once()
    receipt = orchestrator.send.await_args.args[1]
    assert receipt.type == "confirmation_decision"
    assert receipt.payload["status"] == "accepted"
    assert receipt.payload["dispatch_accepted"] is True
    assert receipt.payload["tool_outcome_verified"] is False


async def _request_confirmation(registry, orchestrator):
    mock_state = MagicMock()
    mock_state.app_registry = registry
    mock_state._daemon_session_bindings = {}
    with patch("api.state.state", mock_state):
        await handle_ui_event(orchestrator, session_id="s1", action_id="danger", event="tap",
                              value={"reviewed": "fixture"}, app_id="demo-app", screen_id="demo-app:home:s1")
    message = orchestrator.send.await_args.args[1]
    request_id = next(key for key in _collect_action_ids(message.payload["root"]) if key.startswith("confirm_"))[8:]
    return mock_state, request_id, message


@pytest.mark.asyncio
async def test_authoritative_app_confirmation_metadata_and_protocol(registry, orchestrator):
    _, request_id, message = await _request_confirmation(registry, orchestrator)
    assert str(UUID(request_id)) == request_id
    assert "confirmation" not in message.payload["root"]
    metadata = message.payload["confirmation"]
    assert metadata["contract_version"] == 1
    assert metadata["session_id"] == message.session_id == "s1"
    assert metadata["app_id"] == "demo-app"
    assert metadata["surface_id"] == "home"
    assert metadata["action_id"] == "danger"
    assert metadata["screen_id"] == "demo-app:home:s1"
    assert metadata["handler"] == "app_event"
    assert metadata["value"] == {"reviewed": "fixture"}
    assert metadata["requires_confirmation"] is True
    assert metadata["expires_at"] - metadata["created_at"] == 300
    _, parsed = parse_message(message.model_dump())
    assert parsed.confirmation.request_id == request_id


@pytest.mark.asyncio
async def test_foreign_replay_and_reject_owning_app_confirmation(registry, orchestrator):
    mock_state, request_id, _ = await _request_confirmation(registry, orchestrator)
    pending = orchestrator._pending_confirmations[request_id]
    with patch("api.state.state", mock_state):
        await handle_ui_event(orchestrator, session_id="foreign", action_id="confirm_" + request_id, event="tap")
        assert orchestrator._pending_confirmations[request_id] is pending
        assert orchestrator.send.await_count == 1
        await handle_ui_event(orchestrator, session_id="s1", action_id="reject_" + request_id, event="tap")
        decision = orchestrator.send.await_args.args[1]
        assert decision.session_id == "s1"
        assert decision.payload["request_id"] == request_id
        assert decision.payload["status"] == "rejected"
        assert decision.payload["dispatch_accepted"] is False
        _, parsed = parse_message(decision.model_dump())
        assert parsed.scope == "app_action"
        await handle_ui_event(orchestrator, session_id="s1", action_id="confirm_" + request_id, event="tap")
        assert orchestrator.send.await_count == 2
    orchestrator.handle_command.assert_not_called()
    assert not orchestrator._pending_confirmations


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("created_at",None),("created_at",True),("created_at",float("nan")),
                                         ("expires_at",None),("expires_at",float("inf")),("action_spec",None),("manifest_snapshot",None)])
async def test_malformed_new_app_confirmation_fails_closed(registry, orchestrator, field, value):
    mock_state, request_id, _ = await _request_confirmation(registry, orchestrator)
    orchestrator._pending_confirmations[request_id][field] = value
    with patch("api.state.state", mock_state):
        await handle_ui_event(orchestrator, session_id="s1", action_id="confirm_" + request_id, event="tap")
    assert orchestrator.send.await_args.args[1].payload["status"] == "error"
    assert request_id not in orchestrator._pending_confirmations
    orchestrator.handle_command.assert_not_called()


class _TimestampInt(int):
    pass


class _TimestampFloat(float):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    pytest.param("created_at", "0", id="string-created"),
    pytest.param("expires_at", "300", id="string-expiry"),
    pytest.param("created_at", False, id="bool-created"),
    pytest.param("expires_at", True, id="bool-expiry"),
    pytest.param("created_at", _TimestampInt(0), id="int-subclass-created"),
    pytest.param("expires_at", _TimestampInt(300), id="int-subclass-expiry"),
    pytest.param("created_at", _TimestampFloat(0), id="float-subclass-created"),
    pytest.param("expires_at", _TimestampFloat(300), id="float-subclass-expiry"),
    pytest.param("created_at", 10 ** 1000, id="oversized-created"),
    pytest.param("expires_at", 10 ** 1000, id="oversized-expiry"),
])
async def test_confirmation_timestamp_types_and_overflow_refuse_once(
    registry, orchestrator, monkeypatch, field, value,
):
    # At time zero, False/True and numeric subclasses would otherwise describe
    # a live bounded window. This distinguishes strict type rejection from an
    # unrelated expired/future timestamp refusal.
    monkeypatch.setattr("agents.ui_handlers.time.time", lambda: 0)
    mock_state, request_id, _ = await _request_confirmation(registry, orchestrator)
    pending = orchestrator._pending_confirmations[request_id]
    pending[field] = value
    with patch("api.state.state", mock_state):
        await handle_ui_event(orchestrator, session_id="foreign", action_id="confirm_" + request_id, event="tap")
        assert orchestrator._pending_confirmations[request_id] is pending
        assert orchestrator.send.await_count == 1
        await handle_ui_event(orchestrator, session_id="s1", action_id="confirm_" + request_id, event="tap")
        receipt = orchestrator.send.await_args.args[1]
        assert receipt.payload["status"] == "error"
        assert receipt.payload["dispatch_accepted"] is False
        assert receipt.payload["tool_outcome_verified"] is False
        assert request_id not in orchestrator._pending_confirmations
        await handle_ui_event(orchestrator, session_id="s1", action_id="confirm_" + request_id, event="tap")
        assert orchestrator.send.await_count == 2
    orchestrator.handle_command.assert_not_called()
    orchestrator._execute_tool_call.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("created,expiry,now,status", [
    pytest.param(0, 300.0, 0, "accepted", id="exact-300-second-window"),
    pytest.param(1, 301, 0, "error", id="future-created"),
    pytest.param(0, 301, 0, "error", id="overlong-window"),
    pytest.param(0, 0, 0, "error", id="empty-window"),
    pytest.param(0, -1, 0, "error", id="reversed-window"),
    pytest.param(0, 300, 300, "expired", id="exact-expiry"),
    pytest.param(-1e308, 1e308, 0, "error", id="unbounded-finite-gap"),
])
async def test_confirmation_timestamp_window_boundaries(
    registry, orchestrator, monkeypatch, created, expiry, now, status,
):
    monkeypatch.setattr("agents.ui_handlers.time.time", lambda: now)
    mock_state, request_id, _ = await _request_confirmation(registry, orchestrator)
    pending = orchestrator._pending_confirmations[request_id]
    pending["created_at"], pending["expires_at"] = created, expiry
    with patch("api.state.state", mock_state):
        await handle_ui_event(orchestrator, session_id="s1", action_id="confirm_" + request_id, event="tap")
        receipt = orchestrator.send.await_args.args[1]
        assert receipt.payload["status"] == status
        assert receipt.payload["dispatch_accepted"] is (status == "accepted")
        assert receipt.payload["tool_outcome_verified"] is False
        assert request_id not in orchestrator._pending_confirmations
        await handle_ui_event(orchestrator, session_id="s1", action_id="confirm_" + request_id, event="tap")
        assert orchestrator.send.await_count == 2
    if status == "accepted":
        orchestrator.handle_command.assert_awaited_once()
    else:
        orchestrator.handle_command.assert_not_called()
    orchestrator._execute_tool_call.assert_not_called()


@pytest.mark.asyncio
async def test_expired_confirmation_never_dispatches(registry, orchestrator):
    mock_state, request_id, _ = await _request_confirmation(registry, orchestrator)
    now = time.time()
    orchestrator._pending_confirmations[request_id]["created_at"] = now - 400
    orchestrator._pending_confirmations[request_id]["expires_at"] = now - 100
    with patch("api.state.state", mock_state):
        await handle_ui_event(orchestrator, session_id="s1", action_id="confirm_" + request_id, event="tap")
    assert orchestrator.send.await_args.args[1].payload["status"] == "expired"
    orchestrator.handle_command.assert_not_called()


@pytest.mark.asyncio
async def test_action_contract_drift_never_dispatches_reviewed_confirmation(registry, orchestrator, tmp_path):
    mock_state, request_id, _ = await _request_confirmation(registry, orchestrator)
    # get() decodes a fresh SQLite record; mutating its return object does
    # not alter the installed contract. Exercise a real registry update.
    changed = registry.get("demo-app").manifest
    changed.surfaces[0].action_contract[-1].handler = "skill_call"
    changed.surfaces[0].action_contract[-1].target = "unexpected/privileged"
    update = tmp_path / "contract-update"
    update.mkdir()
    (update / "manifest.json").write_text(changed.model_dump_json())
    registry.install_from_dir(update)
    assert registry.validate_action("demo-app", "home", "danger").target == "unexpected/privileged"
    assert orchestrator._pending_confirmations[request_id]["action_spec"]["handler"] == "app_event"
    with patch("api.state.state", mock_state):
        await handle_ui_event(orchestrator, session_id="s1", action_id="confirm_" + request_id, event="tap")
    assert orchestrator.send.await_args.args[1].payload["status"] == "error"
    orchestrator.handle_command.assert_not_called()
    orchestrator._execute_tool_call.assert_not_called()


@pytest.mark.asyncio
async def test_app_handler_exception_reports_unverified_error(registry, orchestrator):
    mock_state, request_id, _ = await _request_confirmation(registry, orchestrator)
    orchestrator.handle_command.side_effect = RuntimeError("private-fixture-error")
    with patch("api.state.state", mock_state):
        await handle_ui_event(orchestrator, session_id="s1", action_id="confirm_" + request_id, event="tap")
    receipt = orchestrator.send.await_args.args[1]
    assert receipt.payload["status"] == "error"
    assert receipt.payload["dispatch_accepted"] is False
    assert receipt.payload["tool_outcome_verified"] is False
    assert "private-fixture-error" not in str(receipt.payload)


@pytest.mark.asyncio
async def test_legacy_generic_confirmation_compatibility_with_ownership(orchestrator):
    orchestrator._pending_confirmations["legacy"] = {"session_id":"s1", "tool_call":{"name":"fixture/read","args":{}}}
    await handle_ui_event(orchestrator, session_id="foreign", action_id="confirm_legacy", event="tap")
    assert "legacy" in orchestrator._pending_confirmations
    await handle_ui_event(orchestrator, session_id="s1", action_id="confirm_legacy", event="tap")
    await handle_ui_event(orchestrator, session_id="s1", action_id="confirm_legacy", event="tap")
    orchestrator._execute_tool_call.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", [False, None])
async def test_undelivered_app_confirmation_withdrawn(registry, orchestrator, delivery):
    mock_state = MagicMock()
    mock_state.app_registry = registry
    mock_state._daemon_session_bindings = {}
    orchestrator.send.return_value = delivery
    with patch("api.state.state", mock_state):
        await handle_ui_event(orchestrator, session_id="s1", action_id="danger",
                              event="tap", app_id="demo-app", screen_id="demo-app:home:s1")
        confirm_msg = orchestrator.send.await_args.args[1]
        action = next(a for a in _collect_action_ids(confirm_msg.payload["root"]) if a.startswith("confirm_"))
        assert orchestrator._pending_confirmations == {}
        await handle_ui_event(orchestrator, session_id="s1", action_id=action, event="tap")
    orchestrator.handle_command.assert_not_called()
    orchestrator._execute_tool_call.assert_not_called()


@pytest.mark.asyncio
async def test_app_confirmation_delivery_exception_best_effort(registry, orchestrator):
    mock_state = MagicMock()
    mock_state.app_registry = registry
    orchestrator.send.side_effect = RuntimeError("fixture transport down")
    orchestrator._send_text.side_effect = RuntimeError("fixture notice also down")
    with patch("api.state.state", mock_state):
        await handle_ui_event(orchestrator, session_id="s1", action_id="danger",
                              event="tap", app_id="demo-app", screen_id="demo-app:home:s1")
    assert orchestrator._pending_confirmations == {}
    orchestrator.handle_command.assert_not_called()
    orchestrator._execute_tool_call.assert_not_called()


@pytest.mark.asyncio
async def test_failed_delivery_preserves_replaced_pending_entry(registry, orchestrator):
    mock_state = MagicMock()
    mock_state.app_registry = registry
    mock_state._daemon_session_bindings = {}
    replacement = {"session_id": "foreign", "created_at": 123}
    async def replace_during_send(session_id, message):
        action = next(a for a in _collect_action_ids(message.payload["root"]) if a.startswith("confirm_"))
        orchestrator._pending_confirmations[action[8:]] = replacement
        return False
    orchestrator.send.side_effect = replace_during_send
    with patch("api.state.state", mock_state):
        await handle_ui_event(orchestrator, session_id="s1", action_id="danger",
                              event="tap", app_id="demo-app", screen_id="demo-app:home:s1")
    assert list(orchestrator._pending_confirmations.values()) == [replacement]
    orchestrator.handle_command.assert_not_called()


@pytest.mark.asyncio
async def test_unverified_legacy_node_relay_cannot_authorize_confirmation(registry, orchestrator):
    mock_state = MagicMock()
    mock_state.app_registry = registry
    mock_state._daemon_session_bindings = {"phone": {"s1"}}
    mock_state.send_to_daemon = AsyncMock(return_value=None)
    orchestrator.send.return_value = False
    with patch("api.state.state", mock_state):
        await handle_ui_event(orchestrator, session_id="s1", action_id="danger",
                              event="tap", app_id="demo-app", screen_id="demo-app:home:s1")
    mock_state.send_to_daemon.assert_awaited_once()
    assert orchestrator._pending_confirmations == {}
    orchestrator.handle_command.assert_not_called()


@pytest.mark.asyncio
async def test_navigate_action_relays_genui_push_to_bound_phone(registry, orchestrator):
    mock_state = MagicMock()
    mock_state.app_registry = registry
    mock_state._daemon_session_bindings = {"phone-node-1": {"s1"}}
    mock_state.send_to_daemon = AsyncMock()
    with patch("api.state.state", mock_state):
        await handle_ui_event(
            orchestrator,
            session_id="s1",
            action_id="open_thread",
            event="tap",
            app_id="demo-app",
            screen_id="demo-app:home:s1",
        )
    mock_state.send_to_daemon.assert_awaited_once()
    daemon_msg = mock_state.send_to_daemon.await_args.args[1]
    assert daemon_msg.type == "genui_push"
    assert daemon_msg.payload["kind"] == "interactive"
    assert daemon_msg.payload["app_id"] == "demo-app"
