"""Canonical model pricing loaded from providers/model_catalog.json.

This module is the **single source of truth** for $/1k token rates. All
provider adapters read pricing through :func:`get_shared_pricing` (or
the convenience :func:`pricing_per_1k`) — there are no adapter-local
``_pricing = {...}`` backstops.  every adapter shipped its own
literal, which drifted out of sync with the catalog the moment a
provider published a new rate (the OpenAI ``gpt-5.5`` divergence
documented in findings/13-llm-core.md was caused by exactly that).

Catalog reconciliation is part of the build: each entry in
``model_catalog.json`` either carries a verified rate or is marked
``__needs_verification__`` so a follow-up PR can either confirm or
flag the divergence. See findings/13-llm-core.md fix #4 + the
"pricing reconciliation" table in the  LLM router PR body.

Prompt-cache rates
------------------
Anthropic bills the two prompt-cache token classes separately from
ordinary input tokens, and at different rates:

===================  ====================================  ==========
usage field          what it is                            rate
===================  ====================================  ==========
cache_creation_...   tokens WRITTEN to the cache           1.25x input (5m TTL)
                                                           2x input (1h TTL)
cache_read_...       tokens SERVED from the cache          0.1x input
===================  ====================================  ==========

Source (fetched 2026-08-03):
https://platform.claude.com/docs/en/about-claude/pricing#prompt-caching
The per-model dollar figures behind those multipliers are on the same
page under "Model pricing", and they are what
``providers/model_catalog.json`` carries verbatim as the per-model
``cache_write`` / ``cache_read`` keys ($/1k, same units as
``input`` / ``output``).

``cache_write`` is the **5-minute** write rate. FERAL never sends
``cache_control.ttl``, so the only cache writes it can incur are the
5-minute kind; billing them at the 1h rate would over-charge by 60%.

These rates are OPTIONAL per model. A model whose catalog entry has no
``cache_write`` / ``cache_read`` keeps the pre-existing behaviour: its
cache tokens are simply not billed. That is deliberate. Folding cache
tokens into the plain prompt count would bill a cache READ at 10x its
real price, which is a worse error than under-counting, and inventing a
multiplier for a provider that does not publish one (OpenAI's cached
input discount is a different shape entirely, and it has no write
charge at all) would be a fabricated number in a cost gate.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger("feral.cost.pricing")


def _resolve_catalog_path() -> Path:
    """Locate ``providers/model_catalog.json`` in a way that works in
    BOTH layouts the wheel ends up in:

    * editable / source checkout — ``providers/`` is a sibling of
      ``cost/`` so ``Path(__file__).parents[1] / providers`` resolves.
    * pip-installed wheel — ``providers`` is a top-level package in
      ``site-packages`` and ``importlib.resources.files`` reaches the
      bundled data file regardless of where the package lives on disk
      (lone-file installs, zip-imported eggs, namespace packages).

    Tries ``importlib.resources`` first so an unusual install layout
    (zipapp, frozen bundle) still finds the catalog; falls back to the
    historical filesystem path so existing tests / dev workflows are
    unchanged.
    """
    try:
        from importlib.resources import files

        candidate = files("providers").joinpath("model_catalog.json")
        # ``Traversable.is_file`` is available on Python 3.11+ and
        # returns False inside zip-imports without raising — the path
        # below uses ``str()`` so a zipimporter-backed Traversable can
        # still surface a usable filesystem path via ``as_file`` (we
        # don't need a real FS file because ``ModelPricing.reload``
        # reads the bytes through ``Path.read_text`` only when the
        # path is_file()).
        as_path = Path(str(candidate))
        if as_path.is_file():
            return as_path
    except Exception:
        pass
    return Path(__file__).resolve().parents[1] / "providers" / "model_catalog.json"


_CATALOG_PATH = _resolve_catalog_path()
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
        entry: dict[str, float] = {"input": inp, "output": out}
        # Prompt-cache rates are optional (see the module docstring). A
        # missing / malformed / negative value is DROPPED rather than
        # defaulted to 0.0 or to the input rate: absence has to stay
        # distinguishable from "free", because ``compute_token_cost``
        # keys "don't bill this class of token" off the key not being
        # there. A 0.0 default would silently declare cache reads free.
        for cache_key in ("cache_write", "cache_read"):
            raw = rates.get(cache_key)
            if raw is None:
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if value < 0.0:
                continue
            entry[cache_key] = value
        target[model_id] = entry
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
    cache_write_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> tuple[float, dict[str, float]]:
    """Return total USD and the per-1k rates used.

    Reasoning tokens are billed at the **output** rate per OpenAI's
    Responses API documentation: thinking tokens count toward
    ``completion_tokens_details.reasoning_tokens`` AND are charged at
    the same per-1k rate as visible output tokens.  the budget
    estimator never read this field — see findings/13-llm-core.md
    fix #5 + audit-r13 05-token-billing-leakage.

    ``cache_write_tokens`` / ``cache_read_tokens`` are Anthropic's
    ``usage.cache_creation_input_tokens`` /
    ``usage.cache_read_input_tokens``. They are billed at their own
    per-model rates (module docstring), and only when the model
    actually has those rates: for a model with no published cache rate
    they contribute $0 rather than a guess. Both default to 0, so
    every existing caller is unaffected.
    """
    rates = pricing.lookup(model)
    prompt = max(0, int(prompt_tokens))
    completion = max(0, int(completion_tokens))
    reasoning = max(0, int(reasoning_tokens))
    input_dollars = (prompt / 1000.0) * rates["input"]
    output_dollars = ((completion + reasoning) / 1000.0) * rates["output"]
    cache_dollars = cache_token_cost(rates, cache_write_tokens, cache_read_tokens)
    return input_dollars + output_dollars + cache_dollars, rates


def cache_token_cost(
    rates: dict[str, float],
    cache_write_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> float:
    """USD for a turn's prompt-cache tokens, given already-looked-up *rates*.

    Returns 0.0 when the model carries no cache rates, which is the
    documented "leave them unbilled" fallback rather than an assertion
    that they were free.
    """
    write = max(0, int(cache_write_tokens or 0))
    read = max(0, int(cache_read_tokens or 0))
    dollars = 0.0
    if write and rates.get("cache_write") is not None:
        dollars += (write / 1000.0) * float(rates["cache_write"])
    if read and rates.get("cache_read") is not None:
        dollars += (read / 1000.0) * float(rates["cache_read"])
    return dollars


def cache_equivalent_prompt_tokens(
    model: str,
    cache_write_tokens: int = 0,
    cache_read_tokens: int = 0,
    pricing: ModelPricing | None = None,
) -> int:
    """How many *base-input-rate* tokens cost the same as this turn's
    prompt-cache tokens.

    This is the adapter that lets a caller bill cache tokens through a
    ledger whose only input-side column is a plain prompt-token count
    (``cost.budget.record_usage``) without either under-charging (drop
    them, the pre-existing bug) or over-charging (add them raw, which
    would bill a cache READ at 10x its real price).

    ``dollars(prompt + equivalent) == dollars(prompt) + dollars(cache)``
    by construction, so the cap moves by exactly the right amount. The
    ``round`` is worth at most half a token, well under a hundredth of a
    cent at any published rate, and it is the only place precision is
    lost.

    Returns 0 (meaning "bill nothing extra") when the model has no
    cache rates or no usable base input rate. Never raises: a pricing
    miss must not be able to take down a chat turn.
    """
    try:
        table = pricing if pricing is not None else get_shared_pricing()
        rates = table.lookup(model)
        base_input = float(rates.get("input") or 0.0)
        if base_input <= 0.0:
            # No base rate to express the equivalence against (a
            # catalog entry of 0.0, or a lookup that produced nothing).
            return 0
        dollars = cache_token_cost(rates, cache_write_tokens, cache_read_tokens)
        if dollars <= 0.0:
            return 0
        return int(round(dollars / base_input * 1000.0))
    except Exception as exc:
        logger.debug("cache_equivalent_prompt_tokens(%r) failed: %s", model, exc)
        return 0


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
