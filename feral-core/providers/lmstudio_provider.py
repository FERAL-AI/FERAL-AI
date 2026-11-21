"""LM Studio provider adapter.

LM Studio serves an OpenAI-compatible REST API at
``http://localhost:1234/v1`` by default. The catalog treats it as a
local-first provider:

* ``supports_local = True`` — no cloud dependency, no API key.
* ``refresh_models`` hits ``GET /v1/models`` so the catalog surfaces
  exactly the models the user has loaded in LM Studio's UI.
* ``chat`` posts to ``POST /v1/chat/completions`` with the same shape
  the OpenAI adapter uses.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from .base import BaseProvider, ChatMessage, ChatResponse

logger = logging.getLogger("feral.providers.lmstudio")


class LMStudioProvider(BaseProvider):
    provider_id = "lmstudio"
    display_name = "LM Studio (local)"

    # Populated from `/v1/models` at first refresh. Seeded empty
    # because LM Studio doesn't ship with any model until the user
    # downloads one inside its UI — we refuse to list fake defaults.
    _models: list[str] = []
    _pricing: dict[str, dict[str, float]] = {}
    _capabilities = {"streaming", "tool_calling"}

    def __init__(self, base_url: Optional[str] = None) -> None:
        self._base_url = (base_url or "http://localhost:1234/v1").rstrip("/")

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

        async with httpx.AsyncClient(timeout=120.0) as c:
            r = await c.post(f"{self._base_url}/chat/completions", json=payload)
            r.raise_for_status()
            data = r.json()
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message", {})
        usage = data.get("usage", {}) or {}
        return ChatResponse(
            text=msg.get("content", "") or "",
            model=data.get("model", model),
            usage={
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
            finish_reason=choice.get("finish_reason", "stop"),
            tool_calls=msg.get("tool_calls") or [],
        )

    async def refresh_models(self) -> list[str]:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(f"{self._base_url}/models")
            r.raise_for_status()
        payload = r.json()
        if not isinstance(payload, dict) or payload.get("error") is not None or not isinstance(payload.get("data"), list):
            raise ValueError("LM Studio returned an invalid model inventory")
        ids: list[str] = []
        for item in payload["data"]:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"].strip():
                raise ValueError("LM Studio returned an invalid model inventory entry")
            ids.append(item["id"])
        # A valid empty response is authoritative. Failed parsing above
        # must leave the previous inventory intact instead of clearing it.
        self._models = sorted(ids)
        return list(self._models)
