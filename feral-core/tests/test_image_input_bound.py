"""Published image upper bound and unknown-context refusal, without image fetch."""
from copy import deepcopy

import pytest

from cost.image_input import openai_image_input_bound


def payload(*, detail="auto", url="data:image/png;base64,synthetic"):
    return {"model": "gpt-5.4", "input": [{"role": "user", "content": [
        {"type": "input_text", "text": "synthetic"},
        {"type": "input_image", "image_url": url, "detail": detail}]}], "max_output_tokens": 500}


def bound(body, **kwargs):
    return openai_image_input_bound(body, **({"provider": "openai", "model": "gpt-5.4",
        "base_url": "https://api.openai.com/v1"} | kwargs))


@pytest.mark.parametrize("detail,tokens", [("auto", 3001), ("high", 3001), ("low", 7374), ("original", 12001)])
async def test_detail_bound_is_published_maximum_plus_rounding(detail, tokens):
    result = bound(payload(detail=detail))
    assert result.image_count == 1 and result.image_tokens == tokens
    assert result.prompt_tokens > tokens + 4096


def test_encoded_image_bytes_are_not_counted_as_text_or_decoded():
    small = payload()
    large = payload(url="data:image/png;base64," + "A" * 2000000)
    before = deepcopy(large)
    assert bound(small) == bound(large)
    assert large == before
    second = payload()
    second["input"][0]["content"].append(second["input"][0]["content"][-1])
    assert bound(second).image_tokens == 6002


@pytest.mark.parametrize("override", [
    {"provider": "anthropic"}, {"provider": "openrouter"}, {"model": "gpt-5.4-mini"},
    {"model": "gpt-5.4-2029-01-01"}, {"base_url": "https://regional.api.openai.com/v1"},
    {"base_url": "https://api.openai.com.attacker.invalid/v1"},
    {"base_url": "https://user:password@api.openai.com/v1"},
    {"base_url": "https://api.openai.com/v1?private=1"},
])
def test_other_provider_model_origin_remains_unknown(override):
    assert bound(payload(), **override) is None


@pytest.mark.parametrize("mutation", [
    {"previous_response_id": "resp-history"}, {"conversation": "stored-history"},
    {"tools": [{"type": "web_search"}]}, {"service_tier": "priority"}, {"service_tier": []},
])
def test_unbounded_history_tools_or_pricing_tier_remains_unknown(mutation):
    assert bound(payload() | mutation) is None


@pytest.mark.parametrize("part", [
    {"type": "item_reference", "id": "hidden-history"},
    {"type": "reasoning", "encrypted_content": "opaque-history"},
    {"type": "input_audio", "data": "inert"}, {"type": "input_file", "file_id": "file-inert"},
    {"type": "input_image", "file_id": "file-inert"},
    {"type": "input_image", "image_url": "inert", "detail": "future-detail"},
    {"type": "input_image", "image_url": "inert", "detail": []},
])
def test_mixed_or_unknown_media_remains_unpriceable(part):
    body = payload()
    body["input"][0]["content"].append(part)
    assert bound(body) is None


def test_prompt_image_bound_cannot_enter_long_context_surcharge_regime():
    body = payload()
    body["input"][0]["content"][0]["text"] = "x" * 280000
    assert bound(body) is None
    body = payload()
    body["input"][0]["content"] = [body["input"][0]["content"][-1]] * 65
    assert bound(body) is None
    assert bound(payload(), model="gpt-5.4-2026-03-05")
