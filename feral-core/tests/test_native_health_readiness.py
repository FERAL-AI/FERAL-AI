"""Health is passive reachability, not an encrypted-memory readiness claim."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytestmark = pytest.mark.no_auto_feral_home


@pytest.mark.asyncio
@pytest.mark.parametrize("memory,orchestrator,bootstrap,vault_state,expected,ready", [
    (None, None, True, "locked", "locked", False),
    (None, None, True, "unlocking", "locked", False),
    (None, None, True, "unavailable", "degraded", False),
    (object(), None, True, "ready", "degraded", False),
    (object(), object(), True, "ready", "degraded", False),
    (object(), object(), False, "locked", "ok", True),
    (object(), object(), False, "ready", "ok", True),
])
async def test_passive_readiness(monkeypatch, memory, orchestrator, bootstrap, vault_state, expected, ready):
    from api.routes import dashboard
    coordinator = SimpleNamespace(status=Mock(return_value={"state": vault_state, "code": "fixture", "credentials_available": vault_state == "ready", "in_flight": vault_state == "unlocking"}), require_ready=Mock(side_effect=AssertionError("Health must not unlock")))
    monkeypatch.setattr(dashboard, "state", SimpleNamespace(memory=memory, orchestrator=orchestrator, _native_bootstrap_required=bootstrap, vault_coordinator=coordinator))
    result = await dashboard.health()
    assert result["status"] == expected
    assert result["agent_ready"] is ready
    assert result["memory_available"] is (memory is not None)
    assert result["service_reachable"] is True
    assert result["bootstrap_required"] is bootstrap
    coordinator.status.assert_called_once_with()
    coordinator.require_ready.assert_not_called()


@pytest.mark.asyncio
async def test_missing_passive_status_is_unknown_not_ready(monkeypatch):
    from api.routes import dashboard
    coordinator = SimpleNamespace(status=Mock(side_effect=RuntimeError("private keychain detail")))
    monkeypatch.setattr(dashboard, "state", SimpleNamespace(memory=None, orchestrator=None, vault_coordinator=coordinator))
    result = await dashboard.health()
    assert result["status"] == "degraded" and result["agent_ready"] is False
    assert result["vault"]["state"] == "unavailable"
    assert "private keychain" not in str(result)


@pytest.mark.asyncio
async def test_legacy_does_not_query_vault(monkeypatch):
    from api.routes import dashboard
    legacy = SimpleNamespace(retrieve=Mock(side_effect=AssertionError("No key queries")))
    monkeypatch.setattr(dashboard, "state", SimpleNamespace(memory=object(), orchestrator=object(), vault=legacy, vault_coordinator=None))
    result = await dashboard.health()
    assert result["status"] == "ok" and result["agent_ready"] is True
    assert result["vault"]["credentials_available"] is None
    legacy.retrieve.assert_not_called()
