"""Google Gemini provider adapter."""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from .base import BaseProvider, ChatMessage, ChatResponse
from .model_classes import classify

logger = logging.getLogger("feral.providers.gemini")


class GeminiProvider(BaseProvider):
    provider_id = "gemini"
    display_name = "Google Gemini"

    # Verified 2026-04-24 frontier IDs. The 2.x lineage was retired
    # March 2026 (gemini-3-pro-preview also shut down that month) and
    # /v1beta/models now lists only the 3.x preview series until the
    # GA flip. Mirrors gemini.models in
    # feral-core/providers/model_catalog.json.
    # Verified 2026-04-26 against /v1beta/models. The ``-preview``
    # suffix is retained until the 3.x pro/flash/flash-lite families
    # flip to GA; ``-thinking`` variants are research builds. The
    # bundled ``model_catalog.json`` pins the same set so the v2
    # picker's "Refresh models" button sees zero drift against the
    # bundled seed on a keyless host.
    _models = [
        "gemini-3.1-pro-preview",
        "gemini-3-flash-preview",
        "gemini-3.1-flash-lite-preview",
        "gemini-3.1-pro-thinking",
        "gemini-3.1-flash-image-preview",
        "gemini-3-pro-image-preview",
    ]
    # Pricing lives in providers/model_catalog.json — see
    # findings/13-llm-core.md fix #4.
    _pricing: dict[str, dict[str, float]] = {}
    _capabilities = {"tool_calling", "vision", "streaming", "audio_in"}

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None) -> None:
        self._api_key = api_key
        self._base_url = (base_url or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")

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
            raise RuntimeError("gemini provider has no api_key configured")

        # Gemini API splits system from the conversation.
        system_chunks = [m.content for m in messages if m.role == "system"]
        contents = []
        for m in messages:
            if m.role == "system":
                continue
            role = "user" if m.role == "user" else "model"
            contents.append({"role": role, "parts": [{"text": m.content}]})

        payload: dict[str, Any] = {"contents": contents}
        if system_chunks:
            payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_chunks)}]}
        gen_cfg: dict[str, Any] = {}
        if max_tokens is not None:
            gen_cfg["maxOutputTokens"] = max_tokens
        if temperature is not None:
            gen_cfg["temperature"] = temperature
        # Thinking-variant fork: ``-thinking`` models accept a
        # ``thinkingConfig`` block inside ``generationConfig``. Non-
        # thinking Gemini 3.x models reject the block with a 400, so
        # only send it when the classifier says reasoning.
        if classify("gemini", model) == "reasoning":
            thinking_cfg = gen_cfg.setdefault("thinkingConfig", {})
            thinking_cfg.setdefault("enabled", True)
            budget = kwargs.get("thinking_budget")
            if budget:
                thinking_cfg["thinkingBudget"] = int(budget)
        if gen_cfg:
            payload["generationConfig"] = gen_cfg
        if tools:
            payload["tools"] = tools

        url = f"{self._base_url}/models/{model}:generateContent?key={self._api_key}"
        async with httpx.AsyncClient(timeout=60.0) as c:
            r = await c.post(url, json=payload)
            r.raise_for_status()
            data = r.json()

        candidates = data.get("candidates", [])
        text = ""
        if candidates:
            parts = candidates[0].get("content", {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts)
        usage = data.get("usageMetadata", {})
        return ChatResponse(
            text=text,
            model=model,
            usage={
                "input_tokens": usage.get("promptTokenCount", 0),
                "output_tokens": usage.get("candidatesTokenCount", 0),
                "total_tokens": usage.get("totalTokenCount", 0),
            },
            finish_reason=candidates[0].get("finishReason", "STOP") if candidates else "STOP",
        )

    async def refresh_models(self) -> list[str]:
        if not self._api_key:
            return list(self._models)
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.get(f"{self._base_url}/models?key={self._api_key}")
            r.raise_for_status()
        ids = [m["name"].split("/")[-1] for m in r.json().get("models", [])]
        if ids:
            self._models = sorted(set(ids))
        return list(self._models)
