"""Smoke test for the (private) GenUI publisher demo.

The demo script lives at private/demos/demo_genui_publisher.sh and is
gitignored. This test is the PUBLIC guarantee that the five moving
parts the script drives — validate, install, manifest lookup, open
surface, uninstall — still work.

If this breaks, fix the test first.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


pytestmark = pytest.mark.no_auto_feral_home


DEMO_MANIFEST = """
app_id: demo-weather
version: 0.1.0
author: FERAL demo
description: Lightweight weather app bound to the weather skill.

brand:
  name: Demo Weather
  primary_color: "#1A73E8"

permissions:
  - weather.read

entry_surface_id: home

surfaces:
  - surface_id: home
    kind: authored
    title: Today
    template_root:
      type: stack
      children:
        - { type: heading, text: "Today" }
        - { type: text, value: "$data.location" }
    action_contract:
      - action_id: refresh
        handler: navigate
        target: home

  - surface_id: forecast
    kind: hybrid
    title: Next 5 days
    generation_prompt: "Forecast summary"
    template_root:
      type: stack
      children:
        - { type: heading, text: "Next 5 days" }
"""


@pytest.fixture
def brain(tmp_path):
    from agents.app_registry import AppRegistry, HybridGenerator

    registry = AppRegistry(
        db_path=str(tmp_path / "demo_apps.db"),
        apps_dir=tmp_path / "apps",
    )
    hybrid = HybridGenerator(cache_dir=tmp_path / "cache")
    registry.set_hybrid_generator(hybrid)

    orch = MagicMock()
    orch.handle_ui_event = AsyncMock()

    mock = MagicMock()
    mock.app_registry = registry
    mock.orchestrator = orch
    mock.sessions = {}

    with patch("api.state.state", mock), patch("api.routes.apps.state", mock):
        from api.server import app
        yield TestClient(app, raise_server_exceptions=False), registry, tmp_path


def test_demo_step_1_validate_manifest(brain):
    c, _registry, _tmp = brain
    r = c.post("/api/apps/validate", json={"manifest": DEMO_MANIFEST})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    assert body["summary"]["app_id"] == "demo-weather"
    assert set(body["summary"]["surfaces"]) == {"home", "forecast"}


def _consented_install(c, source: dict):
    """Preview, then install with the token the preview minted.

    Installing an app is a two-step consent flow: the app's own reach and
    the skills it pulls in (which execute Python in this process) are
    shown before anything is written. These tests are about the routes
    downstream of that decision, so they take the same two steps a client
    takes rather than asserting the old ungated shape. See
    tests/test_app_install_consent.py for the gate itself.
    """
    preview = c.post("/api/apps/preview", json=source)
    if preview.status_code != 200:
        return preview
    body = {"install_token": preview.json().get("install_token", "")}
    if "overwrite" in source:
        body["overwrite"] = source["overwrite"]
    return c.post("/api/apps/install", json=body)


def test_demo_step_2_install_from_manifest(brain, tmp_path):
    c, registry, _tmp = brain
    # Write the manifest to a temp app folder so /api/apps/install path
    # works (it expects a directory with manifest.yaml or manifest.json).
    src = tmp_path / "src-demo"
    src.mkdir()
    (src / "manifest.yaml").write_text(DEMO_MANIFEST)

    r = _consented_install(c, {"path": str(src), "overwrite": True, "unsigned": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    assert body["app"]["app_id"] == "demo-weather"
    assert registry.get("demo-weather") is not None


def test_demo_step_3_fetch_manifest(brain, tmp_path):
    c, _registry, _tmp = brain
    src = tmp_path / "src-demo"
    src.mkdir()
    (src / "manifest.yaml").write_text(DEMO_MANIFEST)
    _consented_install(c, {"path": str(src), "unsigned": True})

    r = c.get("/api/apps/demo-weather/manifest")
    assert r.status_code == 200
    body = r.json()
    assert body["app_id"] == "demo-weather"
    assert body["manifest"]["brand"]["name"] == "Demo Weather"


def test_demo_step_4_open_home_surface(brain, tmp_path):
    c, _registry, _tmp = brain
    src = tmp_path / "src-demo"
    src.mkdir()
    (src / "manifest.yaml").write_text(DEMO_MANIFEST)
    _consented_install(c, {"path": str(src), "unsigned": True})

    r = c.post("/api/apps/demo-weather/open", json={"data": {"location": "Brooklyn"}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    assert body["surface_id"] == "home"
    # The authored template must hydrate the "$data.location" placeholder.
    root = body["root"]
    # Walk for "Brooklyn" anywhere in the rendered tree text.
    import json as _json
    blob = _json.dumps(root)
    assert "Brooklyn" in blob


def test_demo_step_5_uninstall(brain, tmp_path):
    c, registry, _tmp = brain
    src = tmp_path / "src-demo"
    src.mkdir()
    (src / "manifest.yaml").write_text(DEMO_MANIFEST)
    _consented_install(c, {"path": str(src), "unsigned": True})
    assert registry.get("demo-weather") is not None

    r = c.delete("/api/apps/demo-weather")
    assert r.status_code == 200
    assert r.json()["success"] is True
    assert registry.get("demo-weather") is None
