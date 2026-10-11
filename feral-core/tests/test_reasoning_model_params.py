"""Wire-shape tests for the per-provider reasoning-family param fork.

These tests invoke the adapter's ``chat()`` coroutine (or the
``apply_reasoning_fork`` helper from ``agents.llm_provider``) against a
mocked ``httpx.AsyncClient`` and assert the outbound JSON body's
parameter names match the provider's reasoning-mode contract.

The shipped v2026.5.0 400s were all here: OpenAI reasoning models
require ``max_completion_tokens`` (not ``max_tokens``) and forbid
``temperature != 1`` / ``top_p`` / penalty params. Anthropic extended-
thinking requires a ``thinking`` block. DeepSeek v4-pro requires
``extra_body.thinking``. Gemini ``-thinking`` requires
``generationConfig.thinkingConfig.enabled``. Each assertion below is
the exact shape that, when violated, produces the 400 in the log.

None of these tests hit the real API (the live-smoke matrix is
env-gated and lives outside CI).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from providers.anthropic_provider import AnthropicProvider
from providers.base import ChatMessage
from providers.deepseek_provider import DeepSeekProvider
from providers.gemini_provider import GeminiProvider
from providers.groq_provider import GroqProvider
from providers.openai_provider import OpenAIProvider


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_openai_json_response(text: str = "hi") -> dict[str, Any]:
    return {
        "model": "ignored",
        "choices": [
            {"message": {"content": text, "role": "assistant"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def _make_anthropic_json_response() -> dict[str, Any]:
    return {
        "model": "ignored",
        "content": [{"type": "text", "text": "hi"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def _make_gemini_json_response() -> dict[str, Any]:
    return {
        "candidates": [{"content": {"parts": [{"text": "hi"}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1, "totalTokenCount": 2},
    }


async def _capture_openai_body(
    adapter_factory, *, model: str, **chat_kwargs: Any
) -> dict[str, Any]:
    """Drive an OpenAI-compat adapter once and return the POST body."""
    captured: dict[str, Any] = {}

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return _make_openai_json_response()

    async def _post(url: str, **kwargs: Any) -> _Resp:
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        captured["headers"] = kwargs.get("headers")
        return _Resp()

    fake_client = AsyncMock()
    fake_client.post = _post
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=fake_client):
        adapter = adapter_factory()
        await adapter.chat(
            [ChatMessage(role="user", content="hello")],
            model=model,
            **chat_kwargs,
        )
    return captured["json"]


# ---------------------------------------------------------------------------
# OpenAI reasoning fork (chat completions path)
# ---------------------------------------------------------------------------


class TestOpenAIReasoningFork:
    @pytest.mark.parametrize("model", ["gpt-5.5", "gpt-5.4-mini", "gpt-5", "o3", "o4-mini", "o1"])
    async def test_reasoning_model_uses_max_completion_tokens(self, model: str) -> None:
        body = await _capture_openai_body(
            lambda: OpenAIProvider(api_key="sk-test"),
            model=model,
            max_tokens=128,
            temperature=0.7,
        )
        key = "max_output_tokens" if model in {"gpt-5.5", "gpt-5.4-mini"} else "max_completion_tokens"
        assert body[key] == 128
        assert "max_tokens" not in body

    @pytest.mark.parametrize("model", ["gpt-5.5", "o3"])
    async def test_reasoning_model_strips_temperature_when_not_one(self, model: str) -> None:
        body = await _capture_openai_body(
            lambda: OpenAIProvider(api_key="sk-test"),
            model=model,
            temperature=0.5,
        )
        assert "temperature" not in body

    async def test_reasoning_model_keeps_temperature_eq_one(self) -> None:
        body = await _capture_openai_body(
            lambda: OpenAIProvider(api_key="sk-test"),
            model="gpt-5.5",
            temperature=1,
        )
        # temperature=1 is the only allowed reasoning value; may appear
        # or may be dropped as a no-op — either is fine, the test
        # guarantees we don't SEND a non-1 value.
        assert body.get("temperature", 1) == 1

    async def test_reasoning_model_sets_default_reasoning_effort(self) -> None:
        body = await _capture_openai_body(
            lambda: OpenAIProvider(api_key="sk-test"),
            model="gpt-5.5",
            max_tokens=32,
        )
        assert body.get("reasoning", {}).get("effort") == "medium"

    async def test_reasoning_effort_override_respected(self) -> None:
        body = await _capture_openai_body(
            lambda: OpenAIProvider(api_key="sk-test"),
            model="gpt-5.5",
            max_tokens=32,
            reasoning_effort="high",
        )
        assert body.get("reasoning", {}).get("effort") == "high"

    @pytest.mark.parametrize("model", ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-3.5-turbo"])
    async def test_non_reasoning_model_keeps_legacy_shape(self, model: str) -> None:
        body = await _capture_openai_body(
            lambda: OpenAIProvider(api_key="sk-test"),
            model=model,
            max_tokens=64,
            temperature=0.3,
        )
        assert body["max_tokens"] == 64
        assert body["temperature"] == 0.3
        assert "max_completion_tokens" not in body
        assert "reasoning_effort" not in body


# ---------------------------------------------------------------------------
# DeepSeek reasoning fork
# ---------------------------------------------------------------------------


class TestDeepSeekReasoningFork:
    async def test_v4_pro_adds_thinking_block(self) -> None:
        body = await _capture_openai_body(
            lambda: DeepSeekProvider(api_key="ds-test"),
            model="deepseek-v4-pro",
            temperature=0.7,
            max_tokens=256,
        )
        assert body.get("extra_body", {}).get("thinking", {}).get("type") == "enabled"
        assert body.get("reasoning_effort") == "high"
        # Sampling params stripped.
        for key in ("temperature", "top_p", "presence_penalty", "frequency_penalty"):
            assert key not in body, f"DeepSeek v4-pro should not receive {key}"

    async def test_reasoner_alias_forks_too(self) -> None:
        body = await _capture_openai_body(
            lambda: DeepSeekProvider(api_key="ds-test"),
            model="deepseek-reasoner",
            temperature=0.7,
        )
        assert body.get("extra_body", {}).get("thinking", {}).get("type") == "enabled"

    async def test_v4_flash_passes_through(self) -> None:
        body = await _capture_openai_body(
            lambda: DeepSeekProvider(api_key="ds-test"),
            model="deepseek-v4-flash",
            temperature=0.4,
            max_tokens=128,
        )
        assert "extra_body" not in body or body["extra_body"] == {}
        assert body.get("temperature") == 0.4
        assert body.get("max_tokens") == 128
        assert "reasoning_effort" not in body

    async def test_reasoning_effort_max_override(self) -> None:
        body = await _capture_openai_body(
            lambda: DeepSeekProvider(api_key="ds-test"),
            model="deepseek-v4-pro",
            reasoning_effort="max",
        )
        assert body.get("reasoning_effort") == "max"


# ---------------------------------------------------------------------------
# Anthropic reasoning fork
# ---------------------------------------------------------------------------


async def _capture_anthropic_body(model: str, **chat_kwargs: Any) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return _make_anthropic_json_response()

    async def _post(url: str, **kwargs: Any) -> _Resp:
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        captured["headers"] = kwargs.get("headers")
        return _Resp()

    fake_client = AsyncMock()
    fake_client.post = _post
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=fake_client):
        adapter = AnthropicProvider(api_key="ak-test")
        await adapter.chat(
            [ChatMessage(role="user", content="hello")],
            model=model,
            **chat_kwargs,
        )
    return captured["json"]


class TestAnthropicReasoningFork:
    async def test_extended_thinking_model_adds_thinking_block(self) -> None:
        body = await _capture_anthropic_body(
            "claude-sonnet-4-6",
            reasoning=True,
        )
        thinking = body.get("thinking")
        assert thinking is not None, (
            "Sonnet 4.6 is extended-thinking-capable; the fork MUST add "
            "thinking={'type':'enabled','budget_tokens':N}"
        )
        assert thinking["type"] == "enabled"
        assert thinking["budget_tokens"] > 0

    async def test_adaptive_thinking_opus_47_omits_block(self) -> None:
        body = await _capture_anthropic_body(
            "claude-opus-4-7",
            reasoning=True,
        )
        # Opus 4.7 uses adaptive thinking (no explicit block). Sending
        # thinking={"type":"enabled"} is a 400 — the fork must NOT add
        # it for this model.
        assert "thinking" not in body, (
            "claude-opus-4-7 declines the explicit thinking block — "
            "sending one triggers the live 400 in the v2026.5.0 log"
        )

    async def test_haiku_45_extended_thinking_opt_in(self) -> None:
        body = await _capture_anthropic_body(
            "claude-haiku-4-5",
            reasoning=True,
            thinking_budget=4096,
        )
        assert body.get("thinking", {}).get("budget_tokens") == 4096

    async def test_non_reasoning_request_no_thinking(self) -> None:
        # Default chat call without `reasoning=True` — the adapter
        # still classifies claude-opus-4-7 as reasoning but the
        # ``reasoning`` flag is required to add the (empty) thinking
        # block. Keep Opus 4.7's adaptive default -> no thinking key.
        body = await _capture_anthropic_body("claude-opus-4-7")
        assert "thinking" not in body


# ---------------------------------------------------------------------------
# Gemini reasoning fork
# ---------------------------------------------------------------------------


async def _capture_gemini_body(model: str, **chat_kwargs: Any) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return _make_gemini_json_response()

    async def _post(url: str, **kwargs: Any) -> _Resp:
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        return _Resp()

    fake_client = AsyncMock()
    fake_client.post = _post
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=fake_client):
        adapter = GeminiProvider(api_key="gk-test")
        await adapter.chat(
            [ChatMessage(role="user", content="hello")],
            model=model,
            **chat_kwargs,
        )
    return captured["json"]


class TestGeminiReasoningFork:
    async def test_thinking_variant_sets_thinking_config(self) -> None:
        body = await _capture_gemini_body("gemini-3.1-pro-thinking")
        gen = body.get("generationConfig", {})
        assert gen.get("thinkingConfig", {}).get("enabled") is True

    async def test_non_thinking_variant_no_thinking_config(self) -> None:
        body = await _capture_gemini_body("gemini-3.1-pro-preview", max_tokens=128)
        gen = body.get("generationConfig", {})
        assert "thinkingConfig" not in gen

    async def test_thinking_budget_honored(self) -> None:
        body = await _capture_gemini_body(
            "gemini-3.1-pro-thinking", thinking_budget=8192
        )
        assert (
            body["generationConfig"]["thinkingConfig"].get("thinkingBudget")
            == 8192
        )


# ---------------------------------------------------------------------------
# Groq reasoning fork (mirrors OpenAI)
# ---------------------------------------------------------------------------


class TestGroqReasoningFork:
    async def test_r1_distill_uses_max_completion_tokens(self) -> None:
        body = await _capture_openai_body(
            lambda: GroqProvider(api_key="gsk-test"),
            model="deepseek-r1-distill-llama-70b",
            max_tokens=256,
            temperature=0.5,
        )
        assert body.get("max_completion_tokens") == 256
        assert "max_tokens" not in body
        assert "temperature" not in body

    async def test_non_reasoning_groq_passthrough(self) -> None:
        body = await _capture_openai_body(
            lambda: GroqProvider(api_key="gsk-test"),
            model="llama-3.3-70b-versatile",
            max_tokens=256,
            temperature=0.5,
        )
        assert body["max_tokens"] == 256
        assert body["temperature"] == 0.5
        assert "max_completion_tokens" not in body


# ---------------------------------------------------------------------------
# Dispatcher-level fork (agents.llm_provider.apply_reasoning_fork)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
class TestDispatcherFork:
    """The dispatcher-level helper must match the adapter behavior.

    Methods are sync on purpose — the class-level asyncio loop scope
    keeps pytest-asyncio from warning that each method is not async
    (we still inherit the parametrise / fixture integration). No event
    loop is used; ``apply_reasoning_fork`` is pure.
    """

    async def test_dispatcher_fork_openai_reasoning(self) -> None:
        from agents.llm_provider import apply_reasoning_fork
        body = {"model": "gpt-5.5", "max_tokens": 128, "temperature": 0.7, "top_p": 0.9}
        apply_reasoning_fork("openai", "gpt-5.5", body)
        assert body["max_completion_tokens"] == 128
        assert "max_tokens" not in body
        assert "temperature" not in body
        assert "top_p" not in body
        assert body["reasoning_effort"] == "medium"

    async def test_dispatcher_fork_openai_non_reasoning(self) -> None:
        from agents.llm_provider import apply_reasoning_fork
        body = {"model": "gpt-4o", "max_tokens": 128, "temperature": 0.7}
        apply_reasoning_fork("openai", "gpt-4o", body)
        assert body == {"model": "gpt-4o", "max_tokens": 128, "temperature": 0.7}

    async def test_dispatcher_fork_deepseek_v4_pro(self) -> None:
        from agents.llm_provider import apply_reasoning_fork
        body = {"model": "deepseek-v4-pro", "temperature": 0.7, "top_p": 0.9, "max_tokens": 512}
        apply_reasoning_fork("deepseek", "deepseek-v4-pro", body)
        assert body["extra_body"]["thinking"]["type"] == "enabled"
        assert body["reasoning_effort"] == "high"
        assert "temperature" not in body
        assert body["max_tokens"] == 512  # max_tokens is accepted on DeepSeek

    async def test_dispatcher_fork_gemini_thinking(self) -> None:
        from agents.llm_provider import apply_reasoning_fork
        body = {"generationConfig": {"maxOutputTokens": 512}}
        apply_reasoning_fork("gemini", "gemini-3.1-pro-thinking", body)
        assert body["generationConfig"]["thinkingConfig"]["enabled"] is True
