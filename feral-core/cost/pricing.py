"""Canonical model pricing loaded from providers/model_catalog.json."""

from __future__ import annotations

import json
import logging
import re
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
        if not isinstance(rates, dict):
            continue
        try:
            target[model_id] = {
                "input": float(rates.get("input", 0.0)),
                "output": float(rates.get("output", 0.0)),
            }
        except (TypeError, ValueError):
            continue
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
    """Return total USD and the per-1k rates used."""
    rates = pricing.lookup(model)
    prompt = max(0, int(prompt_tokens))
    completion = max(0, int(completion_tokens))
    reasoning = max(0, int(reasoning_tokens))
    input_dollars = (prompt / 1000.0) * rates["input"]
    output_dollars = ((completion + reasoning) / 1000.0) * rates["output"]
    return input_dollars + output_dollars, rates
