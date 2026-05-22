"""Verify node_register payload advertises HUP v1.3.0."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

from feral_node_sdk import FeralNode
from feral_node_sdk.schemas import HUP_VERSION


def test_hup_version_is_1_3_0():
    assert HUP_VERSION == "1.3.0"


def test_node_register_frame_contains_version():
    """The handshake frame must carry hup_version='1.3.0' in the envelope."""
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
    assert register_frame["hup_version"] == "1.3.0"
