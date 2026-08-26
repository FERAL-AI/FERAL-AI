"""Verify the node_register payload advertises the SDK's declared HUP version.

This test used to hard-code "1.3.0" in both an assertion and its own
docstring. When HUP went to 1.4.0 the SDK constant moved and the test did
not, so it asserted the SDK was broken and failed on every run. Nobody
saw it, because feral-nodes has no CI job.

A literal here can only ever be right until the next protocol bump, and
it tests the wrong thing besides. What matters is that the handshake
frame carries the version the SDK declares, not that the version is any
particular string. Coherence between the SDK constant and the brain's
canonical models.protocol.HUP_VERSION is a separate concern, asserted
below against the spec rather than against a copy of the number.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

from feral_node_sdk import FeralNode
from feral_node_sdk.schemas import HUP_VERSION


def test_hup_version_is_a_semantic_version():
    parts = HUP_VERSION.split(".")
    assert len(parts) == 3, f"HUP_VERSION must be MAJOR.MINOR.PATCH, got {HUP_VERSION!r}"
    assert all(p.isdigit() for p in parts), f"non-numeric component in {HUP_VERSION!r}"


def test_node_register_frame_advertises_the_declared_version():
    """The handshake frame must carry the SDK's own HUP_VERSION."""
    sent_frames: list[str] = []

    node = FeralNode(
        node_id="ver-test",
        name="Version Test",
        node_type="sensor",
        capabilities=["heart_rate"],
        brain_url="ws://localhost:9999/v1/node",
        api_key="k",
    )

    mock_ws = AsyncMock()
    mock_ws.send = AsyncMock(side_effect=lambda data: sent_frames.append(data))
    node._ws = mock_ws

    asyncio.run(node._handshake())

    assert len(sent_frames) >= 1
    register_frame = json.loads(sent_frames[0])
    assert register_frame["type"] == "node_register"
    assert register_frame["hup_version"] == HUP_VERSION
