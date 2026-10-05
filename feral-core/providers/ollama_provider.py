"""Ollama provider adapter — talks to a local Ollama server."""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from .base import BaseProvider, ChatMessage, ChatResponse
from agents.context_manager import verify_ollama_request_context

logger = logging.getLogger("feral.providers.ollama")


class OllamaProvider(BaseProvider):
    provider_id = "ollama"
    display_name = "Ollama (local)"

    # No static model list. The local Ollama server is the only honest
    # source of truth for "what's installed", so the picker should never
    # advertise names the operator has not pulled. The previous
    # hardcoded fallback (``["llama3.3", "qwen2.5", "deepseek-r1",
    # "mistral"]``) caused the picker to show models the user could not
    # actually run; selecting one then 404'd at first chat. ``refresh_models``
    # below replaces this list with whatever ``/api/tags`` reports — empty
    # included.
    _models: list[str] = []
    _pricing: dict[str, dict[str, float]] = {}
    _capabilities = {"streaming", "tool_calling"}

    def __init__(self, base_url: Optional[str] = None) -> None:
        self._base_url = self.native_base_url(base_url)

    @staticmethod
    def native_base_url(base_url: Optional[str] = None) -> str:
        """Translate only the terminal OpenAI API suffix, retaining proxy paths."""
        root = (base_url or "http://localhost:11434").rstrip("/")
        return root[:-3] if root.endswith("/v1") else root

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
            "stream": False,
        }
        # An omitted output limit must not defeat the reserved-output check.
        output_limit = max_tokens if max_tokens is not None else 1024
        options: dict[str, Any] = {"num_predict": output_limit}
        if temperature is not None:
            options["temperature"] = temperature
        payload["options"] = options
        if tools:
            payload["tools"] = tools

        async with httpx.AsyncClient(base_url=self._base_url, timeout=120.0) as c:
            await verify_ollama_request_context(c, {
                "model": model, "messages": payload["messages"],
                "tools": tools or [], "max_tokens": output_limit,
            })
            r = await c.post(f"{self._base_url}/api/chat", json=payload)
            r.raise_for_status()
            data = r.json()
        msg = data.get("message", {})
        return ChatResponse(
            text=msg.get("content", ""),
            model=data.get("model", model),
            usage={
                "input_tokens": data.get("prompt_eval_count", 0),
                "output_tokens": data.get("eval_count", 0),
                "total_tokens": data.get("prompt_eval_count", 0) + data.get("eval_count", 0),
            },
            finish_reason="stop",
            tool_calls=msg.get("tool_calls", []),
        )

    async def refresh_models(self) -> list[str]:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(f"{self._base_url}/api/tags")
            r.raise_for_status()
        ids = [m["name"] for m in r.json().get("models", []) if m.get("name")]
        # Trust /api/tags as the source of truth — including the empty
        # case. The previous behaviour preserved a stale ``self._models``
        # list when the server returned no models, so the picker kept
        # advertising names the operator had since removed (or never
        # pulled). When Ollama itself is unreachable we raise above and
        # the catalog keeps its previous cached / fallback list.
        self._models = sorted(ids)
        return list(self._models)
