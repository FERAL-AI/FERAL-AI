"""``OllamaProvider.refresh_models`` returns exactly what ``/api/tags``
reports — including the empty list.

Pre-fix the adapter kept a hardcoded fallback (``["llama3.3",
"qwen2.5", "deepseek-r1", "mistral"]``) and only overwrote it when
``/api/tags`` returned at least one entry. That caused the v2 picker
to advertise models the operator had never pulled (or had since
removed) whenever Ollama itself was reachable but empty.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from providers.ollama_provider import OllamaProvider


def _mock_client(payload: dict):
    resp = MagicMock()
    resp.json.return_value = payload
    resp.raise_for_status = MagicMock()
    client = MagicMock()
    client.get = AsyncMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


@pytest.mark.asyncio
async def test_refresh_models_returns_tags_list():
    client = _mock_client(
        {"models": [{"name": "llama3.1:8b"}, {"name": "mistral:7b"}]}
    )
    with patch("providers.ollama_provider.httpx.AsyncClient", return_value=client):
        p = OllamaProvider()
        out = await p.refresh_models()
    assert sorted(out) == ["llama3.1:8b", "mistral:7b"]


@pytest.mark.asyncio
async def test_refresh_models_returns_empty_when_no_models_pulled():
    """Empty ``/api/tags`` MUST yield an empty list — not a stale
    hardcoded fallback. The picker then truthfully shows "no models
    installed" instead of offering names the operator can't actually
    use."""
    client = _mock_client({"models": []})
    with patch("providers.ollama_provider.httpx.AsyncClient", return_value=client):
        p = OllamaProvider()
        out = await p.refresh_models()
    assert out == []
    # And ``list_models`` (the sync cache) reflects the truth too.
    assert p.list_models() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("models", [
    [{"name": ""}, {"name": "llama3.1"}, {}],
    [{"name": "llama3.1"}, {}],
    [{}],
])
async def test_refresh_models_refuses_malformed_inventory_without_replacing_prior_ids(models):
    """A partial or malformed read cannot certify removals or new model IDs."""
    client = _mock_client({"models": [{"name": "installed-before:7b"}]})
    with patch("providers.ollama_provider.httpx.AsyncClient", return_value=client):
        p = OllamaProvider()
        assert await p.refresh_models() == ["installed-before:7b"]
        client.get.return_value.json.return_value = {"models": models}
        with pytest.raises(ValueError, match="invalid model inventory entry"):
            await p.refresh_models()
    assert p.list_models() == ["installed-before:7b"]
    assert client.get.await_count == 2


def test_default_models_are_empty():
    """The class-level fallback list MUST be empty — otherwise an
    unreachable Ollama on first-boot would advertise hardcoded names
    via the picker's "fallback" branch."""
    p = OllamaProvider()
    assert p.list_models() == []
