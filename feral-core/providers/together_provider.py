"""Together.ai provider adapter (OpenAI-compatible /v1/chat/completions).

Track A stub: the shape is production-ready (Together's API mirrors
OpenAI closely), but we don't ship live tests yet. The follow-up PR
will add a `[provider-together]`-equivalent extra (bare-name
``together`` per the repo convention) and wire the live
``/v1/models`` fetch into the model catalog.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from .base import BaseProvider, ChatMessage, ChatResponse

logger = logging.getLogger("feral.providers.together")


class TogetherProvider(BaseProvider):
    provider_id = "together"
    display_name = "Together AI"

    # Hand-curated snapshot; refresh_models() fetches the live catalog
    # when an API key is present.
    _models = [
        "meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo",
        "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo",
        "Qwen/Qwen2.5-72B-Instruct-Turbo",
        "mistralai/Mixtral-8x22B-Instruct-v0.1",
    ]
    # Together is catalog-only at runtime; pricing lives in
    # model_catalog.json. Pre-W2 these adapter literals were marked
    # "indicative" and never reconciled with together.ai/pricing.
    _pricing: dict[str, dict[str, float]] = {}
    _capabilities = {"tool_calling", "streaming"}

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None) -> None:
        self._api_key = api_key
        self._base_url = (base_url or "https://api.together.xyz/v1").rstrip("/")

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
            raise RuntimeError("together provider has no api_key configured")
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
        choice = data["choices"][0]
        msg = choice["message"]
        return ChatResponse(
            text=msg.get("content", ""),
            model=data.get("model", model),
            usage=data.get("usage", {}),
            finish_reason=choice.get("finish_reason", "stop"),
            tool_calls=msg.get("tool_calls") or [],
        )

    async def refresh_models(self) -> list[str]:
        if not self._api_key:
            return list(self._models)
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.get(
                f"{self._base_url}/models",
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            r.raise_for_status()
        payload = r.json()
        ids = [
            m["id"]
            for m in (payload.get("data", payload) if isinstance(payload, dict) else payload)
            if isinstance(m, dict) and "id" in m
        ]
        if ids:
            self._models = sorted(ids)
        return list(self._models)
