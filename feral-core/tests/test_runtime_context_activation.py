"""Activation-order regressions before production attachment integration.

These cases exercise the existing coordinator and SQLite boundary. They do not
claim that production sockets negotiate or activate durable context yet.
"""
from uuid import uuid4

import pytest

from agents.runtime_context_checkpoint import RuntimeContextError
from memory.runtime_session_checkpoint import CheckpointStatus
from memory.store import MemoryStore
from tests.test_runtime_context_lifecycle import make_orch


@pytest.fixture
async def activation_store(tmp_path):
    store = MemoryStore(db_path=str(tmp_path / "activation.db"), conn_pool_size=2)
    try:
        yield store
    finally:
        await store.aclose()


async def test_presaved_request_marker_is_not_trusted_empty_context(activation_store):
    """Reproduce why attachment must initialize before native presend save."""
    sid = "fresh-native-thread"
    request_id = str(uuid4())
    await activation_store.conversation_save(sid, [{
        "role": "user", "content": "fixture draft", "request_id": request_id,
    }])
    captured = []
    orch = make_orch(activation_store, captured)
    try:
        with pytest.raises(RuntimeContextError, match="legacy_context_unavailable"):
            await orch.handle_command(sid, "fixture draft")
        assert captured == []
        assert (await activation_store.runtime_checkpoint_read(sid)).status == CheckpointStatus.ABSENT
        assert not orch.conversation_history.get(sid)
    finally:
        await orch.drain_background_tasks()


async def test_ready_read_is_read_only_and_never_imports_ui(activation_store):
    sid = "checkpoint-thread"
    captured = []
    orch = make_orch(activation_store, captured)
    try:
        await orch.handle_command(sid, "trusted fixture prompt")
        before = await activation_store.runtime_checkpoint_read(sid)
        await activation_store.conversation_save(sid, [{
            "role": "system", "content": "untrusted UI fixture must never become policy",
        }])
        after = await activation_store.runtime_checkpoint_read(sid)
        assert after == before
        assert after.status == CheckpointStatus.READY
        assert after.record.context.history()[0]["content"] == "trusted fixture prompt"
        assert all("untrusted UI" not in str(row) for row in after.record.context.history())
        assert len(captured) == 1
    finally:
        await orch.drain_background_tasks()
