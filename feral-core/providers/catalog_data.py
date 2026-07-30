"""Typed read access to the bundled ``providers/model_catalog.json``.

``cost/pricing.py`` already owns the pricing projection of this file.
This module is the *other* projection: model lists and per-model
capability records, for callers that must not hardcode either.

Why it exists
-------------
``providers/anthropic_provider.py`` used to carry two hand-maintained
literals — a ``_models`` list and an ``_ADAPTIVE_THINKING_MODELS``
frozenset with a single entry (``claude-opus-4-7``). That frozenset is
what decides whether the adapter may send ``temperature``, and
``temperature`` / ``top_p`` / ``top_k`` return HTTP 400 on every
adaptive-thinking Claude from 4.7 onward. Adding a Claude 5 id to the
model list without also remembering to extend the frozenset would have
400'd every Claude 5 call that carried a temperature — a class of bug
the roadmap §3.5 P0 ban on stale model literals exists to prevent.

Both now derive from the catalog's ``capabilities`` block, which
``scripts/research_providers.py`` refreshes from Anthropic's live
``GET /v1/models`` (it returns ``thinking.types``, ``effort`` levels,
``max_input_tokens``, ``max_tokens`` and the image/pdf/structured-output
booleans). The literal is gone; the data is one refresh away from
current.

Reads are mtime-cached so a hot catalog edit lands on the next call
without a process restart, matching ``cost.pricing.ModelPricing``.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger("feral.providers.catalog_data")


def _resolve_catalog_path() -> Path:
    """Locate ``providers/model_catalog.json`` in both install layouts.

    Mirrors :func:`cost.pricing._resolve_catalog_path` — ``importlib``
    first so a zipapp / frozen bundle still finds the bundled data file,
    then the historical filesystem path for editable checkouts.
    """
    try:
        from importlib.resources import files

        candidate = Path(str(files("providers").joinpath("model_catalog.json")))
        if candidate.is_file():
            return candidate
    except Exception as exc:
        logger.debug("importlib catalog lookup failed: %s", exc)
    return Path(__file__).resolve().parent / "model_catalog.json"


_CATALOG_PATH = _resolve_catalog_path()
_LOCK = threading.Lock()
_CACHE: dict[str, Any] = {}
_CACHE_MTIME: float | None = None


def catalog_path() -> Path:
    """Absolute path to the bundled catalog (tests + tooling use this)."""
    return _CATALOG_PATH


def load_catalog(*, force: bool = False) -> dict[str, Any]:
    """Return the parsed catalog document, mtime-cached.

    Returns an empty dict when the file is missing or unparseable. It
    never raises: every caller here is on a metadata path, and a bad
    catalog must degrade to "we know nothing" rather than take down a
    chat turn.
    """
    global _CACHE_MTIME
    path = _CATALOG_PATH
    if not path.is_file():
        logger.warning("model catalog missing at %s", path)
        return {}
    mtime = path.stat().st_mtime
    if not force and _CACHE_MTIME == mtime and _CACHE:
        return _CACHE
    with _LOCK:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("model catalog unreadable at %s: %s", path, exc)
            return {}
        if not isinstance(payload, dict):
            logger.warning("model catalog is not an object: %s", path)
            return {}
        _CACHE.clear()
        _CACHE.update(payload)
        _CACHE_MTIME = mtime
    return _CACHE


def provider_ids() -> set[str]:
    """Every provider id the bundled catalog carries data for."""
    providers = load_catalog().get("providers")
    return set(providers) if isinstance(providers, dict) else set()


def provider_entry(provider_id: str) -> dict[str, Any]:
    """The raw catalog entry for *provider_id* (empty dict when absent)."""
    providers = load_catalog().get("providers")
    if not isinstance(providers, dict):
        return {}
    entry = providers.get(provider_id)
    return entry if isinstance(entry, dict) else {}


def bundled_models(provider_id: str) -> list[str]:
    """The bundled model-id list for *provider_id*, in catalog order."""
    models = provider_entry(provider_id).get("models")
    if not isinstance(models, list):
        return []
    return [m for m in models if isinstance(m, str) and m]


def capabilities(provider_id: str) -> dict[str, dict[str, Any]]:
    """``{model_id: capability_record}`` for *provider_id*."""
    caps = provider_entry(provider_id).get("capabilities")
    if not isinstance(caps, dict):
        return {}
    return {k: v for k, v in caps.items() if isinstance(v, dict)}


def capability(provider_id: str, model_id: str, key: str, default: Any = None) -> Any:
    """Read one capability field, falling back to *default*.

    Unknown model ids return *default* rather than raising: a model that
    shipped after the last catalog refresh must not crash the caller,
    and every call site here treats "unknown" as "assume the
    conservative default".
    """
    record = capabilities(provider_id).get(model_id)
    if not isinstance(record, dict) or key not in record:
        return default
    return record[key]


def models_with_capability(provider_id: str, key: str, *path: str) -> frozenset[str]:
    """Ids whose capability record has a truthy value at ``key[/path...]``.

    ``models_with_capability("anthropic", "thinking", "adaptive")`` is
    the replacement for the old hand-maintained
    ``_ADAPTIVE_THINKING_MODELS`` frozenset.
    """
    out: set[str] = set()
    for model_id, record in capabilities(provider_id).items():
        node: Any = record.get(key)
        for step in path:
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(step)
        if isinstance(node, dict):
            node = node.get("supported")
        if node:
            out.add(model_id)
    return frozenset(out)
