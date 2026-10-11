"""Exact attachment media admission stays read-only and fails closed."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from agents.runtime_context_checkpoint import ContextReadinessState, RuntimeContextError
from api.runtime_context import established_legacy_media_readiness
from memory.store import MemoryStore
from tests.test_runtime_context_activation import isolated_coordinator


@pytest_asyncio.fixture
async def legacy_media(tmp_path):
    store = MemoryStore(db_path=str(tmp_path / "media.db"))
    coordinator, locks, history, _ = isolated_coordinator(store)
    attachment = await coordinator.attach("legacy")
    state = SimpleNamespace(memory=store, orchestrator=SimpleNamespace(_context_checkpoints=coordinator))
    try:
        yield store, coordinator, attachment, state, locks, history
    finally:
        await coordinator.detach(attachment.token)
        await store.aclose()


@pytest.mark.asyncio
async def test_media_readiness_does_not_wait_or_write_history(legacy_media):
    store, coordinator, attachment, state, locks, history = legacy_media
    before = await store.runtime_checkpoint_read("legacy")
    async with locks["legacy"]:
        ready = await asyncio.wait_for(established_legacy_media_readiness(state, coordinator, attachment), 0.2)
    assert ready.state == ContextReadinessState.LEGACY and not ready.managed
    assert await store.runtime_checkpoint_read("legacy") == before
    assert history == {} and coordinator._writers == {}


@pytest.mark.asyncio
async def test_detached_token_cannot_admit_media(legacy_media, monkeypatch):
    store, coordinator, attachment, state, locks, history = legacy_media
    await coordinator.detach(attachment.token)
    read = AsyncMock()
    monkeypatch.setattr(store, "runtime_checkpoint_read", read)
    with pytest.raises(RuntimeContextError) as refusal:
        await coordinator.established_legacy_media_readiness(attachment.token)
    assert refusal.value.code == "context_attachment_invalid"
    read.assert_not_awaited()


@pytest.mark.asyncio
async def test_initial_refused_attachment_never_downgrades(legacy_media, monkeypatch):
    store, coordinator, attachment, state, locks, history = legacy_media
    await store.conversation_save("refused", [{"role": "user", "content": "UI-only fixture"}])
    refused = await coordinator.attach("refused", checkpoint_version=1)
    read = AsyncMock()
    monkeypatch.setattr(store, "runtime_checkpoint_read", read)
    try:
        ready = await coordinator.established_legacy_media_readiness(refused.token)
        assert ready.state == ContextReadinessState.LEGACY_UNAVAILABLE and ready.managed
        read.assert_not_awaited()
    finally:
        monkeypatch.undo()
        await coordinator.detach(refused.token)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["detach", "managed", "store"])
async def test_media_read_rechecks_exact_lifetime_after_await(legacy_media, monkeypatch, change, tmp_path):
    store, coordinator, attachment, state, locks, history = legacy_media
    original = store.runtime_checkpoint_read
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0
    async def delayed(sid):
        nonlocal calls
        calls += 1
        value = await original(sid)
        if calls == 1:
            entered.set()
            await release.wait()
        return value
    monkeypatch.setattr(store, "runtime_checkpoint_read", delayed)
    task = asyncio.create_task(coordinator.established_legacy_media_readiness(attachment.token))
    await entered.wait()
    managed = None
    replacement = None
    try:
        if change == "detach":
            await coordinator.detach(attachment.token)
        elif change == "managed":
            managed = await coordinator.attach("legacy", checkpoint_version=1)
            assert managed.readiness.ready
        else:
            replacement = MemoryStore(db_path=str(tmp_path / "replacement.db"))
            coordinator.store = replacement
        release.set()
        if change == "detach":
            with pytest.raises(RuntimeContextError):
                await task
        else:
            ready = await task
            assert ready.managed and ready.state != ContextReadinessState.LEGACY
    finally:
        release.set()
        coordinator.store = store
        await asyncio.gather(task, return_exceptions=True)
        if managed is not None:
            await coordinator.detach(managed.token)
        if replacement is not None:
            await replacement.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["error", "timeout"])
async def test_media_read_failure_never_asserts_legacy(legacy_media, monkeypatch, failure):
    store, coordinator, attachment, state, locks, history = legacy_media
    async def unavailable(sid):
        if failure == "error":
            raise OSError("synthetic refusal")
        await asyncio.Future()
    monkeypatch.setattr(store, "runtime_checkpoint_read", unavailable)
    monkeypatch.setattr("agents.runtime_context_checkpoint.LEGACY_MEDIA_READ_TIMEOUT_SECONDS", 0.01)
    try:
        ready = await coordinator.established_legacy_media_readiness(attachment.token)
        assert ready.managed and ready.state == ContextReadinessState.UNAVAILABLE
    finally:
        monkeypatch.undo()
