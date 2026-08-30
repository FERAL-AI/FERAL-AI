"""The robot template must not teach fake acknowledgements.

``robot_template.py`` is the file a node author copies to attach a
physical robot to the brain. Whatever it does is what every third-party
actuator node will do.

The brain side already refuses to fake success:

* ``feral-core/hardware/capability_skill.py`` re-reads telemetry after an
  actuator call and, on mismatch, returns ``success=False`` with "Do NOT
  claim it worked — report the device's actual state".
* ``feral-core/hardware/adapters/robot_arm.py`` refuses to connect at all
  without a real serial transport, because an adapter that reports
  success for movements that never happened gives the LLM a tool named
  "move the robot" that always says it worked.

The template must hold the same line: report what actually happened, or
refuse.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_TEMPLATE_DIR = Path(__file__).resolve().parents[1]
if str(_TEMPLATE_DIR) not in sys.path:
    sys.path.insert(0, str(_TEMPLATE_DIR))

import robot_template  # noqa: E402


class FakeTransport(robot_template.RobotTransport):
    """A transport whose read-back is under the test's control."""

    def __init__(self, state: dict, *, fail_on: str = ""):
        self.state = dict(state)
        self.fail_on = fail_on
        self.calls: list[tuple] = []
        self.connected = True

    async def move(self, direction: str, speed: int) -> None:
        self.calls.append(("move", direction, speed))
        if self.fail_on == "move":
            raise RuntimeError("motor driver returned NAK")

    async def grip(self, action: str) -> None:
        self.calls.append(("grip", action))
        if self.fail_on == "grip":
            raise RuntimeError("gripper stalled")

    async def read_state(self) -> dict | None:
        if self.fail_on == "read":
            return None
        return dict(self.state)


def _node(transport=None) -> robot_template.RobotNode:
    return robot_template.RobotNode("ws://localhost:9090", "k", transport=transport)


# ─────────────────────────────────────────────
# 1. No transport → refuse, never acknowledge
# ─────────────────────────────────────────────


def test_move_without_transport_is_refused_not_acked():
    """The defect: `success = True` before any hardware was touched."""
    node = _node(transport=None)
    resp = asyncio.run(node.build_result({"executor": "robot_move",
                                          "args": {"direction": "forward"}}, "m1"))
    payload = resp["payload"]
    assert payload["status"] == "error", (
        "a node with no transport must refuse, not report a move it never made"
    )
    assert payload["stdout"] == ""
    assert "transport" in payload["error"].lower()


def test_grip_without_transport_is_refused_not_acked():
    node = _node(transport=None)
    resp = asyncio.run(node.build_result({"executor": "robot_grip",
                                          "args": {"action": "close"}}, "m2"))
    assert resp["payload"]["status"] == "error"


def test_transportless_node_does_not_advertise_actuator_capabilities():
    """Advertising `robot_move` you cannot perform is the same lie, earlier."""
    caps = _node(transport=None).capabilities()
    assert "robot_move" not in caps
    assert "robot_grip" not in caps


def test_node_with_transport_does_advertise_actuator_capabilities():
    caps = _node(transport=FakeTransport({"motion": "idle"})).capabilities()
    assert "robot_move" in caps
    assert "robot_grip" in caps


# ─────────────────────────────────────────────
# 2. Verify after actuating (capability_skill.py parity)
# ─────────────────────────────────────────────


def test_move_verified_by_telemetry_reports_success():
    transport = FakeTransport({"motion": "forward"})
    node = _node(transport)
    payload = asyncio.run(node.build_result(
        {"executor": "robot_move", "args": {"direction": "forward"}}, "m3"))["payload"]
    assert payload["status"] == "success"
    assert payload["verified"] is True
    assert payload["observed"] == "forward"
    assert transport.calls == [("move", "forward", 30)]


def test_move_contradicted_by_telemetry_is_a_failure():
    """The brain's rule, applied at the node: telemetry wins."""
    transport = FakeTransport({"motion": "idle"})
    node = _node(transport)
    payload = asyncio.run(node.build_result(
        {"executor": "robot_move", "args": {"direction": "forward"}}, "m4"))["payload"]
    assert payload["status"] == "error"
    assert payload["verified"] is False
    assert payload["observed"] == "idle"
    assert "Do NOT claim it worked" in payload["error"]


def test_grip_contradicted_by_telemetry_is_a_failure():
    transport = FakeTransport({"gripper": "open"})
    node = _node(transport)
    payload = asyncio.run(node.build_result(
        {"executor": "robot_grip", "args": {"action": "close"}}, "m5"))["payload"]
    assert payload["status"] == "error"
    assert payload["verified"] is False


def test_unreadable_telemetry_is_reported_as_unknown_not_success():
    """Acked but unverifiable: verified is None and the text says so."""
    transport = FakeTransport({"motion": "forward"}, fail_on="read")
    node = _node(transport)
    payload = asyncio.run(node.build_result(
        {"executor": "robot_move", "args": {"direction": "forward"}}, "m6"))["payload"]
    assert payload["verified"] is None
    assert "could not" in payload["stdout"].lower()
    assert "successfully" not in payload["stdout"].lower()


def test_transport_error_is_reported_as_error():
    transport = FakeTransport({"motion": "idle"}, fail_on="move")
    node = _node(transport)
    payload = asyncio.run(node.build_result(
        {"executor": "robot_move", "args": {"direction": "forward"}}, "m7"))["payload"]
    assert payload["status"] == "error"
    assert "NAK" in payload["error"]


def test_unknown_command_still_refused():
    node = _node(FakeTransport({"motion": "idle"}))
    payload = asyncio.run(node.build_result({"executor": "self_destruct"}, "m8"))["payload"]
    assert payload["status"] == "error"
    assert "Unknown command" in payload["error"]


# ─────────────────────────────────────────────
# 3. Telemetry must be read, never invented
# ─────────────────────────────────────────────


def test_telemetry_is_not_fabricated_without_a_transport():
    """The template used to publish battery_pct=98.5 from nothing."""
    assert asyncio.run(_node(transport=None).build_telemetry()) is None


def test_telemetry_comes_from_the_transport():
    transport = FakeTransport({"battery_pct": 41.0, "motion": "idle"})
    frame = asyncio.run(_node(transport).build_telemetry())
    assert frame is not None
    assert frame["payload"]["sensors"] == {"battery_pct": 41.0, "motion": "idle"}


def test_no_hardcoded_sensor_values_in_the_template_source():
    """A template is read as much as it is run."""
    src = (_TEMPLATE_DIR / "robot_template.py").read_text()
    assert "98.5" not in src
    assert "Simulating physical state feedback" not in src


@pytest.mark.parametrize("banned", ["Moved robot", "successful"])
def test_no_unconditional_success_strings(banned):
    src = (_TEMPLATE_DIR / "robot_template.py").read_text()
    assert f'f"{banned}' not in src and f'"{banned}' not in src
