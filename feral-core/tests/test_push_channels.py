"""
Tests for channels/push.py — PushChannel device registration,
FCM/APNs send paths, and token management with mocked HTTP.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import api.routes.timeline as timeline_routes
from channels.push import PushChannel, _normalize_platform


@pytest.fixture
def push(tmp_path, monkeypatch):
    monkeypatch.setattr("channels.push._db_path", lambda: tmp_path / "tokens.db")
    monkeypatch.delenv("FERAL_FIREBASE_CREDENTIALS", raising=False)
    monkeypatch.delenv("FERAL_APNS_KEY_PATH", raising=False)
    ch = PushChannel()
    yield ch
    ch.close()


class TestPushChannelInit:
    def test_init_without_config(self, push):
        assert push._firebase_project_id is None
        assert push._apns_token is None

    def test_init_with_firebase(self, tmp_path, monkeypatch):
        creds = tmp_path / "sa.json"
        creds.write_text('{"project_id": "my-proj"}')
        monkeypatch.setenv("FERAL_FIREBASE_CREDENTIALS", str(creds))
        monkeypatch.delenv("FERAL_APNS_KEY_PATH", raising=False)
        monkeypatch.setattr("channels.push._db_path", lambda: tmp_path / "t.db")
        ch = PushChannel()
        assert ch._firebase_project_id == "my-proj"
        ch.close()


class TestDeviceRegistration:
    def test_register_and_get(self, push):
        push.register_device("u1", "tok-abc", "fcm")
        tokens = push.get_tokens("u1")
        assert len(tokens) == 1
        assert tokens[0]["token"] == "tok-abc"
        assert tokens[0]["platform"] == "fcm"

    def test_register_multiple_platforms(self, push):
        push.register_device("u1", "fcm-tok", "fcm")
        push.register_device("u1", "apns-tok", "apns")
        tokens = push.get_tokens("u1")
        assert len(tokens) == 2

    def test_upsert_on_duplicate(self, push):
        push.register_device("u1", "tok", "fcm")
        push.register_device("u1", "tok", "fcm")
        tokens = push.get_tokens("u1")
        assert len(tokens) == 1

    def test_get_tokens_empty(self, push):
        assert push.get_tokens("nonexistent") == []


class TestFCMSend:
    def test_fcm_no_project_returns_error(self, push):
        result = push._send_fcm("tok", "Title", "Body", None)
        assert result["success"] is False
        assert "not configured" in result["error"]

    def test_fcm_no_bearer_returns_error(self, push):
        push._firebase_project_id = "test-proj"
        with patch.object(push, "_get_fcm_bearer_token", return_value=None):
            result = push._send_fcm("tok", "T", "B", None)
        assert result["success"] is False
        assert "bearer" in result["error"].lower()

    def test_fcm_success_mocked(self, push):
        push._firebase_project_id = "test-proj"
        mock_resp = MagicMock(status_code=200, text="ok")
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_resp
        mock_httpx = MagicMock()
        mock_httpx.Client.return_value = mock_client
        with patch.object(push, "_get_fcm_bearer_token", return_value="bearer-tok"):
            with patch.dict("sys.modules", {"httpx": mock_httpx}):
                result = push._send_fcm("device-tok", "Hey", "Body", {"key": "val"})
        assert result["success"] is True
        assert result["platform"] == "fcm"


class TestAPNsSend:
    def test_apns_no_key_returns_error(self, push):
        result = push._send_apns("tok", "T", "B", None)
        assert result["success"] is False
        assert "not configured" in result["error"]

    def test_apns_no_bearer_returns_error(self, push, tmp_path, monkeypatch):
        key_file = tmp_path / "key.p8"
        key_file.write_text("fake-key")
        push._apns_key_path = str(key_file)
        with patch.object(push, "_get_apns_token", return_value=None):
            result = push._send_apns("tok", "T", "B", None)
        assert result["success"] is False

    def test_apns_sandbox_host(self, push, tmp_path):
        key_file = tmp_path / "key.p8"
        key_file.write_text("fake")
        push._apns_key_path = str(key_file)
        push._apns_environment = "sandbox"
        mock_resp = MagicMock(status_code=200, text="ok")
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_resp
        mock_httpx = MagicMock()
        mock_httpx.Client.return_value = mock_client
        with patch.object(push, "_get_apns_token", return_value="jwt-tok"):
            with patch.dict("sys.modules", {"httpx": mock_httpx}):
                result = push._send_apns("device-tok", "Hey", "Body", None)
        assert result["success"] is True
        assert result["platform"] == "apns"


class TestSendPushRouting:
    def test_routes_to_fcm_by_default(self, push):
        with patch.object(push, "_send_fcm", return_value={"success": True}) as mock_fcm:
            push.send_push("tok", "T", "B", platform="fcm")
        mock_fcm.assert_called_once()

    def test_routes_to_apns(self, push):
        with patch.object(push, "_send_apns", return_value={"success": True}) as mock_apns:
            push.send_push("tok", "T", "B", platform="apns")
        mock_apns.assert_called_once()

    async def test_broadcast_no_tokens(self, push):
        # broadcast is a coroutine now (see TestBroadcastIsAwaitable): the
        # old sync call returned a list that the async route then awaited.
        results = await push.broadcast("no_user", "T", "B")
        assert results == []


# ─────────────────────────────────────────────────────────────────────
# Regression guards for the send path that had never once executed.
#
# api/routes/timeline.py awaited PushChannel.broadcast while broadcast was
# a plain `def` returning list[dict], so POST /api/push/send raised
# "TypeError: object list can't be used in 'await' expression" on every
# request it ever received. The route swallows exceptions into an error
# dict and device_tokens has never held a row, so nothing surfaced it.
# ─────────────────────────────────────────────────────────────────────


def _awaited_push_channel_attrs(module) -> set[str]:
    """Names the route module awaits off ``state.push_channel``.

    Parsed from source rather than called, so the check holds for endpoints
    no test happens to exercise (which is exactly how the original bug
    survived: /api/push/send had no test that reached the await).
    """
    tree = ast.parse(Path(inspect.getfile(module)).read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Await):
            continue
        call = node.value
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        if not isinstance(func, ast.Attribute):
            continue
        base = func.value
        if isinstance(base, ast.Attribute) and base.attr == "push_channel":
            found.add(func.attr)
    return found


class TestBroadcastIsAwaitable:
    def test_broadcast_is_a_coroutine_function(self):
        # The single assertion that would have caught the original bug.
        assert inspect.iscoroutinefunction(PushChannel.broadcast)

    def test_route_only_awaits_awaitable_push_methods(self):
        awaited = _awaited_push_channel_attrs(timeline_routes)
        assert awaited, "expected the timeline routes to await something on state.push_channel"
        for name in sorted(awaited):
            attr = getattr(PushChannel, name, None)
            assert attr is not None, f"route awaits state.push_channel.{name}() which does not exist"
            assert inspect.iscoroutinefunction(attr), (
                f"route awaits state.push_channel.{name}() but PushChannel.{name} "
                f"is not a coroutine function -- awaiting its return value raises TypeError"
            )

    def test_send_push_is_not_awaitable_and_stays_sync(self):
        # send_push does blocking httpx IO. It must stay sync so callers are
        # forced to decide how to get it off the event loop.
        assert not inspect.iscoroutinefunction(PushChannel.send_push)

    async def test_broadcast_runs_blocking_send_off_the_event_loop(self, push):
        """The whole reason broadcast offloads: httpx.Client blocks for 10s."""
        push.register_device("u1", "tok-123456", "fcm")
        loop_thread = threading.get_ident()
        seen: list[int] = []

        def _record(*_args, **_kwargs):
            seen.append(threading.get_ident())
            return {"success": True, "platform": "fcm"}

        with patch.object(push, "_send_one", side_effect=_record):
            await push.broadcast("u1", "T", "B")

        assert seen, "send was never invoked"
        assert seen[0] != loop_thread, (
            "send_push ran on the event-loop thread; a 10s httpx timeout "
            "would stall every other request in the brain"
        )

    async def test_broadcast_returns_one_result_per_device(self, push):
        push.register_device("u1", "tok-aaaaaa", "fcm")
        push.register_device("u1", "tok-bbbbbb", "apns")
        with patch.object(push, "_send_one", return_value={"success": True}):
            results = await push.broadcast("u1", "T", "B")
        assert len(results) == 2
        # Token suffix, never the token itself: device tokens are secrets.
        assert {r["token_suffix"] for r in results} == {"aaaaaa", "bbbbbb"}


class TestNoCredentials:
    """The real state of this machine: no .p8, no firebase JSON, 0 tokens."""

    def test_credentials_status_all_false(self, push):
        status = push.credentials_status()
        assert status == {"fcm": False, "apns": False, "any": False}

    def test_fcm_send_reports_missing_config(self, push):
        result = push._send_fcm("tok", "T", "B", None)
        assert result["success"] is False
        assert result["platform"] == "fcm"

    def test_apns_send_reports_missing_config(self, push):
        result = push._send_apns("tok", "T", "B", None)
        assert result["success"] is False
        assert result["platform"] == "apns"

    async def test_broadcast_with_token_but_no_creds_fails_honestly(self, push):
        push.register_device("u1", "tok-123456", "apns")
        results = await push.broadcast("u1", "T", "B")
        assert len(results) == 1
        assert results[0]["success"] is False
        assert "not configured" in results[0]["error"]

    def test_credentials_status_true_when_firebase_loaded(self, push):
        push._firebase_project_id = "proj"
        assert push.credentials_status()["fcm"] is True
        assert push.credentials_status()["any"] is True

    def test_apns_status_needs_key_and_team_and_key_id(self, push, tmp_path):
        key = tmp_path / "k.p8"
        key.write_text("x")
        push._apns_key_path = str(key)
        # Key file alone is not enough to sign a JWT.
        assert push.credentials_status()["apns"] is False
        push._apns_team_id = "TEAM"
        push._apns_key_id = "KEY"
        assert push.credentials_status()["apns"] is True


class TestPlatformNormalization:
    """An iOS client posting platform="ios" used to be routed to Firebase.

    send_push only tested ``platform == "apns"`` and fell through to FCM for
    every other value, so an APNs device token would have been handed to
    Firebase and rejected, with no log line saying why.
    """

    @pytest.mark.parametrize("raw,expected", [
        ("apns", "apns"), ("ios", "apns"), ("iPhone", "apns"),
        ("fcm", "fcm"), ("android", "fcm"), ("FIREBASE", "fcm"),
        ("", "fcm"), ("windows-phone", "fcm"),
    ])
    def test_normalize(self, raw, expected):
        assert _normalize_platform(raw) == expected

    def test_register_stores_normalized_platform(self, push):
        push.register_device("u1", "tok", "ios")
        assert push.get_tokens("u1")[0]["platform"] == "apns"

    def test_ios_registration_routes_to_apns(self, push):
        push.register_device("u1", "tok", "ios")
        with patch.object(push, "_send_apns", return_value={"success": True}) as mock_apns:
            with patch.object(push, "_send_fcm", return_value={"success": True}) as mock_fcm:
                asyncio.run(push.broadcast("u1", "T", "B"))
        mock_apns.assert_called_once()
        mock_fcm.assert_not_called()


class TestAPNsTopic:
    def test_bundle_id_is_not_leaked_into_the_payload(self, push, tmp_path):
        """apns-topic is routing metadata, not a user-visible payload key."""
        key = tmp_path / "k.p8"
        key.write_text("fake")
        push._apns_key_path = str(key)

        captured: dict = {}
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)

        def _post(url, json=None, headers=None):
            captured["json"] = json
            captured["headers"] = headers
            return MagicMock(status_code=200, text="ok")

        mock_client.post.side_effect = _post
        mock_httpx = MagicMock()
        mock_httpx.Client.return_value = mock_client
        with patch.object(push, "_get_apns_token", return_value="jwt"):
            with patch.dict("sys.modules", {"httpx": mock_httpx}):
                push._send_apns("tok", "T", "B", {"bundle_id": "com.example.app", "deep_link": "x"})

        assert captured["headers"]["apns-topic"] == "com.example.app"
        assert "bundle_id" not in captured["json"]
        assert captured["json"]["deep_link"] == "x"

    def test_topic_defaults_to_configured_bundle_id(self, push, tmp_path, monkeypatch):
        key = tmp_path / "k.p8"
        key.write_text("fake")
        push._apns_key_path = str(key)
        push._apns_topic = "com.feral.configured"

        captured: dict = {}
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.side_effect = lambda url, json=None, headers=None: (
            captured.update(headers=headers) or MagicMock(status_code=200, text="ok")
        )
        mock_httpx = MagicMock()
        mock_httpx.Client.return_value = mock_client
        with patch.object(push, "_get_apns_token", return_value="jwt"):
            with patch.dict("sys.modules", {"httpx": mock_httpx}):
                push._send_apns("tok", "T", "B", None)
        assert captured["headers"]["apns-topic"] == "com.feral.configured"


class TestSendPushRoute:
    """End-to-end through api/routes/timeline.py:send_push."""

    @pytest.fixture
    def route(self, push, monkeypatch):
        monkeypatch.setattr(timeline_routes.state, "push_channel", push, raising=False)
        return timeline_routes

    async def test_no_channel_returns_degraded(self, monkeypatch):
        monkeypatch.setattr(timeline_routes.state, "push_channel", None, raising=False)
        resp = await timeline_routes.send_push({"title": "T", "body": "B"})
        assert resp["success"] is False
        assert "not initialized" in resp["degraded"][0]

    async def test_no_tokens_is_not_reported_as_success(self, route, push):
        resp = await route.send_push({"user_id": "nobody", "title": "T", "body": "B"})
        # The pre-fix route returned a bare list here; "delivered" and
        # "nowhere to deliver" were the same JSON shape.
        assert resp["success"] is False
        assert resp["sent"] == 0
        assert any("no devices registered" in d for d in resp["degraded"])

    async def test_no_credentials_is_named_in_degraded(self, route, push):
        push.register_device("u1", "tok-123456", "fcm")
        resp = await route.send_push({"user_id": "u1", "title": "T", "body": "B"})
        assert resp["success"] is False
        assert resp["failed"] == 1
        assert any("no push credentials configured" in d for d in resp["degraded"])

    async def test_route_does_not_raise_the_await_typeerror(self, route, push):
        """The exact regression: the route used to return this error string."""
        push.register_device("u1", "tok-123456", "fcm")
        resp = await route.send_push({"user_id": "u1", "title": "T", "body": "B"})
        assert "await" not in str(resp.get("error", ""))
        assert "await" not in " ".join(resp["degraded"])

    async def test_successful_send_reports_counts(self, route, push):
        push.register_device("u1", "tok-aaaaaa", "fcm")
        push.register_device("u1", "tok-bbbbbb", "fcm")
        with patch.object(push, "_send_one", return_value={"success": True, "platform": "fcm"}):
            with patch.object(push, "credentials_status",
                              return_value={"fcm": True, "apns": False, "any": True}):
                resp = await route.send_push({"user_id": "u1", "title": "T", "body": "B"})
        assert resp["success"] is True
        assert resp["sent"] == 2
        assert resp["failed"] == 0
        assert resp["degraded"] == []

    async def test_partial_failure_is_visible(self, route, push):
        push.register_device("u1", "tok-aaaaaa", "fcm")
        push.register_device("u1", "tok-bbbbbb", "fcm")
        outcomes = iter([
            {"success": True, "platform": "fcm"},
            {"success": False, "platform": "fcm", "error": "UNREGISTERED"},
        ])
        with patch.object(push, "_send_one", side_effect=lambda *a, **k: next(outcomes)):
            with patch.object(push, "credentials_status",
                              return_value={"fcm": True, "apns": False, "any": True}):
                resp = await route.send_push({"user_id": "u1", "title": "T", "body": "B"})
        assert resp["success"] is True  # one device did get it
        assert (resp["sent"], resp["failed"]) == (1, 1)
        assert any("UNREGISTERED" in d for d in resp["degraded"])

    async def test_broadcast_exception_is_reported_not_swallowed(self, route, push):
        push.register_device("u1", "tok-123456", "fcm")
        with patch.object(push, "broadcast", side_effect=RuntimeError("boom")):
            resp = await route.send_push({"user_id": "u1", "title": "T", "body": "B"})
        assert resp["success"] is False
        assert any("RuntimeError: boom" in d for d in resp["degraded"])

    async def test_register_echoes_normalized_platform(self, route, push):
        resp = await route.register_push_device({"user_id": "u1", "token": "t", "platform": "ios"})
        assert resp == {"success": True, "platform": "apns"}
