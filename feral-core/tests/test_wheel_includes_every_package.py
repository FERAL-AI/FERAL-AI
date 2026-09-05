"""Every top-level package in feral-core ships in the wheel, or is excluded on purpose.

The published wheel is built from an explicit include list in
``pyproject.toml`` (``[tool.setuptools.packages.find].include``). A package
added to the tree after that list was written is silently left out, and
because CI runs from the checkout, nothing notices until an operator
installs from PyPI.

That happened at least three times: ``workflows`` (audit-r9, packs missing
at runtime), the ``providers`` model catalog JSON (fallback pricing on
every request), and on 2026-09-04 four packages at once. The installed
2026.9.2 logged on every boot::

    [WARNING] [feral.brain] migration pass failed; continuing boot
    ModuleNotFoundError: No module named 'migrations'

and ``process``, ``system`` and ``bridges`` were missing with it, so the
~/.feral migrations never ran on a pip install and ``cli/main.py``'s
``system.preflight`` import could only ever fail there.

These tests read the same ``pyproject.toml`` setuptools reads and compare
it with the tree, so a new package cannot be added without either
shipping it or naming it in ``exclude``. They do not build a wheel; the
release script does that, and this is the check that runs on every PR.
"""

from __future__ import annotations

import fnmatch
import re
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Directories that are packages by shape but are not part of the product.
# ``build`` and ``dist`` are setuptools output; ``tests`` and
# ``scripts_audit`` are named in pyproject's exclude list and asserted so
# below, which is deliberately the only way a package may be left out.
_NOT_PRODUCT = {"build", "dist", "__pycache__", "node_modules"}


def _find_config() -> dict:
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text())
    return cfg["tool"]["setuptools"]["packages"]["find"]


def _top_level_packages() -> list[str]:
    out = []
    for d in sorted(ROOT.iterdir()):
        if d.is_dir() and (d / "__init__.py").exists() and d.name not in _NOT_PRODUCT:
            out.append(d.name)
    return out


def _matches(name: str, globs: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, g) for g in globs)


def test_every_top_level_package_is_included_or_deliberately_excluded():
    find = _find_config()
    include, exclude = find["include"], find.get("exclude", [])
    missing = [
        name for name in _top_level_packages()
        if not _matches(name, include) and not _matches(name, exclude)
    ]
    assert not missing, (
        f"top-level packages {missing} are in neither the wheel include list "
        f"nor the exclude list in feral-core/pyproject.toml. A package that is "
        f"imported at runtime must be included; one that is not must be named "
        f"in exclude so the omission is a decision rather than an accident."
    )


_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+([A-Za-z_][A-Za-z0-9_]*)", re.M)


def _runtime_imported_local_packages() -> set[str]:
    """Top-level local packages imported by non-test product code.

    Lazy imports inside functions count too (``from migrations import
    run_pending`` at api/server.py is one), which is why this is a regex
    over source rather than a static import graph.
    """
    local = set(_top_level_packages())
    find = _find_config()
    excluded = {n for n in local if _matches(n, find.get("exclude", []))}
    seen: set[str] = set()
    for py in ROOT.rglob("*.py"):
        parts = py.relative_to(ROOT).parts
        if parts[0] in _NOT_PRODUCT or parts[0] in excluded:
            continue
        try:
            src = py.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for name in _IMPORT_RE.findall(src):
            if name in local and name != parts[0]:
                seen.add(name)
    return seen


def test_every_package_product_code_imports_is_in_the_wheel():
    find = _find_config()
    include = find["include"]
    unshipped = sorted(n for n in _runtime_imported_local_packages() if not _matches(n, include))
    assert not unshipped, (
        f"product code imports {unshipped} but the wheel include list does not "
        f"ship them; a pip install would raise ModuleNotFoundError at runtime, "
        f"exactly as 2026.9.2 did for 'migrations'."
    )


@pytest.mark.parametrize("name", ["migrations", "process", "system", "bridges"])
def test_the_four_packages_that_shipped_missing_are_now_included(name):
    """Pins the 2026-09-04 case by name so the story survives a refactor."""
    assert (ROOT / name / "__init__.py").exists(), f"{name} moved; update this test"
    assert _matches(name, _find_config()["include"])


def test_excluded_packages_are_not_imported_by_product_code():
    """The exclude list is only safe if nothing shipped needs those packages."""
    find = _find_config()
    excluded = {n for n in _top_level_packages() if _matches(n, find.get("exclude", []))}
    needed = sorted(excluded & _runtime_imported_local_packages())
    assert not needed, f"{needed} are excluded from the wheel but product code imports them"
