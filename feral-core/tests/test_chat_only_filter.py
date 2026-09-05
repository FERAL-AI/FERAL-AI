"""The chat-only filter — the fix for the 132-models picker.

The v2026.5.0 bug: ``OpenAIProvider.refresh_models`` returned all 132
ids from ``/v1/models`` (including babbage-002, whisper-1, image models,
text-embedding-3-large, gpt-realtime-1.5, gpt-4o-mini-tts, etc.), the
v2 picker rendered them, the user picked one that wasn't chat, and
``/v1/chat/completions`` returned 400.

After , ``BaseProvider.list_models(model_class="chat")`` filters
through :mod:`providers.model_classes`; the raw list is still
available with the legacy no-arg call so feral-voice (audio), feral-
memory (embedding), etc. can still discover the right models. This
test suite pins the contract using the full 2026-04-26 OpenAI
``/v1/models`` fixture.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from providers.openai_provider import OpenAIProvider
from providers.deepseek_provider import DeepSeekProvider


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def openai_fixture() -> dict:
    return json.loads((FIXTURES / "openai_models.json").read_text())


async def _seed_openai_from_fixture(adapter: OpenAIProvider, fixture: dict) -> None:
    """Drive refresh_models with the fixture response."""

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "data": [e for e in fixture["data"]],
                "object": "list",
            }

    async def _get(url: str, **kwargs) -> _Resp:
        return _Resp()

    fake_client = AsyncMock()
    fake_client.get = _get
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=fake_client):
        await adapter.refresh_models()


@pytest.mark.asyncio
async def test_refresh_stores_full_raw_list(openai_fixture: dict) -> None:
    """``refresh_models`` keeps the raw 132-entry list — the chat-only
    filter runs in ``list_models``, not in ``refresh_models``. feral-voice
    and feral-memory depend on the unfiltered list being available."""
    adapter = OpenAIProvider(api_key="sk-test")
    await _seed_openai_from_fixture(adapter, openai_fixture)
    raw = adapter.list_models()
    assert "babbage-002" in raw
    assert "whisper-1" in raw
    # An image model, to prove the raw list keeps non-chat ids. This used
    # to be dall-e-3; OpenAI retired dall-e-2 and dall-e-3 from the models
    # endpoint some time before 2026-09-04, when a live refresh dropped
    # them from the fixture and this assertion started failing. The point
    # of the assertion is the class, not that one id, so it now names an
    # image model the account really serves.
    assert "chatgpt-image-latest" in raw
    assert "text-embedding-3-large" in raw
    assert "gpt-5.5" in raw


@pytest.mark.asyncio
async def test_list_models_chat_class_drops_non_chat(openai_fixture: dict) -> None:
    adapter = OpenAIProvider(api_key="sk-test")
    await _seed_openai_from_fixture(adapter, openai_fixture)
    chat_only = adapter.list_models(model_class="chat")
    # The four ids the maintainer's terminal proved 400'd on
    # /v1/chat/completions MUST be gone.
    for forbidden in ("babbage-002", "davinci-002",
                      "whisper-1", "gpt-4o-transcribe",
                      "gpt-4o-mini-transcribe", "gpt-4o-mini-tts",
                      # Dated-snapshot suffixes (operator report
                      # 2026-05-08): the catalog handed
                      # `gpt-4o-mini-transcribe-2025-12-15` out as the
                      # OpenAI chat default and every chat completion
                      # 404'd with `This is not a chat model`. The
                      # classifier regex is now anchored to also catch
                      # the dated tail — pinned here.
                      "gpt-4o-mini-transcribe-2025-03-20",
                      "gpt-4o-mini-transcribe-2025-12-15",
                      "gpt-4o-transcribe-diarize",
                      "gpt-4o-transcribe-diarize-2025-12-15",
                      "gpt-4o-mini-tts-2025-03-20",
                      "gpt-4o-mini-tts-2025-12-15",
                      "tts-1", "tts-1-hd",
                      # dall-e-* are gone from the account but stay
                      # listed here: if they ever come back they must
                      # still never be offered as a chat model.
                      "dall-e-2", "dall-e-3",
                      "gpt-image-2", "gpt-image-1.5", "chatgpt-image-latest",
                      # The 2026-07 transcription line without the gpt-4o
                      # prefix. Both classified as unknown until the
                      # 2026-09-04 refresh surfaced them on a live account.
                      "gpt-transcribe", "gpt-live-transcribe",
                      "text-embedding-3-small", "text-embedding-3-large",
                      "text-embedding-ada-002",
                      "gpt-realtime-1.5", "gpt-4o-realtime-preview",
                      "gpt-4o-realtime-preview-2024-12-17",
                      "gpt-3.5-turbo-instruct"):
        assert forbidden not in chat_only, (
            f"{forbidden} is non-chat and should NOT reach the chat "
            "class filter — sending it to /chat/completions is the "
            "exact 400 the shipped terminal log reported"
        )


@pytest.mark.asyncio
async def test_list_models_chat_class_includes_reasoning(openai_fixture: dict) -> None:
    adapter = OpenAIProvider(api_key="sk-test")
    await _seed_openai_from_fixture(adapter, openai_fixture)
    chat_only = adapter.list_models(model_class="chat")
    # Reasoning is a strict subset of chat — reasoning ids must appear
    # in the chat filter.
    for needed in ("gpt-5.5", "gpt-5.4-mini", "gpt-5", "o3", "o4-mini", "o1"):
        assert needed in chat_only, f"{needed} (reasoning) must be in chat-class result"


@pytest.mark.asyncio
async def test_list_models_chat_class_includes_legacy_chat(openai_fixture: dict) -> None:
    adapter = OpenAIProvider(api_key="sk-test")
    await _seed_openai_from_fixture(adapter, openai_fixture)
    chat_only = adapter.list_models(model_class="chat")
    for needed in ("gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-3.5-turbo", "gpt-4-turbo"):
        assert needed in chat_only


@pytest.mark.asyncio
async def test_list_models_reasoning_class_is_strict_subset(openai_fixture: dict) -> None:
    adapter = OpenAIProvider(api_key="sk-test")
    await _seed_openai_from_fixture(adapter, openai_fixture)
    reasoning_only = adapter.list_models(model_class="reasoning")
    assert "gpt-5.5" in reasoning_only
    assert "o3" in reasoning_only
    # Non-reasoning chat models must be EXCLUDED from reasoning filter.
    assert "gpt-4o" not in reasoning_only
    assert "gpt-4.1" not in reasoning_only


@pytest.mark.asyncio
async def test_list_models_embedding_class_is_exact(openai_fixture: dict) -> None:
    adapter = OpenAIProvider(api_key="sk-test")
    await _seed_openai_from_fixture(adapter, openai_fixture)
    embs = adapter.list_models(model_class="embedding")
    assert set(embs) == {
        "text-embedding-3-small",
        "text-embedding-3-large",
        "text-embedding-ada-002",
    }


@pytest.mark.asyncio
async def test_list_models_legacy_no_arg_is_unchanged(openai_fixture: dict) -> None:
    """The no-arg call must match the v2026.5.0 behaviour byte-for-byte."""
    adapter = OpenAIProvider(api_key="sk-test")
    await _seed_openai_from_fixture(adapter, openai_fixture)
    legacy = adapter.list_models()
    # The adapter sorts ids on refresh; both branches must use the same
    # ordering so downstream diff-sensitive consumers don't see churn.
    assert legacy == sorted(legacy)
    assert legacy == adapter.list_models(model_class=None)


def test_deepseek_chat_class_excludes_reasoner_when_asked_pure() -> None:
    """``classify("deepseek", "deepseek-reasoner") == "reasoning"``; the
    chat-class filter INCLUDES it (reasoning ⊂ chat); the reasoning-class
    filter narrows to reasoning only."""
    adapter = DeepSeekProvider(api_key="ds-test")
    # Bypass the network — set the raw list directly.
    adapter._models = [
        "deepseek-v4-pro", "deepseek-v4-flash",
        "deepseek-chat", "deepseek-reasoner",
    ]
    chat = adapter.list_models(model_class="chat")
    assert set(chat) == {
        "deepseek-v4-pro", "deepseek-v4-flash",
        "deepseek-chat", "deepseek-reasoner",
    }
    reasoning = adapter.list_models(model_class="reasoning")
    assert set(reasoning) == {"deepseek-v4-pro", "deepseek-reasoner"}


def test_unknown_id_defaults_to_chat_class_not_reasoning() -> None:
    """A freshly-released unknown id must reach the picker.

    Default-include-unknown is the behaviour the proposal argues for —
    silently dropping a novel id from the chat class the moment it
    ships would be worse UX than classifying it chat. The reasoning-
    only filter is strict (unknown excluded) so a wrong positive
    doesn't leak in.
    """
    adapter = OpenAIProvider(api_key="sk-test")
    adapter._models = ["gpt-6-hyperthinking-2027-01-01"]
    assert "gpt-6-hyperthinking-2027-01-01" in adapter.list_models(model_class="chat")
    assert adapter.list_models(model_class="reasoning") == []
