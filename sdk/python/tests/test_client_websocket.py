"""SDK tracked-turn contracts. Fake wire plus registered-server integration."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "sdk/python"))
sys.path.insert(0, str(REPO / "feral-core"))
from feral_sdk import FeralClient, ChatTurnError, ChatTurnTimeout, TurnProcessingOutcome


@pytest.fixture(autouse=True)
def isolate_sdk_runtime(monkeypatch, tmp_path):
    # SDK tests can run outside feral-core's conftest tree. Always fence
    # module-level BrainState creation before importing registered handlers.
    monkeypatch.setenv("FERAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FERAL_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    from security import vault
    keys = {}
    monkeypatch.setattr(vault, "_keyring_get_password", lambda service, user: keys.get((service, user)))
    monkeypatch.setattr(vault, "_keyring_set_password", lambda service, user, value: keys.__setitem__((service, user), value))
    monkeypatch.setattr(vault, "_keyring_delete_password", lambda service, user: keys.pop((service, user), None))
    vault.reset_vault()
    yield
    vault.reset_vault()


def run(coroutine):
    return asyncio.run(coroutine)


class Wire:
    def __init__(self, *, command=None, capability=None):
        self.command = command or self.success
        self.capability = capability
        self.sent = []
        self.closed = False
        self.queue = asyncio.Queue()
        self.uri = ""
        self.sid = ""
        self.turn_id = str(uuid4())
        self.kwargs = {}

    def dial(self, uri, **kwargs):
        self.uri, self.kwargs = uri, kwargs
        self.sid = parse_qs(urlsplit(uri).query)["session_id"][0]
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def close(self):
        self.closed = True

    def emit(self, kind, payload=None, **fields):
        frame = {"type": kind, "session_id": self.sid, "payload": payload or {}, **fields}
        self.queue.put_nowait(json.dumps(frame))

    def acceptance(self, command, **changes):
        payload = {"contract_version": 1, "request_id": command["msg_id"], "turn_id": self.turn_id,
                   "session_id": self.sid, "status": "accepted", "durable": True, "replayed": False, **changes}
        self.emit("chat_turn_accepted", payload)

    def terminal(self, command, **changes):
        payload = {"contract_version": 1, "request_id": command["msg_id"], "turn_id": self.turn_id,
                   "session_id": self.sid, "processing_outcome": "completed", "final_text": "42",
                   "action_outcome": "not_asserted", "approval_request_ids": [], "durable": True,
                   "replayed": False, **changes}
        self.emit("chat_turn_terminal", payload)

    def success(self, command):
        self.acceptance(command)
        # These frames must not resolve chat; both can precede effects/retries.
        self.emit("text_response", {"text": "Please approve this purchase"})
        self.emit("stream_delta", {"delta": "not final task", "stream_id": "round1", "is_final": True})
        self.terminal(command)

    async def send(self, text):
        frame = json.loads(text)
        self.sent.append(frame)
        if frame["type"] == "req":
            if self.capability is not None:
                self.capability(frame)
            else:
                self.emit("res", {"turn_contract_versions": [1], "durable_receipts": True,
                                  "whole_turn_terminal": True, "session_id": self.sid}, id=frame["id"], ok=True)
        elif frame["type"] == "text_command":
            self.command(frame)

    async def recv(self):
        item = await self.queue.get()
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.fixture
def wire(monkeypatch):
    import websockets
    result = Wire()
    monkeypatch.setattr(websockets, "connect", result.dial)
    return result


def commands(wire):
    return [frame for frame in wire.sent if frame["type"] == "text_command"]


def test_auth_negotiation_identity_and_exact_terminal_ignore_prose(wire):
    async def scenario():
        async with FeralClient("https://fixture.invalid/proxy", bearer_token="fixture-secret") as client:
            assert await client.chat("31 + 11", session_id="project/+&二") == "42"
    run(scenario())
    assert wire.uri == "wss://fixture.invalid/proxy/v1/session?session_id=project%2F%2B%26%E4%BA%8C"
    assert "fixture-secret" not in wire.uri
    assert [frame["type"] for frame in wire.sent] == ["auth", "req", "text_command"]
    assert wire.sent[0] == {"type": "auth", "token": "fixture-secret"}
    assert commands(wire)[0]["payload"] == {"text": "31 + 11", "turn_contract_version": 1}
    assert wire.kwargs["logger"].disabled is True
    assert wire.closed


@pytest.mark.parametrize("outcome", [item.value for item in TurnProcessingOutcome if item != TurnProcessingOutcome.COMPLETED])
def test_typed_failure_and_pending_receipt_never_becomes_chat_success(wire, outcome):
    def command(frame):
        wire.acceptance(frame)
        wire.terminal(frame, processing_outcome=outcome, final_text="Review required", approval_request_ids=["review-fixture"])
    wire.command = command

    async def scenario():
        async with FeralClient(chat_timeout=1) as client:
            receipt = await client.chat_turn("fixture")
            assert receipt.processing_outcome == outcome
            assert receipt.action_outcome == "not_asserted"
            assert receipt.approval_request_ids == ("review-fixture",)
            with pytest.raises(ChatTurnError) as error:
                await client.chat("fixture")
            assert error.value.code == outcome
            assert error.value.receipt.processing_outcome == outcome
    run(scenario())
    assert len(commands(wire)) == 2  # Separate explicit caller invocations, never SDK retries.


@pytest.mark.parametrize("mode", ["unknown_method", "greeting_only", "malformed", "wrong_session", "missing_whole_turn"])
def test_legacy_negotiation_refuses_before_any_user_command(wire, mode):
    def capability(frame):
        if mode == "unknown_method":
            wire.emit("res", id=frame["id"], ok=False, error={"code": "METHOD_NOT_FOUND", "message": "private"})
        elif mode == "greeting_only":
            wire.emit("text_response", {"text": "How can I help?"})
        else:
            payload = {"turn_contract_versions": [1], "durable_receipts": True,
                       "whole_turn_terminal": True, "session_id": wire.sid}
            if mode == "malformed":
                payload["turn_contract_versions"] = [True]
            elif mode == "wrong_session":
                payload["session_id"] = "other-thread"
            else:
                payload.pop("whole_turn_terminal")
            wire.emit("res", payload, id=frame["id"], ok=True)
    wire.capability = capability

    async def scenario():
        async with FeralClient(chat_timeout=0.025) as client:
            with pytest.raises((ChatTurnError, ChatTurnTimeout)):
                await client.chat("effectful task")
    run(scenario())
    assert commands(wire) == []
    assert wire.closed


@pytest.mark.parametrize("changes,expected", [
    ({"turn_id": str(uuid4())}, "uncorrelated_terminal"),
    ({"session_id": "another-session"}, "invalid_turn_receipt"),
    ({"contract_version": True}, "invalid_turn_receipt"),
    ({"durable": False}, "invalid_turn_receipt"),
    ({"replayed": None}, "invalid_turn_receipt"),
    ({"processing_outcome": "success"}, "invalid_turn_outcome"),
    ({"approval_request_ids": [None]}, "invalid_turn_receipt"),
    ({"action_outcome": "verified"}, "invalid_turn_receipt"),
])
def test_invalid_terminal_rejected(wire, changes, expected):
    def command(frame):
        wire.acceptance(frame)
        wire.terminal(frame, **changes)
    wire.command = command

    async def scenario():
        async with FeralClient(chat_timeout=1) as client:
            with pytest.raises(ChatTurnError) as error:
                await client.chat("fixture")
            assert error.value.code == expected
    run(scenario())
    assert len(commands(wire)) == 1


def test_terminal_before_acceptance_rejected(wire):
    wire.command = lambda frame: wire.terminal(frame)

    async def scenario():
        async with FeralClient() as client:
            with pytest.raises(ChatTurnError, match="uncorrelated_terminal"):
                await client.chat("fixture")
    run(scenario())


@pytest.mark.parametrize("mode", ["partial", "mismatched_request", "no_terminal"])
def test_whole_deadline_never_returns_partial_success(wire, mode):
    def command(frame):
        wire.acceptance(frame)
        if mode == "partial":
            wire.emit("stream_delta", {"delta": "partial", "is_final": True})
            wire.emit("text_response", {"text": "Approval notification"})
        elif mode == "mismatched_request":
            wire.terminal(frame, request_id=str(uuid4()))
    wire.command = command

    async def scenario():
        async with FeralClient(chat_timeout=0.025) as client:
            with pytest.raises(ChatTurnTimeout):
                await client.chat("fixture")
    run(scenario())
    assert len(commands(wire)) == 1
    assert wire.closed


@pytest.mark.parametrize("raw,code", [("not-json-private", "invalid_json"), ("[]", "invalid_frame"),
                                     ('{"type":"event","payload":[]}', "invalid_payload")])
def test_malformed_frames_are_redacted_failures(wire, raw, code):
    wire.command = lambda _: wire.queue.put_nowait(raw)

    async def scenario():
        async with FeralClient(bearer_token="fixture-secret") as client:
            with pytest.raises(ChatTurnError) as error:
                await client.chat("private prompt")
            assert error.value.code == code
            assert "private" not in str(error.value) and "fixture-secret" not in str(error.value)
    run(scenario())


@pytest.mark.parametrize("code,expected", [(4001, "unauthorized"), (1000, "connection_closed")])
def test_close_is_not_success_or_retry(wire, code, expected):
    from websockets.exceptions import ConnectionClosedError
    from websockets.frames import Close
    wire.capability = lambda _: wire.queue.put_nowait(ConnectionClosedError(Close(code, "fixture-secret"), None))

    async def scenario():
        async with FeralClient(bearer_token="fixture-secret") as client:
            with pytest.raises(ChatTurnError) as error:
                await client.chat("fixture")
            assert error.value.code == expected
            assert "fixture-secret" not in str(error.value)
    run(scenario())
    assert commands(wire) == []


def test_cancellation_and_session_busy_no_false_server_cancel(wire):
    wire.command = lambda frame: wire.acceptance(frame)

    async def scenario():
        async with FeralClient() as client:
            task = asyncio.create_task(client.chat("fixture", session_id="same"))
            while not commands(wire):
                await asyncio.sleep(0)
            with pytest.raises(ChatTurnError, match="session_busy"):
                await client.chat("second", session_id="same")
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert wire.closed
            assert not client._chat_sessions and not client._chat_sockets
    run(scenario())
    assert len(commands(wire)) == 1
    assert not any(frame.get("method") == "chat.abort" for frame in wire.sent)


@pytest.mark.parametrize("session", ["", " x", "x ", "x\x00", "x\n", "x" * 1025])
def test_invalid_sessions_and_url_credentials_refuse_before_connect(wire, session):
    with pytest.raises(ValueError):
        FeralClient(session_id=session)
    assert not wire.sent


@pytest.mark.parametrize("url", ["http://user:secret@fixture", "http://fixture?token=secret", "file:///tmp/brain"])
def test_no_credentials_in_urls(wire, url):
    with pytest.raises(ValueError):
        FeralClient(url)
    assert not wire.sent

# The backend worker owns this reusable fixture; importing it preserves the
# production registered route, actual SQLite manager and real orchestrator.
from tests.test_chat_turn_abort import tracked_client as _tracked_client

tracked_client = _tracked_client


class RegisteredSocket:
    """Adapt synchronous TestClient WebSocket I/O to the SDK's async wire API."""

    def __init__(self, client, uri):
        self.client, self.uri = client, uri
        self.sent = []
        self.received = []
        self.closed = False

    async def __aenter__(self):
        url = urlsplit(self.uri)
        self.manager = self.client.websocket_connect(url.path + "?" + url.query)
        self.ws = await asyncio.to_thread(self.manager.__enter__)
        return self

    async def __aexit__(self, *args):
        await asyncio.to_thread(self.manager.__exit__, *args)
        self.closed = True

    async def send(self, raw):
        self.sent.append(json.loads(raw))
        await asyncio.to_thread(self.ws.send_text, raw)

    async def recv(self):
        from starlette.websockets import WebSocketDisconnect
        from websockets.exceptions import ConnectionClosedError
        from websockets.frames import Close
        try:
            raw = await asyncio.to_thread(self.ws.receive_text)
        except WebSocketDisconnect as exc:
            raise ConnectionClosedError(Close(exc.code, ""), None) from None
        self.received.append(json.loads(raw))
        return raw

    async def close(self):
        if not self.closed:
            await asyncio.to_thread(self.ws.close)
            self.closed = True


def registered_dial(monkeypatch, client):
    import websockets
    sockets = []

    def dial(uri, **kwargs):
        socket = RegisteredSocket(client, uri)
        sockets.append(socket)
        return socket
    monkeypatch.setattr(websockets, "connect", dial)
    return sockets


def test_sdk_actual_registered_multiround_turn_and_committed_receipt(tracked_client, monkeypatch):
    from unittest.mock import AsyncMock
    client, state, orch, prepare = tracked_client
    sockets = registered_dial(monkeypatch, client)
    rounds = []

    async def stream(*args, **kwargs):
        rounds.append(kwargs)
        if len(rounds) == 1:
            yield {"type": "text_delta", "content": "I will inspect it."}
            yield {"type": "tool_call_delta", "tool_call": {"id": "fixture-tool", "name": "notes_memory__default", "args": {}}}
        else:
            yield {"type": "text_delta", "content": "Actual final result"}
        yield {"type": "done"}
    orch.llm.chat_stream = stream
    orch._execute_tool_call_for_llm = AsyncMock(return_value={"success": True, "data": {"message": "fixture"}})
    orch._try_genui_for_result = AsyncMock()

    async def scenario():
        async with FeralClient("http://testserver", chat_timeout=5) as sdk:
            return await sdk.chat_turn("inspect disposable fixture", session_id="SDK-thread-A")
    receipt = run(scenario())
    assert receipt.processing_outcome == TurnProcessingOutcome.COMPLETED
    assert receipt.final_text == "Actual final result" and receipt.action_outcome == "not_asserted"
    assert len(rounds) == 2 and prepare.await_count == 1
    assert any(frame["type"] == "stream_delta" and frame["payload"].get("is_final") for frame in sockets[0].received[:-1])
    from functools import partial
    stored = client.portal.call(partial(state.chat_turns.status, session_id="SDK-thread-A", turn_id=receipt.turn_id))
    assert stored["request_id"] == receipt.request_id and stored["final_text"] == receipt.final_text


@pytest.mark.parametrize("credential,accepted", [("fixture-operator-key", True), ("wrong-key", False)])
def test_sdk_first_auth_actual_registered_remote_policy(tracked_client, monkeypatch, credential, accepted):
    import api.server as server
    client, _state, _orch, prepare = tracked_client
    monkeypatch.setattr(server, "is_localhost", lambda _: False)
    monkeypatch.setattr(server, "local_bypass_enabled", lambda: False)
    monkeypatch.setattr(server, "verify_session", lambda _: False)
    monkeypatch.setattr(server, "FERAL_API_KEY", "fixture-operator-key")
    sockets = registered_dial(monkeypatch, client)

    async def scenario():
        async with FeralClient("http://testserver", bearer_token=credential, chat_timeout=5) as sdk:
            if accepted:
                receipt = await sdk.chat_turn("fixture", session_id="authenticated-SDK")
                assert receipt.processing_outcome == TurnProcessingOutcome.COMPLETED
            else:
                with pytest.raises(ChatTurnError) as error:
                    await sdk.chat_turn("fixture", session_id="authenticated-SDK")
                assert error.value.code == "unauthorized"
    run(scenario())
    assert sockets[0].sent[0] == {"type": "auth", "token": credential}
    assert credential not in sockets[0].uri
    assert prepare.await_count == (1 if accepted else 0)


def test_actual_registered_unsupported_capability_has_zero_commands(tracked_client, monkeypatch):
    client, state, _orch, prepare = tracked_client
    monkeypatch.setattr(state.memory, "chat_turn_claim", None)
    sockets = registered_dial(monkeypatch, client)

    async def scenario():
        async with FeralClient("http://testserver", chat_timeout=5) as sdk:
            with pytest.raises(ChatTurnError) as error:
                await sdk.chat("effectful task")
            assert error.value.code == "unsupported_turn_contract"
    run(scenario())
    assert commands(sockets[0]) == [] and prepare.await_count == 0

@pytest.mark.parametrize("code", ["chat_turn_receipt_unavailable", "private arbitrary code"])
def test_correlated_request_errors_preserve_known_safe_code_only(wire, code):
    wire.command = lambda frame: wire.emit("error", {"request_id": frame["msg_id"], "code": code, "message": "private server diagnostic"})

    async def scenario():
        async with FeralClient() as client:
            with pytest.raises(ChatTurnError) as error:
                await client.chat("fixture")
            assert error.value.code == (code if code.startswith("chat_turn_") else "request_rejected")
            assert "private" not in str(error.value)
    run(scenario())
    assert len(commands(wire)) == 1


def test_distinct_sessions_run_concurrently_and_default_thread_is_stable(monkeypatch):
    import websockets
    sockets = []

    def dial(uri, **kwargs):
        socket = Wire()
        sockets.append(socket)
        return socket.dial(uri, **kwargs)
    monkeypatch.setattr(websockets, "connect", dial)

    async def scenario():
        async with FeralClient() as client:
            first, second = await asyncio.gather(client.chat_turn("one", "one"), client.chat_turn("two", "two"))
            assert (first.session_id, second.session_id) == ("one", "two")
            third = await client.chat_turn("default one")
            fourth = await client.chat_turn("default two")
            assert third.session_id == fourth.session_id
            assert third.request_id != fourth.request_id
        async with FeralClient() as other:
            fifth = await other.chat_turn("new client")
            assert fifth.session_id != third.session_id
    run(scenario())
    assert len(sockets) == 4 and all(socket.closed for socket in sockets)
