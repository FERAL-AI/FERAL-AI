import asyncio
import hashlib
import hmac
import json
import threading
import time
from unittest.mock import AsyncMock

import pytest

from channels.base import ChannelManager, ChannelResponse, ChannelSendError
from channels.imessage import IMessageChannel, normalize_event
from channels.inbound_receipts import InboundReceipts, InboundReceiptConflict


def test_normalize_imessage_event_is_stable_and_scoped():
    payload = {
        "event_type": "imessage.received",
        "id": "evt-1",
        "data": {
            "message": {
                "id": "msg-1",
                "conversation_id": "thread-1",
                "sender_number": "+15551230000",
                "content": "find that bottle",
                "reply_to_message_id": "old-1",
                "is_group": False,
                "is_from_me": False,
                "media": [
                    {
                        "url": "https://example.invalid/bottle.jpg",
                        "content_type": "image/jpeg",
                    }
                ],
            }
        },
    }
    message = normalize_event(payload)
    assert message is not None
    assert message.channel_type == "imessage"
    assert message.channel_id == "thread-1"
    assert message.user_id == "+15551230000"
    assert message.metadata["event_id"] == "evt-1"
    assert message.metadata["conversation_key"] == "imessage:thread-1"
    assert message.reply_to == "old-1"
    assert message.metadata["attachments"][0]["content_type"] == "image/jpeg"


def test_delivery_receipts_and_malformed_events_are_not_user_turns():
    assert normalize_event({"event_type": "imessage.delivered", "data": {}}) is None
    assert (
        normalize_event(
            {"event_type": "imessage.received", "data": {"message": {"content": "hi"}}}
        )
        is None
    )


@pytest.mark.asyncio
async def test_channel_disabled_without_explicit_transport():
    channel = IMessageChannel({"enabled": True})
    await channel.start()
    assert channel._running is False
    assert channel._connected is False
    assert (
        await channel.ingest_event(
            {
                "event_type": "imessage.received",
                "data": {
                    "message": {
                        "conversation_id": "thread",
                        "sender_number": "+1",
                        "content": "hello",
                    }
                },
            }
        )
        is False
    )


def event(**changes):
    value = {
        "schema_version": 1,
        "account_id": "fixture-account",
        "event_type": "imessage.received",
        "id": "event-1",
        "data": {
            "message": {
                "conversation_id": "thread",
                "sender_number": "+1",
                "content": "fixture task",
                "is_group": False,
                "is_from_me": False,
            }
        },
    }
    value.update(changes)
    return value


def signed(value, *, timestamp=None, secret="x" * 32):
    raw = json.dumps(value).encode()
    timestamp = timestamp or str(int(time.time()))
    signature = hmac.new(
        secret.encode(), timestamp.encode() + b"." + raw, hashlib.sha256
    ).hexdigest()
    return raw, timestamp, signature


def fixture_channel(tmp_path):
    channel = IMessageChannel(
        {
            "allowed_senders": ["+1"],
            "allowed_chats": ["thread"],
            "account_id": "fixture-account",
            "signing_secret": "x" * 32,
        }
    )
    channel._running = True  # Inert transport; does not represent a real bridge.
    channel._receipts = InboundReceipts(tmp_path / "inbox.db")
    channel.set_handler(AsyncMock(return_value=ChannelResponse(text="fixture reply")))
    channel.send = AsyncMock(return_value=None)
    return channel


@pytest.mark.asyncio
async def test_signed_admission_exact_reply_and_restart_dedupe(tmp_path):
    channel = fixture_channel(tmp_path)
    assert await channel.ingest_signed_event(*signed(event())) == {
        "accepted": True,
        "state": "reply_unverified",
    }
    channel.send.assert_awaited_once()
    assert channel.send.call_args.args[0] == "thread"
    channel._receipts.close()
    reopened = fixture_channel(tmp_path)
    result = await reopened.ingest_signed_event(*signed(event()))
    assert result == {"accepted": True, "state": "reply_unverified", "duplicate": True}
    reopened._handler.assert_not_awaited()
    reopened.send.assert_not_awaited()
    with pytest.raises(InboundReceiptConflict):
        await reopened.ingest_signed_event(
            *signed(
                event(
                    data={
                        "message": {
                            "conversation_id": "thread",
                            "sender_number": "+1",
                            "content": "changed",
                            "is_group": False,
                            "is_from_me": False,
                        }
                    }
                )
            )
        )
    reopened._receipts.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    [
        "signature",
        "stale",
        "account",
        "sender",
        "group",
        "missing_id",
        "echo",
        "unsigned",
    ],
)
async def test_untrusted_events_dispatch_nothing(tmp_path, kind):
    channel = fixture_channel(tmp_path)
    value = event()
    if kind == "account":
        value["account_id"] = "other"
    if kind == "sender":
        value["data"]["message"]["sender_number"] = "+2"
    if kind == "group":
        value["data"]["message"]["is_group"] = True
    if kind == "missing_id":
        del value["id"]
    if kind == "echo":
        value["data"]["message"]["is_from_me"] = True
    args = signed(
        value, timestamp=str(int(time.time()) - 301) if kind == "stale" else None
    )
    if kind == "signature":
        args = (*args[:2], "0" * 64)
    result = (
        await channel.ingest_event(value)
        if kind == "unsigned"
        else await channel.ingest_signed_event(*args)
    )
    assert result is False or result["accepted"] is False
    channel._handler.assert_not_awaited()
    channel.send.assert_not_awaited()
    channel._receipts.close()


@pytest.mark.asyncio
async def test_unknown_after_effect_or_cancel_is_never_replayed(tmp_path):
    channel = fixture_channel(tmp_path)
    reached = asyncio.Event()

    async def handler(message):
        reached.set()
        await asyncio.Event().wait()

    channel.set_handler(AsyncMock(side_effect=handler))
    task = asyncio.create_task(channel.ingest_signed_event(*signed(event())))
    await asyncio.wait_for(reached.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    channel._receipts.close()
    reopened = fixture_channel(tmp_path)
    result = await reopened.ingest_signed_event(*signed(event()))
    assert result["state"] == "outcome_unknown" and result["duplicate"]
    reopened._handler.assert_not_awaited()
    reopened.send.assert_not_awaited()
    reopened._receipts.close()


@pytest.mark.asyncio
async def test_bridge_unavailable_does_not_start_or_report_send_success():
    manager = ChannelManager()
    assert not (
        await manager.start_channel(
            "imessage", {"backend": "imsg", "endpoint": "fixture"}
        )
    )["started"]
    with pytest.raises(ChannelSendError):
        await IMessageChannel({}).send("thread", ChannelResponse(text="fixture"))


def test_receipt_claim_reopen_and_capacity(tmp_path):
    path = tmp_path / "inbox.db"
    store = InboundReceipts(path, capacity=1)
    assert store.claim("scope", "event", {"text": "one"}) == "new"
    store.close()
    reopened = InboundReceipts(path, capacity=1)
    assert reopened.claim("scope", "event", {"text": "one"}) == "claimed"
    with pytest.raises(InboundReceiptConflict):
        reopened.claim("scope", "event2", {"text": "two"})
    reopened.close()


@pytest.mark.asyncio
async def test_identical_text_with_different_event_ids_is_not_discarded(tmp_path):
    channel = fixture_channel(tmp_path)
    await channel.ingest_signed_event(*signed(event()))
    await channel.ingest_signed_event(*signed(event(id="event-2")))
    assert channel._handler.await_count == 2
    assert channel.send.await_count == 2
    channel._receipts.close()


@pytest.mark.asyncio
async def test_reply_failure_never_reexecutes_the_task(tmp_path):
    channel = fixture_channel(tmp_path)
    channel.send.side_effect = ChannelSendError("inert rejected send")
    assert (await channel.ingest_signed_event(*signed(event())))[
        "state"
    ] == "outcome_unknown"
    channel.send.side_effect = None
    assert (await channel.ingest_signed_event(*signed(event())))["duplicate"]
    channel._handler.assert_awaited_once()
    channel.send.assert_awaited_once()
    channel._receipts.close()


def test_receipts_are_private_and_refuse_symlinks(tmp_path):
    path = tmp_path / "inbox.db"
    store = InboundReceipts(path)
    assert path.stat().st_mode & 0o777 == 0o600
    store.close()
    alias = tmp_path / "alias.db"
    alias.symlink_to(path)
    with pytest.raises(OSError):
        InboundReceipts(alias)


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", [None, True, 1, "true", "false"])
async def test_echo_direction_requires_explicit_false(tmp_path, marker):
    channel = fixture_channel(tmp_path)
    value = event()
    if marker is None:
        value["data"]["message"].pop("is_from_me")
    else:
        value["data"]["message"]["is_from_me"] = marker
    assert (await channel.ingest_signed_event(*signed(value)))[
        "state"
    ] == "invalid_event"
    channel._handler.assert_not_awaited()
    channel._receipts.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["", "x" * 16385, "\ud800"])
async def test_unsupported_attachment_or_invalid_text_never_dispatches(tmp_path, text):
    channel = fixture_channel(tmp_path)
    value = event()
    value["data"]["message"]["content"] = text
    value["data"]["message"]["attachments"] = [{"url": "fixture"}]
    assert (await channel.ingest_signed_event(*signed(value)))[
        "state"
    ] == "invalid_event"
    channel._handler.assert_not_awaited()
    channel.send.assert_not_awaited()
    channel._receipts.close()


@pytest.mark.asyncio
async def test_stop_cancels_admitted_handler_and_persists_uncertainty(tmp_path):
    channel = fixture_channel(tmp_path)
    reached = asyncio.Event()
    effects = []

    async def handler(message):
        reached.set()
        await asyncio.Event().wait()
        effects.append("must not dispatch after stop")

    channel.set_handler(AsyncMock(side_effect=handler))
    ingress = asyncio.create_task(channel.ingest_signed_event(*signed(event())))
    await asyncio.wait_for(reached.wait(), 2)
    await channel.stop()
    with pytest.raises(asyncio.CancelledError):
        await ingress
    assert effects == []
    assert channel._active_ingress is None and not channel._receipt_tasks
    channel.send.assert_not_awaited()
    channel._receipts.close()
    reopened = fixture_channel(tmp_path)
    result = await reopened.ingest_signed_event(*signed(event()))
    assert result == {"accepted": True, "state": "outcome_unknown", "duplicate": True}
    reopened._handler.assert_not_awaited()
    reopened._receipts.close()


@pytest.mark.asyncio
async def test_stop_is_bounded_when_handler_cannot_cancel_and_never_claims_undo(
    tmp_path,
):
    channel = fixture_channel(tmp_path)
    channel.STOP_DRAIN_TIMEOUT_SECONDS = 0.02
    reached = asyncio.Event()
    release = asyncio.Event()
    effects = []

    async def handler(message):
        reached.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            # Simulates an external adapter that cannot confirm cancellation.
            await release.wait()
        effects.append("inert uncertain effect")
        return ChannelResponse(text="fixture result")

    channel.set_handler(AsyncMock(side_effect=handler))
    ingress = asyncio.create_task(channel.ingest_signed_event(*signed(event())))
    await asyncio.wait_for(reached.wait(), 2)
    started = asyncio.get_running_loop().time()
    await channel.stop()
    assert asyncio.get_running_loop().time() - started < 0.5
    assert not ingress.done() and channel._active_ingress is ingress
    assert channel._degraded and "outcome remains unknown" in channel._degraded_reason
    assert (await channel.ingest_signed_event(*signed(event(id="another"))))[
        "state"
    ] == "unavailable"
    release.set()
    assert await asyncio.wait_for(ingress, 2) == {
        "accepted": True,
        "state": "outcome_unknown",
    }
    assert effects == ["inert uncertain effect"]
    channel.send.assert_not_awaited()
    channel._receipts.close()
    reopened = fixture_channel(tmp_path)
    assert (await reopened.ingest_signed_event(*signed(event())))["duplicate"]
    reopened._handler.assert_not_awaited()
    reopened._receipts.close()


@pytest.mark.asyncio
async def test_stop_does_not_cancel_retained_claim_thread(tmp_path):
    channel = fixture_channel(tmp_path)
    channel.STOP_DRAIN_TIMEOUT_SECONDS = 0.02
    reached = threading.Event()
    release = threading.Event()
    claim = channel._receipts.claim

    def delayed_claim(*args):
        reached.set()
        assert release.wait(2)
        return claim(*args)

    channel._receipts.claim = delayed_claim
    ingress = asyncio.create_task(channel.ingest_signed_event(*signed(event())))
    assert await asyncio.to_thread(reached.wait, 2)
    await channel.stop()
    with pytest.raises(asyncio.CancelledError):
        await ingress
    pending = set(channel._receipt_tasks)
    assert pending and all(not task.cancelled() for task in pending)
    assert channel._degraded and "outcome remains unknown" in channel._degraded_reason
    release.set()
    await asyncio.wait_for(asyncio.gather(*pending), 2)
    channel._handler.assert_not_awaited()
    channel._receipts.close()
    reopened = fixture_channel(tmp_path)
    assert (await reopened.ingest_signed_event(*signed(event())))[
        "state"
    ] == "outcome_unknown"
    reopened._handler.assert_not_awaited()
    reopened._receipts.close()
