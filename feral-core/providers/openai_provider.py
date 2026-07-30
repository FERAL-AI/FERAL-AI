"""OpenAI provider adapter.

Thin wrapper that exposes the existing OpenAI client used elsewhere in
the codebase behind the pluggable :class:`Provider` Protocol. The
real inference code in ``agents/`` and ``voice/`` continues to use the
raw OpenAI SDK; this adapter is for the capability / pricing / model-
listing surface the orchestrator uses to pick models.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from .base import BaseProvider, ChatMessage, ChatResponse
from .catalog_data import bundled_models
from .model_classes import classify

logger = logging.getLogger("feral.providers.openai")


# Reasoning-family params the /v1/chat/completions endpoint rejects with
# 400 when sent against a reasoning model (gpt-5, gpt-5.4*, gpt-5.5*,
# o1 / o3 / o4). Source: the OpenAI reasoning guide + the live 400s the
# maintainer reported in v2026.5.0. Anything here is removed from the
# outbound body when ``classify(provider, model) == "reasoning"``; the
# replaced cousin for ``max_tokens`` is ``max_completion_tokens`` and
# is added explicitly below.
_REASONING_STRIP_PARAMS = frozenset(
    {"max_tokens", "top_p", "presence_penalty", "frequency_penalty"}
)


def _apply_reasoning_fork(model: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Mutate ``payload`` in-place to match the reasoning-family contract.

    Called by :class:`OpenAIProvider.chat` and imported by
    ``llm_provider.py`` so the dispatch path converges on the same fork.
    Legacy (non-reasoning) models are left untouched, preserving the
    existing ``max_tokens`` / ``temperature`` / ``top_p`` semantics.
    """
    if classify("openai", model) != "reasoning":
        return payload
    max_tokens = payload.pop("max_tokens", None)
    if max_tokens is not None and "max_completion_tokens" not in payload:
        payload["max_completion_tokens"] = max_tokens
    # Temperature on reasoning models must be 1 (or absent). Strip any
    # other value the caller supplied. The default server-side is 1 so
    # silent drop is correct here.
    temp = payload.get("temperature")
    if temp is not None and temp != 1 and temp != 1.0:
        payload.pop("temperature", None)
    for key in _REASONING_STRIP_PARAMS:
        payload.pop(key, None)
    payload.setdefault("reasoning_effort", "medium")
    return payload


class OpenAIProvider(BaseProvider):
    provider_id = "openai"
    display_name = "OpenAI"

    # Bundled fallback model list, read from
    # ``providers/model_catalog.json`` rather than hardcoded here.
    # A literal list goes stale the moment the provider ships a new
    # frontier name (roadmap §3.5 P0) — and because
    # ``ProviderCatalog.default_model_for`` falls back to this list when
    # no live refresh has run, a stale literal here silently became the
    # provider's DEFAULT model. Instances replace it with the live
    # ``/v1/models`` response in :meth:`refresh_models`.
    _models = bundled_models("openai")
    # Pricing lives in providers/model_catalog.json (single source of
    # truth).  this adapter shipped its own divergent literals
    # — gpt-5.5 was 0.006/0.018 here vs 0.005/0.030 in the catalog,
    # which broke budget routing whenever the catalog won. See
    # findings/13-llm-core.md fix #4.
    _pricing: dict[str, dict[str, float]] = {}
    _capabilities = {"tool_calling", "json_mode", "vision", "streaming", "audio_in", "audio_out"}

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None) -> None:
        self._api_key = api_key
        self._base_url = (base_url or "https://api.openai.com/v1").rstrip("/")

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
            raise RuntimeError("openai provider has no api_key configured")
        payload: dict[str, Any] = {
            "model": model,
            "messages": [_msg_to_openai(m) for m in messages],
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if temperature is not None:
            payload["temperature"] = temperature
        if tools:
            payload["tools"] = tools

        # Fork: reasoning models (gpt-5, gpt-5.4*, gpt-5.5*, o1/o3/o4)
        # reject ``max_tokens`` + arbitrary ``temperature`` on
        # ``/v1/chat/completions`` with a 400. The helper rewrites the
        # payload in-place so non-reasoning models (gpt-4o, gpt-4.1)
        # keep the legacy parameter shape.
        _apply_reasoning_fork(model, payload)
        # The orchestrator can bump the reasoning effort to ``"high"``
        # (or ``"max"`` for subagent workloads) by passing
        # ``reasoning_effort=...`` through kwargs; the fork's default
        # is ``"medium"`` per the OpenAI reasoning guide.
        if "reasoning_effort" in kwargs and kwargs["reasoning_effort"]:
            payload["reasoning_effort"] = kwargs["reasoning_effort"]

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
            text=msg.get("content") or "",
            model=data.get("model", model),
            usage=data.get("usage", {}),
            finish_reason=choice.get("finish_reason", "stop"),
            tool_calls=msg.get("tool_calls") or [],
        )

    async def refresh_models(self) -> list[str]:
        if not self._api_key:
            return list(self._models)
        # Let HTTP errors propagate to ProviderCatalog so the v2 picker
        # can surface "key rejected" instead of silently rendering the
        # hardcoded fallback list. Other failure modes (DNS, timeout)
        # are treated the same — the catalog turns them into a warning.
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.get(
                f"{self._base_url}/models",
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            r.raise_for_status()
        ids = [m["id"] for m in r.json().get("data", [])]
        if ids:
            # Store the FULL raw list. The chat-only filter runs inside
            # :meth:`BaseProvider.list_models` via the classifier —
            # filtering here would silently hide embedding / whisper /
            # dall-e models from callers that legitimately want them
            # (feral-voice reads the audio class; feral-memory reads
            # the embedding class).
            self._models = sorted(ids)
        return list(self._models)


def _msg_to_openai(m: ChatMessage) -> dict[str, Any]:
    out: dict[str, Any] = {"role": m.role, "content": m.content}
    if m.name:
        out["name"] = m.name
    if m.tool_calls:
        out["tool_calls"] = m.tool_calls
    return out
