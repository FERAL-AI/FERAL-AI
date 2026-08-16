"""Hot-reload of a skill has to actually reload it, and has to say so.

Two defects, one button
=======================

**The reload itself.** ``SkillRegistry.reload_skill`` resolved a shipped
manifest as ``skills/manifests/{skill_id}.json``. The file stem is not the
skill id: eight of the shipped manifests declare an id that differs from
their file name (``calendar.json`` -> ``calendar_google``, ``github.json``
-> ``github_api``, ``task.json`` -> ``background_task``,
``messaging.json`` -> ``messaging_sms``, ``notes.json`` ->
``notes_memory``, ``robot_action.json`` -> ``robot_ext``,
``smart_home.json`` -> ``smart_home_hue``, ``spotify.json`` ->
``spotify_music``). ``register()`` keys on the DECLARED id, which is what
``GET /skills`` reports and therefore the only id the Skills page can send
back. So a hot-reload of any of those eight found no candidate and
returned False. Measured on a clean tree: 9 of the 40 loaded skills failed
to reload, the ninth being ``weather_current``, which is a Python constant
with no file to re-read at all.

**The reporting.** ``POST /api/skills/reload`` answered that failure with
HTTP 200 and ``{"ok": false, "skill_id": ...}``, no ``error`` key. The v2
client raises on a non-2xx status, or on a 2xx body carrying ``error``;
that shape trips neither, so ``Skills.jsx`` rendered "Hot-reloaded <id>"
for a reload that had done nothing. A success status on a failed operation
does not mislead one caller, it disables every generic caller at once.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from skills.registry import SkillRegistry

MANIFESTS_DIR = Path(__file__).resolve().parents[1] / "skills" / "manifests"


def _declared_ids() -> list[tuple[str, str]]:
    """``(file stem, declared skill_id)`` for every shipped manifest."""
    out = []
    for path in sorted(MANIFESTS_DIR.glob("*.json")):
        with open(path) as fh:
            data = json.load(fh)
        declared = data.get("skill_id")
        if declared:
            out.append((path.stem, declared))
    return out


# ── the reload itself ────────────────────────────────────────────


def test_manifest_ids_really_do_diverge_from_their_filenames():
    """Pins the premise. If this ever goes empty the defect is unreachable
    and the tests below stop meaning anything, which is worth knowing."""
    divergent = [(stem, sid) for stem, sid in _declared_ids() if stem != sid]
    assert divergent, (
        "no shipped manifest declares an id different from its file stem, so "
        "this suite is no longer exercising the resolution bug it was written for"
    )


def test_reload_finds_a_manifest_whose_filename_is_not_the_skill_id():
    """``calendar_google`` lives in ``calendar.json``. It must still reload."""
    registry = SkillRegistry()
    registry.load_builtin_skills()
    assert "calendar_google" in registry.skills

    assert registry.reload_skill("calendar_google") is True


@pytest.mark.parametrize("stem,skill_id", _declared_ids())
def test_every_shipped_manifest_reloads_by_its_declared_id(stem, skill_id):
    registry = SkillRegistry()
    registry.load_builtin_skills()

    ok, code, reason = registry.reload_skill_detail(skill_id)
    assert ok, f"{stem}.json declares {skill_id!r} but reload said {code}: {reason}"
    assert skill_id in registry.skills


def test_reload_reports_why_when_there_is_no_source_on_disk():
    """``weather_current`` is a Python constant (models/skill_manifest.py).

    There is genuinely no file to re-read, so reload must fail. What it
    must not do is fail mutely: the reason names the id and the two
    directories that were searched.
    """
    registry = SkillRegistry()
    registry.load_builtin_skills()
    assert "weather_current" in registry.skills

    ok, code, reason = registry.reload_skill_detail("weather_current")
    assert ok is False
    assert code == "no_source"
    assert "weather_current" in reason


def test_reload_picks_up_edited_manifest_content(tmp_path):
    """A marketplace package edited on disk is what comes back."""
    pkg_dir = tmp_path / "skills" / "edited_skill"
    pkg_dir.mkdir(parents=True)
    manifest = {
        "skill_id": "edited_skill",
        "description": "before",
        "brand": {"name": "Edited"},
        "endpoints": [],
    }
    (pkg_dir / "manifest.json").write_text(json.dumps(manifest))

    registry = SkillRegistry()
    with patch("skills.registry.feral_home", return_value=tmp_path):
        assert registry.reload_skill("edited_skill") is True
        assert registry.skills["edited_skill"].description == "before"

        manifest["description"] = "after"
        (pkg_dir / "manifest.json").write_text(json.dumps(manifest))

        assert registry.reload_skill("edited_skill") is True
    assert registry.skills["edited_skill"].description == "after"


def test_reload_reports_a_package_it_cannot_parse(tmp_path):
    pkg_dir = tmp_path / "skills" / "broken_skill"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "manifest.json").write_text("{ not json")

    registry = SkillRegistry()
    with patch("skills.registry.feral_home", return_value=tmp_path):
        ok, code, reason = registry.reload_skill_detail("broken_skill")
    assert ok is False
    assert code == "unloadable"
    assert "broken_skill" in reason


# ── the route ────────────────────────────────────────────────────


@pytest.fixture
def client():
    registry = SkillRegistry()
    registry.load_builtin_skills()
    mock = MagicMock()
    mock.skill_registry = registry
    mock.skill_gen = None
    mock.skill_executor = None
    with patch("api.state.state", mock), patch("api.routes.skills.state", mock):
        from api.server import app
        yield TestClient(app, raise_server_exceptions=False), registry


def test_route_reports_a_reload_that_did_not_happen_as_a_failure(client):
    c, _registry = client
    r = c.post("/api/skills/reload", params={"skill_id": "weather_current"})

    # The whole point: a reload that changed nothing must not answer with a
    # success status, and must not answer with a body a generic client
    # reads as fine. Either alone was enough to hide this.
    assert r.status_code != 200, (
        "a reload that did nothing answered 200; every generic caller "
        "(including the v2 apiFetch error sniff) reads that as success"
    )
    body = r.json()
    assert body["ok"] is False
    assert body["error"]
    assert "weather_current" in body["error"]
    assert body["code"] == "no_source"


def test_route_reports_an_unknown_skill_as_a_failure(client):
    c, _registry = client
    r = c.post("/api/skills/reload", params={"skill_id": "no_such_skill"})
    assert r.status_code != 200
    body = r.json()
    assert body["ok"] is False
    assert body["error"]


def test_route_confirms_a_reload_that_did_happen(client):
    c, _registry = client
    r = c.post("/api/skills/reload", params={"skill_id": "calendar_google"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["skill_id"] == "calendar_google"
    assert "error" not in body


def test_route_answers_503_when_the_registry_is_missing():
    mock = MagicMock()
    mock.skill_registry = None
    with patch("api.state.state", mock), patch("api.routes.skills.state", mock):
        from api.server import app
        c = TestClient(app, raise_server_exceptions=False)
        r = c.post("/api/skills/reload", params={"skill_id": "anything"})
    assert r.status_code == 503
    assert r.json()["ok"] is False
    assert r.json()["error"]


def test_route_surfaces_a_registry_that_raises():
    registry = MagicMock()
    registry.reload_skill_detail.side_effect = RuntimeError("disk on fire")
    registry.reload_skill.side_effect = RuntimeError("disk on fire")
    mock = MagicMock()
    mock.skill_registry = registry
    with patch("api.state.state", mock), patch("api.routes.skills.state", mock):
        from api.server import app
        c = TestClient(app, raise_server_exceptions=False)
        r = c.post("/api/skills/reload", params={"skill_id": "anything"})
    assert r.status_code == 500
    assert r.json()["ok"] is False
    assert "disk on fire" in r.json()["error"]
