"""The multi-agent worker path must not destroy tool-result images.

``AgentWorker.run`` serialized every tool result with
``serialize_tool_result``, which stringifies the whole envelope and
clamps it to the per-tool character budget. A screenshot is about
400 000 base64 characters, so the worker received roughly 1 400
characters of truncated base64 and a note saying the result was cut.
It could not decode that into anything.

This matters more than a subagent edge case: the comment at the gate in
this same function records that ``features.multi_agent`` defaults to
True, so this is the primary text chat path (iOS chat and voice both
ride it).

Images now travel out of band keyed by tool_call_id and are spliced into
a per-request copy of the message list, which is the same discipline the
orchestrator uses so history stays provider-agnostic and survives a
failover to a provider that cannot accept images.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

from agents.multimodal_blocks import (
    IMAGE_DELIVERY_FOLLOWUP_USER,
    IMAGE_DELIVERY_NONE,
    materialize_tool_result_images,
)
from skills.result_budget import serialize_tool_result, serialize_tool_result_with_images

BIG_IMAGE = "/9j/4AAQ" + "A" * 400_000
SCREENSHOT = {
    "success": True,
    "status_code": 200,
    "data": {"image_base64": BIG_IMAGE, "width": 1920, "height": 1200},
    "error": None,
}


class TestTheImageSurvives:
    def test_the_old_path_destroyed_it(self):
        """Pins the defect so the fix cannot be quietly reverted."""
        text = serialize_tool_result("gui_computer_use__screenshot", SCREENSHOT)
        assert len(text) < 5000
        assert BIG_IMAGE not in text

    def test_the_new_path_keeps_it_whole(self):
        text, images = serialize_tool_result_with_images(
            "gui_computer_use__screenshot", SCREENSHOT,
        )
        assert len(images) == 1, "the screenshot was not extracted as an image"
        assert images[0].data_url.split(",", 1)[1] == BIG_IMAGE, "image was altered"
        assert BIG_IMAGE not in text, "the image is still riding in the text half"
        assert len(text) < 5000, "the text half is no longer budgeted"


class TestTheSpliceIsCorrect:
    def _messages(self):
        return [
            {"role": "user", "content": "what is on my screen"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
            {"role": "tool", "tool_call_id": "call_1", "name": "gui_computer_use__screenshot",
             "content": "{\"success\": true}"},
        ]

    def _table(self):
        _, images = serialize_tool_result_with_images(
            "gui_computer_use__screenshot", SCREENSHOT,
        )
        return {"call_1": {"images": [i.to_dict() for i in images], "pruned": False,
                           "tool_name": "gui_computer_use__screenshot"}}

    def test_an_image_reaches_the_request(self):
        out = materialize_tool_result_images(
            self._messages(), self._table(), IMAGE_DELIVERY_FOLLOWUP_USER,
        )
        assert len(out) == 4, "no follow-up user message was added"
        assert out[-1]["role"] == "user"

    def test_the_stored_messages_are_never_mutated(self):
        msgs = self._messages()
        before = [dict(m) for m in msgs]
        materialize_tool_result_images(msgs, self._table(), IMAGE_DELIVERY_FOLLOWUP_USER)
        assert msgs == before, "history was mutated; it must stay replayable"

    def test_a_text_only_provider_gets_no_image(self):
        out = materialize_tool_result_images(
            self._messages(), self._table(), IMAGE_DELIVERY_NONE,
        )
        for m in out:
            assert BIG_IMAGE not in str(m), "image sent to a provider that cannot take it"


def test_the_worker_no_longer_uses_the_destroying_serializer():
    """Structural guard.

    ``serialize_tool_result`` clamps images out of existence. The worker
    must use the image-preserving variant, and must splice before it
    calls the model, or the side table is populated and never read.
    """
    from agents.multi_agent import AgentWorker

    src = inspect.getsource(AgentWorker.run)
    tree = ast.parse(textwrap.dedent(src))
    called = {
        n.func.id for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "serialize_tool_result_with_images" in called, (
        "AgentWorker.run does not use the image-preserving serializer"
    )
    assert "serialize_tool_result" not in called, (
        "AgentWorker.run still calls the serializer that destroys images"
    )
    assert "_materialize" in called, (
        "images are stashed but never spliced into the outgoing request"
    )
