"""Regression guard for ``scripts/release_wheel_smoke.py``.

The smoke test exists to catch one specific shipped regression: a wheel
built without ``feral-core/webui_v2/``. Its root-page assertion used to be
"the body contains FERAL and v2 and does not contain leaflet". Every one
of those holds for ``api/server.py``'s ``_PACKAGING_FAULT_HTML``, which is
the page the brain serves *because* the bundle is missing. It says "this
install shipped without the v2 dashboard" and names ``webui_v2/``. The
check was satisfied by the failure it was written for.

These tests read the fault pages out of ``api/server.py`` rather than
restating them, so a reworded fault page cannot quietly drift back into
passing.

Run with::

    python -m pytest scripts/tests/test_release_wheel_smoke.py

Nothing in CI collects this directory today; see the note in
``docs/RELEASE.md`` and the handover in the audit report.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from release_wheel_smoke import (  # noqa: E402
    _NOT_THE_BUNDLE,
    _bundle_asset_refs,
)

SERVER_PY = REPO_ROOT / "feral-core" / "api" / "server.py"
BUNDLE_INDEX = REPO_ROOT / "feral-core" / "webui_v2" / "index.html"


def _html_constant(name: str) -> str:
    """Pull a triple-quoted HTML constant out of ``api/server.py``.

    Reading the source rather than importing it: importing ``api.server``
    constructs real subsystems and needs the full runtime dependency set,
    which is far more than this assertion is worth.
    """
    source = SERVER_PY.read_text(encoding="utf-8")
    match = re.search(rf'^{re.escape(name)} = """(.*?)"""', source, re.S | re.M)
    if match is None:
        pytest.fail(
            f"{name} is no longer a module-level triple-quoted constant in "
            f"{SERVER_PY}. This test guards the served-page contract; update "
            "it deliberately rather than deleting it."
        )
    return match.group(1)


@pytest.fixture(scope="module")
def bundle_refs() -> list[str]:
    if not BUNDLE_INDEX.is_file():
        pytest.skip(f"no built bundle at {BUNDLE_INDEX}")
    return _bundle_asset_refs(BUNDLE_INDEX.read_text(encoding="utf-8"))


def test_the_built_bundle_declares_asset_entry_points(bundle_refs):
    """Without this the whole assertion is vacuous."""
    assert bundle_refs, "webui_v2/index.html declares no assets/*.js or *.css"
    assert any(r.endswith(".js") for r in bundle_refs)
    assert any(r.endswith(".css") for r in bundle_refs)
    for ref in bundle_refs:
        assert ref.startswith("assets/"), ref


@pytest.mark.parametrize(
    "constant",
    ["_PACKAGING_FAULT_HTML", "_FALLBACK_HTML"],
)
def test_a_no_bundle_page_does_not_satisfy_the_bundle_assertion(
    constant, bundle_refs
):
    """The defect this file exists for.

    Neither page the brain serves in place of the bundle may contain the
    bundle's own hashed asset references.
    """
    page = _html_constant(constant)
    for ref in bundle_refs:
        assert ref not in page, (
            f"{constant} contains the bundle asset reference {ref!r}, so the "
            "smoke test's root assertion would pass on a wheel that shipped "
            "without webui_v2/"
        )


@pytest.mark.parametrize(
    "constant",
    ["_PACKAGING_FAULT_HTML", "_FALLBACK_HTML"],
)
def test_a_no_bundle_page_is_recognised_by_its_own_wording(constant):
    """The second, independent leg: a named marker with a clear message.

    If ``api/server.py`` rewords a fault page, this fails here rather
    than silently downgrading the release gate to the asset check alone.
    """
    page = _html_constant(constant).lower()
    assert any(marker in page for marker in _NOT_THE_BUNDLE), (
        f"{constant} matches none of release_wheel_smoke._NOT_THE_BUNDLE. "
        "The page was reworded; update the marker list."
    )


def test_the_old_word_markers_really_were_blind():
    """Documents why the assertion changed, and stays true or fails.

    This is the measurement that justified the fix: the retired check was
    `FERAL in body and v2 in body and leaflet not in body`.
    """
    fault = _html_constant("_PACKAGING_FAULT_HTML").lower()
    assert "feral" in fault
    assert "v2" in fault
    assert "leaflet" not in fault
    # ... so the retired assertion passed on the packaging fault page.


def test_the_legacy_banner_is_flagged():
    """``FERAL_SERVE_LEGACY_WEBUI=1`` serves v1 with a banner.

    A release smoke must not accept that either, and the v1 index carries
    no v2 asset hashes, so both legs catch it.
    """
    banner = _html_constant("_LEGACY_WEBUI_BANNER").lower()
    assert any(marker in banner for marker in _NOT_THE_BUNDLE)
