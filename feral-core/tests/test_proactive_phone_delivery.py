"""Proactive messages reach a bound, capable phone without blocking peers."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agents.proactive_engine import Priority, ProactiveMessage
from api.state import BrainState
from models.protocol import HUP_VERSION


class _Socket:
    def __init__(
        self,
        *,
        node_type: str,
        platform: str,
        capabilities: list[str] | None = None,
    ):
        self._feral_node_type = node_type
        self._feral_platform = platform
        self._feral_capabilities = capabilities or []
        self.frames: list[dict] = []

    async def send_json(self, frame: dict) -> None:
        self.frames.append(frame)


def _state() -> BrainState:
    state = BrainState.__new__(BrainState)
    state.primary_session_id = "shared-session"
    state._daemon_session_bindings = {}
    state.daemons = {}
    return state


@pytest.mark.asyncio
async def test_ambient_text_response_goes_only_to_bound_phone_nodes():
    state = _state()
    phone = _Socket(
        node_type="phone",
        platform="ios",
        capabilities=["ambient_delivery"],
    )
    unbound_phone = _Socket(
        node_type="phone",
        platform="ios",
        capabilities=["ambient_delivery"],
    )
    robot = _Socket(node_type="robot", platform="linux")
    state.daemons = {
        "phone-bound": phone,
        "phone-unbound": unbound_phone,
        "robot": robot,
    }
    state._daemon_session_bindings = {
        "phone-bound": {"shared-session"},
        "phone-unbound": {"another-session"},
        "robot": {"shared-session"},
    }

    delivered = await state._deliver_proactive_to_phone_nodes({
        "trigger_id": "hr_elevated",
        "priority": "IMPORTANT",
        "title": "Heart rate changed",
        "body": "I noticed a sustained change.",
        "voice_text": "I noticed a sustained change.",
        "context": {
            "metric": "heart_rate",
            "source": "jw_health_glasses",
            "sample_age_s": 3,
        },
    })

    assert delivered == 1
    assert unbound_phone.frames == []
    assert robot.frames == []
    assert len(phone.frames) == 1
    frame = phone.frames[0]
    assert frame["hup_version"] == HUP_VERSION
    assert frame["type"] == "text_response"
    assert frame["payload"]["session_id"] == "shared-session"
    assert frame["payload"]["channel"] == "ambient"
    assert frame["payload"]["trigger_id"] == "hr_elevated"
    assert frame["payload"]["context"]["source"] == "jw_health_glasses"


@pytest.mark.asyncio
async def test_ambient_delivery_requires_explicit_node_capability():
    state = _state()
    capable = _Socket(
        node_type="phone",
        platform="ios",
        capabilities=["ambient_delivery"],
    )
    legacy = _Socket(node_type="phone", platform="ios")
    state.daemons = {"capable": capable, "legacy": legacy}
    state._daemon_session_bindings = {
        "capable": {"shared-session"},
        "legacy": {"shared-session"},
    }

    delivered = await state._deliver_proactive_to_phone_nodes({"body": "Hello"})

    assert delivered == 1
    assert len(capable.frames) == 1
    assert legacy.frames == []


@pytest.mark.asyncio
async def test_one_backpressured_phone_does_not_block_other_delivery(monkeypatch):
    import api.state as state_module

    class _BlockedSocket(_Socket):
        async def send_json(self, frame: dict) -> None:
            await asyncio.Event().wait()

    monkeypatch.setattr(state_module, "PROACTIVE_NODE_SEND_TIMEOUT_S", 0.01)
    state = _state()
    blocked = _BlockedSocket(
        node_type="phone",
        platform="ios",
        capabilities=["ambient_delivery"],
    )
    healthy = _Socket(
        node_type="phone",
        platform="android",
        capabilities=["ambient_delivery"],
    )
    state.daemons = {"blocked": blocked, "healthy": healthy}
    state._daemon_session_bindings = {
        "blocked": {"shared-session"},
        "healthy": {"shared-session"},
    }

    delivered = await state._deliver_proactive_to_phone_nodes({"body": "Hello"})

    assert delivered == 1
    assert len(healthy.frames) == 1


def test_daemon_binding_cleanup_is_explicit():
    state = _state()
    state._daemon_session_bindings = {"phone": {"one", "two"}}

    removed = state.clear_daemon_bindings("phone")

    assert removed == {"one", "two"}
    assert state.get_sessions_for_daemon("phone") == set()


@pytest.mark.asyncio
async def test_recording_proactive_turn_preserves_trigger_context():
    state = _state()
    note_turn = AsyncMock()
    state.orchestrator = SimpleNamespace(
        note_proactive_assistant_turn=note_turn,
    )
    msg = ProactiveMessage(
        trigger_id="spo2_low",
        priority=Priority.CRITICAL,
        title="Low blood oxygen",
        body="Your recent reading is low.",
        context={"metric": "spo2", "value": 91},
    )

    await state._record_proactive_assistant_turn(msg)

    note_turn.assert_awaited_once_with(
        "shared-session",
        "Your recent reading is low.",
        trigger_id="spo2_low",
        priority="CRITICAL",
        context={"metric": "spo2", "value": 91},
    )
