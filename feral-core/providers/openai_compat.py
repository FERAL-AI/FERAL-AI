"""Generic adapter for providers that speak the OpenAI wire format.

Several providers FERAL can already dispatch chat against had a runtime
binding in ``agents/llm_provider.py::_PROVIDER_REGISTRY`` but no
:class:`~providers.catalog.ProviderDescriptor` and no adapter class —
so ``ProviderCatalog`` could not advertise them and the setup wizard
could not offer them at all. ``kimi`` and ``qwen`` were in that state
for their whole lifetime; ``xai`` was the mirror-image bug (catalog data
present, no runtime binding).

Rather than copy ``groq_provider.py`` six times, this module supplies
one parameterised adapter. Concrete subclasses set ``provider_id`` /
``display_name`` / ``default_base_url`` and nothing else; model lists
and pricing come from the bundled catalog through
:mod:`providers.catalog_data` and :mod:`cost.pricing` respectively, so
no subclass carries a model or price literal.

Providers whose ``/v1/models`` is not pollable (xAI, MiniMax, Z.ai —
see ``CURATED_ONLY`` in ``scripts/research_providers.py``) set
``pollable = False``; :meth:`refresh_models` then serves the curated
catalog list instead of dialling an endpoint that isn't there.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from .base import BaseProvider, ChatMessage, ChatResponse
from .catalog_data import bundled_models, provider_entry

logger = logging.getLogger("feral.providers.openai_compat")


class OpenAICompatProvider(BaseProvider):
    """Chat + model discovery over an OpenAI-compatible HTTP surface."""

    #: Fallback when the caller passes no ``base_url`` and the catalog
    #: entry carries none. Subclasses override.
    default_base_url: str = ""
    #: Whether ``GET {base_url}/models`` returns an OpenAI-shaped index.
    pollable: bool = True

    _pricing: dict[str, dict[str, float]] = {}
    _capabilities = {"tool_calling", "streaming"}

    def __init__(
        self, api_key: Optional[str] = None, base_url: Optional[str] = None
    ) -> None:
        self._api_key = api_key
        entry = provider_entry(self.provider_id)
        resolved = base_url or entry.get("base_url") or self.default_base_url
        self._base_url = str(resolved or "").rstrip("/")
        # Instance-level copy so a live refresh on one adapter can't
        # mutate the class attribute shared with every other instance.
        self._models = bundled_models(self.provider_id)

    # ------------------------------------------------------------------

    def _dialable(self) -> bool:
        """False when the base URL is still a template.

        Qwen's chat host is workspace-scoped
        (``https://[{WorkspaceId}].ap-southeast-1.maas.aliyuncs.com/...``).
        Until the operator's workspace id is substituted in there is no
        host to dial, and saying so is better than issuing a request
        against a literal ``{WorkspaceId}`` and reporting DNS failure.
        """
        return bool(self._base_url) and "{" not in self._base_url

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        tools: Optional[list[dict[str, Any]]] = None,
        **kwargs: Any,
    ) -> ChatResponse:
        if not self._api_key:
            raise RuntimeError(f"{self.provider_id} provider has no api_key configured")
        if not self._dialable():
            raise RuntimeError(
                f"{self.provider_id} base_url is unresolved ({self._base_url!r}); "
                "substitute the workspace id before dispatching"
            )
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if temperature is not None:
            payload["temperature"] = temperature
        if tools:
            payload["tools"] = tools

        async with httpx.AsyncClient(timeout=60.0) as c:
            r = await c.post(
                f"{self._base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload,
            )
            r.raise_for_status()
            data = r.json()
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        return ChatResponse(
            text=msg.get("content") or "",
            model=data.get("model", model),
            usage=data.get("usage", {}),
            finish_reason=choice.get("finish_reason", "stop"),
            tool_calls=msg.get("tool_calls") or [],
        )

    async def refresh_models(self) -> list[str]:
        if not self.pollable or not self._api_key or not self._dialable():
            return list(self._models)
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.get(
                f"{self._base_url}/models",
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            r.raise_for_status()
            data = r.json()
        ids = sorted({m["id"] for m in (data.get("data") or []) if m.get("id")})
        if ids:
            self._models = ids
        return list(self._models)


class MoonshotProvider(OpenAICompatProvider):
    """Moonshot / Kimi.

    The API host is ``api.moonshot.ai``. The DOCS host moved to
    platform.kimi.ai but the API host did NOT — pointing the client at
    ``api.kimi.ai`` does not work. Model ids use dots (``kimi-k2.7-code``).
    """

    provider_id = "moonshot"
    display_name = "Moonshot (Kimi)"
    default_base_url = "https://api.moonshot.ai/v1"


class QwenProvider(OpenAICompatProvider):
    """Alibaba Qwen. Base URL is workspace-scoped — see :meth:`_dialable`."""

    provider_id = "qwen"
    display_name = "Qwen (Alibaba)"
    default_base_url = (
        "https://[{WorkspaceId}].ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1"
    )


class XAIProvider(OpenAICompatProvider):
    """xAI / Grok. OpenAI-compatible for chat; model index is curated."""

    provider_id = "xai"
    display_name = "xAI (Grok)"
    default_base_url = "https://api.x.ai/v1"
    pollable = False


class ZaiProvider(OpenAICompatProvider):
    """Z.ai / GLM. ``glm-5.2`` is text-only (no vision)."""

    provider_id = "zai"
    display_name = "Z.ai (GLM)"
    default_base_url = "https://api.z.ai/api/paas/v4/"
    pollable = False


class MiniMaxProvider(OpenAICompatProvider):
    """MiniMax. Model id is CamelCase — ``MiniMax-M3``."""

    provider_id = "minimax"
    display_name = "MiniMax"
    default_base_url = "https://api.minimax.io/v1"
    pollable = False


class MistralProvider(OpenAICompatProvider):
    """Mistral. Model ids are date-coded (``mistral-medium-2604``)."""

    provider_id = "mistral"
    display_name = "Mistral AI"
    default_base_url = "https://api.mistral.ai/v1"
