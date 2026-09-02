"""HUP_SPEC.md section 6, node side.

Two defects, one test file. The TypeScript SDK had both in the same
shape; see ``feral-nodes/ts-node-sdk/tests/capabilityGrants.test.ts``.

1. **The grant was never read.** ``self._granted`` appeared at exactly
   three lines: declared, assigned from ``node_ack``, and logged. Nothing
   consulted it before dispatching. A handler registered for a capability
   the operator had denied ran exactly as if it had not been. The spec's
   "nodes MUST refuse any ``hup_action_request`` whose ``name`` is not in
   their registered capabilities" was satisfied only incidentally, by the
   handler lookup missing, which answers a different question.

2. **The fallback failed open.** The assignment was

       set(payload.get("granted_capabilities") or self.capabilities)

   so an empty list -- a brain saying "you may do nothing" -- was falsy
   and got replaced with everything this node declared. The one answer
   the operator most needs to be able to give was the one that could not
   be transmitted.

``asyncio.run`` in sync tests rather than ``@pytest.mark.asyncio``: this
SDK's dev extra carries pytest and pytest-timeout and no async plugin, so
an asyncio-marked test is a warning and a no-op in the
``Subprojects -- pytest`` CI job. ``tests/test_node_bye.py`` drives its
coroutines the same way.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from feral_node_sdk import FeralNode


@pytest.fixture
def node():
    return FeralNode(
        node_id="grant-test-node",
        node_type="sensor",
        capabilities=["camera", "heart_rate", "buzzer"],
        brain_url="ws://localhost:9999/v1/node",
        api_key="test-key",
    )


@pytest.fixture
def responses(node):
    """Capture ``_send_action_response`` instead of writing to a socket."""
    captured: list = []

    async def _capture(action_id, **kwargs):
        captured.append({"action_id": action_id, **kwargs})

    node._send_action_response = _capture
    return captured


class _AckSocket:
    """An async-iterable stand-in for the brain's side of the socket."""

    def __init__(self, frames):
        self._frames = list(frames)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._frames:
            raise StopAsyncIteration
        return self._frames.pop(0)


def _feed_ack(node, payload: dict) -> None:
    node._ws = _AckSocket([json.dumps({"type": "node_ack", "payload": payload})])
    asyncio.run(node._read_loop())


def _dispatch(node, name: str, action_id: str = "a1") -> None:
    asyncio.run(
        node._dispatch_action({"action_id": action_id, "name": name, "params": {}})
    )


async def _record(sink, _params):
    sink.append(True)
    return {"ok": True}


def _handler_into(sink):
    async def _fn(params):
        return await _record(sink, params)
    return _fn


class TestNodeAckGrantHandling:
    def test_granted_is_none_before_any_ack(self, node):
        # Not the empty set. The dispatcher has to tell "nobody told me
        # yet" apart from "you may do nothing", and the old ``set()``
        # initial value made those the same value.
        assert node._granted is None

    def test_an_explicit_grant_list_is_taken_verbatim(self, node):
        _feed_ack(node, {
            "granted_capabilities": ["heart_rate"],
            "denied_capabilities": ["camera", "buzzer"],
        })
        assert node._granted == {"heart_rate"}

    def test_an_empty_grant_list_is_a_full_deny(self, node):
        _feed_ack(node, {"granted_capabilities": []})
        assert node._granted == set()

    def test_an_omitted_key_falls_back_to_the_declaration(self, node):
        # A brain with no grant store. Only this case may be read as
        # "everything I declared".
        _feed_ack(node, {"heartbeat_ms": 5000})
        assert node._granted == {"camera", "heart_rate", "buzzer"}

    def test_the_session_token_still_lands(self, node):
        _feed_ack(node, {"session_token": "tok-1", "granted_capabilities": []})
        assert node._session_token == "tok-1"


class TestDispatchHonoursTheGrant:
    def test_a_denied_capability_is_refused(self, node, responses):
        ran: list = []
        node.on_action("camera")(_handler_into(ran))
        _feed_ack(node, {"granted_capabilities": ["heart_rate"]})
        _dispatch(node, "camera")
        assert ran == []
        assert responses[0]["success"] is False
        assert "capability_denied" in responses[0]["error"]

    def test_an_empty_grant_denies_everything(self, node, responses):
        ran: list = []
        node.on_action("buzzer")(_handler_into(ran))
        _feed_ack(node, {"granted_capabilities": []})
        _dispatch(node, "buzzer")
        assert ran == []
        assert responses[0]["success"] is False

    def test_a_granted_capability_still_runs(self, node, responses):
        ran: list = []
        node.on_action("buzzer")(_handler_into(ran))
        _feed_ack(node, {"granted_capabilities": ["buzzer"]})
        _dispatch(node, "buzzer")
        assert ran == [True]
        assert responses[0]["success"] is True

    def test_dispatch_before_any_ack_is_not_blocked(self, node, responses):
        # Fail-open on "nobody has told me yet" is deliberate: a node that
        # reconnects and is handed an action before its ack must not
        # refuse it.
        ran: list = []
        node.on_action("buzzer")(_handler_into(ran))
        _dispatch(node, "buzzer")
        assert ran == [True]
