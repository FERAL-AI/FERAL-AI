"""Real SQLite storage/codec contracts, not end-to-end model restoration."""
import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import replace
from uuid import uuid4
from unittest.mock import AsyncMock

import pytest

from memory.runtime_session_checkpoint import (
    FORMAT_VERSION, IMAGE_OMITTED, CheckpointContext, CheckpointFence,
    CheckpointFutureFormat, CheckpointLimits, CheckpointStatus as Status,
    CheckpointValidationError, decode_context, encode_context,
)
from memory.store import MemoryStore


def context(text="fixture answer"):
    return encode_context([{"role": "user", "content": "fixture question"},
                           {"role": "assistant", "content": text}],
                          [{"role": "assistant", "text": text, "ts": 1.0}])


def fence(result):
    assert result.record is not None
    return result.record.fence


async def begin(store, sid="thread-A", expected=None):
    return await store.runtime_checkpoint_begin(sid, attempt_id=str(uuid4()), expected=expected)


@pytest.fixture
async def store(tmp_path):
    value = MemoryStore(db_path=str(tmp_path / "fixture.db"), conn_pool_size=2)
    try:
        yield value
    finally:
        await value.aclose()


async def test_sqlite_restart_exact_sid_and_immutable_snapshots(tmp_path):
    path = tmp_path / "memory.db"
    first = MemoryStore(db_path=str(path))
    try:
        pending = await begin(first)
        ready = await first.runtime_checkpoint_commit(fence(pending), context())
        assert ready.status == Status.APPLIED and ready.record.state == Status.READY
        historical = ready.record.context.history()
        historical[0]["content"] = "mutated caller copy"
    finally:
        await first.aclose()
    second = MemoryStore(db_path=str(path))
    try:
        read = await second.runtime_checkpoint_read("thread-A")
        assert read.status == Status.READY and read.record.context == context()
        assert fence(read) == fence(ready)
        assert (await second.runtime_checkpoint_read("thread-a")).status == Status.ABSENT
        with pytest.raises(CheckpointValidationError):
            await second.runtime_checkpoint_read(" thread-A")
        assert not (tmp_path / "primary_session_thread.json").exists()
    finally:
        await second.aclose()


async def test_checkpoint_inherits_existing_whole_database_encryption(tmp_path):
    from memory.at_rest import encrypt_memory_db

    class FixtureVault:
        def _master_key(self):
            return b"isolated-fixture-master-key-only"

    vault = FixtureVault()  # In-memory fixture only; no OS vault/keychain.
    path = tmp_path / "memory.db"
    store = MemoryStore(db_path=str(path))
    payload = context("isolated private context encryption canary")
    try:
        await store.runtime_checkpoint_commit(fence(await begin(store)), payload)
    finally:
        await store.aclose()
    encrypt_memory_db(vault=vault, db_path=path, shred_plaintext=True)
    ciphertext = path.with_name("memory.db.enc")
    assert not path.exists() and ciphertext.exists()
    assert b"isolated private context encryption canary" not in ciphertext.read_bytes()
    reopened = MemoryStore(db_path=str(path), authenticated_vault=vault)
    try:
        assert (await reopened.runtime_checkpoint_read("thread-A")).record.context == payload
        assert not (tmp_path / "primary_session_thread.json").exists()
    finally:
        await reopened.aclose()


async def test_generation_stable_attempt_revision_fence_stale_commit_delete(store):
    initial = await begin(store)
    ready = await store.runtime_checkpoint_commit(fence(initial), context("first"))
    next_writer = await begin(store, expected=fence(ready))
    assert fence(next_writer).generation == fence(initial).generation
    assert fence(next_writer).revision == fence(ready).revision + 1
    assert fence(next_writer).attempt_id != fence(ready).attempt_id
    assert (await store.runtime_checkpoint_commit(fence(initial), context("stale"))).status == Status.CONFLICT
    assert (await store.runtime_checkpoint_delete(fence(ready))).status == Status.CONFLICT
    assert (await store.runtime_checkpoint_read("thread-A")).record.context is None
    updated = await store.runtime_checkpoint_commit(fence(next_writer), context("second"))
    assert updated.status == Status.APPLIED
    assert (await store.runtime_checkpoint_commit(fence(next_writer), context("double"))).status == Status.CONFLICT
    assert (await begin(store, expected=fence(ready))).status == Status.CONFLICT
    assert (await store.runtime_checkpoint_read("thread-A")).record.context == context("second")


async def test_delete_pending_fences_late_writer_and_never_resurrects(store):
    pending = await begin(store)
    deleted = await store.runtime_checkpoint_delete(fence(pending))
    assert deleted.status == Status.APPLIED and deleted.record.state == Status.DELETED
    assert (await store.runtime_checkpoint_commit(fence(pending), context())).status == Status.DELETED
    assert (await begin(store)).status == Status.DELETED
    assert (await begin(store, expected=fence(deleted))).status == Status.DELETED
    async with sqlite_read(store) as conn:
        row = await conn.execute_fetchall("SELECT payload_json,payload_bytes FROM runtime_session_checkpoints")
        assert tuple(row[0]) == ("", 0)
    assert (await begin(store, "thread-B")).status == Status.APPLIED


@asynccontextmanager
async def sqlite_read(store):
    conn = await store._conn()
    try:
        yield conn
    finally:
        await store._release(conn)


async def test_restart_pending_not_ready_and_no_auto_resume(tmp_path):
    path = str(tmp_path / "pending.db")
    first = MemoryStore(db_path=path)
    try:
        ready = await first.runtime_checkpoint_commit(fence(await begin(first)), context("old safe"))
        pending = await begin(first, expected=fence(ready))
    finally:
        await first.aclose()
    second = MemoryStore(db_path=path)
    try:
        read = await second.runtime_checkpoint_read("thread-A")
        assert read.status == Status.IN_PROGRESS and read.record.context is None
        assert fence(read) == fence(pending)
        assert (await begin(second, expected=fence(read))).status == Status.IN_PROGRESS
    finally:
        await second.aclose()


async def test_wrong_generation_attempt_session_and_missing_fences(store):
    original = fence(await begin(store))
    wrong = [replace(original, generation=str(uuid4())), replace(original, attempt_id=str(uuid4())),
             replace(original, revision=original.revision + 1)]
    for bad in wrong:
        assert (await store.runtime_checkpoint_commit(bad, context())).status == Status.CONFLICT
        assert (await store.runtime_checkpoint_delete(bad)).status == Status.CONFLICT
    absent = replace(original, session_id="missing")
    assert (await store.runtime_checkpoint_commit(absent, context())).status == Status.ABSENT
    assert (await store.runtime_checkpoint_delete(absent)).status == Status.ABSENT
    assert (await begin(store, "thread-B", expected=original)).status == Status.CONFLICT


async def test_two_stores_racing_begin_same_sid_and_stale_cas(tmp_path):
    path = str(tmp_path / "concurrent.db")
    first, second = MemoryStore(db_path=path), MemoryStore(db_path=path)
    try:
        results = await asyncio.gather(begin(first), begin(second))
        assert sorted(result.status.value for result in results) == ["applied", "in_progress"]
        owner = next(result for result in results if result.status == Status.APPLIED)
        ready = await first.runtime_checkpoint_commit(fence(owner), context())
        writes = await asyncio.gather(begin(first, expected=fence(ready)), begin(second, expected=fence(ready)))
        assert sum(item.status == Status.APPLIED for item in writes) == 1
        assert any(item.status == Status.IN_PROGRESS for item in writes)
    finally:
        await first.aclose()
        await second.aclose()


async def test_concurrent_session_row_quota_counts_tombstones(tmp_path):
    path = str(tmp_path / "quota.db")
    limits = CheckpointLimits(sessions=1)
    first = MemoryStore(db_path=path, runtime_checkpoint_limits=limits)
    second = MemoryStore(db_path=path, runtime_checkpoint_limits=limits)
    try:
        results = await asyncio.gather(begin(first, "A"), begin(second, "B"))
        assert sorted(result.status.value for result in results) == ["applied", "quota"]
        owner = next(result for result in results if result.status == Status.APPLIED)
        assert (await first.runtime_checkpoint_delete(fence(owner))).status == Status.APPLIED
        assert (await begin(second, "C")).status == Status.QUOTA
    finally:
        await first.aclose()
        await second.aclose()


async def test_concurrent_total_byte_quota_atomic_and_releases_deleted_payload(tmp_path):
    payload = context()
    limits = CheckpointLimits(total_bytes=payload.byte_count)
    path = str(tmp_path / "bytes.db")
    first = MemoryStore(db_path=path, runtime_checkpoint_limits=limits)
    second = MemoryStore(db_path=path, runtime_checkpoint_limits=limits)
    try:
        a, b = await begin(first, "A"), await begin(second, "B")
        results = await asyncio.gather(first.runtime_checkpoint_commit(fence(a), payload),
                                       second.runtime_checkpoint_commit(fence(b), payload))
        assert sorted(result.status.value for result in results) == ["applied", "quota"]
        winner = next(result for result in results if result.status == Status.APPLIED)
        loser = b if fence(winner).session_id == "A" else a
        assert (await first.runtime_checkpoint_read(fence(loser).session_id)).status == Status.IN_PROGRESS
        await first.runtime_checkpoint_delete(fence(winner))
        assert (await second.runtime_checkpoint_commit(fence(loser), payload)).status == Status.APPLIED
    finally:
        await first.aclose()
        await second.aclose()


async def test_byte_quota_invalid_future_context_leave_fence_pending(store):
    pending = await begin(store)
    oversized = CheckpointContext("x" * (512 * 1024 + 1))
    assert (await store.runtime_checkpoint_commit(fence(pending), oversized)).status == Status.QUOTA
    assert (await store.runtime_checkpoint_commit(fence(pending), CheckpointContext("{broken"))).status == Status.INVALID
    assert (await store.runtime_checkpoint_commit(fence(pending), CheckpointContext('{"format_version":999}'))).status == Status.UNSUPPORTED
    assert (await store.runtime_checkpoint_read("thread-A")).record == pending.record
    assert (await store.runtime_checkpoint_commit(fence(pending), context())).status == Status.APPLIED


async def test_rejected_save_retains_prior_private_bytes_with_pending_fence(tmp_path):
    old = context("old")
    store = MemoryStore(db_path=str(tmp_path / "protected.db"), runtime_checkpoint_limits=CheckpointLimits(total_bytes=old.byte_count))
    try:
        ready = await store.runtime_checkpoint_commit(fence(await begin(store)), old)
        pending = await begin(store, expected=fence(ready))
        result = await store.runtime_checkpoint_commit(fence(pending), context("larger than old"))
        assert result.status == Status.QUOTA and result.record.context is None
        read = await store.runtime_checkpoint_read("thread-A")
        assert read.status == Status.IN_PROGRESS and fence(read) == fence(pending)
        with sqlite3.connect(store.db_path) as conn:
            assert conn.execute("SELECT payload_json FROM runtime_session_checkpoints").fetchone()[0] == old.encoded
    finally:
        await store.aclose()


async def test_sqlite_write_failure_rolls_back_and_releases_connection(store):
    pending = await begin(store)
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("""CREATE TRIGGER fixture_checkpoint_failure BEFORE UPDATE ON runtime_session_checkpoints
                      BEGIN SELECT RAISE(ABORT, 'fixture failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="fixture failure"):
        await store.runtime_checkpoint_commit(fence(pending), context())
    assert (await store.runtime_checkpoint_read("thread-A")).record == pending.record
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("DROP TRIGGER fixture_checkpoint_failure")
    assert (await store.runtime_checkpoint_commit(fence(pending), context())).status == Status.APPLIED


async def test_cancel_before_commit_rolls_back_real_sqlite_fence(tmp_path, monkeypatch):
    store = MemoryStore(db_path=str(tmp_path / "cancel.db"), conn_pool_size=1)
    try:
        pending = await begin(store)
        conn = await store._conn()
        original_commit = conn.commit
        entered = asyncio.Event()

        async def held_commit():
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(conn, "commit", held_commit)
        await store._release(conn)
        task = asyncio.create_task(store.runtime_checkpoint_commit(fence(pending), context()))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        monkeypatch.setattr(conn, "commit", original_commit)
        assert (await store.runtime_checkpoint_read("thread-A")).record == pending.record
        assert (await store.runtime_checkpoint_commit(fence(pending), context())).status == Status.APPLIED
    finally:
        await store.aclose()


@pytest.mark.parametrize("column,value,status", [
    ("payload_json", "{broken", Status.CORRUPT),
    ("payload_json", '{"format_version":999}', Status.UNSUPPORTED),
    ("format_version", 999, Status.UNSUPPORTED),
    ("revision", 0, Status.CORRUPT),
    ("generation", "not-uuid", Status.CORRUPT),
    ("payload_bytes", -1, Status.CORRUPT),
    ("payload_json", '{"format_version":1,"history":[],"working":[],"grants":[]}', Status.CORRUPT),
])
async def test_corrupt_future_rows_cannot_be_silently_replaced(store, column, value, status):
    pending = await begin(store)
    ready = await store.runtime_checkpoint_commit(fence(pending), context())
    # Fault injection only in this disposable database. Column is fixture-owned.
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(f"UPDATE runtime_session_checkpoints SET {column}=? WHERE session_id=?", (value, "thread-A"))
        if column == "payload_json":
            conn.execute("UPDATE runtime_session_checkpoints SET payload_bytes=? WHERE session_id=?", (len(value.encode("utf-8")), "thread-A"))
    assert (await store.runtime_checkpoint_read("thread-A")).status == status
    assert (await begin(store, expected=fence(ready))).status == status
    assert (await store.runtime_checkpoint_delete(fence(ready))).status == status
    assert (await store.runtime_checkpoint_commit(fence(pending), context())).status == status


@pytest.mark.parametrize("sid", ["", "A\nB", "A\x00B", "A\x7fB", "A" * 1025,
                               " A", "A ", "\tA", "A\n", " ", "\u00a0A", "A\u00a0", "A\u0085B", "A\u009fB"])
async def test_invalid_id_redacted_no_row(store, sid):
    with pytest.raises(CheckpointValidationError, match="identity"):
        await begin(store, sid)
    with pytest.raises(CheckpointValidationError, match="identity"):
        await store.runtime_checkpoint_read(sid)


async def test_internal_whitespace_is_valid_exact_session_identity(store):
    pending = await begin(store, "thread A")
    assert pending.status == Status.APPLIED
    assert (await store.runtime_checkpoint_read("thread A")).status == Status.IN_PROGRESS
    assert (await store.runtime_checkpoint_read("threadA")).status == Status.ABSENT


def tool_round():
    return [
        {"role": "assistant", "tool_calls": [
            {"id": "call-A", "type": "function", "function": {"name": "read", "arguments": "{}"}},
            {"id": "call-B", "type": "function", "function": {"name": "read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call-B", "name": "read", "content": "second"},
        {"role": "tool", "tool_call_id": "call-A", "name": "read", "content": "first"},
    ]


def test_codec_complete_tool_group_and_row_cap_does_not_split():
    history = [{"role": "user", "content": "question"}, *tool_round(), {"role": "assistant", "content": "answer"}]
    restored = encode_context(history, [], limits=CheckpointLimits(history_rows=4))
    assert len(restored.history()) == 4 and len(restored.history()[0]["tool_calls"]) == 2
    tiny = encode_context(history, [], limits=CheckpointLimits(history_rows=2))
    assert tiny.history() == [{"role": "assistant", "content": "answer"}]
    assert tiny.omissions()["history_rows"] == 4
    assert decode_context(restored.encoded) == restored


@pytest.mark.parametrize("rows", [tool_round()[:2], tool_round()[1:],
                                  [*tool_round()[:2], tool_round()[1]],
                                  [tool_round()[0], {"role": "tool", "tool_call_id": "unknown", "content": "bad"}]])
def test_codec_incomplete_orphan_duplicate_groups_omitted_explicitly(rows):
    result = encode_context(rows, [])
    assert result.history() == []
    assert result.omissions()["history_rows"] == len(rows)


def test_decode_refuses_partial_tool_group_rather_than_repairs():
    raw = json.loads(context().encoded)
    raw["history"] = tool_round()[:2]
    with pytest.raises(CheckpointValidationError, match="groups"):
        decode_context(json.dumps(raw))


async def test_actual_orchestrator_provider_null_is_stored_as_omitted_content():
    from agents.llm_provider import LLMProvider
    from tests.test_stream_nonstream_parity import _make_default_gate_orchestrator

    orch = _make_default_gate_orchestrator()
    orch._multi_agent_enabled = False
    orch.memory = None
    tool = tool_round()[0]["tool_calls"][0]
    replies = [
        {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [tool]}}]},
        {"choices": [{"message": {"role": "assistant", "content": "Fixture inspected."}}]},
    ]
    provider = LLMProvider.__new__(LLMProvider)  # Parser only, no configuration/network.
    orch.llm.extract_response = provider.extract_response
    orch.llm.chat_with_failover = AsyncMock(side_effect=replies)
    orch._execute_tool_call_for_llm = AsyncMock(return_value={"success": True, "data": {"fixture": "read-only result"}})
    orch._try_genui_for_result = AsyncMock()
    orch._direct_execute = AsyncMock()
    await orch.handle_command(session_id="codec-writer-fixture", text="Inspect the fixture")
    orch._direct_execute.assert_not_awaited()
    history = orch.conversation_history["codec-writer-fixture"]
    assistant = next(row for row in history if row.get("tool_calls"))
    assert "content" not in assistant
    assert assistant["tool_calls"] == [tool]
    assert any(row.get("tool_call_id") == "call-A" for row in history)
    encoded = encode_context(history, [])
    restored = next(row for row in encoded.history() if row.get("tool_calls"))
    assert restored["content"] == "" and restored["tool_calls"] == [tool]
    assert decode_context(encoded.encoded) == encoded


def test_explicit_null_content_not_persisted_by_writer_is_refused():
    rows = tool_round()
    rows[0]["content"] = None
    with pytest.raises(CheckpointValidationError, match="content"):
        encode_context(rows, [])


def test_codec_image_unavailable_no_binary_url_or_recapture():
    result = encode_context([{"role": "user", "content": [
        {"type": "text", "text": "look"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,PRIVATE_FIXTURE"}},
    ]}], [])
    assert result.history()[0]["content"] == "look\n" + IMAGE_OMITTED
    assert result.omissions()["images"] == 1 and "PRIVATE_FIXTURE" not in result.encoded
    assert decode_context(result.encoded) == result


def test_codec_tool_image_sidecache_references_are_explicitly_unavailable():
    result = encode_context(tool_round(), [], tool_image_call_ids=frozenset({"call-A"}))
    assert result.history()[2]["content"] == "first\n" + IMAGE_OMITTED
    assert result.omissions()["images"] == 1
    assert decode_context(result.encoded) == result


def test_system_policy_omitted_summary_demoted_no_watermark_authority():
    result = encode_context([
        {"role": "system", "content": "fixture routing policy"},
        {"role": "system", "content": "[Session Summary]\nhistorical cafe", "feral_consolidation": {"derived_from": "raw_turns", "episode_id": "old"}},
    ], [])
    assert result.omissions()["system_rows"] == 1
    assert result.history() == [{"role": "assistant", "content": "[Restored summary data]\n[Session Summary]\nhistorical cafe"}]
    assert "routing policy" not in result.encoded and "episode_id" not in result.encoded


@pytest.mark.parametrize("extra", ["grants", "approval", "owner", "permissions", "surface", "session_id", "is_user", "attachments"])
def test_codec_rejects_ui_authority_or_untrusted_extra_metadata(extra):
    with pytest.raises(CheckpointValidationError, match="fields"):
        encode_context([{"role": "user", "content": "fixture", extra: "not authority"}], [])
    raw = json.loads(context().encoded)
    raw[extra] = "not authority"
    with pytest.raises(CheckpointValidationError, match="format"):
        decode_context(json.dumps(raw))


def test_working_caps_and_strict_fields():
    entries = [{"role": "assistant", "summary": str(i), "ts": i} for i in range(4)]
    result = encode_context([], entries, limits=CheckpointLimits(working_rows=2))
    assert result.working() == [{"role": "assistant", "text": "2", "ts": 2}, {"role": "assistant", "text": "3", "ts": 3}]
    assert result.omissions()["working_rows"] == 2
    with pytest.raises(CheckpointValidationError, match="working fields"):
        encode_context([], [{"role": "assistant", "text": "fixture", "grants": []}])


def test_existing_server_greeting_and_voice_working_shapes_normalized():
    result = encode_context([], [
        {"role": "assistant", "content": "fixture greeting", "ts": 1.0},
        {"role": "user", "text": "fixture voice", "source": "voice", "ts": 2.0},
    ])
    assert result.working() == [{"role": "assistant", "text": "fixture greeting", "ts": 1.0},
                                {"role": "user", "text": "fixture voice", "ts": 2.0}]
    assert "source" not in result.encoded


@pytest.mark.parametrize("raw", ['{"format_version":1,"format_version":1}', '[]', 'null', '{broken'])
def test_decode_duplicate_malformed_not_silent_empty(raw):
    with pytest.raises(CheckpointValidationError):
        decode_context(raw)


def test_future_format_quota_and_limit_validation():
    with pytest.raises(CheckpointFutureFormat):
        decode_context(json.dumps({"format_version": FORMAT_VERSION + 1}))
    with pytest.raises(CheckpointValidationError, match="byte quota"):
        encode_context([{"role": "user", "content": "😀" * 50}], [], limits=CheckpointLimits(record_bytes=200))
    with pytest.raises(CheckpointValidationError):
        CheckpointLimits(sessions=1001)
    with pytest.raises(CheckpointValidationError):
        CheckpointLimits(history_rows=True)
    with pytest.raises(CheckpointValidationError):
        CheckpointFence("A", str(uuid4()), 0, str(uuid4()))


async def test_conversations_snapshots_receipts_not_imported_or_mutated(store):
    await store.conversation_save("thread-A", [{"role": "assistant", "content": "UI-only row"}])
    assert (await store.runtime_checkpoint_read("thread-A")).status == Status.ABSENT
    ready = await store.runtime_checkpoint_commit(fence(await begin(store)), context())
    assert ready.record.context == context()
    conversation = await store.conversation_get("thread-A")
    assert conversation["messages"][0]["content"] == "UI-only row"
    assert await store.chat_turn_get(session_id="thread-A", request_id=str(uuid4())) is None
    from memory.sync import SyncEngine
    assert "runtime_session_checkpoints" not in SyncEngine._SYNC_ALLOWED_TABLES
