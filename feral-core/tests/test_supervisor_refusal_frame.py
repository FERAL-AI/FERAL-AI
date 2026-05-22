"""Lane 08 WS6 — supervisor denials surface as a structured WS frame.

Before WS6: ``Supervisor.wrap`` raised ``SupervisorBlocked`` when
paused or when the policy gate denied a request. The WS handler in
``api/server.py`` saw the bare exception and rendered it as a 500-ish
error toast on the WebUI, which (a) didn't tell the user how to fix
the deny and (b) couldn't be styled as a refusal chip.

After WS6: the wrap layer catches the deny inline and emits

    {type: "refusal", payload: {reason, retry_hint, source, kind}}

over the session's WS, then returns ``None`` so the WS handler stays
on the happy path. The user sees a yellow refusal chip with an
actionable hint.

This module pins:

  1. Paused supervisor → ``refusal`` frame with reason
     ``supervisor_paused`` and a retry hint that points to Settings.
  2. Policy denied → ``refusal`` frame with reason ``policy_denied``.
  3. The wrapped call returns ``None`` (no exception bubbles up).
  4. The audit event is still recorded (kill-switch oversight intact).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.supervisor import Supervisor, SupervisorStore


class _OrchStub:
    """Just enough of Orchestrator's surface for the supervisor to
    decorate ``handle_command`` and emit refusal frames via ``send``.
    """

    def __init__(self) -> None:
        self.send = AsyncMock()
        self.sent_frames: list[dict] = []
        self.handle_command_called = False

        async def _record(session_id: str, msg: Any) -> None:
            self.sent_frames.append(
                msg.model_dump() if hasattr(msg, "model_dump") else dict(msg)
            )

        self.send = _record

    async def handle_command(self, session_id: str, text: str, context: dict | None = None):
        # If this runs, the deny gate FAILED to intercept.
        self.handle_command_called = True
        return {"ok": True}


@pytest.fixture
def store(tmp_path):
    return SupervisorStore(db_path=str(tmp_path / "supervisor.db"))


@pytest.mark.asyncio
async def test_paused_supervisor_emits_refusal_frame(store):
    sup = Supervisor(store=store)
    orch = _OrchStub()
    sup.wrap(orch)
    sup.set_paused(True)

    result = await orch.handle_command(session_id="s-aaaaaaaa", text="open finder")

    # No exception propagated; the wrapper returned None instead.
    assert result is None
    assert orch.handle_command_called is False

    # Exactly one ``refusal`` frame on the wire.
    refusals = [f for f in orch.sent_frames if f.get("type") == "refusal"]
    assert len(refusals) == 1, f"expected 1 refusal frame, got {orch.sent_frames}"
    payload = refusals[0]["payload"]
    assert payload["reason"] == "supervisor_paused"
    assert payload["source"] == "supervisor"
    assert payload["kind"] == "handle_command"
    assert payload["retry_hint"], "retry_hint must be non-empty"
    assert "Settings" in payload["retry_hint"] or "supervisor" in payload["retry_hint"].lower()

    # Audit row recorded.
    recent = sup.recent(limit=5)
    assert any(
        r["decision"] == "denied" and r.get("detail", {}).get("reason") == "supervisor_paused"
        for r in recent
    )


@pytest.mark.asyncio
async def test_policy_denied_emits_refusal_frame(store):
    sup = Supervisor(
        store=store,
        policy_gate=lambda event: "denied",
    )
    orch = _OrchStub()
    sup.wrap(orch)

    result = await orch.handle_command(session_id="s-bbbbbbbb", text="delete everything")

    assert result is None
    assert orch.handle_command_called is False

    refusals = [f for f in orch.sent_frames if f.get("type") == "refusal"]
    assert len(refusals) == 1
    payload = refusals[0]["payload"]
    assert payload["reason"] == "policy_denied"
    assert payload["source"] == "supervisor"
    assert payload["kind"] == "handle_command"
    assert payload["retry_hint"]

    recent = sup.recent(limit=5)
    assert any(
        r["decision"] == "denied" and r.get("detail", {}).get("reason") == "policy_denied"
        for r in recent
    )


@pytest.mark.asyncio
async def test_allowed_request_passes_through_without_refusal(store):
    sup = Supervisor(store=store)
    orch = _OrchStub()
    sup.wrap(orch)

    result = await orch.handle_command(session_id="s", text="hi")

    # Allowed request reached the orchestrator and returned its
    # normal payload; no refusal frame was emitted.
    assert orch.handle_command_called is True
    assert result == {"ok": True}
    assert not any(f.get("type") == "refusal" for f in orch.sent_frames)


@pytest.mark.asyncio
async def test_refusal_frame_does_not_raise_when_send_fails(store):
    """Best-effort emission: a broken WS must not propagate into the
    handler. The deny + audit still complete."""
    sup = Supervisor(store=store)

    class _BrokenSendOrch(_OrchStub):
        async def send(self, session_id: str, msg: Any) -> None:
            raise RuntimeError("ws gone")

    orch = _BrokenSendOrch()
    sup.wrap(orch)
    sup.set_paused(True)

    result = await orch.handle_command(session_id="s", text="hi")
    assert result is None

    recent = sup.recent(limit=5)
    assert any(r["decision"] == "denied" for r in recent), recent
