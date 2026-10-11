"""Cold local inference timeout policy and safe transport failure messages."""
from unittest.mock import patch

import httpx
import pytest

from agents.llm_provider import LLMProvider, _describe_error


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,base_url,read,connect", [
    ("ollama", "http://127.0.0.1:11435/v1", 180, 10),
    ("ollama", "http://localhost:11434/v1", 180, 10),
    ("lmstudio", "http://[::1]:1234/v1", 180, 10),
    ("ollama", "http://192.168.1.20:11434/v1", 60, 60),
    ("ollama", "https://remote.example/v1", 60, 60),
    ("openai", "https://api.openai.com/v1", 60, 60),
    ("openai", "http://127.0.0.1:8080/v1", 60, 60),
])
async def test_client_timeout_recomputed_on_hot_swaps(provider, base_url, read, connect, monkeypatch):
    # Switch directly and through REST's reconfigure hot path; no requests
    # or provider discovery are needed to verify transport construction.
    async def healthy_probe(self):
        return True, "mocked healthy probe"
    monkeypatch.setattr(LLMProvider, "_probe_chat_availability", healthy_probe)
    with patch.object(LLMProvider, "__init__", lambda self: None):
        llm = LLMProvider()
    llm.provider = "ollama"
    llm.model = "local-test"
    llm.base_url = "http://localhost:11434/v1"
    llm.api_key = ""
    llm.client = llm._build_client()
    llm._codex_adapter = None
    try:
        await llm.switch_provider(provider, model="test-model", api_key="test-only-key", base_url=base_url)
        assert llm.client.timeout.read == read
        assert llm.client.timeout.connect == connect
        assert llm.client.timeout.write == 60
        assert llm.client.timeout.pool == 60
        await llm.reconfigure(provider="ollama", model="local-test", base_url="http://127.0.0.1:11435/v1")
        assert llm.client.timeout.read == 180
        assert llm.client.timeout.connect == 10
        await llm.reconfigure(provider="openai", model="test-model", api_key="test-only-key", base_url="https://api.openai.com/v1")
        assert llm.client.timeout.read == 60
        assert llm.client.timeout.connect == 60
    finally:
        await llm.client.aclose()


@pytest.mark.parametrize("kind", [httpx.ReadTimeout, httpx.ConnectTimeout, httpx.WriteTimeout, httpx.PoolTimeout, httpx.TimeoutException])
@pytest.mark.parametrize("message", ["", "secret-credential private-prompt https://name:password@server/?token=hidden"])
def test_timeout_description_is_actionable_without_request_secrets(kind, message):
    request = httpx.Request("POST", "https://name:password@server/?token=hidden", headers={"Authorization": "Bearer secret-credential"}, content=b"private-prompt")
    description = _describe_error(kind(message, request=request))
    assert description
    assert "timed out" in description.lower()
    assert all(secret not in description for secret in ["secret-credential", "private-prompt", "password", "hidden", "server/?"])


def test_non_timeout_diagnostic_is_preserved():
    assert _describe_error(ValueError("unsupported response format")) == "unsupported response format"
