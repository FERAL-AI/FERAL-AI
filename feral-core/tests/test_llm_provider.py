"""
Tests for ``LLMProvider``: env-based setup, Anthropic normalization, streaming, and hot-swap.
"""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from agents.llm_provider import (
    LLMProvider,
    SUPPORTED_RUNTIME_PROVIDERS,
    is_supported_runtime_provider,
)


@pytest.fixture
def anthropic_env() -> dict[str, str]:
    return {
        "FERAL_LLM_PROVIDER": "anthropic",
        "ANTHROPIC_API_KEY": "sk-ant-test",
        "FERAL_LLM_MODEL": "claude-test-model",
    }


class TestLLMProviderInit:
    def test_init_detects_provider_from_env(self, anthropic_env: dict[str, str]) -> None:
        with patch.dict(os.environ, anthropic_env, clear=False):
            with patch.object(LLMProvider, "_detect_ollama", return_value=None):
                llm = LLMProvider()
        assert llm.provider == "anthropic"
        assert "anthropic.com" in llm.base_url or llm.base_url.endswith("/v1")
        assert llm.model == "claude-test-model"

    def test_available_property_true_with_key(self, anthropic_env: dict[str, str]) -> None:
        with patch.dict(os.environ, anthropic_env, clear=False):
            with patch.object(LLMProvider, "_detect_ollama", return_value=None):
                llm = LLMProvider()
        assert llm.available is True

    def test_available_false_without_keys_and_no_ollama(self) -> None:
        env = {
            "FERAL_LLM_PROVIDER": "openai",
            "OPENAI_API_KEY": "",
        }
        with patch.dict(os.environ, env, clear=False):
            with patch.object(LLMProvider, "_detect_ollama", return_value=None):
                llm = LLMProvider()
        assert llm.available is False


class TestChatAnthropic:
    @pytest.mark.asyncio
    async def test_chat_anthropic_normalizes_openai_shape(self, anthropic_env: dict[str, str]) -> None:
        anthropic_payload = {
            "content": [
                {"type": "text", "text": "Hello from Claude"},
            ],
            "stop_reason": "end_turn",
        }

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = anthropic_payload

        post_mock = AsyncMock(return_value=mock_resp)

        with patch.dict(os.environ, anthropic_env, clear=False):
            with patch.object(LLMProvider, "_detect_ollama", return_value=None):
                llm = LLMProvider()
        llm.client.post = post_mock

        out = await llm._chat_anthropic(
            [{"role": "user", "content": "Hi"}],
            tools=None,
            temperature=0.5,
            max_tokens=128,
        )

        post_mock.assert_awaited_once()
        call_kw = post_mock.call_args
        assert call_kw[0][0] == "/messages"
        body = call_kw[1]["json"]
        assert body["model"] == llm.model
        assert body["messages"] == [{"role": "user", "content": "Hi"}]

        text, tools = llm.extract_response(out)
        assert text == "Hello from Claude"
        assert tools == []


class TestChatStream:
    @pytest.mark.asyncio
    async def test_chat_stream_yields_text_delta_and_done(self) -> None:
        env = {
            "FERAL_LLM_PROVIDER": "openai",
            "OPENAI_API_KEY": "sk-openai-test",
            "FERAL_LLM_MODEL": "gpt-test",
        }

        lines = [
            'data: {"choices":[{"delta":{"content":"Hel"}}]}',
            'data: {"choices":[{"delta":{"content":"lo"}}]}',
            "data: [DONE]",
        ]

        async def aiter_lines():
            for line in lines:
                yield line

        mock_stream_resp = MagicMock()
        mock_stream_resp.raise_for_status = MagicMock()
        mock_stream_resp.aiter_lines = aiter_lines

        stream_cm = MagicMock()
        stream_cm.__aenter__ = AsyncMock(return_value=mock_stream_resp)
        stream_cm.__aexit__ = AsyncMock(return_value=None)

        with patch.dict(os.environ, env, clear=False):
            with patch.object(LLMProvider, "_detect_ollama", return_value=None):
                llm = LLMProvider()
        llm.client.stream = MagicMock(return_value=stream_cm)

        events: list[dict] = []
        async for ev in llm.chat_stream([{"role": "user", "content": "x"}], tools=None):
            events.append(ev)

        texts = [e["content"] for e in events if e.get("type") == "text_delta"]
        assert "".join(texts) == "Hello"
        assert events[-1] == {"type": "done"}

    @pytest.mark.asyncio
    async def test_chat_stream_uses_nonstream_failover_on_primary_400(self) -> None:
        env = {
            "FERAL_LLM_PROVIDER": "openai",
            "OPENAI_API_KEY": "sk-openai-test",
            "FERAL_LLM_MODEL": "gpt-4o-mini",
        }
        with patch.dict(os.environ, env, clear=False):
            with patch.object(LLMProvider, "_detect_ollama", return_value=None):
                llm = LLMProvider()

        response = httpx.Response(
            400,
            json={"error": {"type": "invalid_request_error", "param": "model", "message": "bad model"}},
            request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"),
        )
        stream_error = httpx.HTTPStatusError("400 bad request", request=response.request, response=response)

        stream_cm = MagicMock()
        stream_cm.__aenter__ = AsyncMock(side_effect=stream_error)
        stream_cm.__aexit__ = AsyncMock(return_value=None)
        llm.client.stream = MagicMock(return_value=stream_cm)

        llm.set_config({"fallback_providers": ["anthropic"]})
        llm.chat_with_failover = AsyncMock(
            return_value={"choices": [{"message": {"content": "fallback answer"}}]}
        )

        events = []
        async for ev in llm.chat_stream([{"role": "user", "content": "hi"}], tools=None):
            events.append(ev)

        assert events[0]["type"] == "text_delta"
        assert "fallback answer" in events[0]["content"]
        assert events[-1] == {"type": "done"}
        llm.chat_with_failover.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_chat_stream_surfaces_structured_http_error_details(self) -> None:
        env = {
            "FERAL_LLM_PROVIDER": "openai",
            "OPENAI_API_KEY": "sk-openai-test",
            "FERAL_LLM_MODEL": "gpt-4o-mini",
        }
        with patch.dict(os.environ, env, clear=False):
            with patch.object(LLMProvider, "_detect_ollama", return_value=None):
                llm = LLMProvider()

        response = httpx.Response(
            400,
            json={
                "error": {
                    "type": "invalid_request_error",
                    "param": "model",
                    "message": "The model is invalid for chat.completions",
                }
            },
            request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"),
        )
        stream_error = httpx.HTTPStatusError("400 bad request", request=response.request, response=response)

        stream_cm = MagicMock()
        stream_cm.__aenter__ = AsyncMock(side_effect=stream_error)
        stream_cm.__aexit__ = AsyncMock(return_value=None)
        llm.client.stream = MagicMock(return_value=stream_cm)
        llm.set_config({"fallback_providers": []})

        events = []
        async for ev in llm.chat_stream([{"role": "user", "content": "hi"}], tools=None):
            events.append(ev)

        assert events
        assert events[0]["type"] == "error"
        assert "HTTP 400" in events[0]["content"]
        assert "invalid_request_error" in events[0]["content"]
        assert "param=model" in events[0]["content"]

    @pytest.mark.asyncio
    async def test_chat_stream_rejects_completion_only_model_preflight(self) -> None:
        env = {
            "FERAL_LLM_PROVIDER": "openai",
            "OPENAI_API_KEY": "sk-openai-test",
            "FERAL_LLM_MODEL": "babbage-002",
        }
        with patch.dict(os.environ, env, clear=False):
            with patch.object(LLMProvider, "_detect_ollama", return_value=None):
                llm = LLMProvider()

        llm.client.stream = MagicMock()
        events = []
        async for ev in llm.chat_stream([{"role": "user", "content": "hi"}], tools=None):
            events.append(ev)

        assert events
        assert events[0]["type"] == "error"
        assert "completion-only" in events[0]["content"]
        llm.client.stream.assert_not_called()


class TestSwitchProvider:
    @pytest.mark.asyncio
    async def test_switch_provider_updates_base_url_and_model(self) -> None:
        env = {
            "FERAL_LLM_PROVIDER": "openai",
            "OPENAI_API_KEY": "sk-x",
        }
        with patch.dict(os.environ, env, clear=False):
            with patch.object(LLMProvider, "_detect_ollama", return_value=None):
                llm = LLMProvider()

        await llm.switch_provider("groq", model="llama-custom", api_key="gq-key")

        assert llm.provider == "groq"
        assert llm.base_url == "https://api.groq.com/openai/v1"
        assert llm.model == "llama-custom"
        assert llm.api_key == "gq-key"

        await llm.close()


class TestVisionAndPresets:
    @pytest.mark.asyncio
    async def test_ollama_text_model_rejects_image_input(self) -> None:
        env = {
            "FERAL_LLM_PROVIDER": "ollama",
            "FERAL_LLM_MODEL": "llama3.1",
        }
        with patch.dict(os.environ, env, clear=False):
            with patch.object(LLMProvider, "_detect_ollama", return_value="http://127.0.0.1:11434"):
                llm = LLMProvider()

        out = await llm.chat(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "describe this"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
                    ],
                }
            ],
            tools=None,
        )
        assert "error" in out
        assert "ollama_vision" in out["error"]
        await llm.close()

    @pytest.mark.asyncio
    async def test_chat_stream_yields_error_for_vision_mismatch(self) -> None:
        env = {
            "FERAL_LLM_PROVIDER": "local",
            "FERAL_LLM_MODEL": "tiny-local",
        }
        with patch.dict(os.environ, env, clear=False):
            with patch.object(LLMProvider, "_detect_ollama", return_value=None):
                llm = LLMProvider()

        events = []
        async for ev in llm.chat_stream(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what is this"},
                        {"type": "image_url", "image_url": {"url": "https://example.com/pic.png"}},
                    ],
                }
            ],
            tools=None,
        ):
            events.append(ev)

        assert events
        assert events[0]["type"] == "error"
        await llm.close()

    @pytest.mark.asyncio
    async def test_apply_preset_switches_provider_model(self) -> None:
        env = {
            "FERAL_LLM_PROVIDER": "openai",
            "OPENAI_API_KEY": "sk-test",
        }
        with patch.dict(os.environ, env, clear=False):
            with patch.object(LLMProvider, "_detect_ollama", return_value=None):
                llm = LLMProvider()

        presets = llm.list_presets()
        assert any(p["id"] == "ollama_vision" for p in presets)

        result = await llm.apply_preset("ollama_vision")
        assert result["ok"] is True
        assert llm.provider == "ollama"
        assert llm.model == "llava"
        await llm.close()


class TestSupportedRuntimeProviders:
    """The runtime registry is the single source of truth for which
    provider ids can actually run a chat call. Catalog-only ids
    (``bedrock``, ``together``, ``fireworks``) expose no wire here
    and must be reported honestly instead of silently masquerading
    as OpenAI — which is exactly what the -A3 code did via
    every ``dict.get(key, OPENAI_DEFAULT)`` fallback in this module.
    """

    def test_registry_contains_all_runtime_wired_providers(self) -> None:
        required = {
            "openai", "anthropic", "gemini", "groq", "deepseek",
            "openrouter", "kimi", "qwen",
            "ollama", "lmstudio", "local", "hybrid",
        }
        assert required <= SUPPORTED_RUNTIME_PROVIDERS

    def test_known_providers_are_supported(self) -> None:
        for pid in ("openai", "anthropic", "gemini", "ollama", "lmstudio"):
            assert is_supported_runtime_provider(pid), pid

    def test_unknown_providers_are_unsupported(self) -> None:
        # Catalog-only descriptors + typos + empty string must all
        # report False — the runtime has no adapter for them.
        for pid in ("bedrock", "together", "fireworks", "open ai", "gpt", "", "not-real"):
            assert not is_supported_runtime_provider(pid), pid


class TestInitWithUnknownProvider:
    """``__init__`` used to silently redirect any unknown provider to
    ``https://api.openai.com/v1`` and inherit ``OPENAI_API_KEY``. The
    new contract keeps the unknown id visible, refuses to pretend the
    runtime is available, and never leaks the OpenAI key to a
    different provider's metrics / log lines.
    """

    def test_unknown_provider_does_not_silently_become_openai(self) -> None:
        env = {
            "FERAL_LLM_PROVIDER": "bedrock",
            "OPENAI_API_KEY": "sk-from-openai",
            "FERAL_LLM_BASE_URL": "",
        }
        with patch.dict(os.environ, env, clear=False):
            with patch.object(LLMProvider, "_detect_ollama", return_value=None):
                with patch.object(LLMProvider, "_detect_lmstudio", return_value=None):
                    llm = LLMProvider()
        assert llm.provider == "bedrock"
        assert llm.available is False
        # Must NOT have inherited the OpenAI base_url.
        assert "api.openai.com" not in (llm.base_url or "")
        # Must NOT have leaked the OpenAI key into the bedrock slot.
        assert llm.api_key != "sk-from-openai"

    def test_unknown_provider_with_explicit_base_url_is_honoured(self) -> None:
        """Operator-supplied base_url is the documented escape hatch for
        custom OpenAI-compatible gateways — we trust it."""
        env = {
            "FERAL_LLM_PROVIDER": "my-gateway",
            "FERAL_LLM_BASE_URL": "https://gw.example/v1",
            "OPENAI_API_KEY": "sk-gateway",
        }
        with patch.dict(os.environ, env, clear=False):
            with patch.object(LLMProvider, "_detect_ollama", return_value=None):
                with patch.object(LLMProvider, "_detect_lmstudio", return_value=None):
                    llm = LLMProvider()
        assert llm.provider == "my-gateway"
        assert llm.base_url == "https://gw.example/v1"


class TestSwitchProviderUnknown:
    @pytest.mark.asyncio
    async def test_switch_to_unknown_provider_marks_unavailable(self) -> None:
        """Switching to a catalog-only id without a base_url override
        must not silently retarget OpenAI. The new contract: mark the
        adapter unavailable and keep the unknown id visible."""
        env = {"FERAL_LLM_PROVIDER": "openai", "OPENAI_API_KEY": "sk-x"}
        with patch.dict(os.environ, env, clear=False):
            with patch.object(LLMProvider, "_detect_ollama", return_value=None):
                llm = LLMProvider()

        await llm.switch_provider("bedrock", model="", api_key="anything")

        assert llm.provider == "bedrock"
        assert llm.available is False
        assert "api.openai.com" not in (llm.base_url or "")
        await llm.close()

    @pytest.mark.asyncio
    async def test_switch_to_unknown_provider_with_base_url_is_honoured(self) -> None:
        env = {"FERAL_LLM_PROVIDER": "openai", "OPENAI_API_KEY": "sk-x"}
        with patch.dict(os.environ, env, clear=False):
            with patch.object(LLMProvider, "_detect_ollama", return_value=None):
                llm = LLMProvider()

        await llm.switch_provider(
            "my-gateway",
            model="custom-model",
            api_key="sk-custom",
            base_url="https://gw.example/v1",
        )

        assert llm.provider == "my-gateway"
        assert llm.base_url == "https://gw.example/v1"
        assert llm.api_key == "sk-custom"
        assert llm.available is True
        await llm.close()

    @pytest.mark.asyncio
    async def test_switch_to_deepseek_hits_deepseek_not_openai(self) -> None:
        """Regression: ``switch_provider("deepseek")`` used to fall
        through the old PROVIDER_BASES dict (which didn't list
        deepseek, openrouter, kimi or qwen) and silently re-point at
        ``https://api.openai.com/v1``. The unified registry path now
        keeps each provider on its own endpoint."""
        env = {"FERAL_LLM_PROVIDER": "openai", "OPENAI_API_KEY": "sk-x"}
        with patch.dict(os.environ, env, clear=False):
            with patch.object(LLMProvider, "_detect_ollama", return_value=None):
                llm = LLMProvider()

        await llm.switch_provider("deepseek", model="deepseek-chat", api_key="sk-ds")
        assert llm.provider == "deepseek"
        assert "deepseek.com" in llm.base_url
        assert "openai.com" not in llm.base_url
        await llm.close()

        await llm.switch_provider("openrouter", model="anthropic/claude-opus-4-7", api_key="sk-or")
        assert llm.provider == "openrouter"
        assert "openrouter.ai" in llm.base_url
        await llm.close()


class TestGetProviderConfigUnknown:
    def test_get_provider_config_unknown_returns_unsupported_shape(self) -> None:
        env = {"FERAL_LLM_PROVIDER": "openai", "OPENAI_API_KEY": "sk-x"}
        with patch.dict(os.environ, env, clear=False):
            with patch.object(LLMProvider, "_detect_ollama", return_value=None):
                llm = LLMProvider()

        cfg = llm._get_provider_config("bedrock")
        assert cfg["supported"] is False
        assert cfg["base_url"] == ""
        assert cfg["api_key"] == ""

    def test_get_provider_config_known_marks_supported_true(self) -> None:
        env = {
            "FERAL_LLM_PROVIDER": "openai",
            "OPENAI_API_KEY": "sk-x",
            "ANTHROPIC_API_KEY": "sk-ant",
        }
        with patch.dict(os.environ, env, clear=False):
            with patch.object(LLMProvider, "_detect_ollama", return_value=None):
                llm = LLMProvider()
            cfg = llm._get_provider_config("anthropic")
            assert cfg["supported"] is True
            assert "anthropic.com" in cfg["base_url"]
            assert cfg["api_key"] == "sk-ant"


class TestHealthSnapshotUnsupported:
    def test_health_snapshot_flags_unsupported_fallback(self) -> None:
        """Unsupported fallback candidates must be rendered
        explicitly — ``supported=False`` + ``has_key=False`` — and
        must NOT inflate ``total_available`` even when a same-named
        env var is present."""
        env = {"FERAL_LLM_PROVIDER": "openai", "OPENAI_API_KEY": "sk-x"}
        with patch.dict(os.environ, env, clear=False):
            with patch.object(LLMProvider, "_detect_ollama", return_value=None):
                llm = LLMProvider()

        llm.set_config({"fallback_providers": ["bedrock", "anthropic"]})

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant"}, clear=False):
            snap = llm.health_snapshot()

        by_name = {c["provider"]: c for c in snap["candidates"]}
        assert by_name["openai"]["supported"] is True
        assert by_name["bedrock"]["supported"] is False
        assert by_name["bedrock"]["has_key"] is False
        assert by_name["bedrock"]["base_url"] == ""
        assert by_name["anthropic"]["supported"] is True
        # total_available excludes bedrock (unsupported) even if the
        # OpenAI-keyed primary were somehow counted.
        assert "bedrock" not in [
            c["provider"] for c in snap["candidates"]
            if c["has_key"] and not c["in_cooldown"] and c["supported"]
        ]

    def test_health_snapshot_includes_budget_context(self) -> None:
        env = {"FERAL_LLM_PROVIDER": "openai", "OPENAI_API_KEY": "sk-x"}
        with patch.dict(os.environ, env, clear=False):
            with patch.object(LLMProvider, "_detect_ollama", return_value=None):
                llm = LLMProvider()

        llm.set_config({
            "fallback_providers": [],
            "daily_budget_usd": 2.0,
            "daily_spend_usd": 1.5,
            "budget_tight_ratio": 0.25,
        })
        llm._last_budget_routing = {
            "remaining_usd": 0.5,
            "candidate_costs": [{"provider": "openai", "estimated_usd": 0.02, "affordable": True}],
            "over_budget_providers": [],
            "prompt_tokens_estimate": 123,
            "completion_tokens_estimate": 456,
        }

        snap = llm.health_snapshot()
        budget = snap["budget"]
        assert budget["enabled"] is True
        assert budget["daily_budget_usd"] == 2.0
        assert budget["daily_spend_usd"] == 1.5
        assert budget["remaining_usd"] == 0.5
        assert budget["last_routing"]["prompt_tokens_estimate"] == 123


class TestChatRefusesUnsupportedProvider:
    @pytest.mark.asyncio
    async def test_chat_refuses_unsupported_primary(self) -> None:
        env = {
            "FERAL_LLM_PROVIDER": "openai",
            "OPENAI_API_KEY": "sk-x",
        }
        with patch.dict(os.environ, env, clear=False):
            with patch.object(LLMProvider, "_detect_ollama", return_value=None):
                llm = LLMProvider()

        # Flip the primary to a bogus provider without going through
        # switch_provider — simulates a stale settings.json loaded at
        # boot that names a catalog-only descriptor.
        llm.provider = "bedrock"
        out = await llm.chat([{"role": "user", "content": "hi"}])
        assert "error" in out
        assert "bedrock" in out["error"]
        assert out["choices"] == []
        await llm.close()

    @pytest.mark.asyncio
    async def test_chat_stream_refuses_unsupported_primary(self) -> None:
        env = {
            "FERAL_LLM_PROVIDER": "openai",
            "OPENAI_API_KEY": "sk-x",
        }
        with patch.dict(os.environ, env, clear=False):
            with patch.object(LLMProvider, "_detect_ollama", return_value=None):
                llm = LLMProvider()

        llm.provider = "bedrock"
        events = []
        async for ev in llm.chat_stream([{"role": "user", "content": "hi"}]):
            events.append(ev)

        assert events
        assert events[0]["type"] == "error"
        assert "bedrock" in events[0]["content"]
        await llm.close()
