"""Saving a conversation must survive multimodal message content.

`conversation_save` derived its title/preview with `msg["content"][:120]`,
which assumes the string shape. OpenAI-shaped messages carry a LIST of typed
content blocks once vision, screen-attach or file attachments are involved.
Slicing a list yields a list, and binding that to a TEXT column raises

    sqlite3.ProgrammingError: Error binding parameter 2:
    type 'list' is not supported

which surfaced as a 500 on POST /api/conversations/save and lost the whole
thread. Operator report 2026-07-30, reproduced from a screenshot showing
`/api/conversations/save 500` on a thread containing a vision turn.
"""

from __future__ import annotations

import pytest

from memory.store import MemoryStore, _message_text


@pytest.fixture
async def store(tmp_path):
    s = MemoryStore(db_path=str(tmp_path / "m.db"))
    yield s
    try:
        await s.aclose()
    except Exception:
        pass


class TestMessageTextFlattening:
    def test_plain_string_passes_through(self):
        assert _message_text("hello") == "hello"

    def test_multimodal_list_yields_a_string(self):
        out = _message_text([
            {"type": "text", "text": "what is this"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ])
        assert isinstance(out, str)
        assert "what is this" in out
        assert "[image]" in out

    def test_base64_payload_is_not_inlined(self):
        """A data: URL must never end up in a preview column."""
        out = _message_text([
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 5000}},
        ])
        assert "base64" not in out
        assert len(out) < 50

    @pytest.mark.parametrize("value", [None, "", [], 123, {"unexpected": "shape"}])
    def test_odd_shapes_return_a_string(self, value):
        assert isinstance(_message_text(value), str)


class TestConversationSave:
    async def test_multimodal_turn_saves(self, store):
        """The exact shape that produced the 500."""
        result = await store.conversation_save("c1", [{
            "role": "user",
            "content": [
                {"type": "text", "text": "what is this"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ],
        }])
        assert result["id"] == "c1"
        assert isinstance(result["title"], str)
        assert "what is this" in result["title"]

    async def test_image_only_turn_saves(self, store):
        result = await store.conversation_save("c2", [{
            "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": "x"}}],
        }])
        assert isinstance(result["title"], str)

    async def test_plain_string_still_works(self, store):
        result = await store.conversation_save("c3", [
            {"role": "user", "content": "hello there"},
        ])
        assert result["title"] == "hello there"

    async def test_mixed_thread_saves(self, store):
        """A realistic thread: text turns and a vision turn interleaved."""
        result = await store.conversation_save("c4", [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": [
                {"type": "text", "text": "look at this"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,BBBB"}},
            ]},
        ])
        assert result["message_count"] == 3
        assert isinstance(result["title"], str)
