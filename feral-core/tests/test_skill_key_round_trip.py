"""A skill API key saved in the UI must be the key the executor uses.

The defect this closes
======================
Two operator surfaces wrote skill API keys and neither reached the
execution path:

* the v1 Settings page POSTed ``{"skill_keys": {id: key}}`` to
  ``/api/config/credentials``. The route handed it to
  ``ConfigLoader.save_credentials``, which parked it in
  ``ConfigLoader._credentials["skill_keys"]``. The only reader of that
  dict is ``ConfigLoader.get_skill_key``, called by no production code,
  only by two tests.
* the default (v2) UI had no surface at all.

``SkillExecutor._get_key`` meanwhile read ``blind_vault.retrieve(skill_id)``
or an in-process cache populated ONLY from ``FERAL_KEY_<SKILL_ID>`` env
vars at construction. So the single working way to give a skill its key
was an env var plus a brain restart, documented nowhere, while the route
comment claimed the executor read what it had just written.

These tests pin the round trip end to end: POST a key, then assert the
executor resolves that exact value, that it survives a restart (a fresh
executor over the same vault file), and that the response distinguishes
"stored" from "dropped".
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.no_auto_feral_home


def _executor(vault_path):
    from security.vault import BlindVault
    from skills.executor import SkillExecutor

    executor = SkillExecutor()
    executor.set_blind_vault(BlindVault(vault_path=str(vault_path)))
    return executor


def _mock_state(tmp_path):
    mock = MagicMock()
    mock.config = MagicMock()
    mock.config.save_credentials = MagicMock(return_value=True)
    mock.vault = None
    mock.provider_catalog = None
    mock.orchestrator = None
    mock.channel_manager = None
    mock.skill_executor = _executor(tmp_path / "credentials.json")
    return mock


@pytest.fixture
def client(tmp_path):
    mock = _mock_state(tmp_path)
    with patch("api.state.state", mock), \
         patch("api.routes.config.state", mock), \
         patch("api.routes.skills.state", mock):
        from api.server import app
        yield TestClient(app, raise_server_exceptions=False), mock, tmp_path


# ── executor-level round trip ────────────────────────────────────


def test_store_key_is_visible_to_get_key(tmp_path):
    executor = _executor(tmp_path / "credentials.json")
    assert executor._get_key("weather") is None

    assert executor.store_key("weather", "w-secret") is True
    assert executor._get_key("weather") == "w-secret"
    assert executor.has_key("weather") is True
    assert "weather" in executor.key_ids()


def test_stored_key_survives_a_restart(tmp_path):
    """A fresh executor over the same vault file resolves the key.

    This is the "no brain restart required, and no key lost on restart"
    half of the contract. The pre-fix path kept skill keys in a plain
    in-memory dict, so both halves failed.
    """
    _executor(tmp_path / "credentials.json").store_key("weather", "w-secret")

    reborn = _executor(tmp_path / "credentials.json")
    assert reborn._get_key("weather") == "w-secret"


def test_store_key_never_lands_in_the_provider_credential_namespace(tmp_path):
    """A skill called ``openai_api_key`` must not shadow the chat key."""
    from security.vault import BlindVault

    executor = _executor(tmp_path / "credentials.json")
    executor.store_key("openai_api_key", "skill-value")

    flat = BlindVault(vault_path=str(tmp_path / "credentials.json"))
    assert flat.retrieve("openai_api_key") is None
    assert executor._get_key("openai_api_key") == "skill-value"


def test_flat_namespace_keys_still_resolve(tmp_path):
    """Keys written by older builds keep working."""
    from security.vault import BlindVault
    from skills.executor import SkillExecutor

    vault = BlindVault(vault_path=str(tmp_path / "credentials.json"))
    vault.store("legacy_skill", "old-value")

    executor = SkillExecutor()
    executor.set_blind_vault(vault)
    assert executor._get_key("legacy_skill") == "old-value"


def test_remove_key_clears_both_layers(tmp_path):
    executor = _executor(tmp_path / "credentials.json")
    executor.store_key("weather", "w-secret")

    assert executor.remove_key("weather") is True
    assert executor._get_key("weather") is None

    reborn = _executor(tmp_path / "credentials.json")
    assert reborn._get_key("weather") is None


@pytest.mark.parametrize("skill_id,key", [("", "v"), ("weather", ""), ("weather", "   ")])
def test_store_key_refuses_blanks(tmp_path, skill_id, key):
    executor = _executor(tmp_path / "credentials.json")
    with pytest.raises(ValueError):
        executor.store_key(skill_id, key)


# ── HTTP round trip: v1 surface ──────────────────────────────────


def test_config_credentials_skill_key_reaches_the_executor(client):
    c, mock, _tmp = client
    r = c.post("/api/config/credentials", json={"skill_keys": {"weather": "w-secret"}})
    assert r.status_code == 200
    body = r.json()
    assert body["skill_keys_saved"] == ["weather"]
    assert body["skill_keys_rejected"] == []
    # The whole point: the executor the brain runs tool calls through
    # resolves the value that was just typed into Settings.
    assert mock.skill_executor._get_key("weather") == "w-secret"


def test_config_credentials_skill_key_is_not_reported_as_an_env_key(client):
    c, _mock, _tmp = client
    r = c.post("/api/config/credentials", json={"skill_keys": {"weather": "w-secret"}})
    assert r.json()["keys_saved"] == []


def test_config_credentials_reports_dropped_skill_keys(tmp_path):
    """No executor means nothing can read the key. Say so, don't 200 blindly."""
    mock = _mock_state(tmp_path)
    mock.skill_executor = None
    with patch("api.state.state", mock), patch("api.routes.config.state", mock):
        from api.server import app

        c = TestClient(app, raise_server_exceptions=False)
        body = c.post(
            "/api/config/credentials", json={"skill_keys": {"weather": "w"}},
        ).json()
    assert body["skill_keys_saved"] == []
    assert body["skill_keys_rejected"] == ["weather"]


# ── HTTP round trip: v2 surface ──────────────────────────────────


def test_post_skill_key_route_round_trip(client):
    c, mock, _tmp = client
    r = c.post("/api/skills/weather/key", json={"key": "w-secret"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["persisted"] is True
    assert body["has_key"] is True
    assert mock.skill_executor._get_key("weather") == "w-secret"


def test_post_skill_key_rejects_empty_value(client):
    c, mock, _tmp = client
    body = c.post("/api/skills/weather/key", json={"key": "  "}).json()
    assert body["ok"] is False
    assert "key is required" in body["error"]
    assert mock.skill_executor._get_key("weather") is None


def test_skill_key_routes_never_echo_the_secret(client):
    c, _mock, _tmp = client
    c.post("/api/skills/weather/key", json={"key": "w-secret"})
    for path in ("/api/skills/keys",):
        assert "w-secret" not in c.get(path).text


def test_get_skill_keys_lists_configured_ids(client):
    c, mock, _tmp = client
    mock.skill_registry.skills = {}
    c.post("/api/skills/weather/key", json={"key": "w-secret"})
    body = c.get("/api/skills/keys").json()
    assert body["ok"] is True
    assert "weather" in body["configured"]


def test_delete_skill_key_route(client):
    c, mock, _tmp = client
    c.post("/api/skills/weather/key", json={"key": "w-secret"})
    body = c.delete("/api/skills/weather/key").json()
    assert body["removed"] is True
    assert body["has_key"] is False
    assert mock.skill_executor._get_key("weather") is None
