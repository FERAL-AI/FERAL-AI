"""Live SDK threads: real provider context, isolation, and loss before resubmit."""
import asyncio
from copy import deepcopy
from uuid import uuid4

import pytest

from test_client_websocket import (
    FeralClient, ChatTurnError, ChatTurnTimeout, Wire, commands, run,
    isolate_sdk_runtime as _isolate, tracked_client as _tracked, registered_dial,
)

isolate_sdk_runtime = _isolate
tracked_client = _tracked


@pytest.fixture
def channels(monkeypatch):
    import websockets
    wires = []

    def dial(uri, **kwargs):
        wire = Wire()
        original = wire.command

        def reply(command):
            wire.turn_id = str(uuid4())
            original(command)
        wire.command = reply
        wires.append(wire)
        return wire.dial(uri, **kwargs)
    monkeypatch.setattr(websockets, "connect", dial)
    return wires


def test_actual_provider_sees_three_turns_on_one_negotiated_socket(tracked_client, monkeypatch):
    client, state, orch, _prepare = tracked_client
    sockets = registered_dial(monkeypatch, client)
    captured = []

    async def provider(messages, **kwargs):
        captured.append(deepcopy(messages))
        yield {"type": "text_delta", "content": f"reply-{len(captured)}"}
        yield {"type": "done"}
    orch.llm.chat_stream = provider

    async def scenario():
        async with FeralClient("http://testserver", bearer_token="fixture", session_id="thread-alpha", chat_timeout=5) as sdk:
            receipts = [await sdk.chat_turn(text) for text in ("violet-maple-47", "What did I say?", "And your reply?")]
            assert not sockets[0].closed
            assert len(state.orchestrator.conversation_history["thread-alpha"]) >= 6
            return receipts
    receipts = run(scenario())
    assert len(sockets) == 1 and sockets[0].closed
    assert [frame["type"] for frame in sockets[0].sent].count("auth") == 1
    assert [frame["type"] for frame in sockets[0].sent].count("req") == 1
    assert len({row.request_id for row in receipts}) == len({row.turn_id for row in receipts}) == 3
    assert receipts[-1].final_text == "reply-3"
    assert any(row.get("content") == "violet-maple-47" for row in captured[1])
    assert any(row.get("content") == "reply-1" for row in captured[1])
    assert any(row.get("content") == "reply-2" for row in captured[2])
    assert state.orchestrator.conversation_history.get("thread-alpha") is None


def test_actual_simultaneous_threads_keep_distinct_provider_context(tracked_client, monkeypatch):
    client, state, orch, _prepare = tracked_client
    sockets = registered_dial(monkeypatch, client)
    captured = []

    async def provider(messages, **kwargs):
        captured.append(deepcopy(messages))
        yield {"type": "text_delta", "content": "controlled reply"}
        yield {"type": "done"}
    orch.llm.chat_stream = provider

    async def scenario():
        async with FeralClient("http://testserver", chat_timeout=5) as sdk:
            await asyncio.gather(sdk.chat("violet-alpha", "A"), sdk.chat("orange-beta", "B"))
            receipts = await asyncio.gather(sdk.chat_turn("continue A", "A"), sdk.chat_turn("continue B", "B"))
            assert {row.session_id for row in receipts} == {"A", "B"}
            assert state.session_attach_count == {"A": 1, "B": 1}
    run(scenario())
    assert len(sockets) == 2 and all(w.closed for w in sockets)
    for messages in captured:
        text = str(messages)
        assert not ("violet-alpha" in text and "orange-beta" in text)
    a = next(rows for rows in captured if any(row.get("content") == "continue A" for row in rows))
    b = next(rows for rows in captured if any(row.get("content") == "continue B" for row in rows))
    assert any(row.get("content") == "violet-alpha" for row in a)
    assert any(row.get("content") == "orange-beta" for row in b)
    assert state.session_attach_count == {}


def test_idle_disconnect_refuses_old_thread_before_connect_or_command(channels):
    from websockets.exceptions import ConnectionClosedError
    from websockets.frames import Close

    async def scenario():
        async with FeralClient(session_id="old") as sdk:
            await sdk.chat("first")
            old = sdk._channels["old"]
            channels[0].queue.put_nowait(ConnectionClosedError(Close(1001, ""), None))
            await old.reader
            with pytest.raises(ChatTurnError, match="context_lost"):
                await sdk.chat("do not submit")
            assert len(channels) == 1 and len(commands(channels[0])) == 1
            assert not sdk._channels and not sdk._chat_sockets
            assert await sdk.chat("new deliberate task", "NEW") == "42"
    run(scenario())
    assert len(channels) == 2 and all(w.closed for w in channels)


@pytest.mark.parametrize("interruption", ["timeout", "cancel", "close_thread", "close_client"])
def test_active_interruption_releases_reader_and_never_resubmits(channels, interruption):
    async def scenario():
        sdk = FeralClient(session_id="old", chat_timeout=.03)
        await sdk.chat("establish")
        wire, channel = channels[0], sdk._channels["old"]
        wire.command = wire.acceptance
        if interruption == "timeout":
            with pytest.raises(ChatTurnTimeout):
                await sdk.chat("waiting")
        else:
            task = asyncio.create_task(sdk.chat("waiting", timeout=5))
            while len(commands(wire)) < 2:
                await asyncio.sleep(0)
            if interruption == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            elif interruption == "close_thread":
                await sdk.close_thread()
                with pytest.raises(ChatTurnError, match="context_lost"):
                    await task
            else:
                await sdk.close()
                with pytest.raises(ChatTurnError, match="client_closed"):
                    await task
        assert channel.reader.done() and wire.closed
        assert not sdk._chat_sockets and not sdk._channels and not sdk._chat_sessions
        with pytest.raises(ChatTurnError, match="client_closed" if interruption == "close_client" else "context_lost"):
            await sdk.chat("no replay")
        assert len(channels) == 1 and len(commands(wire)) == 2
        assert not any(frame.get("method") == "chat.abort" for frame in wire.sent)
        if interruption != "close_client":
            assert await sdk.chat("new chosen ID", "NEW") == "42"
        await sdk.close()
    run(scenario())


def test_stale_terminal_cannot_finish_later_turn(channels):
    async def scenario():
        async with FeralClient() as sdk:
            first = await sdk.chat_turn("first")
            old_command = commands(channels[0])[0]

            def next_turn(command):
                channels[0].terminal(old_command, final_text="stale")
                channels[0].turn_id = str(uuid4())
                channels[0].acceptance(command)
                channels[0].terminal(command, final_text="new exact turn")
            channels[0].command = next_turn
            second = await sdk.chat_turn("second")
            assert second.final_text == "new exact turn" and second.request_id != first.request_id
    run(scenario())
    assert len(channels) == 1 and len(commands(channels[0])) == 2


def test_channel_quota_preserves_live_thread_and_close_frees_capacity(channels):
    async def scenario():
        async with FeralClient(max_chat_threads=1) as sdk:
            await sdk.chat("first", "A")
            with pytest.raises(ChatTurnError, match="thread_quota"):
                await sdk.chat("cannot evict A", "B")
            assert len(channels) == 1 and not channels[0].closed
            await sdk.chat("still same context", "A")
            await sdk.close_thread("A")
            with pytest.raises(ChatTurnError, match="context_lost"):
                await sdk.chat("no reopening A", "A")
            await sdk.chat("deliberate new thread", "B")
    run(scenario())
    assert len(channels) == 2 and all(w.closed for w in channels)


@pytest.mark.parametrize("maximum", [0, -1, 65, True, 1.5])
def test_invalid_channel_bound(maximum):
    with pytest.raises(ValueError, match="max_chat_threads"):
        FeralClient(max_chat_threads=maximum)


def test_identity_tombstones_are_bounded_without_reopening(channels):
    async def scenario():
        async with FeralClient() as sdk:
            # Pre-fill retired identity metadata, without 1024 real connections.
            sdk._lost_threads.update(f"closed-{i}" for i in range(1024))
            with pytest.raises(ChatTurnError, match="thread_identity_quota"):
                await sdk.chat("do not submit", "new")
            with pytest.raises(ChatTurnError, match="context_lost"):
                await sdk.chat("do not reopen", "closed-0")
    run(scenario())
    assert channels == []


@pytest.mark.parametrize("operation", ["close_thread", "close_client", "timeout", "cancel"])
def test_interruption_during_connection_has_zero_prompts_and_no_reader_leak(monkeypatch, operation):
    import websockets
    connections = []

    class SlowConnection(Wire):
        async def __aenter__(self):
            self.entered.set()
            await asyncio.Event().wait()
        def dial(self, uri, **kwargs):
            self.entered = asyncio.Event()
            connections.append(self)
            return super().dial(uri, **kwargs)
    monkeypatch.setattr(websockets, "connect", SlowConnection().dial)

    async def scenario():
        sdk = FeralClient(session_id="lost", chat_timeout=.03)
        task = asyncio.create_task(sdk.chat("do not submit"))
        while not connections or not connections[0].entered.is_set():
            await asyncio.sleep(0)
        if operation == "close_thread":
            await sdk.close_thread()
        elif operation == "close_client":
            await sdk.close()
        elif operation == "cancel":
            task.cancel()
        if operation == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await task
        elif operation == "timeout":
            with pytest.raises(ChatTurnTimeout):
                await task
        else:
            with pytest.raises(ChatTurnError, match="client_closed" if operation == "close_client" else "context_lost"):
                await task
        assert not sdk._chat_readers and not sdk._channels and not sdk._chat_sockets
        assert connections[0].closed and commands(connections[0]) == []
        with pytest.raises(ChatTurnError, match="client_closed" if operation == "close_client" else "context_lost"):
            await sdk.chat("do not reconnect")
        assert len(connections) == 1
        await sdk.close()
    run(scenario())


def test_idle_reader_cancellation_never_reuses_a_closed_transport(channels):
    async def scenario():
        async with FeralClient(session_id="A") as sdk:
            await sdk.chat("establish")
            reader = sdk._channels["A"].reader
            reader.cancel()  # Simulate application/event-loop shutdown of a live reader.
            await asyncio.gather(reader, return_exceptions=True)
            with pytest.raises(ChatTurnError, match="context_lost"):
                await sdk.chat("do not submit on dead transport")
            assert len(channels) == 1 and len(commands(channels[0])) == 1
            assert not sdk._chat_readers and not sdk._channels and channels[0].closed
    run(scenario())
