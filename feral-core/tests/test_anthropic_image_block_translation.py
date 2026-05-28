"""v2026.5.44 — Anthropic multimodal block translation regression.

Live verification of the v2026.5.44 release candidate caught every
vision-bearing chat turn 400-ing against Anthropic:

    HTTP 400 — invalid_request_error: messages.0.content.1: Input tag
    'image_url' found using 'type' does not match any of the expected
    tags: 'image', 'text', 'tool_use', 'tool_result', ...

The orchestrator (and ``perception.fusion.to_llm_user_content``)
emits OpenAI Chat-Completions shape

    {"type": "image_url",
     "image_url": {"url": "data:image/png;base64,...", "detail": "low"}}

while Anthropic's Messages API expects

    {"type": "image",
     "source": {"type": "base64",
                "media_type": "image/png",
                "data": "..."}}

These tests pin the translator
(``agents.multimodal_blocks.to_anthropic_blocks``) and prove that the
shape conversion lands in the Anthropic request body that
``_chat_anthropic`` / ``_chat_stream_anthropic`` /
``_build_anthropic_body`` send on the wire.
"""

from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from agents.multimodal_blocks import (
    to_anthropic_blocks,
    to_gemini_parts,
)
from agents.llm_anthropic_shape import _convert_messages_for_anthropic
from agents.llm_provider import LLMProvider


pytestmark = pytest.mark.no_auto_feral_home


# ─────────────────────────────────────────────────────────────────────
# 1) Pure-function translator
# ─────────────────────────────────────────────────────────────────────


def test_data_url_image_translates_to_anthropic_base64_source():
    blocks = [
        {"type": "text", "text": "What's in this screenshot?"},
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,iVBORw0KG..."},
        },
    ]
    out = to_anthropic_blocks(blocks)
    assert out[0] == {"type": "text", "text": "What's in this screenshot?"}
    assert out[1]["type"] == "image"
    assert out[1]["source"]["type"] == "base64"
    assert out[1]["source"]["media_type"] == "image/png"
    assert out[1]["source"]["data"] == "iVBORw0KG..."


def test_data_url_image_jpeg_media_type_preserved():
    blocks = [
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,/9j/4AA..."}},
    ]
    out = to_anthropic_blocks(blocks)
    assert out[0]["source"]["media_type"] == "image/jpeg"
    assert out[0]["source"]["data"] == "/9j/4AA..."


def test_http_url_image_translates_to_anthropic_url_source():
    blocks = [{"type": "image_url", "image_url": {"url": "https://example.com/x.jpg"}}]
    out = to_anthropic_blocks(blocks)
    assert out[0]["type"] == "image"
    assert out[0]["source"]["type"] == "url"
    assert out[0]["source"]["url"] == "https://example.com/x.jpg"


def test_text_only_pass_through():
    blocks = [{"type": "text", "text": "hi"}]
    out = to_anthropic_blocks(blocks)
    assert out == blocks


def test_flat_string_image_url_form_is_accepted():
    """Some older emitters flatten the image_url to a bare string."""
    blocks = [{"type": "image_url", "image_url": "https://example.com/y.png"}]
    out = to_anthropic_blocks(blocks)
    assert out[0]["type"] == "image"
    assert out[0]["source"] == {"type": "url", "url": "https://example.com/y.png"}


def test_native_anthropic_blocks_pass_through_untouched():
    blocks = [
        {"type": "tool_use", "id": "tu_1", "name": "search", "input": {}},
        {"type": "tool_result", "tool_use_id": "tu_1", "content": "ok"},
        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "JVBERi..."}},
    ]
    out = to_anthropic_blocks(blocks)
    assert out == blocks


def test_unknown_block_passes_through_with_warning(caplog):
    caplog.set_level(logging.WARNING, logger="feral.llm.multimodal")
    blocks = [{"type": "feral_screenloop_v3", "payload": {"foo": "bar"}}]
    out = to_anthropic_blocks(blocks)
    assert out == blocks
    assert any(
        "feral_screenloop_v3" in rec.getMessage()
        for rec in caplog.records
    )


def test_empty_image_url_is_dropped_with_warning(caplog):
    caplog.set_level(logging.WARNING, logger="feral.llm.multimodal")
    blocks = [
        {"type": "text", "text": "hello"},
        {"type": "image_url", "image_url": {"url": ""}},
    ]
    out = to_anthropic_blocks(blocks)
    assert out == [{"type": "text", "text": "hello"}]
    assert any("invalid url" in rec.getMessage() for rec in caplog.records)


def test_input_image_responses_shape_translates():
    blocks = [{"type": "input_image", "image_url": "data:image/webp;base64,UklGR..."}]
    out = to_anthropic_blocks(blocks)
    assert out[0]["type"] == "image"
    assert out[0]["source"]["type"] == "base64"
    assert out[0]["source"]["media_type"] == "image/webp"
    assert out[0]["source"]["data"] == "UklGR..."


def test_non_dict_items_pass_through():
    """Forgiveness: a bare string or None mixed in must not crash."""
    out = to_anthropic_blocks(["raw", None, {"type": "text", "text": "x"}])
    assert out == ["raw", None, {"type": "text", "text": "x"}]


# ─────────────────────────────────────────────────────────────────────
# 2) Gemini parity translator
# ─────────────────────────────────────────────────────────────────────


def test_to_gemini_parts_text_only():
    parts = to_gemini_parts([{"type": "text", "text": "hi"}])
    assert parts == [{"text": "hi"}]


def test_to_gemini_parts_data_url_to_inline_data():
    parts = to_gemini_parts([
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBOR..."}}
    ])
    assert parts == [
        {"inline_data": {"mime_type": "image/png", "data": "iVBOR..."}}
    ]


def test_to_gemini_parts_http_url_to_file_data():
    parts = to_gemini_parts([
        {"type": "image_url", "image_url": {"url": "https://example.com/x.jpg"}}
    ])
    assert parts == [
        {"file_data": {"mime_type": "image/png", "file_uri": "https://example.com/x.jpg"}}
    ]


# ─────────────────────────────────────────────────────────────────────
# 3) Provider-boundary integration via ``_convert_messages_for_anthropic``
# ─────────────────────────────────────────────────────────────────────


def test_convert_messages_translates_user_multimodal_content():
    """The Anthropic message converter is the choke point for every
    Anthropic-targeted call path (``_chat_anthropic``,
    ``_chat_stream_anthropic``, ``_build_anthropic_body`` for primary +
    fallback failover). Wiring the translator there picks up all four.
    """
    system, conv = _convert_messages_for_anthropic([
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": [
            {"type": "text", "text": "What's in this screenshot?"},
            {"type": "image_url",
             "image_url": {"url": "data:image/png;base64,AAAA", "detail": "low"}},
        ]},
    ])
    assert system.strip() == "be terse"
    assert len(conv) == 1
    user_msg = conv[0]
    assert user_msg["role"] == "user"
    assert user_msg["content"][0] == {"type": "text", "text": "What's in this screenshot?"}
    assert user_msg["content"][1]["type"] == "image"
    assert user_msg["content"][1]["source"] == {
        "type": "base64", "media_type": "image/png", "data": "AAAA",
    }


def test_convert_messages_passes_plain_string_content_through():
    """Text-only turns must not be touched."""
    _, conv = _convert_messages_for_anthropic([
        {"role": "user", "content": "hello"},
    ])
    assert conv == [{"role": "user", "content": "hello"}]


def test_build_anthropic_body_uses_translated_image_blocks():
    """Sanity-check at the body-build seam used by ``chat_with_failover``."""
    body = LLMProvider._build_anthropic_body(  # type: ignore[arg-type]
        model="claude-opus-4-7",
        messages=[
            {"role": "user", "content": [
                {"type": "text", "text": "describe"},
                {"type": "image_url",
                 "image_url": {"url": "https://example.com/z.png"}},
            ]},
        ],
        tools=None,
        temperature=0.7,
        max_tokens=1024,
    )
    img_block = body["messages"][0]["content"][1]
    assert img_block["type"] == "image"
    assert img_block["source"] == {"type": "url", "url": "https://example.com/z.png"}


# ─────────────────────────────────────────────────────────────────────
# 4) End-to-end: Anthropic HTTP request body sees translated shape
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_chat_anthropic_sends_translated_image_block(monkeypatch):
    """End-to-end smoke: ``_chat_anthropic`` POSTs Anthropic-shape blocks.

    Before the v2026.5.44 fix the dispatcher forwarded the OpenAI
    ``image_url`` part verbatim and api.anthropic.com 400-ed. This
    test stubs ``self.client.post`` and asserts the wire body contains
    the Anthropic ``image`` block with the right base64 source.
    """
    # Disable boot side effects + force the provider into the
    # anthropic branch without hitting the network.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    captured: dict = {}

    async def fake_post(path, json=None, **_kwargs):
        captured["path"] = path
        captured["body"] = json
        # Minimal Anthropic Messages API success envelope.
        return httpx.Response(
            200,
            json={
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-4-7",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
            request=httpx.Request("POST", "https://api.anthropic.com/v1" + path),
        )

    # Instantiate, then point at anthropic + swap the client. The
    # ``__init__`` reads env so ANTHROPIC_API_KEY is already set above.
    provider = LLMProvider()
    provider.provider = "anthropic"
    provider.model = "claude-opus-4-7"
    provider.api_key = "test-key"
    provider.base_url = "https://api.anthropic.com/v1"
    provider.client = MagicMock()
    provider.client.post = AsyncMock(side_effect=fake_post)

    messages = [
        {"role": "user", "content": [
            {"type": "text", "text": "What's in this screenshot?"},
            {"type": "image_url",
             "image_url": {"url": "data:image/png;base64,iVBORw0KG..."}},
        ]},
    ]
    result = await provider._chat_anthropic(
        messages, tools=None, temperature=0.7, max_tokens=1024,
    )

    assert "error" not in result, result
    assert captured["path"] == "/messages"
    body = captured["body"]
    user_content = body["messages"][0]["content"]
    # Translated text block survives.
    assert user_content[0] == {"type": "text", "text": "What's in this screenshot?"}
    # Image block is Anthropic-shape, not OpenAI-shape.
    assert user_content[1]["type"] == "image"
    assert user_content[1]["source"] == {
        "type": "base64", "media_type": "image/png", "data": "iVBORw0KG...",
    }
    # No leftover OpenAI key — Anthropic 400s on extras under strict
    # validation, so make sure we didn't accidentally double-emit.
    assert "image_url" not in user_content[1]
