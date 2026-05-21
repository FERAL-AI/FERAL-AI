"""Groq provider adapter (OpenAI-compatible /v1/chat/completions)."""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from .base import BaseProvider, ChatMessage, ChatResponse
from .model_classes import classify

logger = logging.getLogger("feral.providers.groq")


# Groq hosts OpenAI-compatible reasoning models (deepseek-r1 distills,
# qwen-qwq series). When the backend is reasoning, the same param fork
# the OpenAI adapter applies is needed: ``max_tokens`` must become
# ``max_completion_tokens`` and ``temperature != 1`` must be stripped.
# Keep this list in sync with ``providers/openai_provider.py``.
_REASONING_STRIP_PARAMS = frozenset(
    {"max_tokens", "top_p", "presence_penalty", "frequency_penalty"}
)


def _apply_reasoning_fork(model: str, payload: dict[str, object]) -> dict[str, object]:
    if classify("groq", model) != "reasoning":
        return payload
    max_tokens = payload.pop("max_tokens", None)
    if max_tokens is not None and "max_completion_tokens" not in payload:
        payload["max_completion_tokens"] = max_tokens
    temp = payload.get("temperature")
    if temp is not None and temp != 1 and temp != 1.0:
        payload.pop("temperature", None)
    for key in _REASONING_STRIP_PARAMS:
        payload.pop(key, None)
    payload.setdefault("reasoning_effort", "medium")
    return payload


class GroqProvider(BaseProvider):
    provider_id = "groq"
    display_name = "Groq"

    _models = [
        "llama-3.3-70b-versatile",
        "llama-3.1-8b-instant",
        "mixtral-8x7b-32768",
        "gemma2-9b-it",
    ]
    # Pricing lives in providers/model_catalog.json — see
    # findings/13-llm-core.md fix #4.
    _pricing: dict[str, dict[str, float]] = {}
    _capabilities = {"tool_calling", "streaming"}

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None) -> None:
        self._api_key = api_key
        self._base_url = (base_url or "https://api.groq.com/openai/v1").rstrip("/")

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
            raise RuntimeError("groq provider has no api_key configured")
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

        _apply_reasoning_fork(model, payload)

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
        ids = [m["id"] for m in r.json().get("data", [])]
        if ids:
            self._models = sorted(ids)
        return list(self._models)
