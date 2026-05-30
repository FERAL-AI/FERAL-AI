"""HTTP-surface tests for the MCP JSON-RPC and management routes.

These tests guard the wiring between ``feral-core/api/routes/mcp.py`` and
``mcp.server.FeralMCPServer``. They complement ``test_mcp_full.py`` which
covers the JSON-RPC protocol logic and ``FeralMCPServer.get_http_routes``.

Required tests:

* ``test_mcp_routes_listed_in_openapi`` — every route registered under
  ``/mcp*`` appears in the OpenAPI schema served at ``/openapi.json``.
* ``test_mcp_endpoint_smoke_post`` — POSTing a minimal JSON-RPC 2.0
  envelope to ``/mcp`` returns 200 plus a JSON-RPC envelope with the
  caller's ``id`` echoed back.
"""

from __future__ import annotations

from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _mcp_route_paths(app: FastAPI) -> set[str]:
    """Return the set of MCP-related paths registered on ``app``.

    A path is considered MCP-related when ``"mcp"`` appears anywhere in
    the route path (covers both ``/mcp`` and ``/api/mcp/...``).
    """
    paths: set[str] = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        if path and "mcp" in path:
            paths.add(path)
    return paths


class TestMCPRouteSurface:
    """Routes from ``api.routes.mcp`` must be discoverable via OpenAPI."""

    def test_mcp_routes_listed_in_openapi(self) -> None:
        """Every ``/mcp*`` route registered on the app appears in /openapi.json.

        The OpenAPI document is the contract clients (Claude Desktop,
        Cursor, third-party MCP consumers) read to discover endpoints.
        Silent route removal would break those clients without any test
        signal — this guard catches that class of regression early.
        """
        from api.routes.mcp import router as mcp_router

        app = FastAPI()
        app.include_router(mcp_router)

        registered_paths = _mcp_route_paths(app)
        assert registered_paths, (
            "Expected at least one /mcp* route to be registered on the app; "
            "got none. The MCP router may have been emptied."
        )

        client = TestClient(app)
        schema_response = client.get("/openapi.json")
        assert schema_response.status_code == 200, (
            f"/openapi.json should be served; got HTTP "
            f"{schema_response.status_code}."
        )
        schema = schema_response.json()
        schema_paths = set(schema.get("paths", {}).keys())

        missing = registered_paths - schema_paths
        assert not missing, (
            "The following MCP routes are registered but absent from the "
            f"OpenAPI schema: {sorted(missing)}. "
            "Either restore them in api/routes/mcp.py or stop registering them."
        )

        # Sanity assertions on the canonical surface contract.
        assert "/mcp" in schema_paths, (
            "The JSON-RPC entrypoint /mcp must be in the OpenAPI schema."
        )
        for expected in ("/api/mcp/status", "/api/mcp/tools", "/api/mcp/registry", "/api/mcp/connect"):
            assert expected in schema_paths, (
                f"Expected MCP management route {expected!r} in OpenAPI schema; "
                f"got: {sorted(schema_paths)}"
            )


class TestMCPEndpointSmoke:
    """End-to-end smoke checks for the ``/mcp`` JSON-RPC entrypoint."""

    def test_mcp_endpoint_smoke_post(self) -> None:
        """POST a minimal JSON-RPC envelope to /mcp; assert id round-trip.

        The MCP JSON-RPC contract requires every response to:
        * include ``"jsonrpc": "2.0"``,
        * echo the original request's ``id`` field,
        * carry either a ``result`` or an ``error`` (not both).

        We exercise ``tools/list`` because it is parameter-free and always
        available regardless of which optional subsystems (devices,
        memory, perception) are configured.
        """
        from mcp.server import FeralMCPServer
        from api.routes.mcp import router as mcp_router

        app = FastAPI()
        app.include_router(mcp_router)

        # Build a real MCPServer with no optional dependencies; tools/list
        # works with the bare-minimum object and exercises real code paths.
        server = FeralMCPServer()

        class _StubState:
            def __init__(self, mcp_server: FeralMCPServer) -> None:
                self.mcp_server = mcp_server
                self.mcp_client = None

        stub_state = _StubState(server)

        envelope = {
            "jsonrpc": "2.0",
            "id": "smoke-42",
            "method": "tools/list",
            "params": {},
        }

        with patch("api.routes.mcp.state", stub_state):
            client = TestClient(app)
            response = client.post("/mcp", json=envelope)

        assert response.status_code == 200, (
            f"/mcp should respond 200 to a valid JSON-RPC POST; "
            f"got {response.status_code}: {response.text!r}"
        )
        body = response.json()
        assert body.get("jsonrpc") == "2.0", (
            f"Response must declare jsonrpc=2.0; got {body!r}"
        )
        assert body.get("id") == "smoke-42", (
            f"Response must echo the request id; got {body.get('id')!r}"
        )
        # Either error or result, but for tools/list we expect result.
        assert body.get("error") in (None, {}), (
            f"tools/list should not return an error envelope; got {body!r}"
        )
        assert "result" in body, (
            f"tools/list should populate 'result'; got {body!r}"
        )
        assert "tools" in body["result"], (
            f"tools/list result must contain a 'tools' array; got {body['result']!r}"
        )
        assert isinstance(body["result"]["tools"], list)

    def test_mcp_endpoint_returns_jsonrpc_error_when_server_missing(self) -> None:
        """When ``state.mcp_server`` is None, /mcp must still answer JSON-RPC.

        This guards the explicit ``-32603`` fallback path in
        ``api/routes/mcp.py`` — clients should always get a structured
        envelope, never an HTTP 5xx, even when the brain hasn't booted
        the MCP subsystem.
        """
        from api.routes.mcp import router as mcp_router

        app = FastAPI()
        app.include_router(mcp_router)

        class _EmptyState:
            mcp_server = None
            mcp_client = None

        envelope = {
            "jsonrpc": "2.0",
            "id": "missing-server",
            "method": "tools/list",
            "params": {},
        }

        with patch("api.routes.mcp.state", _EmptyState()):
            client = TestClient(app)
            response = client.post("/mcp", json=envelope)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["jsonrpc"] == "2.0"
        assert body["id"] == "missing-server"
        assert body["error"]["code"] == -32603
