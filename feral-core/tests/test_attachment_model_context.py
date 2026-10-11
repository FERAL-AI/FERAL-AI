from pathlib import Path
import json

import pytest

from memory.attachment_context import attachment_model_context, append_attachment_model_data, durable_chat_context, MAX_TEXT_BYTES
from memory.uploads import UploadStore


@pytest.fixture
def uploads(tmp_path):
    return UploadStore(root=tmp_path / "uploads")


def test_requires_explicit_boolean_authorization(uploads):
    record = uploads.store(data=b"private fixture", filename="note.txt", content_type="text/plain")
    for consent in (False, None, "true", 1):
        result = attachment_model_context(uploads, [record.as_dict()], authorized=consent)
        assert "private fixture" not in result
        assert "not authorized" in result


def test_canonical_content_not_client_supplied_metadata(uploads):
    record = uploads.store(data=b"violet maple 47", filename="note.txt", content_type="text/plain")
    ref = {**record.as_dict(), "path": "/etc/passwd", "filename": "forged", "content_type": "application/pdf"}
    result = attachment_model_context(uploads, [ref], authorized=True)
    assert "violet maple 47" in result
    assert '"filename": "note.txt"' in result
    assert "/etc/passwd" not in result
    assert "untrusted document data" in result


def test_traversal_unknown_and_missing_store_do_not_read(uploads):
    assert "invalid_upload_reference" in attachment_model_context(uploads, [{"upload_id": "../secret"}], authorized=True)
    assert "upload_not_found" in attachment_model_context(uploads, [{"upload_id": "a" * 32}], authorized=True)
    assert "upload_not_found" in attachment_model_context(None, [{"upload_id": "a" * 32}], authorized=True)


def test_changed_file_and_symlink_refused(uploads, tmp_path):
    record = uploads.store(data=b"original", filename="note.txt", content_type="text/plain")
    path = Path(record.path)
    path.write_bytes(b"modified")
    assert "integrity_failure" in attachment_model_context(uploads, [record.as_dict()], authorized=True)
    path.unlink()
    secret = tmp_path / "secret"
    secret.write_text("outside private")
    path.symlink_to(secret)
    result = attachment_model_context(uploads, [record.as_dict()], authorized=True)
    assert "unreadable_text" in result
    assert "outside private" not in result


def test_formats_and_size_have_explicit_unreadable_status(uploads):
    for data, mime, expected in ((b"%PDF fixture", "application/pdf", "unsupported_format"),
                                 (b"x" * (MAX_TEXT_BYTES + 1), "text/plain", "budget_exceeded"),
                                 (b"\xff\xfe", "text/plain", "unreadable_text"),
                                 (b"a\x00b", "text/plain", "binary_content")):
        record = uploads.store(data=data, filename="file", content_type=mime)
        assert expected in attachment_model_context(uploads, [record.as_dict()], authorized=True)


def test_total_budget_and_count_limit(uploads):
    first = uploads.store(data=b"a" * MAX_TEXT_BYTES, filename="one.txt", content_type="text/plain")
    second = uploads.store(data=b"second", filename="two.txt", content_type="text/plain")
    result = attachment_model_context(uploads, [first.as_dict(), second.as_dict()], authorized=True)
    assert "text_budget_exceeded" in result
    assert '"text": "second"' not in result
    assert "attachment_count_limit" in attachment_model_context(uploads, [second.as_dict()] * 9, authorized=True)


def test_empty_refs_unchanged(uploads):
    assert attachment_model_context(uploads, [], authorized=True) == ""


@pytest.mark.asyncio
async def test_actual_chat_prelude_threads_authorized_text(uploads, monkeypatch):
    from api import server
    from types import SimpleNamespace
    record = uploads.store(data=b"violet maple 47", filename="note.txt", content_type="text/plain")
    memory = SimpleNamespace(working_push=lambda *args: None, working_get=lambda *args: [])
    monkeypatch.setattr(server.state, "uploads", uploads)
    monkeypatch.setattr(server.state, "memory", memory)
    monkeypatch.setattr(server.state, "orchestrator", SimpleNamespace(llm=None))
    text, context, _ = await server._prepare_chat_turn_context(
        session_id="fixture", text="read file", raw_context={"attachment_content_authorized": True},
        attachments=[record.as_dict()],
    )
    assert "violet maple 47" not in text
    assert "violet maple 47" in context["_attachment_model_data"]
    assert context["attachments"][0]["upload_id"] == record.upload_id
    text, context, _ = await server._prepare_chat_turn_context(
        session_id="fixture", text="read file", raw_context={}, attachments=[record.as_dict()],
    )
    assert "violet maple 47" not in text
    assert "not authorized" in context["_attachment_model_data"]


def test_model_content_keeps_original_command_and_vision_blocks():
    context = {"_attachment_model_data": "fixture document data"}
    assert append_attachment_model_data("original command", context) == "original commandfixture document data"
    blocks = [{"type": "text", "text": "original command"}, {"type": "image_url", "image_url": {"url": "fixture"}}]
    result = append_attachment_model_data(blocks, context)
    assert result[:2] == blocks
    assert blocks == [{"type": "text", "text": "original command"}, {"type": "image_url", "image_url": {"url": "fixture"}}]
    assert result[-1]["text"] == "fixture document data"
    assert append_attachment_model_data("original", {}) == "original"


def test_durable_context_excludes_expanded_document_but_preserves_metadata():
    context = {"_attachment_model_data": "third party private document", "attachments": [{"upload_id": "fixture"}], "future": {"keep": 1}}
    durable = durable_chat_context(context)
    assert "third party private document" not in json.dumps(durable)
    assert durable == {"attachments": [{"upload_id": "fixture"}], "future": {"keep": 1}}
    assert context["_attachment_model_data"] == "third party private document"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["_handle_command_body", "_handle_command_stream_body"])
async def test_actual_orchestrator_episode_excludes_document_prose(method):
    from agents.orchestrator import Orchestrator
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    agent = object.__new__(Orchestrator)
    agent._session_finalized = set()
    agent._session_surfaces = {}
    agent._maybe_handle_plan_meta_command = AsyncMock(return_value=False)
    agent._maybe_handle_pending_tool_approval_text = AsyncMock(return_value=False)
    agent.taskflows = None
    agent._somatic_engine = None
    agent._streaming_enabled = True
    agent.llm = SimpleNamespace(available=True)
    captured = []
    class EpisodeReached(Exception):
        pass
    def capture(**kwargs):
        captured.append(kwargs)
        raise EpisodeReached()
    agent._save_episode_async = capture
    with pytest.raises(EpisodeReached):
        await getattr(agent, method)("fixture", "read my file", {"_attachment_model_data": "private third party prose", "attachments": [{"upload_id": "fixture"}]}, {})
    assert captured[0]["summary"] == "read my file"
    assert "private third party prose" not in captured[0]["detail"]
    assert json.loads(captured[0]["detail"])["context"]["attachments"] == [{"upload_id": "fixture"}]
