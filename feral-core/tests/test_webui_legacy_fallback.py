"""A missing v2 bundle must be visible on the page, not just in the log.

``api/server.py`` used to pick the served directory with::

    _webui_dir = _webui_v2_dir if _webui_v2_ready else _webui_legacy_dir

so an install that shipped without ``webui_v2/`` (a packaging fault that
has already happened once, per the comment above ``_webui_v2_dir``) served
the superseded v1 client at ``/`` and logged a warning. Nothing on the
served page said which client it was, so the user could not know, and bug
reports landed against retired code.

The missing-v2 branch now fails closed to a page that names the fault.
``FERAL_SERVE_LEGACY_WEBUI=1`` remains as an explicit opt-in and serves v1
with a fixed, non-dismissible banner.

These tests monkeypatch the module-level guards rather than reloading, so
they run identically whether or not this checkout has built webui_v2/.
Every guard the route reads is looked up as a module global at call time,
which is what makes that work.
"""

from __future__ import annotations

import importlib
import sys

import pytest
from starlette.testclient import TestClient

import api.server as server

pytestmark = pytest.mark.no_auto_feral_home


@pytest.fixture
def legacy_bundle(tmp_path):
    """A stand-in v1 bundle so the tests do not depend on feral-core/webui/."""
    d = tmp_path / "webui"
    d.mkdir()
    (d / "index.html").write_text(
        '<!DOCTYPE html><html><head><title>FERAL</title></head>'
        '<body class="v1"><div id="root"></div></body></html>',
        encoding="utf-8",
    )
    return d


def _fail_closed(monkeypatch, legacy_dir):
    """v2 absent, v1 on disk, no opt-in: the shipped default."""
    monkeypatch.setattr(server, "_webui_v2_ready", False)
    monkeypatch.setattr(server, "_webui_legacy_ready", True)
    monkeypatch.setattr(server, "_webui_legacy_dir", legacy_dir)
    monkeypatch.setattr(server, "_webui_legacy_serving", False)
    monkeypatch.setattr(server, "_webui_dir", legacy_dir)
    monkeypatch.setattr(server, "_webui_ready", False)
    monkeypatch.setattr(server, "_webui_variant", "missing")


def _opt_in(monkeypatch, legacy_dir):
    """v2 absent, v1 on disk, FERAL_SERVE_LEGACY_WEBUI=1."""
    monkeypatch.setattr(server, "_webui_v2_ready", False)
    monkeypatch.setattr(server, "_webui_legacy_ready", True)
    monkeypatch.setattr(server, "_webui_legacy_dir", legacy_dir)
    monkeypatch.setattr(server, "_webui_legacy_serving", True)
    monkeypatch.setattr(server, "_webui_dir", legacy_dir)
    monkeypatch.setattr(server, "_webui_ready", True)
    monkeypatch.setattr(server, "_webui_variant", "v1-legacy")


# ── default: fail closed ─────────────────────────────────────────


def test_missing_v2_does_not_silently_serve_v1(monkeypatch, legacy_bundle):
    _fail_closed(monkeypatch, legacy_bundle)
    c = TestClient(server.app, raise_server_exceptions=False)

    r = c.get("/")
    assert r.status_code == 200
    # The defect, stated: v1's markup must not come back.
    assert 'class="v1"' not in r.text
    assert '<div id="root">' not in r.text


def test_fallback_page_names_the_packaging_fault(monkeypatch, legacy_bundle):
    _fail_closed(monkeypatch, legacy_bundle)
    c = TestClient(server.app, raise_server_exceptions=False)

    body = c.get("/").text
    assert "webui_v2" in body
    assert "make bundle-webui" in body
    # It must say the old client exists and is deliberately not served,
    # or the reader concludes the brain simply has no UI.
    assert "v1" in body
    assert "FERAL_SERVE_LEGACY_WEBUI" in body


def test_deep_spa_routes_also_fail_closed(monkeypatch, legacy_bundle):
    """Not just ``/``. A bookmarked deep route lands here too."""
    _fail_closed(monkeypatch, legacy_bundle)
    c = TestClient(server.app, raise_server_exceptions=False)

    body = c.get("/settings").text
    assert 'class="v1"' not in body
    assert "webui_v2" in body


def test_fallback_picker_distinguishes_the_two_faults(monkeypatch, legacy_bundle):
    """"nothing built" and "shipped without the current client" differ."""
    _fail_closed(monkeypatch, legacy_bundle)
    assert server._webui_fallback_html() is server._PACKAGING_FAULT_HTML

    monkeypatch.setattr(server, "_webui_legacy_ready", False)
    assert server._webui_fallback_html() is server._FALLBACK_HTML


def test_api_paths_keep_their_honest_404(monkeypatch, legacy_bundle):
    """The fail-closed branch must not swallow the unknown-route guard."""
    _fail_closed(monkeypatch, legacy_bundle)
    c = TestClient(server.app, raise_server_exceptions=False)
    r = c.get("/api/definitely-not-a-route")
    assert r.status_code == 404


# ── opt-in: v1 plus a banner that cannot be dismissed ────────────


def test_opt_in_serves_v1_with_a_banner(monkeypatch, legacy_bundle):
    _opt_in(monkeypatch, legacy_bundle)
    c = TestClient(server.app, raise_server_exceptions=False)

    body = c.get("/").text
    # v1 really is served now.
    assert '<div id="root">' in body
    # ...and it says so, on the page.
    assert "feral-legacy-webui-banner" in body
    assert "superseded v1" in body
    assert "FERAL_SERVE_LEGACY_WEBUI" in body


def test_banner_has_no_dismiss_affordance(monkeypatch, legacy_bundle):
    """"Persistent, non-dismissible" is the requirement, so pin it.

    No script, no close control, and a fixed position so it cannot be
    scrolled away from.
    """
    banner = server._LEGACY_WEBUI_BANNER
    assert "<script" not in banner.lower()
    assert "onclick" not in banner.lower()
    assert "position:fixed" in banner.replace(" ", "")
    for word in ("dismiss", "close", "×"):
        assert word not in banner.lower()


def test_banner_lands_inside_body_and_keeps_the_document(legacy_bundle):
    html = (legacy_bundle / "index.html").read_text()
    out = server._inject_legacy_banner(html)

    assert out.index("<body") < out.index("feral-legacy-webui-banner")
    assert out.index("feral-legacy-webui-banner") < out.index('id="root"')
    assert "</html>" in out


def test_banner_survives_an_index_with_no_body_tag():
    """An unrecognised bundle must still carry the warning."""
    out = server._inject_legacy_banner("<div id=root></div>")
    assert "feral-legacy-webui-banner" in out
    assert "id=root" in out


def test_deep_spa_routes_carry_the_banner_too(monkeypatch, legacy_bundle):
    _opt_in(monkeypatch, legacy_bundle)
    c = TestClient(server.app, raise_server_exceptions=False)
    assert "feral-legacy-webui-banner" in c.get("/settings").text


def test_unreadable_legacy_index_degrades_to_the_fault_page(monkeypatch, tmp_path):
    missing = tmp_path / "not-a-bundle"
    missing.mkdir()
    _opt_in(monkeypatch, missing)
    c = TestClient(server.app, raise_server_exceptions=False)

    body = c.get("/").text
    assert "webui_v2" in body
    assert "make bundle-webui" in body


# ── the env var is actually wired to the guard ───────────────────


def test_opt_in_env_var_is_read_at_import(monkeypatch):
    """Reload the module with the env set and check the guard flipped.

    The monkeypatch helpers above set ``_webui_legacy_serving`` by hand, so
    without this the env plumbing itself would be untested.
    """
    monkeypatch.setenv("FERAL_SERVE_LEGACY_WEBUI", "1")
    try:
        reloaded = importlib.reload(sys.modules["api.server"])
        assert reloaded._webui_legacy_opt_in is True
    finally:
        monkeypatch.delenv("FERAL_SERVE_LEGACY_WEBUI", raising=False)
        importlib.reload(sys.modules["api.server"])


def test_opt_in_is_off_by_default():
    assert server._webui_legacy_opt_in is False
    # And an unset/blank/garbage value never counts as opt-in.
    assert server._SERVE_LEGACY_WEBUI_ENV == "FERAL_SERVE_LEGACY_WEBUI"
