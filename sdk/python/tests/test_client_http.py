"""SDK HTTP contracts against registered routes plus transport failure cases.

Synchronous wrappers execute coroutines even without pytest-asyncio. Server
imports and route state use a disposable home; no lifespan or accounts are run.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import httpx
import pytest

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "sdk/python"))
sys.path.insert(0, str(REPO / "feral-core"))

from feral_sdk.client import FeralClient


def run(awaitable):
    return asyncio.run(awaitable)


@pytest.fixture
def server_contract(monkeypatch, tmp_path):
    monkeypatch.setenv("FERAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FERAL_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("FERAL_API_KEY", "sdk-test-credential")
    monkeypatch.setenv("FERAL_LOCAL_BYPASS", "0")
    from api import server
    from api.routes import conversations, memory, tools
    from fastapi import FastAPI
    from models.skill_manifest import BrandProfile, SkillEndpoint, SkillManifest
    from skills.call_context import current_context
    from skills.registry import SkillRegistry

    monkeypatch.setattr(server, "FERAL_API_KEY", "sdk-test-credential")
    registry = SkillRegistry()
    registry.load_builtin_skills()
    for skill_id, endpoint_id, tier in [
        ("sdk_confirm", "act", "confirm"), ("sdk_deny", "act", "deny"),
    ]:
        registry.register(SkillManifest(
            skill_id=skill_id, brand=BrandProfile(name=skill_id, primary_color="#111"),
            description="Disposable SDK fixture",
            endpoints=[SkillEndpoint(id=endpoint_id, method="PYTHON", url=f"python://{skill_id}/{endpoint_id}",
                                     description="Fixture endpoint", safety_tier=tier)],
        ))
    calls = []
    reads = []

    class Memory:
        last_search_degradations = []

        async def search_all(self, query, limit):
            reads.append(("search", query, limit))
            return [{"id": "fixture-note", "tier": "notes", "content": "Disposable record"}]

        async def conversation_page(self, *, limit, offset, query):
            reads.append(("conversations", limit, offset, query))
            return {"items": [{"id": "fixture-thread", "title": "Disposable thread"}],
                    "total": 1, "limit": limit, "offset": offset, "has_more": False}

    class Executor:
        async def execute(self, tool_name, args, manifest, endpoint):
            calls.append((tool_name, args, current_context()))
            return {"success": True, "status_code": 200, "data": {"echo": args}, "error": None}

    monkeypatch.setattr(tools.state, "skill_registry", registry)
    monkeypatch.setattr(tools.state, "orchestrator", SimpleNamespace(executor=Executor()))
    monkeypatch.setattr(tools.state, "memory", Memory())
    app = FastAPI()
    app.add_middleware(server.APIKeyMiddleware)
    # Use the actual registration functions for dual SPA/JSON endpoints.
    app.add_api_route("/health", server.health_page_or_json, methods=["GET"])
    app.add_api_route("/skills", server.skills_page_or_json, methods=["GET"])
    app.include_router(tools.router)
    app.include_router(memory.router)
    app.include_router(conversations.router)
    transport = httpx.ASGITransport(app=app, client=("192.0.2.10", 5000))
    return SimpleNamespace(server=server, transport=transport, calls=calls, reads=reads, registry=registry)


def test_registered_routes_health_skills_and_invocation(server_contract):
    async def scenario():
        async with FeralClient("http://brain", bearer_token="sdk-test-credential",
                               transport=server_contract.transport) as client:
            health = await client.health()
            assert health["service_reachable"] is True
            assert "agent_ready" in health
            skills = await client.list_skills()
            assert any(row["skill_id"] == "notes_memory" for row in skills)
            result = await client.invoke_skill("notes_memory", "search_notes", {"query": "fixture"},
                                               session_id="sdk-session-a")
            assert result["success"] is True
            assert result["tool_name"] == "notes_memory__search_notes"
            assert server_contract.calls[-1][2].session_id == "sdk-session-a"
            assert server_contract.calls[-1][2].surface == "http_api"
            assert (await client.search_memory("fixture", limit=3))[0]["id"] == "fixture-note"
            assert (await client.list_conversations(limit=7))[0]["id"] == "fixture-thread"
    run(scenario())
    routes = {(r.path, method) for r in server_contract.server.app.routes for method in getattr(r, "methods", [])}
    assert {("/health", "GET"), ("/skills", "GET"), ("/api/tools/execute", "POST"),
            ("/api/dashboard", "GET"), ("/api/system/info", "GET"),
            ("/api/memory/search", "GET"), ("/api/conversations", "GET")} <= routes
    assert ("/api/notes", "POST") not in routes
    assert ("/api/skills", "GET") not in routes
    assert server_contract.reads == [("search", "fixture", 3), ("conversations", 7, 0, "")]


@pytest.mark.parametrize("credential", [None, "wrong-sdk-credential"])
def test_real_middleware_rejects_remote_without_valid_credential(server_contract, credential):
    async def scenario():
        async with FeralClient("http://brain", bearer_token=credential,
                               transport=server_contract.transport) as client:
            assert (await client.health())["service_reachable"] is True
            with pytest.raises(httpx.HTTPStatusError) as error:
                await client.invoke_skill("notes_memory", "search_notes")
            assert error.value.response.status_code == 401
    run(scenario())
    assert server_contract.calls == []


def test_confirmation_denial_and_unknown_are_application_results(server_contract):
    async def scenario():
        async with FeralClient("http://brain", bearer_token="sdk-test-credential",
                               transport=server_contract.transport) as client:
            needs_review = await client.invoke_skill("sdk_confirm", "act", {"fixture": 1},
                                                   session_id="sdk-reviewed-session")
            assert needs_review["success"] is False
            assert needs_review["status_code"] == 412
            assert needs_review["policy"]["level"] == "confirm"
            assert server_contract.calls == []  # No automatic confirmation or retry.
            approved = await client.invoke_skill("sdk_confirm", "act", {"fixture": 1}, confirm=True,
                                               session_id="sdk-reviewed-session")
            assert approved["success"] is True
            assert len(server_contract.calls) == 1
            assert server_contract.calls[0][2].session_id == "sdk-reviewed-session"
            denied = await client.invoke_skill("sdk_deny", "act", confirm=True)
            assert denied["success"] is False and denied["status_code"] == 403
            missing = await client.invoke_skill("unknown_sdk_skill", "act")
            assert missing["success"] is False and missing["status_code"] == 404
            assert len(server_contract.calls) == 1
    run(scenario())


@pytest.mark.parametrize("session_id", [None, "", " leading", "trailing ", "private\nidentity",
                                       "private\u0085identity", "x" * 1025, True, 1])
def test_missing_or_invalid_caller_session_cannot_dispatch_reviewed_action(server_contract, session_id):
    async def scenario():
        async with FeralClient("http://brain", bearer_token="sdk-test-credential",
                               session_id="different-chat-session",
                               transport=server_contract.transport) as client:
            result = await client.invoke_skill("sdk_confirm", "act", {"fixture": 1},
                                               confirm=True, session_id=session_id)
            assert result["success"] is False and result["status_code"] == 422
            assert result["error_code"] == "context_invalid_session"
            assert server_contract.calls == []
    run(scenario())


def test_create_note_uses_registered_skill_and_session(server_contract):
    async def scenario():
        async with FeralClient("http://brain", bearer_token="sdk-test-credential",
                               transport=server_contract.transport) as client:
            result = await client.create_note("Disposable note", ["fixture"], session_id="sdk-session-b")
            assert result["success"] is True
            assert result["tool_name"] == "notes_memory__save_note"
    run(scenario())
    tool, args, context = server_contract.calls[0]
    assert tool == "notes_memory__save_note"
    assert args == {"content": "Disposable note", "tags": ["fixture"]}
    assert context.session_id == "sdk-session-b"


def test_every_http_method_raises_status_failure_before_json():
    methods = [
        lambda c: c.health(), lambda c: c.get_dashboard(), lambda c: c.get_system_info(),
        lambda c: c.list_skills(), lambda c: c.search_memory("q"), lambda c: c.create_note("x"),
        lambda c: c.list_conversations(), lambda c: c.invoke_skill("s", "e"),
    ]
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(503, text="Non-JSON server error")

    async def scenario():
        async with FeralClient(transport=httpx.MockTransport(respond)) as client:
            for method in methods:
                with pytest.raises(httpx.HTTPStatusError) as error:
                    await method(client)
                assert error.value.response.status_code == 503
    run(scenario())
    assert len(calls) == len(methods)


@pytest.mark.parametrize("payload", [b"<html>dashboard</html>", b"null", b"[]", b'"string"'])
def test_malformed_object_response_is_not_success(payload):
    async def scenario():
        transport = httpx.MockTransport(lambda request: httpx.Response(200, content=payload))
        async with FeralClient(transport=transport) as client:
            with pytest.raises(ValueError) as error:
                await client.invoke_skill("s", "e")
            assert payload.decode() not in str(error.value)
    run(scenario())


@pytest.mark.parametrize("method", ["list_skills", "search_memory", "list_conversations"])
def test_malformed_record_lists_are_not_empty_success(method):
    async def scenario():
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"error": "fixture"}))
        async with FeralClient(transport=transport) as client:
            with pytest.raises(ValueError):
                await (client.search_memory("q") if method == "search_memory" else getattr(client, method)())
    run(scenario())


@pytest.mark.parametrize("failure", [httpx.ReadTimeout, httpx.ConnectError])
def test_transport_failure_propagates_without_retry(failure):
    calls = []

    def respond(request):
        calls.append(request)
        raise failure("Disposable transport failure", request=request)

    async def scenario():
        async with FeralClient(timeout=0.25, transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(failure):
                await client.invoke_skill("s", "e")
    run(scenario())
    assert len(calls) == 1
    assert calls[0].extensions["timeout"]["read"] == 0.25


def test_request_shape_credentials_and_no_redirect_following():
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"success": False, "status_code": 403, "error": "fixture refusal"})

    async def scenario():
        async with FeralClient("https://brain/", bearer_token="fixture-bearer", transport=httpx.MockTransport(respond)) as client:
            assert client._http.follow_redirects is False
            result = await client.invoke_skill("s", "e")
            assert result == {"success": False, "status_code": 403, "error": "fixture refusal"}
    run(scenario())
    assert len(calls) == 1
    assert calls[0].url.path == "/api/tools/execute"
    assert calls[0].headers["authorization"] == "Bearer fixture-bearer"
    assert calls[0].headers["accept"] == "application/json"
    assert json.loads(calls[0].content) == {"skill_id": "s", "endpoint": "e", "args": {}, "confirm": False}


@pytest.mark.parametrize("confirm,args", [("false", {}), (1, {}), (False, [])])
def test_approval_and_arguments_validate_before_network(confirm, args):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={})

    async def scenario():
        async with FeralClient(transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(ValueError):
                await client.invoke_skill("s", "e", args, confirm=confirm)
    run(scenario())
    assert calls == []


@pytest.mark.parametrize("token", ["", " ", "fixture\r\nInjected: value"])
def test_invalid_credentials_fail_without_connection(token):
    with pytest.raises(ValueError, match="single-line credential"):
        FeralClient(bearer_token=token)


def test_bearer_is_not_emitted_in_sdk_logs(caplog):
    caplog.set_level("DEBUG")
    async def scenario():
        async with FeralClient(bearer_token="fixture-secret-never-log",
                               transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"status": "ok"}))) as client:
            await client.health()
    run(scenario())
    assert "fixture-secret-never-log" not in caplog.text


def test_redirect_is_reported_without_resending_credentials():
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(307, headers={"Location": "https://other.invalid/"})

    async def scenario():
        async with FeralClient(bearer_token="fixture-bearer", transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(httpx.HTTPStatusError) as error:
                await client.invoke_skill("s", "e")
            assert error.value.response.status_code == 307
    run(scenario())
    assert len(calls) == 1


def test_cancellation_propagates_without_replay():
    calls = []

    async def scenario():
        entered = asyncio.Event()

        async def respond(request):
            calls.append(request)
            entered.set()
            await asyncio.Event().wait()

        async with FeralClient(transport=httpx.MockTransport(respond)) as client:
            task = asyncio.create_task(client.invoke_skill("s", "e"))
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    run(scenario())
    assert len(calls) == 1
