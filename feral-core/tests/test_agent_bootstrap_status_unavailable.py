"""Passive unsupported continuation status is inspectable without startup I/O."""
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from fastapi import FastAPI

from api.routes.agent_bootstrap import create_agent_bootstrap_router


def app_for(provider):
    app = FastAPI()
    app.include_router(create_agent_bootstrap_router(provider))
    return app


@pytest.mark.asyncio
async def test_missing_controller_passive_get_has_conservative_native_contract():
    provider = Mock(return_value=None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(provider)), base_url="http://unit.test") as client:
        response = await client.get("/api/security/agent-bootstrap/status")
    assert response.status_code == 200
    status = response.json()
    assert status["supported"] is False
    assert status["phase"] in {"pending", "starting", "ready", "failed", "cleanup_failed"}
    for key in ("agent_ready", "in_flight", "restart_required", "credentials_available", "memory_available", "orchestrator_available"):
        assert status[key] is False
    assert type(status["bootstrap_required"]) is bool
    assert status["code"] == "continuation_unsupported"
    provider.assert_called_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("path,body", [
    ("/api/security/agent-bootstrap/review", {}),
    ("/api/security/agent-bootstrap", {"review_token": "a" * 43}),
    ("/api/security/agent-bootstrap/cancel", {"review_token": "a" * 43}),
])
async def test_missing_controller_mutations_remain_unavailable(path, body):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(lambda: None)), base_url="http://unit.test") as client:
        response = await client.post(path, json=body)
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "continuation_unsupported"


@pytest.mark.asyncio
async def test_existing_controller_passive_status_does_not_call_action_methods():
    status = {"supported": False, "phase": "pending", "agent_ready": False}
    controller = SimpleNamespace(status=Mock(return_value=status), review=Mock(side_effect=AssertionError("no review")), confirm=Mock(side_effect=AssertionError("no confirmation")), cancel=Mock(side_effect=AssertionError("no cancel")))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(lambda: controller)), base_url="http://unit.test") as client:
        response = await client.get("/api/security/agent-bootstrap/status")
    assert response.status_code == 200 and response.json() == status
    controller.status.assert_called_once_with()
    controller.review.assert_not_called()
    controller.confirm.assert_not_called()
    controller.cancel.assert_not_called()
