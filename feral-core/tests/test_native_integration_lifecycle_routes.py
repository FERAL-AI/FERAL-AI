"""Runtime teardown contracts. No live listeners, subprocesses, or accounts."""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Response
import httpx

from api.routes import channels, mcp


@pytest.fixture
def managers(monkeypatch):
    cm = SimpleNamespace(_channels={})
    mm = SimpleNamespace(_servers={}, _server_configs={}, _degraded_servers={})
    monkeypatch.setattr(channels.state, "channel_manager", cm)
    monkeypatch.setattr(mcp.state, "mcp_client", mm)
    return cm, mm


class Connection:
    def __init__(self, effect=None):
        self._connected = True
        self.calls = 0
        self.effect = effect

    async def disconnect(self):
        self.calls += 1
        if self.effect:
            await self.effect()
        self._connected = False


class Listener:
    def __init__(self, effect=None):
        self._running = True
        self._bg_tasks = set()
        self.calls = 0
        self.effect = effect

    async def stop(self):
        self.calls += 1
        if self.effect:
            await self.effect()
        self._running = False


@pytest.mark.asyncio
async def test_registered_http_routes_accept_exact_bodies(managers):
    cm, mm = managers
    cm._channels["slack"] = Listener()
    mm._servers["local-tools"] = Connection()
    app = FastAPI()
    app.include_router(channels.router)
    app.include_router(mcp.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        stopped = await client.post("/api/channels/stop", json={"type": "slack"})
        closed = await client.post("/api/mcp/disconnect", json={"name": "local-tools"})
    assert stopped.status_code == closed.status_code == 200
    assert stopped.json()["stopped"] is True
    assert closed.json()["disconnected"] is True
    for result in (stopped.json(), closed.json()):
        assert result["scope"] == "runtime_only"
        assert result["credentials_revoked"] is False
        assert result["saved_configuration_changed"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, "", "../../secret", "x" * 129, 123, "token\n"])
async def test_invalid_identifiers_never_dispatch(managers, value):
    for route, key in ((channels.stop_channel, "type"), (mcp.mcp_disconnect, "name")):
        response = Response()
        result = await route({key: value}, response)
        assert response.status_code == 422
        assert result.get("ok", result.get("success")) is False


@pytest.mark.asyncio
async def test_absent_and_unsupported_managers(monkeypatch):
    for module, attribute, route, key in (
        (channels, "channel_manager", channels.stop_channel, "type"),
        (mcp, "mcp_client", mcp.mcp_disconnect, "name"),
    ):
        for manager, expected in ((None, 503), (object(), 501)):
            monkeypatch.setattr(module.state, attribute, manager)
            response = Response()
            await route({key: "slack"}, response)
            assert response.status_code == expected


@pytest.mark.asyncio
async def test_missing_runtime_not_success(managers):
    for route, key in ((channels.stop_channel, "type"), (mcp.mcp_disconnect, "name")):
        response = Response()
        await route({key: "unknown"}, response)
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_swallowed_http_close_failure_not_reported_as_success(managers):
    cm, mm = managers
    listener = Listener()
    connection = Connection()
    listener._http = SimpleNamespace(is_closed=False)
    connection._http_client = SimpleNamespace(is_closed=False)
    cm._channels["slack"] = listener
    mm._servers["local"] = connection
    for route, body in ((channels.stop_channel, {"type": "slack"}),
                        (mcp.mcp_disconnect, {"name": "local"})):
        response = Response()
        result = await route(body, response)
        assert response.status_code == 502
        assert result["runtime_outcome"] == "uncertain"
    assert cm._channels["slack"] is listener
    assert mm._servers["local"] is connection


@pytest.mark.asyncio
async def test_mcp_failure_keeps_registry_and_redacts_errors(managers):
    _, manager = managers
    async def fail():
        raise RuntimeError("SECRET_CREDENTIAL")
    connection = Connection(fail)
    manager._servers["local"] = connection
    manager._server_configs["local"] = {"env": {"TOKEN": "secret"}}
    response = Response()
    result = await mcp.mcp_disconnect({"name": "local"}, response)
    assert response.status_code == 502
    assert manager._servers["local"] is connection
    assert manager._server_configs["local"]["env"]["TOKEN"] == "secret"
    assert "SECRET" not in str(result)


@pytest.mark.asyncio
async def test_mcp_replacement_survives_await(managers):
    _, manager = managers
    replacement = Connection()
    async def replace():
        manager._servers["local"] = replacement
        manager._server_configs["local"] = {"replacement": True}
    old = Connection(replace)
    manager._servers["local"] = old
    response = Response()
    result = await mcp.mcp_disconnect({"name": "local"}, response)
    assert response.status_code == 409
    assert result["captured_connection_closed"] is True
    assert manager._servers["local"] is replacement
    assert replacement.calls == 0
    assert manager._server_configs["local"] == {"replacement": True}


@pytest.mark.asyncio
async def test_mcp_reaps_only_captured_child(managers):
    _, manager = managers
    class Process:
        returncode = None
        calls = 0
        async def wait(self):
            self.calls += 1
            self.returncode = -9
    connection = Connection()
    process = Process()
    connection._process = process
    manager._servers["local"] = connection
    result = await mcp.mcp_disconnect({"name": "local"}, Response())
    assert result["success"] is True
    assert process.calls == 1


@pytest.mark.asyncio
async def test_channel_failure_keeps_registration_and_redacts_errors(managers):
    manager, _ = managers
    async def fail():
        raise RuntimeError("SECRET_TOKEN")
    listener = Listener(fail)
    manager._channels["slack"] = listener
    response = Response()
    result = await channels.stop_channel({"type": "slack"}, response)
    assert response.status_code == 502
    assert manager._channels["slack"] is listener
    assert "SECRET" not in str(result)


@pytest.mark.asyncio
async def test_channel_replacement_survives_await(managers):
    manager, _ = managers
    replacement = Listener()
    async def replace():
        manager._channels["slack"] = replacement
    old = Listener(replace)
    manager._channels["slack"] = old
    response = Response()
    result = await channels.stop_channel({"type": "slack"}, response)
    assert response.status_code == 409
    assert result["captured_channel_stopped"] is True
    assert manager._channels["slack"] is replacement
    assert replacement._running is True
    assert replacement.calls == 0


@pytest.mark.asyncio
async def test_stop_drains_poll_and_background_but_not_shared_task(managers):
    manager, _ = managers
    drained = []
    async def work(name):
        try:
            await asyncio.Event().wait()
        finally:
            drained.append(name)
    listener = Listener()
    poll = asyncio.create_task(work("poll"))
    gateway = asyncio.create_task(work("gateway"))
    shared = asyncio.create_task(work("shared"))
    await asyncio.sleep(0)
    listener._poll_task = poll
    listener._bg_tasks.add(gateway)
    manager._channels["discord"] = listener
    try:
        result = await channels.stop_channel({"type": "discord"}, Response())
        assert result["ok"] is True
        assert set(drained) == {"poll", "gateway"}
        assert not shared.done()
        assert listener.calls == 1
        assert "discord" not in manager._channels
    finally:
        shared.cancel()
        await asyncio.gather(shared, return_exceptions=True)
