"""v2026.5.26 autonomy mode persistence, and the second gate it missed.

Pre-fix: ``POST /api/autonomy {mode}`` only updated the in-memory
``ToolRunner._autonomy_mode``. The choice never landed in
``~/.feral/settings.json``, so the next brain restart reverted to
"hybrid" (or whatever ``FERAL_AUTONOMY`` env var pinned). Operator's
WebUI Settings -> Autonomy pick was effectively a no-op across
sessions (operator screenshot 3 shows "loose" active, but it was
gone after restart).

Pre-fix: ``POST /api/autonomy {mode}`` only updated the in-memory
``ToolRunner._autonomy_mode``. The choice never landed in
``~/.feral/settings.json``, so the next brain restart reverted.

Second defect, same endpoint, found later: the tier gates two things.
``ToolRunner`` gates tool approvals from its own attribute, and
``security/exec_mode.current_autonomy_mode()`` gates the shell path from
``FERAL_AUTONOMY`` and nothing else. The route rewrote the first and the
settings file, never the env var, so moving to ``strict`` tightened the
approval gate immediately and left the shell gate loose until restart.

Tests:
* POST persists to settings.json via state.config.update_settings
* GET returns the live ToolRunner value
* Invalid mode rejected without persisting
* `update_settings` failure doesn't roll back the live state
* The boot-time load path picks settings.json over the default
* POST moves ``FERAL_AUTONOMY``, so both gates agree immediately
* It moves even when the disk write throws
* It does not move for a rejected mode
"""
from __future__ import annotations

import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import MagicMock, patch


def _make_state_with_orchestrator(mode: str = "hybrid"):
    """Build a fake BrainState shell sufficient for the autonomy route
    + a no-op ConfigLoader stub for update_settings."""
    fake = MagicMock()
    fake.orchestrator = MagicMock()
    fake.orchestrator.tool_runner = MagicMock()
    fake.orchestrator.tool_runner._autonomy_mode = mode

    def set_mode(value: str) -> str:
        fake.orchestrator.tool_runner._autonomy_mode = value
        # mirror the property contract so GET reads back the new value
        type(fake.orchestrator.tool_runner).autonomy_mode = property(
            lambda self: self._autonomy_mode
        )
        return value

    fake.orchestrator.tool_runner.set_autonomy_mode = set_mode
    # Read the in-memory value via the same `autonomy_mode` attribute
    # the route handler uses.
    fake.orchestrator.tool_runner.autonomy_mode = mode

    fake.config = MagicMock()
    fake.config.update_settings = MagicMock(return_value=None)
    return fake


def _client():
    from api.routes.timeline import router
    app = FastAPI()
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


def test_post_autonomy_persists_to_settings_json():
    fake = _make_state_with_orchestrator(mode="hybrid")
    with patch("api.routes.timeline.state", fake):
        c = _client()
        r = c.post("/api/autonomy", json={"mode": "loose"})
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["mode"] == "loose"
    assert body["persisted"] is True
    # The live ToolRunner was updated.
    fake.orchestrator.tool_runner.set_autonomy_mode  # function called
    # AND the persist call landed.
    fake.config.update_settings.assert_called_once_with(
        "security", "autonomy_mode", "loose",
    )


def test_get_autonomy_returns_live_runner_value():
    fake = _make_state_with_orchestrator(mode="strict")
    with patch("api.routes.timeline.state", fake):
        c = _client()
        r = c.get("/api/autonomy")
    assert r.status_code == 200
    assert r.json()["mode"] == "strict"


def test_invalid_mode_returns_error_and_does_not_persist():
    fake = _make_state_with_orchestrator(mode="hybrid")
    with patch("api.routes.timeline.state", fake):
        c = _client()
        r = c.post("/api/autonomy", json={"mode": "yolo"})
    # Handler returns 200 with an error key (existing contract — not
    # changed in v2026.5.26). What MUST be true: nothing persisted.
    assert "error" in r.json()
    fake.config.update_settings.assert_not_called()


def test_persist_failure_does_not_roll_back_live_state():
    # Disk write fails → live mode still flipped (so the operator
    # gets the immediate UX they clicked for) but `persisted=False`
    # tells the client to surface a "restart will revert" hint.
    fake = _make_state_with_orchestrator(mode="hybrid")
    fake.config.update_settings.side_effect = OSError("read-only fs")
    with patch("api.routes.timeline.state", fake):
        c = _client()
        r = c.post("/api/autonomy", json={"mode": "loose"})
    body = r.json()
    assert body["success"] is True
    assert body["mode"] == "loose"
    assert body["persisted"] is False  # honest about the disk failure
    # In-memory flip still happened.
    assert fake.orchestrator.tool_runner._autonomy_mode == "loose"


@pytest.mark.parametrize("mode", ["strict", "hybrid", "loose"])
def test_post_autonomy_moves_the_shell_gate_too(mode, monkeypatch):
    """Both gates must read the new tier without a restart.

    ``security.exec_mode`` is deliberately imported here rather than
    mocked: the assertion is that the module the shell path actually
    consults returns the tier the operator just picked.
    """
    from security.exec_mode import current_autonomy_mode

    monkeypatch.setenv("FERAL_AUTONOMY", "hybrid" if mode != "hybrid" else "loose")
    fake = _make_state_with_orchestrator(mode="hybrid")
    with patch("api.routes.timeline.state", fake):
        c = _client()
        r = c.post("/api/autonomy", json={"mode": mode})
    assert r.json()["mode"] == mode
    assert os.environ["FERAL_AUTONOMY"] == mode
    assert current_autonomy_mode() == mode


def test_shell_gate_moves_even_when_the_disk_write_fails(monkeypatch):
    """The env write is ahead of the persist and independent of it.

    A read-only settings file is a reason to warn about the restart
    reverting, not a reason to leave the shell running at the old tier
    for the rest of the session.
    """
    from security.exec_mode import current_autonomy_mode

    monkeypatch.setenv("FERAL_AUTONOMY", "loose")
    fake = _make_state_with_orchestrator(mode="loose")
    fake.config.update_settings.side_effect = OSError("read-only fs")
    with patch("api.routes.timeline.state", fake):
        c = _client()
        r = c.post("/api/autonomy", json={"mode": "strict"})
    assert r.json()["persisted"] is False
    assert current_autonomy_mode() == "strict"


def test_rejected_mode_does_not_move_the_shell_gate(monkeypatch):
    monkeypatch.setenv("FERAL_AUTONOMY", "strict")
    fake = _make_state_with_orchestrator(mode="strict")
    with patch("api.routes.timeline.state", fake):
        c = _client()
        r = c.post("/api/autonomy", json={"mode": "yolo"})
    assert "error" in r.json()
    assert os.environ["FERAL_AUTONOMY"] == "strict"


def test_settings_write_re_exports_the_env_var(tmp_path, monkeypatch):
    """The generic funnel, not just this one route.

    ``ConfigLoader.update_settings`` is the single write path behind the
    WebUI, the gateway ``config.set`` method, the ``system_settings``
    skill and the audio config route. Before this, every one of them
    persisted a value whose only reader was an env var and left that env
    var stale until the next boot.
    """
    from config.loader import ConfigLoader

    monkeypatch.delenv("FERAL_AUTONOMY", raising=False)
    monkeypatch.delenv("FERAL_TTS_VOICE", raising=False)
    loader = ConfigLoader()
    loader.user_home = tmp_path
    loader.discover()

    changed = loader.update_settings("security", "autonomy_mode", "strict")
    assert "FERAL_AUTONOMY" in changed
    assert os.environ["FERAL_AUTONOMY"] == "strict"

    # A write to an unrelated section touches only its own env var.
    changed = loader.update_settings("audio", "tts_voice", "shimmer")
    assert changed == ("FERAL_TTS_VOICE",)
    assert os.environ["FERAL_TTS_VOICE"] == "shimmer"
    assert os.environ["FERAL_AUTONOMY"] == "strict"


def test_settings_write_never_re_exports_a_secret(tmp_path, monkeypatch):
    """Channel tokens and provider keys stay out of process-global env.

    ``api/state.py::_should_export_runtime_env_key`` already refuses
    them at boot; the runtime re-export must not be the hole that lets
    them in.
    """
    from config.loader import ConfigLoader

    monkeypatch.delenv("NODE_API_KEY", raising=False)
    loader = ConfigLoader()
    loader.user_home = tmp_path
    loader.discover()

    changed = loader.update_settings("security", "node_api_key", "s3cret")
    assert "NODE_API_KEY" not in changed
    assert "NODE_API_KEY" not in os.environ


def test_autonomy_load_at_boot_prefers_settings_json_over_default():
    """The state.py boot path reads
    config.get("security", "autonomy_mode") and calls set_autonomy_mode
    when the persisted value differs from the default. This test
    simulates that boot sequence in isolation.
    """
    fake = _make_state_with_orchestrator(mode="hybrid")

    # ConfigLoader.get returns the persisted value.
    fake.config.get = MagicMock(return_value="loose")
    # Inline the boot-load logic — mirrors what api/state.py does
    # after orchestrator construction (the env var is empty here, so
    # the persisted value wins).
    import os
    os.environ.pop("FERAL_AUTONOMY", None)
    persisted = fake.config.get("security", "autonomy_mode") or ""
    if persisted.strip().lower() in ("strict", "hybrid", "loose"):
        fake.orchestrator.tool_runner.set_autonomy_mode(persisted)

    assert fake.orchestrator.tool_runner._autonomy_mode == "loose"
