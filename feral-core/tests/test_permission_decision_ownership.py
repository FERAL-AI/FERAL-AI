"""Folder permission decisions bind to their owner and actual policy outcome."""
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from agents.ui_handlers import handle_permission_response, send_permission_request, handle_ui_event
from models.protocol import PermissionDecisionPayload
from security.sandbox_policy import SandboxPolicy


def owner(operation="write", **extra):
    return SimpleNamespace(
        _pending_permission_requests={"request": {
            "session_id": "owner", "path": "/disposable/project", "operation": operation,
            "expires_at": time.time() + 300, **extra,
        }}, send=AsyncMock(), _send_text=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_foreign_session_cannot_grant_or_consume(monkeypatch):
    orch = owner()
    load = Mock()
    monkeypatch.setattr(SandboxPolicy, "load_default", load)
    await handle_permission_response(orch, "foreign", "request", True)
    await handle_permission_response(orch, "foreign", "request", False)
    assert "request" in orch._pending_permission_requests
    load.assert_not_called()
    orch.send.assert_not_awaited()
    orch._send_text.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,mode", [("read", "read"), ("write", "readwrite"), ("readwrite", "readwrite")])
async def test_grant_receipt_and_replay_bind_actual_mode(monkeypatch, operation, mode):
    orch = owner(operation)
    policy = SimpleNamespace(grant_folder=Mock(return_value={"ok": True, "path": "/disposable/project", "mode": mode}))
    monkeypatch.setattr(SandboxPolicy, "load_default", lambda: policy)
    await handle_permission_response(orch, "owner", "request", True)
    await handle_permission_response(orch, "owner", "request", True)
    policy.grant_folder.assert_called_once_with("/disposable/project", mode=mode)
    assert orch._pending_permission_requests == {}
    assert orch.send.await_count == 1
    frame = orch.send.await_args.args[1]
    receipt = PermissionDecisionPayload(**frame.payload)
    assert frame.session_id == "owner" and receipt.status == "granted"
    assert receipt.mode == mode and receipt.scope == "persistent_workspace"
    assert "Persistent workspace" in orch._send_text.await_args.args[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [{"ok": False, "error": "private policy detail"}, None, {"ok": True, "mode": "wrong"}])
async def test_failed_policy_never_reports_success_or_echoes_error(monkeypatch, result):
    orch = owner()
    monkeypatch.setattr(SandboxPolicy, "load_default", lambda: SimpleNamespace(grant_folder=Mock(return_value=result)))
    await handle_permission_response(orch, "owner", "request", True)
    assert orch.send.await_args.args[1].payload == {"request_id": "request", "status": "error"}
    reply = orch._send_text.await_args.args[1]
    assert "Access granted" not in reply and "private policy detail" not in reply


@pytest.mark.asyncio
async def test_policy_exception_has_uncertain_receipt(monkeypatch):
    orch = owner()
    monkeypatch.setattr(SandboxPolicy, "load_default", lambda: SimpleNamespace(grant_folder=Mock(side_effect=OSError("private"))))
    await handle_permission_response(orch, "owner", "request", True)
    assert orch.send.await_args.args[1].payload["status"] == "error"
    assert "private" not in orch._send_text.await_args.args[1]


@pytest.mark.asyncio
async def test_expired_and_unsupported_never_touch_policy(monkeypatch):
    load = Mock()
    monkeypatch.setattr(SandboxPolicy, "load_default", load)
    for orch, expected in [(owner(expires_at=0), "expired"), (owner("execute"), "error")]:
        await handle_permission_response(orch, "owner", "request", True)
        assert orch.send.await_args.args[1].payload["status"] == expected
        assert not orch._pending_permission_requests
    load.assert_not_called()


@pytest.mark.asyncio
async def test_denial_is_single_use_and_never_grants(monkeypatch):
    orch = owner()
    load = Mock()
    monkeypatch.setattr(SandboxPolicy, "load_default", load)
    await handle_permission_response(orch, "owner", "request", False)
    await handle_permission_response(orch, "owner", "request", True)
    assert orch.send.await_count == 1 and orch.send.await_args.args[1].payload["status"] == "denied"
    load.assert_not_called()


@pytest.mark.asyncio
async def test_request_declares_expiry_persistent_scope_and_strong_id():
    orch = owner()
    orch._pending_permission_requests.clear()
    await send_permission_request(orch, "owner", "/disposable/project", "readwrite")
    frame = orch.send.await_args.args[1]
    payload = frame.payload
    assert len(payload["request_id"]) == 36
    assert time.time() < payload["expires_at"] <= time.time() + 300
    assert payload["scope"] == "persistent_workspace"
    assert orch._pending_permission_requests[payload["request_id"]]["session_id"] == "owner"


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["confirm_", "reject_"])
async def test_foreign_generic_confirmation_cannot_consume_or_execute(prefix):
    orch = owner()
    orch._pending_confirmations = {"action": {"session_id": "owner", "tool_call": {"name": "disposable", "args": {}}, "skills": []}}
    orch._execute_tool_call = AsyncMock()
    await handle_ui_event(orch, "foreign", prefix + "action", "tap")
    assert "action" in orch._pending_confirmations
    orch._execute_tool_call.assert_not_awaited()
    orch._send_text.assert_not_awaited()
    await handle_ui_event(orch, "owner", prefix + "action", "tap")
    await handle_ui_event(orch, "owner", prefix + "action", "tap")
    assert not orch._pending_confirmations
    assert orch._execute_tool_call.await_count == (1 if prefix == "confirm_" else 0)
    assert orch._send_text.await_count == (1 if prefix == "reject_" else 0)
