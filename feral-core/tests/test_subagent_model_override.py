"""model_override is honoured on the spawned child's first LLM call.

Part of the canonical lifecycle test set: allowlist / cron-note /
lifecycle / model / scope.
"""

from __future__ import annotations

import asyncio

import pytest

from agents import subagent_policy
from agents.subagent_spawner import (
    get_registry,
    register_llm_provider,
    register_runner,
    spawn_subsession,
)


pytestmark = pytest.mark.no_auto_feral_home


class _RecordingProvider:
    """Minimal LLM provider stand-in.

    The default runner calls ``provider.chat(messages=..., model=...)``
    once on spawn so we can observe ``model_override``. ``chat`` is
    async to mirror the real LLMProvider surface, but the test does
    not depend on a return value.
    """

    def __init__(self, *, model_name: str = "default-model"):
        self.model_name = model_name
        self.calls: list[dict] = []
        self._call_event = asyncio.Event()

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        self._call_event.set()
        return {"choices": []}

    async def wait_for_call(self, timeout: float = 1.0) -> None:
        await asyncio.wait_for(self._call_event.wait(), timeout=timeout)


@pytest.fixture(autouse=True)
def reset_state(tmp_path, monkeypatch):
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    subagent_policy.clear()
    get_registry().reset()
    register_runner(None)
    register_llm_provider(None)
    yield
    subagent_policy.clear()
    get_registry().reset()
    register_runner(None)
    register_llm_provider(None)


@pytest.mark.asyncio
async def test_model_override_threaded_into_first_chat_call():
    provider = _RecordingProvider(model_name="default-model")
    register_llm_provider(provider)

    await spawn_subsession(
        "parent-1",
        "tool_runner",
        scope_key="alpha",
        model_override="claude-haiku-4-5",
    )
    await provider.wait_for_call(timeout=1.0)
    await get_registry().cancel_all_children("parent-1")

    assert len(provider.calls) == 1
    assert provider.calls[0].get("model") == "claude-haiku-4-5"
    assert provider.calls[0].get("messages") == []


@pytest.mark.asyncio
async def test_default_model_used_when_override_missing():
    provider = _RecordingProvider(model_name="default-model")
    register_llm_provider(provider)

    await spawn_subsession("parent-1", "tool_runner", scope_key="alpha")
    await provider.wait_for_call(timeout=1.0)
    await get_registry().cancel_all_children("parent-1")

    assert provider.calls[0].get("model") == "default-model"


@pytest.mark.asyncio
async def test_model_override_persists_in_registry_record():
    provider = _RecordingProvider(model_name="default-model")
    register_llm_provider(provider)

    await spawn_subsession(
        "parent-1",
        "research",
        scope_key="alpha",
        model_override="opencode/claude",
    )
    children = get_registry().children_of("parent-1")
    assert len(children) == 1
    assert children[0]["model_override"] == "opencode/claude"
    await get_registry().cancel_all_children("parent-1")
