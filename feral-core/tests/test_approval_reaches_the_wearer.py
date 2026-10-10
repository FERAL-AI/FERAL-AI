"""An approval has to reach whatever the operator is actually wearing.

A pending approval only ever posted to the Mac web UI, and the phone was
told "FERAL is waiting for your approval on the Mac". For anything bought
or acted on while out of the house, that is useless.

Every node on the session now gets an approval_request frame carrying a
sentence the glasses can speak, and an approval_resolved frame when it is
settled, so a prompt answered on the phone stops being asked by the
glasses. A prompt nobody answered expires rather than staying answerable
from a pocket an hour later.
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest

from agents.tool_runner import ToolRunner
from models.protocol import ApprovalRequestPayload, ApprovalResolvedPayload

SESSION = "s-approval"


def _runner() -> ToolRunner:
    runner = ToolRunner.__new__(ToolRunner)
    runner._pending_approvals = {}
    runner._pending_task_origins = {}
    runner._pending_phone_reviews = {}
    runner._pending_scope_kinds = {}
    return runner


def _denial(**over):
    base = {
        "status": "pending_approval",
        "request_id": "req-1",
        "tool_name": "web_actions__make_purchase",
        "args": {"url": "https://www.noon.com/item", "item_description": "Oat flat white"},
        "safety_level": "confirm",
        "created_at": time.time(),
        "expires_at": time.time() + 300,
    }
    base.update(over)
    return base


async def _pushed(monkeypatch, runner, denial):
    sent: list[dict] = []

    class _State:
        async def push_to_session_nodes(self, session_id, msg):
            sent.append({"session_id": session_id, **msg})

    import api.state as state_module
    monkeypatch.setattr(state_module, "state", _State())
    await runner._push_approval_request(SESSION, denial)
    return sent


class TestRequestFrame:
    @pytest.mark.asyncio
    async def test_the_frame_reaches_the_session_nodes(self, monkeypatch):
        sent = await _pushed(monkeypatch, _runner(), _denial())
        assert len(sent) == 1
        assert sent[0]["type"] == "approval_request"
        payload = ApprovalRequestPayload(**sent[0]["payload"])
        assert payload.request_id == "req-1"
        assert payload.merchant == "noon.com"
        assert payload.detail == "Oat flat white"
        assert payload.expires_at > payload.created_at

    @pytest.mark.asyncio
    async def test_it_carries_a_sentence_the_glasses_can_speak(self, monkeypatch):
        sent = await _pushed(monkeypatch, _runner(), _denial())
        speak = sent[0]["payload"]["speak"]
        assert speak == "Approve web actions make purchase at noon.com?"
        assert "__" not in speak, "a spoken prompt must not read a tool id aloud"

    @pytest.mark.asyncio
    async def test_a_known_amount_is_spoken(self, monkeypatch):
        """Present only when the caller already knows it."""
        denial = _denial(args={"merchant": "noon.com", "amount": "24.00", "currency": "AED"})
        sent = await _pushed(monkeypatch, _runner(), denial)
        assert sent[0]["payload"]["speak"] == "Approve 24.00 AED at noon.com?"

    @pytest.mark.asyncio
    async def test_a_scraped_purchase_has_no_amount_yet(self, monkeypatch):
        """make_purchase is given no price; it discovers one after this."""
        sent = await _pushed(monkeypatch, _runner(), _denial())
        assert sent[0]["payload"]["amount"] == ""

    @pytest.mark.asyncio
    async def test_a_push_failure_never_breaks_the_turn(self, monkeypatch):
        class _Boom:
            async def push_to_session_nodes(self, *a, **k):
                raise RuntimeError("socket gone")

        import api.state as state_module
        monkeypatch.setattr(state_module, "state", _Boom())
        await _runner()._push_approval_request(SESSION, _denial())  # must not raise


class TestExpiry:
    def test_an_expired_approval_is_no_longer_answerable(self):
        runner = _runner()
        runner._pending_approvals["req-1"] = _denial(expires_at=time.time() - 1)
        assert runner.get_pending("req-1") is None
        assert "req-1" not in runner._pending_approvals

    def test_a_live_one_still_is(self):
        runner = _runner()
        runner._pending_approvals["req-1"] = _denial()
        assert runner.get_pending("req-1")["request_id"] == "req-1"

    def test_the_ttl_has_a_floor(self, monkeypatch):
        monkeypatch.setattr(
            "config.loader.load_settings", lambda: {"security": {"approval_ttl_seconds": 1}})
        assert ToolRunner._approval_ttl_seconds() >= 30


class TestResolvedFrame:
    @pytest.mark.asyncio
    async def test_resolution_tells_every_surface_to_stop_asking(self, monkeypatch):
        from agents.orchestrator import Orchestrator as _Orch

        sent: list[dict] = []

        class _State:
            async def push_to_session_nodes(self, session_id, msg):
                sent.append(msg)

        import api.state as state_module
        monkeypatch.setattr(state_module, "state", _State())
        orch = _Orch.__new__(_Orch)
        await orch._push_approval_resolved(SESSION, "req-1", "approved", "tool", "phone")

        assert sent[0]["type"] == "approval_resolved"
        payload = ApprovalResolvedPayload(**sent[0]["payload"])
        assert (payload.outcome, payload.resolved_by) == ("approved", "phone")
