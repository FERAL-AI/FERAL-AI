"""``POST /api/supervisor/pause`` must not coerce its way into pausing.

The pre-fix handler took a bare ``dict`` and ran::

    paused = bool((body or {}).get("paused", False))

That is Python truthiness applied to a JSON document. ``{"paused": "no"}``
and ``{"paused": "false"}`` are non-empty strings, so both coerced to
``True`` and stopped the brain. The v2 client always sends a real boolean,
which made this latent rather than live, but it is the kill switch: the
wire type has to be exactly ``true`` or ``false``.

A non-dict body used to fall out of FastAPI as a 422 with a nested
``loc``/``msg``/``input`` array. These tests also pin the 400-with-a-
sentence replacement.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from agents.supervisor import Supervisor, SupervisorStore

pytestmark = pytest.mark.no_auto_feral_home


@pytest.fixture
def client(tmp_path):
    store = SupervisorStore(db_path=str(tmp_path / "sup.db"))
    supervisor = Supervisor(store=store)
    mock = MagicMock()
    mock.supervisor = supervisor
    with patch("api.state.state", mock), patch("api.routes.supervisor.state", mock):
        from api.server import app
        yield TestClient(app, raise_server_exceptions=False), supervisor


@pytest.mark.parametrize("value", ["no", "false", "true", "0", "off", "yes"])
def test_string_paused_is_rejected_not_coerced(client, value):
    """The headline case. Every one of these used to pause the brain."""
    c, sup = client
    assert sup.paused is False

    r = c.post("/api/supervisor/pause", json={"paused": value})

    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert detail["field"] == "paused"
    assert detail["code"] == "invalid_paused"
    assert repr(value) in detail["message"]
    # The kill switch did not move.
    assert sup.paused is False


@pytest.mark.parametrize("value", [1, 0, 1.0, [], {}, None, "1"])
def test_non_boolean_paused_is_rejected(client, value):
    c, sup = client
    r = c.post("/api/supervisor/pause", json={"paused": value})
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["field"] == "paused"
    assert sup.paused is False


def test_missing_paused_is_rejected(client):
    """``.get("paused", False)`` used to silently mean "unpause"."""
    c, sup = client
    sup.set_paused(True)
    r = c.post("/api/supervisor/pause", json={})
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["field"] == "paused"
    # An unreadable request must not change the switch in either direction.
    assert sup.paused is True


def test_non_object_body_gets_a_sentence_not_a_422(client):
    c, sup = client
    r = c.post("/api/supervisor/pause", json=["paused"])
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "invalid_body"
    assert "paused" in detail["message"]
    assert sup.paused is False


def test_unparseable_body_gets_a_sentence(client):
    c, sup = client
    r = c.post(
        "/api/supervisor/pause",
        content=b"{not json",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["code"] == "invalid_json"
    assert sup.paused is False


def test_real_booleans_still_work_both_ways(client):
    c, sup = client

    r = c.post("/api/supervisor/pause", json={"paused": True})
    assert r.status_code == 200, r.text
    assert r.json()["paused"] is True
    assert sup.paused is True

    r = c.post("/api/supervisor/pause", json={"paused": False})
    assert r.status_code == 200, r.text
    assert r.json()["paused"] is False
    assert sup.paused is False


def test_extra_keys_are_ignored_not_fatal(client):
    """Forward compatibility: only ``paused`` is load-bearing here."""
    c, sup = client
    r = c.post("/api/supervisor/pause", json={"paused": True, "reason": "manual"})
    assert r.status_code == 200, r.text
    assert sup.paused is True
