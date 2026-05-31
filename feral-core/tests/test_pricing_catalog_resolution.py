"""``cost.pricing._resolve_catalog_path`` finds ``model_catalog.json``
both in the source tree and through ``importlib.resources``.

Operator report: pip-installed users hit ``[feral.cost.pricing] model
catalog missing at .../site-packages/providers/model_catalog.json;
using fallback pricing`` on every request. Root cause was a
``[tool.setuptools.package-data]`` gap that left the JSON out of the
wheel — fixed in the parent commit. This test guards the loader's
side of the contract so a future refactor can't quietly regress to
the static / broken path.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def test_resolve_catalog_path_returns_existing_file():
    from cost.pricing import _resolve_catalog_path

    path = _resolve_catalog_path()
    assert isinstance(path, Path)
    assert path.is_file(), f"resolved catalog path does not exist: {path}"
    assert path.name == "model_catalog.json"


def test_module_level_catalog_path_is_loadable():
    """``_CATALOG_PATH`` is captured at import time; ``ModelPricing``
    relies on the file being readable to populate non-fallback rates.
    A working loader path means the operator never sees the "model
    catalog missing ... using fallback pricing" warning."""
    import json

    from cost.pricing import _CATALOG_PATH

    assert _CATALOG_PATH.is_file()
    payload = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
    # Catalog shape sanity — the loader walks ``providers.*.pricing``
    # so at least one provider entry must exist for the rates to land.
    assert "providers" in payload
    assert isinstance(payload["providers"], dict)
    assert payload["providers"]
