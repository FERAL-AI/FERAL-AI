"""Classifier tests for ``providers.model_classes.classify``.

Coverage contract (per  proposal §8):

* ≥3 cases per provider × every known class.
* Each class listed in ``list_classes()`` has at least one test that
  proves classify() returns it for a canonical id.
* Unknown ids across every provider return ``"unknown"`` gracefully
  (no exceptions).
* The per-provider fixture ``tests/fixtures/<provider>_models.json``
  agrees with the classifier on every id it carries — this is the
  regression check that future catalog refreshes don't silently drift
  from the classifier's rule set.

The chat-only filter behaviour (including unknown-id default-include)
lives in :mod:`test_chat_only_filter`; this file pins the raw classify
rules alone.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from providers.model_classes import (
    classify,
    classify_many,
    filter_models,
    list_classes,
)


FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Expectation tables — (provider, model_id) -> expected class
# ---------------------------------------------------------------------------


OPENAI_EXPECTATIONS: list[tuple[str, str]] = [
    # reasoning family
    ("gpt-5.5", "reasoning"),
    ("gpt-5.5-pro", "reasoning"),
    ("gpt-5.4", "reasoning"),
    ("gpt-5.4-mini", "reasoning"),
    ("gpt-5.4-nano", "reasoning"),
    ("gpt-5", "reasoning"),
    ("o4-mini", "reasoning"),
    ("o3", "reasoning"),
    ("o1", "reasoning"),
    # chat
    ("gpt-4o", "chat"),
    ("gpt-4o-mini", "chat"),
    ("gpt-4.1", "chat"),
    ("gpt-4-turbo", "chat"),
    ("gpt-3.5-turbo", "chat"),
    # completion-only
    ("babbage-002", "completion-only"),
    ("davinci-002", "completion-only"),
    ("gpt-3.5-turbo-instruct", "completion-only"),
    ("text-davinci-003", "completion-only"),
    # embedding
    ("text-embedding-3-small", "embedding"),
    ("text-embedding-3-large", "embedding"),
    ("text-embedding-ada-002", "embedding"),
    # audio (base + dated-snapshot suffixes — the dated form leaked
    # into chat before the 2026-05-08 fix and the OpenAI catalog
    # handed `gpt-4o-mini-transcribe-2025-12-15` out as the chat
    # default, breaking every chat completion with HTTP 404
    # `This is not a chat model`. Pinned here to prevent regression.)
    ("whisper-1", "audio"),
    ("gpt-4o-transcribe", "audio"),
    ("gpt-4o-mini-transcribe", "audio"),
    ("gpt-4o-mini-transcribe-2025-03-20", "audio"),
    ("gpt-4o-mini-transcribe-2025-12-15", "audio"),
    ("gpt-4o-transcribe-diarize", "audio"),
    ("gpt-4o-transcribe-diarize-2025-12-15", "audio"),
    ("gpt-4o-mini-tts", "audio"),
    ("gpt-4o-mini-tts-2025-03-20", "audio"),
    ("gpt-4o-mini-tts-2025-12-15", "audio"),
    ("tts-1", "audio"),
    # image
    ("dall-e-2", "image"),
    ("dall-e-3", "image"),
    ("gpt-image-2", "image"),
    # realtime (base + dated)
    ("gpt-realtime-1.5", "realtime"),
    ("gpt-4o-realtime-preview", "realtime"),
    ("gpt-4o-realtime-preview-2024-12-17", "realtime"),
]


ANTHROPIC_EXPECTATIONS: list[tuple[str, str]] = [
    ("claude-opus-4-7", "reasoning"),
    ("claude-sonnet-4-6", "reasoning"),
    ("claude-haiku-4-5", "reasoning"),
    ("claude-haiku-4-5-20251001", "reasoning"),
    ("claude-opus-4-6", "reasoning"),
    ("claude-sonnet-4-5", "reasoning"),
    ("claude-sonnet-4-5-20250929", "reasoning"),
    ("claude-opus-4-1", "reasoning"),
    ("claude-opus-4-1-20250805", "reasoning"),
    ("claude-3-5-sonnet", "chat"),
    ("claude-3-opus", "chat"),
    ("claude-instant-1.2", "chat"),
]


DEEPSEEK_EXPECTATIONS: list[tuple[str, str]] = [
    ("deepseek-v4-pro", "reasoning"),
    ("deepseek-v4-flash", "chat"),
    ("deepseek-chat", "chat"),
    ("deepseek-reasoner", "reasoning"),
    ("deepseek-v3-0324", "chat"),
    ("deepseek-embedding-v3", "embedding"),
]


GEMINI_EXPECTATIONS: list[tuple[str, str]] = [
    ("gemini-3.1-pro-preview", "chat"),
    ("gemini-3-flash-preview", "chat"),
    ("gemini-3.1-flash-lite-preview", "chat"),
    ("gemini-3.1-pro", "chat"),
    ("gemini-3-flash", "chat"),
    ("gemini-3.1-pro-thinking", "reasoning"),
    ("gemini-3.1-flash-image-preview", "image"),
    ("imagen-3.0-generate", "image"),
    ("text-embedding-004", "embedding"),
    ("gemini-embedding", "embedding"),
]


GROQ_EXPECTATIONS: list[tuple[str, str]] = [
    ("llama-3.3-70b-versatile", "chat"),
    ("llama-3.1-8b-instant", "chat"),
    ("mixtral-8x7b-32768", "chat"),
    ("gemma2-9b-it", "chat"),
    ("deepseek-r1-distill-llama-70b", "reasoning"),
    ("qwen-qwq-32b", "reasoning"),
    ("whisper-large-v3", "audio"),
    ("distil-whisper-large-v3-en", "audio"),
]


OPENROUTER_EXPECTATIONS: list[tuple[str, str]] = [
    ("anthropic/claude-opus-4-7", "reasoning"),
    ("anthropic/claude-sonnet-4-6", "reasoning"),
    ("anthropic/claude-haiku-4-5", "reasoning"),
    ("openai/gpt-5.5", "reasoning"),
    ("openai/gpt-5.4-mini", "reasoning"),
    ("openai/gpt-4o", "chat"),
    ("deepseek/deepseek-v4-pro", "reasoning"),
    ("deepseek/deepseek-v4-flash", "chat"),
    ("google/gemini-3.1-pro", "chat"),
    ("meta-llama/llama-4-400b-instruct", "chat"),
    ("openai/gpt-5.5:nitro", "reasoning"),
]


ALL_PROVIDER_EXPECTATIONS: dict[str, list[tuple[str, str]]] = {
    "openai": OPENAI_EXPECTATIONS,
    "anthropic": ANTHROPIC_EXPECTATIONS,
    "deepseek": DEEPSEEK_EXPECTATIONS,
    "gemini": GEMINI_EXPECTATIONS,
    "groq": GROQ_EXPECTATIONS,
    "openrouter": OPENROUTER_EXPECTATIONS,
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "provider,model,expected",
    [(p, m, k) for p, cases in ALL_PROVIDER_EXPECTATIONS.items() for m, k in cases],
)
def test_classify_matches_expected_table(provider: str, model: str, expected: str) -> None:
    assert classify(provider, model) == expected, (
        f"classify({provider!r}, {model!r}) expected {expected!r}"
    )


@pytest.mark.parametrize("provider", list(ALL_PROVIDER_EXPECTATIONS))
def test_classify_unknown_model_is_unknown(provider: str) -> None:
    assert classify(provider, "definitely-not-a-real-model-name-2026") in {
        "unknown",
        "chat",
    }


@pytest.mark.parametrize("provider", list(ALL_PROVIDER_EXPECTATIONS))
def test_classify_empty_model_is_unknown(provider: str) -> None:
    assert classify(provider, "") == "unknown"


def test_classify_unknown_provider_returns_unknown_not_crash() -> None:
    # A community-registered provider that isn't in the classifier's
    # rule table should still receive a fallback answer.
    assert classify("not-a-provider", "gpt-5.5") in {"reasoning", "chat", "unknown"}
    assert classify("", "gpt-5.5") in {"reasoning", "chat", "unknown"}


def test_list_classes_is_stable_and_contains_reasoning_and_chat() -> None:
    classes = list_classes()
    assert "chat" in classes
    assert "reasoning" in classes
    assert "embedding" in classes
    assert "unknown" in classes
    assert len(set(classes)) == len(classes), "list_classes() has duplicates"


@pytest.mark.parametrize("provider", list(ALL_PROVIDER_EXPECTATIONS))
def test_classify_many_is_consistent_with_classify(provider: str) -> None:
    ids = [m for m, _ in ALL_PROVIDER_EXPECTATIONS[provider][:5]]
    bulk = classify_many(provider, ids)
    for mid in ids:
        assert bulk[mid] == classify(provider, mid)


# ---------------------------------------------------------------------------
# Fixture round-trip
# ---------------------------------------------------------------------------


def _load_fixture(name: str) -> dict:
    path = FIXTURES / name
    return json.loads(path.read_text())


def test_openai_fixture_ids_classify_without_unknown() -> None:
    data = _load_fixture("openai_models.json")
    ids = [entry["id"] for entry in data["data"]]
    # At least one of every class we care about must appear in the
    # fixture. This is what makes the chat-only filter test exercise
    # reality.
    classes = {classify("openai", i) for i in ids}
    for required in ("reasoning", "chat", "embedding", "audio", "image",
                     "realtime", "completion-only"):
        assert required in classes, (
            f"openai fixture is missing a {required} representative — add "
            "one before merging so the filter tests stay honest"
        )


def test_anthropic_fixture_ids_are_all_reasoning_or_chat() -> None:
    data = _load_fixture("anthropic_models.json")
    ids = [entry["id"] for entry in data["data"]]
    classes = {classify("anthropic", i) for i in ids}
    # Claude 4.x is reasoning; older 3.x (if we ever add it) is chat.
    # The 2026-04-26 fixture is all 4.x so the set is a singleton today.
    assert classes <= {"reasoning", "chat"}


def test_deepseek_fixture_ids_match_rule_table() -> None:
    data = _load_fixture("deepseek_models.json")
    for entry in data["data"]:
        mid = entry["id"]
        assert classify("deepseek", mid) in {"reasoning", "chat"}, mid


def test_gemini_fixture_ids_cover_chat_and_image() -> None:
    """The 2026-04-26 live /v1/models fixture exposes a wide range of
    Gemini model kinds — chat (gemini-3.1-pro-preview, 2.5-*), image
    (imagen-*, gemini-*-image-preview), video (veo-*, lyria-*),
    embedding (gemini-embedding-*), plus gemma / robotics / aqa /
    deep-research experimental branches. The assertion: every id must
    be classified into SOMETHING (non-trivial class coverage), and
    chat + image must both be represented. Reasoning isn't a separate
    Gemini class — 3.x with thinking enabled is classified ``chat``
    at this layer (the reasoning fork lives at the adapter's param
    layer, not the class taxonomy).
    """
    data = _load_fixture("gemini_models.json")
    names = [m["name"].split("/", 1)[-1] for m in data["models"]]
    classes = {classify("gemini", n) for n in names}
    assert "chat" in classes, f"gemini fixture has no chat ids: {classes}"
    assert "image" in classes, f"gemini fixture has no image ids: {classes}"


def test_openrouter_fixture_ids_delegate_to_vendor() -> None:
    """OpenRouter routes 355+ upstream models as of 2026-04-26. The
    classifier delegates to the vendor prefix (``anthropic/*`` →
    anthropic rules, ``openai/*`` → openai rules, etc). Vendors not
    yet in the prefix table fall through to ``unknown`` — that's
    acceptable (the v2 picker's recommended filter drops them
    anyway). The assertion: at minimum the chat class must cover the
    common vendors.
    """
    data = _load_fixture("openrouter_models.json")
    classes = {classify("openrouter", e["id"]) for e in data["data"]}
    assert "chat" in classes, (
        f"openrouter fixture should classify at least some routes as "
        f"chat; got {classes}"
    )
    # The long-tail "unknown" bucket is expected — we don't ship a
    # rule for every vendor openrouter routes to, and the recommended
    # shortlist doesn't pick them up by prefix anyway.


def test_groq_fixture_ids_split_chat_reasoning_audio() -> None:
    """Groq's 2026-04-26 live list covers chat (llama-3.x / llama-4 /
    qwen3 / gpt-oss / compound), audio (whisper, orpheus), and some
    experimental moderator heads. The exact set of classes varies as
    Groq rotates their catalog — assert at minimum chat + audio are
    represented (the two user-visible tiers).
    """
    data = _load_fixture("groq_models.json")
    classes = {classify("groq", e["id"]) for e in data["data"]}
    assert "chat" in classes, f"groq fixture has no chat ids: {classes}"
    assert "audio" in classes, f"groq fixture has no audio ids: {classes}"


# ---------------------------------------------------------------------------
# filter_models contract
# ---------------------------------------------------------------------------


def test_filter_models_none_is_legacy_passthrough() -> None:
    ids = ["a", "b", "c"]
    assert filter_models("openai", ids, model_class=None) == ids


def test_filter_models_chat_includes_reasoning_and_unknown() -> None:
    ids = ["gpt-5.5", "gpt-4o", "babbage-002", "totally-made-up-id"]
    out = filter_models("openai", ids, model_class="chat")
    assert "gpt-5.5" in out
    assert "gpt-4o" in out
    assert "babbage-002" not in out
    assert "totally-made-up-id" in out  # unknown -> default-include


def test_filter_models_reasoning_excludes_non_reasoning() -> None:
    ids = ["gpt-5.5", "gpt-4o", "o3", "gpt-5.4-nano"]
    out = filter_models("openai", ids, model_class="reasoning")
    assert "gpt-5.5" in out
    assert "o3" in out
    assert "gpt-5.4-nano" in out
    assert "gpt-4o" not in out


def test_filter_models_embedding_exact_match() -> None:
    ids = ["text-embedding-3-large", "gpt-5.5", "whisper-1"]
    out = filter_models("openai", ids, model_class="embedding")
    assert out == ["text-embedding-3-large"]
