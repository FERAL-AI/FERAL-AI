"""Canonical model pricing loaded from providers/model_catalog.json.

This module is the **single source of truth** for $/1k token rates. All
provider adapters read pricing through :func:`get_shared_pricing` (or
the convenience :func:`pricing_per_1k`) — there are no adapter-local
``_pricing = {...}`` backstops. Pre-W2 every adapter shipped its own
literal, which drifted out of sync with the catalog the moment a
provider published a new rate (the OpenAI ``gpt-5.5`` divergence
documented in findings/13-llm-core.md was caused by exactly that).

Catalog reconciliation is part of the build: each entry in
``model_catalog.json`` either carries a verified rate or is marked
``__needs_verification__`` so a follow-up PR can either confirm or
flag the divergence. See findings/13-llm-core.md fix #4 + the
"pricing reconciliation" table in the W2 LLM router PR body.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger("feral.cost.pricing")

_CATALOG_PATH = Path(__file__).resolve().parents[1] / "providers" / "model_catalog.json"
_FALLBACK_PER_1K = {"input": 0.005, "output": 0.025}


def _normalize_model_id(model: str) -> str:
    slug = model.strip().lower()
    if "/" in slug:
        slug = slug.rsplit("/", 1)[-1]
    slug = slug.replace(".", "-")
    slug = re.sub(r"-{2,}", "-", slug)
    return slug


def _merge_pricing_blob(target: dict[str, dict[str, float]], pricing: dict[str, Any]) -> None:
    for model_id, rates in pricing.items():
        # ``_pricing_meta`` is a catalog-level annotation block (source
        # URL, reconciliation date, "needs verification" notes); it is
        # NOT a model id. Other ``_<key>`` entries are reserved for
        # similar metadata so consumers can ignore them as a class.
        if model_id.startswith("_"):
            continue
        if not isinstance(rates, dict):
            continue
        # Reject malformed entries instead of silently substituting 0:
        # a 0/0 rate makes budget routing think every call is free,
        # which is the opposite of safe behaviour for a cost gate.
        try:
            inp = float(rates.get("input", 0.0))
            out = float(rates.get("output", 0.0))
        except (TypeError, ValueError):
            continue
        target[model_id] = {"input": inp, "output": out}
        norm = _normalize_model_id(model_id)
        target.setdefault(norm, target[model_id])


class ModelPricing:
    """Hot-reloadable pricing table sourced from model_catalog.json."""

    def __init__(self, catalog_path: Path | None = None) -> None:
        self.catalog_path = catalog_path or _CATALOG_PATH
        self._mtime: float | None = None
        self._by_model: dict[str, dict[str, float]] = {}

    def reload(self, *, force: bool = False) -> None:
        path = self.catalog_path
        if not path.is_file():
            logger.warning("model catalog missing at %s; using fallback pricing", path)
            self._by_model = {"__default__": dict(_FALLBACK_PER_1K)}
            self._mtime = None
            return

        mtime = path.stat().st_mtime
        if not force and self._mtime == mtime and self._by_model:
            return

        payload = json.loads(path.read_text(encoding="utf-8"))
        merged: dict[str, dict[str, float]] = {}
        for provider in (payload.get("providers") or {}).values():
            if isinstance(provider, dict):
                _merge_pricing_blob(merged, provider.get("pricing") or {})

        if not merged:
            merged["__default__"] = dict(_FALLBACK_PER_1K)

        self._by_model = merged
        self._mtime = mtime

    def lookup(self, model: str) -> dict[str, float]:
        self.reload()
        if model in self._by_model:
            return dict(self._by_model[model])

        norm = _normalize_model_id(model)
        if norm in self._by_model:
            return dict(self._by_model[norm])

        for key, rates in self._by_model.items():
            if key == "__default__":
                continue
            if norm.startswith(key) or key.startswith(norm):
                return dict(rates)

        logger.debug("no catalog pricing for model %r; using fallback", model)
        return dict(_FALLBACK_PER_1K)


def compute_token_cost(
    pricing: ModelPricing,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    reasoning_tokens: int = 0,
) -> tuple[float, dict[str, float]]:
    """Return total USD and the per-1k rates used.

    Reasoning tokens are billed at the **output** rate per OpenAI's
    Responses API documentation: thinking tokens count toward
    ``completion_tokens_details.reasoning_tokens`` AND are charged at
    the same per-1k rate as visible output tokens. Pre-W2 the budget
    estimator never read this field — see findings/13-llm-core.md
    fix #5 + audit-r13 05-token-billing-leakage.
    """
    rates = pricing.lookup(model)
    prompt = max(0, int(prompt_tokens))
    completion = max(0, int(completion_tokens))
    reasoning = max(0, int(reasoning_tokens))
    input_dollars = (prompt / 1000.0) * rates["input"]
    output_dollars = ((completion + reasoning) / 1000.0) * rates["output"]
    return input_dollars + output_dollars, rates


# ─────────────────────────────────────────────────────────────────────
# Process-wide singleton
# ─────────────────────────────────────────────────────────────────────
#
# Every adapter / orchestrator / cost-budget caller reads pricing
# through this single instance so a catalog hot-edit (pricing
# reconciliation, runtime rate update) lands everywhere on the next
# request. The ``ModelPricing`` instance is mtime-aware so callers
# don't need to invalidate explicitly.

_SHARED_LOCK = threading.Lock()
_SHARED_PRICING: ModelPricing | None = None


def get_shared_pricing() -> ModelPricing:
    """Return the process-wide ``ModelPricing`` singleton."""
    global _SHARED_PRICING
    if _SHARED_PRICING is not None:
        return _SHARED_PRICING
    with _SHARED_LOCK:
        if _SHARED_PRICING is None:
            _SHARED_PRICING = ModelPricing()
    return _SHARED_PRICING


def pricing_per_1k(model: str) -> dict[str, float]:
    """Convenience wrapper: ``{"input": $/1k, "output": $/1k}`` for *model*.

    Provider adapters call this through ``BaseProvider.pricing_per_1k``;
    direct callers (estimator, telemetry) use it as the canonical
    catalog lookup. Returns the fallback rate
    (``_FALLBACK_PER_1K``) for unknown models — never raises so cost
    accounting can never be the path that takes down a chat turn.
    """
    return get_shared_pricing().lookup(model)
