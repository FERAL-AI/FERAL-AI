"""OpenRouter provider adapter (OpenAI-compatible /v1/chat/completions).

OpenRouter is a router: it forwards a request to whichever vendor owns
the ``<vendor>/<model>`` slug. Capability questions are therefore
per-route, not per-provider. The v2026.5.0 terminal log showed the
adapter early-returning ``Provider 'openrouter' does not support
vision input`` for every image call, even when the routed target was
a vision-capable model — that's the bug this adapter fixes.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from .base import BaseProvider, ChatMessage, ChatResponse
from .catalog_data import bundled_models

logger = logging.getLogger("feral.providers.openrouter")


# A conservative allowlist of modality strings the /api/v1/models
# endpoint returns. New values may appear over time; we treat any
# string containing ``"image"`` as vision-capable so a future
# ``"text+image+audio"`` still classifies correctly.
_VISION_MODALITY_TOKENS = ("image", "vision", "multimodal")
_AUDIO_MODALITY_TOKENS = ("audio", "speech")


class OpenRouterProvider(BaseProvider):
    provider_id = "openrouter"
    display_name = "OpenRouter"

    # Bundled fallback model list, read from
    # ``providers/model_catalog.json`` rather than hardcoded here.
    # A literal list goes stale the moment the provider ships a new
    # frontier name (roadmap §3.5 P0) — and because
    # ``ProviderCatalog.default_model_for`` falls back to this list when
    # no live refresh has run, a stale literal here silently became the
    # provider's DEFAULT model. Instances replace it with the live
    # ``/v1/models`` response in :meth:`refresh_models`.
    _models = bundled_models("openrouter")
    _pricing: dict[str, dict[str, float]] = {}
    # Vision is in the superset because OpenRouter routes vision
    # requests to vision-capable downstreams. Per-model narrowing runs
    # through :meth:`_capabilities_for_model` which consults the live
    # router snapshot. See the  proposal §4 for the full rationale.
    _capabilities = {"tool_calling", "streaming", "vision", "thinking", "json_mode"}

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        referer: str = "https://feral.ai",
        title: str = "FERAL",
    ) -> None:
        self._api_key = api_key
        self._base_url = (base_url or "https://openrouter.ai/api/v1").rstrip("/")
        # OpenRouter asks integrators to send these for ranking + abuse control.
        self._referer = referer
        self._title = title
        # Populated by :meth:`refresh_models` with the live per-route
        # capability bag: ``{slug: {"vision": bool, "audio": bool,
        # "reasoning": bool, "tool_calling": bool}}``. Empty on a
        # first-boot host that hasn't refreshed yet — the superset
        # ``_capabilities`` answer is used instead.
        self._model_caps: dict[str, dict[str, bool]] = {}

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "HTTP-Referer": self._referer,
            "X-Title": self._title,
        }
        return headers

    def _capabilities_for_model(self, model_id: str) -> set[str]:
        """Per-route capabilities for *model_id*.

        Returns the router-level superset when no live snapshot is
        cached for this slug (first boot, or a freshly-listed slug
        that predates the most recent refresh). When a live snapshot
        is present, the return is the narrowed intersection — a
        text-only route drops ``"vision"`` from the answer so the
        orchestrator can early-return a helpful "this route can't see
        images, try ``anthropic/claude-opus-4-7``" error instead of
        a confusing upstream 400.
        """
        live = self._model_caps.get(model_id)
        if live is None:
            return set(self._capabilities)
        caps: set[str] = set()
        if live.get("tool_calling"):
            caps.add("tool_calling")
        if live.get("streaming", True):
            caps.add("streaming")
        if live.get("thinking"):
            caps.add("thinking")
        if live.get("vision"):
            caps.add("vision")
        if live.get("audio_in"):
            caps.add("audio_in")
        if live.get("audio_out"):
            caps.add("audio_out")
        if live.get("json_mode"):
            caps.add("json_mode")
        return caps

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
            raise RuntimeError("openrouter provider has no api_key configured")
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
                headers=self._headers(),
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
        async with httpx.AsyncClient(timeout=30.0) as c:
            # OpenRouter's /models endpoint is public — keyless refresh
            # works, so the catalog stays up to date even without
            # credentials. Each entry carries ``architecture.modality``
            # and ``supported_parameters`` which we unpack into the
            # per-model capability bag.
            r = await c.get(f"{self._base_url}/models")
            r.raise_for_status()
        data = r.json().get("data", []) or []
        ids: list[str] = []
        caps: dict[str, dict[str, bool]] = {}
        for entry in data:
            if not isinstance(entry, dict):
                continue
            mid = entry.get("id")
            if not mid:
                continue
            ids.append(mid)
            caps[mid] = _extract_capabilities(entry)
        if ids:
            self._models = sorted(ids)
            self._model_caps = caps
        return list(self._models)


def _extract_capabilities(entry: dict[str, Any]) -> dict[str, bool]:
    """Parse an OR ``/api/v1/models`` entry into a capability bag.

    The router's schema has evolved: older entries carry
    ``architecture.modality`` as ``"text"`` / ``"text+image"``; newer
    entries add ``input_modalities`` + ``output_modalities`` arrays and
    a ``supported_parameters`` array. This helper accepts both shapes
    so a mid-version catalog refresh doesn't silently lose information.
    """
    arch = entry.get("architecture") or {}
    modality = str(arch.get("modality") or "").lower()
    in_mods = [str(m).lower() for m in arch.get("input_modalities") or []]
    out_mods = [str(m).lower() for m in arch.get("output_modalities") or []]
    supported_params = [
        str(p).lower() for p in entry.get("supported_parameters") or []
    ]
    modality_blob = " ".join([modality, *in_mods, *out_mods])
    has_vision = any(tok in modality_blob for tok in _VISION_MODALITY_TOKENS)
    has_audio_in = any(tok in " ".join(in_mods) for tok in _AUDIO_MODALITY_TOKENS)
    has_audio_out = any(tok in " ".join(out_mods) for tok in _AUDIO_MODALITY_TOKENS)
    return {
        "vision": has_vision,
        "audio_in": has_audio_in,
        "audio_out": has_audio_out,
        "tool_calling": "tools" in supported_params or "tool_choice" in supported_params,
        "json_mode": "response_format" in supported_params,
        "thinking": "reasoning" in supported_params
        or "reasoning_effort" in supported_params,
        "streaming": True,
    }
