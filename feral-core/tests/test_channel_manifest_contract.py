""" — generic send/receive contract test.

Purpose: any in-tree adapter that ships a ``feral-channel.manifest.json``
must satisfy the **manifest-declared capability surface** end-to-end
(stubbed network). Today only Telegram is migrated, so the contract
runs against Telegram. As  lands additional manifests, each one
gets parametrised in here automatically — no per-channel test rewrites.

Key idea: the test does NOT import ``TelegramChannel`` by name. It
walks ``loader.discover_bundled()``, looks up the channel's
implementation through a small in-test bridge, and exercises the
abstract-base-class surface from ``channels/base.py``. That bridge is
the seam that  full SDK will formalise; here it's a 5-line
mapping so we can keep this PR scoped to Phase 1.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from channels.base import (
    Channel,
    ChannelManager,
    ChannelMessage,
    ChannelResponse,
    TelegramChannel,
)
from channels.loader import discover_bundled


# Phase-1 bridge: channel id (manifest.id) → adapter class. Phase 3
# () replaces this with an entry-point / SDK barrel so 3rd-party
# channels register themselves without touching this file.
_ADAPTER_BY_MANIFEST_ID: dict[str, type[Channel]] = {
    "telegram": TelegramChannel,
}


def _instantiate_for_test(channel_id: str) -> Channel:
    """Build a stubbed adapter instance suitable for off-network smoke."""
    cls = _ADAPTER_BY_MANIFEST_ID.get(channel_id)
    if cls is None:
        pytest.skip(f"no adapter registered for manifest id {channel_id!r}")
    if channel_id == "telegram":
        # Inbound is default-deny (channels.base "Inbound access
        # control"); the contract test drives a synthetic sender id
        # of 999 in chat 12345, so both are allowlisted here.
        ch = cls({
            "bot_token": "test-token-not-real",
            "allowed_senders": ["999"],
            "allowed_chats": ["12345"],
        })
        # Skip the real `start()` (which would hit api.telegram.org) and
        # patch the http client + base url manually.
        ch._base_url = "https://api.telegram.org/botfake"
        ch._http = AsyncMock()
        ch._running = True
        return ch
    pytest.skip(f"no test wiring for manifest id {channel_id!r}")
    raise AssertionError("unreachable")  # pragma: no cover — pytest.skip raises


def _all_manifests_with_messaging() -> list[Any]:
    return [m for m in discover_bundled() if m.capability("messagingProvider")]


@pytest.mark.parametrize(
    "manifest",
    _all_manifests_with_messaging(),
    ids=lambda m: m.id,
)
class TestMessagingContract:
    """Contract: every messaging-capable manifest can send and receive."""

    @pytest.mark.asyncio
    async def test_send_text_does_not_hit_network(self, manifest: Any) -> None:
        ch = _instantiate_for_test(manifest.id)
        await ch.send("dest-chat", ChannelResponse(text="hello"))

        # Whatever HTTP client the adapter uses, it MUST be the mocked
        # one — no real outbound traffic.
        if hasattr(ch, "_http"):
            assert ch._http.post.await_count >= 1, (
                f"{manifest.id} send did not call its HTTP client at all"
            )
            args, kwargs = ch._http.post.call_args
            payload = kwargs.get("json", {})
            # The payload should *carry the recipient and the text* in
            # SOME form. Different channels use different keys, so we
            # just check that both values appear somewhere serialisable.
            blob = repr(payload)
            assert "dest-chat" in blob, f"{manifest.id}: recipient missing from payload"
            assert "hello" in blob, f"{manifest.id}: text missing from payload"

    @pytest.mark.asyncio
    async def test_receive_round_trip_invokes_handler(self, manifest: Any) -> None:
        # Stand up an instance and register a handler. The handler
        # captures the inbound ChannelMessage and returns a canned
        # response. Then we drive an "incoming" event through the
        # adapter's internal entry point and assert the round-trip.
        ch = _instantiate_for_test(manifest.id)

        captured: dict[str, ChannelMessage] = {}

        async def handler(msg: ChannelMessage) -> ChannelResponse:
            captured["msg"] = msg
            return ChannelResponse(text="echo: " + msg.text)

        ch.set_handler(handler)

        if manifest.id == "telegram":
            # Use the same shape the real Telegram getUpdates loop
            # synthesises before calling _handle_message.
            await ch._handle_message({
                "chat": {"id": 12345},
                "from": {"id": 999, "first_name": "Alice", "username": "alice"},
                "text": "ping",
            })
        else:  # pragma: no cover — Phase-1 ships only Telegram
            pytest.skip(f"no inbound wiring for manifest id {manifest.id!r}")

        assert "msg" in captured, "adapter never invoked the handler"
        assert captured["msg"].text == "ping"
        assert captured["msg"].channel_type == manifest.id
        # Round-trip outbound: handler returned ChannelResponse → ch.send was invoked
        assert ch._http.post.await_count >= 1


class TestBundledRegistryShape:
    """Manifest <-> adapter parity guard: every bundled manifest must
    have a corresponding adapter the contract test can exercise.

    This is what catches "someone shipped a manifest but forgot to wire
    it into Phase 1's adapter bridge" before it silently turns into an
    unverified channel.
    """

    def test_every_bundled_manifest_has_an_adapter(self) -> None:
        manifests = discover_bundled()
        missing = [m.id for m in manifests if m.id not in _ADAPTER_BY_MANIFEST_ID]
        assert missing == [], (
            f"manifests with no adapter wired in test_channel_manifest_contract: "
            f"{missing} (expected only telegram in Phase 1; new channels need W21.2)"
        )

    def test_telegram_manifest_advertises_messaging_capability(self) -> None:
        manifests = discover_bundled()
        telegram = next((m for m in manifests if m.id == "telegram"), None)
        assert telegram is not None, "bundled telegram manifest missing"
        assert telegram.capability("messagingProvider") is True
        assert "telegram" in telegram.providers

    def test_channel_manager_recognises_manifest_provider(self) -> None:
        # The ChannelManager's CHANNEL_TYPES map and the manifest's
        # `id` must agree. This is the seam that lets ChannelManager
        # consume manifest discovery once  lands.
        manifests = discover_bundled()
        for m in manifests:
            for provider in m.providers:
                assert provider in ChannelManager.CHANNEL_TYPES, (
                    f"manifest {m.id!r} declares provider {provider!r} but "
                    f"ChannelManager.CHANNEL_TYPES has no entry for it"
                )
