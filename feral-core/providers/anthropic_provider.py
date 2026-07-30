"""Anthropic provider adapter.

Calls the public ``/v1/messages`` API. Anthropic now also publishes
``/v1/models`` (paginated) — we use it when an API key is configured
and fall back to the hand-curated catalog in
``providers/model_catalog.json`` when no key is present (the picker
still renders something on a first-run host).

Extended-thinking handling
--------------------------
The Anthropic models endpoint returns a ``capabilities`` object per
model. Claude 4.7 and later use *adaptive* thinking (no explicit
``thinking`` block — the model decides) while Sonnet 4.6 / Haiku 4.5 and
earlier support *enabled* thinking with an explicit ``budget_tokens``
knob. Sending ``thinking={"type":"enabled"}`` to an adaptive model is a
400, and so is sending ``temperature`` / ``top_p`` / ``top_k`` to one.

Neither split is hardcoded here any more. Both are read from the
catalog's ``capabilities`` block via :mod:`providers.catalog_data`,
which ``scripts/research_providers.py`` refreshes from the live
endpoint. The old static ``frozenset({"claude-opus-4-7"})`` would have
silently 400'd every Claude 5 call carrying a temperature the moment the
Claude 5 ids were added to the model list.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from .base import BaseProvider, ChatMessage, ChatResponse
from .catalog_data import bundled_models, capability, models_with_capability
from .model_classes import classify

logger = logging.getLogger("feral.providers.anthropic")


# The tip of the supported anthropic-version header. 2023-06-01 still
# works for plain /v1/messages but rejects newer beta features; newer
# values unlock the richer capabilities surface (tool_use extensions,
# adaptive thinking, 1M context). Keep in sync with the upstream docs.
_ANTHROPIC_VERSION = "2023-06-01"


def _default_budget_tokens(model: str) -> Optional[int]:
    """Return the adapter's default thinking budget for *model*.

    Opus → 32k (most of a 200-400k deep-reasoning window); Sonnet → 16k;
    Haiku → off (returns ``None`` to mean "don't send the thinking
    block"). These defaults are what the reasoning-models doc suggests
    as sensible starting points; callers pass ``thinking_budget=`` to
    override.
    """
    low = model.lower()
    if "opus" in low:
        return 32_000
    if "sonnet" in low:
        return 16_000
    return None


class AnthropicProvider(BaseProvider):
    provider_id = "anthropic"
    display_name = "Anthropic"

    # Bundled fallback model list. NOT a literal — read from
    # ``providers/model_catalog.json`` so a catalog refresh (daily
    # workflow, or a hot edit) is the only thing needed to surface a new
    # Claude id in the v2 picker. Instances replace this with the live
    # ``/v1/models`` response in :meth:`refresh_models` when a key is
    # present.
    _models = bundled_models("anthropic")
    # Pricing lives in providers/model_catalog.json — see
    # findings/13-llm-core.md fix #4.
    _pricing: dict[str, dict[str, float]] = {}
    _capabilities = {"tool_calling", "vision", "streaming", "thinking"}

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None) -> None:
        self._api_key = api_key
        self._base_url = (base_url or "https://api.anthropic.com/v1").rstrip("/")
        # Populated by :meth:`refresh_models` with the per-model
        # capability flags from the live /v1/models response. Shape:
        # ``{model_id: {"thinking_enabled": bool, "thinking_adaptive": bool}}``.
        # Falls back to the static overlay above when empty.
        self._thinking_caps: dict[str, dict[str, bool]] = {}

    # ------------------------------------------------------------------
    # Capability lookups
    # ------------------------------------------------------------------
    #
    # Resolution order for every lookup below:
    #   1. ``self._thinking_caps`` — flags parsed from the live
    #      ``/v1/models`` response by :meth:`refresh_models`.
    #   2. the bundled catalog's ``capabilities`` block (refreshed daily
    #      from the same endpoint by scripts/research_providers.py).
    #   3. a conservative default for ids neither source knows.
    #
    # Step 2 replaces the two hand-maintained frozensets this class used
    # to carry. See the module docstring.

    @staticmethod
    def adaptive_thinking_models() -> frozenset[str]:
        """Catalog-derived set of adaptive-thinking Claude ids."""
        return models_with_capability("anthropic", "thinking", "adaptive")

    @staticmethod
    def extended_thinking_models() -> frozenset[str]:
        """Catalog-derived set of ``budget_tokens``-accepting Claude ids."""
        return models_with_capability("anthropic", "thinking", "enabled")

    def supports_extended_thinking(self, model: str) -> bool:
        live = self._thinking_caps.get(model, {})
        if "thinking_enabled" in live:
            return bool(live["thinking_enabled"])
        return model in self.extended_thinking_models()

    def supports_adaptive_thinking(self, model: str) -> bool:
        live = self._thinking_caps.get(model, {})
        if "thinking_adaptive" in live:
            return bool(live["thinking_adaptive"])
        return model in self.adaptive_thinking_models()

    def supports_sampling_params(self, model: str) -> bool:
        """Whether *model* accepts ``temperature`` / ``top_p`` / ``top_k``.

        These were removed on Claude 4.7 and every later model: sending
        any of them returns HTTP 400. Unknown ids default to ``True`` so
        a model released after the last catalog refresh keeps the
        historical behaviour rather than silently losing a caller's
        temperature.
        """
        explicit = capability("anthropic", model, "sampling_params")
        if explicit is not None:
            return bool(explicit)
        # No explicit record: adaptive thinking is the observable proxy
        # — the two changed together on 4.7 and have tracked since.
        return not self.supports_adaptive_thinking(model)

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str,
        max_tokens: Optional[int] = 4096,
        temperature: Optional[float] = None,
        tools: Optional[list[dict[str, Any]]] = None,
        **kwargs: Any,
    ) -> ChatResponse:
        if not self._api_key:
            raise RuntimeError("anthropic provider has no api_key configured")

        system_chunks: list[str] = []
        turns: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "system":
                system_chunks.append(m.content)
                continue
            turns.append({"role": m.role, "content": m.content})

        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens or 4096,
            "messages": turns,
        }
        if system_chunks:
            payload["system"] = "\n\n".join(system_chunks)
        if tools:
            payload["tools"] = tools

        # Fork: thinking-capable models accept / require a specific
        # ``thinking`` shape. Callers opt in via ``reasoning=True`` or
        # by selecting a thinking-capable model.
        want_reasoning = (
            kwargs.get("reasoning") is True
            or classify("anthropic", model) == "reasoning"
        )
        thinking_budget = kwargs.get("thinking_budget")
        if want_reasoning and self.supports_extended_thinking(model):
            budget = thinking_budget or _default_budget_tokens(model)
            if budget:
                payload["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": int(budget),
                }
                # Anthropic invariant: max_tokens must be strictly
                # greater than thinking.budget_tokens, otherwise the
                # API returns 400 ("`max_tokens` must be greater than
                # `thinking.budget_tokens`"). Callers that passed a
                # tiny max_tokens (e.g. smoke tests passing 20) would
                # crash here. Bump max_tokens to leave at least
                # _RESPONSE_ROOM_TOKENS for the post-thinking response.
                _RESPONSE_ROOM_TOKENS = 1024
                required = int(budget) + _RESPONSE_ROOM_TOKENS
                existing = payload.get("max_tokens") or 0
                if existing < required:
                    payload["max_tokens"] = required
                # Temperature on extended-thinking messages must be
                # either 1 or omitted; sending a different value is
                # a 400. Drop the caller-supplied value silently.
                temperature = None
        elif want_reasoning and self.supports_adaptive_thinking(model):
            # Adaptive-thinking models choose their own depth; pass no
            # thinking block.
            temperature = None

        # Sampling params were REMOVED (not deprecated) on Claude 4.7 and
        # every later model — temperature / top_p / top_k each return a
        # 400. This guard is deliberately outside the reasoning fork
        # above: that fork only fires when the caller asked for reasoning
        # or the classifier tagged the model as reasoning, whereas the
        # 400 fires on *any* request carrying the param. A plain chat
        # turn on claude-opus-5 with a temperature has to be caught here.
        if temperature is not None and not self.supports_sampling_params(model):
            logger.debug(
                "dropping temperature for %s: model rejects sampling params", model
            )
            temperature = None

        if temperature is not None:
            payload["temperature"] = temperature

        async with httpx.AsyncClient(timeout=60.0) as c:
            r = await c.post(
                f"{self._base_url}/messages",
                headers={
                    "x-api-key": self._api_key,
                    "anthropic-version": _ANTHROPIC_VERSION,
                    "content-type": "application/json",
                },
                json=payload,
            )
            r.raise_for_status()
            data = r.json()

        text_blocks = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
        tool_blocks = [b for b in data.get("content", []) if b.get("type") == "tool_use"]
        usage = data.get("usage", {})
        return ChatResponse(
            text="".join(text_blocks),
            model=data.get("model", model),
            usage={
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
            },
            finish_reason=data.get("stop_reason", "end_turn"),
            tool_calls=tool_blocks,
        )

    async def refresh_models(self) -> list[str]:
        """Fetch live Anthropic models via the models-list API.

        Anthropic added ``/v1/models`` with a ``capabilities`` object
        per model id (documented 2025-08, refined 2026). When an API
        key is present we pull the full paginated list and update
        ``self._thinking_caps`` so the chat fork picks the right
        thinking shape per id. Without a key we fall back to the
        bundled list — no network call happens on a dry-run host.
        """
        if not self._api_key:
            return list(self._models)
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
        }
        all_ids: list[str] = []
        caps: dict[str, dict[str, bool]] = {}
        cursor: Optional[str] = None
        async with httpx.AsyncClient(timeout=30.0) as c:
            # Paginated. ``has_more`` tells us when to stop.
            for _ in range(20):  # safety cap: 20 pages of 100 = 2000 models
                params: dict[str, Any] = {"limit": 100}
                if cursor:
                    params["after_id"] = cursor
                r = await c.get(
                    f"{self._base_url}/models", headers=headers, params=params
                )
                r.raise_for_status()
                body = r.json()
                for entry in body.get("data", []) or []:
                    mid = entry.get("id")
                    if not mid:
                        continue
                    all_ids.append(mid)
                    # Capability flags may appear either as flat booleans
                    # or as {"supported": bool} objects. Accept both.
                    capsmap = entry.get("capabilities") or {}
                    thinking = capsmap.get("thinking") or {}
                    types = thinking.get("types") or {}
                    enabled = types.get("enabled") or {}
                    adaptive = types.get("adaptive") or {}
                    caps[mid] = {
                        "thinking_enabled": bool(
                            enabled.get("supported") if isinstance(enabled, dict)
                            else enabled
                        ),
                        "thinking_adaptive": bool(
                            adaptive.get("supported") if isinstance(adaptive, dict)
                            else adaptive
                        ),
                    }
                if not body.get("has_more"):
                    break
                cursor = body.get("last_id")
                if not cursor:
                    break
        if all_ids:
            self._models = list(dict.fromkeys(all_ids))  # preserve order
            self._thinking_caps = caps
        return list(self._models)
